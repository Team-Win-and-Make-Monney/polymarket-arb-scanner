"""Resolution sniping — buy near-certain outcomes at a discount before settlement."""

import logging
import time

from config import RESOLUTION_SNIPE_WINDOW_HOURS
from .helpers import capital_efficiency_score

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Resolution sniping detection
# ---------------------------------------------------------------------------

def scan_resolution_snipes(
    markets: list[dict],
    platform: str = "polymarket",
    min_probability: float = 0.95,
    max_price: float = 0.97,
    min_profit: float = 0.005,
    signal_sources: list[dict] | None = None,
) -> list[dict]:
    """Detect resolution sniping opportunities.

    Finds markets where an outcome is near-certain (>95% probability from
    signals) but the market price hasn't fully converged to $1.00.

    Args:
        markets: List of market dicts from a platform API.
        platform: Platform name (for tagging opportunities).
        min_probability: Minimum consensus probability to consider near-certain.
        max_price: Maximum market price — opportunity exists when price < this
            despite high consensus (buy at discount, settle at $1.00).
        min_profit: Minimum net profit threshold.
        signal_sources: Optional list of signal dicts with ``market_key``,
            ``probability``, ``source`` fields for external confirmation.

    Returns:
        List of opportunity dicts sorted by net profit.
    """
    opportunities = []
    signal_map = _build_signal_map(signal_sources) if signal_sources else {}

    for market in markets:
        title = market.get("question") or market.get("title", "")
        market_key = market.get("condition_id") or market.get("id", "")

        # Check if market is approaching resolution
        if not _is_near_resolution(market):
            continue

        # Get the best price for the leading outcome
        prices = _extract_outcome_prices(market, platform)
        if not prices:
            continue

        for outcome_name, price in prices.items():
            if price >= max_price or price <= 0:
                continue

            # Check consensus probability from signals
            consensus = _get_consensus(market_key, outcome_name, signal_map)
            if consensus is not None and consensus < min_probability:
                continue

            # If no external signals, use the price itself as a proxy
            # (only if price > min_probability — the market itself thinks it's certain)
            if consensus is None and price < min_probability:
                continue

            # Profit: settle at $1.00 minus buy price minus estimated fees
            gross_profit = 1.0 - price
            # Estimate 2% fee (Polymarket winner fee model)
            estimated_fee = 0.02 * gross_profit
            net_profit = gross_profit - estimated_fee

            if net_profit < min_profit:
                continue

            net_roi = net_profit / price if price > 0 else 0

            opportunity = {
                "type": "ResolutionSnipeOpp",
                "_layer": 2,  # Layer 2: near-arbitrage
                "market": title[:60] if title else market_key,
                "prices": f"{platform}_{outcome_name}={price:.4f} settle=1.00",
                "total_cost": f"${price:.4f}",
                "net_profit": net_profit,
                "net_roi": net_roi,
                "confidence": consensus or price,
                "_platform": platform,
                "_outcome": outcome_name,
                "_price": price,
                "_consensus": consensus,
                "_market_key": market_key,
                "_direction": "BUY_YES" if outcome_name == "yes" else "BUY_NO",
            }
            opportunity["_efficiency"] = capital_efficiency_score(opportunity)
            opportunities.append(opportunity)

    opportunities.sort(key=lambda o: o["net_profit"], reverse=True)
    logger.info("Resolution snipe scan: found %d opportunities", len(opportunities))
    return opportunities


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _is_near_resolution(market: dict) -> bool:
    """Check if a market is approaching resolution.

    Looks for: close_time within ``RESOLUTION_SNIPE_WINDOW_HOURS`` (default
    48h, env-configurable), status fields indicating resolution, or
    high-volume recent trading indicating resolution imminent.
    """
    # Polymarket: check end_date_iso or close_time
    close_time = market.get("end_date_iso") or market.get("close_time")
    if close_time:
        try:
            from datetime import datetime, timezone
            if isinstance(close_time, str):
                # Handle ISO format
                ct = close_time.replace("Z", "+00:00")
                close_dt = datetime.fromisoformat(ct)
            elif isinstance(close_time, (int, float)):
                close_dt = datetime.fromtimestamp(close_time / 1000, tz=timezone.utc)
            else:
                return False

            now = datetime.now(timezone.utc)
            hours_until_close = (close_dt - now).total_seconds() / 3600
            if 0 < hours_until_close < RESOLUTION_SNIPE_WINDOW_HOURS:
                return True
        except (ValueError, TypeError, OverflowError):
            pass

    # Kalshi: check status field
    status = market.get("status", "").lower()
    if status in ("closed", "settled", "finalized"):
        return False  # Already resolved — too late
    if status in ("closing", "determination_pending"):
        return True

    # Check if market has "resolved" or "resolving" in any field
    for key in ("result", "resolution", "state"):
        val = str(market.get(key, "")).lower()
        if val in ("resolving", "pending_resolution"):
            return True

    return False


def _extract_outcome_prices(market: dict, platform: str) -> dict[str, float]:
    """Extract outcome prices from a market dict.

    Returns:
        Dict mapping outcome name to price (e.g., {"yes": 0.96, "no": 0.04}).
    """
    prices = {}

    if platform == "polymarket":
        # Polymarket token prices
        tokens = market.get("tokens", [])
        for token in tokens:
            outcome = token.get("outcome", "").lower()
            price = token.get("price")
            if price is not None and outcome:
                prices[outcome] = float(price)
        # Alternative format
        if not prices:
            outcome_prices = market.get("outcomePrices") or []
            if not isinstance(outcome_prices, (list, tuple)):
                outcome_prices = []
            yes_price = market.get("yes_price")
            no_price = market.get("no_price")
            if yes_price is None and len(outcome_prices) > 0:
                yes_price = outcome_prices[0]
            if no_price is None and len(outcome_prices) > 1:
                no_price = outcome_prices[1]
            if yes_price is not None:
                prices["yes"] = float(yes_price)
            if no_price is not None:
                prices["no"] = float(no_price)

    elif platform == "kalshi":
        yes_price = market.get("yes_ask") or market.get("yes_price")
        no_price = market.get("no_ask") or market.get("no_price")
        if yes_price is not None:
            prices["yes"] = float(yes_price) / 100 if float(yes_price) > 1 else float(yes_price)
        if no_price is not None:
            prices["no"] = float(no_price) / 100 if float(no_price) > 1 else float(no_price)

    elif platform in ("gemini", "ibkr"):
        contracts = market.get("contracts", [])
        for contract in contracts:
            side = contract.get("side", "").lower()
            price = contract.get("price") or contract.get("lastTradePrice")
            if price is not None and side in ("yes", "no"):
                prices[side] = float(price)

    return prices


def _build_signal_map(signal_sources: list[dict] | None) -> dict[str, list[dict]]:
    """Build a lookup map from market_key to signal entries."""
    if not signal_sources:
        return {}
    result: dict[str, list[dict]] = {}
    for signal in signal_sources:
        key = signal.get("market_key", "")
        if key:
            result.setdefault(key, []).append(signal)
    return result


def _get_consensus(market_key: str, outcome: str, signal_map: dict) -> float | None:
    """Get consensus probability for an outcome from signal sources.

    Returns weighted average probability or None if no signals exist.
    """
    signals = signal_map.get(market_key, [])
    if not signals:
        return None

    total_weight = 0.0
    weighted_sum = 0.0
    for signal in signals:
        prob = signal.get("probability")
        weight = signal.get("weight", 1.0)
        if prob is not None:
            # If outcome is "no", flip the probability
            if outcome == "no":
                prob = 1.0 - prob
            weighted_sum += prob * weight
            total_weight += weight

    if total_weight <= 0:
        return None
    return weighted_sum / total_weight
