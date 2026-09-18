# Plan 10 — Kalshi reward-MM pilot prep (safety layer)

**Status:** IMPLEMENTED (branch `feat/mm-pilot-prep`) — see "Implementation deviations" at the end for the places where code reality differed from the draft. For the arbgrid repo (`~/Dev/polymarket-arb-scanner`).
**Strategy class:** Layer 3, passive market making for LIP/VIP reward capture (execution tracker queue item 4, second half).
**Effort:** Medium (hardens modules that already exist; the quote engine, trackers, and hedger are built — the live wiring and safety choke points are not).
**Flag:** `MM_KALSHI_PILOT_ENABLED` (default `false`). Independently gated from the legacy Polymarket `MM_ENABLED` path.
**Depends on:** PR #43 (`scans/lip_select.py` Phase A selector, currently in the `~/Dev/pm-arb-lip-rebase` worktree) landing first — market selection is its job, not this plan's.
**Venue:** Kalshi ONLY. Every order in this plan routes through `ENABLED_EXECUTION_PLATFORMS` (config.py:143-146) and is additionally hard-checked `platform == "kalshi"` at the choke point (§5). Sports categories excluded by the selector (`LIP_EXCLUDED_CATEGORIES` default `"Sports"`).
**Capital:** $2-3K pilot across 2-3 liquid non-sports Kalshi markets, ≤$300 gross per market, sized ≤ book depth. Tranche-1 rules from the portfolio command center apply.

## 0. Why this plan exists

Everything needed to *quote* already exists on `origin/master`; almost nothing needed to *survive being filled* does. Study findings this spec is built on:

| Component | State on origin/master | Gap for a live pilot |
|---|---|---|
| `MarketMaker` / `QuoteEngine` (market_maker.py:112-463) | Working quote math: min spread, inventory skew, volatility multiplier hook | Fine as-is |
| `QuoteManager.place_quote` (market_maker.py:209-252) | **Live path is a stub** — with a real trader it logs "MM quote:" and returns `None` (line 248-252). Only dry-run produces order IDs | Pilot needs a real Kalshi place/cancel path |
| Fill detection | **Does not exist.** `MarketMaker.on_fill` (market_maker.py:465-516) is never called in production; `continuous.py` never polls MM fills | Requirement 1 (§3-§4) |
| `InventoryTracker` (market_maker.py:33-105) | Per-market + total caps, but `can_trade` is only consulted inside `refresh_quotes` — an advisory pre-check, not an order-path gate. Defaults $500/market (config.py:411) exceed the pilot's $300 ceiling | Requirement 2 (§5) |
| `ToxicFlowDetector` (market_maker.py:1270-1395) | Deterministic pause logic wired into `refresh_quotes` (line 409-413), but `record_fill` is fed by nothing — with no fills recorded, toxicity is permanently 0.0 | Requirement 3 (§6) |
| `VolatilityTracker` (market_maker.py:1044-1147) | Spread multiplier wired into `QuoteEngine.calculate_quotes` (line 154-159); `record_price` wired in continuous.py:686-696 only when flags on | Requirement 3 (§6) |
| `PartialFillHedger.hedge_inventory` / `_hedge_kalshi` (hedger.py:288, 157) | Working sell-at-best-bid hedge with `max_loss` check and DB audit row | Reused as the hedge executor (§4) |
| `KalshiClient` (kalshi_api.py) | `place_order` (L312), `cancel_order` (L410), `get_order_status` (L399), `get_fills` (L361, REST `/portfolio/fills`, cursor pagination, `min_ts`), `get_order_book_depth` (L419) | Fill polling reuses `get_fills` (VIP-tracker precedent, kalshi_vip.py:150) |
| Kalshi WS (ws_feeds.py:375, 392) | `orderbook_delta` channel only — **no user fill channel implemented** | WS fills are a NON-goal; REST polling is the pilot mechanism |
| Kill switch | `bot_controls` Supabase table exists (supabase/migrations/0001_rewards_schema.sql:50-56) with `lip_bot_enabled` et al.; nothing in the trading loop reads it | §7 |
| `scans/toxic_flow_pause.py` | Observability-only mirror of detector state (correct; keep) | Unchanged |

The pilot go-live gate is therefore: **fills detected → hedged automatically → inventory hard-capped → toxic/vol gates in the hot path → operator kill switch → dry-run and canary phases proven.** No LIP reward is worth an unhedged inventory blowup on a $2-3K bankroll with a 4.70% hurdle.

## 1. Mechanism

One `KalshiMMPilot` orchestrator runs inside continuous mode:

```text
lip_select (PR #43, hourly)          orderbook_delta WS / REST poll
        │ top 2-3 markets                     │ mid updates
        ▼                                     ▼
┌──────────────────────────────────────────────────────────┐
│ KalshiMMPilot loop (every MM_REFRESH_INTERVAL, def 10s)  │
│                                                          │
│  for each market:  GATE CHAIN (§6, all deterministic)    │
│      pass → compute quotes (existing QuoteEngine)        │
│             → choke point check (§5)                     │
│             → cancel/replace GTC orders on Kalshi        │
│      fail → cancel resting quotes (fail closed)          │
│                                                          │
│  FillPoller (every MM_FILL_POLL_SECONDS, def 2s):        │
│      get_fills(min_ts) → dedupe → FillEvent              │
│      → InventoryTracker.update                           │
│      → ToxicFlowDetector.record_fill                     │
│      → HedgeController.on_fill (§4)                      │
│      → canary accounting (§8)                            │
└──────────────────────────────────────────────────────────┘
        │ any hedge failure / cap breach / kill switch
        ▼
   HALT: cancel ALL resting orders, set halted=true,
   Telegram alert, require manual restart
```

Deterministic code only — no LLM anywhere in this plan, hot path or otherwise. Quoting earns LIP score via resting orders (`kalshi_lip.KalshiLipScorer` accrues estimates; VIP tracker unchanged); the safety layer is what this plan ships.

## 2. Pre-registered requirements → sections

| # | Requirement (pre-registered, execution tracker item 4b) | Section |
|---|---|---|
| R1 | Auto-hedge on REAL fills at tiny size; fill detection, hedge sizing, latency budget, fail-closed | §3, §4 |
| R2 | `MM_MAX_INVENTORY` caps per-market AND total, enforced in the order path, quoting stops/one-sides on breach | §5 |
| R3 | Toxic-flow / volatility gates in the quoting hot path, deterministic, spread widening + quote pulling | §6 |

## 3. Fill detection (R1a)

**Mechanism: REST polling of `KalshiClient.get_fills`** — the capability the codebase already has and already exercises (VIP tracker, kalshi_vip.py:150-166). The Kalshi WS user-fill channel is explicitly out of scope (§NON-goals); `ws_feeds.py` speaks only `orderbook_delta` today and adding an authenticated user channel is new surface the pilot does not need at 2-3 markets.

- Poll interval: `MM_FILL_POLL_SECONDS` (default `2.0`). At pilot size the expected fill rate is well under one per minute; 2s keeps the hedge latency budget (§4) honest without rate-limit pressure (fills poll shares the global Kalshi rate limiter in `kalshi_api._rate_limit`).
- Query: `get_fills(min_ts=last_seen_ts - 60)` with a 60s overlap window; dedupe on the fill's unique id (`trade_id`; fall back to `(order_id, created_time, count)` tuple if absent). Seen-id set bounded to the last 1,000 fills.
- Only fills whose `order_id` is in the pilot's own resting-order registry are attributed to the pilot. **A fill on an unknown order_id in a pilot market is a deviation → canary/halt logic (§8) fires.** (Prevents silent interference with the arb executor's Kalshi orders.)
- Each accepted fill becomes a `FillEvent` and is written to the local SQLite trade log with `strategy="KalshiMMPilot"` and `tax_bucket="ordinary"` (operating rule 5: tagged from trade one), then mirrored by the existing `supabase_sync` path.

```python
@dataclass(frozen=True)
class FillEvent:
    fill_id: str
    order_id: str
    ticker: str
    side: str            # "yes" | "no"
    action: str          # "buy" | "sell"
    count: int           # contracts
    price: float         # dollars 0.01-0.99 (from cents, kalshi_vip.fill_price_dollars)
    is_taker: bool       # True should never happen for resting quotes → deviation
    created_ts: float
    mid_at_detect: float # book mid when we detected the fill (for toxicity)
```

## 4. Auto-hedge on real fills (R1b)

**Owner:** new `HedgeController` in `market_maker.py` (or `mm_pilot.py`), composing the existing `PartialFillHedger` — `hedge_inventory` → `_hedge_kalshi` (sell at best bid with `max_loss` guard, hedger.py:157-182) is the placement primitive and is not rewritten.

**Hedge decision, per fill:**

1. Update `InventoryTracker` with the signed dollar delta.
2. `excess = |net_inventory_usd| - MM_INVENTORY_TARGET_USD` (default target `0.0` for the pilot: flatten toward zero).
3. If `excess <= MM_HEDGE_DEADBAND_USD` (default `5.0` — roughly one quote's worth): no hedge order; the existing inventory skew in `QuoteEngine` works the position off passively. This is the "rebalance" arm.
4. Else place a reducing order for `excess` dollars via `hedge_inventory(...)` — sell the over-long side at best bid (or buy back the over-short side at best ask; extend `_hedge_kalshi` with the buy-reduce direction, currently sell-only).
5. Hedge order is IOC-style: place at touch, `_confirm_fill_kalshi`-poll (executor.py:3227) up to `FILL_POLL_TIMEOUT`, cancel remainder on timeout, re-evaluate `excess`.

**Latency budget (fill → hedge order on the wire):**

| Stage | Budget |
|---|---|
| Fill detection (poll) | ≤ `MM_FILL_POLL_SECONDS` = 2s worst case |
| Decision + book fetch | ≤ 1s |
| Order placement | ≤ 2s |
| **Total, hard ceiling** | **≤ 5s p95, 10s absolute (`MM_HEDGE_MAX_LATENCY_SECONDS`, default `10`)** |

If the absolute ceiling is exceeded before the hedge order is accepted, treat as hedge failure.

**Failure handling — fail closed, no exceptions:**

| Hedge failure mode | Detection | Action |
|---|---|---|
| Book empty / no bid on reduce side | `_hedge_kalshi` returns False (bid_info None) | PULL all quotes in that market, market → `halted` |
| Hedge loss would exceed `max_loss` (existing hedger guard) | `loss > max_loss` branch | Same: pull quotes, halt market — do NOT quote on while carrying an unhedgeable position |
| API error / rate limit / timeout | exception or latency ceiling | Retry once; second failure → pull quotes, halt market |
| Any market halted twice in `MM_HALT_WINDOW_SECONDS` (def 3600) | halt counter | HALT the whole pilot (all markets), Telegram alert |

"Pull quotes" = cancel every resting pilot order in the market via `cancel_order` and refuse to re-quote until inventory is manually reconciled or the position is flattened. A market carrying inventory it cannot hedge never keeps live quotes — that is the fail-closed contract.

`MM_AUTO_HEDGE_ENABLED` (config.py:260) must be `true` for the pilot; the pilot refuses to start live (non-dry-run) with it false — hedging is not optional here, unlike the legacy MM path.

## 5. Inventory caps — hard, in the order path (R2)

**Choke point:** one function every pilot order (quote, requote, and hedge alike) must pass through — there is no second code path that can reach `kalshi_client.place_order` for the pilot:

```python
def authorize_order(self, ticker: str, side: str, action: str,
                    count: int, price: float) -> GateResult:
    """The ONLY gate to place_order for the pilot. Deterministic, thread-safe.
    Checks, in order: kill switch state, platform allowlist ("kalshi" in
    ENABLED_EXECUTION_PLATFORMS and platform hardcoded "kalshi"), halted flag,
    per-market caps, total caps, per-order size, book-depth ratio.
    Returns GateResult(allowed: bool, reason: str). Reason is logged and
    written to the decisions audit trail on every rejection."""
```

Hedge/reducing orders (orders that strictly decrease `|net_inventory|`) bypass the *inventory* checks only — caps must never block the exit — but still pass kill-switch, platform, and size checks.

**Units — both enforced, most restrictive wins:**

| Config key | Meaning | Pilot default | Rationale for $2-3K / ≤$300 per market |
|---|---|---|---|
| `MM_MAX_INVENTORY_USD` | max **net** inventory per market, dollars at cost | `100.0` | leaves ≥$200 headroom to the $300 gross ceiling for resting quotes both sides |
| `MM_MAX_INVENTORY_CONTRACTS` | max net inventory per market, contracts | `250` | ≈$100 at mid prices; binds tighter at low prices where contract risk ≠ dollar cost |
| `MM_MAX_TOTAL_INVENTORY_USD` | max **total** net inventory across all pilot markets | `250.0` | ~10% of the $2-3K pilot bankroll can be directional at once |
| `MM_MAX_GROSS_PER_MARKET_USD` | inventory at cost + open resting-quote notional, per market | `300.0` | the pre-registered ≤$300/market ceiling, enforced not advisory |
| `MM_QUOTE_SIZE_USD` | per-quote size (post-canary) | `10.0` | small vs. LIP `MIN_TARGET_SIZE`=100 contracts; scale later, not now |

The legacy `MM_MAX_INVENTORY=500.0` / `MM_MAX_TOTAL_EXPOSURE=500.0` (config.py:411-412) stay untouched for the old path; the pilot reads only its own `MM_MAX_*` keys above. `validate_config()` gains: pilot enabled ⇒ `MM_MAX_INVENTORY_USD ≤ MM_MAX_GROSS_PER_MARKET_USD ≤ 300` and `MM_MAX_TOTAL_INVENTORY_USD ≤ 0.15 * pilot bankroll` sanity warnings.

**Behavior at cap (stop / one-side, not clamp-and-continue):**

| Condition | Quoting behavior |
|---|---|
| net long ≥ per-market cap (either unit) | **one-side**: cancel + stop the bid; keep/refresh the ask (reduces inventory) |
| net short ≥ per-market cap | one-side mirror: stop the ask, keep the bid |
| total cap hit | stop the accumulating side in **every** market; only inventory-reducing quotes remain |
| gross cap hit | cancel newest resting orders in that market until gross < cap; no new quotes |

One-siding is computed inside the refresh loop AND re-verified by `authorize_order` — belt and suspenders, since refresh-loop state can be one cycle stale.

## 6. Toxic-flow / volatility gates in the hot path (R3)

The detector and tracker exist and are already deterministic; this plan **feeds them real data and makes their verdicts binding** before any quote is placed. All gates below run inside the quote loop, before `authorize_order`, in fixed order; first failure short-circuits. No network calls inside the gate evaluation itself (inputs are pre-fetched state).

**Data feeds (new wiring):**
- `ToxicFlowDetector.record_fill(ticker, side, price, count*price, mid_at_detect)` from every `FillEvent` (§3). Adverse = mid moved through our quote (existing logic, market_maker.py:1316-1320).
- `VolatilityTracker.record_price(ticker, mid)` on every `orderbook_delta` WS tick for subscribed pilot tickers (ws_feeds already delivers these) and, as fallback, on every refresh-loop book fetch. Pilot tickers are added to the WS subscription set (`FeedManager.subscribe_kalshi`).
- `MM_TOXIC_FLOW_ENABLED` and `MM_VOLATILITY_ADJUSTED_ENABLED` are forced-on preconditions for the pilot (refuse live start if false), so the existing flag-gated no-ops become active.

**Deterministic pre-quote gate table** (evaluated per market per refresh; "PULL" = cancel resting quotes in market and skip quoting this cycle):

| # | Gate | Input | Threshold (default) | On fail |
|---|---|---|---|---|
| G1 | Kill switch | `bot_controls.mm_pilot_enabled` cache + `MM_KALSHI_PILOT_ENABLED` env | must be true & fresh (§7) | PULL all markets, halt |
| G2 | Venue allowlist | leg platform | `"kalshi" ∈ ENABLED_EXECUTION_PLATFORMS`, platform literal `"kalshi"` | PULL, halt (config error) |
| G3 | Market halted flag | §4 failures, §8 canary | not halted | skip market |
| G4 | Still selected | latest `select_lip_markets` output (PR #43) | ticker in top-N and passes its filters (pool ≥ `LIP_MIN_POOL`, non-excluded category, ≥ `LIP_MIN_HOURS_REMAINING` to close/program end) | PULL (graceful exit from de-selected market) |
| G5 | Price band | book mid | `LIP_PRICE_BAND_LOW=0.10 ≤ mid ≤ LIP_PRICE_BAND_HIGH=0.90` | PULL (tail/binary-gap risk) |
| G6 | Book staleness | age of last book/WS update | ≤ `MM_BOOK_MAX_STALE_SECONDS` (def `30`) | PULL (never quote blind) |
| G7 | Toxic-flow pause | `ToxicFlowDetector.should_pause(ticker)` (toxicity ≥ `MM_TOXIC_FLOW_THRESHOLD=0.60` over last 20 fills, or active pause window `MM_TOXIC_FLOW_PAUSE_SECONDS=60`) | must be false | PULL for the pause window; `scans/toxic_flow_pause.py` surfaces it |
| G8 | Volatility ceiling | `VolatilityTracker.get_spread_multiplier(ticker)` | < `MM_VOL_PULL_MULTIPLIER` (def `2.5`, below the tracker's 3.0 clamp) | PULL until multiplier decays |
| G9 | Volatility widening | same multiplier, `1.0 < m < ceiling` | — | quote with widened spread (existing QuoteEngine hook, market_maker.py:154-159) — degrade before withdrawing |
| G10 | Inventory caps | §5 state | under caps / one-side rules | one-side or PULL per §5 table |
| G11 | Depth sizing | top-of-book resting size | quote count ≤ `MM_MAX_BOOK_DEPTH_FRACTION` (def `0.25`) × same-side best size, and ≥ Kalshi 1-contract min | shrink quote; if < 1 contract, skip side |
| G12 | Crossing guard | our bid < best ask, our ask > best bid (post-only semantics) | non-crossing | reprice to one tick inside; if impossible, skip side |

Every gate decision (pass/fail + reason) is appended to the existing decisions audit trail (`_write_decision` pattern, executor.py:3433) so post-mortems are replayable. Toxicity additionally arms `trigger_pause` when a fill batch pushes the ratio over threshold, so the pause outlasts the instantaneous computation.

## 7. Kill switch & halt integration

- **Supabase `bot_controls`:** add row `('mm_pilot_enabled', false)` in a new idempotent migration (mirrors 0001's `lip_bot_enabled` seed). A `ControlsPoller` (reusing `supabase_sync.build_client_from_env`) refreshes the value every `MM_CONTROLS_POLL_SECONDS` (def `60`) into a timestamped local cache.
- **Fail closed on control-plane loss:** if the cached value is older than `MM_CONTROLS_MAX_STALE_SECONDS` (def `300`) because Supabase is unreachable, G1 fails → pull all quotes and halt. Unknown operator intent = off.
- **Local overrides (any one suffices to stop):** `MM_KALSHI_PILOT_ENABLED=false` env, `DRY_RUN=true` (forces dry-run regardless of other flags — existing semantic, config.py:133), SIGTERM handler cancels all resting pilot orders before exit (extend `MarketMaker.stop`, market_maker.py:564, to actually call `cancel_order` per live order id — today it only clears the local dict).
- **Alert bus:** every halt, hedge failure, cap breach, and canary deviation emits through the existing `AlertManager` (alerting.py) → ClaudeClaw Telegram, severity ≥ WARNING; halts are CRITICAL.

## 8. Canary sizing & auto-halt on deviation

Live trading begins in canary mode, always:

- First `MM_CANARY_FILLS` (default `10`) fills execute at `MM_CANARY_QUOTE_SIZE_USD` (default `2.0` — one to a few contracts) regardless of `MM_QUOTE_SIZE_USD`.
- **Deviation triggers — any one auto-halts the whole pilot** (cancel all, `halted=true`, CRITICAL alert, manual restart required):
  1. Any hedge failure (§4 table).
  2. Fill on an order_id not in the pilot registry, or a fill with `is_taker=true` (a resting quote should never be the taker).
  3. Cumulative realized pilot P&L < `-MM_CANARY_MAX_LOSS_USD` (default `10.0`).
  4. Hedge latency ceiling exceeded (even if the hedge eventually lands).
  5. Inventory observed above a §5 cap (caps are pre-trade; observing a breach post-trade means the choke point leaked — the worst possible bug).
  6. Toxicity ≥ threshold on ≥ 2 of the pilot markets simultaneously.
- Graduation to `MM_QUOTE_SIZE_USD` happens only after `MM_CANARY_FILLS` clean fills AND ≥ `MM_CANARY_MIN_HOURS` (default `24`) of live runtime — then logged as an explicit "CANARY PASSED" audit event. No automatic size increases beyond that; tranche moves are operator decisions per the capital policy.

**Rollout phases (each requires the previous to be verified):**

| Phase | Mode | Exit gate |
|---|---|---|
| D0 | `DRY_RUN=true` — full loop incl. simulated fills (dry-run order IDs, synthetic FillEvents from book crosses) | 48h clean: gates firing correctly in logs, zero exceptions, decisions audit complete |
| D1 | Live, canary sizing | §8 graduation gate |
| D2 | Live, pilot sizing ($2-3K, 2-3 markets, ≤$300/market) | day-45/60 tranche review per tracker |

## 9. Files to touch

| File | Change |
|---|---|
| `mm_pilot.py` (new) | `KalshiMMPilot` orchestrator, `FillPoller`, `HedgeController`, `ControlsPoller`, `authorize_order` choke point, gate chain, canary state machine |
| `market_maker.py` | `QuoteManager` gains a real Kalshi place/cancel backend (delegating to `kalshi_client.place_order`/`cancel_order`, GTC, non-crossing G12); `MarketMaker.stop` cancels live orders; no changes to QuoteEngine math |
| `hedger.py` | `_hedge_kalshi` gains buy-to-reduce direction (currently sell-only) |
| `config.py` | `MM_KALSHI_PILOT_ENABLED` + the `MM_*` keys tabled in §4-§8; `validate_config()` pilot invariants (§5); forced-precondition checks (§4, §6) |
| `continuous.py` | Instantiate + drive `KalshiMMPilot` when flag on; subscribe pilot tickers to Kalshi WS; feed `VolatilityTracker.record_price`; wire `scan_toxic_flow_pause(pilot_tickers)` observability |
| `supabase/migrations/0004_mm_pilot_controls.sql` (new) | Seed `mm_pilot_enabled=false` in `bot_controls` (0003 was already taken by the pnl schema — deviation #1) |
| `db.py` | UNCHANGED — the existing `opportunities` (`type="KalshiMMPilot"`) + `trades` tables cover fill/hedge rows; no new column was needed (deviation #8) |
| `tests/test_mm_pilot.py`, `tests/test_mm_pilot_gates.py` (new); `tests/test_hedger.py` (extend) | §10 |
| `docs/plans/10-mm-pilot-prep.md` | This spec, finalized, once approved |
| `CLAUDE.md` / `docs/strategy-framework-v2.md` | Register flag + mode |

## 10. Test plan — fail-before cases

Every safety property gets a test that **fails on today's origin/master behavior** (or on a deliberately broken stub) before the fix makes it pass. `sys.modules` SDK-stub pattern from `test_executor.py`; fake `KalshiClient` with scripted `get_fills` / `place_order` / order books.

1. **Fill detection:** scripted `get_fills` returns a fill for a registered order → exactly one `FillEvent`; duplicate fill across two polls (overlap window) → still one event. *Fail-before:* no fill event on master (nothing polls).
2. **Unknown-order fill → halt:** fill with foreign order_id → pilot halted, all cancels issued. *Fail-before:* silently updates nothing.
3. **Auto-hedge fires:** fill pushes `|inventory|` past deadband → `hedge_inventory` called with `excess`, reduce direction correct for long and short. *Fail-before:* `on_fill` never invoked live.
4. **Hedge fail ⇒ quotes pulled:** book with no bids → hedge returns False → assert every resting order in that market cancelled and market halted; next refresh places zero quotes. *Fail-before:* master keeps quoting after a failed hedge (auto-hedge exception is swallowed, market_maker.py:514-516).
5. **Latency ceiling:** frozen-clock test — hedge confirm exceeds `MM_HEDGE_MAX_LATENCY_SECONDS` → halt path taken.
6. **Per-market cap in the order path:** drive fills to $99 net, next accumulating quote of $10 → `authorize_order` rejects with reason `per_market_inventory_cap`; reducing order of $10 → allowed. *Fail-before:* `InventoryTracker.can_trade` is never consulted at placement time on master's live stub.
7. **Contract-unit cap binds independently:** low-price market where 250 contracts < $100 → contract cap rejects first.
8. **Total cap one-sides all markets:** total $250 net long across 3 markets → bids stopped in all three, asks still quoted.
9. **Gross ≤ $300:** inventory $100 + resting $190, new $20 quote → rejected.
10. **No bypass:** grep-level test asserting `kalshi_client.place_order` is referenced exactly once in `mm_pilot.py`/pilot-owned `QuoteManager` code, behind `authorize_order` (plus a runtime assertion counter in tests).
11. **Toxicity pause pulls quotes:** feed 20 fills, 13 adverse (ratio 0.65 ≥ 0.60) → G7 fails, quotes cancelled, `scan_toxic_flow_pause` emits the observability opp; ratio 0.55 → quotes placed. *Fail-before:* with `record_fill` unwired, ratio stays 0.0 and the pause never triggers on master.
12. **Vol widening then pulling:** price series with multiplier ≈ 2.0 → spread widened (assert wider than base); multiplier ≥ 2.5 → G8 pulls.
13. **Kill switch:** `bot_controls` cache flips false → next cycle cancels everything; cache older than 300s → same (fail closed on control-plane loss).
14. **Canary:** 10th clean fill + 24h fake clock → graduation event; canary loss $10.01 → halt; canary fill sized > `MM_CANARY_QUOTE_SIZE_USD` → deviation halt.
15. **Dry-run isolation:** `DRY_RUN=true` → zero calls on the fake client's `place_order`/`cancel_order` across a full simulated session.
16. **Config invariants:** pilot enabled with `MM_AUTO_HEDGE_ENABLED=false` or `MM_TOXIC_FLOW_ENABLED=false` → `validate_config` refuses live start.
17. **Crossing guard:** best ask 0.52, computed bid 0.53 → repriced to 0.51 or side skipped; never a marketable quote.

## 11. Verification

```bash
pip install -r requirements.txt -r requirements-dev.txt
pytest tests/test_mm_pilot.py tests/test_mm_pilot_gates.py tests/test_hedger.py -v
pytest tests/ -q                                   # full suite still green
```

### D0 48-hour dry-run soak launch

The D0 command is intentionally isolated from every legacy market-maker and
reward-execution path. Run it from the repository root only after PR #43 is in
the base, migration `0004_mm_pilot_controls.sql` is applied, read-only Kalshi
and Supabase credentials are injected through the approved secret manager, and
an operator has set `bot_controls.mm_pilot_enabled=true` for the D0 environment.
The command itself does not change that control or any account state.

```bash
SOAK_ID="$(date -u +%Y%m%dT%H%M%SZ)"
SOAK_DIR="artifacts/mm-d0/${SOAK_ID}"
mkdir -p "$SOAK_DIR"

DRY_RUN=true \
EXECUTION_MODE=semi-auto \
ENABLED_EXECUTION_PLATFORMS=kalshi \
MM_KALSHI_PILOT_ENABLED=true \
MM_ENABLED=false \
REWARDS_ENABLED=false \
KALSHI_MULTI_ENABLED=false \
KALSHI_LIP_ENABLED=true \
MM_AUTO_HEDGE_ENABLED=true \
MM_TOXIC_FLOW_ENABLED=true \
MM_VOLATILITY_ADJUSTED_ENABLED=true \
DATA_DIR="$SOAK_DIR" \
MM_DECISIONS_LOG_PATH="$SOAK_DIR/decisions.jsonl" \
MM_STATE_PATH="$SOAK_DIR/mm_pilot_state.json" \
python scanner.py --continuous --mode mm-pilot --dry-run --interval 60 \
  --dashboard-port 0 --log-level INFO --log-file "$SOAK_DIR/scanner.log"
```

Let the process run for 48 continuous hours, then stop it with one `SIGINT` or
`SIGTERM` so the normal cleanup path runs. Do not set `DRY_RUN=false`, do not
enable the legacy `MM_ENABLED`/`REWARDS_ENABLED` paths, and do not advance to D1
from this command.

The review artifact is the timestamped `$SOAK_DIR` directory:

- `scanner.log` — startup configuration, gate fires, exceptions, and graceful shutdown.
- `decisions.jsonl` — append-only G0-G12/order/halt decision audit trail.
- `mm_pilot_state.json` — restart/reconciliation checkpoint.
- `trades.db` — isolated dry-run opportunity/fill ledger.

D0 passes only if the folder covers at least 48 hours, every decision row is
valid JSON, both dry-run venue mutation counters remain zero, there are no
unhandled exceptions, and observed gate behavior matches the pre-registered
§10 cases. D0 never graduates the canary; `DRY_RUN=true` prevents graduation by
construction.

## 12. Definition of Done

- All §10 tests green, including every fail-before case demonstrated failing first (commit history or PR notes show the red state).
- `authorize_order` is the provably single route to `place_order` for pilot code (test 10).
- A simulated fill produces: inventory update → toxicity record → hedge (or deadband skip) → audit rows, end to end, in dry-run.
- Hedge failure, cap breach, kill switch, and canary deviation each demonstrably cancel all resting orders in scope (tests 4, 6, 13, 14).
- 48h D0 dry-run soak clean; D1 canary criteria wired and asserted, not aspirational.
- `bot_controls.mm_pilot_enabled` migration applied; flipping it in Supabase stops quoting within one poll interval + one refresh cycle, verified live in D0 (dry-run orders).
- Flag default `false`; full suite green; strategy + flags registered in `docs/strategy-framework-v2.md` and repo `CLAUDE.md`.

## 13. NON-goals (explicit)

- **No Polymarket MM** and no changes to the legacy `MM_ENABLED` Polymarket path beyond shared-class fixes; venue is Kalshi only.
- **No sports markets** (selector excludes; operating rule 3 untouched).
- **No Kalshi WS user-fill channel** — REST polling only for the pilot; WS fills are a post-pilot latency optimization, reconsider at tranche 2.
- **No LLM anywhere** — not in gates, not in market selection, not in sizing, not in post-mortems that feed parameters.
- **No market-selection logic** — that is PR #43's `select_lip_markets`; this plan consumes its output.
- **No dynamic/adaptive sizing** beyond the canary → pilot step; no Avellaneda-Stoikov or fancier quoting math; the existing `QuoteEngine` is sufficient for a $2-3K reward-capture pilot.
- **No capital-pool sharing** with other engines (operating rule 4); pilot inventory caps are independent of arb executor budgets.
- **No tranche-2 scaling parameters** — day-45/60 review owns that decision.

## Side-findings (log separately, do not fix in this PR)

1. `MarketMaker.stop` (market_maker.py:564-568) "cancels" orders only in the local dict — live resting orders would survive process death. Fixed here for the pilot path; the legacy path deserves the same fix.
2. `QuoteManager.place_quote` live branch returning `None` (market_maker.py:248-252) means the legacy `MM_ENABLED` path has never been capable of live quoting — worth a docs note so nobody assumes otherwise.
3. `continuous.py:1777` registers MM markets as Polymarket-only; if the legacy path is ever revived it silently ignores Kalshi.
4. `PartialFillHedger._hedge_kalshi` is sell-only (hedger.py:157); short-side MM inventory from the legacy path would be unhedgeable today.

## Implementation deviations (code reality vs. this draft)

1. **Migration is `0004_mm_pilot_controls.sql`, not `0003`** — `0003_pnl_schema.sql`
   already existed on master when this landed.
2. **The legacy `QuoteManager` did NOT gain a live place backend.** Section 9
   proposed giving `market_maker.QuoteManager` a real Kalshi place/cancel path,
   but that would have created a second code path to `kalshi_client.place_order`
   and silently given the legacy `MM_ENABLED` Polymarket path live capability —
   both contrary to §5's "no second code path" contract. Instead, ALL pilot
   placement lives in `mm_pilot.py` behind `authorize_order`, and the legacy
   `QuoteManager`/`MarketMaker.stop` gained live **cancel** support only
   (side-finding 1 fixed: cancels now reach the exchange when a trader is passed).
3. **Hedge placement routes through the choke point via a client proxy.**
   `PartialFillHedger._hedge_kalshi` remains the placement primitive (touch
   price + `max_loss` guard unmodified, §4), but the pilot hands it a
   `_PilotKalshiProxy` whose `place_order` goes back through
   `authorize_order`/`place_pilot_order`. This reconciles §4 ("hedger places")
   with §5 ("there is no second code path that can reach place_order") and
   registers hedge order ids so hedge fills are attributable (otherwise the
   unknown-order deviation in §3 would false-positive on our own hedges).
4. **Hedge confirm-poll (§4 step 5) is implemented as fill_or_kill + a
   pending-hedge latency sweep**, not a `_confirm_fill_kalshi` poll:
   `hedge_inventory` returns a bool, not an order id, so the pilot places
   hedges `fill_or_kill` at touch and `_check_pending_hedges` (each fill-poll
   cycle) cancels any hedge order still resting past
   `MM_HEDGE_MAX_LATENCY_SECONDS` and takes the hedge-failure halt path.
   Hedge fills come back through the same `FillPoller`.
5. **G4 with PR #43 not landed:** `scans/lip_select.py` does not exist on
   master yet. `continuous.py` imports it if available; without it the pilot
   never receives a selection snapshot and G4 fails closed — the pilot places
   zero quotes. No substitute selection logic was added (NON-goal).
6. **Pilot hedge direction is always sell-to-reduce** (the pilot only quotes
   buy-YES/buy-NO, so inventory is always a holding). The §4 buy-to-reduce
   direction was still added to `PartialFillHedger._hedge_kalshi`
   (`action="buy"` via the `reduce_action` identifier) for true shorts on the
   legacy path (side-finding 4) and is tested.
7. **`MM_PILOT_BANKROLL_USD` (default 2000.0)** was added so the
   `MM_MAX_TOTAL_INVENTORY_USD <= 0.15 * pilot bankroll` sanity warning in
   `validate_config()` has a concrete denominator.
8. **Fill/trade rows carry `strategy="KalshiMMPilot"` via the opportunities
   table** (`opportunities.type`, joined by the existing P&L views); the
   `trades` table has no strategy/tax_bucket columns. `tax_bucket="ordinary"`
   is stamped on every pilot decision-audit row, and the shared Supabase pnl
   schema (0003) already tags engine/lane/tax_bucket at the portfolio layer.
9. **Canary graduation is live-only** (`DRY_RUN=true` never graduates) — D0
   soaks stay at canary sizing by construction.
