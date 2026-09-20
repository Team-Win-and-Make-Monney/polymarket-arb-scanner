"""Settlement Timing Arbitrage — Strategy #33.

Buy winning outcomes on slow-settling platforms before settlement propagates.

Different platforms settle events at different times after resolution:
- Polymarket: Usually within minutes to hours
- Kalshi: Same day or next business day
- Betfair/Smarkets: Within hours to 1 day

When Platform A settles and Platform B hasn't yet:
1. Platform A confirms the outcome (price → 1.0 or 0.0)
2. Platform B still trades at 0.97-0.99 for the winning outcome
3. Buy the winning side on Platform B at discount
4. Receive full payout when Platform B settles

Layer 2: Near-arbitrage — outcome is known but not yet settled everywhere.
"""

import logging
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

from config import (
    SETTLEMENT_TIMING_ENABLED,
    SETTLEMENT_TIMING_MIN_DISCOUNT,
    SETTLEMENT_TIMING_MAX_TRADE_SIZE,
)
from .helpers import capital_efficiency_score, filter_dust, _fetch_clob_for_market

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Platform settlement detection
# ---------------------------------------------------------------------------

def _is_settled(market: dict, platform: str) -> tuple[bool, str | None]:
    """Check if a market has settled on a platform.

    Returns:
        Tuple of (is_settled: bool, winning_side: "yes" | "no" | None).
    """
    if platform == "polymarket":
        if market.get("closed") or market.get("resolved"):
            resolution = market.get("resolution_outcome") or market.get("resolution")
            if resolution in ("Yes", "YES", "yes", "1", 1):
                return True, "yes"
            elif resolution in ("No", "NO", "no", "0", 0):
                return True, "no"
    elif platform == "kalshi":
        status = market.get("status", "").lower()
        if status in ("settled", "closed", "resolved"):
            result = market.get("result") or market.get("settlement_value")
            if result in ("yes", "YES", "Yes", 1, "1"):
                return True, "yes"
            elif result in ("no", "NO", "No", 0, "0"):
                return True, "no"
    elif platform in ("betfair", "smarkets", "matchbook"):
        status = market.get("status", "").lower()
        if status in ("closed", "settled", "resulted"):
            winner = market.get("winner") or market.get("settlement_outcome")
            if winner:
                return True, "yes" if "yes" in str(winner).lower() else "no"

    return False, None


def _get_settlement_price(
    market: dict,
    platform: str,
    side: str,
) -> float | None:
    """Get the current trading price for a side on an unsettled platform.

    Returns the price for the specified side if market is still trading.
    """
    if platform == "polymarket":
        if side == "yes":
            return market.get("yes_price") or market.get("yes_mid")
        else:
            return market.get("no_price") or market.get("no_mid")
    elif platform == "kalshi":
        if side == "yes":
            return market.get("yes_price") or market.get("yes_bid")
        else:
            return market.get("no_price") or market.get("no_bid")
    else:
        if side == "yes":
            return market.get("yes_price") or market.get("back_price")
        else:
            return market.get("no_price") or market.get("lay_price")

    return None


# ---------------------------------------------------------------------------
# Scan function
# ---------------------------------------------------------------------------

def scan_settlement_timing(
    cross_matched_markets: list[dict],
    platform_clients: dict | None = None,
    min_discount: float | None = None,
    min_profit: float = 0.005,
    price_cache: dict | None = None,
) -> list[dict]:
    """Scan for settlement timing arbitrage opportunities.

    Identifies markets that have settled on one platform but are still
    trading at a discount on another platform.

    Args:
        cross_matched_markets: List of matched market dicts with markets
            from multiple platforms.
        platform_clients: Dict of platform clients for fetching settlement status.
        min_discount: Minimum discount from payout to flag (default from config).
        min_profit: Minimum net profit threshold.
        price_cache: Optional WS price cache.

    Returns:
        List of opportunity dicts sorted by net_profit descending.
    """
    if not SETTLEMENT_TIMING_ENABLED:
        return []

    min_discount = min_discount or SETTLEMENT_TIMING_MIN_DISCOUNT
    opportunities = []

    logger.debug("Scanning %d cross-matched markets for settlement timing", len(cross_matched_markets))

    for match in cross_matched_markets:
        platform_a = match.get("platform_a", "")
        platform_b = match.get("platform_b", "")
        market_a = match.get("market_a") or match.get("a", {})
        market_b = match.get("market_b") or match.get("b", {})

        if not all([platform_a, platform_b, market_a, market_b]):
            continue

        settled_a, winner_a = _is_settled(market_a, platform_a)
        settled_b, winner_b = _is_settled(market_b, platform_b)

        if settled_a and not settled_b and winner_a:
            slow_platform = platform_b
            slow_market = market_b
            winning_side = winner_a
        elif settled_b and not settled_a and winner_b:
            slow_platform = platform_a
            slow_market = market_a
            winning_side = winner_b
        else:
            continue

        current_price = _get_settlement_price(slow_market, slow_platform, winning_side)
        if current_price is None or current_price <= 0:
            continue

        discount = 1.0 - current_price
        if discount < min_discount:
            continue

        from fees import net_profit_settlement_timing
        result = net_profit_settlement_timing(
            current_price=current_price,
            expected_payout=1.0,
            platform=slow_platform,
        )

        if result["net_profit"] < min_profit:
            continue

        title = (
            slow_market.get("title") or
            slow_market.get("question") or
            slow_market.get("ticker", "")
        )

        opp = {
            "type": "SettlementTimingArb",
            "_layer": 2,
            "market": f"{title[:40]}... (settlement timing)",
            "prices": f"{slow_platform}_{winning_side}={current_price:.4f} → $1.00",
            "total_cost": f"${current_price:.4f}",
            "net_profit": result["net_profit"],
            "net_roi": result.get("net_roi", 0),
            "confidence": 0.95,
            "_market_key": (
                slow_market.get("conditionId")
                or slow_market.get("condition_id")
                or slow_market.get("id", "")
            ),
            "_condition_id": (
                slow_market.get("conditionId")
                or slow_market.get("condition_id")
                or slow_market.get("id", "")
            ),
            "_platform": slow_platform,
            "_slow_market": slow_market,
            "_winning_side": winning_side,
            "_current_price": current_price,
            "_discount": discount,
            "_direction": f"BUY_{winning_side.upper()}",
        }
        opp["_efficiency"] = capital_efficiency_score(opp)
        opportunities.append(opp)

    opportunities = _refine_settlement_with_clob(
        opportunities, min_profit, min_discount, price_cache
    )
    opportunities = filter_dust(opportunities, min_amount=min_profit)
    opportunities.sort(key=lambda o: o["net_profit"], reverse=True)

    logger.info("Settlement timing scan: found %d opportunities", len(opportunities))
    return opportunities


def _refine_settlement_with_clob(
    opportunities: list[dict],
    min_profit: float,
    min_discount: float,
    price_cache: dict | None = None,
) -> list[dict]:
    """Refine settlement timing opportunities with CLOB ask prices.

    Args:
        opportunities: List of opportunity dicts from Stage 1.
        min_profit: Minimum net profit threshold.
        min_discount: Minimum discount from payout to keep opportunity.
        price_cache: Optional WS price cache.

    Returns:
        Filtered list of opportunities that survive ask-price validation.
    """
    if not opportunities:
        return []

    refined = []
    fetch_tasks = {}

    for opp in opportunities:
        market = opp.get("_slow_market")
        if market:
            market_key = (
                market.get("conditionId")
                or market.get("condition_id")
                or market.get("id", "")
            )
            if market_key and market_key not in fetch_tasks:
                fetch_tasks[market_key] = market

    clob_results = {}
    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = {
            pool.submit(_fetch_clob_for_market, m, price_cache): mk
            for mk, m in fetch_tasks.items()
        }
        for future in as_completed(futures):
            mk = futures[future]
            try:
                _, clob = future.result()
                if clob:
                    clob_results[mk] = clob
            except Exception as e:
                logger.debug("CLOB fetch failed for %s: %s", mk, e)

    for opp in opportunities:
        market = opp.get("_slow_market")
        market_key = (
            market.get("conditionId")
            or market.get("condition_id")
            or market.get("id", "")
        )
        winning_side = opp.get("_winning_side", "yes")
        platform = opp.get("_platform", "polymarket")

        clob = clob_results.get(market_key)
        if clob and isinstance(clob, dict):
            if winning_side == "yes":
                current_price = clob.get("yes_ask", opp["_current_price"])
                depth = clob.get("yes_ask_size", 0)
            else:
                current_price = clob.get("no_ask", opp["_current_price"])
                depth = clob.get("no_ask_size", 0)
        else:
            current_price = opp["_current_price"]
            depth = 0

        if not isinstance(current_price, (int, float)):
            current_price = opp["_current_price"]

        discount = 1.0 - current_price
        if discount < min_discount:
            continue

        from fees import net_profit_settlement_timing
        result = net_profit_settlement_timing(
            current_price=current_price,
            expected_payout=1.0,
            platform=platform,
        )

        if result["net_profit"] >= min_profit:
            opp["prices"] = f"{platform}_{winning_side}={current_price:.4f} → $1.00"
            opp["total_cost"] = f"${current_price:.4f}"
            opp["net_profit"] = result["net_profit"]
            opp["net_roi"] = result.get("net_roi", 0)
            opp["_current_price"] = current_price
            opp["_discount"] = discount
            opp["_clob_depth"] = depth
            refined.append(opp)

    return refined
