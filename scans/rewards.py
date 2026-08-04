"""Liquidity rewards scan for Polymarket and Kalshi reward programs."""

import json
import logging
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date
from decimal import Decimal, InvalidOperation, ROUND_CEILING, ROUND_FLOOR

from fees import polymarket_maker_rebate
from scans.helpers import _fetch_clob_for_market

logger = logging.getLogger(__name__)


def _active_reward_allocations(allocations: object, today: date | None = None) -> list[dict]:
    """Return current Polymarket CLOB reward allocations.

    Gamma exposes reward allocations at ``clobRewards`` with ISO date fields.
    Malformed allocations fail closed so an old or ambiguous program cannot be
    surfaced as current economics.
    """
    if not isinstance(allocations, list):
        return []
    current = today or date.today()
    active = []
    for allocation in allocations:
        if not isinstance(allocation, dict):
            continue
        try:
            start = date.fromisoformat(str(allocation["startDate"])[:10])
            end = date.fromisoformat(str(allocation["endDate"])[:10])
            daily_rate = float(allocation.get("rewardsDailyRate") or 0)
        except (KeyError, TypeError, ValueError):
            continue
        if start <= current <= end and daily_rate > 0:
            active.append(allocation)
    return active


def _extract_polymarket_reward_metadata(market: dict) -> dict:
    """Normalize current Gamma reward fields into the scanner schema.

    The current API uses top-level ``rewardsMinSize``,
    ``rewardsMaxSpread`` (cents), and ``clobRewards``. Older fixtures and
    callers use the scanner's nested ``incentives`` representation, so both
    are accepted during migration.
    """
    legacy = market.get("incentives")
    if isinstance(legacy, dict) and legacy:
        return dict(legacy)

    allocations = _active_reward_allocations(market.get("clobRewards"))
    if not allocations:
        return {}
    try:
        min_size = float(market.get("rewardsMinSize"))
        max_spread = float(market.get("rewardsMaxSpread")) / 100.0
        daily_rate = sum(float(a.get("rewardsDailyRate") or 0) for a in allocations)
    except (TypeError, ValueError):
        return {}
    return {
        "min_incentive_size": min_size,
        "max_incentive_spread": max_spread,
        "pool_size_usdc": daily_rate,
        "reward_daily_rate_usdc": daily_rate,
        "allocations": allocations,
    }


def _parse_yes_price(market: dict) -> float | None:
    """Parse the first Gamma outcome price from list or JSON-string form."""
    raw = market.get("outcomePrices")
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError:
            return None
    if not isinstance(raw, list) or len(raw) < 2:
        return None
    try:
        price = float(raw[0])
    except (TypeError, ValueError):
        return None
    return price if 0 < price < 1 else None


def _quantize_kalshi_price(price: float, price_ranges: list[dict], *, round_up: bool) -> float | None:
    """Round a Kalshi quote onto the market-specific dynamic price grid."""
    ranges = price_ranges or [{"start": "0.00", "end": "1.00", "step": "0.01"}]
    try:
        candidate = Decimal(str(price))
    except InvalidOperation:
        return None
    for price_range in ranges:
        try:
            start = Decimal(str(price_range["start"]))
            end = Decimal(str(price_range["end"]))
            step = Decimal(str(price_range["step"]))
        except (InvalidOperation, KeyError, TypeError):
            continue
        if step <= 0 or not (start <= candidate <= end):
            continue
        rounding = ROUND_CEILING if round_up else ROUND_FLOOR
        units = ((candidate - start) / step).to_integral_value(rounding=rounding)
        quantized = start + units * step
        if start <= quantized <= end and Decimal("0") < quantized < Decimal("1"):
            return float(quantized)
    return None


def _validate_reward_metadata(reward_dict: dict) -> bool:
    """Validate reward metadata fields are present and sane.

    Args:
        reward_dict: Reward metadata from Markets API.

    Returns:
        True if all fields are valid, False otherwise.
    """
    if not reward_dict:
        return False

    min_size = reward_dict.get("min_incentive_size")
    max_spread = reward_dict.get("max_incentive_spread")
    pool_size = reward_dict.get("pool_size_usdc", 0)

    # Validate ranges
    if min_size is None or min_size <= 0:
        return False
    if max_spread is None or max_spread <= 0 or max_spread > 0.50:
        return False
    if pool_size < 0:
        return False

    return True


def _calculate_optimal_quotes(reward_info: dict, mid_price: float, inventory: float = 0.0) -> dict:
    """Calculate optimal bid/ask quotes for reward qualification.

    Args:
        reward_info: Reward metadata with max_incentive_spread, etc.
        mid_price: Current market mid-price (0-1).
        inventory: Current inventory position (for skew).

    Returns:
        Dict with bid, ask, spread keys.
    """
    max_spread = reward_info.get("max_incentive_spread", 0.05)

    # Target spread: 60% of max for high reward score without competing too hard
    target_spread = max_spread * 0.6
    half_spread = target_spread / 2

    # Inventory skew: when long, encourage selling
    skew = 0.0
    if inventory > 0:
        skew = -target_spread * 0.1

    bid = mid_price - half_spread + skew
    ask = mid_price + half_spread + skew

    # Clamp to valid range
    bid = max(0.01, min(0.99, bid))
    ask = max(0.01, min(0.99, ask))

    return {
        "bid": round(bid, 4),
        "ask": round(ask, 4),
        "spread": round(ask - bid, 4),
    }


def _refine_rewards_with_clob(opportunities: list[dict], markets_by_key: dict,
                              price_cache: dict | None = None) -> list[dict]:
    """Stage 2: Verify optimal quotes are achievable against live CLOB.

    Checks that optimal bids/asks don't cross the market and that depth is sufficient.

    Args:
        opportunities: Candidates from stage 1.
        markets_by_key: Market objects indexed by market key.
        price_cache: Optional WS price cache.

    Returns:
        Refined opportunities (may drop some if CLOB unavailable or crosses).
    """
    if not opportunities:
        return opportunities

    logger.info("Refining %d reward candidates with CLOB depth check...", len(opportunities))

    # Pre-fetch CLOB prices in parallel
    fetch_tasks = {}
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
                    logger.debug("CLOB fetch failed for reward refinement: %s", e)

    refined = []
    for opp in opportunities:
        market_key = opp.get("_market_key")
        market = markets_by_key.get(market_key) if market_key else None
        if not market:
            refined.append(opp)
            continue

        clob = clob_results.get(market_key)
        if not clob or clob["yes_bid"] is None or clob["yes_ask"] is None:
            # CLOB unavailable: keep opportunity (graceful degradation)
            opp["_clob_refined"] = False
            refined.append(opp)
            continue

        # Check depth: minimum $5 on each side
        min_depth = 5.0
        yes_bid_depth = clob.get("yes_bid_size", 0) or 0
        yes_ask_depth = clob.get("yes_ask_size", 0) or 0

        if yes_bid_depth < min_depth or yes_ask_depth < min_depth:
            logger.debug(
                "Dropping reward opportunity on %s: insufficient depth (bid=%s, ask=%s)",
                market_key,
                yes_bid_depth,
                yes_ask_depth,
            )
            continue

        # Verify optimal quotes don't cross market
        optimal_bid = opp.get("optimal_bid", 0)
        optimal_ask = opp.get("optimal_ask", 0)
        clob_mid = (clob["yes_bid"] + clob["yes_ask"]) / 2

        if optimal_bid > clob["yes_ask"] or optimal_ask < clob["yes_bid"]:
            logger.debug(
                "Dropping reward opportunity on %s: quotes cross market",
                market_key,
            )
            continue

        opp["_clob_refined"] = True
        opp["_clob_depth"] = min(yes_bid_depth, yes_ask_depth)
        refined.append(opp)

    dropped = len(opportunities) - len(refined)
    if dropped:
        logger.info("Dropped %d reward candidates at CLOB validation.", dropped)
    return refined


def scan_polymarket_rewards(markets: list[dict], reward_tracker, min_pool_usdc: float = 10.0,
                            price_cache: dict | None = None) -> list[dict]:
    """Scan for Polymarket reward-eligible markets and generate resting order opportunities.

    Two-stage scan:
    1. Filter markets with active reward programs and sufficient pool size.
    2. Refine with CLOB depth check.

    Args:
        markets: List of Polymarket market objects from Markets API.
        reward_tracker: RewardTracker instance for calculating optimal spreads.
        min_pool_usdc: Minimum reward pool size to include market (default $10).
        price_cache: Optional WS price cache for faster lookup.

    Returns:
        List of reward opportunity dicts.
    """
    opportunities = []
    markets_by_key = {}

    logger.info("Scanning %d Polymarket markets for reward programs...", len(markets))

    filtered_no_incentives = 0
    filtered_small_pool = 0
    filtered_invalid_metadata = 0

    for market in markets:
        market_key = market.get("conditionId", market.get("question", ""))
        if not market_key:
            continue

        # Stage 1: Check for active reward program
        incentives = _extract_polymarket_reward_metadata(market)
        if not incentives:
            filtered_no_incentives += 1
            continue

        # Validate reward metadata
        if not _validate_reward_metadata(incentives):
            filtered_invalid_metadata += 1
            logger.debug("Invalid reward metadata on %s: %s", market_key, incentives)
            continue

        pool_size = incentives.get("pool_size_usdc", 0)
        if pool_size < min_pool_usdc:
            filtered_small_pool += 1
            continue

        min_size = incentives.get("min_incentive_size", 5.0)

        # Get mid-price from cache or API
        mid_price = None
        if price_cache:
            cached = price_cache.get(market_key, {})
            yes_bid = cached.get("yes_bid")
            yes_ask = cached.get("yes_ask")
            if yes_bid is not None and yes_ask is not None:
                mid_price = (yes_bid + yes_ask) / 2

        # Fall back to market mid-price
        if mid_price is None:
            mid_price = _parse_yes_price(market)
            if mid_price is None:
                continue

        # Calculate optimal quotes using reward tracker
        optimal = _calculate_optimal_quotes(incentives, mid_price, inventory=0.0)

        # Check if single-sided is allowed based on midpoint range
        single_sided_ok = 0.10 <= mid_price <= 0.90

        # Maker-rebate stream (stacks with liquidity rewards): expected rebate
        # income per contract if the resting bid fills. Category drives both
        # the taker rate and the rebate share (25%, crypto 20%).
        category = market.get("category") or ""
        rebate_per_contract = polymarket_maker_rebate(optimal["bid"], 1, category=category)
        min_size_rebate = rebate_per_contract * min_size
        min_size_cost = optimal["bid"] * min_size

        # Create opportunity
        markets_by_key[market_key] = market
        opportunities.append({
            "type": "PolymarketRewards",
            "_layer": 3,  # Layer 3: market making / liquidity provision
            "market": market.get("question", market.get("title", "Unknown"))[:60],
            "platform": "polymarket",
            "reward_pool_usdc": pool_size,
            "min_size": min_size,
            "optimal_bid": optimal["bid"],
            "optimal_ask": optimal["ask"],
            "optimal_spread": optimal["spread"],
            "single_sided_ok": single_sided_ok,
            "maker_rebate_per_contract": rebate_per_contract,
            "estimated_maker_rebate_at_min_size": min_size_rebate,
            "total_cost": f"${min_size_cost:.4f}",
            "net_profit": min_size_rebate,
            "net_roi": min_size_rebate / min_size_cost if min_size_cost > 0 else 0.0,
            "reward_daily_rate_usdc": incentives.get("reward_daily_rate_usdc", pool_size),
            "_category": category,
            "_reward_allocations": incentives.get("allocations", []),
            "_market_key": market_key,
            "_market_volume": float(market.get("volume", 0) or 0),
        })

    if filtered_no_incentives:
        logger.info("Filtered %d markets without reward programs.", filtered_no_incentives)
    if filtered_small_pool:
        logger.info("Filtered %d markets with reward pool < $%.2f.", filtered_small_pool, min_pool_usdc)
    if filtered_invalid_metadata:
        logger.info("Filtered %d markets with invalid reward metadata.", filtered_invalid_metadata)

    # Stage 2: Refine with CLOB depth check
    opportunities = _refine_rewards_with_clob(opportunities, markets_by_key, price_cache=price_cache)

    logger.info("Found %d Polymarket reward opportunities.", len(opportunities))
    return opportunities


def scan_kalshi_rewards(kalshi_client, reward_tracker, min_pool_usdc: float = 10.0,
                        kalshi_data: tuple | None = None) -> list[dict]:
    """Scan for Kalshi reward-eligible markets and generate resting order opportunities.

    Uses Kalshi's public ``/incentive_programs`` endpoint through the canonical
    LIP selector. Volume is not evidence that a market has a reward program.

    Args:
        kalshi_client: KalshiClient instance.
        reward_tracker: KalshiRewardTracker instance for logging orders.
        min_pool_usdc: Minimum active reward pool in dollars.
        kalshi_data: Optional already-fetched Kalshi events/markets tuple.

    Returns:
        List of reward opportunity dicts for Kalshi markets.
    """
    opportunities = []

    logger.info("Scanning Kalshi markets for reward-eligible opportunities...")

    try:
        from scans.lip_select import select_lip_markets

        selected = select_lip_markets(kalshi_client, kalshi_data=kalshi_data)
        for market in selected:
            pool = float(market.get("pool_dollars") or 0)
            if pool < min_pool_usdc:
                continue
            ticker = market.get("ticker")
            mid_price = market.get("mid")
            if not ticker or not isinstance(mid_price, (int, float)):
                continue
            target_spread = max(0.02, min(0.05, mid_price * 0.03))
            price_ranges = market.get("price_ranges") or []
            bid = _quantize_kalshi_price(
                max(0.01, mid_price - target_spread / 2), price_ranges, round_up=False)
            ask = _quantize_kalshi_price(
                min(0.99, mid_price + target_spread / 2), price_ranges, round_up=True)
            if bid is None or ask is None or bid >= ask:
                logger.debug("LIP candidate %s has no valid quote pair on price_ranges", ticker)
                continue

            opportunities.append({
                "type": "KalshiRewards",
                "_layer": 3,  # Layer 3: market making
                "market": market.get("title", ticker)[:60],
                "platform": "kalshi",
                "ticker": ticker,
                "market_ticker": ticker,
                "reward_pool_usdc": pool,
                "optimal_bid": round(bid, 4),
                "optimal_ask": round(ask, 4),
                "optimal_spread": round(ask - bid, 4),
                "discount_factor_bps": market.get("discount_factor_bps"),
                "program_end": market.get("program_end"),
                "competition_depth": market.get("competition_depth"),
                "selection_score": market.get("score"),
                "_market_key": ticker,
                "_price_ranges": price_ranges,
            })

    except Exception as e:
        logger.error("Kalshi reward scan failed: %s", e)

    logger.info("Found %d Kalshi reward opportunities.", len(opportunities))
    return opportunities
