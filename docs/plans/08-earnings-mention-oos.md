# 08 — Earnings-Mention NO-Harvest OOS Logger

**Status:** spec / v1 build-ready (Kalshi-data-only, **no new API keys**) · **Owner:** Claude (build) + Codex (adversarial review) · **Capital:** $0 (detection/logging only)
**Related:** command-center `02-UNIFIED-OPPORTUNITY-MATRIX-v2` (R2-1, the in-sample pilot), `01-EXECUTION-TRACKER` (8/3 verdict task), `20-INTEGRATION-DECISION-AND-BUILD-PLAN` (Phase 2), `17-STRATEGY-VALIDITY-REVIEW` (R2-1 still valid 6/24). House-style template: `07-perp-executor.md`.

## 0. TL;DR
Re-run the round-2 **earnings-mention NO-harvest** pilot **out-of-sample** on newly-settled Kalshi company-KPI/earnings-mention markets, to confirm the in-sample finding: **11–50c YES contracts are ~10pts too rich at T-24h** (+10.3pts, z=2.41, 240 markets) vs ~4.6c round-trip cost. The shipped logger discovers settled markets, reconstructs one YES price at T-24h from historical candles, joins it to realized settlement, and accumulates the OOS richness sample. **Detection/logging only — places no orders** (deterministic statistical logger; no LLM in any path). Feeds the pre-registered **8/3 verdict**: OOS 11–50c gap **≥9pts & z≥2 → PURSUE-NOW** (passive NO ladders, ≤10% market volume, ≤$300/market — a *separate* executor build); **<4.5pts → kill**; else keep logging.
**v1 = Kalshi data only (zero new keys).** v2 layers financialdatasets.ai KPI-guidance + FMP transcript base-rates as per-event priors to sharpen *which* contracts to fade.

## 1. Thesis & edge (from R2-1)
At T-24h, company-KPI / earnings-mention YES contracts priced 11–50c settle systematically **below** their price (overpriced ~10pts) vs ~4.6c cost; institutional MM share ~5% of bid matches; median market $1–2K notional → small, **capacity-limited**, behavioral (retail over-buys "will they mention X" YES). Fade = sell YES / buy NO in the 11–50c band, T-24h→T-6h, **only in categories that test rich OOS**.

## 2. Scope & rules
- **Logger only.** No order placement in this module. Sizing constants (≤10% market volume, ≤$300/market) are defined for the *future* passive-NO-ladder executor that the 8/3 PURSUE verdict would authorize — **not built here**.
- Venue: Kalshi only (CFTC, MI-eligible, non-sports). On-allowlist.
- Deterministic; no LLM. (v2 priors are an *offline* pre-compute, never in a hot path.)

## 3. Data (as shipped — redesigned 2026-07-05, see §3a)
Exact methods in `kalshi_api.py`:
- **Identify markets:** `fetch_settled_markets(min_close_ts)` — `GET /markets?status=settled&min_close_ts=...`, cursor-paginated (raises `RuntimeError` rather than returning a silently-partial page set). Filter to the mention family via `classify_market()` — **series-ticker prefix only** (`SERIES_PREFIXES = ("KXEARNINGSMENTION",)`; a title/text-pattern classifier was tried and removed — see §3a).
- **T-24h price reconstruction:** `earnings_mention.price_at_t24h(client, market)` reads `fetch_candlesticks(series_ticker, ticker, start_ts, end_ts)` (`GET /series/{s}/markets/{t}/candlesticks`) over the `[close−26h, close−23h]` window (hourly candles) and takes the last candle in it. Price fields are `*_dollars` strings, not cents. Returns `None` (permanent — no data ever existed in the window) or raises `RuntimeError` (transient — the request itself failed); these are NOT the same thing and are handled differently by the runner (§3a).
- **Realized settlement:** the `result` field ("yes"/"no") comes for free on each row from `fetch_settled_markets` — no separate per-market fetch needed. (`fetch_market(ticker)`, `GET /markets/{ticker}`, still exists as a general-purpose single-market lookup but is not on this pipeline's hot path.)
- **Join:** `richness_i = t24h_price_i − outcome_i`, `outcome_i ∈ {0,1}` (1.0 if `result=="yes"`).

### 3a. Redesign note (2026-07-05, post-build adversarial review)
Two build-time designs were tried and superseded before landing on the above, both caught by adversarial review before merge:
1. **Live-snapshot window (v1, reverted).** Originally: watch markets while they sit inside their own open `[close−24h, close−6h]` window, resolve later via a "pending" state machine. This cannot reliably catch a market whose entire lifetime falls between two weekly cron runs — a real coverage gap, not a timing nuance. Replaced with the settled-market candle-reconstruction method above (same method the in-sample pilot itself used — see `T1-pm-dispersion-novelty.md` §a, "T-24h/T-6h candle reconstruction"). There is no "pending" state as a result: state is a time watermark + a set of already-processed tickers + the accumulated resolved history.
2. **Loose title-pattern classifier (reverted).** `classify_market()` originally OR'd a text regex (`\bmention(s|ed)?\b`, `\bsay\b`, ...) against ANY settled market's title. That let unrelated markets like "Will Trump say recession?" pass and contaminate the sample. `SERIES_PREFIXES` is now the sole classifier, populated with the one prefix directly confirmed against both `T1-pm-dispersion-novelty.md` and the in-sample pilot's actual market list: `KXEARNINGSMENTION`. The broader KPI-bracket family this doc also mentions (e.g. `KXTESLAPROD`) is deliberately NOT covered yet — enumerating it correctly needs the full in-sample pilot market list, which this pipeline does not read from; a wrong/incomplete guess risks the same contamination this fix closes. Extending `SERIES_PREFIXES` is a follow-up once that list is available to whatever maintains this file.

Fail-closed handling, at two levels: a per-ticker transient failure (`RuntimeError`) is retried next cycle without advancing the watermark past it; a whole-cycle discovery failure (`RuntimeError`) aborts the cycle entirely (no state change, no alert) rather than computing a verdict from a silently-partial market list.

## 4. OOS statistical method
- Filter to the **11–50c YES band** at the reconstructed T-24h price.
- `richness_i = yes_price_i − outcome_i` (positive mean ⇒ YES overpriced ⇒ fade works).
- `mean_richness` (pts), `n`, `z = mean / (std/√n)`. Bucket by category/series for the **category-conditional** read (R2-1: the edge reversed in some categories — only deploy where it tests rich).
- Rolling OOS accumulation across weekly runs → v1 persists locally (JSON state file, `actions/cache` across scheduled runs — see the runner's module docstring for why this was kept over adding a Supabase table / a git-committed state blob). Migrating to Supabase (`engine='arbgrid'`, `lane='earnings_mention'`, `tax_bucket` per the 3-bucket ruleset) remains a clean follow-up once that table exists (§7 step 6).
- **Verdict toward 8/3:** PURSUE if `gap ≥ 9pts AND z ≥ 2 AND n ≥ 100`; KILL if `gap < 4.5pts`; else CONTINUE.

## 5. Module design (as shipped)
`earnings_mention.py` — pure functions plus one client-calling function (`price_at_t24h`), duck-typed against a client exposing only `fetch_candlesticks`:
- `classify_market(market) -> bool` — series-ticker prefix match (`SERIES_PREFIXES`); see §3a for why this is prefix-only, not text-pattern.
- `has_valid_result(market) -> bool` — does this settled market carry a definitive yes/no result (excludes voided/undecided).
- `price_at_t24h(client, market) -> float | None` — candlestick reconstruction; raises `RuntimeError` on a transient fetch failure, returns `None` (no raise) when the request succeeded but no usable candle exists.
- `build_resolved(market, yes_price_t24h) -> Resolved`.
- `compute_oos_stats(resolved) -> OosStats` — band filter, mean richness, z, by-category.
- `verdict(stats) -> "pursue"|"kill"|"continue"` — pre-registered thresholds.
- Pure/deterministic; no order surface; structurally cannot place a trade.

`scripts/run_earnings_mention_oos.py` — weekly entry, owns all stateful orchestration: load state (`{watermark_ts, seen, resolved, last_verdict, first_seen_ts}`) → `client.fetch_settled_markets(watermark)` → classify → resolve via `price_at_t24h` (fail-closed per §3a) → accumulate → compute → persist → Telegram-ticket the running verdict. `requests`-only; secrets Infisical-injected; Telegram failures never log the tokenized URL (fixed at the source in the shared `notifier.py`, since the bug predated and was not specific to this runner).

`.github/workflows/earnings-mention-oos.yml` — weekly cron + `workflow_dispatch` always_alert; env-routed; lean install; also uploads the state file as a 90-day build artifact (human-recoverable backup alongside the `actions/cache` restore path).

## 6. Tests (mocked, verifiable with NO live keys)
`tests/test_earnings_mention.py`:
- `classify_market`: confirmed `KXEARNINGSMENTION*` fixtures vs unrelated markets (including title-only false positives like "Will Trump say recession?" — a regression test for the reverted text-pattern classifier) → True/False.
- `price_at_t24h`: correct candle window/series-ticker derivation; last-candle-in-window selection; `*_dollars` field extraction incl. bid/ask-midpoint fallback; raises `RuntimeError` on a `None` (failed) fetch vs. returns `None` (no raise) on a genuinely empty candle list.
- `compute_oos_stats`: fixed fixture → **exact** mean richness + z (deterministic); band filter excludes <11c/>50c.
- `verdict`: pursue/kill/continue at thresholds (incl. n<100 → continue).
- Determinism: same input → same output; no network in unit tests.

`tests/test_run_earnings_mention_oos.py`: state roundtrip, the state-anomaly guard, and the full discover→reconstruct→accumulate→verdict wiring in `run_cycle`, including per-ticker fail-closed retry (transient vs. permanent) and whole-cycle abort on `RuntimeError`.

## 7. Build sequence
1. **Sync first** — local checkout is on stale `fix/layer4-continuous-wiring` (12 behind origin/master); branch `feat/earnings-mention-oos` **off origin/master** (which has edgar/fallen-angel/pnl/lip merged).
2. Add additive `fetch_market(ticker)` to `kalshi_api.py` + unit test.
3. Build `earnings_mention.py` + mocked tests → `pytest` green.
4. Add run script + workflow.
5. Codex adversarial review (no self-review) — ran 3 rounds: round 1 caught the live-snapshot-window coverage gap, the missing-exception-handling and silent-partial-pagination bugs, and the loose title-pattern classifier (all fixed, §3a); round 2 confirmed those fixes and found no new issues on this branch.
6. **[OP]** add Supabase + Telegram secrets → activate weekly cron → accumulate OOS to 8/3.

## 8. v2 (after keys — sharpens, not required for 8/3)
financialdatasets.ai **KPI guidance** + FMP **transcript base-rates** → per-event prior `p̂` (how often the company actually mentions X / hits the KPI) → fade only where `market_yes − p̂ ≥ threshold`, instead of blanket 11–50c. Offline pre-compute; deterministic gate. Wire via the Phase-2 MCP/data layer (command-center doc 20 §C).

## 9. Pre-registered gate (do not move)
**8/3 verdict:** OOS 11–50c gap **≥9pts & z≥2 (n≥100)** → PURSUE-NOW (authorize the separate passive-NO-ladder executor, ≤10% volume, ≤$300/market). gap **<4.5pts** → kill the lane. Else continue logging. No discretionary override.
