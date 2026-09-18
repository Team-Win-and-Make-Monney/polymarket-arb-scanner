# Execution-path HIGH triage — 2026-07-21

Triage of the ~21 execution-path HIGH findings from the 2026-07-17 consolidated audit
(`.codebase-audit/report-20260717-consolidated.md`), each re-verified against
`origin/master` @ `7d26e20` by independent verification passes. This document
records the disposition of that backlog — it is a triage record, not a new audit.

Verdicts: **CONFIRMED** (defect exists as described), **STALE** (already fixed/mitigated),
**INVALID** (misreading), **ACCEPT** (real but knowingly not fixed — rationale recorded).
Exposure assumes the deployed config as of 2026-07-21: `DRY_RUN=true`,
`ENABLED_EXECUTION_PLATFORMS=kalshi`, `MAX_TRADE_SIZE=10`, `NEWS_SNIPE_ENABLED=true`,
`DYNAMIC_FEE_ENABLED=true`.

## FIX-NOW (active today or poisons data regardless of DRY_RUN)

| Finding | Verdict | Why now |
|---|---|---|
| `position_sizer.py` `_extract_net_roi` misparses sub-1% percent strings ("0.50%" → 0.5 = 50% ROI) | CONFIRMED, severe | 100× ROI overstatement drives Kelly sizing to oversize the thinnest-margin trades; wrong in the sizing path today, catastrophic on live flip |
| `continuous.py` news-snipe `NameError` on `FINNHUB_API_KEY` (never imported), swallowed at debug | CONFIRMED | **Active in prod right now** — Railway sets `NEWS_SNIPE_ENABLED=true`, so the scan crashes and no-ops every cycle (same silent-kwargs failure class PR #62 fixed elsewhere) |
| `scans/rewards.py` uses Gamma's JSON-string `outcomePrices` as numeric mid_price | CONFIRMED | Crashes/short-circuits the rewards scan on any price-cache miss; `event_monitor.py` has the correct `json.loads`+`float` pattern to copy. Grep for other bare `outcomePrices` uses |
| `superforecaster_api.py` calls nonexistent `MetaculusClient.get_community_prediction_by_title` | CONFIRMED | Metaculus leg of expert-divergence silently dead on every run (AttributeError swallowed at debug) |
| `scans/cross.py:466` reversed `fee_key` fallback in `scan_cross_all` | CONFIRMED, dormant | Never fires today only because platform iteration order happens to match `_ALL_PLATFORMS`; order-coupling landmine, cheap fix |

## FIX-BEFORE-LIVE (gate items — must land or be risk-accepted before `DRY_RUN=false`)

| Finding | Verdict | Notes |
|---|---|---|
| `scans/cross.py` Kalshi inversion swap not persisted → executor trades the wrong Kalshi side on logically-inverted pairs | CONFIRMED | Highest priority of this tier — Kalshi is the enabled platform; persist `_inverted`, flip side in `_build_legs` |
| `continuous.py` cross-position settlement queries Gamma with the Kalshi ticker | CONFIRMED | Cross positions never settle; store PM conditionId per-leg, branch lookup per-leg |
| `recovery.py` orphan→partial-fill conversion stuffs `order_id` into `token_id` | CONFIRMED | Recovery hedges would target a nonexistent instrument after a mid-fill crash |
| `hedger.py` Betfair/Smarkets/SX Bet/Matchbook hedges ignore `max_loss` + current book (stale fill price) | CONFIRMED | Backfill the book-fetch + max-loss gate pattern already used for PM/Kalshi/Gemini |
| `event_monitor.py` consensus includes the platform's own price (comment claims exclusion) | CONFIRMED | Self-referential bias dampens divergence signals (Layer 4) |
| `scans/smarkets.py` `min_depth` accumulator resets on zero-size levels | CONFIRMED | Overstates depth → oversized dynamic sizing on Smarkets back-alls |
| `dashboard.py` no CSRF/Origin check on state-changing POSTs (pause/resume/purge/rebalance) | CONFIRMED | Add CSRF token or Origin allowlist before real balances sit behind a non-loopback dashboard |
| `alerting.py` loss-limit escalation flag set before send; shared rate-limit key can swallow the 100% CRITICAL | CONFIRMED, unwired | `check_daily_loss` is dead code today (never called from prod modules); fix when wiring it in — which is itself a gate item |
| `ws_feeds.py` `FeedHealthTracker` pipeline never wired (API-outage detection dead) | CONFIRMED | Monitoring gap, not a bad-trade source; wire before live |
| `sxbet_api.py` `place_order`/`cancel_order` bypass `_request`, circuit breaker, DRY_RUN guard | CONFIRMED | Moot while SX Bet stays quarantined (unsigned-JSON, startup-blocked); fix if ever unquarantined |

## ACCEPT / STALE / INVALID (closed with rationale)

| Finding | Verdict | Rationale |
|---|---|---|
| `executor.py:3095` Betfair/Matchbook LAY decimal-odds conversion "wrong" | **INVALID** | Scans store each side's own implied probability (`1/odds`); `1/price` symmetrically reconstructs the correct odds for both BACK and LAY |
| `smarkets_api.py` "/100 read vs ×10000 write mismatch" | **INVALID** | Two consistent conversions at different API boundaries (percent read → probability; probability → basis-points write) |
| `gemini_api.py` `withdraw_usdc` no DRY_RUN/allowlist | **STALE** | Sole caller `treasury.py:execute_transfer` fully gates: `AUTO_REBALANCE_ENABLED` (default false), dry-run short-circuit, hardcoded destination, corridor whitelist, daily cap, kill switch — all tested |
| `gemini_api.py`/IBKR missing client-boundary DRY_RUN guard | ACCEPT | Upstream executor/mm_pilot gates cover every current call path; backfill Matchbook-style guard as low-priority hardening |
| `sxbet_api.py:219` maker odds as taker price | ACCEPT | Real detection defect, but SX Bet is execution-quarantined; fix alongside EIP-712 signing if ever pursued |
| `cross_pair_index.py` inverted-pair `_kalshi_yes/_no` mislabeled | ACCEPT | Verified inert: no consumer reads those fields for this opp type (executor parses `prices_str`; revalidation keys are absent → skip). Cosmetic fix optional |

## Scoreboard

21 findings: **15 confirmed** (5 fix-now, 10 fix-before-live), **1 stale**, **2 invalid**,
**3 accepted** with rationale. The go-live gate's "execution-path HIGHs triaged" criterion
is satisfied by this document; the gate now depends on the FIX-BEFORE-LIVE table reaching
zero open items (fixed or explicitly risk-accepted).
