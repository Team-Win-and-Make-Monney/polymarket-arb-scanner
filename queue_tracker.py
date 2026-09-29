"""Queue Position Estimation & Microstructure Fill Probability Tracker.

Tracks resting limit orders in FIFO orderbook queues, estimating queue position
and microstructure fill probabilities based on orderbook dynamics, trade velocity,
and depth changes. Enables intelligent quote preservation to prevent time-priority
forfeiture during MM refresh cycles.
"""

from __future__ import annotations

import logging
import math
import threading
from dataclasses import dataclass
from typing import Any

from kalshi_api import parse_orderbook

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data Models
# ---------------------------------------------------------------------------

@dataclass
class QueuePosition:
    """Estimated queue position and fill probability for a resting order."""

    order_id: str
    ticker: str
    side: str            # "yes" | "no"
    action: str          # "buy" | "sell"
    count: int           # contracts
    price: float         # price in dollars
    purpose: str         # "quote_bid" | "quote_ask"
    placed_at: float
    initial_depth_ahead: float = 0.0
    estimated_queue_ahead: float = 0.0
    last_book_depth: float = 0.0
    fill_probability: float = 0.0
    last_updated_at: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        """Convert queue position to a serializable dictionary."""
        return {
            "order_id": self.order_id,
            "ticker": self.ticker,
            "side": self.side,
            "action": self.action,
            "count": self.count,
            "price": round(self.price, 4),
            "purpose": self.purpose,
            "placed_at": round(self.placed_at, 2),
            "initial_depth_ahead": round(self.initial_depth_ahead, 2),
            "estimated_queue_ahead": round(self.estimated_queue_ahead, 2),
            "last_book_depth": round(self.last_book_depth, 2),
            "fill_probability": round(self.fill_probability, 4),
            "last_updated_at": round(self.last_updated_at, 2),
        }


# ---------------------------------------------------------------------------
# Queue Position Tracker
# ---------------------------------------------------------------------------

class QueuePositionTracker:
    """Thread-safe tracker of FIFO orderbook queue positions and fill probabilities."""

    def __init__(
        self,
        enabled: bool = True,
        preservation_enabled: bool = True,
        resize_tolerance: float = 0.20,
        max_queue_ahead: int = 100,
        min_fill_probability: float = 0.05,
        horizon_sec: float = 30.0,
        time_fn: Any = None,
    ) -> None:
        self.enabled = enabled
        self.preservation_enabled = preservation_enabled
        self.resize_tolerance = resize_tolerance
        self.max_queue_ahead = max_queue_ahead
        self.min_fill_probability = min_fill_probability
        self.horizon_sec = horizon_sec
        self._time_fn = time_fn or (lambda: 0.0)

        self._orders: dict[str, QueuePosition] = {}
        self._recent_trades: dict[str, list[tuple[float, float, float]]] = {}  # ticker -> [(ts, price, count)]
        self._last_book_snapshots: dict[str, dict] = {}  # ticker -> parsed orderbook
        self._preserved_count = 0
        self._replaced_count = 0
        self._lock = threading.Lock()

    def _normalize_book(self, book: dict | None) -> dict | None:
        """Ensure book is a parsed dict with 'yes_bids' and 'no_bids'."""
        if not book or not isinstance(book, dict):
            return None
        if "raw" in book and isinstance(book["raw"], dict):
            book = book["raw"]
        if "yes_bids" in book and "no_bids" in book:
            return book
        return parse_orderbook(book)

    def _get_level_depth(self, parsed_book: dict, side: str, price: float) -> float:
        """Find resting depth at a specific price level on the given side."""
        bids_key = "yes_bids" if side.lower() in ("yes", "bid") else "no_bids"
        levels = parsed_book.get(bids_key) or []
        for level_price, qty in levels:
            if abs(float(level_price) - price) < 1e-4:
                return float(qty)
        return 0.0

    def record_placement(
        self,
        order_id: str,
        ticker: str,
        side: str,
        action: str,
        count: int,
        price: float,
        purpose: str,
        book: dict | None = None,
        placed_at: float | None = None,
    ) -> QueuePosition:
        """Record entry of a new resting quote order into the queue tracker."""
        now = self._time_fn() if placed_at is None else placed_at
        parsed_book = self._normalize_book(book)
        if parsed_book is None:
            with self._lock:
                parsed_book = self._last_book_snapshots.get(ticker)

        initial_depth = 0.0
        if parsed_book:
            initial_depth = self._get_level_depth(parsed_book, side, price)

        # In FIFO queue, resting contracts already at this price level sit ahead of us
        initial_ahead = max(0.0, initial_depth)

        q_pos = QueuePosition(
            order_id=order_id,
            ticker=ticker,
            side=side,
            action=action,
            count=count,
            price=price,
            purpose=purpose,
            placed_at=now,
            initial_depth_ahead=initial_ahead,
            estimated_queue_ahead=initial_ahead,
            last_book_depth=initial_depth,
            fill_probability=0.0,
            last_updated_at=now,
        )

        prob = self.estimate_fill_probability(q_pos, book=parsed_book)
        q_pos.fill_probability = prob

        with self._lock:
            self._orders[order_id] = q_pos
            if parsed_book:
                self._last_book_snapshots[ticker] = parsed_book

        return q_pos

    def update_book(self, ticker: str, raw_or_parsed_book: dict | None) -> None:
        """Update queue positions for resting orders based on fresh orderbook levels."""
        parsed = self._normalize_book(raw_or_parsed_book)
        if not parsed:
            return

        now = self._time_fn()
        with self._lock:
            self._last_book_snapshots[ticker] = parsed
            matching_orders = [o for o in self._orders.values() if o.ticker == ticker]

            for order in matching_orders:
                curr_depth = self._get_level_depth(parsed, order.side, order.price)
                delta = curr_depth - order.last_book_depth

                if delta < 0:
                    # Depth at this price decreased: orders ahead were cancelled or filled
                    order.estimated_queue_ahead = max(0.0, order.estimated_queue_ahead + delta)
                elif delta > 0:
                    # Depth increased: new orders arrived BEHIND us in FIFO queue, queue_ahead does not increase
                    pass

                order.last_book_depth = curr_depth
                order.last_updated_at = now
                order.fill_probability = self._estimate_fill_prob_locked(order, book=parsed)

    def record_fill(self, order_id: str, count: int) -> None:
        """Record an execution fill on an order, resetting queue ahead to zero."""
        now = self._time_fn()
        with self._lock:
            order = self._orders.get(order_id)
            if not order:
                return

            # When our order gets filled, all prior orders ahead of us in FIFO are gone
            order.estimated_queue_ahead = 0.0
            order.count = max(0, order.count - count)
            order.last_updated_at = now

            if order.count <= 0:
                self._orders.pop(order_id, None)
            else:
                book = self._last_book_snapshots.get(order.ticker)
                order.fill_probability = self._estimate_fill_prob_locked(order, book=book)

    def record_cancel(self, order_id: str) -> None:
        """Remove a cancelled order from tracking."""
        with self._lock:
            self._orders.pop(order_id, None)

    def record_trade(
        self,
        ticker: str,
        price: float,
        count: int,
        timestamp: float | None = None,
    ) -> None:
        """Record a market trade print and deplete queue ahead for orders at that price."""
        now = self._time_fn() if timestamp is None else timestamp
        with self._lock:
            trades = self._recent_trades.setdefault(ticker, [])
            trades.append((now, price, float(count)))
            # Keep last 300 seconds of trades
            self._recent_trades[ticker] = [t for t in trades if now - t[0] <= 300.0]

            for order in self._orders.values():
                if order.ticker == ticker and abs(order.price - price) < 1e-4:
                    order.estimated_queue_ahead = max(0.0, order.estimated_queue_ahead - count)
                    book = self._last_book_snapshots.get(ticker)
                    order.fill_probability = self._estimate_fill_prob_locked(order, book=book)

    def get_trade_velocity(self, ticker: str, window_sec: float = 60.0) -> float:
        """Calculate recent execution trade velocity in contracts per second."""
        now = self._time_fn()
        with self._lock:
            trades = self._recent_trades.get(ticker, [])
            recent = [t[2] for t in trades if now - t[0] <= window_sec]
            if not recent:
                return 0.0
            total_contracts = sum(recent)
            return total_contracts / max(1.0, window_sec)

    def _estimate_fill_prob_locked(
        self,
        order: QueuePosition,
        book: dict | None = None,
        trade_velocity: float | None = None,
        horizon_sec: float | None = None,
    ) -> float:
        """Internal fill probability calculation holding self._lock."""
        horizon = horizon_sec or self.horizon_sec
        q_ahead = max(0.0, order.estimated_queue_ahead)
        q_eff = q_ahead + (order.count / 2.0)

        # Determine trade velocity
        if trade_velocity is not None:
            v = max(0.0, trade_velocity)
        else:
            trades = self._recent_trades.get(order.ticker, [])
            now = self._time_fn()
            recent = [t[2] for t in trades if now - t[0] <= 60.0]
            v = (sum(recent) / 60.0) if recent else 0.0

        # Baseline velocity fallback (e.g. 0.2 contracts/sec) when no trade tape is available
        eff_velocity = max(0.20, v)
        tau = q_eff / eff_velocity  # expected seconds to reach and clear our order

        # Orderbook imbalance multiplier
        imbalance_mult = 1.0
        ticks_from_touch = 0.0

        if book:
            parsed = self._normalize_book(book)
            if parsed:
                yes_bids = parsed.get("yes_bids") or []
                no_bids = parsed.get("no_bids") or []

                best_yes_price, best_yes_qty = yes_bids[-1] if yes_bids else (0.50, 10.0)
                best_no_price, best_no_qty = no_bids[-1] if no_bids else (0.50, 10.0)

                is_yes_bid = order.side.lower() in ("yes", "bid")
                same_qty = best_yes_qty if is_yes_bid else best_no_qty
                opp_qty = best_no_qty if is_yes_bid else best_yes_qty

                tot = same_qty + opp_qty
                if tot > 0:
                    rho = opp_qty / tot
                    imbalance_mult = max(0.2, min(2.0, 2.0 * rho))

                best_price = best_yes_price if is_yes_bid else best_no_price
                ticks_from_touch = max(0.0, round(abs(best_price - order.price) / 0.01))

        # Hazard rate lambda
        distance_decay = math.exp(-0.5 * ticks_from_touch)
        hazard_rate = (1.0 / max(0.01, tau)) * imbalance_mult * distance_decay

        # Poisson fill probability over horizon T: 1 - exp(-lambda * T)
        p_fill = 1.0 - math.exp(-hazard_rate * horizon)
        return max(0.0, min(1.0, p_fill))

    def estimate_fill_probability(
        self,
        order_or_id: str | QueuePosition,
        book: dict | None = None,
        trade_velocity: float | None = None,
        horizon_sec: float | None = None,
    ) -> float:
        """Estimate the probability of order fill over horizon_sec."""
        with self._lock:
            if isinstance(order_or_id, QueuePosition):
                order = order_or_id
            else:
                order = self._orders.get(order_or_id)
                if not order:
                    return 0.0
            return self._estimate_fill_prob_locked(
                order, book=book, trade_velocity=trade_velocity, horizon_sec=horizon_sec
            )

    def should_preserve_quote(
        self,
        order_id: str,
        target_price: float,
        target_count: int,
        book: dict | None = None,
        order_dict: dict | None = None,
    ) -> tuple[bool, str, dict[str, Any]]:
        """Evaluate whether a resting quote should be preserved to retain FIFO queue priority."""
        if not self.enabled:
            return False, "tracker_disabled", {}
        if not self.preservation_enabled:
            return False, "preservation_disabled", {}

        with self._lock:
            order = self._orders.get(order_id)
            if order is None and order_dict is not None:
                # Auto-register order if missing from tracking
                order = self.record_placement(
                    order_id=order_id,
                    ticker=order_dict.get("ticker", ""),
                    side=order_dict.get("side", "yes"),
                    action=order_dict.get("action", "buy"),
                    count=order_dict.get("count", target_count),
                    price=order_dict.get("price", target_price),
                    purpose=order_dict.get("purpose", ""),
                    book=book,
                )

            if order is None:
                return False, "order_not_tracked", {}

            if target_count <= 0:
                self._replaced_count += 1
                return False, "target_count_zero", {}

            # Price check: limit order price must match target quote exactly
            if abs(order.price - target_price) >= 1e-4:
                self._replaced_count += 1
                return False, f"price_changed (cur={order.price:.2f} tgt={target_price:.2f})", {}

            # Size check: count difference must be within resize tolerance
            diff = abs(order.count - target_count)
            tolerance = max(1, int(order.count * self.resize_tolerance))
            if diff > tolerance:
                self._replaced_count += 1
                return (
                    False,
                    f"size_diff_exceeded (cur={order.count} tgt={target_count} tol={tolerance})",
                    {},
                )

            # Queue position check: order must not be buried behind an excessive wall
            if order.estimated_queue_ahead > self.max_queue_ahead:
                self._replaced_count += 1
                return (
                    False,
                    f"buried_in_queue (ahead={order.estimated_queue_ahead:.1f} > max={self.max_queue_ahead})",
                    {},
                )

            # Fill probability check: fill probability must meet floor
            fill_prob = self._estimate_fill_prob_locked(order, book=book)
            if fill_prob < self.min_fill_probability:
                self._replaced_count += 1
                return (
                    False,
                    f"low_fill_probability ({fill_prob:.3f} < min={self.min_fill_probability:.3f})",
                    {},
                )

            self._preserved_count += 1
            meta = {
                "order_id": order_id,
                "ticker": order.ticker,
                "queue_ahead": order.estimated_queue_ahead,
                "fill_probability": fill_prob,
                "count": order.count,
                "price": order.price,
            }
            return True, "preserve_priority", meta

    def get_queue_position(self, order_id: str) -> dict[str, Any] | None:
        """Get queue position details for an order."""
        with self._lock:
            order = self._orders.get(order_id)
            return order.to_dict() if order else None

    def get_status(self) -> dict[str, Any]:
        """Get summary status of queue tracker."""
        with self._lock:
            active_count = len(self._orders)
            if active_count > 0:
                avg_q = sum(o.estimated_queue_ahead for o in self._orders.values()) / active_count
                avg_p = sum(o.fill_probability for o in self._orders.values()) / active_count
            else:
                avg_q = 0.0
                avg_p = 0.0

            orders_summary = [o.to_dict() for o in self._orders.values()]
            return {
                "enabled": self.enabled,
                "preservation_enabled": self.preservation_enabled,
                "active_orders_count": active_count,
                "avg_queue_ahead": round(avg_q, 2),
                "avg_fill_probability": round(avg_p, 4),
                "preserved_count": self._preserved_count,
                "replaced_count": self._replaced_count,
                "orders": orders_summary,
            }

    def to_dict(self) -> dict[str, Any]:
        """Alias for get_status() for state serialization."""
        return self.get_status()
