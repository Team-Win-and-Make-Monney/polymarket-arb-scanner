"""CTF Primitives Arbitrage Scan (Polymarket on-chain Merge and Split).

Scans for:
1. CTFMerge: Buy YES + NO on order book and merge via ConditionalTokens.mergePositions() (YES_ask + NO_ask < $1.00).
2. CTFMint: Mint YES + NO from $1.00 collateral via ConditionalTokens.splitPosition() and sell on order book (YES_bid + NO_bid > $1.00).
"""

from __future__ import annotations

from datetime import datetime, timezone, timedelta
import logging
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

from config import (
    CTF_ENABLED,
    CTF_MAX_RESOLUTION_DAYS,
    CTF_MERGE_ENABLED,
    CTF_MIN_PROFIT,
    CTF_MINT_SELL_ENABLED,
)
from fees import net_profit_ctf_merge, net_profit_ctf_mint
from polymarket_api import get_binary_markets, parse_outcome_prices
from scans.helpers import (
    _WS_CACHE_MAX_AGE,
    _days_to_resolution,
    _extract_token_ids,
    _fetch_clob_for_market,
    _within_resolution_window,
    filter_dust,
)

logger = logging.getLogger(__name__)


def _is_ctf_market_eligible(market: dict, max_days: int | None = None) -> bool:
    """Check if a binary market is active and eligible for CTF atomic operations.

    Rejects:
    - explicitly closed or inactive markets
    - markets that have already resolved/expired in the past
    - unparseable or missing resolution dates
    - markets resolving beyond max_days (when max_days > 0).

    When max_days <= 0 (default: CTF_MAX_RESOLUTION_DAYS = 0), allows any
    active unexpired market regardless of horizon, because CTF merge and split
    settle atomically on-chain without holding to resolution.

    Args:
        market: Polymarket market dictionary.
        max_days: Maximum days to resolution. 0 or None disables the horizon filter.

    Returns:
        True if market is eligible, False otherwise.
    """
    if max_days is None:
        max_days = CTF_MAX_RESOLUTION_DAYS

    if market.get("closed") is True or market.get("active") is False:
        return False

    date_str = market.get("endDateIso")
    if not date_str:
        return False

    try:
        if date_str.endswith("Z"):
            date_str = date_str[:-1] + "+00:00"
        resolve_dt = datetime.fromisoformat(date_str)
        if resolve_dt.tzinfo is None:
            resolve_dt = resolve_dt.replace(tzinfo=timezone.utc)
        now = datetime.now(timezone.utc)
        if resolve_dt <= now:
            return False

        if max_days > 0:
            cutoff = now + timedelta(days=max_days)
            if resolve_dt > cutoff:
                return False

        return True
    except (ValueError, TypeError):
        return False


def _get_cached_ws_book(
    token_ids: list[str],
    price_cache: dict | None,
) -> tuple[dict | None, dict | None]:
    """Retrieve fresh YES and NO book quotes from WS price_cache if available.

    Args:
        token_ids: List of CLOB token IDs [yes_token, no_token].
        price_cache: Shared WebSocket price cache keyed by (platform, token_id).

    Returns:
        (cached_yes, cached_no) if both exist and are fresh (< _WS_CACHE_MAX_AGE),
        else (None, None).
    """
    if not price_cache or len(token_ids) < 2:
        return None, None
    now = time.time()
    cached_yes = price_cache.get(("polymarket", token_ids[0]))
    cached_no = price_cache.get(("polymarket", token_ids[1]))
    if not (cached_yes and cached_no):
        return None, None
    if (now - cached_yes.get("_ts", 0) >= _WS_CACHE_MAX_AGE
            or now - cached_no.get("_ts", 0) >= _WS_CACHE_MAX_AGE):
        return None, None
    return cached_yes, cached_no


def _refine_ctf_with_clob(
    opportunities: list[dict],
    markets_by_question: dict,
    min_profit: float,
    price_cache: dict | None = None,
    funnel=None,
) -> list[dict]:
    """Stage 2: Re-check CTF candidates using live CLOB ask/bid prices."""
    if not opportunities:
        return opportunities

    logger.info("Refining %d CTF candidates with CLOB order book prices...", len(opportunities))

    fetch_tasks = {}
    for opp in opportunities:
        market_key = opp.get("_market_key")
        market = markets_by_question.get(market_key) if market_key else None
        if market and market_key not in fetch_tasks:
            fetch_tasks[market_key] = market

    clob_results = {}
    if fetch_tasks:
        with ThreadPoolExecutor(max_workers=4) as pool:
            futures = {
                pool.submit(_fetch_clob_for_market, m, price_cache): mk
                for mk, m in fetch_tasks.items()
            }
            for future in as_completed(futures):
                mk = futures[future]
                try:
                    _, clob = future.result()
                    clob_results[mk] = clob
                except Exception as e:
                    logger.debug("CLOB fetch failed for CTF refinement: %s", e)

    refined = []
    for opp in opportunities:
        market_key = opp.get("_market_key")
        market = markets_by_question.get(market_key) if market_key else None
        if not market:
            refined.append(opp)
            continue

        clob = clob_results.get(market_key)
        if not clob:
            opp["_clob_refined"] = False
            refined.append(opp)
            continue

        opp_type = opp.get("type")
        category = market.get("category")

        if opp_type == "CTFMerge":
            yes_ask = clob.get("yes_ask")
            no_ask = clob.get("no_ask")
            if yes_ask is None or no_ask is None:
                opp["_clob_refined"] = False
                refined.append(opp)
                continue

            result = net_profit_ctf_merge(yes_ask, no_ask, category=category)
            if result["net_profit"] >= min_profit:
                opp["prices"] = f"Y={yes_ask:.3f} N={no_ask:.3f}"
                opp["total_cost"] = f"${yes_ask + no_ask:.4f}"
                opp["gross_spread"] = f"{result['gross_spread']:.4f}"
                opp["fees"] = f"${result['fees']:.4f}"
                opp["net_profit"] = result["net_profit"]
                opp["net_roi"] = f"{result['net_profit'] / (yes_ask + no_ask) * 100:.2f}%"
                opp["_clob_depth"] = min(
                    clob.get("yes_ask_size") or 0,
                    clob.get("no_ask_size") or 0,
                )
                opp["_clob_refined"] = True
                refined.append(opp)
            else:
                if funnel:
                    if result.get("gross_spread", 0) > 0:
                        funnel.record_fee_dropped(1)
                    else:
                        funnel.record_clob_dropped(1)

        elif opp_type == "CTFMint":
            yes_bid = clob.get("yes_bid")
            no_bid = clob.get("no_bid")
            if yes_bid is None or no_bid is None:
                opp["_clob_refined"] = False
                refined.append(opp)
                continue

            result = net_profit_ctf_mint(yes_bid, no_bid, category=category)
            if result["net_profit"] >= min_profit:
                opp["prices"] = f"Y={yes_bid:.3f} N={no_bid:.3f}"
                opp["total_cost"] = "$1.0000"
                opp["gross_spread"] = f"{result['gross_spread']:.4f}"
                opp["fees"] = f"${result['fees']:.4f}"
                opp["net_profit"] = result["net_profit"]
                opp["net_roi"] = f"{result['net_profit'] / 1.0 * 100:.2f}%"
                opp["_clob_depth"] = min(
                    clob.get("yes_bid_size") or 0,
                    clob.get("no_bid_size") or 0,
                )
                opp["_clob_refined"] = True
                refined.append(opp)
            else:
                if funnel:
                    if result.get("gross_spread", 0) > 0:
                        funnel.record_fee_dropped(1)
                    else:
                        funnel.record_clob_dropped(1)

    dropped = len(opportunities) - len(refined)
    if dropped:
        logger.info("Dropped %d CTF candidates at CLOB prices.", dropped)
    return refined


def scan_ctf(
    markets: list[dict],
    min_profit: float | None = None,
    price_cache: dict | None = None,
    funnel=None,
    enable_merge: bool | None = None,
    enable_mint: bool | None = None,
) -> list[dict]:
    """Scan for CTF Merge and Mint arbitrage on Polymarket binary markets.

    Args:
        markets: Polymarket market dictionaries.
        min_profit: Minimum net profit in dollars (defaults to CTF_MIN_PROFIT).
        price_cache: Shared WebSocket price cache.
        funnel: Optional FunnelTracker instance.
        enable_merge: Explicit override to enable/disable CTFMerge scanning.
        enable_mint: Explicit override to enable/disable CTFMint scanning.

    Returns:
        List of opportunity dictionaries.
    """
    if min_profit is None:
        min_profit = CTF_MIN_PROFIT

    if enable_merge is None:
        enable_merge = CTF_MERGE_ENABLED or (not CTF_MINT_SELL_ENABLED)
    if enable_mint is None:
        enable_mint = CTF_MINT_SELL_ENABLED or (not CTF_MERGE_ENABLED)

    if not (enable_merge or enable_mint):
        return []

    if funnel is None:
        try:
            from funnel import get_funnel_tracker
            funnel = get_funnel_tracker()
        except ImportError:
            funnel = None

    opportunities: list[dict] = []
    markets_by_question: dict[str, dict] = {}

    binary_markets = get_binary_markets(markets)
    logger.info("Scanning %d binary markets for CTF primitives...", len(binary_markets))
    if funnel:
        funnel.record_screened(len(binary_markets))

    filtered_resolution = 0
    for m in binary_markets:
        if not _is_ctf_market_eligible(m, max_days=CTF_MAX_RESOLUTION_DAYS):
            filtered_resolution += 1
            continue

        market_key = m.get("conditionId") or m.get("condition_id") or m.get("question", "")
        token_ids = _extract_token_ids(m)
        category = m.get("category")
        condition_id = m.get("conditionId") or m.get("condition_id") or market_key

        cached_yes, cached_no = _get_cached_ws_book(token_ids, price_cache)
        if cached_yes and cached_no:
            yes_ask = cached_yes.get("best_ask")
            no_ask = cached_no.get("best_ask")
            yes_bid = cached_yes.get("best_bid")
            no_bid = cached_no.get("best_bid")
            yes_ask_size = cached_yes.get("best_ask_size", 0) or 0
            no_ask_size = cached_no.get("best_ask_size", 0) or 0
            yes_bid_size = cached_yes.get("best_bid_size", 0) or 0
            no_bid_size = cached_no.get("best_bid_size", 0) or 0

            # 1. CTFMerge candidate via WS cache (buy YES ask + buy NO ask < 1.0)
            if (
                enable_merge
                and yes_ask is not None
                and no_ask is not None
                and yes_ask > 0.001
                and no_ask > 0.001
                and (yes_ask + no_ask < 1.0)
            ):
                res_merge = net_profit_ctf_merge(yes_ask, no_ask, category=category)
                if res_merge["net_profit"] >= min_profit:
                    markets_by_question[market_key] = m
                    opportunities.append({
                        "type": "CTFMerge",
                        "_layer": 1,
                        "market": m.get("question", m.get("title", "Unknown"))[:60],
                        "category": category,
                        "prices": f"Y={yes_ask:.3f} N={no_ask:.3f}",
                        "total_cost": f"${yes_ask + no_ask:.4f}",
                        "gross_spread": f"{res_merge['gross_spread']:.4f}",
                        "fees": f"${res_merge['fees']:.4f}",
                        "net_profit": res_merge["net_profit"],
                        "net_roi": f"{res_merge['net_profit'] / (yes_ask + no_ask) * 100:.2f}%",
                        "volume": f"${float(m.get('volume', 0) or 0):,.0f}",
                        "_market_key": market_key,
                        "_condition_id": condition_id,
                        "_token_ids": token_ids,
                        "_action": "merge",
                        "_clob_depth": min(yes_ask_size, no_ask_size),
                        "_price_source": "ws_cache",
                        "_days_to_resolution": _days_to_resolution(m, "polymarket"),
                    })

            # 2. CTFMint candidate via WS cache (sell YES bid + sell NO bid > 1.0)
            if (
                enable_mint
                and yes_bid is not None
                and no_bid is not None
                and yes_bid > 0.001
                and no_bid > 0.001
                and (yes_bid + no_bid > 1.0)
            ):
                res_mint = net_profit_ctf_mint(yes_bid, no_bid, category=category)
                if res_mint["net_profit"] >= min_profit:
                    markets_by_question[market_key] = m
                    opportunities.append({
                        "type": "CTFMint",
                        "_layer": 1,
                        "market": m.get("question", m.get("title", "Unknown"))[:60],
                        "category": category,
                        "prices": f"Y={yes_bid:.3f} N={no_bid:.3f}",
                        "total_cost": "$1.0000",
                        "gross_spread": f"{res_mint['gross_spread']:.4f}",
                        "fees": f"${res_mint['fees']:.4f}",
                        "net_profit": res_mint["net_profit"],
                        "net_roi": f"{res_mint['net_profit'] / 1.0 * 100:.2f}%",
                        "volume": f"${float(m.get('volume', 0) or 0):,.0f}",
                        "_market_key": market_key,
                        "_condition_id": condition_id,
                        "_token_ids": token_ids,
                        "_action": "mint",
                        "_clob_depth": min(yes_bid_size, no_bid_size),
                        "_price_source": "ws_cache",
                        "_days_to_resolution": _days_to_resolution(m, "polymarket"),
                    })
        else:
            # Fall back to mid-prices when WS cache is not available
            prices = parse_outcome_prices(m)
            if not prices or len(prices) != 2:
                continue

            yes_price, no_price = prices[0], prices[1]

            # Skip illiquid / zero-price markets
            if yes_price <= 0.001 or no_price <= 0.001:
                continue

            # Skip resolved markets
            if (yes_price >= 0.99 or no_price >= 0.99) and (yes_price + no_price) > 0.98:
                continue

            # 1. CTFMerge candidate: mid-price sum < 1.0
            if enable_merge and (yes_price + no_price < 1.0):
                res_merge = net_profit_ctf_merge(yes_price, no_price, category=category)
                if res_merge["net_profit"] >= min_profit:
                    markets_by_question[market_key] = m
                    opportunities.append({
                        "type": "CTFMerge",
                        "_layer": 1,
                        "market": m.get("question", m.get("title", "Unknown"))[:60],
                        "category": category,
                        "prices": f"Y={yes_price:.3f} N={no_price:.3f}",
                        "total_cost": f"${yes_price + no_price:.4f}",
                        "gross_spread": f"{res_merge['gross_spread']:.4f}",
                        "fees": f"${res_merge['fees']:.4f}",
                        "net_profit": res_merge["net_profit"],
                        "net_roi": f"{res_merge['net_profit'] / (yes_price + no_price) * 100:.2f}%",
                        "volume": f"${float(m.get('volume', 0) or 0):,.0f}",
                        "_market_key": market_key,
                        "_condition_id": condition_id,
                        "_token_ids": token_ids,
                        "_action": "merge",
                        "_price_source": "rest_mid",
                        "_days_to_resolution": _days_to_resolution(m, "polymarket"),
                    })

            # 2. CTFMint candidate: mid-price sum > 1.0
            if enable_mint and (yes_price + no_price > 1.0):
                res_mint = net_profit_ctf_mint(yes_price, no_price, category=category)
                if res_mint["net_profit"] >= min_profit:
                    markets_by_question[market_key] = m
                    opportunities.append({
                        "type": "CTFMint",
                        "_layer": 1,
                        "market": m.get("question", m.get("title", "Unknown"))[:60],
                        "category": category,
                        "prices": f"Y={yes_price:.3f} N={no_price:.3f}",
                        "total_cost": "$1.0000",
                        "gross_spread": f"{res_mint['gross_spread']:.4f}",
                        "fees": f"${res_mint['fees']:.4f}",
                        "net_profit": res_mint["net_profit"],
                        "net_roi": f"{res_mint['net_profit'] / 1.0 * 100:.2f}%",
                        "volume": f"${float(m.get('volume', 0) or 0):,.0f}",
                        "_market_key": market_key,
                        "_condition_id": condition_id,
                        "_token_ids": token_ids,
                        "_action": "mint",
                        "_price_source": "rest_mid",
                        "_days_to_resolution": _days_to_resolution(m, "polymarket"),
                    })

    if filtered_resolution:
        logger.info(
            "Filtered %d/%d binary markets outside resolution window for CTF.",
            filtered_resolution,
            len(binary_markets),
        )

    if funnel:
        funnel.record_mid_candidates(len(opportunities))
        funnel.record_clob_evaluated(len(opportunities))

    # Stage 2: Refine with CLOB order book prices
    opportunities = _refine_ctf_with_clob(
        opportunities,
        markets_by_question,
        min_profit,
        price_cache=price_cache,
        funnel=funnel,
    )

    opportunities = filter_dust(opportunities, min_amount=min_profit)

    return opportunities
