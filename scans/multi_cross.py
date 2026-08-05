"""Multi-outcome cross-platform arbitrage scan.

Finds opportunities where the same event exists on multiple platforms
(e.g. Polymarket and Kalshi) and the cheapest YES price for each outcome
across all platforms sums to less than $1.00 minus fees.

Example: 3-outcome event "Who wins the race?"
  Polymarket: A=0.35, B=0.30, C=0.20   (sum = 0.85)
  Kalshi:     A=0.40, B=0.25, C=0.22   (sum = 0.87)
  Best-of:    A=0.35(PM), B=0.25(K), C=0.20(PM)  (sum = 0.80 -> arb!)
"""

import logging
from concurrent.futures import ThreadPoolExecutor, as_completed

from polymarket_api import get_negrisk_events, parse_outcome_prices
from fees import net_profit_multi_cross
from matcher import normalize_title, _extract_entities
from scans.helpers import (
    _extract_token_ids,
    _fetch_clob_for_market,
    _within_resolution_window,
    _days_to_resolution,
    filter_dust,
)

logger = logging.getLogger(__name__)

# Minimum fuzzy overlap ratio to consider two event titles a match
_EVENT_MATCH_THRESHOLD = 0.70

# Synonym map for outcome labels that differ across platforms
_OUTCOME_SYNONYMS = {
    "tie": "draw",
    "tied": "draw",
}


def _normalize_outcome_label(label: str) -> str:
    """Normalize an outcome label with synonym replacement."""
    norm = normalize_title(label)
    for old, new in _OUTCOME_SYNONYMS.items():
        norm = norm.replace(old, new)
    return norm


# ---------------------------------------------------------------------------
# Event-level matching
# ---------------------------------------------------------------------------

def _match_events_by_title(
    pm_events: list[dict],
    kalshi_events: dict[str, list[dict]],
    kalshi_event_titles: dict[str, str],
) -> list[tuple[dict, str, list[dict]]]:
    """Match Polymarket events to Kalshi events by normalised title similarity.

    Uses entity overlap to avoid false positives (e.g. two unrelated events
    about different races).

    Args:
        pm_events: Polymarket neg-risk events (from get_negrisk_events).
        kalshi_events: Kalshi markets grouped by event_ticker.
        kalshi_event_titles: Mapping event_ticker -> event title.

    Returns:
        List of (pm_event, kalshi_event_ticker, kalshi_markets) tuples.
    """
    try:
        from thefuzz import fuzz, utils as fuzz_utils
    except ImportError:
        try:
            from fuzzywuzzy import fuzz, utils as fuzz_utils
        except ImportError:
            logger.warning("thefuzz/fuzzywuzzy not installed — multi-cross matching disabled.")
            return []

    try:
        from rapidfuzz import fuzz as rapid_fuzz
        from rapidfuzz import process as rapid_process
    except ImportError:
        rapid_fuzz = None
        rapid_process = None

    matches = []
    # Build normalised title -> ticker index for Kalshi
    kalshi_index: list[tuple[str, str, set[str]]] = []
    for kticker, ktitle in kalshi_event_titles.items():
        norm = normalize_title(ktitle)
        entities = _extract_entities(norm)
        kalshi_index.append((kticker, norm, entities))
    kalshi_norms = [entry[1] for entry in kalshi_index]
    kalshi_score_titles = [
        fuzz_utils.full_process(title, force_ascii=True)
        for title in kalshi_norms
    ]

    for pm_event in pm_events:
        pm_title = pm_event.get("title", "")
        pm_norm = normalize_title(pm_title)
        pm_entities = _extract_entities(pm_norm)

        best_ticker = None
        best_score = 0

        # rapidfuzz performs the full-universe score pass in native code. The
        # previous Python loop made millions of scorer calls per production
        # scan. Keep the same 70-point cutoff, entity gate, combined score, and
        # original-index tie break so matching behavior does not change.
        if rapid_process is not None:
            scored_candidates = (
                (index, int(round(score)))
                for _, score, index in rapid_process.extract_iter(
                    fuzz_utils.full_process(pm_norm, force_ascii=True),
                    kalshi_score_titles,
                    scorer=rapid_fuzz.token_sort_ratio,
                    # thefuzz rounds native scores to integers; include values
                    # that round up to the existing 70-point threshold.
                    score_cutoff=(_EVENT_MATCH_THRESHOLD * 100) - 0.5,
                )
            )
        else:
            scored_candidates = (
                (index, fuzz.token_sort_ratio(pm_norm, knorm))
                for index, (_, knorm, _) in enumerate(kalshi_index)
            )

        best_index = len(kalshi_index)
        for index, score in scored_candidates:
            kticker, _, kentities = kalshi_index[index]
            entity_overlap = len(pm_entities & kentities)

            # Require both fuzzy similarity and entity overlap
            if score >= _EVENT_MATCH_THRESHOLD * 100 and entity_overlap >= 1:
                combined = score + entity_overlap * 5
                if combined > best_score or (combined == best_score and index < best_index):
                    best_score = combined
                    best_index = index
                    best_ticker = kticker

        if best_ticker and best_ticker in kalshi_events:
            matches.append((pm_event, best_ticker, kalshi_events[best_ticker]))

    logger.info("Matched %d multi-outcome events across platforms.", len(matches))
    return matches


# ---------------------------------------------------------------------------
# Outcome-level matching within a matched event
# ---------------------------------------------------------------------------

def _match_outcomes(
    pm_markets: list[dict],
    kalshi_markets: list[dict],
    kalshi_client=None,
) -> list[dict]:
    """Match individual outcomes within a matched event pair.

    For each PM outcome, tries to find the corresponding Kalshi market
    by normalised title/question similarity.

    Returns:
        List of outcome dicts, each with:
            "label": str,
            "pm_price": float | None,
            "kalshi_price": float | None,
            "pm_market": dict | None,
            "kalshi_market": dict | None,
            "best_price": float,
            "best_platform": str,
    """
    try:
        from thefuzz import fuzz
    except ImportError:
        try:
            from fuzzywuzzy import fuzz
        except ImportError:
            return []

    outcomes = []

    # Build Kalshi outcome index
    # Use yes_sub_title (e.g. "Frosinone", "Bari", "Tie") when available —
    # Kalshi multi-outcome markets share the same title (e.g. "Frosinone vs
    # Bari Winner?") with per-outcome labels only in yes_sub_title.
    kalshi_by_norm: list[tuple[dict, str, float]] = []
    for km in kalshi_markets:
        sub_title = km.get("yes_sub_title", "")
        title = sub_title if sub_title else km.get("title", km.get("ticker", ""))
        norm = _normalize_outcome_label(title)
        if kalshi_client:
            yes_price, _ = kalshi_client.get_market_price(km)
        else:
            yes_price = km.get("yes_price") or km.get("last_price")
        if yes_price is not None and yes_price > 0:
            kalshi_by_norm.append((km, norm, yes_price))

    kalshi_matched = set()

    for pm_m in pm_markets:
        label = pm_m.get("groupItemTitle", pm_m.get("question", "?"))
        pm_norm = _normalize_outcome_label(label)
        prices = parse_outcome_prices(pm_m)
        pm_yes = prices[0] if prices else None

        # Find best matching Kalshi outcome
        best_idx = None
        best_score = 0
        for i, (km, knorm, kprice) in enumerate(kalshi_by_norm):
            if i in kalshi_matched:
                continue
            score = fuzz.token_sort_ratio(pm_norm, knorm)
            if score > best_score:
                best_score = score
                best_idx = i

        kalshi_market = None
        kalshi_yes = None
        if best_idx is not None and best_score >= 60:
            kalshi_matched.add(best_idx)
            kalshi_market = kalshi_by_norm[best_idx][0]
            kalshi_yes = kalshi_by_norm[best_idx][2]

        # Pick cheapest platform for this outcome
        if pm_yes and kalshi_yes:
            if pm_yes <= kalshi_yes:
                best_price, best_platform = pm_yes, "polymarket"
            else:
                best_price, best_platform = kalshi_yes, "kalshi"
        elif pm_yes:
            best_price, best_platform = pm_yes, "polymarket"
        elif kalshi_yes:
            best_price, best_platform = kalshi_yes, "kalshi"
        else:
            continue

        outcomes.append({
            "label": label[:30],
            "pm_price": pm_yes,
            "kalshi_price": kalshi_yes,
            "pm_market": pm_m,
            "kalshi_market": kalshi_market,
            "best_price": best_price,
            "best_platform": best_platform,
        })

    return outcomes


# ---------------------------------------------------------------------------
# Main scan function
# ---------------------------------------------------------------------------

def scan_multi_cross(
    events: list[dict],
    kalshi_client=None,
    min_profit: float = 0.01,
    kalshi_data: tuple | None = None,
    price_cache: dict | None = None,
) -> list[dict]:
    """Scan for multi-outcome cross-platform arbitrage opportunities.

    Compares multi-outcome events on Polymarket (neg-risk) with corresponding
    events on Kalshi, picking the cheapest YES price for each outcome across
    both platforms.

    Args:
        events: Polymarket events from fetch_events().
        kalshi_client: Authenticated KalshiClient (or None).
        min_profit: Minimum net profit threshold.
        kalshi_data: Pre-fetched Kalshi data tuple (markets, by_event, titles).
        price_cache: WebSocket price cache.

    Returns:
        List of opportunity dicts.
    """
    opportunities = []

    # Get Polymarket multi-outcome events
    pm_negrisk = get_negrisk_events(events)
    if not pm_negrisk:
        logger.info("No Polymarket NegRisk events found for multi-cross scan.")
        return opportunities

    # Get Kalshi multi-outcome events
    if kalshi_data:
        kalshi_events, kalshi_by_event, kalshi_event_titles = kalshi_data
    elif kalshi_client:
        from .kalshi import _fetch_kalshi_data
        kalshi_events, kalshi_by_event, kalshi_event_titles = _fetch_kalshi_data(kalshi_client)
    else:
        logger.info("No Kalshi credentials — multi-cross scan requires at least 2 platforms.")
        return opportunities

    if not kalshi_by_event:
        logger.info("No Kalshi events found for multi-cross matching.")
        return opportunities

    # Complete-set gate: only mutually-exclusive Kalshi events qualify as
    # multi-outcome complete sets (see scan_kalshi_multi for the rationale —
    # non-exclusive market ladders produced phantom arbs in production).
    me_by_event = {e.get("event_ticker"): e.get("mutually_exclusive") for e in kalshi_events}

    # Filter Kalshi to multi-outcome, mutually-exclusive events only
    kalshi_multi = {
        k: v for k, v in kalshi_by_event.items()
        if len(v) >= 2 and me_by_event.get(k) is True
    }

    # Match events across platforms
    matched_events = _match_events_by_title(pm_negrisk, kalshi_multi, kalshi_event_titles)
    if not matched_events:
        logger.info("No multi-outcome events matched across platforms.")
        return opportunities

    filtered_resolution = 0

    for pm_event, kticker, kalshi_markets in matched_events:
        pm_markets = pm_event.get("markets", [])

        # Check resolution window on PM side
        skip = False
        for m in pm_markets:
            if not _within_resolution_window(m, platform="polymarket"):
                filtered_resolution += 1
                skip = True
                break
        if skip:
            continue

        # Match individual outcomes
        outcomes = _match_outcomes(pm_markets, kalshi_markets, kalshi_client)

        # Require ALL Polymarket outcomes to be matched — if any outcome is
        # dropped, the total cost is artificially low and produces false arbs
        if len(outcomes) != len(pm_markets):
            logger.debug(
                "MultiCross skipped: '%s' matched %d/%d outcomes",
                pm_event.get("title", "?")[:40], len(outcomes), len(pm_markets),
            )
            continue
        if len(outcomes) < 2:
            continue

        # Sanity check: sum of cheapest per-outcome should be close to 1.0
        # (since exactly one outcome wins, the fair sum is ~1.0)
        # With the all-outcomes-matched check above, this is a secondary filter
        total_cost = sum(o["best_price"] for o in outcomes)
        if total_cost < 0.50:
            logger.warning(
                "Likely missing outcomes in multi-cross: '%s' (%d matched, sum=%.3f)",
                pm_event.get("title", "?")[:60], len(outcomes), total_cost,
            )
            continue

        # Compare: is cross-platform cheaper than single-platform?
        # Require ALL outcomes to have prices on each platform being compared
        pm_prices = [o["pm_price"] for o in outcomes]
        kalshi_prices = [o["kalshi_price"] for o in outcomes]
        if all(p is not None for p in pm_prices):
            pm_total = sum(pm_prices)
        else:
            pm_total = float("inf")
        if all(p is not None for p in kalshi_prices):
            kalshi_total = sum(kalshi_prices)
        else:
            kalshi_total = float("inf")
        single_best = min(pm_total, kalshi_total)

        # Only report if cross-platform mix is actually cheaper than both single-platforms
        if total_cost >= single_best:
            continue

        outcome_prices = [o["best_price"] for o in outcomes]
        outcome_platforms = [o["best_platform"] for o in outcomes]

        result = net_profit_multi_cross(outcome_prices, outcome_platforms)

        if result["net_profit"] >= min_profit:
            n = len(outcomes)
            price_parts = []
            for o in sorted(outcomes, key=lambda x: -x["best_price"])[:5]:
                plat_abbr = "PM" if o["best_platform"] == "polymarket" else "K"
                price_parts.append(f"{plat_abbr}:{o['best_price']:.3f}")
            price_summary = ", ".join(price_parts)
            if n > 5:
                price_summary += f"... ({n} total)"

            # Build execution legs
            outcome_legs = []
            for o in outcomes:
                leg = {
                    "platform": o["best_platform"],
                    "outcome": o["label"],
                    "price": o["best_price"],
                    "side": "yes",
                }
                if o["best_platform"] == "polymarket" and o["pm_market"]:
                    tids = _extract_token_ids(o["pm_market"])
                    leg["_token_id"] = tids[0] if tids else ""
                elif o["best_platform"] == "kalshi" and o["kalshi_market"]:
                    leg["_kalshi_ticker"] = o["kalshi_market"].get("ticker", "")
                outcome_legs.append(leg)

            event_title = pm_event.get("title", "Unknown")
            opportunities.append({
                "type": f"MultiCross({n})",
                "_layer": 1,  # Layer 1: pure arbitrage
                "market": event_title[:60],
                "prices": price_summary,
                "total_cost": f"${total_cost:.4f}",
                "gross_spread": f"{result['gross_spread']:.4f}",
                "fees": f"${result['fees']:.4f}",
                "net_profit": result["net_profit"],
                "net_roi": f"{result['net_profit'] / total_cost * 100:.2f}%",
                "volume": f"${sum(float(m.get('volume', 0) or 0) for m in pm_markets):,.0f}",
                "_outcome_legs": outcome_legs,
                "_event_key": pm_event.get("id", event_title),
                "_kalshi_event_ticker": kticker,
                "_days_to_resolution": _days_to_resolution(pm_markets[0], "polymarket"),
            })

    if filtered_resolution:
        logger.info("Filtered %d multi-cross events outside resolution window.",
                     filtered_resolution)

    # Stage 2: Refine with CLOB ask prices for Polymarket legs
    opportunities = _refine_multi_cross_with_clob(opportunities, min_profit,
                                                   price_cache=price_cache)

    opportunities = filter_dust(opportunities)

    return opportunities


# ---------------------------------------------------------------------------
# Stage 2: CLOB refinement
# ---------------------------------------------------------------------------

def _refine_multi_cross_with_clob(
    opportunities: list[dict],
    min_profit: float,
    price_cache: dict | None = None,
) -> list[dict]:
    """Re-check MultiCross candidates using CLOB ask prices for PM legs.

    Fetches real ask prices from Polymarket CLOB for each PM-side outcome.
    Kalshi legs keep their mid-prices (Kalshi is a central limit order book
    with instant FOK fills).
    """
    if not opportunities:
        return opportunities

    logger.info("Refining %d MultiCross candidates with CLOB ask prices...",
                len(opportunities))

    refined = []
    for opp in opportunities:
        legs = opp.get("_outcome_legs", [])
        new_prices = []
        new_platforms = []
        clob_ok = True

        for leg in legs:
            if leg["platform"] == "polymarket" and leg.get("_token_id"):
                # Multi-cross legs have a single YES token per outcome.
                # Check WS price cache directly (not _fetch_clob_for_market
                # which expects a full market dict with YES+NO token pair).
                token_id = leg["_token_id"]
                cached = price_cache.get(("polymarket", token_id)) if price_cache else None
                if cached and cached.get("best_ask") is not None:
                    new_prices.append(cached["best_ask"])
                    new_platforms.append("polymarket")
                    leg["price"] = cached["best_ask"]
                else:
                    # Fallback to mid-price
                    new_prices.append(leg["price"])
                    new_platforms.append(leg["platform"])
            else:
                new_prices.append(leg["price"])
                new_platforms.append(leg["platform"])

        if not clob_ok:
            continue

        total_cost = sum(new_prices)
        if total_cost >= 1.0:
            continue

        result = net_profit_multi_cross(new_prices, new_platforms)
        if result["net_profit"] >= min_profit:
            opp["net_profit"] = result["net_profit"]
            opp["total_cost"] = f"${total_cost:.4f}"
            opp["gross_spread"] = f"{result['gross_spread']:.4f}"
            opp["fees"] = f"${result['fees']:.4f}"
            opp["net_roi"] = f"{result['net_profit'] / total_cost * 100:.2f}%"
            refined.append(opp)
        else:
            logger.debug("MultiCross dropped after CLOB refinement: %s (profit $%.4f)",
                        opp.get("market", "?")[:30], result["net_profit"])

    logger.info("MultiCross: %d -> %d after CLOB refinement.", len(opportunities), len(refined))
    return refined
