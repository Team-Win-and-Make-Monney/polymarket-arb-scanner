# Trade-ledger mirror and venue reconciliation: rollout plan

**Status (2026-09-29):** plan only. Nothing here has been done. Each step needs its own operator approval at the time it is taken. That covers merges, deploys, migrations, roles, grants, credentials, Railway variables, volumes and provider settings. No step places, cancels or modifies orders. No step changes trading limits, DRY_RUN or kill switches.

Design and claim rules are in [`LEDGER-REPORTING.md`](LEDGER-REPORTING.md).

## Targets (observed read-only on 2026-09-29; re-verify at each gate)

| What | Identifier | Observed | Source |
| --- | --- | --- | --- |
| GitHub repo | `tamm-labs/polymarket-arb-scanner` (formerly `Team-Win-and-Make-Monney/polymarket-arb-scanner`), default branch `master` | old name redirects; PR heads unchanged | `gh repo view`, Jonathon |
| Railway project | `66c8da70-55d7-4dbc-b84b-c200a018dc05`, environment `production` `b68bb4ec-165c-45b3-90f2-4ae798cee5d4` | — | Railway, Jonathon |
| Service `arb-scanner` | `b07b9dc6-0cab-4e96-8cbc-bfb4ea4fbb74`, deployment `713b965b-7f3b-485d-891e-7668136cf666` | SUCCESS, `/data` volume attached | Railway, Jonathon |
| Service `kalshi-mm-pilot` | `7b983a5a-2977-4e38-bb6c-4fe0f3ac2896`, deployment `d656e7c8-c2a3-407a-ab5a-a8efc04005d8` | SUCCESS, **no volume listed** | Railway, Jonathon |
| Railway variables | both services list `DRY_RUN` and `SUPABASE_URL`; no `LEDGER_*` flags | values redacted, so the binding is **unverified** | Railway, Jonathon |
| Supabase project | `financial-markets-rewards`, ref `rtvusfddepldnpknqpjt`, PostgreSQL 17.6 | ACTIVE_HEALTHY; `public` has opportunity, reward and broker tables; no `ledger_*` tables; no `ledger_reporting` schema | Supabase `list_tables`, Jonathon |
| Kalshi API | `https://api.elections.kalshi.com/trade-api/v2` (`kalshi_api.KALSHI_BASE_URL`) | OpenAPI 3.31.0 docs retrieved 2026-09-29 | docs.kalshi.com |

## Gates that must pass first

1. **Repository transfer.**
   - Confirm that Railway's GitHub integration for both services points at `tamm-labs/polymarket-arb-scanner`, branch `master`. A merge must still deploy, and nothing else may deploy.
   - Confirm that `master` branch protection still requires `test` and CodeRabbit, conversation resolution and no force-push.
   - Confirm that CodeRabbit, CodeQL and the Claude GitHub App are installed on the new owner.
   - Read-only checks only. Do not change the project connection or provider permissions without separate approval.
2. **Supabase binding.**
   - Confirm that both services' `SUPABASE_URL` host is `rtvusfddepldnpknqpjt.supabase.co` without printing secrets. The operator checks the host in Railway's UI, or the service logs the host at start.
   - Confirm that `SUPABASE_SERVICE_KEY` belongs to that project.
   - If either is wrong, stop.
3. **Remote migration history.**
   - Run `list_migrations` on the project and compare it with `supabase/migrations`. Per `supabase/API-DEFAULTS-2026-09-21.md`, the history is known to differ.
   - Promote the draft to `supabase/migrations/<UTC timestamp>_trade_ledger_reporting.sql` in its own PR, after reconciling with that history.
   - Re-run `tests/supabase-ledger-reporting.sh` on **PostgreSQL 17** (local evidence so far is 16.13).
4. **MM pilot durability.**
   - `kalshi-mm-pilot` has no volume, so its `trades.db` and capture state are lost on every redeploy. The mirror would see a new `db_instance_id` each time, and any unexported tail is gone.
   - Before capture is enabled there, attach a volume and point `DATA_DIR` at it (a production infra change, needing its own approval).
   - Then verify that `db_instance_id` survives one redeploy.
   - Until then, the reconciliation job marks days that span a pilot redeploy as incomplete (`ledger_mirror_source_changed` or `ledger_capture_not_covering_interval`). That is honest, but it means those days are never verified.
5. **Kalshi key scope.**
   - In Kalshi account settings, record whether the key the collector will use is restricted to a subaccount.
   - Record its fingerprint with `python scripts/reconcile_venue_fills.py --print-key-fingerprint`, run where the key is already configured.
   - Write the `LEDGER_KALSHI_SCOPE` JSON: `account_ref`, `key_fingerprint`, `subaccount` (`"all"` only if the key is verified unrestricted), `ledger_services: ["arb-scanner", "kalshi-mm-pilot"]`, `verified_by`, `verified_on`.
   - The collector only reads fills. A dedicated read-only key is preferable to reusing the trading key, but it needs a new credential (operator decision).
6. **Pilot fill schema.**
   - Kalshi's current Fill schema lists `count_fp` and no integer `count`.
   - `mm_pilot._build_event` reads `fill.get("count", 0)` and ignores fills with count 0.
   - Resolve this separately before live pilot fills are relied on. The reconciliation job would report those fills as `missing_in_ledger`.

## Rollout order (each step is approved separately)

| # | Step | Verify | Roll back |
| --- | --- | --- | --- |
| 1 | Merge #190 after review. Flags stay off. Auto-deploy restarts both services. New nullable `run_mode`/`account_ref` columns are added; writers stamp `run_mode` and the pilot stamps `fill_qty`. | `/healthz` 200 on both services; `Tests` green on `master`; logs show no `ConfigError`; no `ledger_outbox` table yet | Revert the merge. Old code ignores the new columns. |
| 2 | Apply the promoted migration to `rtvusfddepldnpknqpjt`. | As the owner: the tables have RLS enabled with no policies; `anon`/`authenticated` have no grants on them or on `ledger_reporting`; `ledger_reporting` is **not** in PostgREST's exposed schemas; `get_advisors` shows no new security findings | `drop schema ledger_reporting cascade; drop table public.ledger_trades, public.ledger_positions, public.ledger_sync_status, public.ledger_venue_reconciliations; drop function public.ledger_version_guard(), public.ledger_touch_synced_at(); drop role ledger_reporter;` (nothing else depends on them) |
| 3 | Create a login role for reporting routines: `create role ledger_report_reader login in role ledger_reporter; alter role ledger_report_reader set default_transaction_read_only = on;` Set its password outside the repository, and connect it through the pooler. | As that role: `select` from `ledger_reporting.*` works; `select` from `public.ledger_trades` fails; any `insert` fails | `drop role ledger_report_reader;` |
| 4 | Set `LEDGER_ACCOUNT_REFS={"kalshi":"<account_ref>"}` and `LEDGER_CAPTURE_ENABLED=true` on `arb-scanner`, and on `kalshi-mm-pilot` once gate 4 has passed. | `ledger_meta` has `capture_epoch`, `db_instance_id` and `capture_since`; the outbox grows with trades; no order-path latency change in logs | Set `LEDGER_CAPTURE_ENABLED=false` and restart. In maintenance, drop the six `ledger_capture_*` triggers. |
| 5 | Set `LEDGER_SYNC_ENABLED=true` on the same services. `LEDGER_SERVICE_NAME` is optional; `RAILWAY_SERVICE_NAME` is used otherwise. | `ledger_reporting.sources` shows one row per service with `mirror_complete = true` after the snapshot, and it stays true; `Trade ledger Supabase sync failed` warnings are absent or transient | Set `LEDGER_SYNC_ENABLED=false`. Mirror rows stay and are simply no longer updated. |
| 6 | Add a separate scheduled job (not inside a trading service) that runs `python scripts/reconcile_venue_fills.py --venue kalshi --write` after 00:15 America/Detroit for the previous day. It needs `LEDGER_KALSHI_SCOPE`, the Kalshi credential and the Supabase backend credential. First run it without `--write` for a few days and review the JSON. | `ledger_reporting.venue_reconciliation_days` has one row per day; reasons are explained; `matched` appears only for days whose gates all pass | Disable the schedule. Rows stay for audit, and a newer check supersedes an older one. |
| 7 | Point the daily and hourly reporting routines at the `ledger_reporting` views, using the step 3 role. | Reports show source/run identity, `last_success_at`, covered days, `incomplete_reasons` and `coverage_scope`. Zero claims only on `fills_verified` days. PnL stays unverified. | Revert the routine instructions to the null/unverified wording. |

## Still unverified or missing after this plan

- **Fees:** reconciled against the venue only as a sum. Settlements, positions and PnL are not reconciled.
- **Least privilege:** the exporter and the job still use the backend `SUPABASE_SERVICE_KEY`. A least-privilege writer role is a follow-up.
- **Other venues:** only Kalshi has a collector.
