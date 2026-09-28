"""Shared helpers used across scan modules."""

import json
import logging
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone, timedelta

from polymarket_api import get_clob_prices
from kalshi_api import KalshiClient
from config import MAX_RESOLUTION_DAYS, MIN_PROFIT_AMOUNT

logger = logging.getLogger(__name__)

# Maximum age (seconds) of a WS cache entry before we fall back to REST
_WS_CACHE_MAX_AGE = 30


def filter_dust(opportunities: list[dict], min_amount: float = None) -> list[dict]:
    """Remove opportunities with net_profit below the dust threshold."""
    if min_amount is None:
        min_amount = MIN_PROFIT_AMOUNT
    before = len(opportunities)
    filtered = [o for o in opportunities if o.get("net_profit", 0) >= min_amount]
    removed = before - len(filtered)
    if removed:
        logger.info("Filtered %d dust trades below $%.2f profit.", removed, min_amount)
    return filtered


def _days_to_resolution(market: dict, platform: str = "polymarket") -> float | None:
    """Calculate days until market resolution. Returns None if no date available."""
    if platform == "kalshi":
        date_str = market.get("close_time") or market.get("expected_expiration_time")
    else:
        date_str = market.get("endDateIso")

    if not date_str:
        return None

    try:
        if date_str.endswith("Z"):
            date_str = date_str[:-1] + "+00:00"
        resolve_dt = datetime.fromisoformat(date_str)
        if resolve_dt.tzinfo is None:
            resolve_dt = resolve_dt.replace(tzinfo=timezone.utc)
        days = (resolve_dt - datetime.now(timezone.utc)).total_seconds() / 86400
        return max(days, 0.01)  # Floor at 0.01 to avoid division by zero
    except (ValueError, TypeError):
        return None


def _within_resolution_window(market: dict, max_days: int = None, platform: str = "polymarket") -> bool:
    """Check if a market resolves within max_days from now **and has not already resolved**.

    Returns True if the market resolves in the future within the window (keep it),
    False if it has already resolved, resolves too far out, or has no date (skip it).
    """
    if max_days is None:
        max_days = MAX_RESOLUTION_DAYS
    if max_days <= 0:
        return True  # 0 = disabled

    now = datetime.now(timezone.utc)
    cutoff = now + timedelta(days=max_days)

    if platform == "kalshi":
        date_str = market.get("close_time") or market.get("expected_expiration_time")
    else:  # polymarket
        date_str = market.get("endDateIso")

    if not date_str:
        return False  # No date = skip (conservative)

    try:
        # Parse ISO 8601 (handles both "Z" and "+00:00" suffixes)
        if date_str.endswith("Z"):
            date_str = date_str[:-1] + "+00:00"
        resolve_dt = datetime.fromisoformat(date_str)
        if resolve_dt.tzinfo is None:
            resolve_dt = resolve_dt.replace(tzinfo=timezone.utc)
        # Must resolve in the future AND within the max_days window
        return now <= resolve_dt <= cutoff
    except (ValueError, TypeError):
        return False  # Unparseable date = skip


def _extract_token_ids(market: dict) -> list[str]:
    """Extract CLOB token IDs from a Polymarket market dict."""
    token_ids_raw = market.get("clobTokenIds")
    if not token_ids_raw:
        return []
    try:
        if isinstance(token_ids_raw, str):
            return json.loads(token_ids_raw)
        return list(token_ids_raw)
    except (json.JSONDecodeError, ValueError, TypeError):
        return []


def _parallel_fetch_kalshi(kalshi_client: KalshiClient, tickers: list[str], max_workers: int = 4) -> dict:
    """Pre-fetch Kalshi markets for multiple event tickers in parallel."""
    results = {}
    if not tickers:
        return results

    unique_tickers = list(set(t for t in tickers if t))
    logger.info("Fetching Kalshi markets for %d events (parallel, %d workers)...", len(unique_tickers), max_workers)

    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {
            pool.submit(kalshi_client.fetch_markets_for_event, t): t
            for t in unique_tickers
        }
        for future in as_completed(futures):
            ticker = futures[future]
            try:
                results[ticker] = future.result()
            except Exception as e:
                logger.warning("Failed to fetch Kalshi markets for %s: %s", ticker, e)
                results[ticker] = []

    return results


def _extract_levels_from_book(book: dict) -> tuple[float | None, float, float | None, float]:
    """Extract (best_ask, best_ask_size, best_bid, best_bid_size) from a book dict."""
    asks = book.get("asks") or []
    bids = book.get("bids") or []
    best_ask, ask_size = None, 0.0
    best_bid, bid_size = None, 0.0

    def _parse_entry(entry):
        if isinstance(entry, dict):
            return float(entry.get("price", 0)), float(entry.get("size", 0))
        elif isinstance(entry, (list, tuple)) and len(entry) >= 2:
            return float(entry[0]), float(entry[1])
        return None, 0.0

    if asks:
        valid_asks = []
        for a in asks:
            try:
                p, s = _parse_entry(a)
                if p is not None and p > 0:
                    valid_asks.append((p, s))
            except (ValueError, TypeError):
                continue
        if valid_asks:
            best_ask_entry = min(valid_asks, key=lambda x: x[0])
            best_ask, ask_size = best_ask_entry[0], best_ask_entry[1]

    if bids:
        valid_bids = []
        for b in bids:
            try:
                p, s = _parse_entry(b)
                if p is not None and p > 0:
                    valid_bids.append((p, s))
            except (ValueError, TypeError):
                continue
        if valid_bids:
            best_bid_entry = max(valid_bids, key=lambda x: x[0])
            best_bid, bid_size = best_bid_entry[0], best_bid_entry[1]

    return best_ask, ask_size, best_bid, bid_size


def _fetch_clob_for_market(market: dict, price_cache: dict | None = None,
                           feed_manager=None) -> tuple[dict, dict | None]:
    """Fetch CLOB prices for a single market, checking FeedManager orderbook and WS cache first.

    If fresh (< ``_WS_CACHE_MAX_AGE`` seconds) in-memory orderbook data exists
    in *feed_manager* or normalised price data exists in *price_cache* for both
    the YES and NO tokens, those values are returned immediately without making
    any REST API calls. Otherwise falls back to the standard ``get_clob_prices``
    REST fetch.

    Args:
        market: Polymarket market dict (must contain ``clobTokenIds``).
        price_cache: Shared WS price cache keyed by ``(platform, token_id)``.
        feed_manager: FeedManager instance with in-memory orderbooks.

    Returns:
        ``(market, clob_data)`` where *clob_data* has ``yes_ask``,
        ``yes_ask_size``, ``no_ask``, ``no_ask_size``, ``yes_bid``,
        ``yes_bid_size``, ``no_bid``, ``no_bid_size`` — or ``None``.
    """
    token_ids = _extract_token_ids(market) if (feed_manager or price_cache) else []

    # 1. First priority: in-memory WebSocket reconstructed orderbooks from FeedManager
    if feed_manager and len(token_ids) >= 2:
        book_yes = feed_manager.get_polymarket_orderbook(token_ids[0])
        book_no = feed_manager.get_polymarket_orderbook(token_ids[1])
        age_yes = feed_manager.get_polymarket_orderbook_age(token_ids[0])
        age_no = feed_manager.get_polymarket_orderbook_age(token_ids[1])
        if (book_yes and book_no
                and age_yes is not None and age_no is not None
                and age_yes < _WS_CACHE_MAX_AGE and age_no < _WS_CACHE_MAX_AGE):
            yes_ask, yes_ask_size, yes_bid, yes_bid_size = _extract_levels_from_book(book_yes)
            no_ask, no_ask_size, no_bid, no_bid_size = _extract_levels_from_book(book_no)
            if yes_ask is not None and no_ask is not None:
                return market, {
                    "yes_ask": yes_ask,
                    "yes_ask_size": yes_ask_size,
                    "no_ask": no_ask,
                    "no_ask_size": no_ask_size,
                    "yes_bid": yes_bid,
                    "yes_bid_size": yes_bid_size,
                    "no_bid": no_bid,
                    "no_bid_size": no_bid_size,
                }

    # 2. Second priority: normalised WS price cache
    if price_cache and len(token_ids) >= 2:
        now = time.time()
        cached_yes = price_cache.get(("polymarket", token_ids[0]))
        cached_no = price_cache.get(("polymarket", token_ids[1]))
        if (cached_yes and cached_no
                and now - cached_yes.get("_ts", 0) < _WS_CACHE_MAX_AGE
                and now - cached_no.get("_ts", 0) < _WS_CACHE_MAX_AGE):
            yes_ask = cached_yes.get("best_ask")
            no_ask = cached_no.get("best_ask")
            if yes_ask is not None and no_ask is not None:
                return market, {
                    "yes_ask": yes_ask,
                    "yes_ask_size": cached_yes.get("best_ask_size", 0) or 0,
                    "no_ask": no_ask,
                    "no_ask_size": cached_no.get("best_ask_size", 0) or 0,
                    "yes_bid": cached_yes.get("best_bid"),
                    "yes_bid_size": cached_yes.get("best_bid_size", 0) or 0,
                    "no_bid": cached_no.get("best_bid"),
                    "no_bid_size": cached_no.get("best_bid_size", 0) or 0,
                }

    # Fallback to REST API
    return market, get_clob_prices(market)


def capital_efficiency_score(opp: dict) -> float:
    """Score an opportunity by capital efficiency: (net_profit / total_cost) * min(depth, 50).

    When _days_to_resolution is present, divides the base score by days
    to favor fast-resolving markets. Fallback: original score when no date.
    """
    net_profit = opp.get("net_profit", 0)
    if net_profit <= 0:
        return 0.0

    total_cost_str = opp.get("total_cost", "$0")
    try:
        total_cost = float(total_cost_str.replace("$", "")) if isinstance(total_cost_str, str) else float(total_cost_str)
    except (ValueError, TypeError):
        return 0.0

    if total_cost <= 0:
        return 0.0

    depth = opp.get("_clob_depth") or 0
    if depth <= 0:
        depth = 1

    roi = net_profit / total_cost
    base_score = roi * min(depth, 50)

    days = opp.get("_days_to_resolution")
    if days is not None and days > 0:
        return base_score / days

    return base_score
