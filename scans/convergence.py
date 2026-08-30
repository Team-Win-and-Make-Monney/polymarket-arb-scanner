"""Cross-platform convergence — directional bets on outlier prices converging to median."""

import logging
import statistics

from .helpers import capital_efficiency_score

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Cross-platform convergence detection
# ---------------------------------------------------------------------------

def scan_convergence(
    matched_markets: list[dict],
    min_divergence: float = 0.05,
    min_platforms: int = 3,
    min_profit: float = 0.005,
) -> list[dict]:
    """Detect cross-platform convergence opportunities.

    When one platform's price significantly diverges from the median of all
    other platforms (but not enough for pure arb after fees), take a
    directional position expecting the outlier to converge.

    Args:
        matched_markets: List of matched market dicts, each with
            ``market_key``, ``title``, and ``platform_prices`` dict mapping
            platform names to price dicts (with ``yes`` and ``no`` fields).
        min_divergence: Minimum divergence from median to qualify (0-1).
        min_platforms: Minimum number of platforms with prices for the market.
        min_profit: Minimum estimated net profit.

    Returns:
        List of opportunity dicts sorted by estimated profit.
    """
    opportunities = []

    for match in matched_markets:
        market_key = match.get("market_key", "")
        title = match.get("title", market_key)
        platform_prices = match.get("platform_prices", {})

        if len(platform_prices) < min_platforms:
            continue

        # Extract YES prices from each platform
        yes_prices = {}
        for platform, prices in platform_prices.items():
            yes_price = prices.get("yes")
            if yes_price is not None and 0 < yes_price < 1:
                yes_prices[platform] = yes_price

        if len(yes_prices) < min_platforms:
            continue

        # Calculate median price (excluding each platform to detect outliers)
        all_prices = list(yes_prices.values())
        median_price = statistics.median(all_prices)

        for platform, price in yes_prices.items():
            divergence = price - median_price

            if abs(divergence) < min_divergence:
                continue

            # Calculate median without this platform (leave-one-out)
            others = [p for plat, p in yes_prices.items() if plat != platform]
            if len(others) < 2:
                continue
            loo_median = statistics.median(others)

            divergence_from_loo = price - loo_median

            if abs(divergence_from_loo) < min_divergence:
                continue

            # Estimate profit: convergence to median minus a conservative
            # fee charged against capital at risk, not against the edge.
            gross_profit = abs(divergence_from_loo)
            # Direction: if platform is cheap (below median), buy YES there.
            # If platform is expensive (above median), buy NO there.
            if divergence_from_loo < 0:
                direction = "BUY_YES"
                trade_price = price
            else:
                direction = "BUY_NO"
                trade_price = 1.0 - price

            estimated_fee = trade_price * 0.05
            net_profit = gross_profit - estimated_fee
            if net_profit < min_profit:
                continue

            net_roi = net_profit / trade_price if trade_price > 0 else 0

            # Confidence based on number of agreeing platforms and divergence size
            confidence = _convergence_confidence(
                len(others), abs(divergence_from_loo), loo_median
            )

            opportunity = {
                "type": "ConvergenceOpp",
                "_layer": 4,  # Layer 4: informed trading
                "market": title[:60],
                "prices": (
                    f"{platform}={price:.4f} "
                    f"median={loo_median:.4f} "
                    f"div={divergence_from_loo:+.4f}"
                ),
                "total_cost": f"${trade_price:.4f}",
                "net_profit": net_profit,
                "net_roi": net_roi,
                "confidence": confidence,
                "_platform": platform,
                "_direction": direction,
                "_trade_price": trade_price,
                "_median_price": loo_median,
                "_divergence": divergence_from_loo,
                "_num_platforms": len(yes_prices),
                "_market_key": market_key,
                "_platform_prices": yes_prices,
            }
            opportunity["_efficiency"] = capital_efficiency_score(opportunity)
            opportunities.append(opportunity)

    opportunities.sort(key=lambda o: o["net_profit"], reverse=True)
    logger.info("Convergence scan: found %d opportunities", len(opportunities))
    return opportunities


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _convergence_confidence(
    num_agreeing: int,
    divergence: float,
    median: float,
) -> float:
    """Estimate confidence (0-1) for a convergence opportunity.

    Higher when more platforms agree and the outlier is clearly wrong.

    Args:
        num_agreeing: Number of platforms whose prices are near the median.
        divergence: Absolute divergence of the outlier from the median.
        median: The leave-one-out median price.
    """
    # More platforms agreeing = higher confidence
    platform_factor = min(1.0, num_agreeing / 5.0)  # 5+ platforms = max

    # Moderate divergence is best — too small is noise, too large might be correct
    if divergence < 0.03:
        div_factor = 0.3
    elif divergence < 0.08:
        div_factor = 0.8
    elif divergence < 0.15:
        div_factor = 0.7
    else:
        div_factor = 0.5  # Huge divergence — maybe the outlier knows something

    # Mid-range prices are more reliable than extremes
    if 0.2 < median < 0.8:
        range_factor = 0.9
    elif 0.1 < median < 0.9:
        range_factor = 0.7
    else:
        range_factor = 0.5

    return round(platform_factor * div_factor * range_factor, 3)
