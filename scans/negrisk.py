"""NegRisk internal arbitrage scan (multi-outcome sum < $1.00 on Polymarket)."""

import logging
from concurrent.futures import ThreadPoolExecutor, as_completed

from polymarket_api import get_negrisk_events, parse_outcome_prices
from fees import net_profit_negrisk_internal, net_profit_negrisk_no_side
from scans.helpers import _extract_token_ids, _fetch_clob_for_market, _within_resolution_window, filter_dust, _days_to_resolution

logger = logging.getLogger(__name__)


def _refine_negrisk_with_clob(opportunities: list[dict], events_by_title: dict, min_profit: float,
                              price_cache: dict | None = None) -> list[dict]:
    """Stage 2: Re-check NegRisk candidates using CLOB ask prices."""
    if not opportunities:
        return opportunities

    logger.info("Refining %d NegRisk candidates with CLOB ask prices...", len(opportunities))

    # Pre-fetch all CLOB data for NegRisk markets in parallel
    # _fetch_clob_for_market checks the WS cache first before hitting REST.
    all_markets = []
    for opp in opportunities:
        event_key = opp.get("_event_key")
        event = events_by_title.get(event_key) if event_key else None
        if event:
            all_markets.extend(event.get("markets", []))

    clob_cache = {}
    if all_markets:
        with ThreadPoolExecutor(max_workers=4) as pool:
            futures = {pool.submit(_fetch_clob_for_market, m, price_cache): m for m in all_markets}
            for future in as_completed(futures):
                try:
                    market, clob = future.result()
                    m_key = id(market)
                    clob_cache[m_key] = clob
                except Exception as e:
                    logger.debug("CLOB fetch failed for NegRisk refinement: %s", e)

    refined = []
    for opp in opportunities:
        event_key = opp.get("_event_key")
        event = events_by_title.get(event_key) if event_key else None
        if not event:
            logger.debug("Dropping NegRisk opportunity: event unavailable during refinement")
            continue

        markets = event.get("markets", [])
        yes_asks = []
        min_depth = float("inf")
        clob_count = 0
        total_outcomes = len(markets)

        for m in markets:
            clob = clob_cache.get(id(m))
            ask = clob.get("yes_ask") if clob else None
            if isinstance(ask, (int, float)) and 0.0 < ask < 1.0:
                yes_asks.append(ask)
                clob_count += 1
                depth = clob["yes_ask_size"] or 0
                min_depth = min(min_depth, depth)
            else:
                yes_asks.append(None)

        # Remove entries where we couldn't get any price
        if None in yes_asks:
            logger.debug("Dropping NegRisk opportunity: executable YES ask unavailable or invalid")
            continue

        # Every outcome is a required leg; partial coverage is not an arb.
        clob_coverage = clob_count / total_outcomes if total_outcomes > 0 else 0
        if clob_coverage < 1.0:
            continue

        category = event.get("category") or (markets[0].get("category") if markets else None)
        result = net_profit_negrisk_internal(yes_asks, category=category)
        if result["net_profit"] >= min_profit:
            total = sum(yes_asks)
            price_summary = ", ".join(f"{p:.3f}" for p in sorted(yes_asks, reverse=True)[:5])
            if len(yes_asks) > 5:
                price_summary += f"... ({len(yes_asks)} total)"
            opp["prices"] = price_summary
            opp["total_cost"] = f"${total:.4f}"
            opp["gross_spread"] = f"{result['gross_spread']:.4f}"
            opp["fees"] = f"${result['fees']:.4f}"
            opp["net_profit"] = result["net_profit"]
            opp["net_roi"] = f"{result['net_profit'] / total * 100:.2f}%"
            opp["_clob_depth"] = min_depth if min_depth != float("inf") else 0
            opp["_partial_clob"] = clob_count < total_outcomes
            opp["_clob_coverage"] = f"{clob_count}/{total_outcomes}"
            refined.append(opp)
        else:
            mid_profit = opp.get("net_profit", 0)
            logger.info(
                "NegRisk dropped: %s | mid=$%.4f -> ask=$%.4f (min=%.4f, coverage=%d/%d, depth=%.0f)",
                opp.get("market", "?")[:40], mid_profit, result["net_profit"],
                min_profit, clob_count, total_outcomes,
                min_depth if min_depth != float("inf") else 0,
            )

    dropped = len(opportunities) - len(refined)
    if dropped:
        logger.info("Dropped %d NegRisk candidates at CLOB ask prices.", dropped)
    return refined


def scan_negrisk_internal(events: list[dict], min_profit: float,
                          price_cache: dict | None = None) -> list[dict]:
    """Scan for NegRisk arbitrage on Polymarket multi-outcome events."""
    opportunities = []
    events_by_title = {}

    negrisk_events = get_negrisk_events(events)
    logger.info("Scanning %d NegRisk events...", len(negrisk_events))

    filtered_resolution = 0
    for event in negrisk_events:
        markets = event.get("markets", [])
        if len(markets) < 2:
            continue

        # Collect YES prices for each outcome
        yes_prices = []
        outcome_labels = []
        valid = True

        for m in markets:
            if not _within_resolution_window(m, platform="polymarket"):
                filtered_resolution += 1
                valid = False
                break
            prices = parse_outcome_prices(m)
            if not prices:
                valid = False
                break
            # For negRisk markets, first price is the YES price for that outcome
            yes_price = prices[0]
            if not isinstance(yes_price, (int, float)) or not 0.0 < yes_price < 1.0:
                valid = False
                break
            yes_prices.append(yes_price)
            label = m.get("groupItemTitle", m.get("question", "?"))
            outcome_labels.append(label[:20])

        if not valid or not yes_prices:
            continue

        # Sanity check: very low total with many outcomes likely means missing markets
        total_yes = sum(yes_prices)
        if len(yes_prices) >= 5 and total_yes < 0.50:
            event_title = event.get("title", "Unknown")[:60]
            logger.warning("Likely missing outcomes: '%s' (%d outcomes sum to %.3f)",
                          event_title, len(yes_prices), total_yes)

        category = event.get("category") or (markets[0].get("category") if markets else None)
        result = net_profit_negrisk_internal(yes_prices, category=category)

        if result["net_profit"] >= min_profit:
            total = sum(yes_prices)
            price_summary = ", ".join(f"{p:.3f}" for p in sorted(yes_prices, reverse=True)[:5])
            if len(yes_prices) > 5:
                price_summary += f"... ({len(yes_prices)} total)"

            event_key = event.get("id", event.get("title", ""))
            events_by_title[event_key] = event
            # Extract token IDs for each outcome market (YES token = index 0)
            negrisk_token_ids = []
            for m in markets:
                tids = _extract_token_ids(m)
                negrisk_token_ids.append(tids[0] if tids else "")
            opportunities.append({
                "type": f"NegRisk({len(yes_prices)})",
                "_layer": 1,  # Layer 1: pure arbitrage
                "market": event.get("title", "Unknown")[:60],
                "prices": price_summary,
                "total_cost": f"${total:.4f}",
                "gross_spread": f"{result['gross_spread']:.4f}",
                "fees": f"${result['fees']:.4f}",
                "net_profit": result["net_profit"],
                "net_roi": f"{result['net_profit'] / total * 100:.2f}%",
                "volume": f"${sum(float(m.get('volume', 0) or 0) for m in markets):,.0f}",
                "_event_key": event_key,
                "_token_ids": negrisk_token_ids,
                "_days_to_resolution": _days_to_resolution(markets[0], "polymarket"),
            })

    if filtered_resolution:
        logger.info("Filtered %d NegRisk events outside resolution window.", filtered_resolution)

    # Stage 2: Refine with CLOB ask prices
    opportunities = _refine_negrisk_with_clob(opportunities, events_by_title, min_profit, price_cache=price_cache)

    opportunities = filter_dust(opportunities)

    return opportunities


def scan_negrisk_no_side(events: list[dict], min_profit: float,
                         price_cache: dict | None = None) -> list[dict]:
    """Scan for NegRisk NO-side arbitrage (buy all NO when Σ NO < N-1).

    For an N-outcome mutually-exclusive event, buying one NO on every outcome
    guarantees a payout of exactly $(N-1) (only the winning outcome's NO loses).
    Risk-free when Σ(no_ask) < (N-1) - fees - gas.
    """
    from config import NEGRISK_NO_SIDE_ENABLED
    if not NEGRISK_NO_SIDE_ENABLED:
        logger.info("NegRisk NO-side scan skipped: NEGRISK_NO_SIDE_ENABLED=false")
        return []

    opportunities = []
    events_by_title = {}

    negrisk_events = get_negrisk_events(events)
    logger.info("Scanning %d NegRisk events (NO-side)...", len(negrisk_events))

    filtered_resolution = 0
    for event in negrisk_events:
        markets = event.get("markets", [])
        if len(markets) < 2:
            continue

        no_prices = []
        no_token_ids = []
        valid = True
        for m in markets:
            if not _within_resolution_window(m, platform="polymarket"):
                filtered_resolution += 1
                valid = False
                break
            prices = parse_outcome_prices(m)
            if not prices or len(prices) < 2:
                valid = False
                break
            no_price = prices[1]  # index 0 = YES, index 1 = NO
            if no_price <= 0:
                valid = False
                break
            no_prices.append(no_price)
            tids = _extract_token_ids(m)
            # Every leg must be executable: a missing NO token ID means the
            # opportunity would pass scan/refine and then fail at execution.
            if len(tids) <= 1 or not tids[1]:
                valid = False
                break
            no_token_ids.append(tids[1])

        if not valid or len(no_prices) < 2:
            continue

        result = net_profit_negrisk_no_side(no_prices)
        if result["net_profit"] >= min_profit:
            n = len(no_prices)
            total = sum(no_prices)
            price_summary = ", ".join(f"{p:.3f}" for p in sorted(no_prices, reverse=True)[:5])
            if n > 5:
                price_summary += f"... ({n} total)"

            event_key = event.get("id", event.get("title", ""))
            events_by_title[event_key] = event
            opportunities.append({
                "type": f"NegRiskNO({n})",
                "_layer": 1,  # Layer 1: pure arbitrage
                "market": event.get("title", "Unknown")[:60],
                "prices": price_summary,
                "total_cost": f"${total:.4f}",
                "gross_spread": f"{result['gross_spread']:.4f}",
                "fees": f"${result['fees']:.4f}",
                "net_profit": result["net_profit"],
                "net_roi": f"{result['net_profit'] / total * 100:.2f}%",
                "volume": f"${sum(float(m.get('volume', 0) or 0) for m in markets):,.0f}",
                "_event_key": event_key,
                "_token_ids": no_token_ids,      # NO tokens (used by _build_legs)
                "_no_prices": list(no_prices),   # full list for execution (no truncation)
                "_days_to_resolution": _days_to_resolution(markets[0], "polymarket"),
            })

    if filtered_resolution:
        logger.info("Filtered %d NegRisk events outside resolution window (NO-side).", filtered_resolution)

    opportunities = _refine_negrisk_no_side_with_clob(
        opportunities, events_by_title, min_profit, price_cache=price_cache)
    opportunities = filter_dust(opportunities)

    return opportunities


def _refine_negrisk_no_side_with_clob(opportunities: list[dict], events_by_title: dict,
                                      min_profit: float, price_cache: dict | None = None) -> list[dict]:
    """Stage 2: re-check NegRisk NO-side candidates using CLOB NO-ask prices."""
    if not opportunities:
        return opportunities

    logger.info("Refining %d NegRiskNO candidates with CLOB ask prices...", len(opportunities))

    all_markets = []
    for opp in opportunities:
        event = events_by_title.get(opp.get("_event_key"))
        if event:
            all_markets.extend(event.get("markets", []))

    clob_cache = {}
    if all_markets:
        with ThreadPoolExecutor(max_workers=4) as pool:
            futures = {pool.submit(_fetch_clob_for_market, m, price_cache): m for m in all_markets}
            for future in as_completed(futures):
                try:
                    market, clob = future.result()
                    clob_cache[id(market)] = clob
                except Exception as e:
                    logger.debug("CLOB fetch failed for NegRiskNO refinement: %s", e)

    refined = []
    for opp in opportunities:
        event = events_by_title.get(opp.get("_event_key"))
        if not event:
            logger.debug("Dropping NegRiskNO opportunity: event unavailable during refinement")
            continue

        markets = event.get("markets", [])
        no_asks = []
        min_depth = float("inf")
        clob_count = 0
        total_outcomes = len(markets)

        for m in markets:
            clob = clob_cache.get(id(m))
            ask = clob.get("no_ask") if clob else None
            if isinstance(ask, (int, float)) and 0.0 < ask < 1.0:
                no_asks.append(ask)
                clob_count += 1
                depth = clob.get("no_ask_size") or 0
                min_depth = min(min_depth, depth)
            else:
                no_asks.append(None)

        if None in no_asks:
            logger.debug("Dropping NegRiskNO opportunity: executable NO ask unavailable or invalid")
            continue

        clob_coverage = clob_count / total_outcomes if total_outcomes > 0 else 0
        if clob_coverage < 1.0:
            continue

        result = net_profit_negrisk_no_side(no_asks)
        if result["net_profit"] >= min_profit:
            total = sum(no_asks)
            price_summary = ", ".join(f"{p:.3f}" for p in sorted(no_asks, reverse=True)[:5])
            if len(no_asks) > 5:
                price_summary += f"... ({len(no_asks)} total)"
            opp["prices"] = price_summary
            opp["total_cost"] = f"${total:.4f}"
            opp["gross_spread"] = f"{result['gross_spread']:.4f}"
            opp["fees"] = f"${result['fees']:.4f}"
            opp["net_profit"] = result["net_profit"]
            opp["net_roi"] = f"{result['net_profit'] / total * 100:.2f}%"
            opp["_no_prices"] = list(no_asks)
            opp["_clob_depth"] = min_depth if min_depth != float("inf") else 0
            opp["_partial_clob"] = clob_count < total_outcomes
            opp["_clob_coverage"] = f"{clob_count}/{total_outcomes}"
            refined.append(opp)
        else:
            logger.info(
                "NegRiskNO dropped: %s | ask=$%.4f (min=%.4f, coverage=%d/%d)",
                opp.get("market", "?")[:40], result["net_profit"], min_profit,
                clob_count, total_outcomes,
            )

    dropped = len(opportunities) - len(refined)
    if dropped:
        logger.info("Dropped %d NegRiskNO candidates at CLOB ask prices.", dropped)
    return refined
