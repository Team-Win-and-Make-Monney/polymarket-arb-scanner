"""Expert Individual Forecaster Divergence — Strategy #40.

Trade when superforecaster predictions diverge from market prices.

Superforecasters have historically outperformed prediction markets
on certain question types. When their forecasts diverge significantly
from market prices, there may be alpha in betting on convergence.

Sources:
- Good Judgment Open (superforecaster track)
- INFER (intelligence community forecasters)
- Metaculus (community + expert forecasters)

Layer 4: Informed trading — directional bet based on expert edge.
"""

import logging

from config import (
    EXPERT_DIVERGENCE_ENABLED,
    EXPERT_DIVERGENCE_MIN_DIVERGENCE,
    EXPERT_DIVERGENCE_MIN_FORECASTERS,
)
from .helpers import capital_efficiency_score, filter_dust

logger = logging.getLogger(__name__)


def scan_expert_divergence(
    markets: list[dict],
    platform: str = "polymarket",
    superforecaster_client=None,
    metaculus_client=None,
    min_divergence: float | None = None,
    min_forecasters: int | None = None,
    min_profit: float = 0.005,
) -> list[dict]:
    """Scan for expert forecaster divergence opportunities.

    Identifies markets where superforecaster predictions diverge
    significantly from the current market price.

    Args:
        markets: List of market dicts.
        platform: Platform name for the markets.
        superforecaster_client: SuperforecasterClient instance.
        metaculus_client: Optional MetaculusClient for additional signal.
        min_divergence: Minimum divergence to flag (default from config).
        min_forecasters: Minimum forecasters required (default from config).
        min_profit: Minimum net profit threshold.

    Returns:
        List of opportunity dicts sorted by net_profit descending.
    """
    if not EXPERT_DIVERGENCE_ENABLED:
        return []

    if superforecaster_client is None:
        logger.debug("Expert divergence scan requires SuperforecasterClient")
        return []

    if min_divergence is None:
        min_divergence = EXPERT_DIVERGENCE_MIN_DIVERGENCE
    if min_forecasters is None:
        min_forecasters = EXPERT_DIVERGENCE_MIN_FORECASTERS
    opportunities = []

    for market in markets:
        title = market.get("title") or market.get("question", "")
        market_price = market.get("yes_price") or market.get("yes_mid", 0)

        if not title or not market_price or market_price <= 0 or market_price >= 1:
            continue

        expert_forecast = superforecaster_client.get_aggregated_expert_forecast(
            market_title=title,
            metaculus_client=metaculus_client,
        )

        if expert_forecast is None:
            continue

        if expert_forecast.get("num_sources", 0) < min_forecasters:
            continue

        expert_prob = expert_forecast["probability"]
        divergence = expert_prob - market_price

        if abs(divergence) < min_divergence:
            continue

        if divergence > 0:
            direction = "BUY_YES"
            entry_price = market_price
            edge = divergence
        else:
            direction = "BUY_NO"
            entry_price = 1.0 - market_price
            edge = -divergence

        from fees import net_profit_expert_divergence
        result = net_profit_expert_divergence(
            market_price=entry_price,
            expert_prob=expert_prob if direction == "BUY_YES" else (1.0 - expert_prob),
            platform=platform,
        )

        if result["net_profit"] < min_profit:
            continue

        confidence = expert_forecast.get("confidence", 0.60)

        opp = {
            "type": "ExpertDivergence",
            "_layer": 4,
            "market": f"{title[:40]}... (expert divergence)",
            "prices": f"market={market_price:.3f} expert={expert_prob:.3f} (div={divergence:+.3f})",
            "total_cost": f"${entry_price:.4f}",
            "net_profit": result["net_profit"],
            "net_roi": result.get("net_roi", 0),
            "confidence": confidence,
            "_market_key": market.get("condition_id") or market.get("id", ""),
            "_platform": platform,
            "_market": market,
            "_market_price": market_price,
            "_expert_prob": expert_prob,
            "_num_sources": expert_forecast.get("num_sources", 1),
            "_divergence": divergence,
            "_direction": direction,
        }
        opp["_efficiency"] = capital_efficiency_score(opp)
        opportunities.append(opp)

    opportunities = filter_dust(opportunities, min_amount=min_profit)
    opportunities.sort(key=lambda o: o["net_profit"], reverse=True)

    logger.info("Expert divergence scan: found %d opportunities", len(opportunities))
    return opportunities
