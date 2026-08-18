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
