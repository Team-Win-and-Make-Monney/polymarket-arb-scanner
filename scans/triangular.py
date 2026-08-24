"""Triangular cross-platform arbitrage scans (3-way mispricings).

For a binary market (YES/NO) on platforms A, B, C:
- Find the cheapest YES across all 3 platforms
- Find the cheapest NO across all 3 platforms
- If cheapest_YES + cheapest_NO < 1.0, guaranteed profit regardless of outcome

This extends 2-way cross-platform arbs by considering all platforms simultaneously.
The best YES might be on platform A while the best NO is on platform C -- a pair that
sequential pairwise scanning would miss.
"""

import logging
from itertools import combinations

from concurrent.futures import ThreadPoolExecutor, as_completed

from polymarket_api import parse_outcome_prices, get_clob_prices
from matcher import match_cross_platform, match_cross_platform_semantic, _get_title
from config import FUZZY_MATCH_THRESHOLD, SEMANTIC_MATCHING_ENABLED, SEMANTIC_MATCH_THRESHOLD
from fees import net_profit_triangular
from scans.helpers import filter_dust, _extract_token_ids

logger = logging.getLogger(__name__)


def _get_market_prices(market: dict, platform: str, client=None) -> tuple[float | None, float | None]:
    """Extract YES/NO prices for a market on any platform.

    Args:
        market: Market dict from the platform API.
        platform: Platform name (e.g. "polymarket", "kalshi", "betfair").
        client: Platform client instance (required for non-Polymarket platforms).

    Returns:
        Tuple of (yes_price, no_price), or (None, None) if unavailable.
    """
    if platform == "polymarket":
        prices = parse_outcome_prices(market)
        if prices and len(prices) >= 2:
            return prices[0], prices[1]
        return None, None

    # All other platforms use client.get_market_price(market) -> (yes, no)
    if client is not None:
        try:
            result = client.get_market_price(market)
            if result and len(result) == 2:
                return result[0], result[1]
        except Exception as exc:
            logger.debug("Failed to get price from %s: %s", platform, exc)

    return None, None


def _group_cross_matches(all_matches: list[dict]) -> dict[str, dict]:
    """Group pairwise matches into multi-platform groups.

    Takes the output of multiple pairwise ``match_cross_platform()`` calls and
    groups them so that the same market across 3+ platforms is collected into a
    single entry.

    Uses a union-find approach: if market X on platform A matches market Y on
    platform B, and market Y on platform B matches market Z on platform C, then
    X, Y, Z are all the same market.

    Args:
        all_matches: List of match dicts from ``match_cross_platform()``.

    Returns:
        Dict keyed by canonical title, valued by dicts of
        ``{platform_name: market_dict}``.  Only groups with 3+ platforms are
        returned.
    """
    # parent[key] -> canonical parent key  (union-find)
    parent: dict[str, str] = {}
    # key -> (platform, market_dict, title)
    key_info: dict[str, tuple[str, dict, str]] = {}

    def _make_key(platform: str, market: dict) -> str:
        """Create a unique key for a (platform, market) pair."""
        market_id = (
            market.get("conditionId", "")
            or market.get("ticker", "")
            or market.get("event_ticker", "")
            or market.get("marketHash", "")
            or market.get("id", "")
            or _get_title(market)
        )
        return f"{platform}::{market_id}"

    def _find(key: str) -> str:
        while parent.get(key, key) != key:
            parent[key] = parent.get(parent[key], parent[key])
            key = parent[key]
        return key

    def _union(a: str, b: str):
        ra, rb = _find(a), _find(b)
        if ra != rb:
            parent[rb] = ra

    for match in all_matches:
        pa = match["platform_a"]
        pb = match["platform_b"]
        ma = match["market_a"]
        mb = match["market_b"]

        key_a = _make_key(pa, ma)
        key_b = _make_key(pb, mb)

        key_info[key_a] = (pa, ma, match.get("title_a", _get_title(ma)))
        key_info[key_b] = (pb, mb, match.get("title_b", _get_title(mb)))

        parent.setdefault(key_a, key_a)
        parent.setdefault(key_b, key_b)
        _union(key_a, key_b)

    # Collect groups
    groups: dict[str, dict[str, dict]] = {}
    group_titles: dict[str, str] = {}

    for key, (platform, market, title) in key_info.items():
        root = _find(key)
        if root not in groups:
            groups[root] = {}
            group_titles[root] = title
        groups[root][platform] = market

    # Filter to 3+ platforms
    return {
        group_titles[root]: platforms_dict
        for root, platforms_dict in groups.items()
        if len(platforms_dict) >= 3
    }


def _attach_exec_metadata(opp: dict, market: dict, platform: str):
    """Attach platform-specific execution metadata to a triangular opportunity."""
    if platform == "polymarket":
        opp["_token_ids"] = _extract_token_ids(market)
    elif platform == "kalshi":
        opp["_kalshi_ticker"] = market.get("ticker", "")
    elif platform == "betfair":
        opp["_market_id"] = market.get("marketId", "")
        runners = market.get("runners", [])
        if runners:
            opp["_selection_id"] = runners[0].get("selectionId")
    elif platform == "smarkets":
        opp["_sm_market_id"] = market.get("id", "")
    elif platform == "sxbet":
        opp["_sx_market_hash"] = market.get("marketHash", market.get("id", ""))
    elif platform == "matchbook":
        opp["_mb_market_id"] = market.get("id", "")
        runners = market.get("runners", [])
        if runners:
            opp["_mb_runner_id"] = runners[0].get("id")
    elif platform == "gemini":
        opp["_gm_event_id"] = market.get("id", "")
        contracts = market.get("contracts", [])
        for c in contracts:
            label = (c.get("label") or c.get("outcome") or "").lower()
            if "yes" in label:
                opp["_gm_yes_symbol"] = c.get("instrumentSymbol", "")
            elif "no" in label:
                opp["_gm_no_symbol"] = c.get("instrumentSymbol", "")
    elif platform == "ibkr":
        opp["_ibkr_event_id"] = market.get("id", "")
        contracts = market.get("contracts", [])
        for c in contracts:
            side = (c.get("side") or c.get("label") or "").upper()
            if "YES" in side:
                opp["_ibkr_yes_conid"] = c.get("conid", "")
            elif "NO" in side:
                opp["_ibkr_no_conid"] = c.get("conid", "")


def scan_triangular(
    platform_markets: dict[str, list],
    platform_clients: dict[str, object],
    min_profit: float,
    min_confidence: str = "LOW",
) -> list[dict]:
    """Scan for triangular (3+ platform) arbitrage opportunities.

    For each market that appears on 3+ platforms, finds the cheapest YES
    and cheapest NO across ALL platforms. If their sum < 1.0, it's a
    guaranteed profit regardless of outcome.

    This extends the 2-way cross-platform logic by considering all platforms
    simultaneously rather than in pairs.

    Args:
        platform_markets: Dict mapping platform name to list of market dicts.
            Example: {"polymarket": [...], "kalshi": [...], "betfair": [...]}
        platform_clients: Dict mapping platform name to client instance.
            Example: {"kalshi": kalshi_client, "betfair": bf_client}
            Polymarket does not require a client (uses parse_outcome_prices).
        min_profit: Minimum net profit threshold to include an opportunity.
        min_confidence: Minimum confidence tier for fuzzy matching.

    Returns:
        List of opportunity dicts sorted by net_profit descending.
    """
    opportunities = []

    # Need at least 3 platforms to find triangular arbs
    active_platforms = {name: mkts for name, mkts in platform_markets.items() if mkts}
    platforms = list(active_platforms.keys())

    if len(platforms) < 3:
        logger.info(
            "Triangular scan requires 3+ platforms, only %d active: %s. Skipping.",
            len(platforms),
            ", ".join(platforms),
        )
        return opportunities

    logger.info(
        "Triangular scan: matching across %d platforms: %s",
        len(platforms),
        ", ".join(platforms),
    )

    # Step 1: Run pairwise matching across all platform pairs
    all_matches = []
    for pa, pb in combinations(platforms, 2):
        markets_a = active_platforms[pa]
        markets_b = active_platforms[pb]
        if not markets_a or not markets_b:
            continue

        logger.debug("Matching %s (%d) vs %s (%d)...", pa, len(markets_a), pb, len(markets_b))
        if SEMANTIC_MATCHING_ENABLED:
            matched = match_cross_platform_semantic(
                markets_a,
                markets_b,
                pa,
                pb,
                threshold=SEMANTIC_MATCH_THRESHOLD,
                min_confidence=min_confidence,
            )
        else:
            matched = match_cross_platform(
                markets_a,
                markets_b,
                pa,
                pb,
                threshold=FUZZY_MATCH_THRESHOLD,
                min_confidence=min_confidence,
            )
        all_matches.extend(matched)
        logger.debug("Found %d matches between %s and %s", len(matched), pa, pb)

    if not all_matches:
        logger.info("No pairwise matches found across platforms.")
        return opportunities

    # Step 2: Group into multi-platform groups (3+ platforms)
    multi_groups = _group_cross_matches(all_matches)
    logger.info("Found %d markets appearing on 3+ platforms.", len(multi_groups))

    if not multi_groups:
        return opportunities

    # Step 3: For each multi-platform market, find cheapest YES and NO
    for market_title, platforms_dict in multi_groups.items():
        platform_prices = {}

        for platform_name, market in platforms_dict.items():
            client = platform_clients.get(platform_name)
            yes_price, no_price = _get_market_prices(market, platform_name, client)
            if yes_price is not None and no_price is not None:
                platform_prices[platform_name] = {
                    "yes": yes_price,
                    "no": no_price,
                    "market": market,
                }

        # Need prices from at least 3 platforms
        if len(platform_prices) < 3:
            continue

        # Find cheapest YES and cheapest NO across all platforms
        best_yes_platform = min(platform_prices, key=lambda p: platform_prices[p]["yes"])
        best_no_platform = min(platform_prices, key=lambda p: platform_prices[p]["no"])

        best_yes = platform_prices[best_yes_platform]["yes"]
        best_no = platform_prices[best_no_platform]["no"]
        total_cost = best_yes + best_no

        # Only profitable if total cost < 1.0
        if total_cost >= 1.0:
            continue

        # Calculate net profit with fees
        result = net_profit_triangular(
            best_yes,
            best_no,
            best_yes_platform,
            best_no_platform,
        )

        if result["net_profit"] < min_profit:
            continue

        net_profit = result["net_profit"]
        net_roi = (net_profit / total_cost * 100) if total_cost > 0 else 0.0
        platforms_list = sorted(platform_prices.keys())

        opp = {
            "type": "TriangularCross",
            "_layer": 1,  # Layer 1: pure arbitrage
            "market": market_title[:50],
            "prices": f"{best_yes_platform}_Y={best_yes:.3f} {best_no_platform}_N={best_no:.3f}",
            "total_cost": f"${total_cost:.4f}",
            "gross_spread": f"{result['gross_spread']:.4f}",
            "fees": f"${result['fees']:.4f}",
            "net_profit": net_profit,
            "net_roi": f"{net_roi:.2f}%",
            "confidence": min_confidence,
            "_platform_a": best_yes_platform,
            "_platform_b": best_no_platform,
            # Refinement metadata (_refine_triangular_with_clob): platform A
            # holds the YES leg at best_yes, platform B the NO leg at best_no.
            # Without these, refinement fell back to other_price=0 and always
            # treated the Polymarket leg as the YES side.
            "_side_a": "yes",
            "_price_a": best_yes,
            "_side_b": "no",
            "_price_b": best_no,
            "_platforms_checked": platforms_list,
            "_clob_depth": 0,
        }

        # Attach execution metadata for the YES-side platform
        yes_market = platform_prices[best_yes_platform]["market"]
        _attach_exec_metadata(opp, yes_market, best_yes_platform)

        # Attach execution metadata for the NO-side platform.
        # Skip if both sides are the same platform (keys already attached above).
        if best_no_platform != best_yes_platform:
            no_market = platform_prices[best_no_platform]["market"]
            _attach_exec_metadata(opp, no_market, best_no_platform)

        opportunities.append(opp)

    # Stage 2: Refine Polymarket-side opportunities with CLOB ask prices
    opportunities = _refine_triangular_with_clob(opportunities, min_profit)

    # Filter dust and sort by net_profit descending
    opportunities = filter_dust(opportunities)
    opportunities.sort(key=lambda o: o.get("net_profit", 0), reverse=True)

    logger.info("Triangular scan complete: %d opportunities found.", len(opportunities))
    return opportunities


def _refine_triangular_with_clob(opportunities: list[dict], min_profit: float) -> list[dict]:
    """Refine triangular opportunities that include Polymarket using CLOB ask prices.

    For each opportunity where one leg is on Polymarket, fetch the real ask
    price from the CLOB order book and recalculate profit.  Drops candidates
    that are no longer profitable at actual fill prices.

    Args:
        opportunities: Mid-price triangular opportunities.
        min_profit: Minimum net profit threshold.

    Returns:
        Refined list of opportunities with updated prices and depths.
    """
    pm_opps = [o for o in opportunities
               if o.get("_platform_a") == "polymarket" or o.get("_platform_b") == "polymarket"]
    if not pm_opps:
        return opportunities

    logger.info("Refining %d triangular candidates with CLOB ask prices...", len(pm_opps))

    # Pre-fetch CLOB prices in parallel
    clob_cache: dict[tuple, dict] = {}
    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = {}
        for o in pm_opps:
            token_ids = o.get("_token_ids", [])
            if token_ids and len(token_ids) >= 2:
                key = tuple(token_ids[:2])
                if key not in futures:
                    futures[pool.submit(get_clob_prices, {"clobTokenIds": list(key)})] = key
        for future in as_completed(futures):
            key = futures[future]
            try:
                clob_cache[key] = future.result()
            except Exception as e:
                logger.debug("CLOB fetch failed for triangular refinement: %s", e)

    refined = []
    for o in opportunities:
        pa = o.get("_platform_a", "")
        pb = o.get("_platform_b", "")

        # Non-PM opportunities pass through without refinement
        if pa != "polymarket" and pb != "polymarket":
            refined.append(o)
            continue

        token_ids = o.get("_token_ids", [])
        if not token_ids or len(token_ids) < 2:
            logger.debug("Dropping triangular opportunity: Polymarket token IDs unavailable")
            continue

        clob = clob_cache.get(tuple(token_ids[:2]))
        if not clob or clob.get("yes_ask") is None or clob.get("no_ask") is None:
            logger.debug("Dropping triangular opportunity: executable Polymarket asks unavailable")
            continue

        # Determine which side Polymarket is on and use real ask price
        if pa == "polymarket" and pb == "polymarket":
            # Both legs are Polymarket: refine BOTH from the live book.
            # Platform A carries the YES leg, platform B the NO leg —
            # leaving _price_b at its stale Stage-1 NO midpoint would let a
            # false arb survive and execute at an unverified NO price.
            pm_ask = clob["yes_ask"]
            other_price = clob["no_ask"]
            other_platform = pb
            result = net_profit_triangular(pm_ask, other_price, pa, pb)
        elif pa == "polymarket":
            pm_side = o.get("_side_a", "yes") if "_side_a" in o else "yes"
            pm_ask = clob["yes_ask"] if pm_side == "yes" else clob["no_ask"]
            other_price = o.get("_price_b", 0)
            other_platform = pb
            result = net_profit_triangular(
                pm_ask if pm_side == "yes" else other_price,
                other_price if pm_side == "yes" else pm_ask,
                pa, pb,
            )
        else:
            pm_side = o.get("_side_b", "yes") if "_side_b" in o else "yes"
            pm_ask = clob["yes_ask"] if pm_side == "yes" else clob["no_ask"]
            other_price = o.get("_price_a", 0)
            other_platform = pa
            result = net_profit_triangular(
                other_price if pm_side == "no" else pm_ask,
                pm_ask if pm_side == "no" else other_price,
                pa, pb,
            )

        if result["net_profit"] >= min_profit:
            o["gross_spread"] = f"{result['gross_spread']:.4f}"
            o["net_profit"] = result["net_profit"]
            o["fees"] = f"${result['fees']:.4f}"
            total_cost = pm_ask + other_price
            if total_cost > 0:
                o["net_roi"] = f"{result['net_profit'] / total_cost * 100:.2f}%"
            # Persist the LIVE executable prices — execution parses the
            # prices string (and _price_a/_price_b), so leaving the stale
            # Stage-1 mid prices in place would defeat the refinement.
            # Platform A carries the YES leg, platform B the NO leg.
            if pa == "polymarket":
                yes_price, no_price = pm_ask, other_price
            else:
                yes_price, no_price = other_price, pm_ask
            o["prices"] = f"{pa}_Y={yes_price:.3f} {pb}_N={no_price:.3f}"
            o["total_cost"] = f"${total_cost:.4f}"
            o["_price_a"] = yes_price
            o["_price_b"] = no_price
            o["_clob_depth"] = min(
                clob.get("yes_ask_size") or 0,
                clob.get("no_ask_size") or 0,
            )
            refined.append(o)

    dropped = len(opportunities) - len(refined)
    if dropped:
        logger.info("Dropped %d triangular candidates at CLOB ask prices.", dropped)
    return refined


# ---------------------------------------------------------------------------
# Strategy #32: N-Way Exotic Arbitrage (4+ platforms)
# ---------------------------------------------------------------------------


def scan_nway_arb(
    platform_markets: dict[str, list[dict]],
    platform_clients: dict,
    min_profit: float = 0.005,
    min_confidence: float = 0.6,
    max_legs: int = 5,
) -> list[dict]:
    """Scan for N-way cross-platform arbitrage (4+ platforms).

    Extends triangular by exhaustively searching for profitable cycles
    across 4 or more platforms.

    Args:
        platform_markets: Dict mapping platform name to list of market dicts.
        platform_clients: Dict mapping platform name to client instance.
        min_profit: Minimum net profit threshold.
        min_confidence: Minimum confidence tier for fuzzy matching.
        max_legs: Maximum number of platforms to include in a cycle.

    Returns:
        List of opportunity dicts sorted by net_profit descending.
    """
    from config import NWAY_ARB_ENABLED, NWAY_ARB_MAX_LEGS

    if not NWAY_ARB_ENABLED:
        return []

    max_legs = min(max_legs, NWAY_ARB_MAX_LEGS)
    opportunities = []

    active_platforms = {name: mkts for name, mkts in platform_markets.items() if mkts}
    platforms = list(active_platforms.keys())

    if len(platforms) < 4:
        logger.debug("N-way scan requires 4+ platforms, only %d active.", len(platforms))
        return opportunities

    logger.info(
        "N-way scan: searching %d platforms for 4-%d leg cycles: %s",
        len(platforms), max_legs, ", ".join(platforms),
    )

    all_matches = []
    for pa, pb in combinations(platforms, 2):
        markets_a = active_platforms[pa]
        markets_b = active_platforms[pb]
        if not markets_a or not markets_b:
            continue

        if SEMANTIC_MATCHING_ENABLED:
            matched = match_cross_platform_semantic(
                markets_a, markets_b, pa, pb,
                threshold=SEMANTIC_MATCH_THRESHOLD,
                min_confidence=min_confidence,
            )
        else:
            matched = match_cross_platform(
                markets_a, markets_b, pa, pb,
                threshold=FUZZY_MATCH_THRESHOLD,
                min_confidence=min_confidence,
            )
        all_matches.extend(matched)

    if not all_matches:
        return opportunities

    multi_groups = _group_cross_matches(all_matches)

    nway_groups = {k: v for k, v in multi_groups.items() if len(v) >= 4}
    logger.info("Found %d markets appearing on 4+ platforms.", len(nway_groups))

    if not nway_groups:
        return opportunities

    for market_title, platforms_dict in nway_groups.items():
        platform_prices = {}

        for platform_name, market in platforms_dict.items():
            client = platform_clients.get(platform_name)
            yes_price, no_price = _get_market_prices(market, platform_name, client)
            if yes_price is not None and no_price is not None:
                platform_prices[platform_name] = {
                    "yes": yes_price,
                    "no": no_price,
                    "market": market,
                }

        if len(platform_prices) < 4:
            continue

        best_yes_platform = min(platform_prices, key=lambda p: platform_prices[p]["yes"])
        best_no_platform = min(platform_prices, key=lambda p: platform_prices[p]["no"])

        best_yes = platform_prices[best_yes_platform]["yes"]
        best_no = platform_prices[best_no_platform]["no"]
        total_cost = best_yes + best_no

        if total_cost >= 1.0:
            continue

        from fees import net_profit_nway
        platform_price_pairs = [
            (best_yes_platform, best_yes),
            (best_no_platform, best_no),
        ]
        result = net_profit_nway(platform_price_pairs)

        if result["net_profit"] < min_profit:
            continue

        yes_market = platform_prices[best_yes_platform]["market"]
        no_market = platform_prices[best_no_platform]["market"]

        opp = {
            "type": f"NWayArb({len(platform_prices)} platforms)",
            "_layer": 1,
            "market": f"{market_title[:40]}... (N-way arb)",
            "prices": (
                f"{best_yes_platform}_Y={best_yes:.3f} + "
                f"{best_no_platform}_N={best_no:.3f} = {total_cost:.3f}"
            ),
            "total_cost": f"${total_cost:.2f}",
            "net_profit": result["net_profit"],
            "net_roi": result.get("net_roi", 0),
            "confidence": 0.85,
            "_market_key": market_title,
            "_platform_a": best_yes_platform,
            "_platform_b": best_no_platform,
            "_price_a": best_yes,
            "_price_b": best_no,
            "_side_a": "yes",
            "_side_b": "no",
            "_yes_market": yes_market,
            "_no_market": no_market,
            "_all_platforms": list(platform_prices.keys()),
            "_num_platforms": len(platform_prices),
        }

        if best_yes_platform == "polymarket":
            opp["_token_ids"] = _extract_token_ids(yes_market)
        elif best_no_platform == "polymarket":
            opp["_token_ids"] = _extract_token_ids(no_market)

        opportunities.append(opp)

    opportunities = filter_dust(opportunities, min_amount=min_profit)
    opportunities.sort(key=lambda o: o["net_profit"], reverse=True)

    logger.info("N-way scan: found %d opportunities", len(opportunities))
    return opportunities
