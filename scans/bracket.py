"""Bracket/Range Market Arbitrage — Strategy #31.

Detects when the sum of mutually exclusive range brackets is less than 1.0:
    Σ(P(range_i)) < 1.0

Example on Kalshi:
    "S&P 5000-5100 EOD" = 0.22
    "S&P 5100-5200 EOD" = 0.25
    "S&P 5200-5300 EOD" = 0.25
    "S&P 5300+ EOD"     = 0.23
    Total = 0.95 → 5% guaranteed profit by buying all brackets (pay $0.95, receive $1.00)

Requires grouping range markets by their base event (e.g., "S&P EOD").
"""

import logging
import re
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed

from config import (
    BRACKET_ARB_ENABLED,
    BRACKET_ARB_MIN_SPREAD,
    BRACKET_ARB_MAX_TRADE_SIZE,
)
from .helpers import capital_efficiency_score, filter_dust, _fetch_clob_for_market

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Range/bracket parsing patterns
# ---------------------------------------------------------------------------

RANGE_PATTERNS = [
    re.compile(r"(.+?)\s+(\d+(?:\.\d+)?)[–\-](\d+(?:\.\d+)?)\s*(.*)"),
    re.compile(r"(.+?)\s+(\d+(?:\.\d+)?)\s*(?:to|and)\s*(\d+(?:\.\d+)?)\s*(.*)"),
    re.compile(r"(.+?)\s+between\s+(\d+(?:\.\d+)?)\s+and\s+(\d+(?:\.\d+)?)\s*(.*)"),
    re.compile(r"(.+?)\s+(\d+(?:\.\d+)?)\+\s*(.*)"),
    re.compile(r"(.+?)\s+(?:above|over|greater than)\s+(\d+(?:\.\d+)?)\s*(.*)"),
    re.compile(r"(.+?)\s+(?:below|under|less than)\s+(\d+(?:\.\d+)?)\s*(.*)"),
]


def _parse_bracket(title: str) -> dict | None:
    """Parse a bracket/range market title.

    Returns:
        Dict with base_event, lower_bound, upper_bound, suffix, or None.
    """
    for pattern in RANGE_PATTERNS:
        match = pattern.search(title)
        if match:
            groups = match.groups()
            if len(groups) >= 3:
                base_event = groups[0].strip()
                suffix = groups[-1].strip() if len(groups) > 3 else ""

                if "+" in title or "above" in title.lower() or "over" in title.lower():
                    return {
                        "base_event": base_event,
                        "lower_bound": float(groups[1]),
                        "upper_bound": float("inf"),
                        "suffix": suffix,
                    }
                elif "below" in title.lower() or "under" in title.lower():
                    return {
                        "base_event": base_event,
                        "lower_bound": float("-inf"),
                        "upper_bound": float(groups[1]),
                        "suffix": suffix,
                    }
                else:
                    try:
                        return {
                            "base_event": base_event,
                            "lower_bound": float(groups[1]),
                            "upper_bound": float(groups[2]),
                            "suffix": suffix,
                        }
                    except (ValueError, IndexError):
                        continue
    return None


def _normalize_base_event(base: str, suffix: str) -> str:
    """Create a normalized key for grouping related bracket markets."""
    combined = f"{base.lower().strip()} {suffix.lower().strip()}".strip()
    combined = re.sub(r"\s+", " ", combined)
    combined = re.sub(r"[^\w\s]", "", combined)
    return combined


def _group_brackets(markets: list[dict]) -> dict[str, list[dict]]:
    """Group markets by their base event into bracket sets.

    Returns:
        Dict mapping base_event_key to list of bracket market dicts.
    """
    groups: dict[str, list[dict]] = defaultdict(list)

    for market in markets:
        title = market.get("title") or market.get("question", "")
        parsed = _parse_bracket(title)
        if parsed:
            key = _normalize_base_event(parsed["base_event"], parsed["suffix"])
            market["_bracket_info"] = parsed
            groups[key].append(market)

    return {k: v for k, v in groups.items() if len(v) >= 2}


def _brackets_are_complete(brackets: list[dict]) -> bool:
    """Check if brackets form a complete, mutually exclusive set.

    A complete set should cover all possible outcomes without overlap.
    """
    if len(brackets) < 2:
        return False

    sorted_brackets = sorted(
        brackets,
        key=lambda m: m["_bracket_info"]["lower_bound"]
    )

    for i in range(len(sorted_brackets) - 1):
        current = sorted_brackets[i]["_bracket_info"]
        next_bracket = sorted_brackets[i + 1]["_bracket_info"]

        if current["upper_bound"] == float("inf"):
            return True

        if abs(current["upper_bound"] - next_bracket["lower_bound"]) > 0.01:
            return False

    return True


# ---------------------------------------------------------------------------
# Scan functions
# ---------------------------------------------------------------------------

def scan_bracket_arb(
    markets: list[dict],
    min_spread: float | None = None,
    min_profit: float = 0.005,
    price_cache: dict | None = None,
) -> list[dict]:
    """Scan for bracket/range market arbitrage opportunities.

    Groups range markets by base event and identifies sets where
    Σ(prices) < 1.0 (underround), allowing guaranteed profit by buying all.

    Args:
        markets: List of market dicts with title, yes_price.
        min_spread: Minimum spread (1.0 - total_cost) to flag (default from config).
        min_profit: Minimum net profit threshold.
        price_cache: Optional WS price cache for CLOB refinement.

    Returns:
        List of opportunity dicts sorted by net_profit descending.
    """
    if not BRACKET_ARB_ENABLED:
        return []

    min_spread = min_spread or BRACKET_ARB_MIN_SPREAD
    opportunities = []

    bracket_groups = _group_brackets(markets)
    logger.debug("Found %d bracket groups to analyze", len(bracket_groups))

    for base_event, brackets in bracket_groups.items():
        prices = []
        for bracket in brackets:
            price = bracket.get("yes_price") or bracket.get("yes_mid", 0)
            if price > 0:
                prices.append(price)

        if len(prices) < 2:
            continue

        total_cost = sum(prices)
        spread = 1.0 - total_cost

        if spread < min_spread:
            continue

        from fees import net_profit_bracket
        platform = brackets[0].get("platform", "kalshi")
        result = net_profit_bracket(prices, platform=platform)

        if result["net_profit"] < min_profit:
            continue

        bracket_titles = [
            (b.get("title") or b.get("question", ""))[:30]
            for b in brackets[:3]
        ]
        market_desc = f"{base_event[:30]} ({len(brackets)} brackets)"

        opp = {
            "type": "BracketArb",
            "_layer": 1,
            "market": market_desc,
            "prices": f"Σ={total_cost:.4f} (spread={spread:.4f})",
            "total_cost": f"${total_cost:.4f}",
            "net_profit": result["net_profit"],
            "net_roi": result.get("net_roi", 0),
            "confidence": 0.90 if _brackets_are_complete(brackets) else 0.70,
            "_market_key": base_event,
            "_platform": platform,
            "_brackets": brackets,
            "_bracket_prices": prices,
            "_spread": spread,
            "_num_brackets": len(brackets),
        }
        opp["_efficiency"] = capital_efficiency_score(opp)
        opportunities.append(opp)

    opportunities = _refine_bracket_with_clob(
        opportunities, min_profit, price_cache
    )
    opportunities = filter_dust(opportunities, min_amount=min_profit)
    opportunities.sort(key=lambda o: o["net_profit"], reverse=True)

    logger.info("Bracket arb scan: found %d opportunities", len(opportunities))
    return opportunities


def _refine_bracket_with_clob(
    opportunities: list[dict],
    min_profit: float,
    price_cache: dict | None = None,
) -> list[dict]:
    """Refine bracket arb opportunities with CLOB ask prices.

    Args:
        opportunities: List of opportunity dicts from Stage 1.
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
        for bracket in opp.get("_brackets", []):
            market_key = bracket.get("condition_id") or bracket.get("id", "")
            if market_key and market_key not in fetch_tasks:
                fetch_tasks[market_key] = bracket

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
        brackets = opp.get("_brackets", [])
        prices = []
        depths = []

        for bracket in brackets:
            market_key = bracket.get("condition_id") or bracket.get("id", "")
            clob = clob_results.get(market_key)

            if clob:
                price = clob.get("yes_ask", 0)
                depth = clob.get("yes_ask_size", 0)
            else:
                price = bracket.get("yes_price") or bracket.get("yes_mid", 0)
                depth = 0

            if price > 0:
                prices.append(price)
                depths.append(depth)

        if len(prices) < 2:
            continue

        total_cost = sum(prices)
        overround = total_cost - 1.0

        # overround < 0 means total ask cost < $1 -> guaranteed profit; KEEP those.
        # Drop overround >= 0 (no edge after re-pricing on the ask side).
        if overround >= 0:
            continue

        from fees import net_profit_bracket
        platform = opp.get("_platform", "kalshi")
        result = net_profit_bracket(prices, platform=platform)

        if result["net_profit"] >= min_profit:
            opp["prices"] = f"Σ={total_cost:.4f} (overround={overround:.4f})"
            opp["total_cost"] = f"${total_cost:.4f}"
            opp["net_profit"] = result["net_profit"]
            opp["net_roi"] = result.get("net_roi", 0)
            opp["_bracket_prices"] = prices
            opp["_overround"] = overround
            opp["_clob_depth"] = min(depths) if depths else 0
            refined.append(opp)

    return refined
