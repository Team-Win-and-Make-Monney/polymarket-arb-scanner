# Operations Runbook

> **Owner:** Jonathon Tamm · **Review cadence:** monthly, and after any deploy incident.

## Deploy (Railway)
- **Current state:** the observed Railway worker is dry-run. The project is paused for broad expansion, but the worker still consumes resources until an operator stops it.
- Railway auto-deploys on **push to `master`** via GitHub integration (Docker, `python:3.12-slim`). Entrypoint: `python scanner.py --continuous --mode kalshi`.
- **A push to `master` restarts the live worker even for docs-only changes.** Batch changes and merge in a controlled window; avoid merging while opportunities are mid-execution.
- Required dry-run production env vars:
  - `DASHBOARD_HOST=0.0.0.0` (Railway edge healthcheck must reach the container)
  - `DASHBOARD_PASS=<strong secret>` (required whenever host is non-loopback — `validate_config()` enforces)
  - Feature flags as desired: `MM_ENABLED`, `SNAPSHOT_ENABLED`, `DYNAMIC_FEE_ENABLED`, `EVENT_MONITOR_ENABLED` (all default false)
  - All platform credentials (see `PLATFORM-MATRIX.md`)
- Health check: `GET /healthz` on 8080. The Dockerfile healthcheck reads Railway's `PORT` before falling back to 8080 (PR #19).
- Persistent state: `DATA_DIR` holds `trades.db` + `snapshots.db`.
- The primary container runs as the unprivileged `scanner` user. The separate SX Bet proxy requires `SXBET_PROXY_TOKEN` (32+ URL-safe characters), accepts only authenticated GET requests, and strips caller `Authorization` before proxying.
- IBKR needs a reachable IB Gateway socket — not viable from Railway without a persistent gateway host.

## Live Kalshi D0 gate

Generic `--exec-mode full-auto` is disabled and is not an approved runbook. The only verified live path is `scripts/launch-kalshi-d0-live.sh`. Before invoking it, verify the account and credentials, current jurisdiction/product eligibility, limits, physical emergency stop, and one operator message containing all five envelope fields: `venue`, `pair`, `max_notional_usd`, `max_daily_loss_usd`, and `kill_switch`. The envelope bounds both aggregate resting/inventory notional and UTC-day realized loss. Obtain separate action-time approval immediately before launch.

## Post-deploy checklist (run before trusting live trading)
1. `/healthz` returns 200.
2. Dashboard `/status` shows the continuous loop alive.
3. WS feeds connected (Polymarket + Kalshi).
4. Exactly one worker (no duplicate).
5. Balances / open orders reconciled by `recovery.py:reconcile_orphaned_positions()` (runs on startup).
6. `validate_config()` passed (startup log) and execution flags match intent.
7. Authenticated dashboard JSON pause/resume works, and pausing causes the MM pilot to cancel resting quotes.

## Safe feature-flag enablement
Enable one research flag at a time with `DRY_RUN=true` and watch a full cycle. Feature flags and credentials do not authorize `DRY_RUN=false`.

## Observability acceptance contract
Per failure mode: current signal, alert, owner. **Gaps are marked** — they feed `ROADMAP.md`, not silently omitted.

| Failure mode | Log/metric | Alert (`AlertType`) | Owner | Status |
|---|---|---|---|---|
| Daily loss breach | `metrics` P&L + log | `DAILY_LOSS_LIMIT` (CRITICAL) | JT | ✅ |
| Loss streak / spike | log | `LOSS_STREAK`, `LOSS_SPIKE` | JT | ✅ |
| Position limit | log | `POSITION_LIMIT` | JT | ✅ |
| WS reconnect loop | log | `WS_DISCONNECT` | JT | ✅ alert exists; **reconnect-loop threshold tuning = gap** |
| Order reject / partial fill | executor log | `EXECUTION_FAILURE` | JT | ✅ alert; **per-partial-fill metric = gap** |
| Scan failure | log | `SCAN_FAILURE` | JT | ✅ |
| Auth / rate-limit failure | log | `CREDENTIAL_FAILURE` | JT | ⚠️ rate-limit (429) has no dedicated alert — **gap** |
| Low balance | log | `BALANCE_LOW` | JT | ✅ |
| Zero-opp period | log | `ZERO_OPP[_PERIOD]` | JT | ✅ |
| DB write failure | exception → Sentry | — | JT | ⚠️ **no dedicated alert — gap** |
| Process crash | Sentry (PR #26) | — | JT | ✅ Sentry; **no uptime/heartbeat alert — gap** |
| P&L / daily-loss breaker trip | `alerting.py` | `DAILY_LOSS_LIMIT` | JT | ✅ |
| Kill-switch / DRY_RUN flip | startup log | — | JT | ⚠️ **no explicit audit event — gap** |

Metrics are exposed Prometheus-style (`metrics.py:MetricsCollector`).

## Rollback runbook (bad deploy)
1. **Stop the bleeding:** use the physical/operator kill switch first, then set `DRY_RUN=true` in Railway and redeploy.
2. **Pause/cancel pending orders:** check dashboard / `trades.db`; cancel open orders on the affected platform(s) via their console or client.
3. **Revert code:** `git revert <bad-commit>` (or redeploy the prior known-good commit) to `master`.
4. **Reconcile positions:** restart triggers `recovery.py:reconcile_orphaned_positions()`; verify open positions in `trades.db` match each platform's actual positions.
5. **Env snapshot:** keep a copy of Railway env vars before any window so you can restore exact state.
6. Confirm via the post-deploy checklist before re-enabling live trading.
