# Prediction Market Earning Strategies — Reconciled Framework v2 (post-PR #10)

**Date:** 2026-05-09
**Supersedes:** `Full_landscape_of_earning_strategies_for_prediction_markets_3-15-2026.md`, `Strategy_Expansion___Scope_Update_for_predection_markets_3-15-2026.md`, and earlier v2 drafts dated 2026-05-09.
**Reconciled against:** `CLAUDE.md` and `CODEBASE-INVENTORY.md` (generated 2026-04-12) **plus PR #10** (`feat(strategies): first-class coverage for #9, #11, #12, #18`, branch `claude/sad-euler-a029b1`).

---

## Context

The original framework defined 20 strategies across 5 risk layers. Implementation has grown beyond the original plan: the codebase now produces **27 distinct opportunity types** (PR #10 added `FeePromo` and `CrossPlatformMM`) implementing **29 strategies** across the same 5 layers.

This document reconciles the framework with the actual implementation, re-runs the status pass against current code, and documents the remaining gaps.

**Status legend:**
- **BUILT** — fully implemented, in production, no significant flagged issues
- **PARTIAL** — implemented but with known gaps, dead components, platform-specific breakage, or by-design ceilings
- **STUB** — only skeletal code with TODO markers; not functional
- **NOT BUILT** — listed in framework but no implementation exists

---

## Strategy Framework — 5 Layers, 29 Strategies

### Layer 1 — Pure Arbitrage (Risk-Free)

Mathematically guaranteed profit. Buy a complete outcome set for less than payout, or exploit a crossed book.

| # | Strategy | Status | Module(s) | Platforms |
|---|----------|--------|-----------|-----------|
| 1 | Same-platform binary overround | BUILT | `binary.py`, `kalshi.py`, `gemini.py`, `ibkr.py` | PM, Kalshi, Gemini, IBKR |
| 2 | Same-platform multi-outcome overround | BUILT | `negrisk.py`, `kalshi.py`, `gemini.py` | PM (NegRisk), Kalshi, Gemini |
| 3 | Cross-platform 2-way | BUILT | `cross.py` | All 28 pairs of 8 platforms |
| 4 | Multi-outcome cross-platform | BUILT | `multi_cross.py` | PM + Kalshi |
| 5 | Triangular (3-way) | BUILT | `triangular.py` | All platforms |
| 6 | Back-all / Back-lay | PARTIAL | `betfair.py`, `smarkets.py`, `sxbet.py`, `matchbook.py` | SX Bet `place_order()` broken (read-only); Betfair/Smarkets some methods bypass rate limiter |
| 21 | Crossed book / spread detection | BUILT | `spread.py` | PM, Kalshi |

CLAUDE.md categorizes #21 under Layer 5 because the detection feeds MM spread sizing. Mechanically the opp type is pure arb (bid > ask), so it lives in Layer 1 here with a Layer 5 dual-use note.

**Addition (2026-06-08, Plan 01):** **NegRisk NO-side** (`NegRiskNO` opp type — buy 1 NO on every outcome when Σ NO < (N−1)) shipped as the risk-free *complement* to #2 (same-platform multi-outcome). Modules: `scans/negrisk.py:scan_negrisk_no_side` + `_refine_negrisk_no_side_with_clob`, `fees.net_profit_negrisk_no_side`, `executor._build_legs`/`_revalidate_negrisk_no`, mode `--mode negrisk-no`, flag `NEGRISK_NO_SIDE_ENABLED` (default off). Recorded as a variant of #2 rather than a new taxonomy number to keep the status rollup stable; build details in [`docs/plans/01-negrisk-no-side.md`](plans/01-negrisk-no-side.md).

---

### Layer 2 — Near-Arbitrage (Near Risk-Free)

Not mathematically guaranteed, but probability of loss is very low (<5%).

| # | Strategy | Status | Module | Notes |
|---|----------|--------|--------|-------|
| 7 | Resolution sniping | BUILT | `resolution.py` | Window configurable via `RESOLUTION_SNIPE_WINDOW_HOURS` (default 48h) as of PR #18 |
| 8 | Stale price exploitation | BUILT | `stale.py` | Requires `--continuous` mode for real WS history |
| 9 | Fee promotional arbitrage | **BUILT** ✱ | `scans/fee_promo.py` + `near_miss_cache.py` | **PR #10** — distinct `FeePromo` opp type, near-miss capture in `scans/cross.py:_refine_cross_with_clob`, calendar tracking via `*_PROMO_EXPIRES` env vars + `notifier.notify_promo_warning`. Default off (`FEE_PROMO_ENABLED=false`). |

✱ Status changed from PARTIAL → BUILT after PR #10.

---

### Layer 3 — Market Making & Liquidity Provision (Low Risk)

Provide liquidity and earn bid-ask spreads, plus capture exchange-paid liquidity rewards. Layer 3 is broadened from the original framework to include rewards farming, which is liquidity provision compensated by the exchange rather than by spread capture.

| # | Strategy | Status | Module | Notes |
|---|----------|--------|--------|-------|
| 10 | Passive market making | BUILT | `market_maker.py` (`QuoteEngine`, `QuoteManager`) | Single-platform passive MM |
| 11 | Cross-platform market making | **BUILT** ✱ | `scans/cross_mm.py` + `market_maker.CrossPlatformMaker` | **PR #10** — distinct `CrossPlatformMM` opp type. Default off (`CROSS_MM_ENABLED=false`). |
| 12 | Inventory-hedged MM | **BUILT** ✱ | `market_maker.py` (`InventoryTracker`) + `hedger.hedge_inventory()` | **PR #10** — `MarketMaker.on_fill` now wires the auto-hedge action when `needs_hedge()` is True. Pre-PR-10 the trigger fired but no action took place; this v2 had previously over-marked it BUILT. Default off (`MM_AUTO_HEDGE_ENABLED=false`). |
| 22 | Liquidity reward farming — Polymarket | BUILT | `scans/rewards.py` | `PolymarketRewards` opp type |
| 23 | Liquidity reward farming — Kalshi | BUILT | `scans/rewards.py` | `KalshiRewards` opp type |
| 23a | Kalshi reward-MM pilot (safety layer) | **BUILT** | `mm_pilot.py` (`KalshiMMPilot`) | Execution variant of #23 — NOT a separate counted strategy (taxonomy stays 29). Plan 10 (`docs/plans/10-mm-pilot-prep.md`) — live LIP/VIP quoting hardening: `authorize_order` choke point, hard inventory caps, fill polling + auto-hedge, toxic/vol gates in the hot path, Supabase `bot_controls.mm_pilot_enabled` kill switch, canary phase. Kalshi ONLY. Default off (`MM_KALSHI_PILOT_ENABLED=false`); live start additionally requires `MM_AUTO_HEDGE_ENABLED` + `MM_TOXIC_FLOW_ENABLED` + `MM_VOLATILITY_ADJUSTED_ENABLED` and market selection from PR #43's `scans/lip_select.py`. |

✱ Status changed (or made truly accurate) after PR #10.

---

### Layer 4 — Informed / Statistical Edge (Moderate Risk)

Directional positions based on information advantages. Positive expected value, not risk-free. Layer 4 has been substantially expanded from the original 3-strategy framework — 6 additional strategies are implemented in code, with varying degrees of completion.

| # | Strategy | Status | Module | Notes |
|---|----------|--------|--------|-------|
| 13 | Event divergence (multi-source) | BUILT | `event_monitor.py` + `signal_aggregator.py` | Was Metaculus-only; now multi-source |
| 14 | Cross-platform convergence | BUILT | `convergence.py` | Outlier vs median |
| 15 | Multi-source signal aggregation | BUILT | `signal_aggregator.py`, `manifold_api.py`, `metaculus_api.py` | 8+ sources, weighted consensus |
| 24 | Order book imbalance | BUILT | `imbalance.py` | Two-stage refinement |
| 25 | Logical / combinatorial arb | BUILT | `logical_arb.py` | Semantic rule violations on Polymarket |
| 26 | Time decay convergence | BUILT | `time_decay.py` | First-class refiner `_refine_time_decay_with_prices` (l.156); 48 tests pass |
| 27 | News-driven sniping | BUILT | `news_snipe.py` (+ `finnhub_api.py`) | First-class refiner `_refine_news_with_confidence` (l.208); 36 tests pass |
| 28 | Whale copy | BUILT | `whale_copy.py` (+ `polygonscan_api.py` + `whale_copy_decoder.py`) | Refiner `_refine_whale_copy_with_prices` (l.216) + decoded calldata via `decode_calldata`; 76 tests pass |
| 29 | Correlated pairs | BUILT | `correlated.py` (+ `correlation_tracker.py`) | Stage 2 refiner `_refine_correlated_with_depth` (l.306) + auto Pearson-correlation detection over 30-day snapshots; 58 tests pass |

---

### Layer 5 — Capital & Execution Optimization (Multiplier)

Force multipliers that increase returns from all other layers.

| # | Strategy | Status | Module | Notes |
|---|----------|--------|--------|-------|
| 16 | Dynamic fee routing | BUILT | `gas_monitor.py` + `fees.py` | Gas-aware execution gating; 28-pair fee calculators |
| 17 | Kelly criterion sizing | BUILT | `position_sizer.py` | Strategy-aware fractions by layer |
| 18 | Platform fund rebalancing | **PARTIAL** ✱ | `treasury.py`, `gemini_api.withdraw_usdc`, `db.transfers`, `dashboard.py:POST /api/rebalance/execute`, weekly digest via `notifier.py` | **PR #10** — Gemini ↔ Polymarket programmatic auto-transfer via USDC on Polygon. Six other platforms (Kalshi, Betfair, Smarkets, SX Bet, Matchbook, IBKR) remain on the manual-digest path because their public APIs expose no withdraw / deposit / transfer endpoints. This is a by-design ceiling, not a gap. Default off (`AUTO_REBALANCE_ENABLED=false`). |
| 19 | Latency optimization | **BUILT** ✱ | `continuous.py:_execution_priority()` + `asyncio.PriorityQueue` | Pre-existing infrastructure misclassified by earlier v2 drafts. WS-triggered high-priority execution at `continuous.py:909`; priority scoring at `continuous.py:523`. |
| 20 | Backtesting-driven tuning | PARTIAL | `backtest.py` + `snapshot.py` + `scripts/tune.py` | Tuning loop **implemented + tested** (Sprint 6, in-flight as of 2026-05-31): `scripts/tune.py` runs `backtest._suggest_strategy_thresholds()` / `build_recommendations()` over rolling windows and emits per-strategy `MIN_NET_ROI` / `FUZZY_MATCH_THRESHOLD` recommendations; `config.load/apply_backtest_recommendations()` consumes them behind `BACKTEST_TUNING_ENABLED`. **Remaining gap (why still PARTIAL):** applied only on manual invocation — not auto-wired into `continuous.py` startup, and no alert fires when recommendations change. |

✱ Status changed (or correctly classified) after PR #10 review.

---

## Status Roll-Up

| Status | Count | Strategies |
|--------|-------|------------|
| BUILT | 26 | 1, 2, 3, 4, 5, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 19, 21, 22, 23, 24, 25, 26, 27, 28, 29 |
| PARTIAL | 3 | 6, 18, 20 |
| STUB | 0 | — |
| NOT BUILT | 0 | — |
| **Total** | **29** | |

Layer breakdown: Layer 1 = 7 (1, 2, 3, 4, 5, 6, 21), Layer 2 = 3 (7, 8, 9), Layer 3 = 5 (10, 11, 12, 22, 23), Layer 4 = 9 (13, 14, 15, 24, 25, 26, 27, 28, 29), Layer 5 = 5 (16, 17, 18, 19, 20).

Net change vs prior v2: +4 BUILT (#9, #11, #19 corrected, plus #18 lifted from NOT BUILT to PARTIAL is captured separately), −3 PARTIAL (#9, #11, #19 promoted), −1 NOT BUILT (#18 promoted to PARTIAL).

**2026-05-20 audit update:** +4 additional BUILT (#26, #27, #28, #29 — each has a first-class Stage 2 refiner with substantial test coverage; the earlier "dead refiner" / "TODO marker" notes were stale by the time of this audit). −3 PARTIAL (#26, #27, #28 promoted). −1 STUB (#29 promoted).

**2026-05-31 content audit:** counts unchanged (26 BUILT / 3 PARTIAL / 0 STUB). #20 note refreshed to reflect the in-flight tuning-loop implementation (still PARTIAL — manual-apply only). Open follow-up: this 29-strategy taxonomy does **not** 1:1 map the **33 `--mode` scan values** in `cli.py` (excluding `all`). The surplus modes (`negrisk-no`, `nway`, `rewards`, `imbalance`, `news-snipe`, `correlated`, `time-decay`, `logical-arb`, `whale-copy`, `lead-lag-mm`, `toxic-flow`, `vol-mm`) are runnable scans that map onto the layer strategies above (or are execution variants of them) but are not separately enumerated here. A 1:1 mode→strategy reconciliation table is a tracked TODO; until then, treat `cli.py` `--mode` choices as the source of truth for *runnable scans* and this table as the source of truth for the *risk-layer taxonomy*.

---

## Known Gaps

Grouped by remediation type. **Groups A, B, and C are now empty** after the 2026-05-20 audit and the Sprint 1 / Sprint 4 ship cycles. The only remaining original-framework gap is #20.

~~**Group B — Codebase additions with dead Stage 2 refiners**~~ — resolved 2026-05-20 audit. #26 / #27 / #28 each have first-class Stage 2 refiners (`_refine_time_decay_with_prices`, `_refine_news_with_confidence`, `_refine_whale_copy_with_prices`) with substantial test coverage. #28 calldata parsing now uses real `whale_copy_decoder.decode_calldata`.

~~**Group C — Stubbed strategy**~~ — resolved 2026-05-20 audit. #29 Correlated pairs is fully built (`scan_correlated`, `_refine_correlated_with_depth`, auto-detection via `correlation_tracker.py`).

**Group D — Platform / infrastructure issues affecting Layer 1**
- **SX Bet `place_order()`** sends unsigned JSON — non-functional (High severity). Live trading is now blocked by `validate_config()` (PR #18); EIP-712 signing is the real fix.
- **Betfair / Smarkets** — some methods bypass rate limit / circuit breaker (Medium severity)
- ~~Resolution sniping hardcoded 7-day window~~ — resolved in PR #18 via `RESOLUTION_SNIPE_WINDOW_HOURS` env var (default 48h)

**Group E — Code health (not strategy gaps but block production confidence)**
- ~~Dashboard `innerHTML` XSS risk (`dashboard_ui.py`)~~ — resolved in PR #28 (Sprint 1): all `innerHTML` sites replaced with `createElement` / `textContent`; regression-guarded by `tests/test_dashboard_ui.py`.
- ~~Hardcoded password in `run_dashboard.py`~~ — resolved (no default; `DASHBOARD_PASS` must come from env). PR #18 additionally added a `DASHBOARD_HOST` env var with loopback default and a `validate_config()` gate that raises `ConfigError` on non-loopback host + empty password.
- No dedicated test files for `position_sizer.py`, `signal_aggregator.py`, `price_tracker.py`, `manifold_api.py`. Note: `market_maker.py` has direct coverage via `tests/test_hedger_inventory.py` and `tests/test_cross_mm.py` from PR #10; `dashboard_ui.py` covered by `tests/test_dashboard_ui.py` from PR #28.

**Group F — #20 backtesting tuning loop (only original-framework gap left)**
- `backtest.py` and `snapshot.py` exist; no automated tuning loop consumes them.

---

## Remediation Roadmap

### Sequencing note

PR #10 (`feat(strategies): first-class coverage for #9, #11, #12, #18`) is in flight on branch `claude/sad-euler-a029b1`. The roadmap below assumes it merges first. The "v2 + rename" PR described in the Repo Rename section should rebase on top of PR #10's merge — otherwise the v2 status table will conflict with PR #10's CLAUDE.md scope updates.

### Phase 1 — Quick wins (1 week) — DONE in PR #18

Platform-specific fixes and a security item that don't depend on strategy decisions. All three landed in PR #18.

1. ✅ **Quarantine SX Bet** — `validate_config()` raises `ConfigError` when `sxbet` is in `ENABLED_EXECUTION_PLATFORMS` and `DRY_RUN=false`. Detection-only is still allowed. EIP-712 signing is the real fix and is a future PR.
2. ✅ **Make resolution-sniping window configurable** — env var `RESOLUTION_SNIPE_WINDOW_HOURS`, default `48`. (Earlier draft said `_DAYS=7`; reconciled to match the implemented unit.)
3. ✅ **Dashboard credential hardening** — `run_dashboard.py` hardcoded password was already removed before PR #18. PR #18 additionally added a `DASHBOARD_HOST` env var (default `127.0.0.1`, was hardcoded `0.0.0.0`) and a `validate_config()` gate that raises `ConfigError` when the host is non-loopback and `DASHBOARD_PASS` is empty.

### Phase 2 — Finish the four Layer 4 incomplete scans (3–5 weeks)

All four strategies (#26, #27, #28, #29) are being finished rather than killed. Sequenced from easiest to hardest. Each scan needs its Stage 2 CLOB refiner written so it survives the two-stage detection pattern; #28 and #29 need additional foundational work.

4. **#26 Time decay convergence** — write Stage 2 refiner only. Logic: re-fetch live ask prices for candidates near expiry where multi-source consensus is high (>85% confidence) but market price is below ~90%. Filter out candidates that don't survive the ask-price check. Module changes: complete `time_decay.py:_refine_with_clob()`. Smallest of the four.

5. **#27 News-driven sniping** — write Stage 2 refiner. Logic: validate news event freshness (under N minutes old, configurable), re-check market price hasn't already moved against the signal, confirm sentiment direction matches market direction. Dependency: `finnhub_api.py` already exists. Module changes: complete `news_snipe.py:_refine_with_clob()`.

6. **#28 Whale copy** — two-part fix:
   - **(a) Replace MVP calldata parser** — current parser is stub. Need a proper Polymarket CTF (Conditional Token Framework) decoder that extracts `market_id`, `side`, `size`, `price` from raw on-chain transaction calldata. Reference: Polymarket CTF contracts on Polygonscan + py-clob-client decoding utilities. This is its own subproject — budget 1–2 weeks alone.
   - **(b) Write Stage 2 refiner** — once parsing is reliable, validate that the whale's position is still open, that copy size is within risk limits, and that current market price hasn't already moved past the entry. Module changes: rewrite `whale_copy.py:_parse_calldata()`, then write `_refine_with_clob()`.

7. **#29 Correlated pairs** — closer to from-scratch than finish. Three pieces:
   - **(a) Correlation detection** — analyze 30-day price snapshots from `snapshot.py` to identify correlated market pairs. Two paths: (i) statistical (Pearson > 0.85 over rolling window), (ii) rule-based seed pairs (e.g., "Trump popular vote" ↔ "Trump electoral college", "Fed rate hike" ↔ "S&P direction").
   - **(b) Divergence detection** — when a known-correlated pair's spread deviates >2σ from its historical mean, fire a `Correlated` opp.
   - **(c) Stage 2 refiner** — revalidate at ask prices and confirm divergence persists.
   - Module changes: full implementation of `correlated.py` plus a new `correlation_tracker.py` for the historical analysis.

### Phase 3 — Close the original framework (1 week)

Phase 3 collapsed dramatically post-PR-10. Items 8, 9, 10, 11 from the prior v2 are already done or cover by-design ceilings. Only one item remains:

8. **#20 Tuning loop** — new `scripts/tune.py` that runs `backtest.py` over rolling 30-day windows, proposes adjustments to `MIN_NET_ROI`, MM spread defaults, and `EVENT_DIVERGENCE_THRESHOLD`, and writes a tuning report to `scripts/tuning_<date>.md`. Manual review before applying.

### Phase 4 — Hardening (ongoing)

9. **Retry/circuit-breaker discipline** — audit all Betfair and Smarkets methods, route every external call through `rate_limiter.py`.
10. **Dedicated test files** for `position_sizer.py`, `signal_aggregator.py`, `price_tracker.py`, `manifold_api.py`, `dashboard_ui.py`. (`market_maker.py` and `treasury.py` and `near_miss_cache.py` are covered by PR #10's test additions.)
11. **Dashboard XSS fix** — replace all `innerHTML` with `textContent` in `dashboard_ui.py`.

### Phase 5 — Future-state (only when platform APIs allow)

12. **Extend #18 auto-rebalance corridor** — currently Gemini ↔ Polymarket only. If/when Kalshi, Betfair, Smarkets, SX Bet, Matchbook, or IBKR ever add programmatic withdraw / deposit endpoints, extend `treasury.TreasuryManager` to support those corridors. As of 2026-05-09, none of these platforms expose such endpoints, so this phase is gated on platform behavior, not engineering availability.

---

## Repo Rename

The repo will be renamed concurrent with v2 framework adoption. "Polymarket Arb Scanner" no longer describes scope — the project covers 8 trading platforms plus 2 signal sources, with 29 strategies across 5 risk layers.

**New name: `arbgrid`**.

Rationale: tight, accurately describes the architecture (a grid of platforms × layers × strategies — 8 × 5 × 29), and sheds the Polymarket-only baggage of the current name.

The rename touches:
- GitHub repo: `polymarket-arb-scanner` → `arbgrid` (set up redirect on the old name to avoid breaking any external references)
- Railway service name and any associated env vars or webhook URLs referencing the old name
- Docker image tag in `Dockerfile` (currently `polymarket-arb-scanner`)
- References in `CLAUDE.md` (project overview line), `CODEBASE-INVENTORY.md` (title), `README.md`, `.github/workflows/test.yml`
- Any internal log strings, alert channel names, or health check paths that include the old name
- Local clones — `git remote set-url origin git@github.com:<user>/arbgrid.git`

**Sequencing:** Execute the v2-adoption-plus-rename change as one atomic PR **after PR #10 merges**. Do not stack the rename on top of PR #10's branch — it would conflate two reviews. Do not split the rename and the framework adoption across two PRs either — they reference each other and a partial state is harder to review than a single coherent change.

Recommended PR sequence:
1. **PR #10** (in flight) — strategy first-class coverage for #9, #11, #12, #18.
2. **Phase 1 quick-wins PR** — SX Bet quarantine, resolution window env var, dashboard password fix.
3. **v2 + rename PR** — drop this doc into `docs/strategy-framework-v2.md` (or repo root), update `CLAUDE.md` to point to it, perform the rename touching all paths above. Single atomic merge.
4. **Phase 2 milestones** — one PR per scan (#26, #27, #28a, #28b, #29) for reviewability.
5. **Phase 3 PR** — `scripts/tune.py` for #20.
6. **Phase 4 PRs** — hardening as opportunities arise.
7. **Phase 5** — gated on platform API behavior.

---

## Document Maintenance

This framework should be re-reconciled against `CODEBASE-INVENTORY.md` whenever:
- A new opportunity type is added (`_build_legs()` branch added in `executor.py`)
- A scan module is removed or marked dead
- A new platform integration is added
- Strategy-layer assignments change in `config.py`

Suggested review cadence: every 30 days, or after any milestone where >3 status fields would change. The PR-#10 reconciliation is itself an example — it triggered five status updates and required this revision.
