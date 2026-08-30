"""Cross-platform arbitrage scans (PM vs Kalshi, and all-platform pairs)."""

import logging
from concurrent.futures import ThreadPoolExecutor, as_completed

from polymarket_api import get_binary_markets, get_clob_prices, parse_outcome_prices
from kalshi_api import KalshiClient
from matcher import (match_markets_to_events, match_markets_to_events_semantic,
                     match_cross_platform, match_cross_platform_semantic, detect_inverted)
from config import FUZZY_MATCH_THRESHOLD, SEMANTIC_MATCHING_ENABLED, SEMANTIC_MATCH_THRESHOLD
from fees import (
    net_profit_cross_platform,
    net_profit_cross_betfair,
    net_profit_cross_smarkets,
    net_profit_cross_sxbet,
    net_profit_cross_matchbook,
    net_profit_cross_gemini,
    net_profit_cross_ibkr,
    net_profit_cross_generic,
    find_lowest_fee_path,
)
from scans.helpers import _extract_token_ids, _fetch_clob_for_market, _parallel_fetch_kalshi, _within_resolution_window, filter_dust, _days_to_resolution
from near_miss_cache import get_global_cache

logger = logging.getLogger(__name__)

# All supported platforms for cross-platform pairing
_ALL_PLATFORMS = [
    "polymarket", "kalshi", "betfair", "smarkets",
    "sxbet", "matchbook", "gemini", "ibkr",
]


def _make_cross_fee(platform_a: str, platform_b: str):
    """Return a 4-arg fee callable that delegates to net_profit_cross_generic."""
    def _fee_func(price_a: float, price_b: float, side_a: str, side_b: str) -> dict:
        return net_profit_cross_generic(price_a, price_b, side_a, side_b,
                                        platform_a=platform_a, platform_b=platform_b)
    return _fee_func


# Fee function lookup for cross-platform pairs
_CROSS_FEE_FUNCS = {
    ("polymarket", "kalshi"): net_profit_cross_platform,
    ("polymarket", "betfair"): net_profit_cross_betfair,
    ("polymarket", "smarkets"): net_profit_cross_smarkets,
    ("polymarket", "sxbet"): net_profit_cross_sxbet,
    ("polymarket", "matchbook"): net_profit_cross_matchbook,
    ("polymarket", "gemini"): net_profit_cross_gemini,
    ("polymarket", "ibkr"): net_profit_cross_ibkr,
}

# Auto-populate remaining C(8,2) - 7 = 21 non-polymarket pairs
for _i, _pa in enumerate(_ALL_PLATFORMS):
    for _pb in _ALL_PLATFORMS[_i + 1:]:
        if (_pa, _pb) not in _CROSS_FEE_FUNCS:
            _CROSS_FEE_FUNCS[(_pa, _pb)] = _make_cross_fee(_pa, _pb)


def _refine_cross_with_clob(opportunities: list[dict], markets_by_key: dict, min_profit: float,
                            price_cache: dict | None = None) -> list[dict]:
    """Stage 2: Re-check cross-platform candidates using CLOB ask prices for Polymarket side."""
    if not opportunities:
        return opportunities

    logger.info("Refining %d cross-platform candidates with CLOB ask prices...", len(opportunities))

    # Pre-fetch CLOB prices in parallel (WS cache checked inside _fetch_clob_for_market)
    fetch_tasks = {}  # market_key -> market
    for opp in opportunities:
        market_key = opp.get("_market_key")
        market = markets_by_key.get(market_key) if market_key else None
        if market and market_key not in fetch_tasks:
            fetch_tasks[market_key] = market

    clob_results = {}
    if fetch_tasks:
        with ThreadPoolExecutor(max_workers=4) as pool:
            futures = {pool.submit(_fetch_clob_for_market, m, price_cache): mk
                       for mk, m in fetch_tasks.items()}
            for future in as_completed(futures):
                mk = futures[future]
                try:
                    _, clob = future.result()
                    clob_results[mk] = clob
                except Exception as e:
                    logger.debug("CLOB fetch failed for %s: %s", mk, e)

    # FAIL-CLOSED (audit #77 round 2): opps whose CLOB verification cannot be
    # completed (missing market / book / ask+bid / Kalshi prices) are DROPPED,
    # not passed through with stale mid-price profits.
    refined = []
    for opp in opportunities:
        market_key = opp.get("_market_key")
        market = markets_by_key.get(market_key) if market_key else None
        if not market:
            logger.debug("Cross dropped (fail-closed): no market for key %s", market_key)
            continue

        clob = clob_results.get(market_key)
        if not clob:
            logger.debug("Cross dropped (fail-closed): no CLOB book for %s", market_key)
            continue

        # Use ask price, fall back to bid + 0.01 if ask is missing. A
        # malformed book (missing keys entirely) must drop the opportunity,
        # not raise — .get() so absent fields behave like empty sides.
        pm_yes = clob.get("yes_ask")
        pm_no = clob.get("no_ask")
        pm_yes_depth = clob.get("yes_ask_size")
        pm_no_depth = clob.get("no_ask_size")
        partial = False
        if pm_yes is None and clob.get("yes_bid") is not None:
            pm_yes = clob["yes_bid"] + 0.01
            pm_yes_depth = clob.get("yes_bid_size")
            partial = True
        if pm_no is None and clob.get("no_bid") is not None:
            pm_no = clob["no_bid"] + 0.01
            pm_no_depth = clob.get("no_bid_size")
            partial = True
        if (pm_yes is None or pm_no is None
                or pm_yes_depth is None or pm_no_depth is None):
            logger.debug("Cross dropped (fail-closed): empty book sides for %s", market_key)
            continue
        k_yes = opp.get("_kalshi_yes")
        k_no = opp.get("_kalshi_no")

        if k_yes is None or k_no is None:
            logger.debug("Cross dropped (fail-closed): missing Kalshi prices for %s", market_key)
            continue

        result1 = net_profit_cross_platform(pm_yes, k_no, "yes", "no")
        result2 = net_profit_cross_platform(pm_no, k_yes, "no", "yes")
        best = result1 if result1["net_profit"] > result2["net_profit"] else result2

        mid_profit = opp.get("net_profit", 0)
        if best["net_profit"] >= min_profit:
            if best is result1:
                total_cost = pm_yes + k_no
                opp["prices"] = f"PM_Y={pm_yes:.3f} K_N={k_no:.3f}"
            else:
                total_cost = pm_no + k_yes
                opp["prices"] = f"PM_N={pm_no:.3f} K_Y={k_yes:.3f}"
            opp["total_cost"] = f"${total_cost:.4f}"
            opp["gross_spread"] = f"{best['gross_spread']:.4f}"
            opp["fees"] = f"${best['fees']:.4f}"
            opp["net_profit"] = best["net_profit"]
            opp["net_roi"] = f"{best['net_profit'] / total_cost * 100:.2f}%"
            selected_pm_depth = pm_yes_depth if best is result1 else pm_no_depth
            opp["_clob_depth"] = selected_pm_depth or 0
            if partial:
                opp["_partial_clob"] = True
            refined.append(opp)
        else:
            logger.info(
                "Cross dropped: %s | mid=$%.4f -> ask=$%.4f (min=%.4f) | "
                "PM_Y=%.3f PM_N=%.3f K_Y=%.3f K_N=%.3f | "
                "r1=$%.4f r2=$%.4f fees=$%.4f",
                opp.get("market", "?")[:50], mid_profit, best["net_profit"],
                min_profit, pm_yes, pm_no, k_yes, k_no,
                result1["net_profit"], result2["net_profit"], best["fees"],
            )
            # Strategy #9: capture near-misses for fee-promo re-scoring. The
            # band is read at call time so config reloads take effect on the
            # next scan without restart.
            try:
                from config import PROMO_NEAR_MISS_BAND
                gap = min_profit - best["net_profit"]
                if 0 <= gap <= PROMO_NEAR_MISS_BAND:
                    if best is result1:
                        side_a, side_b = "yes", "no"
                        price_a, price_b = pm_yes, k_no
                    else:
                        side_a, side_b = "no", "yes"
                        price_a, price_b = pm_no, k_yes
                    near_miss_entry = {
                        **opp,
                        "_market_key": market_key,
                        "_platform_a": "polymarket",
                        "_platform_b": "kalshi",
                        "_price_a": price_a,
                        "_price_b": price_b,
                        "_side_a": side_a,
                        "_side_b": side_b,
                        "net_profit": best["net_profit"],
                    }
                    get_global_cache().add(near_miss_entry, gap_to_threshold=gap)
            except Exception as exc:
                logger.debug("near-miss capture failed for %s: %s", market_key, exc)

    dropped = len(opportunities) - len(refined)
    if dropped:
        logger.info("Dropped %d cross-platform candidates at CLOB ask prices.", dropped)
    return refined


def scan_cross_platform(
    poly_markets: list[dict],
    kalshi_client: KalshiClient | None,
    min_profit: float,
    kalshi_markets_by_event: dict | None = None,
    min_confidence: str = "LOW",
    kalshi_events_preloaded: list[dict] | None = None,
    price_cache: dict | None = None,
) -> list[dict]:
    """Scan for cross-platform arbitrage between Polymarket and Kalshi."""
    opportunities = []
    markets_by_key = {}

    if not kalshi_client:
        logger.info("Kalshi credentials not configured. Skipping cross-platform scan.")
        return opportunities

    kalshi_events = kalshi_events_preloaded
    if kalshi_events is None:
        logger.info("Fetching Kalshi events...")
        kalshi_events = kalshi_client.fetch_all_events()
    if not kalshi_events:
        logger.warning("No Kalshi events fetched.")
        return opportunities

    # Filter Polymarket to binary markets only for cross-platform matching
    binary_poly = get_binary_markets(poly_markets)
    logger.info("Matching %d Polymarket binary markets vs %d Kalshi events...", len(binary_poly), len(kalshi_events))

    if SEMANTIC_MATCHING_ENABLED:
        matched = match_markets_to_events_semantic(
            binary_poly, kalshi_events,
            threshold=SEMANTIC_MATCH_THRESHOLD, min_confidence=min_confidence,
        )
    else:
        matched = match_markets_to_events(
            binary_poly, kalshi_events,
            threshold=FUZZY_MATCH_THRESHOLD, min_confidence=min_confidence,
        )
    logger.info("Found %d event matches. Fetching Kalshi market prices...", len(matched))

    # Pre-fetch Kalshi markets in parallel if not already done
    if kalshi_markets_by_event is None:
        tickers = [m["kalshi_event"].get("event_ticker", "") for m in matched if m["kalshi_event"].get("event_ticker")]
        kalshi_markets_by_event = _parallel_fetch_kalshi(kalshi_client, tickers)

    filtered_resolution = 0
    for i, match in enumerate(matched):
        pm = match["polymarket"]
        ke = match["kalshi_event"]

        if not _within_resolution_window(pm, platform="polymarket"):
            filtered_resolution += 1
            continue

        # Get Polymarket prices
        pm_prices = parse_outcome_prices(pm)
        if not pm_prices or len(pm_prices) != 2:
            continue
        pm_yes, pm_no = pm_prices[0], pm_prices[1]

        # Use pre-fetched Kalshi markets
        event_ticker = ke.get("event_ticker", "")
        k_markets = kalshi_markets_by_event.get(event_ticker, [])
        if not k_markets:
            continue

        # Find the best opportunity across all Kalshi sub-markets in this event
        best_opp = None

        for km in k_markets:
            if not _within_resolution_window(km, platform="kalshi"):
                continue
            k_yes, k_no = kalshi_client.get_market_price(km)
            if k_yes is None or k_no is None:
                continue

            # Check for inversion
            pm_title = pm.get("question", pm.get("title", ""))
            k_title = km.get("title", "")
            inverted = detect_inverted(pm_title, k_title)
            if inverted:
                k_yes, k_no = k_no, k_yes

            # Strategy 1: Buy PM YES + Kalshi NO
            result1 = net_profit_cross_platform(pm_yes, k_no, "yes", "no")
            # Strategy 2: Buy PM NO + Kalshi YES
            result2 = net_profit_cross_platform(pm_no, k_yes, "no", "yes")

            best = result1 if result1["net_profit"] > result2["net_profit"] else result2
            if best is result1:
                strategy = "PM_YES + K_NO"
                total_cost = pm_yes + k_no
                prices_str = f"PM_Y={pm_yes:.3f} K_N={k_no:.3f}"
                best_k_yes, best_k_no = k_yes, k_no
            else:
                strategy = "PM_NO + K_YES"
                total_cost = pm_no + k_yes
                prices_str = f"PM_N={pm_no:.3f} K_Y={k_yes:.3f}"
                best_k_yes, best_k_no = k_yes, k_no

            if best["net_profit"] >= min_profit and total_cost > 0:
                if best_opp is None or best["net_profit"] > best_opp["net_profit"]:
                    sim = match["similarity"]
                    market_key = pm.get("conditionId", pm.get("question", ""))
                    markets_by_key[market_key] = pm
                    pm_token_ids = _extract_token_ids(pm)
                    best_opp = {
                        "type": f"Cross({strategy})",
                        "_layer": 1,  # Layer 1: pure arbitrage
                        "market": pm_title[:50],
                        "kalshi": k_title[:50],
                        "match": f"{sim}%",
                        "prices": prices_str,
                        "total_cost": f"${total_cost:.4f}",
                        "gross_spread": f"{best['gross_spread']:.4f}",
                        "fees": f"${best['fees']:.4f}",
                        "net_profit": best["net_profit"],
                        "net_roi": f"{best['net_profit'] / total_cost * 100:.2f}%",
                        "volume": f"${float(pm.get('volume', 0) or 0):,.0f}",
                        "_market_key": market_key,
                        "_kalshi_yes": best_k_yes,
                        "_kalshi_no": best_k_no,
                        "_pair_inverted": inverted,
                        "_kalshi_ticker": km.get("ticker", ""),
                        "_token_ids": pm_token_ids,
                        "confidence": match.get("confidence", "LOW"),
                        "_days_to_resolution": _days_to_resolution(pm, "polymarket"),
                    }

        if best_opp:
            opportunities.append(best_opp)
            logger.info(
                "Cross candidate (mid): %s | %s | profit=$%.4f roi=%s",
                best_opp.get("market", "?")[:50], best_opp.get("prices", ""),
                best_opp.get("net_profit", 0), best_opp.get("net_roi", "?"),
            )

        if (i + 1) % 50 == 0:
            logger.info("Processed %d/%d matches...", i + 1, len(matched))

    if filtered_resolution:
        logger.info("Filtered %d/%d cross-platform matches outside resolution window.", filtered_resolution, len(matched))

    # Stage 2: Refine with CLOB ask prices
    opportunities = _refine_cross_with_clob(opportunities, markets_by_key, min_profit, price_cache=price_cache)

    # Attach fee path hints — scan-time metadata for executor re-validation
    for opp in opportunities:
        prices_str = opp.get("prices", "")
        k_yes = opp.get("_kalshi_yes")
        k_no = opp.get("_kalshi_no")
        if k_yes is None or k_no is None:
            continue
        # Parse Polymarket side from prices string (e.g. "PM_Y=0.400 K_N=0.550")
        pm_yes = pm_no = None
        for part in prices_str.split():
            if "=" not in part:
                continue
            key, val = part.split("=", 1)
            try:
                v = float(val)
            except ValueError:
                continue
            if key == "PM_Y":
                pm_yes = v
            elif key == "PM_N":
                pm_no = v
        # Reconstruct both sides: PM_YES and PM_NO are complementary at mid-price level
        # Use explicit values where available; approximate the missing side
        if pm_yes is not None and pm_no is None:
            pm_no = round(1.0 - pm_yes, 3)
        elif pm_no is not None and pm_yes is None:
            pm_yes = round(1.0 - pm_no, 3)
        if pm_yes is None or pm_no is None:
            continue
        fee_path = find_lowest_fee_path(
            ["polymarket", "kalshi"],
            {"polymarket": pm_yes, "kalshi": k_yes},
            {"polymarket": pm_no, "kalshi": k_no},
        )
        if fee_path:
            opp["_fee_path"] = fee_path

    opportunities = filter_dust(opportunities)

    return opportunities


def _attach_exec_metadata(opp: dict, market: dict, platform: str, suffix: str):
    """Attach platform-specific execution metadata to a cross-all opportunity."""
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


def scan_cross_all(
    poly_markets: list[dict],
    platform_clients: dict,
    min_profit: float,
    min_confidence: str = "LOW",
    price_cache: dict | None = None,
) -> list[dict]:
    """Scan for cross-platform arbitrage across all platform pairs."""
    opportunities = []
    binary_poly = get_binary_markets(poly_markets) if poly_markets else []

    # Build list of all platform market sets
    platform_markets = {"polymarket": binary_poly}
    for name, (client, markets) in platform_clients.items():
        if markets:
            platform_markets[name] = markets

    platforms = list(platform_markets.keys())
    logger.info("Cross-all: matching across %d platforms: %s", len(platforms), ", ".join(platforms))

    # Compare all N*(N-1)/2 pairs
    for i, pa in enumerate(platforms):
        for pb in platforms[i + 1:]:
            markets_a = platform_markets[pa]
            markets_b = platform_markets[pb]
            if not markets_a or not markets_b:
                continue

            logger.info("Matching %s (%d) vs %s (%d)...", pa, len(markets_a), pb, len(markets_b))
            if SEMANTIC_MATCHING_ENABLED:
                matched = match_cross_platform_semantic(
                    markets_a, markets_b, pa, pb,
                    threshold=SEMANTIC_MATCH_THRESHOLD, min_confidence=min_confidence,
                )
            else:
                matched = match_cross_platform(
                    markets_a, markets_b, pa, pb,
                    threshold=FUZZY_MATCH_THRESHOLD, min_confidence=min_confidence,
                )
            logger.info("Found %d matches between %s and %s", len(matched), pa, pb)

            # Determine fee function
            fee_key = (pa, pb)
            if fee_key not in _CROSS_FEE_FUNCS:
                fee_key = (pb, pa)
            fee_func = _CROSS_FEE_FUNCS.get(fee_key)
            if not fee_func:
                # For non-Polymarket pairs, skip for now
                continue

            for m in matched:
                ma = m["market_a"]
                mb = m["market_b"]

                # Get prices from each platform's client
                client_a = platform_clients.get(pa, (None, None))[0] if pa != "polymarket" else None
                client_b = platform_clients.get(pb, (None, None))[0] if pb != "polymarket" else None

                if pa == "polymarket":
                    pm_prices = parse_outcome_prices(ma)
                    if not pm_prices or len(pm_prices) != 2:
                        continue
                    a_yes, a_no = pm_prices[0], pm_prices[1]
                else:
                    a_yes, a_no = client_a.get_market_price(ma) if client_a else (None, None)

                if pb == "polymarket":
                    pm_prices = parse_outcome_prices(mb)
                    if not pm_prices or len(pm_prices) != 2:
                        continue
                    b_yes, b_no = pm_prices[0], pm_prices[1]
                else:
                    b_yes, b_no = client_b.get_market_price(mb) if client_b else (None, None)

                if a_yes is None or a_no is None or b_yes is None or b_no is None:
                    continue

                # Check both strategies: A_YES + B_NO, A_NO + B_YES
                result1 = fee_func(a_yes, b_no, "yes", "no")
                result2 = fee_func(a_no, b_yes, "no", "yes")
                best = result1 if result1["net_profit"] > result2["net_profit"] else result2

                if best["net_profit"] >= min_profit:
                    if best is result1:
                        total_cost = a_yes + b_no
                        prices_str = f"{pa}_Y={a_yes:.3f} {pb}_N={b_no:.3f}"
                    else:
                        total_cost = a_no + b_yes
                        prices_str = f"{pa}_N={a_no:.3f} {pb}_Y={b_yes:.3f}"

                    if total_cost <= 0:
                        continue

                    opp_entry = {
                        "type": f"Cross({pa[:2].upper()}-{pb[:2].upper()})",
                        "_layer": 1,  # Layer 1: pure arbitrage
                        "market": m["title_a"][:50],
                        "kalshi": m["title_b"][:50],
                        "match": f"{m['similarity']}%",
                        "prices": prices_str,
                        "total_cost": f"${total_cost:.4f}",
                        "gross_spread": f"{best['gross_spread']:.4f}",
                        "fees": f"${best['fees']:.4f}",
                        "net_profit": best["net_profit"],
                        "net_roi": f"{best['net_profit'] / total_cost * 100:.2f}%",
                        "confidence": m["confidence"],
                        "_platform_a": pa,
                        "_platform_b": pb,
                        "_days_to_resolution": _days_to_resolution(ma, pa),
                    }

                    # Attach execution metadata per platform
                    _attach_exec_metadata(opp_entry, ma, pa, "a")
                    _attach_exec_metadata(opp_entry, mb, pb, "b")

                    opportunities.append(opp_entry)

    # Stage 2: Refine Polymarket side with CLOB ask prices
    _refine_cross_all_with_clob(opportunities, min_profit, price_cache=price_cache)

    # Attach fee path hints for cross-all opportunities
    # prices_str format: "{pa}_Y={price} {pb}_N={price}" or "{pa}_N={price} {pb}_Y={price}"
    # Each opp has exactly one YES price (buy YES on one platform) and one NO price (buy NO on other)
    for opp in opportunities:
        pa = opp.get("_platform_a", "")
        pb = opp.get("_platform_b", "")
        if not pa or not pb:
            continue
        prices_str = opp.get("prices", "")
        a_yes = a_no = b_yes = b_no = 0.0
        pa_up = pa.upper()
        pb_up = pb.upper()
        pa2 = pa[:2].upper()
        pb2 = pb[:2].upper()
        for part in prices_str.split():
            if "=" not in part:
                continue
            key, val = part.split("=", 1)
            try:
                v = float(val)
            except ValueError:
                continue
            key_upper = key.upper()
            if key_upper in (f"{pa_up}_Y", f"{pa2}_Y"):
                a_yes = v
            elif key_upper in (f"{pa_up}_N", f"{pa2}_N"):
                a_no = v
            elif key_upper in (f"{pb_up}_Y", f"{pb2}_Y"):
                b_yes = v
            elif key_upper in (f"{pb_up}_N", f"{pb2}_N"):
                b_no = v
        # Build yes/no price dicts from the parsed values
        # Strategy "A_YES + B_NO": yes_p has a_yes, no_p has b_no
        # Strategy "A_NO + B_YES": yes_p has b_yes, no_p has a_no
        yes_prices: dict[str, float] = {}
        no_prices: dict[str, float] = {}
        if a_yes:
            yes_prices[pa] = a_yes
        if b_yes:
            yes_prices[pb] = b_yes
        if a_no:
            no_prices[pa] = a_no
        if b_no:
            no_prices[pb] = b_no
        if yes_prices and no_prices:
            fee_path = find_lowest_fee_path([pa, pb], yes_prices, no_prices)
            if fee_path:
                opp["_fee_path"] = fee_path

    opportunities = filter_dust(opportunities)

    return opportunities


def _refine_cross_all_with_clob(opportunities: list[dict], min_profit: float,
                                price_cache: dict | None = None):
    """Refine cross-all opportunities that include Polymarket using CLOB ask prices."""
    pm_opps = [o for o in opportunities
               if o.get("_platform_a") == "polymarket" or o.get("_platform_b") == "polymarket"]
    if not pm_opps:
        return

    # Pre-fetch CLOB prices in parallel for all PM-side token IDs.
    # _fetch_clob_for_market checks the WS cache first before hitting REST.
    clob_cache = {}
    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = {}
        for o in pm_opps:
            token_ids = o.get("_token_ids", [])
            if token_ids and len(token_ids) >= 2:
                key = tuple(token_ids[:2])
                if key not in futures:
                    fake_market = {"clobTokenIds": list(key)}
                    futures[pool.submit(_fetch_clob_for_market, fake_market, price_cache)] = key
        for future in as_completed(futures):
            key = futures[future]
            try:
                _, clob = future.result()
                clob_cache[key] = clob
            except Exception as e:
                logger.debug("CLOB fetch failed for cross-all key %s: %s", key, e)

    # FAIL-CLOSED: every PM opp must complete CLOB verification to survive.
    # Both confirmed-unprofitable opps AND opps whose verification cannot be
    # completed (missing token IDs / CLOB book / fee function / unparseable
    # prices) are dropped in place — a stale mid-price net_profit must never
    # reach execution.
    dropped_ids: set[int] = set()
    for o in pm_opps:
        token_ids = o.get("_token_ids", [])
        if not token_ids or len(token_ids) < 2:
            dropped_ids.add(id(o))
            continue

        clob = clob_cache.get(tuple(token_ids[:2]))
        if not clob or clob.get("yes_ask") is None or clob.get("no_ask") is None:
            dropped_ids.add(id(o))
            continue

        pa = o.get("_platform_a", "")
        pb = o.get("_platform_b", "")
        fee_key = (pa, pb) if (pa, pb) in _CROSS_FEE_FUNCS else (pb, pa)
        ff = _CROSS_FEE_FUNCS.get(fee_key)
        if not ff:
            dropped_ids.add(id(o))
            continue

        # Parse BOTH legs (side + price) from the prices string. Format is
        # "{platform}_Y={price} {platform}_N={price}" — exactly one YES leg
        # and one NO leg. Only the combination matching the opportunity's
        # original structure is re-evaluated; treating the other platform's
        # YES price as a NO price (or vice versa) prices an impossible trade.
        pm_side = None
        other_price = None
        other_side = None
        for part in o.get("prices", "").split():
            if "=" not in part:
                continue
            label, _, val_str = part.partition("=")
            lab = label.lower()
            if lab.endswith("_y"):
                side = "yes"
            elif lab.endswith("_n"):
                side = "no"
            else:
                continue
            try:
                candidate = float(val_str)
            except ValueError:
                logger.debug("Could not parse price from prices part %r", part)
                continue
            if not (0.0 < candidate < 1.0):
                continue
            if lab.startswith("pm_") or lab.startswith("polymarket"):
                pm_side = side
            else:
                other_price = candidate
                other_side = side

        if pm_side is None or other_price is None or other_side == pm_side:
            dropped_ids.add(id(o))
            continue

        # Reprice the PM leg from the live book on ITS side only.
        pm_price = clob.get("yes_ask") if pm_side == "yes" else clob.get("no_ask")
        pm_depth_raw = (
            clob.get("yes_ask_size") if pm_side == "yes"
            else clob.get("no_ask_size")
        )
        if pm_price is None or pm_depth_raw is None:
            dropped_ids.add(id(o))
            continue
        pm_depth = pm_depth_raw or 0

        # Argument order mirrors Stage 1: (price_a, price_b, side_a, side_b).
        if pa == "polymarket":
            best_r = ff(pm_price, other_price, pm_side, other_side)
        else:
            best_r = ff(other_price, pm_price, other_side, pm_side)

        if best_r["net_profit"] >= min_profit:
            total_cost = pm_price + other_price
            pm_tag = "Y" if pm_side == "yes" else "N"
            other_tag = "Y" if other_side == "yes" else "N"
            # Persist the LIVE executable prices — the executor parses the
            # prices string at execution time, so leaving the stale mid-price
            # string in place would defeat the refinement.
            if pa == "polymarket":
                o["prices"] = (f"{pa}_{pm_tag}={pm_price:.3f} "
                               f"{pb}_{other_tag}={other_price:.3f}")
            else:
                o["prices"] = (f"{pa}_{other_tag}={other_price:.3f} "
                               f"{pb}_{pm_tag}={pm_price:.3f}")
            o["total_cost"] = f"${total_cost:.4f}"
            o["net_profit"] = best_r["net_profit"]
            o["fees"] = f"${best_r['fees']:.4f}"
            if total_cost > 0:
                o["net_roi"] = f"{best_r['net_profit'] / total_cost * 100:.2f}%"
            o["_clob_depth"] = pm_depth
            o["_clob_refined"] = True
        else:
            # Confirmed unprofitable at live CLOB prices.
            dropped_ids.add(id(o))

    if dropped_ids:
        opportunities[:] = [o for o in opportunities if id(o) not in dropped_ids]
        logger.info(
            "Dropped %d cross-all candidates at CLOB verification.", len(dropped_ids)
        )
