"""Gemini Predictions standalone arbitrage scans (binary and multi-outcome)."""

import logging
from concurrent.futures import ThreadPoolExecutor

from gemini_api import GeminiClient
from fees import net_profit_gemini_binary, net_profit_gemini_multi
import config
from scans.helpers import filter_dust

logger = logging.getLogger(__name__)

_BOOK_FETCH_WORKERS = 5


def _fetch_ask_depths(gemini_client: GeminiClient, symbols: list[str]) -> dict[str, float]:
    """Fetch best-ask depth for each symbol in parallel.

    Returns a dict mapping symbol -> best-ask amount (0.0 if book empty or
    fetch failed). Empty symbols are skipped.
    """
    unique_syms = [s for s in dict.fromkeys(symbols) if s]
    if not unique_syms:
        return {}

    def _one(sym):
        book = gemini_client.get_order_book(sym, limit=1)
        if book and book.get("asks"):
            try:
                return sym, float(book["asks"][0].get("amount", 0))
            except (ValueError, TypeError):
                return sym, 0.0
        return sym, 0.0

    depths: dict[str, float] = {}
    with ThreadPoolExecutor(max_workers=_BOOK_FETCH_WORKERS) as pool:
        for sym, depth in pool.map(_one, unique_syms):
            depths[sym] = depth
    return depths


def scan_gemini_binary(gemini_client: GeminiClient, min_profit: float) -> list[dict]:
    """Scan for Gemini binary arbitrage (under-round YES+NO).

    For each binary event: if YES + NO < 1.0, guaranteed profit.
    Gemini fee: min(P, 1-P) * fee_rate per contract (5% taker, 1% maker).
    """
    opportunities = []

    if not gemini_client or not gemini_client.authenticated:
        return opportunities

    events = gemini_client.fetch_all_markets()
    if not events:
        logger.warning("No Gemini markets fetched.")
        return opportunities

    binary_events = [e for e in events if e.get("type") == "binary"]
    logger.info("Scanning %d Gemini binary events for arbs...", len(binary_events))

    # Stage 1: candidate collection — compute profit from normalized prices.
    candidates: list[dict] = []
    for event in binary_events:
        yes_price, no_price = gemini_client.get_market_price(event)
        if yes_price is None or no_price is None:
            continue
        if yes_price <= 0 or no_price <= 0:
            continue

        result = net_profit_gemini_binary(yes_price, no_price, fee_rate=config.GEMINI_TAKER_RATE)
        if result["net_profit"] < min_profit:
            continue

        contracts = event.get("contracts", [])
        yes_symbol = ""
        no_symbol = ""
        for c in contracts:
            label = (c.get("label") or c.get("outcome") or "").lower()
            if "yes" in label and not yes_symbol:
                yes_symbol = c.get("instrumentSymbol", "")
            elif "no" in label and not no_symbol:
                no_symbol = c.get("instrumentSymbol", "")
        if not yes_symbol and len(contracts) >= 2:
            yes_symbol = contracts[0].get("instrumentSymbol", "")
            no_symbol = contracts[1].get("instrumentSymbol", "")

        candidates.append({
            "event": event, "result": result,
            "yes_price": yes_price, "no_price": no_price,
            "yes_symbol": yes_symbol, "no_symbol": no_symbol,
        })

    # Stage 2: parallel order-book depth fetch across unique symbols.
    all_symbols = []
    for cand in candidates:
        all_symbols.extend([cand["yes_symbol"], cand["no_symbol"]])
    depths = _fetch_ask_depths(gemini_client, all_symbols)

    for cand in candidates:
        event = cand["event"]
        result = cand["result"]
        yes_price = cand["yes_price"]
        no_price = cand["no_price"]
        yes_symbol = cand["yes_symbol"]
        no_symbol = cand["no_symbol"]
        total = yes_price + no_price

        yes_depth = depths.get(yes_symbol, 0.0)
        no_depth = depths.get(no_symbol, 0.0)
        depth = min(yes_depth, no_depth) if (yes_depth and no_depth) else 0

        opportunities.append({
            "type": "GeminiBinary",
            "_layer": 1,
            "market": event.get("title", "")[:60],
            "prices": f"Y={yes_price:.3f} N={no_price:.3f}",
            "total_cost": f"${total:.4f}",
            "gross_spread": f"{result['gross_spread']:.4f}",
            "fees": f"${result['fees']:.4f}",
            "net_profit": result["net_profit"],
            "net_roi": f"{result['net_profit'] / total * 100:.2f}%",
            "_gm_event_id": event.get("id", ""),
            "_gm_yes_symbol": yes_symbol,
            "_gm_no_symbol": no_symbol,
            "_gm_yes_price": yes_price,
            "_gm_no_price": no_price,
            "_clob_depth": depth,
        })

    logger.info("Found %d Gemini binary opportunities.", len(opportunities))
    opportunities = filter_dust(opportunities)
    return opportunities


def scan_gemini_multi(gemini_client: GeminiClient, min_profit: float) -> list[dict]:
    """Scan for Gemini categorical (multi-outcome) arbitrage.

    For each categorical event: if sum of all YES prices < 1.0, guaranteed profit.
    """
    opportunities = []

    if not gemini_client or not gemini_client.authenticated:
        return opportunities

    events = gemini_client.fetch_all_markets()
    if not events:
        return opportunities

    categorical_events = [e for e in events if e.get("type") == "categorical"]
    logger.info("Scanning %d Gemini categorical events for arbs...", len(categorical_events))

    # Stage 1: gather candidates that pass the profit gate on mid prices.
    candidates: list[dict] = []
    implausible_skipped = 0
    for event in categorical_events:
        contracts = event.get("contracts", [])
        if len(contracts) < 3:
            continue

        prices = []
        symbols = []
        valid = True
        for c in contracts:
            price = c.get("price")
            if price is None or float(price) <= 0:
                valid = False
                break
            prices.append(float(price))
            symbols.append(c.get("instrumentSymbol", ""))
        if not valid or not prices:
            continue

        total_mid = sum(prices)
        if total_mid < config.GEMINI_MULTI_MIN_SUM:
            # Outcome sets summing far below $1 are not exhaustive MECE sets
            # (e.g. point-spread events listing alternative lines) — buying
            # them all does NOT guarantee a $1 payout. Same defense as the
            # Kalshi strike-ladder gate.
            implausible_skipped += 1
            continue

        result = net_profit_gemini_multi(prices, fee_rate=config.GEMINI_TAKER_RATE)
        if result["net_profit"] < min_profit:
            continue

        candidates.append({
            "event": event, "result": result,
            "prices": prices, "symbols": symbols,
        })

    # Stage 2: parallel order-book depth fetch across all candidate symbols.
    all_symbols: list[str] = []
    for cand in candidates:
        all_symbols.extend(cand["symbols"])
    depths = _fetch_ask_depths(gemini_client, all_symbols)

    for cand in candidates:
        event = cand["event"]
        result = cand["result"]
        prices = cand["prices"]
        symbols = cand["symbols"]
        total = sum(prices)
        n = len(prices)
        price_summary = ", ".join(f"{p:.3f}" for p in sorted(prices, reverse=True)[:5])
        if n > 5:
            price_summary += f"... ({n} outcomes)"

        # Min depth across all outcome asks; zero if any symbol missing or empty.
        min_depth = float("inf")
        for sym in symbols:
            if not sym:
                min_depth = 0
                break
            d = depths.get(sym, 0.0)
            if d <= 0:
                min_depth = 0
                break
            min_depth = min(min_depth, d)
        if min_depth == float("inf"):
            min_depth = 0

        opportunities.append({
            "type": "GeminiMulti",
            "_layer": 1,
            "market": event.get("title", "")[:60],
            "prices": price_summary,
            "total_cost": f"${total:.4f}",
            "gross_spread": f"{result['gross_spread']:.4f}",
            "fees": f"${result['fees']:.4f}",
            "net_profit": result["net_profit"],
            "net_roi": f"{result['net_profit'] / total * 100:.2f}%",
            "_gm_event_id": event.get("id", ""),
            "_gm_symbols": symbols,
            "_gm_prices": prices,
            "_clob_depth": min_depth,
        })

    if implausible_skipped:
        logger.info(
            "Skipped %d Gemini events with outcome sums < %.2f (non-exhaustive sets).",
            implausible_skipped, config.GEMINI_MULTI_MIN_SUM)
    logger.info("Found %d Gemini multi-outcome opportunities.", len(opportunities))
    opportunities = filter_dust(opportunities)
    return opportunities
