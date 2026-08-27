# arbgrid

Python CLI for detecting and paper-evaluating prediction-market arbitrage. The repository contains multiple venue adapters and execution code, but the project is currently paused for broad expansion and is dry-run by default.

> **Current operating state (2026-08-27):** PR #134 is merged and Railway is running that exact commit as a healthy Kalshi-only dry-run worker. Draft PR #135 contains the remaining audit reconciliation and is not merged or deployed. The only verified live operating target remains the separately gated Kalshi D0 launcher. No generic multi-venue live command is authorized. See [`docs/STATE.md`](docs/STATE.md).
>
> Renamed from `polymarket-arb-scanner` on 2026-05-09. The new name reflects the actual scope — a grid of platforms × layers × strategies, not a Polymarket-only scanner. Local clones may keep the old directory name without any functional impact.

## What It Does

`arbgrid` can inspect multiple venues for mispricings, low-risk edges, and market-making signals. It supports:

- **One-shot scans** — detect opportunities across all enabled strategies and exit.
- **Continuous mode** — long-running loop with WebSocket feeds, opportunity index, and per-market locking.
- **Gated execution code** — dry-run by default; current live readiness is Kalshi D0 only and requires the operator envelope and action-time approval.
- **Backtesting** — replay engine over recorded price snapshots.

### Platforms

| Role | Platforms |
|---|---|
| Adapter-backed detection | Polymarket, Kalshi, Betfair, Smarkets, SX Bet, Matchbook, Gemini Predictions, IBKR ForecastEx |
| Verified live target | Kalshi D0 only, through `scripts/launch-kalshi-d0-live.sh` |
| Signal-only (read) | Metaculus, Manifold |

### Strategy Coverage

29 strategies across 5 risk layers. Canonical taxonomy and status table live in [`docs/strategy-framework-v2.md`](docs/strategy-framework-v2.md). Summary:

| Layer | Theme | Strategies |
|---|---|---|
| 1 — Pure Arbitrage | Risk-free; same-platform overrounds, cross-platform 2-way, multi-outcome, triangular, crossed-book | 6 |
| 2 — Near-Arbitrage | Resolution sniping, stale-price exploitation, fee promotional arbitrage | 3 |
| 3 — Market Making | Passive MM, cross-platform MM, inventory-hedged MM, liquidity rewards farming | 5 |
| 4 — Informed Trading | Event divergence, cross-platform convergence, multi-source signal aggregation | 9 |
| 5 — Capital Optimization | Kelly sizing, fee routing, rebalancing, latency, backtesting-driven tuning | 6 |

The strategy taxonomy is an implementation inventory, not a live-readiness claim. Each strategy still requires executable-book, settlement, eligibility, and operating-window validation before capital use.

## Quick Start

```bash
# Install
pip install -r requirements.txt
pip install -r requirements-dev.txt   # pytest, dev tools

# One-shot scan (all strategies, dry-run)
python scanner.py

# Continuous mode
python scanner.py --continuous --interval 60

# Single strategy
python scanner.py --mode kalshi
python scanner.py --mode cross-all
python scanner.py --mode mm

# Live Kalshi D0 only; do not run until the five-field operator envelope,
# account/credential/jurisdiction checks, physical kill switch, and
# action-time approval are all present.
scripts/launch-kalshi-d0-live.sh
```

Available `--mode` values (authoritative list = `cli.py` argparse choices; `all` runs everything): `binary`, `negrisk`, `cross`, `kalshi`, `cross-all`, `spread`, `betfair`, `smarkets`, `sxbet`, `matchbook`, `gemini`, `ibkr`, `event`, `triangular`, `nway`, `multi-cross`, `stale`, `resolution`, `convergence`, `mm`, `rewards`, `imbalance`, `news-snipe`, `correlated`, `time-decay`, `logical-arb`, `whale-copy`, `fee-promo`, `cross-mm`, `lead-lag-mm`, `toxic-flow`, `vol-mm`.

## Architecture

Three layers plus a thin orchestration shell. `scanner.py` is a **facade** — it re-exports from the real implementation modules so `import scanner` keeps working for tests and backward compatibility. The real entry point is `cli.py:main()`.

```
CLI (scanner.py facade) → cli.py → _run_oneshot()  or  continuous.py:run_continuous()
                              │
              ┌───────────────┼───────────────┐
              ▼               ▼               ▼
          API Layer      Scan Layer      Execution Layer
         (*_api.py)      (scans/)       (executor.py,
          ws_feeds.py                    risk_manager.py,
                                         db.py)
```

- **API layer** (`*_api.py`, `ws_feeds.py`) — platform clients with auth, retries, proxies.
- **Scan layer** (`scans/`) — two-stage detection: mid-price scan (fast) → CLOB refinement (accurate).
- **Execution layer** (`executor.py`, `risk_manager.py`, `db.py`) — risk gates, dynamic sizing, fill confirmation, SQLite persistence.
- **Orchestration** (`cli.py`, `continuous.py`, `display.py`) — argument parsing, parallel fetch/scan via `ThreadPoolExecutor`, WebSocket-driven re-execution.

Full module map: [`CODEBASE-INVENTORY.md`](CODEBASE-INVENTORY.md). Detailed conventions and patterns: [`CLAUDE.md`](CLAUDE.md).

## Configuration

All settings are env vars with defaults defined in `config.py`. `validate_config()` runs at import time and refuses to start if anything required is missing.

Key groups:

- **Platform credentials** — `POLYMARKET_PRIVATE_KEY`, `KALSHI_API_KEY_ID` + `KALSHI_PRIVATE_KEY_PATH`, `BETFAIR_*`, `SMARKETS_API_KEY`, `SXBET_API_KEY`, `MATCHBOOK_USERNAME`/`MATCHBOOK_PASSWORD`, `GEMINI_API_KEY`/`GEMINI_API_SECRET`, explicit `IBKR_HOST`/`IBKR_PORT`/`IBKR_CLIENT_ID`, and `METACULUS_API_KEY` plus `METACULUS_COMMERCIAL_USE_APPROVED=true` only after written commercial API permission.
- **Execution** — `DRY_RUN` (default `true`), `EXECUTION_MODE`, `MAX_TRADE_SIZE`. `DRY_RUN=false` is not sufficient authority by itself.
- **Risk** — `DAILY_LOSS_LIMIT`, `MAX_OPEN_POSITIONS`, `MIN_LIQUIDITY`, `MIN_NET_ROI`.
- **Feature flags** (default `false`) — `MM_ENABLED`, `SNAPSHOT_ENABLED`, `DYNAMIC_FEE_ENABLED`, `EVENT_MONITOR_ENABLED`, `MM_AUTO_HEDGE_ENABLED`, `FEE_PROMO_ENABLED`, `CROSS_MM_ENABLED`, `AUTO_REBALANCE_ENABLED`.
- **Infra** — `WEBHOOK_URL`, `DASHBOARD_PORT`, `DASHBOARD_HOST`, `DASHBOARD_PASS`, `DATA_DIR`, `LOG_LEVEL`, `LOG_FILE`.

Production deploys should set the feature flags explicitly. Full env-var reference is in [`CLAUDE.md`](CLAUDE.md#environment-variables).

## Testing

```bash
pytest tests/ -v                                        # full suite
pytest tests/test_fees.py -v                            # one file
pytest tests/test_executor.py::TestExecutor -v          # one class
```

Tests use `pytest` + `unittest.mock`. External SDKs are mocked via `sys.modules` stubs. No `conftest.py` — per-file `autouse` fixtures provide shared setup.

CI runs correctness lint plus `pytest` on every PR to `master` (Python 3.12) and fails on any test failure or collection crash.

## Deployment

- **Railway** auto-deploys on push to `master` via GitHub integration. Dockerfile-based build (`python:3.12-slim`).
- Default container worker: `python scanner.py --continuous --mode kalshi` in dry-run. Broad `all` scans remain an explicit local choice, not the unattended Railway default.
- Health check: `/healthz` on port 8080.
- Persistent state (`trades.db`) lives under `DATA_DIR`.

IBKR connectivity requires a reachable IB Gateway socket — not viable from Railway without a persistent gateway host.

## Project Status

- Broad multi-venue expansion is parked while safety and reproducibility work is consolidated.
- PR #134 is merged on `master`; its Railway deployment is healthy, Kalshi-only, and dry-run.
- The remaining audit reconciliation is published as draft PR #135; it is not merged or deployed.
- Railway still runs both `arb-scanner` and the legacy `polymarket-egress-proxy`, so the parked project is not resource-free.
- Verified live readiness: Kalshi D0 only, subject to all operator gates and separate action-time approval.
- This is a personal trading tool. **Out of scope:** public-facing product, SaaS, user accounts, selling access.

## Further Reading

| Document | What's in it |
|---|---|
| [`CLAUDE.md`](CLAUDE.md) | Canonical project guide — overview, architecture, env vars, code style, patterns, agent notes. |
| [`docs/strategy-framework-v2.md`](docs/strategy-framework-v2.md) | Authoritative 29-strategy / 5-layer reconciliation with per-strategy status. |
| [`CODEBASE-INVENTORY.md`](CODEBASE-INVENTORY.md) | File-by-file map of modules, functions, classes, and cross-references. |
| [`AGENTS.md`](AGENTS.md) | Notes for AI agents working on the codebase. |
| [`docs/PRD.md`](docs/PRD.md) | Product requirements — problem, users, definition of done, scope/non-goals. |
| [`docs/ROADMAP.md`](docs/ROADMAP.md) | Now / Next / Later sequencing + the path to the done milestone. |
| [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) | System design — layers, data flow, invariants, deploy topology. |
| [`docs/PLATFORM-MATRIX.md`](docs/PLATFORM-MATRIX.md) | **Canonical** platform capability / auth / fee / custody matrix. |
| [`docs/RISK-POLICY.md`](docs/RISK-POLICY.md) | Risk gates, limits, circuit breakers, kill/pause. |
| [`docs/RUNBOOK.md`](docs/RUNBOOK.md) | Deploy, post-deploy checklist, observability contract, rollback. |
| [`docs/BACKTESTING.md`](docs/BACKTESTING.md) | Snapshot → replay → tuning (#20) methodology. |
| [`docs/SECURITY.md`](docs/SECURITY.md) | Secrets, custody-grade keys, dashboard exposure (ENFORCED vs TODO). |
| [`docs/AUDIT-RECONCILIATION-2026-08-26.md`](docs/AUDIT-RECONCILIATION-2026-08-26.md) | Disposition of PR #130's 125 findings and the completion review. |
| [`TASK_CONTRACT.md`](TASK_CONTRACT.md) | Definition of done, opportunity-dict schema, execution legs, exceptions. |
| [`CONTRIBUTING.md`](CONTRIBUTING.md) | Branching, style, testing, how to add a strategy. |
| [`CHANGELOG.md`](CHANGELOG.md) | Release history (Keep a Changelog). |
| [`docs/PLATFORM-RECOMMENDATION.md`](docs/PLATFORM-RECOMMENDATION.md) | Platform-expansion decision memo — ranked candidates + greenlight gates. |
| [`docs/audit/`](docs/audit/) | Content-audit register + platform-research evidence artifacts. |
