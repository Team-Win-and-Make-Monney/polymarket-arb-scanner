"""Kalshi Liquidity Incentive Program (LIP) snapshot scoring.

Deterministic, pure scoring of resting limit orders under Kalshi's LIP rules
(https://help.kalshi.com/en/articles/13823851-liquidity-incentive-program).

Program mechanics implemented here:
  * Kalshi snapshots the book every second during trading hours.
  * Only resting size that helps reach the per-period Target Size qualifies.
  * Each qualifying order scores ``size * distance_multiplier`` where the
    multiplier is ``1.0`` at the same-side best price and decays by the
    Discount Factor for every tick away from it: ``discount_factor ** ticks``.
  * A participant's period score is the sum of its per-snapshot scores.
  * Reward = ``(your_score / total_market_score) * reward_pool``.

No network, no auth, no LLM — this is the deterministic scoring core that the
reward routines call. Kalshi exposes no per-snapshot scoring API, so this
mirrors the published formula to estimate accrual locally.
"""

from __future__ import annotations

from datetime import datetime
import math
import threading

# Kalshi contract prices move in $0.01 ticks (1 cent).
KALSHI_TICK = 0.01

# Program bounds on Target Size, per the LIP help article.
MIN_TARGET_SIZE = 100
MAX_TARGET_SIZE = 20000


def _normalized_levels(levels: list[tuple[float, float]]) -> list[tuple[float, float]]:
    """Drop malformed/non-finite/non-positive size levels before scoring."""
    cleaned: list[tuple[float, float]] = []
    for row in levels:
        try:
            price, size = row[0], row[1]
            px, qty = float(price), float(size)
        except (TypeError, ValueError, IndexError):
            continue
        if not math.isfinite(px) or not math.isfinite(qty) or qty <= 0:
            continue
        cleaned.append((px, qty))
    return cleaned


def reference_price(levels: list[tuple[float, float]], target_size: float) -> float | None:
    """Published LIP Reference Price: walk from best until target_size/5 fills.

    Kalshi scores against this depth-weighted reference, not the naked best
    bid. Penny-bids far below the touch therefore contribute ~zero qualifying
    depth (measured 2026-08-16).
    """
    if target_size <= 0:
        return None
    need = target_size / 5.0
    cumulative = 0.0
    for price, size in sorted(_normalized_levels(levels), key=lambda row: -row[0]):
        cumulative += size
        if cumulative >= need:
            return price
    return None


def ticks_worse(order_price: float, ref_price: float, tick: float = KALSHI_TICK) -> int:
    """Ticks the order sits worse than the reference (better-than-ref = 0)."""
    if tick <= 0:
        raise ValueError(f'tick must be positive, got {tick!r}')
    return max(0, int(round((ref_price - order_price) / tick)))


def qualifying_share(
    levels: list[tuple[float, float]],
    target_size: float,
    discount_factor: float,
    quote_price: float,
    quote_size: float,
    tick: float = KALSHI_TICK,
) -> tuple[float | None, str | None]:
    """Share of distance-discounted qualifying depth for one additional quote.

    HTTP/parse failures must be handled by the caller — this function never
    coerces missing books to zero depth.
    """
    if discount_factor < 0.0 or discount_factor > 1.0:
        raise ValueError(f'discount_factor must be in [0, 1], got {discount_factor!r}')
    ref = reference_price(levels, target_size)
    if ref is None:
        return None, "no_refprice"
    raw_depth = 0.0
    others = 0.0
    for price, size in levels:
        try:
            px = float(price)
            qty = float(size)
        except (TypeError, ValueError):
            continue
        raw_depth += qty
        others += qty * (discount_factor ** ticks_worse(px, ref, tick))
    if raw_depth < target_size:
        return None, "below_target"
    mine = float(quote_size) * (discount_factor ** ticks_worse(float(quote_price), ref, tick))
    denom = others + mine
    if denom <= 0:
        return None, "zero_qualifying"
    return mine / denom, None


def tick_distance(order_price: float, reference_price: float, tick: float = KALSHI_TICK) -> int:
    """Return the whole-tick distance between an order price and the reference.

    Args:
        order_price: Resting order price in dollars (0.01-0.99).
        reference_price: Same-side best price in dollars.
        tick: Price increment in dollars (default $0.01).

    Returns:
        Non-negative integer number of ticks between the two prices.
    """
    if tick <= 0:
        raise ValueError(f'tick must be positive, got {tick!r}')
    return int(round(abs(order_price - reference_price) / tick))


def distance_multiplier(
    order_price: float,
    reference_price: float,
    discount_factor: float,
    tick: float = KALSHI_TICK,
) -> float:
    """Compute the LIP distance multiplier for one order.

    Orders at the same-side best price get full credit (1.0). Orders further
    out are penalised by ``discount_factor`` per tick: ``discount_factor ** ticks``.
    A Discount Factor of 1.0 applies no penalty; lower values penalise harder.

    Args:
        order_price: Resting order price in dollars.
        reference_price: Same-side best price in dollars.
        discount_factor: Per-tick decay in [0, 1].
        tick: Price increment in dollars.

    Returns:
        Multiplier in [0, 1].

    Raises:
        ValueError: If discount_factor is outside [0, 1].
    """
    if discount_factor < 0.0 or discount_factor > 1.0:
        raise ValueError(f'discount_factor must be in [0, 1], got {discount_factor!r}')
    if discount_factor == 1.0:
        return 1.0
    ticks = tick_distance(order_price, reference_price, tick)
    if ticks == 0:
        return 1.0
    return discount_factor ** ticks


def _side_snapshot_score(
    orders: list[dict],
    reference_price: float,
    target_size: float,
    discount_factor: float,
    tick: float = KALSHI_TICK,
) -> float:
    """Score one side of the book for a single snapshot.

    Only size that helps reach ``target_size`` qualifies. Orders are consumed
    closest-to-best first so the most valuable (highest-multiplier) size counts
    toward the cap before further-out orders.

    Args:
        orders: Same-side resting orders, each ``{"price": float, "size": float}``.
        reference_price: Same-side best price in dollars.
        target_size: Qualifying depth cap for this side (contracts).
        discount_factor: Per-tick decay in [0, 1].
        tick: Price increment in dollars.

    Returns:
        Snapshot score for this side.
    """
    if target_size <= 0:
        return 0.0

    ranked = sorted(
        orders,
        key=lambda o: tick_distance(o['price'], reference_price, tick),
    )

    remaining = target_size
    score = 0.0
    for order in ranked:
        if remaining <= 0:
            break
        size = min(float(order['size']), remaining)
        if size <= 0:
            continue
        multiplier = distance_multiplier(order['price'], reference_price, discount_factor, tick)
        score += size * multiplier
        remaining -= size
    return score


def snapshot_score(
    orders: list[dict],
    best_bid: float | None,
    best_ask: float | None,
    target_size: float,
    discount_factor: float,
    tick: float = KALSHI_TICK,
) -> float:
    """Score a participant's resting orders for a single one-second snapshot.

    Bids are scored against ``best_bid`` and asks against ``best_ask``; each side
    qualifies up to ``target_size``.

    Args:
        orders: Resting orders, each ``{"side": "bid"|"ask", "price": float, "size": float}``.
        best_bid: Current best bid price in dollars (None if no bid side).
        best_ask: Current best ask price in dollars (None if no ask side).
        target_size: Qualifying depth cap per side (contracts).
        discount_factor: Per-tick decay in [0, 1].
        tick: Price increment in dollars.

    Returns:
        Total snapshot score across both sides.
    """
    bids = [o for o in orders if o.get('side') == 'bid']
    asks = [o for o in orders if o.get('side') == 'ask']

    total = 0.0
    if best_bid is not None and bids:
        total += _side_snapshot_score(bids, best_bid, target_size, discount_factor, tick)
    if best_ask is not None and asks:
        total += _side_snapshot_score(asks, best_ask, target_size, discount_factor, tick)
    return total


class KalshiLipScorer:
    """Accumulate per-snapshot LIP scores for a single market over a period.

    Thread-safe. One scorer per (market, reward period). Feed it one snapshot
    per second via :meth:`record_snapshot`, then estimate accrual with
    :meth:`estimate_reward` once the period's pool and total market score are
    known (or with an assumed participation share).
    """

    def __init__(
        self,
        market_key: str,
        target_size: float,
        discount_factor: float,
        tick: float = KALSHI_TICK,
    ):
        """Initialise the scorer for one market/period.

        Args:
            market_key: Market identifier (Kalshi ticker).
            target_size: Per-period Target Size; clamped to program bounds.
            discount_factor: Per-period Discount Factor in [0, 1].
            tick: Price increment in dollars.
        """
        if discount_factor < 0.0 or discount_factor > 1.0:
            raise ValueError(f'discount_factor must be in [0, 1], got {discount_factor!r}')
        self.market_key = market_key
        self.target_size = max(MIN_TARGET_SIZE, min(MAX_TARGET_SIZE, target_size))
        self.discount_factor = discount_factor
        self.tick = tick
        self._lock = threading.Lock()
        self._accumulated_score = 0.0
        self._snapshot_count = 0

    def record_snapshot(
        self,
        orders: list[dict],
        best_bid: float | None,
        best_ask: float | None,
    ) -> float:
        """Score one snapshot and add it to the running total.

        Args:
            orders: Resting orders, each ``{"side", "price", "size"}``.
            best_bid: Current best bid price in dollars.
            best_ask: Current best ask price in dollars.

        Returns:
            The score contributed by this snapshot.
        """
        score = snapshot_score(
            orders, best_bid, best_ask, self.target_size, self.discount_factor, self.tick
        )
        with self._lock:
            self._accumulated_score += score
            self._snapshot_count += 1
        return score

    @property
    def accumulated_score(self) -> float:
        """Sum of all recorded snapshot scores this period."""
        with self._lock:
            return self._accumulated_score

    @property
    def snapshot_count(self) -> int:
        """Number of snapshots recorded this period."""
        with self._lock:
            return self._snapshot_count

    def estimate_reward(self, reward_pool: float, total_market_score: float) -> float:
        """Estimate this participant's reward for the period.

        Reward = ``(your_score / total_market_score) * reward_pool``.

        Args:
            reward_pool: Total reward pool for the period in dollars.
            total_market_score: Sum of all participants' scores (including ours).

        Returns:
            Estimated reward in dollars (0.0 if there is no scored liquidity).
        """
        if total_market_score <= 0 or reward_pool <= 0:
            return 0.0
        with self._lock:
            score = self._accumulated_score
        share = score / total_market_score
        return max(0.0, share * reward_pool)

    def estimate_reward_with_share(self, reward_pool: float, participation_share: float) -> float:
        """Estimate reward from an assumed participation share when total score is unknown.

        Args:
            reward_pool: Total reward pool for the period in dollars.
            participation_share: Assumed fraction of total market score we hold, in [0, 1].

        Returns:
            Estimated reward in dollars.
        """
        if reward_pool <= 0 or participation_share <= 0:
            return 0.0
        with self._lock:
            score = self._accumulated_score
        if score <= 0:
            return 0.0
        share = min(1.0, participation_share)
        return max(0.0, share * reward_pool)


# ---------------------------------------------------------------------------
# LIPScoreTracker (continuous real-time accrual & yield estimation)
# ---------------------------------------------------------------------------


def extract_book_levels(book: dict | None) -> tuple[list[tuple[float, float]], list[tuple[float, float]]]:
    """Extract (yes_bids, yes_asks) as [(price, size), ...] from a book dict.

    Accepts raw Kalshi API responses, parsed orderbooks, or mm_pilot book cache dicts.
    """
    if not book or not isinstance(book, dict):
        return [], []

    raw = book.get("raw")
    if isinstance(raw, dict):
        book = raw

    # Current schema: orderbook_fp with dollar strings
    fp = book.get("orderbook_fp")
    if isinstance(fp, dict):
        bids_raw = fp.get("yes_dollars") or []
        no_raw = fp.get("no_dollars") or []
        bids = _normalized_levels([(float(r[0]), float(r[1])) for r in bids_raw if len(r) >= 2])
        asks = _normalized_levels([(round(1.0 - float(r[0]), 4), float(r[1])) for r in no_raw if len(r) >= 2])
        return bids, asks

    # Legacy schema: orderbook with cent integers
    legacy = book.get("orderbook")
    if isinstance(legacy, dict):
        bids_raw = legacy.get("yes") or []
        no_raw = legacy.get("no") or []
        bids = _normalized_levels([(float(r[0]) / 100.0, float(r[1])) for r in bids_raw if len(r) >= 2])
        asks = _normalized_levels([(round(1.0 - float(r[0]) / 100.0, 4), float(r[1])) for r in no_raw if len(r) >= 2])
        return bids, asks

    # Top-of-book levels fallback
    bids: list[tuple[float, float]] = []
    asks: list[tuple[float, float]] = []
    if "yes_bid" in book and book["yes_bid"]:
        bids.append((float(book["yes_bid"][0]), float(book["yes_bid"][1])))
    if "yes_ask" in book and book["yes_ask"]:
        asks.append((float(book["yes_ask"][0]), float(book["yes_ask"][1])))
    return bids, asks


class LIPScoreTracker:
    """Real-time reward accrual and blended yield tracker for Kalshi LIP market making.

    Thread-safe. Tracks snapshot scores, qualifying share against book depth,
    time-weighted reward accruals, and blended APR (LIP incentive yield + trading PnL).
    """

    DEFAULT_TARGET_SIZE: float = 500.0
    DEFAULT_DISCOUNT_FACTOR: float = 0.95
    DEFAULT_POOL_DOLLARS: float = 100.0
    PERIOD_SECONDS: float = 7.0 * 86400.0  # standard weekly LIP pool duration (seconds)

    def __init__(self, time_fn=None):
        import time

        self._time_fn = time_fn or time.time
        self._lock = threading.RLock()
        self._programs: dict[str, dict] = {}
        self._stats: dict[str, dict] = {}

    def set_market_program(
        self,
        ticker: str,
        pool_dollars: float | None = None,
        discount_factor: float | None = None,
        target_size: float | None = None,
        program_end: str | None = None,
        category: str | None = None,
        discount_factor_bps: float | None = None,
    ) -> None:
        """Register or update LIP pool parameters for a market ticker."""
        if not ticker:
            return
        if discount_factor is None and discount_factor_bps is not None:
            try:
                discount_factor = float(discount_factor_bps) / 10000.0
            except (TypeError, ValueError):
                pass
        with self._lock:
            existing = self._programs.get(ticker, {})
            pool = (
                max(0.0, float(pool_dollars))
                if pool_dollars is not None
                else existing.get("pool_dollars", self.DEFAULT_POOL_DOLLARS)
            )
            df = (
                max(0.0, min(1.0, float(discount_factor)))
                if discount_factor is not None
                else existing.get("discount_factor", self.DEFAULT_DISCOUNT_FACTOR)
            )
            ts = (
                max(MIN_TARGET_SIZE, min(MAX_TARGET_SIZE, float(target_size)))
                if target_size is not None
                else existing.get("target_size", self.DEFAULT_TARGET_SIZE)
            )
            self._programs[ticker] = {
                "pool_dollars": pool,
                "discount_factor": df,
                "target_size": ts,
                "program_end": program_end or existing.get("program_end"),
                "category": category or existing.get("category"),
            }
            if ticker not in self._stats:
                self._stats[ticker] = self._empty_stat()

    def _empty_stat(self) -> dict:
        return {
            "accumulated_score": 0.0,
            "market_accumulated_score": 0.0,
            "uptime_seconds": 0.0,
            "snapshots_count": 0,
            "last_snapshot_time": None,
            "last_qualifying_share": 0.0,
            "last_our_score": 0.0,
            "last_market_score": 0.0,
            "accumulated_reward_usd": 0.0,
        }

    def record_snapshot(
        self,
        ticker: str,
        our_orders: list[dict],
        book: dict | None,
        now: float | None = None,
        is_dry_run: bool = True,
    ) -> dict:
        """Score one snapshot for a market and update running reward accrual."""
        if not ticker:
            return {}

        now_val = now if now is not None else self._time_fn()

        with self._lock:
            if ticker not in self._programs:
                self.set_market_program(ticker)
            prog = self._programs[ticker]
            if ticker not in self._stats:
                self._stats[ticker] = self._empty_stat()
            stat = self._stats[ticker]

            last_t = stat["last_snapshot_time"]
            if last_t is None:
                elapsed = 1.0
            else:
                elapsed = max(0.0, min(60.0, now_val - last_t))

            program_end = prog.get("program_end")
            if program_end:
                try:
                    if isinstance(program_end, (int, float)):
                        end_ts = float(program_end)
                    else:
                        end_ts = datetime.fromisoformat(
                            str(program_end).replace("Z", "+00:00")
                        ).timestamp()
                except (AttributeError, TypeError, ValueError, OverflowError):
                    end_ts = None
                if end_ts is not None:
                    credited_start = now_val - elapsed
                    elapsed = max(
                        0.0,
                        min(elapsed, end_ts - credited_start),
                    )

            stat["last_snapshot_time"] = now_val

            target_size = prog["target_size"]
            discount_factor = prog["discount_factor"]
            pool_dollars = prog["pool_dollars"]

            # Parse best bid & ask
            best_bid = None
            best_ask = None
            if book:
                if book.get("yes_bid"):
                    best_bid = float(book["yes_bid"][0])
                if book.get("yes_ask"):
                    best_ask = float(book["yes_ask"][0])
                mid = book.get("mid")
                if best_bid is None and mid is not None:
                    best_bid = max(0.01, round(mid - KALSHI_TICK, 2))
                if best_ask is None and mid is not None:
                    best_ask = min(0.99, round(mid + KALSHI_TICK, 2))

            # Normalize our resting orders
            parsed_orders: list[dict] = []
            for o in our_orders:
                try:
                    price = float(o.get("price", 0.0))
                    size = float(o.get("count") or o.get("size", 0.0))
                    side = str(o.get("side", "")).lower()
                    action = str(o.get("action", "")).lower()
                    purpose = str(o.get("purpose", "")).lower()
                except (TypeError, ValueError):
                    continue

                if purpose == "quote_bid" or (side == "yes" and action == "buy"):
                    mapped_side = "bid"
                    mapped_price = price
                elif purpose == "quote_ask" or (side == "no" and action == "buy"):
                    mapped_side = "ask"
                    mapped_price = round(1.0 - price, 2)
                elif side == "yes" and action == "sell":
                    mapped_side = "ask"
                    mapped_price = price
                else:
                    continue

                if size > 0 and 0 < mapped_price < 1:
                    parsed_orders.append({"side": mapped_side, "price": mapped_price, "size": size})

            if best_bid is None and parsed_orders:
                bids_only = [o["price"] for o in parsed_orders if o["side"] == "bid"]
                if bids_only:
                    best_bid = max(bids_only)
            if best_ask is None and parsed_orders:
                asks_only = [o["price"] for o in parsed_orders if o["side"] == "ask"]
                if asks_only:
                    best_ask = min(asks_only)

            our_score = snapshot_score(
                parsed_orders, best_bid, best_ask, target_size, discount_factor
            )

            # Compute market depth score
            market_bids, market_asks = extract_book_levels(book)
            market_bid_score = (
                _side_snapshot_score(
                    [{"price": p, "size": s} for p, s in market_bids],
                    best_bid,
                    target_size,
                    discount_factor,
                )
                if best_bid is not None
                else 0.0
            )
            market_ask_score = (
                _side_snapshot_score(
                    [{"price": p, "size": s} for p, s in market_asks],
                    best_ask,
                    target_size,
                    discount_factor,
                )
                if best_ask is not None
                else 0.0
            )
            competitor_score = market_bid_score + market_ask_score

            if is_dry_run:
                total_market_score = competitor_score + our_score
            else:
                total_market_score = max(our_score, competitor_score)

            if total_market_score > 0:
                qualifying_share = min(1.0, our_score / total_market_score)
            else:
                qualifying_share = 1.0 if our_score > 0 else 0.0

            pool_rate_per_sec = pool_dollars / self.PERIOD_SECONDS if self.PERIOD_SECONDS > 0 else 0.0
            reward_delta = qualifying_share * pool_rate_per_sec * elapsed

            stat["accumulated_score"] += our_score
            stat["market_accumulated_score"] += total_market_score
            stat["uptime_seconds"] += elapsed
            stat["snapshots_count"] += 1
            stat["last_qualifying_share"] = qualifying_share
            stat["last_our_score"] = our_score
            stat["last_market_score"] = total_market_score
            stat["accumulated_reward_usd"] += reward_delta

            return {
                "ticker": ticker,
                "our_score": round(our_score, 4),
                "total_market_score": round(total_market_score, 4),
                "qualifying_share": round(qualifying_share, 4),
                "reward_delta": round(reward_delta, 6),
                "accumulated_reward_usd": round(stat["accumulated_reward_usd"], 4),
            }

    def get_metrics(
        self,
        capital_deployed_by_ticker: dict[str, float] | None = None,
        realized_pnl_by_ticker: dict[str, float] | None = None,
    ) -> dict:
        """Compile aggregated and per-ticker LIP yield metrics."""
        capital_map = capital_deployed_by_ticker or {}
        pnl_map = realized_pnl_by_ticker or {}

        with self._lock:
            by_ticker = {}
            total_reward_usd = 0.0
            total_daily_usd = 0.0
            total_weekly_usd = 0.0
            total_capital = 0.0
            total_annualized_lip = 0.0
            total_annualized_pnl = 0.0

            for ticker, stat in self._stats.items():
                prog = self._programs.get(ticker, {})
                pool = prog.get("pool_dollars", self.DEFAULT_POOL_DOLLARS)
                share = stat.get("last_qualifying_share", 0.0)
                reward_acc = stat.get("accumulated_reward_usd", 0.0)
                uptime = stat.get("uptime_seconds", 0.0)

                # Run rates (7 days pool basis = 168 hours)
                hourly_rate = share * (pool / 168.0) if pool > 0 else 0.0
                daily_rate = hourly_rate * 24.0
                weekly_rate = hourly_rate * 168.0

                capital = max(10.0, float(capital_map.get(ticker, 50.0)))
                annualized_lip = daily_rate * 365.0
                lip_apr = (annualized_lip / capital) * 100.0

                pnl = float(pnl_map.get(ticker, 0.0))
                if uptime >= 60.0:
                    annualized_pnl = (pnl / (uptime / 86400.0)) * 365.0
                    spread_apr = (annualized_pnl / capital) * 100.0
                else:
                    annualized_pnl = 0.0
                    spread_apr = 0.0

                blended_apr = lip_apr + spread_apr

                total_reward_usd += reward_acc
                total_daily_usd += daily_rate
                total_weekly_usd += weekly_rate
                total_capital += capital
                total_annualized_lip += annualized_lip
                total_annualized_pnl += annualized_pnl

                by_ticker[ticker] = {
                    "pool_dollars": round(pool, 2),
                    "qualifying_share_pct": round(share * 100.0, 2),
                    "accumulated_reward_usd": round(reward_acc, 4),
                    "daily_rate_usd": round(daily_rate, 2),
                    "weekly_rate_usd": round(weekly_rate, 2),
                    "capital_usd": round(capital, 2),
                    "lip_apr_pct": round(lip_apr, 2),
                    "spread_pnl_usd": round(pnl, 2),
                    "blended_apr_pct": round(blended_apr, 2),
                    "uptime_seconds": round(uptime, 1),
                    "snapshots_count": stat.get("snapshots_count", 0),
                }

            blended_apr_overall = (
                round(((total_annualized_lip + total_annualized_pnl) / total_capital) * 100.0, 2)
                if total_capital > 0
                else 0.0
            )

            return {
                "total_estimated_reward_usd": round(total_reward_usd, 4),
                "estimated_daily_rate_usd": round(total_daily_usd, 2),
                "estimated_weekly_rate_usd": round(total_weekly_usd, 2),
                "total_capital_deployed_usd": round(total_capital, 2),
                "blended_apr_pct": blended_apr_overall,
                "by_ticker": by_ticker,
            }

    def to_dict(self) -> dict:
        """Export state for JSON serialization."""
        with self._lock:
            return {
                "programs": dict(self._programs),
                "stats": {k: dict(v) for k, v in self._stats.items()},
            }

    def from_dict(self, data: dict) -> None:
        """Restore state from persisted JSON."""
        if not data or not isinstance(data, dict):
            return
        with self._lock:
            programs = data.get("programs") or {}
            stats = data.get("stats") or {}

            def as_float(value, default):
                try:
                    val = float(value)
                    return val if math.isfinite(val) else default
                except (TypeError, ValueError):
                    return default

            def as_int(value, default):
                try:
                    val = int(float(value))
                    return val
                except (TypeError, ValueError):
                    return default

            if isinstance(programs, dict):
                for ticker, p in programs.items():
                    if isinstance(p, dict):
                        merged = {
                            "pool_dollars": self.DEFAULT_POOL_DOLLARS,
                            "discount_factor": self.DEFAULT_DISCOUNT_FACTOR,
                            "target_size": self.DEFAULT_TARGET_SIZE,
                            "program_end": None,
                            "category": None,
                        }
                        merged.update(p)
                        merged["pool_dollars"] = max(
                            0.0, as_float(merged["pool_dollars"], self.DEFAULT_POOL_DOLLARS)
                        )
                        merged["discount_factor"] = max(
                            0.0,
                            min(1.0, as_float(merged["discount_factor"], self.DEFAULT_DISCOUNT_FACTOR)),
                        )
                        merged["target_size"] = max(
                            MIN_TARGET_SIZE,
                            min(MAX_TARGET_SIZE, as_float(merged["target_size"], self.DEFAULT_TARGET_SIZE)),
                        )
                        if merged.get("program_end") is not None and not isinstance(
                            merged["program_end"], (str, int, float)
                        ):
                            merged["program_end"] = None
                        if merged.get("category") is not None and not isinstance(
                            merged["category"], str
                        ):
                            merged["category"] = str(merged["category"])
                        self._programs[str(ticker)] = merged

            if isinstance(stats, dict):
                for ticker, s in stats.items():
                    if isinstance(s, dict):
                        merged = self._empty_stat()
                        merged.update(s)
                        for key in (
                            "accumulated_score",
                            "market_accumulated_score",
                            "uptime_seconds",
                            "last_qualifying_share",
                            "last_our_score",
                            "last_market_score",
                            "accumulated_reward_usd",
                        ):
                            merged[key] = as_float(merged[key], self._empty_stat()[key])
                        merged["snapshots_count"] = max(0, as_int(merged["snapshots_count"], 0))
                        if merged["last_snapshot_time"] is not None:
                            merged["last_snapshot_time"] = as_float(
                                merged["last_snapshot_time"], None
                            )
                        self._stats[str(ticker)] = merged
