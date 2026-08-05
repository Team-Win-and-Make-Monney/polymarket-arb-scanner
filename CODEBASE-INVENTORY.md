# Codebase Inventory — arbgrid

> Generated 2026-04-12 by 10 parallel analysis agents. Covers every file, function, class, and cross-reference in the project.
>
> **Note (2026-05-09):** Project renamed from `polymarket-arb-scanner` to `arbgrid`. PR #10 added 4 new opportunity types (`FeePromo`, `CrossPlatformMM`, `transfers` audit) — this inventory pre-dates that work; refresh when convenient. Canonical strategy taxonomy is in [`docs/strategy-framework-v2.md`](docs/strategy-framework-v2.md).

---

## Quick Stats

| Metric | Value |
|--------|-------|
| Production Python files | 48 |
| Production lines of code | 24,390 |
| Test files | 69 |
| Test lines of code | ~21,900 |
| Total lines of code | ~46,300 |
| Test methods | 1,946 |
| Test classes | 379 |
| Platforms integrated | 8 trading + 2 signal sources |
| Opportunity types | 26 |
| Strategy layers | 5 |
| CLI scan modes | 23 |
| Environment variables | 100+ |
| Planning phases | 9/9 complete |
| Database tables | 5 + 1 (snapshots) |
| HTTP dashboard endpoints | 24 |
| Dead code functions | 7 confirmed |
| Security issues | 2 critical |

---

## Architecture at a Glance

```
CLI (scanner.py facade) --> cli.py --> _run_oneshot() or continuous.py:run_continuous()
                                |
                    +-----------+-----------+
                    |                       |
             One-Shot Mode           Continuous Mode
          (ThreadPoolExecutor)     (asyncio + WebSocket)
                    |                       |
        +-----------+----------+    +-------+--------+
        |           |          |    |       |        |
   Parallel    Sequential   Platform  WS Feeds  Price Cache
    Scans     Cross-Scans   Scans   (3 feeds)   (60s TTL)
        |           |          |        |
        +-----+-----+----+----+    OpportunityIndex
              |           |              |
         Sort by     Advanced       WS-Triggered
        Cap.Eff.    Strategies       Execution
              |           |              |
              +-----+-----+--------------+
                    |
              executor.py
         (revalidate -> risk -> size -> build legs -> execute)
                    |
              +-----+-----+
              |           |
          Platform    PartialFill
          Traders      Hedger
              |           |
           TradeDB    Recovery
```

---

## File Inventory — Complete Path Index

### Root — Orchestration & Config (5 files, 4,139 lines)

| File | Lines | Type | Purpose |
|------|-------|------|---------|
| `scanner.py` | 58 | `.py` | Re-export facade. Never add logic here. |
| `cli.py` | 1,288 | `.py` | Entry point: arg parsing, client init, one-shot dispatch |
| `continuous.py` | 1,832 | `.py` | Async event loop: WS feeds, periodic re-scans, settlement |
| `display.py` | 121 | `.py` | Output formatting (table/JSON) |
| `config.py` | 840 | `.py` | 100+ env vars, validation, fee reload, strategy layers |

### Root — Platform API Clients (12 files, 4,012 lines)

| File | Lines | Type | Auth Method | Can Trade? |
|------|-------|------|-------------|------------|
| `polymarket_api.py` | 390 | `.py` | Ethereum private key (py-clob-client) | Yes |
| `kalshi_api.py` | 367 | `.py` | RSA-PSS signed headers | Yes |
| `betfair_api.py` | 491 | `.py` | SSO (username/password) | Yes |
| `smarkets_api.py` | 352 | `.py` | OAuth Bearer token | Yes |
| `sxbet_api.py` | 458 | `.py` | Unauthenticated read / unsigned write | READ-ONLY (place_order broken) |
| `matchbook_api.py` | 389 | `.py` | Session token (user/pass) | Yes |
| `gemini_api.py` | 493 | `.py` | HMAC-SHA384 signed headers | Yes |
| `ibkr_api.py` | 387 | `.py` | TWS/IB Gateway socket | BUY-ONLY (no sell) |
| `metaculus_api.py` | 202 | `.py` | Optional token (public API) | Read-only signal source |
| `manifold_api.py` | 217 | `.py` | Optional token (public API) | Read-only signal source |
| `finnhub_api.py` | 133 | `.py` | API key query param | Read-only (news) |
| `polygonscan_api.py` | 142 | `.py` | API key query param | Read-only (whale txns) |

### Root — Execution Layer (5 files, 4,179 lines)

| File | Lines | Type | Purpose |
|------|-------|------|---------|
| `executor.py` | 3,045 | `.py` | Core execution: revalidation, risk gating, leg building, fill polling |
| `risk_manager.py` | 192 | `.py` | 8 risk gates (P&L, balance, depth, dedup, MM inventory) |
| `hedger.py` | 268 | `.py` | Partial fill recovery — sell filled legs when other side fails |
| `position_sizer.py` | 400 | `.py` | Kelly criterion + strategy-aware position sizing |
| `recovery.py` | 274 | `.py` | Crash recovery: reconcile orphaned trades on startup |

### Root — Market Infrastructure (6 files, 3,020 lines)

| File | Lines | Type | Purpose |
|------|-------|------|---------|
| `market_maker.py` | 739 | `.py` | Market making engine: QuoteEngine, InventoryTracker, QuoteManager |
| `ws_feeds.py` | 1,019 | `.py` | WebSocket feeds (Polymarket, Kalshi, Betfair) with auto-reconnect |
| `gas_monitor.py` | 323 | `.py` | Polygon gas price monitoring, dynamic fee thresholds |
| `event_monitor.py` | 452 | `.py` | Metaculus divergence signal detection + multi-source consensus |
| `signal_aggregator.py` | 282 | `.py` | Weighted probability aggregation from 8+ sources |
| `price_tracker.py` | 205 | `.py` | Rolling price tracker with staleness detection |

### Root — Data, Monitoring & Alerting (11 files, 5,021 lines)

| File | Lines | Type | Purpose |
|------|-------|------|---------|
| `db.py` | 832 | `.py` | SQLite (WAL mode): 5 tables, 28 public methods |
| `snapshot.py` | 278 | `.py` | Historical price snapshot recorder for backtesting |
| `backtest.py` | 614 | `.py` | Replay engine over recorded snapshots |
| `dashboard.py` | 889 | `.py` | HTTP server: 24 endpoints, kill switch, Prometheus metrics |
| `dashboard_ui.py` | 1,264 | `.py` | Single-page HTML/JS dashboard template |
| `metrics.py` | 340 | `.py` | Stdlib-only Prometheus-style counters, gauges, histograms |
| `notifier.py` | 223 | `.py` | Webhook alerts (Telegram, Slack, Discord, CallMeBot) |
| `alerting.py` | 440 | `.py` | Structured rate-limited alerts with severity levels |
| `credential_health.py` | 278 | `.py` | Credential validation per platform |
| `rate_limiter.py` | 81 | `.py` | Circuit breaker (fail_limit=3, reset=30s) |
| `run_dashboard.py` | 60 | `.py` | Standalone dashboard launcher for local testing |

### Root — Cross-Cutting Utilities (2 files, 2,241 lines)

| File | Lines | Type | Purpose |
|------|-------|------|---------|
| `matcher.py` | 773 | `.py` | Fuzzy + semantic matching for cross-platform market pairing |
| `fees.py` | 1,468 | `.py` | Net profit calculators for all 28 platform pairs + 16 strategies |

### scans/ — Scan Modules (25 files, 5,786 lines)

| File | Lines | Opp Types Produced | Two-Stage? |
|------|-------|--------------------|------------|
| `__init__.py` | 67 | — | — |
| `helpers.py` | 199 | — | Utilities |
| `binary.py` | 137 | `Binary` | Yes |
| `negrisk.py` | 203 | `Binary` (negRisk variant) | Yes |
| `kalshi.py` | 220 | `KalshiBinary`, `KalshiMulti` | Inline |
| `cross.py` | 645 | `Cross`, `CrossAll` | Yes |
| `spread.py` | 95 | `SpreadPM` | Inline |
| `betfair.py` | 237 | `BetfairBackAll`, `BetfairBackLay` | No (no CLOB) |
| `smarkets.py` | 184 | `SmarketsBackAll`, `SmarketsBackLay` | No |
| `sxbet.py` | 181 | `SXBetBackAll`, `SXBetBackLay` | No |
| `matchbook.py` | 204 | `MatchbookBackAll`, `MatchbookBackLay` | No |
| `gemini.py` | 175 | `GeminiBinary`, `GeminiMulti` | No |
| `ibkr.py` | 83 | `IBKRBinary` | No |
| `triangular.py` | 447 | `TriangularCross` | Yes |
| `multi_cross.py` | 459 | `MultiCross` | Yes |
| `stale.py` | 117 | `StalePriceOpp` | No |
| `resolution.py` | 232 | `ResolutionSnipeOpp` | No |
| `convergence.py` | 172 | `ConvergenceOpp` | No |
| `rewards.py` | 362 | `PolymarketRewards`, `KalshiRewards` | Yes |
| `imbalance.py` | 200 | `Imbalance` | Yes |
| `news_snipe.py` | 284 | `NewsSnipe` | No (refiner dead) |
| `correlated.py` | 310 | `Correlated` | Stubbed |
| `time_decay.py` | 204 | `TimeDecay` | No (refiner dead) |
| `logical_arb.py` | 182 | `LogicalArb` | Yes |
| `whale_copy.py` | 188 | `WhaleCopy` | No (refiner dead) |

### scripts/ — Operational Scripts (4 files + __init__)

| File | Lines | Type | Purpose |
|------|-------|------|---------|
| `scripts/__init__.py` | 0 | `.py` | Package marker |
| `scripts/analytics.py` | ~200 | `.py` | Per-strategy P&L analytics via DuckDB (7-day rolling) |
| `scripts/go_live_check.py` | ~150 | `.py` | Pre-flight health/status/metrics endpoint validation |
| `scripts/pnl_report.py` | ~200 | `.py` | P&L report with success criteria validation |
| `scripts/validation_report.py` | ~250 | `.py` | 7-day milestone validation report |

### tests/ — Test Suite (69 unit + 2 integration + fixtures)

| Directory | Files | Test Methods | Purpose |
|-----------|-------|-------------|---------|
| `tests/` | 67 `.py` | 1,900 | Unit tests (379 classes) |
| `tests/integration/` | 2 `.py` + 2 scripts | 46 | Integration: dry-run subprocess + executor strategies |
| `tests/fixtures/` | 1 `.json` | — | Polymarket reward metadata fixture |

### DevOps & Config

| File | Type | Purpose |
|------|------|---------|
| `Dockerfile` | Docker | Python 3.12-slim, fastembed model pre-download, /healthz check |
| `.github/workflows/test.yml` | YAML | CI: pytest on push/PR to master (Python 3.12) |
| `requirements.txt` | txt | 15 production dependencies |
| `requirements-dev.txt` | txt | 1 dev dependency (pytest) |
| `railway.toml` | TOML | Railway deploy config: Dockerfile builder, /healthz, ON_FAILURE restart |
| `.env.example` | env | Env var template (all platforms) |
| `.env` | env | Live secrets (gitignored) |
| `.gitignore` | git | Ignores .env, *.pem, *.db, *.log, .mcp.json |
| `.dockerignore` | Docker | Excludes .git, tests, .planning, .claude from build |
| `.mcp.json` | JSON | MCP server config (CONTAINS RAILWAY TOKEN - security issue) |
| `ecosystem.config.cjs` | JS | PM2 config for local dashboard on port 8081 |
| `polymarket-arb-scanner.code-workspace` | JSON | VS Code workspace (Peacock yellow) |
| `.vscode/settings.json` | JSON | Peacock color override (blue) |

### sxbet-proxy/ — Reverse Proxy Service

| File | Type | Purpose |
|------|------|---------|
| `sxbet-proxy/Dockerfile` | Docker | nginx:alpine reverse proxy to api.sx.bet |
| `sxbet-proxy/railway.toml` | TOML | Railway deploy: Singapore region (geo-bypass) |

### Orphan / Junk Files (safe to delete)

| File | Type | Why It Exists |
|------|------|---------------|
| `=1.1.0` | empty | Accidental pip artifact |
| `nul` | text | Windows null redirect artifact |
| `dashboard_error.log` | empty | Old dashboard error log |
| `dashboard_output.log` | log | Old dashboard startup log |
| `~/` directory | dir | Literal tilde directory (escaped path bug) |

### Planning & Documentation

| Path | Files | Purpose |
|------|-------|---------|
| `.planning/` | 166 files | 9 phase directories, research, calibration reports |
| `.planning/phases/01-09/` | 8-12 files each | Phase plans, research, validation, summaries |
| `.planning/codebase/` | 7 files | ARCHITECTURE, STACK, STRUCTURE, CONVENTIONS, etc. |
| `.planning/research/` | 5 files | v2.0 research synthesis |
| `.planning/calibration-reports/` | 6 files | Health check reports (all "unreachable" — sandbox limitation) |
| `CLAUDE.md` | 1 file | Project developer guide (305 lines) |
| `AGENTS.md` | 1 file | AI agent instructions (135 lines) |
| `decisions.jsonl` | 1 file | 214 execution decision records |

---

## Database Schema (db.py)

| Table | Key Columns | Purpose |
|-------|-------------|---------|
| **opportunities** | id, timestamp, type, market, prices, total_cost, net_profit, net_roi, depth, action | Every detected opportunity |
| **trades** | id, opportunity_id, timestamp, platform, side, price, size, status, fill_price, order_id, slippage | Individual trade legs |
| **positions** | id, opportunity_id, market_identifier, platform, status, realized_pnl, expected_pnl | Open and settled positions |
| **partial_fills** | id, trade_id, platform, token_id, side, fill_price, size, hedge_status | Partial fills awaiting hedge |
| **reward_metrics** | id, platform, market_key, order_id, event, size, spread, resting_seconds | Kalshi reward tracking |

Separate database: `snapshots.db`

| Table | Key Columns | Purpose |
|-------|-------------|---------|
| **price_snapshots** | id, timestamp, market, platform_a/b, price_a/b, net_profit, opp_type, strategy_layer | Historical prices for backtesting |

---

## Dashboard Endpoints (dashboard.py)

| Endpoint | Method | Auth? | Purpose |
|----------|--------|-------|---------|
| `/` `/dashboard` | GET | Yes | Single-page HTML dashboard |
| `/healthz` | GET | No | Health check (Railway/ECS) |
| `/status` | GET | Yes | Scanner state JSON |
| `/metrics` | GET | Yes | Prometheus text exposition |
| `/alerts` | GET | Yes | Recent alerts |
| `/api/health` | GET | Yes | System health (mode, uptime) |
| `/api/positions` | GET | Yes | Open positions with trades |
| `/api/platforms` | GET | Yes | Positions grouped by platform |
| `/api/trades` | GET | Yes | Recent trades (limit 100) |
| `/api/opportunities` | GET | Yes | Recent opportunities (limit 100) |
| `/api/strategies` | GET | Yes | Stats by strategy type |
| `/api/history` | GET | Yes | Daily P&L (30 days) |
| `/api/slippage` | GET | Yes | Average slippage |
| `/api/failures` | GET | Yes | Failed trades + stats |
| `/api/db-stats` | GET | Yes | Table row counts |
| `/api/strategy-pnl` | GET | Yes | Per-strategy P&L |
| `/api/strategy-leaderboard` | GET | Yes | 7-day rolling leaderboard |
| `/api/balances` | GET | Yes | Platform balances |
| `/api/rebalance` | GET | Yes | Capital transfer recommendations |
| `/api/validation` | GET | Yes | 7-day success criteria check |
| `/api/pause` | GET | Yes | Kill switch state |
| `/api/pause` | POST | Yes | Engage kill switch |
| `/api/resume` | POST | Yes | Disengage kill switch |
| `/api/purge` | POST | Yes | Delete opportunities by type |

---

## Dead Code Registry

These functions exist but are never called in production:

| Function | File | Lines | Why Dead |
|----------|------|-------|----------|
| `_refine_news_with_confidence()` | `scans/news_snipe.py` | ~20 | Stage 2 refiner — never wired into scan |
| `_refine_time_decay_with_prices()` | `scans/time_decay.py` | ~20 | Stage 2 refiner — never wired into scan |
| `_refine_whale_copy_with_prices()` | `scans/whale_copy.py` | ~15 | Stage 2 refiner — never wired into scan |
| `_refine_correlated_with_depth()` | `scans/correlated.py` | ~20 | Stubbed with TODO — accepts all Stage 1 opps |
| `polymarket_fee()` | `fees.py` | 11 | Deprecated legacy settlement-fee model |
| `detect_inverted()` | `matcher.py` | 20 | Inversion detection — only tested, never used in pipeline |
| `subscribe_news_stream_async()` | `finnhub_api.py` | ~5 | WebSocket stub — returns early |
| `get_contract_events()` | `polygonscan_api.py` | ~5 | Phase 10 stub — returns empty |
| `_calculate_total_exposure()` | `dashboard.py` | ~3 | Returns hardcoded 0 |
| `check_zero_opp_period_per_strategy()` | `alerting.py` | ~15 | Defined but never called |

---

## Security Issues

### CRITICAL — Requires Immediate Action

1. **Railway API token in `.mcp.json`** — Plaintext `rw_***REDACTED***` token committed to repo. File is in `.gitignore` but was already committed. **Action:** Revoke token in Railway dashboard, generate new one, store in env var.

2. **Kalshi private key in repo** — `kalshi-private-key.pem` (1,679 bytes) exists in working directory. In `.gitignore` but may have been committed previously. **Action:** Revoke key in Kalshi, regenerate, remove from git history with `git filter-branch`.

---

## Key Functions You Need to Know

These are the functions you'll touch most often when making changes:

### Adding a New Scan Strategy

| Step | File | Function | What to Do |
|------|------|----------|------------|
| 1 | `scans/<name>.py` | `scan_<name>()` | Create scan following two-stage pattern |
| 2 | `scans/__init__.py` | — | Export your scan function |
| 3 | `fees.py` | `net_profit_<name>()` | Add fee calculator |
| 4 | `executor.py` | `_build_legs()` | Add branch for your opp type |
| 5 | `executor.py` | `_revalidate()` | Add revalidation case |
| 6 | `cli.py` | `_run_oneshot()` | Wire into one-shot dispatch |
| 7 | `cli.py` | argparse choices | Add mode string |
| 8 | `continuous.py` | `_continuous_loop()` | Wire into continuous scan loop |
| 9 | `config.py` | — | Add feature flag + any thresholds |
| 10 | `scanner.py` | — | Add re-export |

### Adding a New Platform

| Step | File | Function | What to Do |
|------|------|----------|------------|
| 1 | `<platform>_api.py` | `<Platform>Client` | Create API client with auth, retries, circuit breaker |
| 2 | `fees.py` | `_platform_entry_fee()`, `_platform_win_fee()` | Add fee entries |
| 3 | `scans/cross.py` | `_CROSS_FEE_FUNCS` | Add cross-platform fee function |
| 4 | `executor.py` | `_execute_single_leg()` | Add order placement case |
| 5 | `executor.py` | `_confirm_fill_<platform>()` | Add fill polling |
| 6 | `hedger.py` | `_hedge_<platform>()` | Add hedge method |
| 7 | `recovery.py` | `_check_order_status()` | Add status query case |
| 8 | `cli.py` | `main()` | Initialize client, pass to executor |
| 9 | `config.py` | — | Add credentials + rate limit env vars |

### Modifying Fee Calculations

| File | Key Functions | Notes |
|------|--------------|-------|
| `fees.py` | `polymarket_taker_fee()` | Rate = P*(1-P)*fee_rate. March 2026 entry-time model. |
| `fees.py` | `kalshi_taker_fee()` | Cents-based. Min 2c, max 175c per contract. |
| `fees.py` | `net_profit_cross_generic()` | Catch-all for 21 of 28 platform pairs |
| `fees.py` | `find_lowest_fee_path()` | Dynamic fee routing across all platforms |
| `config.py` | `reload_fee_rates()` | Runtime fee rate refresh (1-hour interval) |

### Modifying Risk Management

| File | Function | What It Controls |
|------|----------|-----------------|
| `risk_manager.py` | `check()` | 8 sequential gates: daily P&L, trade count, positions, balance, depth, ROI, dedup, MM inventory |
| `risk_manager.py` | `calculate_dynamic_size()` | Half-Kelly: base * (1 + ROI * aggressiveness * 20) |
| `executor.py` | `_get_revalidation_threshold()` | Adaptive min-profit floor per strategy layer |
| `config.py` | `REVAL_FLOORS` | Layer-specific thresholds (L1=2%, L2=5%, L3=3%, L4=10%) |

### Modifying the Dashboard

| File | What to Change |
|------|----------------|
| `dashboard.py` | Add new endpoint: create handler method, add to `routes` dict |
| `dashboard.py` | `_DashboardState` class: add new state fields |
| `dashboard_ui.py` | `_TEMPLATE` string: modify HTML/JS (Chart.js for charts) |

### Modifying Continuous Mode

| File | Function | What It Does |
|------|----------|--------------|
| `continuous.py` | `run_continuous()` | Main async entry: WS setup, scan loop, settlement |
| `continuous.py` | `_recalc_profit()` | Recalculate profit on WS price update |
| `continuous.py` | `_execution_priority()` | Score opportunities for priority queue |
| `continuous.py` | `check_settlements()` | Query platforms for resolved markets |

---

## Potential Issues Found Across Codebase

### Execution Layer (executor.py)

| Issue | Location | Severity |
|-------|----------|----------|
| No timeout on ThreadPoolExecutor futures | `_execute_legs_concurrent()` | High — can hang indefinitely |
| Idempotency key only 60-second window | `_make_idempotency_key()` | Medium — minute-bucket collisions |
| Pre-flight balance rounding can underestimate cost | Lines 2186-2202 | Medium |
| Fill confirmation timeout inconsistency (None vs expected_price) | `_confirm_fill_*()` | Medium |
| Recovery dedup doesn't check "pending" siblings | `recovery.py:76-93` | Medium |

### Orchestration (cli.py, continuous.py)

| Issue | Location | Severity |
|-------|----------|----------|
| `logical-arb` and `whale-copy` modes not dispatched in one-shot | `cli.py:_run_oneshot()` | Medium |
| Price cache updated without lock in `on_price_update` | `continuous.py:646` | Low |
| EventMonitor signals not refreshed in continuous loop | `continuous.py` | Medium |
| Wire loss uses expected profit, not realized loss | `continuous.py:851` | Low |

### API Clients

| Issue | Platform | Severity |
|-------|----------|----------|
| `place_order()` sends unsigned JSON — non-functional | SX Bet | High |
| BUY-only constraint (no sell/close) | IBKR | By design |
| No retry mechanism (tenacity missing) | Metaculus | Low |
| Some methods bypass rate limit/circuit breaker | Betfair, Smarkets | Medium |

### Scan Modules

| Issue | File | Severity |
|-------|------|----------|
| 3 dead Stage 2 refiners (news, time_decay, whale) | scans/ | Low |
| Correlated refiner stubbed with TODO | `scans/correlated.py` | Medium |
| Whale copy calldata parsing is MVP/stub | `scans/whale_copy.py` | High |
| Hardcoded 7-day resolution window | `scans/resolution.py` | Low |

### Data & Monitoring

| Issue | File | Severity |
|-------|------|----------|
| XSS risk via innerHTML in dashboard | `dashboard_ui.py` | Medium |
| No automatic cleanup of price_tracker/signal_aggregator caches | Multiple | Low |
| Notifier has no retry logic | `notifier.py` | Low |
| Circuit breaker has no half-open state | `rate_limiter.py` | Low |

---

## Test Coverage Gaps

Source files with no dedicated test file:

| Source File | Lines | Indirect Coverage? |
|-------------|-------|-------------------|
| `dashboard_ui.py` | 1,264 | Partially (via test_dashboard.py) |
| `finnhub_api.py` | 133 | None |
| `polygonscan_api.py` | 142 | None |
| `manifold_api.py` | 217 | Partially (via test_new_strategies.py) |
| `market_maker.py` | 739 | Partially (via test_new_strategies.py) |
| `position_sizer.py` | 400 | Partially (via test_new_strategies.py) |
| `price_tracker.py` | 205 | Partially (via test_new_strategies.py) |
| `signal_aggregator.py` | 282 | Partially (via test_new_strategies.py) |

---

## All 26 Opportunity Types

| Type | Layer | Strategy | Platforms |
|------|-------|----------|-----------|
| `Binary` | 1 | Internal overround | Polymarket |
| `Binary` (negRisk) | 1 | Multi-outcome YES sum | Polymarket |
| `KalshiBinary` | 1 | Internal overround | Kalshi |
| `KalshiMulti` | 1 | Multi-outcome YES sum | Kalshi |
| `Cross` | 1 | 2-way cross-platform | PM + Kalshi |
| `CrossAll` | 1 | 2-way cross-platform | Any pair of 8 |
| `TriangularCross` | 1 | 3-way cross-platform | 3+ of 8 |
| `MultiCross` | 1 | Multi-outcome cross | PM + Kalshi |
| `SpreadPM` | 1 | Bid > ask (crossed book) | Polymarket |
| `BetfairBackAll` | 1 | Under-round | Betfair |
| `BetfairBackLay` | 1 | Crossed books | Betfair |
| `SmarketsBackAll` | 1 | Under-round | Smarkets |
| `SmarketsBackLay` | 1 | Crossed books | Smarkets |
| `SXBetBackAll` | 1 | Under-round | SX Bet |
| `SXBetBackLay` | 1 | Crossed books | SX Bet |
| `MatchbookBackAll` | 1 | Under-round | Matchbook |
| `MatchbookBackLay` | 1 | Crossed books | Matchbook |
| `GeminiBinary` | 1 | Internal overround | Gemini |
| `GeminiMulti` | 1 | Multi-outcome | Gemini |
| `IBKRBinary` | 1 | Internal overround | IBKR ForecastEx |
| `StalePriceOpp` | 2 | Stale price exploitation | Cross-platform |
| `ResolutionSnipeOpp` | 2 | Near-certain at discount | Any |
| `ConvergenceOpp` | 4 | Outlier convergence | Cross-platform |
| `Imbalance` | 4 | Order book imbalance | Any |
| `LogicalArb` | 4 | Semantic rule violation | Polymarket |
| `TimeDecay` | 4 | High consensus near expiry | Any |
| `NewsSnipe` | 4 | Headline sentiment | Any |
| `WhaleCopy` | 4 | Large wallet mirroring | Polymarket |
| `Correlated` | 4 | Spread divergence pairs | Polymarket |
| `PolymarketRewards` | MM | Liquidity farming | Polymarket |
| `KalshiRewards` | MM | Liquidity farming | Kalshi |
| `MarketMake` | 3 | Passive bid/ask spread | Any |

---

## Execution Flow — Full Pipeline

```
executor.execute(opportunity)
  |
  +-- Kill switch check (dashboard pause)
  +-- Failed-trade cooldown check (300s default)
  +-- Idempotency check (60s window)
  +-- Whale copy position limit (max 5 concurrent)
  |
  +-- _revalidate(opportunity, price_cache)
  |     +-- Dispatch to type-specific revalidator (20+ branches)
  |     +-- Adaptive threshold: layer-specific floors (L1=2%, L2=5%, L3=3%, L4=10%)
  |     +-- Accept on API error if scan ROI >= 2%
  |
  +-- risk_manager.check(opportunity, db, balances)
  |     +-- Daily P&L limit
  |     +-- Daily trade count
  |     +-- Open positions limit
  |     +-- Per-platform balance
  |     +-- Order book depth (tiered by ROI)
  |     +-- ROI minimum
  |     +-- Market dedup (with smart re-entry)
  |     +-- MM inventory limit
  |
  +-- gas_monitor.should_execute(opportunity) [if enabled]
  +-- Position sizing (Kelly or dynamic or fixed)
  +-- risk_manager.clamp_size(size, depth, balance)
  |
  +-- _build_legs(opportunity, size)
  |     +-- 40+ branches by opp type
  |     +-- Returns list[dict] with platform, side, price, token, idempotency_key
  |
  +-- Approval gate (semi-auto: prompt user)
  |
  +-- DRY RUN: _dry_run_log() --> log to DB, return True
  +-- LIVE: _execute_legs() or _execute_legs_concurrent()
        +-- Log opportunity + pending trades
        +-- Pre-flight per-platform balance check (95% threshold)
        +-- Submit legs (sequential or ThreadPoolExecutor)
        +-- Poll for fills (_confirm_fill_* per platform, 100ms intervals)
        +-- Success: create_position, invalidate balance cache, notify
        +-- Partial: queue_hedge() + process_pending_hedges(), notify
```

---

## Environment Variables Quick Reference

### Platform Credentials (must-set for trading)

| Variable | Platform | Required For |
|----------|----------|-------------|
| `POLYMARKET_PRIVATE_KEY` | Polymarket | Trading |
| `KALSHI_API_KEY_ID` + `KALSHI_PRIVATE_KEY_PATH` or `_BASE64` | Kalshi | Trading |
| `BETFAIR_USERNAME` + `BETFAIR_PASSWORD` + `BETFAIR_API_KEY` | Betfair | Trading |
| `SMARKETS_API_KEY` | Smarkets | Trading |
| `SXBET_API_KEY` | SX Bet | Read-only |
| `MATCHBOOK_USERNAME` + `MATCHBOOK_PASSWORD` | Matchbook | Trading |
| `GEMINI_API_KEY` + `GEMINI_API_SECRET` | Gemini | Trading |
| `IBKR_HOST` + `IBKR_PORT` + `IBKR_CLIENT_ID` | IBKR | Trading |
| `METACULUS_API_KEY` | Metaculus | Required with approved API access and `METACULUS_COMMERCIAL_USE_APPROVED=true` |

### Execution Controls (tune trading behavior)

| Variable | Default | Purpose |
|----------|---------|---------|
| `DRY_RUN` | `true` | Log only, don't trade |
| `EXECUTION_MODE` | `semi-auto` | `semi-auto` or `full-auto` |
| `MAX_TRADE_SIZE` | `5.0` | Max USD per order |
| `BASE_TRADE_SIZE` | `1.0` | Default trade size |
| `DAILY_LOSS_LIMIT` | `25.0` | Max daily loss before halt |
| `MAX_OPEN_POSITIONS` | `10` | Concurrent position limit |
| `MIN_NET_ROI` | `0` | Minimum ROI filter |
| `ENABLED_EXECUTION_PLATFORMS` | `polymarket,kalshi` | Platform whitelist for live orders |

### Feature Flags (toggle strategies on/off)

| Variable | Default | Strategy |
|----------|---------|----------|
| `MM_ENABLED` | `false` | Market making engine |
| `REWARDS_ENABLED` | `false` | Liquidity reward farming |
| `SNAPSHOT_ENABLED` | `false` | Price snapshot recording |
| `DYNAMIC_FEE_ENABLED` | `false` | Real-time gas monitoring |
| `EVENT_MONITOR_ENABLED` | `false` | Metaculus/Manifold signals |
| `IMBALANCE_ENABLED` | `false` | Order book imbalance |
| `NEWS_SNIPE_ENABLED` | `false` | News-driven sniping |
| `CORRELATED_ENABLED` | `false` | Correlated pairs |
| `TIME_DECAY_ENABLED` | `false` | Time decay convergence |
| `LOGICAL_ARB_ENABLED` | `false` | Combinatorial logical arb |
| `WHALE_COPY_ENABLED` | `false` | Whale wallet copying |
