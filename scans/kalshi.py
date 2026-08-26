"""Kalshi-specific arbitrage scans (binary and multi-outcome)."""

import logging
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

from kalshi_api import KalshiClient
from config import KALSHI_MULTI_MIN_SUM as _KALSHI_MULTI_MIN_SUM
from fees import net_profit_kalshi_binary, net_profit_kalshi_multi
from kalshi_policy import event_blocked as _event_blocked
from scans.helpers import _parallel_fetch_kalshi, _within_resolution_window, filter_dust, _days_to_resolution

logger = logging.getLogger(__name__)


def _blocked_event_tickers(events: list[dict]) -> set[str]:
    return {e.get("event_ticker", "") for e in events if e.get("event_ticker") and _event_blocked(e)}


# TTL cache for `_fetch_kalshi_data` — avoids re-pulling the full /events
# payload on back-to-back scans. Default 60s; tune via env var.
_KALSHI_DATA_CACHE_TTL = float(os.getenv("KALSHI_DATA_CACHE_TTL", "60"))
_kalshi_data_cache: dict = {"ts": 0.0, "value": None}
_kalshi_data_cache_lock = threading.Lock()


def _split_nested_events(events: list[dict]) -> tuple[list[dict], dict]:
    """Split events with nested ``markets`` field into (events_only, markets_by_event).

    When `fetch_all_events(with_nested_markets=True)` is used (default), each
    event already carries a ``markets`` array. This extracts those without
    a second N-call REST round-trip. Events without nested markets are left
    in the resulting list with an empty markets entry — the caller can fall
    back to ``_parallel_fetch_kalshi`` for those if needed.
    """
    markets_by_event: dict = {}
    for e in events:
        ticker = e.get("event_ticker", "")
        if not ticker:
            continue
        markets_by_event[ticker] = e.get("markets", []) or []
    return events, markets_by_event


def _fetch_kalshi_data(kalshi_client: KalshiClient) -> tuple[list[dict], dict, dict]:
    """Shared fetch: get all Kalshi events and their markets once.

    Returns (events, markets_by_event, event_titles).

    Uses a process-wide TTL cache (``KALSHI_DATA_CACHE_TTL`` seconds, default 60).
    Within the TTL window, returns the cached result without making any API
    calls. After expiry, refreshes via a single `/events?with_nested_markets=true`
    paginated call — eliminating the N-event-per-scan REST follow-ups that
    previously dominated scan-cycle latency.
    """
    if not kalshi_client:
        return [], {}, {}

    now = time.time()
    with _kalshi_data_cache_lock:
        cached = _kalshi_data_cache.get("value")
        cached_ts = _kalshi_data_cache.get("ts", 0.0)
        if cached is not None and now - cached_ts < _KALSHI_DATA_CACHE_TTL:
            logger.info(
                "Kalshi data cache hit (age=%.1fs, TTL=%.0fs).",
                now - cached_ts, _KALSHI_DATA_CACHE_TTL,
            )
            return cached

    logger.info("Fetching all Kalshi events (with nested markets)...")
    events = kalshi_client.fetch_all_events()
    if not events:
        logger.warning("No Kalshi events fetched.")
        return [], {}, {}

    # If events came back with nested markets (default), build the lookup
    # table directly. Fall back to per-event REST fetch only when the
    # response is missing the embedded markets array.
    has_nested = any("markets" in e for e in events)
    if has_nested:
        _, markets_by_event = _split_nested_events(events)
        nested_market_count = sum(len(m) for m in markets_by_event.values())
        logger.info(
            "Fetched %d Kalshi events with %d nested markets (skipping per-event REST fetch).",
            len(events), nested_market_count,
        )
    else:
        logger.info("Fetched %d Kalshi events; falling back to per-event REST fetch.", len(events))
        tickers = [e.get("event_ticker", "") for e in events if e.get("event_ticker")]
        markets_by_event = _parallel_fetch_kalshi(kalshi_client, tickers)

    event_titles = {e.get("event_ticker", ""): e.get("title", "Unknown") for e in events}
    result = (events, markets_by_event, event_titles)

    with _kalshi_data_cache_lock:
        _kalshi_data_cache["ts"] = now
        _kalshi_data_cache["value"] = result

    return result


def scan_kalshi_binary(
    kalshi_client: KalshiClient,
    min_profit: float,
    kalshi_data: tuple | None = None,
) -> list[dict]:
    """Scan for Kalshi binary arbitrage (YES + NO < $1.00 on same market)."""
    opportunities = []

    if not kalshi_client:
        logger.info("Kalshi credentials not configured.")
        return opportunities

    if kalshi_data:
        events, markets_by_event, _ = kalshi_data
    else:
        events, markets_by_event, _ = _fetch_kalshi_data(kalshi_client)

    if not markets_by_event:
        return opportunities

    blocked = _blocked_event_tickers(events)
    skipped_blocked = 0

    total_markets = 0
    filtered_resolution = 0
    for event_ticker, markets in markets_by_event.items():
        if event_ticker in blocked:
            skipped_blocked += 1
            continue
        for km in markets:
            total_markets += 1
            if not _within_resolution_window(km, platform="kalshi"):
                filtered_resolution += 1
                continue
            yes_price, no_price = kalshi_client.get_market_price(km)
            if yes_price is None or no_price is None:
                continue
            if yes_price <= 0.001 or no_price <= 0.001:
                continue

            result = net_profit_kalshi_binary(yes_price, no_price)
            if result["net_profit"] >= min_profit:
                ticker = km.get("ticker", "")
                total_cost = yes_price + no_price
                opportunities.append({
                    "type": "KalshiBinary",
                    "_layer": 1,  # Layer 1: pure arbitrage
                    "market": km.get("title", "")[:60],
                    "prices": f"Y={yes_price:.3f} N={no_price:.3f}",
                    "total_cost": f"${total_cost:.4f}",
                    "gross_spread": f"{result['gross_spread']:.4f}",
                    "fees": f"${result['fees']:.4f}",
                    "net_profit": result["net_profit"],
                    "net_roi": f"{result['net_profit'] / total_cost * 100:.2f}%",
                    "_kalshi_ticker": ticker,
                    "_kalshi_yes": yes_price,
                    "_kalshi_no": no_price,
                    "_days_to_resolution": _days_to_resolution(km, "kalshi"),
                })

    if filtered_resolution:
        logger.info("Filtered %d/%d Kalshi markets outside resolution window.", filtered_resolution, total_markets)
    if skipped_blocked:
        logger.info("Skipped %d Kalshi events blocked as sports/mentions.", skipped_blocked)
    logger.info("Scanned %d Kalshi markets across %d events.", total_markets - filtered_resolution, len(events))

    # Stage 2: Re-fetch order book depth for top candidates (parallel)
    if opportunities:
        logger.info("Fetching order book depth for %d candidates...", len(opportunities))

        def _fetch_depth(opp):
            ticker = opp.get("_kalshi_ticker", "")
            if not ticker:
                return opp, 0
            depth = kalshi_client.get_order_book_depth(ticker)
            if depth:
                return opp, min(depth.get("yes_ask_size", 0), depth.get("no_ask_size", 0))
            return opp, 0

        with ThreadPoolExecutor(max_workers=8) as pool:
            futures = {pool.submit(_fetch_depth, opp): opp for opp in opportunities}
            for future in as_completed(futures):
                opp, d = future.result()
                opp["_clob_depth"] = d

    opportunities = filter_dust(opportunities)

    return opportunities


def scan_kalshi_multi(
    kalshi_client: KalshiClient,
    min_profit: float,
    kalshi_data: tuple | None = None,
) -> list[dict]:
    """Scan for Kalshi multi-outcome arbitrage (sum of YES prices < $1.00 across event)."""
    opportunities = []

    if not kalshi_client:
        logger.info("Kalshi credentials not configured.")
        return opportunities

    if kalshi_data:
        events, markets_by_event, event_titles = kalshi_data
    else:
        events, markets_by_event, event_titles = _fetch_kalshi_data(kalshi_client)

    if not markets_by_event:
        return opportunities

    # Complete-set gate (June 2026 audit): a multi-outcome "arb" is only real
    # when EXACTLY ONE outcome pays $1 — i.e. the event is mutually exclusive.
    # Kalshi groups non-exclusive market ladders (e.g. soccer "Spreads": wins
    # by 1+, by 2+, ...) under one event; buying YES on each is NOT a complete
    # set, and treating it as one produced ~296K phantom detections over three
    # months in production. Only events the API marks mutually_exclusive=True
    # qualify; missing/False is skipped.
    me_by_event = {e.get("event_ticker"): e.get("mutually_exclusive") for e in events}
    blocked = _blocked_event_tickers(events)
    skipped_blocked = 0

    def _is_exhaustive_strike_ladder(event_markets: list[dict]) -> bool | None:
        """Whether a scalar strike ladder covers the whole outcome space.

        ``mutually_exclusive`` only guarantees at most one outcome pays — not
        that the listed outcomes are collectively exhaustive. KXTRUMPPHOTO
        (buckets exactly 4/5/6/7, no "3 or fewer" tail) priced at 0.86 was
        reported as a riskless 8% arb; a 0-3 result loses every leg. Kalshi
        encodes exhaustive ladders with open-ended tail buckets (floor-only /
        cap-only strikes), so require one of each. Returns None for
        categorical events (no strike fields) — no structural signal there.
        """
        strikes = [(m.get("floor_strike"), m.get("cap_strike")) for m in event_markets]
        if all(f is None and c is None for f, c in strikes):
            return None
        has_open_bottom = any(f is None and c is not None for f, c in strikes)
        has_open_top = any(f is not None and c is None for f, c in strikes)
        if not (has_open_bottom and has_open_top):
            return False
        # Interior contiguity: tails alone don't rule out a missing middle
        # bucket (<=3, 5, >=6 omits 4). Sort bounded intervals by floor and
        # require each to start within one tick of the previous cap; the same
        # rule links the bottom tail's cap and the top tail's floor.
        try:
            bottom_cap = max(float(c) for f, c in strikes if f is None and c is not None)
            top_floor = min(float(f) for f, c in strikes if f is not None and c is None)
            interior = sorted(
                (float(f), float(c)) for f, c in strikes if f is not None and c is not None
            )
        except (TypeError, ValueError):
            return False
        prev_cap = bottom_cap
        for floor, cap in interior:
            if floor - prev_cap > 1:
                return False
            prev_cap = max(prev_cap, cap)
        return top_floor - prev_cap <= 1

    filtered_resolution = 0
    skipped_non_exclusive = 0
    skipped_non_exhaustive = 0
    for event_ticker, markets in markets_by_event.items():
        if event_ticker in blocked:
            skipped_blocked += 1
            continue
        if len(markets) < 2:
            continue
        if me_by_event.get(event_ticker) is not True:
            skipped_non_exclusive += 1
            continue
        if _is_exhaustive_strike_ladder(markets) is False:
            skipped_non_exhaustive += 1
            continue

        yes_prices = []
        market_tickers = []
        valid = True

        for km in markets:
            if not _within_resolution_window(km, platform="kalshi"):
                filtered_resolution += 1
                valid = False
                break
            yes_price, _ = kalshi_client.get_market_price(km)
            if yes_price is None or yes_price <= 0:
                valid = False
                break
            yes_prices.append(yes_price)
            market_tickers.append(km.get("ticker", ""))

        if not valid or not yes_prices:
            continue

        # Completeness guard: for a true single-winner market the YES asks sum to
        # just under/over 1.0. A sum well below the floor means missing/closed
        # legs or stale quotes — not a real arb. Applies to every outcome count,
        # including the 2-leg events the old ``>= 3`` heuristic ignored.
        total_yes = sum(yes_prices)
        if total_yes < _KALSHI_MULTI_MIN_SUM:
            event_title = event_titles.get(event_ticker, "Unknown")[:60]
            logger.warning(
                "Implausible multi sum (missing/stale legs): '%s' (%d outcomes sum to %.3f, floor=%.2f)",
                event_title, len(yes_prices), total_yes, _KALSHI_MULTI_MIN_SUM,
            )
            continue

        result = net_profit_kalshi_multi(yes_prices)
        if result["net_profit"] >= min_profit:
            total = sum(yes_prices)
            n = len(yes_prices)
            price_summary = ", ".join(f"{p:.3f}" for p in sorted(yes_prices, reverse=True)[:5])
            if n > 5:
                price_summary += f"... ({n} total)"

            event_title = event_titles.get(event_ticker, "Unknown")
            opportunities.append({
                "type": f"KalshiMulti({n})",
                "_layer": 1,  # Layer 1: pure arbitrage
                "market": event_title[:60],
                "prices": price_summary,
                "total_cost": f"${total:.4f}",
                "gross_spread": f"{result['gross_spread']:.4f}",
                "fees": f"${result['fees']:.4f}",
                "net_profit": result["net_profit"],
                "net_roi": f"{result['net_profit'] / total * 100:.2f}%",
                "_kalshi_tickers": market_tickers,
                "_kalshi_prices": yes_prices,
                "_days_to_resolution": _days_to_resolution(markets[0], "kalshi"),
            })

    if filtered_resolution:
        logger.info("Filtered %d Kalshi multi-outcome events outside resolution window.", filtered_resolution)
    if skipped_non_exclusive:
        logger.info("Skipped %d non-mutually-exclusive Kalshi events (not complete sets).", skipped_non_exclusive)
    if skipped_non_exhaustive:
        logger.info("Skipped %d Kalshi strike-ladder events without open tail buckets (non-exhaustive sets).", skipped_non_exhaustive)
    if skipped_blocked:
        logger.info("Skipped %d Kalshi multi events blocked as sports/mentions.", skipped_blocked)

    # Stage 2: Re-fetch order book depth for candidates (parallel, min depth across all legs)
    if opportunities:
        logger.info("Fetching order book depth for %d multi-outcome candidates...", len(opportunities))

        def _fetch_multi_depth(opp):
            min_d = float("inf")
            for ticker in opp.get("_kalshi_tickers", []):
                if ticker:
                    depth = kalshi_client.get_order_book_depth(ticker)
                    if depth:
                        d = depth.get("yes_ask_size", 0)
                        min_d = min(min_d, d)
                    else:
                        min_d = 0
                        break
            return opp, min_d if min_d != float("inf") else 0

        with ThreadPoolExecutor(max_workers=8) as pool:
            futures = {pool.submit(_fetch_multi_depth, opp): opp for opp in opportunities}
            for future in as_completed(futures):
                try:
                    opp, d = future.result()
                except Exception as exc:
                    opp = futures[future]
                    d = 0
                    logger.warning("Kalshi multi depth fetch failed closed: %s", exc)
                opp["_clob_depth"] = d

    opportunities = filter_dust(opportunities)

    return opportunities
