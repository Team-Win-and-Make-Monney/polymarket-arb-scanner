# Plan 08 — Polymarket Activation (CLOB V2, shadow-only)

**Status:** SPEC — build authorized for shadow mode only. **This plan authorizes no live order,
no allowlist change, and no capital movement.**
**Authority:** Warroom decision W5 (2026-07-07) — recorded in the out-of-repo command center
(`~/Financial Markets with AI/24-STRATEGY-WARROOM-2026-07-07.md` §6 and 00-STRATEGY decision log;
not auditable from this repo alone). Polymarket execution enabled from Michigan (non-sports only),
contingent on operator API-level verification.
**Queue position:** W7 lane #1 (2026-07-07 priority queue, item #2 spec / item #4 build).
**Written:** 2026-07-09. Revised same day after adversarial review (Codex rounds 1–2).

> **⚠️ OPEN AUTHORITY CONTRADICTION (must resolve before ANY live consideration):** as of
> 2026-07-09, Polymarket's official geoblock documentation
> (`docs.polymarket.com/api-reference/geoblock`) lists the United States as **close-only** —
> existing positions may close, but new orders cannot open, on both frontend and API. This
> contradicts W5's premise ("execution enabled from MI ~mid-June", operator ground truth). A
> successful authenticated GET does NOT establish order eligibility. The [OP] verification item
> therefore must explicitly confirm **new-order-opening eligibility** for the operator's account,
> and until that confirmation exists this plan's live-activation reference material is void.
> Shadow mode (Phases A–C) is unaffected — it places no orders.

---

## 1. Context and constraints

Per W5, Polymarket became accessible from Michigan ~mid-June 2026 (non-sports; sports remains
blocked statewide), superseding the 6/12 "read-only from MI" posture — subject to the authority
contradiction flagged above. However:

- **App access ≠ API access.** API-level verification from the operator's account is an open
  [OP] item (queue #1). Verification is **read-only**: authenticated GET endpoints (balance,
  open orders) only — it must NOT place test orders, cancel orders, move funds, write secrets,
  or change routing — plus an explicit determination of new-order-opening eligibility (see the
  authority-contradiction banner). A working proxy is never treated as permission to bypass venue
  or jurisdiction restrictions; if venue policy prohibits opening orders from the US, no proxy
  configuration makes activation permissible and the activation plan is void.
- **Shadow-only must be technically enforced, not conventional.** Today `polymarket` is in the
  default `ENABLED_EXECUTION_PLATFORMS` (`config.py`), `DRY_RUN=false` + a key constructs the
  authenticated writer, and `master` auto-deploys to Railway. Phase A therefore adds a
  **default-deny Polymarket live gate** (`POLYMARKET_LIVE_ENABLED`, default `false`) enforced at
  three boundaries: config validation, client construction (shadow mode must be structurally
  unable to construct a signing client), and the executor dispatch. Flipping it is a
  money-authority change (operator 1-tap).
- **Policy broker precondition.** No agent-executed live order until broker PR #78 merges and its
  reconciliation passes tests (loop rule; doc 22). Live sizing/caps come from the broker's
  out-of-repo config — this plan does NOT define capital limits.
- **Money-authority hard-stop.** Any diff that flips the allowlist, touches capital caps, gate
  thresholds, or order-placement paths gets a 1-tap operator merge, never auto-merge.
- **Sports filter is a code gate, not prose.** Phase A adds a central executor-side veto:
  any Polymarket-routable opportunity whose market category is `sports`, missing, or unknown is
  **blocked fail-closed** before order construction, with test coverage for every
  Polymarket-routable strategy branch. **Category provenance:** today the binary, NegRisk, and
  WS-cross producers use category for fees but do not attach it to opportunity dicts — a strict
  veto would block every shadow opportunity, while trusting caller-supplied values would weaken
  the gate. Phase A therefore requires canonical category propagation (fetched from market
  metadata, normalized) from **every** producer including the WebSocket path, with
  known-non-sports positive tests proving legitimate opportunities still flow.

## 2. What already exists (do not rebuild)

| Piece | State | Where |
|---|---|---|
| Category taker-fee model (crypto 7% / sports 3% / finance-politics-mentions-tech 4% / econ-culture-weather-other 5% / geopolitics 0%; `rate·C·P·(1−P)`) | **SHIPPED** (PR #41) | `fees.py:POLYMARKET_CATEGORY_TAKER_RATES`, `polymarket_taker_fee()` |
| Maker-rebate model (25% of taker fee, 20% crypto) | **SHIPPED** (PR #47) | `fees.py:polymarket_maker_rebate()` |
| CLOB V2 client migration WIP: `py-clob-client-v2==1.0.2`, `OrderPayload`/`OrderType` (GTC/FOK map), httpx proxy injection for CLOB writes, pUSD/USDC collateral balance, dollar→shares sizing (`_dollar_size_to_contracts`), order-id persistence (`_persist_order_id`), venue-aware fill finalization, hedger + Kalshi sizing updates, test updates | **UNMERGED WIP** — preserved 2026-07-09 on **local-only** branch `wip/clob-v2-snapshot-2026-07-09` (snapshot of uncommitted work found on `fix/layer4-continuous-wiring`; not on origin) | branch `wip/clob-v2-snapshot-2026-07-09` |
| Proxy support for geoblock (`POLYMARKET_PROXY_URL`) | shipped (V1-era); V2 needs the httpx injection from the WIP | `polymarket_api.py` |

The build task is therefore: **review, harden, and land the V2 WIP as its own PR series**, not a
greenfield migration. The WIP is treated as untrusted input (see §6).

## 3. Build phases

### Phase A — Land the CLOB V2 client migration (this repo, PR-sized)
1. **WIP intake is extraction, not rebase.** Pin the snapshot to an immutable pushed
   commit/patch first (push `wip/clob-v2-snapshot-2026-07-09` to origin or archive its patch),
   then **extract only allowed hunks** onto a fresh branch off `master` — the snapshot spans
   24 files / +1,565 lines including unrelated Firecrawl, Layer-4 wiring, agent docs, and local
   settings that must NOT replay. **Acceptance explicitly rejects** the WIP's live-authority
   content: `CANARY_MODE=live` / `CANARY_LIVE_ACK`, live caps, allowlist/config defaults, and
   local settings files. Adversarial review of the resulting PR must confirm none of these
   survive.
2. Scope: `polymarket_api.py` (V2 imports, `OrderPayload`/`OrderType`, httpx proxy injection,
   pUSD collateral in `get_balance()`), `requirements.txt` (`py-clob-client-v2==1.0.2`),
   sizing in `executor.py`, order/intent persistence, the default-deny live gate and sports veto
   from §1, and matching test updates.
3. **Quantity planning must preserve arb invariants.** The WIP's per-leg
   `floor(dollars / leg_price)` conversion applied independently to each leg produces mismatched
   share counts (e.g. $5 at $0.40/$0.55 → 12 YES vs 9 NO — directional exposure, not an arb).
   Phase A replaces this with a strategy-level quantity planner: compute one target quantity per
   opportunity that preserves the payoff invariant across legs, respect inventory constraints on
   SELL legs, and add payoff-invariant tests per strategy type — not just helper boundary tests.
4. **Fill semantics.** A venue-accepted order is not a fill, and `size_matched > 0` is not a
   complete fill (the WIP currently records the full requested amount as `_fill_qty`). Track
   requested vs matched quantity per leg; handle partial fills, GTC cancel results, cancel races,
   and hedge-confirmation (hedge success = confirmed fill, not acceptance). Tests must cover
   partial-fill, residual-order, cancel-race, and hedge-confirmation paths.
5. **Crash/idempotency semantics at the venue-accept boundary.** py-clob-client-v2 exposes no
   client-order-id hook and the key is never transmitted to the venue, so local keys cannot by
   themselves resolve unknown acceptance (instant fills may not appear among open orders).
   Require a durable intent journal written before submission with a **stable fingerprint**
   (token ID, side, quantity, price — not wall-clock-derived), an explicit **UNKNOWN quarantine**
   state, and restart reconciliation against open orders and fills (extend `recovery.py`).
   **The venue enumerates open orders only** — an accepted-then-immediately-cancelled unfilled
   order with no persisted ID is unresolvable by API; such intents become **permanent UNKNOWN**
   requiring manual operator resolution, with ambiguity tests covering that case. Never
   auto-resubmit; UNKNOWN intents halt that market and alert. A failure-injection test (crash
   between venue accept and `_persist_order_id`) is part of Phase A acceptance.
   **Dead-man switch:** any use of GTC orders requires Polymarket's order heartbeat
   (`docs.polymarket.com/api-reference/trade/send-heartbeat`) so the venue auto-cancels open
   orders if the system stops responding — with startup, heartbeat-failure, shutdown, and
   restart tests. (The WIP contains WebSocket heartbeats only; this is a separate mechanism.)
6. **Review flag:** this touches order-placement paths → money-authority merge (operator 1-tap
   after CodeRabbit + Codex both clean).
7. Acceptance: full suite green ×2 fixed-seed; V1 client fully removed (V1 endpoints are dead);
   dry-run order construction round-trips against recorded fixtures; live-gate/sports-veto/
   invariant/fill-semantics tests from items 3–5 all present and green.

### Phase B — Shadow mode (no live orders)
1. Run continuous mode with `DRY_RUN=true` + `POLYMARKET_LIVE_ENABLED=false` and Polymarket scans
   enabled; every would-be order is priced with the category fee model and logged (opportunity,
   legs, fees, expected net) to `trades.db` shadow tables + snapshot recorder.
2. **Shadow-fill semantics (deterministic, conservative-taker; current dry-run records
   `status="dry_run"`, not a simulated fill — this is new logic).** A shadow order is classified
   from the decision-time book snapshot only: FILLED if visible depth at or better than the limit
   price covers the full quantity; PARTIAL (with the covered quantity) if a GTC order is covered
   in part; UNFILLED if executable depth at or better than the limit is zero (any order type) or
   a FOK is not fully coverable; UNFILLED and excluded from all gate counts when the snapshot is
   missing or stale (**stale = snapshot age at decision time exceeding the same runtime
   price-cache eviction setting the live path uses (`PRICE_CACHE_EVICTION_AGE`, default 60s)** —
   the classifier must read that setting, not hard-code a value, so the shadow and live freshness
   definitions cannot drift). No queue-position or maker-fill modeling — resting-fill assumptions are out
   of scope for shadow gating. Unit tests cover each classification branch, including
   zero-depth-GTC, exact-boundary staleness, and FOK partial-coverage.
3. Divergence logging: for each shadow fill, record book price at decision time vs T+5s re-fetch;
   summarize slippage distribution in the P&L digest (T+5s movement measures price drift, not
   execution — it feeds the slippage gate, never the fill classification).
4. Exit criteria to request live consideration [OP] — must not pass vacuously: ≥1 week shadow
   AND ≥100 shadow opportunities logged AND ≥20 shadow fills — counted **per opportunity**, an
   opportunity counts only when **every leg classifies FILLED** (PARTIALs never count toward
   this gate) — spanning ≥2 strategy types and
   ≥3 market categories (thresholds are proposals subject to operator ratification), no
   crash/recovery incidents, p95 decision-to-T+5s slippage within the strategy's modeled edge,
   shadow fee accounting matches `polymarket_taker_fee()` to the cent on sampled markets,
   read-only API verification passed, and the §authority contradiction resolved.

### Phase C — Settlement-divergence VETO gate (mandatory before any cross-venue pair could go live)
Per W5: every Polymarket↔other-venue pair must pass a resolution-criteria equivalence check.
**Enforcement point: the common final dispatch boundary** (`executor.execute()`), not scan
refinement — the WebSocket cross path (`continuous.py`/`cross_pair_index.py`) generates and
queues opportunities without passing through refinement, and `execute()` accepts plain
opportunity dicts from any caller. The repo's existing tested, TTL-enforced fail-closed veto
(`settlement_divergence.py`) is the mechanism; wire it so every producer hits it:
- Pair resolution-source/rule text (Polymarket UMA/oracle text vs counterpart venue rules);
  mismatch, missing rule text, or gate error → VETO (fail-closed), opportunity dropped and logged.
- Reuses the market-equivalence machinery in `market_discovery.py` + `matcher.py`; the LLM
  settlement-divergence gate (tracker backlog) is the eventual scorer — until it lands, the
  gate is conservative string/source matching with an explicit allowlist of manually-verified
  pairs (allowlist changes are money-authority).

## 4. Explicitly out of scope (operator-owned, NOT authorized here)
- **Live activation entirely**: the `POLYMARKET_LIVE_ENABLED` flip, the allowlist, funding
  (including the W5 $2–3K tranche), and the live scale gate (first 50 dual-venue round-trips,
  zero settlement-divergence losses, net-positive after fees). These belong to a **separate
  operator-approved activation plan** routed through the policy broker; W5's gate criteria are
  recorded here for reference only.
- API-level verification — [OP], read-only as defined in §1.
- Polymarket Liquidity Rewards quoting rotation (W6) — separate plan after MM-pilot verdict (~7/17).
- Agent-analyst judgment lane (queue #3) — separate spec `NN-agent-analyst.md`.
- Any capital/caps/risk policy — lives in the command center + broker config, not this repo.

## 5. Test plan
- Unit: V2 order construction (type map, price/size rounding, tick-size), pUSD balance parsing,
  quantity-planner payoff invariants per strategy type, sizing boundaries (0-share,
  exact-multiple, price→1 and →0 edges), live-gate default-deny at all three boundaries,
  sports/unknown-category fail-closed veto.
- Fill semantics: partial fill, residual GTC order, cancel race, hedge-confirmation-by-fill.
- Idempotency: intent-journal fingerprint stability, UNKNOWN quarantine, crash-injection at the
  venue-accept boundary, restart reconciliation against open orders + fills, permanent-UNKNOWN
  ambiguity case (accepted-then-cancelled unfilled order with no persisted ID).
- Integration: dry-run executor path per strategy branch that can route to Polymarket
  (`tests/integration/test_executor_strategies.py` extensions).
- VETO gate: matched-pair fixtures (equivalent, divergent, missing-rules, gate-error) → only
  equivalent passes.
- Regression: full suite ×2 fixed-seed before every push (loop VERIFY rule).

## 6. Risks
- **V2 WIP provenance:** the snapshot came from an unattributed working tree and includes
  live-authority scope creep (canary-live flags, caps, config defaults) — every hunk must be
  re-reviewed as if new, and the rejected content in Phase A item 1 checked for explicitly.
  The snapshot branch is local-only; losing this machine loses it.
- **Geoblock/proxy:** CLOB writes from MI depend on the httpx proxy injection; a silent bypass
  would send unproxied signed requests. The check must **fail closed on absence**: whenever the
  Polymarket write path is constructible (live gate on), require a non-empty
  `POLYMARKET_PROXY_URL` AND a startup assertion that inspects the actual CLOB write client
  (`py_clob_client_v2.http_helpers.helpers` module-level httpx client) and verifies its
  configured proxy matches — a missing env var must abort startup, not skip the check.
- **Fee drift:** category rates verified 2026-06-10; re-verify against docs.polymarket.com before
  any live consideration and record the check date in `fees.py`.
