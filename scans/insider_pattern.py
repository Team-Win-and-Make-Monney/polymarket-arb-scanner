"""Insider Pattern Detection — Strategy #42.

Detect unusual order flow patterns that may indicate informed trading.

When scheduled events approach (earnings, elections, court rulings):
1. Unusual volume spikes may indicate insider knowledge
2. One-sided order flow (all buys or all sells) is suspicious
3. Large orders at specific price levels may signal price targets

Strategy:
1. Track order flow statistics per market
2. Detect anomalies relative to historical baseline
3. Follow the flow direction when confidence is high

Layer 4: Informed trading — follow informed money.

Risk: Flow may be noise, manipulation, or already priced in.
"""

import logging
import time
from collections import defaultdict

from config import (
    INSIDER_PATTERN_ENABLED,
    INSIDER_PATTERN_VOLUME_THRESHOLD,
    INSIDER_PATTERN_IMBALANCE_THRESHOLD,
)
from .helpers import capital_efficiency_score, filter_dust

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Order flow tracker
# ---------------------------------------------------------------------------

class OrderFlowTracker:
    """Track order flow statistics for anomaly detection."""

    def __init__(
        self,
        lookback_seconds: float = 3600.0,
        baseline_days: int = 7,
    ):
        """Initialize the tracker.

        Args:
            lookback_seconds: Recent window for anomaly detection.
            baseline_days: Historical baseline period.
        """
        self.lookback_seconds = lookback_seconds
        self.baseline_days = baseline_days
        self._trades: dict[str, list[dict]] = defaultdict(list)
        self._baseline_stats: dict[str, dict] = {}

    def record_trade(
        self,
        market_key: str,
        side: str,
        price: float,
        size: float,
        timestamp: float | None = None,
    ) -> None:
        """Record a trade for flow analysis.

        Args:
            market_key: Market identifier.
            side: "buy" or "sell".
            price: Trade price.
            size: Trade size in dollars.
            timestamp: Trade timestamp (default: now).
        """
        now = timestamp or time.time()
        self._trades[market_key].append({
            "timestamp": now,
            "side": side,
            "price": price,
            "size": size,
        })

        cutoff = now - (self.baseline_days * 86400)
        self._trades[market_key] = [
            t for t in self._trades[market_key]
            if t["timestamp"] > cutoff
        ]

    def get_recent_flow(
        self,
        market_key: str,
        lookback_seconds: float | None = None,
    ) -> dict:
        """Get order flow statistics for recent period.

        Args:
            market_key: Market identifier.
            lookback_seconds: Lookback window (default: self.lookback_seconds).

        Returns:
            Dict with buy_volume, sell_volume, imbalance, num_trades.
        """
        lookback = lookback_seconds or self.lookback_seconds
        cutoff = time.time() - lookback

        trades = [
            t for t in self._trades.get(market_key, [])
            if t["timestamp"] > cutoff
        ]

        buy_volume = sum(t["size"] for t in trades if t["side"] == "buy")
        sell_volume = sum(t["size"] for t in trades if t["side"] == "sell")
        total_volume = buy_volume + sell_volume

        imbalance = 0.0
        if total_volume > 0:
            imbalance = (buy_volume - sell_volume) / total_volume

        return {
            "buy_volume": buy_volume,
            "sell_volume": sell_volume,
            "total_volume": total_volume,
            "imbalance": imbalance,
            "num_trades": len(trades),
        }

    def get_baseline_flow(self, market_key: str) -> dict:
        """Get baseline order flow statistics.

        Returns average hourly volume and typical imbalance range.

        Args:
            market_key: Market identifier.

        Returns:
            Dict with avg_hourly_volume, typical_imbalance_range.
        """
        all_trades = self._trades.get(market_key, [])
        if len(all_trades) < 20:
            return {
                "avg_hourly_volume": 0.0,
                "typical_imbalance_range": 0.5,
            }

        timestamps = [t["timestamp"] for t in all_trades]
        time_span_hours = (max(timestamps) - min(timestamps)) / 3600
        if time_span_hours <= 0:
            time_span_hours = 1

        total_volume = sum(t["size"] for t in all_trades)
        avg_hourly = total_volume / time_span_hours

        hourly_imbalances = []
        ref_time = max(timestamps)
        for hour_offset in range(int(time_span_hours)):
            start = ref_time - ((hour_offset + 1) * 3600)
            end = ref_time - (hour_offset * 3600)
            hour_trades = [
                t for t in all_trades
                if start <= t["timestamp"] <= end
                if hour_offset == 0 or t["timestamp"] < end
            ]
            if len(hour_trades) >= 3:
                buy = sum(t["size"] for t in hour_trades if t["side"] == "buy")
                sell = sum(t["size"] for t in hour_trades if t["side"] == "sell")
                total = buy + sell
                if total > 0:
                    hourly_imbalances.append(abs(buy - sell) / total)

        typical_range = 0.5
        if hourly_imbalances:
            hourly_imbalances.sort()
            typical_range = hourly_imbalances[int(len(hourly_imbalances) * 0.75)]

        return {
            "avg_hourly_volume": avg_hourly,
            "typical_imbalance_range": typical_range,
        }

    def detect_anomaly(
        self,
        market_key: str,
        volume_threshold: float | None = None,
        imbalance_threshold: float | None = None,
    ) -> dict | None:
        """Detect order flow anomaly.

        Args:
            market_key: Market identifier.
            volume_threshold: Volume multiple to flag (default from config).
            imbalance_threshold: Imbalance threshold (default from config).

        Returns:
            Anomaly dict with direction, confidence, or None.
        """
        if volume_threshold is None:
            volume_threshold = INSIDER_PATTERN_VOLUME_THRESHOLD
        if imbalance_threshold is None:
            imbalance_threshold = INSIDER_PATTERN_IMBALANCE_THRESHOLD

        recent = self.get_recent_flow(market_key)
        baseline = self.get_baseline_flow(market_key)

        if recent["num_trades"] < 5:
            return None

        volume_spike = False
        if baseline["avg_hourly_volume"] > 0:
            lookback_hours = self.lookback_seconds / 3600
            expected_volume = baseline["avg_hourly_volume"] * lookback_hours
            if recent["total_volume"] > expected_volume * volume_threshold:
                volume_spike = True

        imbalance_anomaly = abs(recent["imbalance"]) > imbalance_threshold

        if not (volume_spike or imbalance_anomaly):
            return None

        direction = "BUY_YES" if recent["imbalance"] > 0 else "BUY_NO"

        confidence = 0.50
        if volume_spike:
            confidence += 0.15
        if imbalance_anomaly:
            confidence += abs(recent["imbalance"]) * 0.20

        return {
            "direction": direction,
            "imbalance": recent["imbalance"],
            "volume_spike": volume_spike,
            "recent_volume": recent["total_volume"],
            "baseline_hourly": baseline["avg_hourly_volume"],
            "confidence": min(confidence, 0.80),
        }


_order_flow_tracker: OrderFlowTracker | None = None


def get_order_flow_tracker() -> OrderFlowTracker:
    """Get or create the module-level OrderFlowTracker."""
    global _order_flow_tracker
    if _order_flow_tracker is None:
        _order_flow_tracker = OrderFlowTracker()
    return _order_flow_tracker


# ---------------------------------------------------------------------------
# Scan function
# ---------------------------------------------------------------------------

def scan_insider_pattern(
    markets: list[dict],
    platform: str = "polymarket",
    order_flow_tracker: OrderFlowTracker | None = None,
    min_profit: float = 0.005,
) -> list[dict]:
    """Scan for insider trading pattern opportunities.

    Identifies markets with unusual order flow that may indicate
    informed trading activity.

    Args:
        markets: List of market dicts.
        platform: Platform name for the markets.
        order_flow_tracker: OrderFlowTracker instance.
        min_profit: Minimum net profit threshold.

    Returns:
        List of opportunity dicts sorted by confidence descending.
    """
    if not INSIDER_PATTERN_ENABLED:
        return []

    tracker = order_flow_tracker or get_order_flow_tracker()
    opportunities = []

    for market in markets:
        title = market.get("title") or market.get("question", "")
        market_price = market.get("yes_price") or market.get("yes_mid", 0)
        market_key = market.get("condition_id") or market.get("id", "")

        if not title or not market_price or market_price <= 0 or market_price >= 1:
            continue

        anomaly = tracker.detect_anomaly(market_key)
        if anomaly is None:
            continue

        direction = anomaly["direction"]
        if direction == "BUY_YES":
            entry_price = market_price
            expected_edge = min(0.10, abs(anomaly["imbalance"]) * 0.15)
        else:
            entry_price = 1.0 - market_price
            expected_edge = min(0.10, abs(anomaly["imbalance"]) * 0.15)

        from fees import net_profit_insider_pattern
        result = net_profit_insider_pattern(
            market_price=entry_price,
            expected_edge=expected_edge,
            platform=platform,
        )

        if result["net_profit"] < min_profit:
            continue

        opp = {
            "type": "InsiderPattern",
            "_layer": 4,
            "market": f"{title[:40]}... (flow anomaly)",
            "prices": f"imbalance={anomaly['imbalance']:+.2f} volume_spike={anomaly['volume_spike']}",
            "total_cost": f"${entry_price:.4f}",
            "net_profit": result["net_profit"],
            "net_roi": result.get("net_roi", 0),
            "confidence": anomaly["confidence"],
            "_market_key": market_key,
            "_platform": platform,
            "_market": market,
            "_market_price": market_price,
            "_imbalance": anomaly["imbalance"],
            "_volume_spike": anomaly["volume_spike"],
            "_recent_volume": anomaly["recent_volume"],
            "_direction": direction,
        }
        opp["_efficiency"] = capital_efficiency_score(opp)
        opportunities.append(opp)

    opportunities = filter_dust(opportunities, min_amount=min_profit)
    opportunities.sort(key=lambda o: o["confidence"], reverse=True)

    logger.info("Insider pattern scan: found %d opportunities", len(opportunities))
    return opportunities
