"""Binary internal arbitrage scan (YES + NO < $1.00 on Polymarket)."""

import logging
from concurrent.futures import ThreadPoolExecutor, as_completed

from polymarket_api import get_binary_markets, parse_outcome_prices
from fees import net_profit_binary_internal
from scans.helpers import _extract_token_ids, _fetch_clob_for_market, _within_resolution_window, filter_dust, _days_to_resolution

logger = logging.getLogger(__name__)


def _refine_binary_with_clob(opportunities: list[dict], markets_by_question: dict, min_profit: float,
                             price_cache: dict | None = None,
                             funnel=None) -> list[dict]:
    """Stage 2: Re-check binary candidates using CLOB ask prices (what you'd actually pay)."""
    if not opportunities:
        return opportunities

    logger.info("Refining %d candidates with CLOB ask prices...", len(opportunities))

    # Pre-fetch CLOB prices in parallel (WS cache checked inside _fetch_clob_for_market)
    fetch_tasks = {}  # market_key -> market
    for opp in opportunities:
        market_key = opp.get("_market_key")
        market = markets_by_question.get(market_key) if market_key else None
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
                    logger.debug("CLOB fetch failed for binary refinement: %s", e)

    refined = []
    for opp in opportunities:
        market_key = opp.get("_market_key")
        market = markets_by_question.get(market_key) if market_key else None
        if not market:
            refined.append(opp)
            continue

        clob = clob_results.get(market_key)
        if not clob or clob["yes_ask"] is None or clob["no_ask"] is None:
            opp["_clob_refined"] = False
            refined.append(opp)  # Keep if CLOB unavailable
            continue

        yes_ask = clob["yes_ask"]
        no_ask = clob["no_ask"]
        result = net_profit_binary_internal(yes_ask, no_ask,
                                            category=market.get("category"))

        if result["net_profit"] >= min_profit:
            opp["prices"] = f"Y={yes_ask:.3f} N={no_ask:.3f}"
            opp["total_cost"] = f"${yes_ask + no_ask:.4f}"
            opp["gross_spread"] = f"{result['gross_spread']:.4f}"
            opp["fees"] = f"${result['fees']:.4f}"
            opp["net_profit"] = result["net_profit"]
            opp["net_roi"] = f"{result['net_profit'] / (yes_ask + no_ask) * 100:.2f}%"
            opp["_clob_depth"] = min(
                clob["yes_ask_size"] or 0,
                clob["no_ask_size"] or 0,
            )
            refined.append(opp)
        else:
            if funnel:
                if result.get("gross_spread", 0) > 0:
                    funnel.record_fee_dropped(1)
                else:
                    funnel.record_clob_dropped(1)

    dropped = len(opportunities) - len(refined)
    if dropped:
        logger.info("Dropped %d candidates at CLOB ask prices.", dropped)
    return refined


def scan_binary_internal(markets: list[dict], min_profit: float,
                         price_cache: dict | None = None,
                         funnel=None) -> list[dict]:
    """Scan for binary arbitrage on Polymarket (YES + NO < $1.00)."""
    if funnel is None:
        try:
            from funnel import get_funnel_tracker
            funnel = get_funnel_tracker()
        except ImportError:
            funnel = None

    opportunities = []
    markets_by_question = {}

    binary_markets = get_binary_markets(markets)
    logger.info("Scanning %d binary markets...", len(binary_markets))
    if funnel:
        funnel.record_screened(len(binary_markets))

    filtered_resolution = 0
    for m in binary_markets:
        if not _within_resolution_window(m, platform="polymarket"):
            filtered_resolution += 1
            continue
        prices = parse_outcome_prices(m)
        if not prices or len(prices) != 2:
            continue

        yes_price, no_price = prices[0], prices[1]

        # Skip markets with no liquidity (essentially zero)
        if yes_price <= 0.001 or no_price <= 0.001:
            continue
        # Skip resolved markets (one side near 1.0 and total near 1.0)
        if (yes_price >= 0.99 or no_price >= 0.99) and (yes_price + no_price) > 0.98:
            continue

        result = net_profit_binary_internal(yes_price, no_price,
                                            category=m.get("category"))

        if result["net_profit"] >= min_profit:
            market_key = m.get("conditionId", m.get("question", ""))
            markets_by_question[market_key] = m
            # Extract CLOB token IDs for execution
            token_ids = _extract_token_ids(m)
            opportunities.append({
                "type": "Binary",
                "_layer": 1,  # Layer 1: pure arbitrage
                "market": m.get("question", m.get("title", "Unknown"))[:60],
                "prices": f"Y={yes_price:.3f} N={no_price:.3f}",
                "total_cost": f"${yes_price + no_price:.4f}",
                "gross_spread": f"{result['gross_spread']:.4f}",
                "fees": f"${result['fees']:.4f}",
                "net_profit": result["net_profit"],
                "net_roi": f"{result['net_profit'] / (yes_price + no_price) * 100:.2f}%",
                "volume": f"${float(m.get('volume', 0) or 0):,.0f}",
                "_market_key": market_key,
                "_condition_id": m.get("conditionId") or m.get("condition_id") or market_key,
                "_token_ids": token_ids,
                "_days_to_resolution": _days_to_resolution(m, "polymarket"),
            })

    if filtered_resolution:
        logger.info("Filtered %d/%d binary markets outside resolution window.", filtered_resolution, len(binary_markets))

    if funnel:
        funnel.record_mid_candidates(len(opportunities))
        funnel.record_clob_evaluated(len(opportunities))

    # Stage 2: Refine with CLOB ask prices
    opportunities = _refine_binary_with_clob(opportunities, markets_by_question, min_profit, price_cache=price_cache, funnel=funnel)

    opportunities = filter_dust(opportunities)

    return opportunities
