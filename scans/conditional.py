"""Conditional Market Arbitrage — Strategy #30.

Detects mispricings between conditional and unconditional markets where:
    P(X|Y) × P(Y) ≠ P(X)

Example:
    "Biden wins nomination" (P(Y)) = 0.90
    "Biden wins general IF nominated" (P(X|Y)) = 0.55
    "Biden wins general" (P(X)) = 0.52

    Combined: 0.90 × 0.55 = 0.495 ≠ 0.52

    If 0.495 < 0.52: Buy conditional combo, sell unconditional
    If 0.495 > 0.52: Buy unconditional, sell conditional combo

Requires parsing conditional market relationships from titles.
"""

import logging
import re
from concurrent.futures import ThreadPoolExecutor, as_completed

try:
    from thefuzz import fuzz
except ImportError:
    try:
        from fuzzywuzzy import fuzz
    except ImportError:
        fuzz = None

from config import (
    CONDITIONAL_ARB_ENABLED,
    CONDITIONAL_ARB_MIN_DIVERGENCE,
    CONDITIONAL_ARB_MAX_TRADE_SIZE,
)
from .helpers import capital_efficiency_score, filter_dust, _fetch_clob_for_market

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Conditional relationship patterns
# ---------------------------------------------------------------------------

CONDITIONAL_PATTERNS = [
    re.compile(r"(.+?)\s+(?:if|given|assuming|conditional on)\s+(.+)", re.IGNORECASE),
    re.compile(r"(.+?)\s+\|\s+(.+)", re.IGNORECASE),
    re.compile(r"Will\s+(.+?)\s+if\s+(.+)\?", re.IGNORECASE),
]


def _parse_conditional(title: str) -> tuple[str, str] | None:
    """Extract (outcome, condition) from a conditional market title.

    Returns:
        Tuple of (outcome_event, condition_event) or None if not conditional.
    """
    for pattern in CONDITIONAL_PATTERNS:
        match = pattern.search(title)
        if match:
            outcome = match.group(1).strip()
            condition = match.group(2).strip()
            if outcome and condition:
                return (outcome, condition)
    return None


def _find_matching_market(
    target_title: str,
    markets: list[dict],
    threshold: int = 75,
) -> dict | None:
    """Find a market matching the target title using fuzzy matching.

    Args:
        target_title: Title to search for.
        markets: List of market dicts with 'title' or 'question' keys.
        threshold: Minimum fuzzy match score (0-100).

    Returns:
        Best matching market dict or None.
    """
    if fuzz is None:
        return None

    best_match = None
    best_score = threshold

    for market in markets:
        title = market.get("title") or market.get("question", "")
        score = fuzz.token_set_ratio(target_title.lower(), title.lower())
        if score > best_score:
            best_score = score
            best_match = market

    return best_match


# ---------------------------------------------------------------------------
# Scan functions
# ---------------------------------------------------------------------------

def scan_conditional_arb(
    markets: list[dict],
    min_divergence: float | None = None,
    min_profit: float = 0.005,
    price_cache: dict | None = None,
) -> list[dict]:
    """Scan for conditional market arbitrage opportunities.

    Identifies triplets: conditional market P(X|Y), condition market P(Y),
    and unconditional market P(X), where the combined probability diverges
    from the direct probability.

    Args:
        markets: List of market dicts with title, yes_price, no_price.
        min_divergence: Minimum price divergence to flag (default from config).
        min_profit: Minimum net profit threshold.
        price_cache: Optional WS price cache for CLOB refinement.

    Returns:
        List of opportunity dicts sorted by net_profit descending.
    """
    if not CONDITIONAL_ARB_ENABLED:
        return []

    min_divergence = min_divergence or CONDITIONAL_ARB_MIN_DIVERGENCE
    opportunities = []
    markets_by_title: dict[str, dict] = {}

    for market in markets:
        title = market.get("title") or market.get("question", "")
        if title:
            markets_by_title[title.lower()] = market

    conditional_markets = []
    for market in markets:
        title = market.get("title") or market.get("question", "")
        parsed = _parse_conditional(title)
        if parsed:
            conditional_markets.append({
                "market": market,
                "outcome_event": parsed[0],
                "condition_event": parsed[1],
            })

    logger.debug("Found %d conditional markets to analyze", len(conditional_markets))

    for cond_info in conditional_markets:
        cond_market = cond_info["market"]
        outcome_event = cond_info["outcome_event"]
        condition_event = cond_info["condition_event"]

        condition_market = _find_matching_market(condition_event, markets)
        if not condition_market:
            continue

        unconditional_market = _find_matching_market(outcome_event, markets)
        if not unconditional_market:
            continue

        if condition_market == cond_market or unconditional_market == cond_market:
            continue

        p_x_given_y = cond_market.get("yes_price") or cond_market.get("yes_mid", 0)
        p_y = condition_market.get("yes_price") or condition_market.get("yes_mid", 0)
        p_x = unconditional_market.get("yes_price") or unconditional_market.get("yes_mid", 0)

        if not all([p_x_given_y, p_y, p_x]):
            continue

        combined_prob = p_x_given_y * p_y
        divergence = abs(combined_prob - p_x)

        if divergence < min_divergence:
            continue

        if combined_prob < p_x:
            direction = "BUY_CONDITIONAL"
            profit_per_unit = p_x - combined_prob
        else:
            direction = "BUY_UNCONDITIONAL"
            profit_per_unit = combined_prob - p_x

        from fees import net_profit_conditional
        result = net_profit_conditional(
            p_x_given_y=p_x_given_y,
            p_y=p_y,
            p_x=p_x,
            direction=direction,
        )

        if result["net_profit"] < min_profit:
            continue

        cond_title = cond_market.get("title") or cond_market.get("question", "")
        opp = {
            "type": "ConditionalArb",
            "_layer": 1,
            "market": f"{cond_title[:40]}... (conditional arb)",
            "prices": f"P(X|Y)={p_x_given_y:.3f} P(Y)={p_y:.3f} P(X)={p_x:.3f}",
            "total_cost": f"${result.get('total_cost', 0):.4f}",
            "net_profit": result["net_profit"],
            "net_roi": result.get("net_roi", 0),
            "confidence": 0.85,
            "_market_key": cond_market.get("condition_id") or cond_market.get("id", ""),
            "_platform": cond_market.get("platform", "polymarket"),
            "_conditional_market": cond_market,
            "_condition_market": condition_market,
            "_unconditional_market": unconditional_market,
            "_direction": direction,
            "_divergence": divergence,
            "_p_x_given_y": p_x_given_y,
            "_p_y": p_y,
            "_p_x": p_x,
        }
        opp["_efficiency"] = capital_efficiency_score(opp)
        opportunities.append(opp)

    opportunities = _refine_conditional_with_clob(
        opportunities, markets_by_title, min_profit, price_cache
    )
    opportunities = filter_dust(opportunities, min_amount=min_profit)
    opportunities.sort(key=lambda o: o["net_profit"], reverse=True)

    logger.info("Conditional arb scan: found %d opportunities", len(opportunities))
    return opportunities


def _refine_conditional_with_clob(
    opportunities: list[dict],
    markets_by_title: dict[str, dict],
    min_profit: float,
    price_cache: dict | None = None,
) -> list[dict]:
    """Refine conditional arb opportunities with CLOB ask prices.

    Re-validates each opportunity using actual ask prices instead of
    mid prices to ensure profitability survives execution.

    Args:
        opportunities: List of opportunity dicts from Stage 1.
        markets_by_title: Dict mapping lowercase titles to market dicts.
        min_profit: Minimum net profit threshold.
        price_cache: Optional WS price cache.

    Returns:
        Filtered list of opportunities that survive ask-price validation.
    """
    if not opportunities:
        return []

    refined = []
    fetch_tasks = {}

    for opp in opportunities:
        for key in ["_conditional_market", "_condition_market", "_unconditional_market"]:
            market = opp.get(key)
            if market:
                market_key = market.get("condition_id") or market.get("id", "")
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
        cond_market = opp["_conditional_market"]
        condition_market = opp["_condition_market"]
        uncond_market = opp["_unconditional_market"]

        cond_key = cond_market.get("condition_id") or cond_market.get("id", "")
        condition_key = condition_market.get("condition_id") or condition_market.get("id", "")
        uncond_key = uncond_market.get("condition_id") or uncond_market.get("id", "")

        cond_clob = clob_results.get(cond_key)
        condition_clob = clob_results.get(condition_key)
        uncond_clob = clob_results.get(uncond_key)

        # Leg pricing depends on direction (see fees.net_profit_conditional):
        # - BUY_CONDITIONAL: buy P(X|Y) and P(Y) at the ask, SELL P(X) at the bid.
        # - BUY_UNCONDITIONAL: buy P(X) at the ask, SELL P(X|Y) and P(Y) at the bid.
        # Sell legs realise the bid, not the ask — pricing every leg from
        # yes_ask overstated sell proceeds and inflated net_profit.
        # A SELL leg with no live bid is unexecutable — there is nothing to
        # sell into — so the opp is dropped rather than falling back to the
        # stale Stage-1 price. Buy legs keep the Stage-1 fallback.
        buy_conditional = opp["_direction"] == "BUY_CONDITIONAL"

        def _leg_price(clob: dict | None, is_buy: bool, fallback: float) -> float | None:
            if is_buy:
                if not clob:
                    return fallback
                val = clob.get("yes_ask")
                return val if val is not None else fallback
            # Sell leg: a live bid is required; None means "drop the opp".
            if not clob:
                return None
            return clob.get("yes_bid")

        p_x_given_y = _leg_price(cond_clob, buy_conditional, opp["_p_x_given_y"])
        p_y = _leg_price(condition_clob, buy_conditional, opp["_p_y"])
        p_x = _leg_price(uncond_clob, not buy_conditional, opp["_p_x"])

        if p_x_given_y is None or p_y is None or p_x is None:
            logger.debug(
                "Conditional dropped: sell leg has no live bid (%s)",
                opp.get("market", "?")[:50],
            )
            continue

        from fees import net_profit_conditional
        result = net_profit_conditional(
            p_x_given_y=p_x_given_y,
            p_y=p_y,
            p_x=p_x,
            direction=opp["_direction"],
        )

        if result["net_profit"] >= min_profit:
            opp["prices"] = f"P(X|Y)={p_x_given_y:.3f} P(Y)={p_y:.3f} P(X)={p_x:.3f}"
            opp["net_profit"] = result["net_profit"]
            opp["net_roi"] = result.get("net_roi", 0)
            opp["_p_x_given_y"] = p_x_given_y
            opp["_p_y"] = p_y
            opp["_p_x"] = p_x

            # Depth per leg matches the side actually traded: ask size for
            # buy legs, bid size for sell legs. Every leg contributes — a
            # missing book or missing traded-side size is ZERO executable
            # depth, not a skipped leg (fail-closed).
            depths = []
            for clob, is_buy in [
                (cond_clob, buy_conditional),
                (condition_clob, buy_conditional),
                (uncond_clob, not buy_conditional),
            ]:
                price_key = "yes_ask" if is_buy else "yes_bid"
                size_key = "yes_ask_size" if is_buy else "yes_bid_size"
                depth = (
                    ((clob or {}).get(size_key) or 0)
                    if clob and clob.get(price_key) is not None
                    else 0
                )
                depths.append(depth)
            opp["_clob_depth"] = min(depths)

            refined.append(opp)

    return refined
