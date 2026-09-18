# 09 — Out-of-Band Policy Broker

**Status:** FOUNDATION MERGED; operational runtime is BUILD + PR only
**Owner:** command center (spec) → this repo (build) · **Blocks:** all auto-flip / auto-capital / auto-secret authority
**Related:** `21-VENUE-STRATEGY-ADOPTION-FRAMEWORK.md`, `01-EXECUTION-TRACKER.md`, `13-SECRETS-AND-INTEGRATIONS-MAP.md` (command center) · `config.py:143` (`ENABLED_EXECUTION_PLATFORMS`), `executor.py` per-leg allowlist checks

## 0. Why

The autonomous loop can auto-merge this repo AND (once authorized) flip lanes / move capital. Without an
out-of-band control, a flawed or gamed auto-merge could widen a cap or edit the venue allowlist, and the same
loop would then trade on the weakened rule — an in-band guardrail defeating itself.

**Fix — loop proposes, broker disposes.** The loop only writes an *intent* (flip lane X / move $Y A→B /
rotate secret Z) to an append-only queue. A separate **deterministic** broker, whose policy config lives
**outside this repo**, validates every intent against caps + allowlist + kill-state + live reconciliation,
then executes. Merging your own code can never change what you're allowed to do with real money.

The broker is CODE, not an AI agent: **no LLM anywhere in it** (consistent with "no LLM in any hot path").
It enforces a pre-registered rulebook; it does not reason. Reviewers (CodeRabbit + Codex) find defects
pre-merge; the operator approves hard-stops; the broker is the sole deterministic enforcer in the money path.

## 1. Architecture (as built)

```text
loop (proposer) ──writes Intent(idempotency_key)──▶ IntentQueue (append-only SQLite,
                                                     UPDATE/DELETE blocked by triggers)
                                                          │
                                                          ▼
                                             PolicyBroker (deterministic)
                                             policy config OUTSIDE this repo
                                             • caps (T1 $8K principal + realized P&L)
                                             • venue allowlist (sportsbooks never)
                                             • pre-registered gate-config hashes
                                             • kill-state
                                             • live ledger-vs-venue reconciliation
                                             • freshness TTLs / heartbeats
                                             • cooldown / micro-entry / dry-run halt
                                                          │ any check fails / in-doubt
                                                          ▼
                                             REJECTED or IN_DOUBT or HARD_STOP
                                             + escalate (Slack via notifier) — never retry
```

### Modules

| File | Contents |
|---|---|
| `broker/policy.py` | `PolicyConfig` loader (fail-closed; **refuses any config path inside this repo**), `compute_gate_hash()` (canonical-JSON SHA-256) |
| `broker/queue.py` | `Intent` dataclass, `IntentQueue` — append-only SQLite (WAL + thread lock, per `db.py` pattern); idempotency-key dedupe (repeat = no-op); event-sourced status; halt ledger (`halt`/`clear` events, clear requires an operator name); single-writer lease (mutable `leases` table) |
| `broker/supabase_queue.py` | `SupabaseIntentQueue` — same interface over Supabase/PostgREST; append-only + dedupe + lease enforced in Postgres (see Backends below). Reads `SUPABASE_URL` + service-role key from env; never logs the key |
| `broker/validator.py` | `LiveSources` (injected callables — the broker never trusts stale state), `BrokerValidator` — the deterministic rulebook; every live-source exception ⇒ fail-closed |
| `broker/secrets.py` | `rotate_secret_via_stdin()` — secret piped get-cmd → set-cmd stdin; value never decoded, logged, or returned (never enters LLM context) |
| `broker/broker.py` | `PolicyBroker.process(intent)` orchestrator: dedupe → hard-stop screen → validate → execute → verify; statuses `EXECUTED` / `REJECTED` / `IN_DOUBT` / `HARD_STOP`; escalation callable |
| `broker/adapters.py` | Concrete out-of-repo authority-snapshot and command-executor adapters; rejects in-repo/symlinked control files and never invokes a shell |
| `broker/worker.py` | Single-writer worker: lease acquire/renew/release, full reconciliation preflight, bounded pending-intent drain, escalation delivery |

### Backends (both implement the same interface)

The broker/validator depend only on the queue interface, never on the backing store. Two implementations:

- **`IntentQueue` (SQLite)** — append-only enforced by `RAISE(ABORT)` triggers on UPDATE/DELETE; used for
  tests and single-host/offline runs. CI needs no network.
- **`SupabaseIntentQueue` (Postgres/PostgREST)** — spec 22's named host, for live authority: shared source
  of truth across Railway + local sessions, plus the single-writer lease. Append-only is enforced in
  Postgres (`BEFORE UPDATE/DELETE` **and** `BEFORE TRUNCATE` triggers — blocks PATCH/DELETE via PostgREST
  too); idempotency dedupe + content-mismatch rejection and the lease are `SECURITY DEFINER` functions
  (`broker_submit_intent`, `broker_acquire_lease`/`renew`/`release`) so concurrency-safety lives in the DB;
  RLS is deny-by-default so only the service role can touch the tables.

Project: `financial-markets-rewards` (`rtvusfddepldnpknqpjt`, Lane A). Migrations (additive-only):
`supabase/migrations/0005_policy_broker.sql` (not applied by this PR). Tables: `public.broker_intents`,
`broker_intent_events`, `broker_halts`, `broker_leases`.

**Env (server-side only, never logged):** `SUPABASE_URL`, `SUPABASE_SERVICE_ROLE_KEY` (or `SUPABASE_KEY`).

**Tests.** `tests/test_broker_queue_contract.py` is one backend-parametrized contract suite: it runs against
SQLite always, and against **live Supabase** when the env vars are set (else that leg skips — CI stays
green). `tests/test_broker_supabase.py` proves the REST/RPC wiring with a mocked session (no network). The
DB guarantees themselves were verified directly on Postgres via the migration + an assertion block.
Run the live leg: `SUPABASE_URL=… SUPABASE_SERVICE_ROLE_KEY=… pytest tests/test_broker_queue_contract.py -q`.

## 2. Policy config — OUTSIDE the repo (non-negotiable)

- Path: `$BROKER_POLICY_PATH`, default `~/Financial Markets with AI/policy/broker-policy.json`.
- `load_policy()` **raises `PolicyError` if the resolved path is inside this repo** — so a merge to this
  repo cannot alter the policy the broker enforces, and cannot smuggle a config file in.
- Fail-closed on: missing file, unreadable file, malformed JSON, missing/invalid required keys.
- Required keys: `tranche`, `principal_cap_usd`, `per_market_cap_usd`, `venue_allowlist`,
  `sportsbook_venues`, `gate_hashes`, `kill_state`, `cooldown_seconds`, `freshness_ttl_seconds`,
  `recon_tolerance_usd`, `micro_entry{max_first_order_usd,first_n_fills,max_fill_deviation_pct}`.

## 3. Intent types

1. `flip_lane` — `{lane, venue, action: enable|disable}`
2. `move_capital` — `{amount_usd, from_venue, to_venue, [market, book_depth_usd], [tranche_advance]}`
3. `rotate_secret` — `{venue, secret_name}`

Every intent carries an `idempotency_key`; a repeat submit is a no-op (returns the prior outcome, executor
is never re-invoked).

## 4. Validation rulebook (ALL must pass, fail-closed)

| Rule | Applies to | Pass condition |
|---|---|---|
| Caps | move_capital | live portfolio ≤ working ceiling (**T1 principal + realized net P&L**; new principal is operator-gated ⇒ `principal` source rejected); amount > 0; amount ≤ ceiling; if market-scoped: ≤ `$300/market` cap AND ≤ supplied book depth (missing depth ⇒ fail) |
| Allowlist | all | every venue on `venue_allowlist`; any sportsbook venue ⇒ always fail |
| Gate-config hash | all | live hash of every registered gate config == pre-registered hash (detects a merged threshold edit) |
| Kill-state clear | all | no global kill, no lane in kill halt |
| Live reconciliation | move_capital | ledger vs live venue balances within tolerance; **a break records a `capital_moves` halt** (all further moves blocked until operator clears) |
| Freshness | all | every gate input + heartbeat age ≤ TTL; empty/unreadable ⇒ fail |
| Cooldown | flip_lane | no other lane flipped within cooldown window |
| Kill-switch dry-run | flip_lane enable | dry-run halt succeeds BEFORE first order |
| Micro-entry | flip_lane enable | valid micro-entry directive attached (micro size, first-N fill deviation auto-halt params) |

## 5. Hard-stops (broker halts + escalates; loop never self-proceeds)

- restart of a lane after a kill-switch halt (flip-enable on a halted lane)
- tranche advance (`tranche_advance` flag)
- reconciliation break ⇒ halt ALL capital moves
- in-doubt capital move / unverifiable secret rotation ⇒ `IN_DOUBT`, never retry
- 2FA/KYC wall (`TwoFactorWallError`) ⇒ escalate, never bypass
- money-authority merges (allowlist / caps / gate thresholds / order paths / broker itself) — enforced at
  the PR gate per spec 22 §Distinct roles; this PR itself is one and does NOT auto-merge

## 6. Definition of Done → test map (`tests/test_broker_*.py`)

1. Queue append-only + idempotency dedupe → `test_broker_queue.py`
2. Every rule: passing + failing (fail-closed) test → `test_broker_validator.py`
3. Recon break halts all capital moves → validator + `test_broker.py` end-to-end
4. Gate-hash mismatch (simulated merged threshold edit) blocks intent → validator tests
5. Policy config demonstrably outside repo (in-repo path refused) → `test_broker_policy.py`
6. Kill-switch dry-run before first order → validator tests
7. Full suite green ×2 fixed-seed; secret + egress scan clean
8. CodeRabbit CLI + PR approval + Codex adversarial pass; PR hard-stops to operator (money-authority)

Until 1–8 are all green, the loop stays **BUILD+PR only** — no live flips, no capital moves.

Operational deployment and account-side authority remain separately operator-gated; see
`docs/plans/10-policy-broker-operations.md`.
