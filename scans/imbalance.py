"""Order book imbalance detection for Polymarket and Kalshi.

Detects directional signals from bid/ask volume ratios at the top price levels.
When bid volume significantly exceeds ask volume, the market is expected to rise
(buy YES). When ask dominates, expect the market to fall (buy NO).
"""

import logging

logger = logging.getLogger(__name__)


def _calculate_imbalance_ratio(order_book: dict, top_levels: int = 5) -> float:
    """Calculate bid/ask volume imbalance ratio for top N price levels.

    Formula: (total_bid_volume - total_ask_volume) / (total_bid_volume + total_ask_volume)
    Range: -1 (pure ask dominance) to +1 (pure bid dominance)

    Args:
        order_book: CLOB order book dict with 'bids' and 'asks' arrays.
                   Each bid/ask is a dict with 'size' and 'price' keys.
        top_levels: Number of price levels to include in calculation (default 5).

    Returns:
        Imbalance ratio in range [-1, 1], or 0.0 if order book is empty.
    """
    bids = order_book.get("bids", [])[:top_levels]
    asks = order_book.get("asks", [])[:top_levels]

    bid_vol = sum(float(b.get("size", 0)) for b in bids)
    ask_vol = sum(float(a.get("size", 0)) for a in asks)

    if bid_vol + ask_vol == 0:
        return 0.0

    return (bid_vol - ask_vol) / (bid_vol + ask_vol)


def _refine_imbalance_with_clob(opportunities: list[dict]) -> list[dict]:
    """Stage 2: Validate imbalance against live CLOB order book.

    Re-fetches order book for each opportunity and re-calculates imbalance ratio.
    Drops opportunities where imbalance has collapsed by >30% (indicating stale
    or spoofed order book in stage 1).

    Args:
        opportunities: List of opportunity dicts from scan_imbalance() with
                      _token_ids and _imbalance_ratio keys.

    Returns:
        Refined list of opportunities (some may be dropped if imbalance collapsed).
    """
    if not opportunities:
        return opportunities

    from polymarket_api import fetch_order_book

    logger.info("Refining %d imbalance candidates with CLOB depth check...", len(opportunities))

    refined = []
    for opp in opportunities:
        token_ids = opp.get("_token_ids", [])
        if not token_ids:
            # No token IDs: drop it (can't refine)
            logger.debug("Dropping imbalance opportunity: no token IDs")
            continue

        original_ratio = opp.get("_imbalance_ratio", 0.0)
        collapse_threshold = 0.7 * abs(original_ratio)

        try:
            yes_book = fetch_order_book(token_ids[0])

            if not yes_book:
                logger.debug("Dropping imbalance opportunity: CLOB unavailable")
                continue

            current_ratio = _calculate_imbalance_ratio(yes_book, top_levels=5)

            # Check for collapse: if current ratio dropped below 70% of original magnitude
            if abs(current_ratio) < collapse_threshold:
                logger.debug(
                    "Dropping imbalance opportunity: ratio collapsed from %.2f to %.2f (>30%% drop)",
                    original_ratio,
                    current_ratio,
                )
                continue

            # Update opportunity with refined ratio and clob info
            opp["_imbalance_ratio_refined"] = current_ratio
            opp["_clob_validated"] = True
            refined.append(opp)

        except Exception as e:
            logger.debug("CLOB fetch failed for imbalance refinement: %s", e)
            # Fail closed: scan-time imbalance is not executable evidence.
            continue

    dropped = len(opportunities) - len(refined)
    if dropped:
        logger.info("Dropped %d imbalance candidates at CLOB validation (>30%% collapse).", dropped)

    return refined


def scan_imbalance(markets_by_key: dict, min_ratio: float = 3.0,
                   price_cache: dict | None = None) -> list[dict]:
    """Stage 1: Detect order book imbalances on Polymarket and Kalshi markets.

    Scans top 5 bid/ask levels for imbalance ratio >= min_ratio threshold.
    Positive ratio indicates bid dominance (predict YES). Negative ratio indicates
    ask dominance (predict NO).

    Args:
        markets_by_key: Dict of market objects indexed by market key (conditionId).
        min_ratio: Minimum imbalance ratio magnitude to trigger opportunity
                  (default 3.0 = 3:1 bid/ask ratio).
        price_cache: Optional WebSocket price cache (unused in scan, for future expansion).

    Returns:
        List of opportunity dicts with keys:
        - type: "Imbalance"
        - market: Market question text
        - _imbalance_ratio: Calculated ratio [-1, 1]
        - _direction: "YES" if bid dominance, "NO" if ask dominance
        - _token_ids: CLOB token IDs for execution
    """
    opportunities = []

    from scans.helpers import _extract_token_ids
    from polymarket_api import fetch_order_book

    logger.info("Scanning %d markets for order book imbalances...", len(markets_by_key))

    filtered_no_token_ids = 0
    filtered_low_ratio = 0

    for market_key, market in markets_by_key.items():
        # Extract token IDs for Polymarket
        token_ids = _extract_token_ids(market) or market.get("clobTokenIds", [])

        if not token_ids or len(token_ids) < 2:
            filtered_no_token_ids += 1
            continue

        # Fetch YES token order book
        try:
            yes_book = fetch_order_book(token_ids[0])
        except Exception as e:
            logger.debug("Failed to fetch order book for %s: %s", market_key, e)
            continue

        if not yes_book:
            continue

        # Calculate imbalance ratio
        ratio = _calculate_imbalance_ratio(yes_book, top_levels=5)

        # Check if imbalance meets threshold
        # min_ratio of 3.0 means 3:1 bid/ask or ask/bid ratio
        # Imbalance ratio ranges [-1, 1]; threshold conversion:
        # 3:1 ratio (bid:ask) → (3-1)/(3+1) = 0.5 imbalance ratio
        # Convert min_ratio (n:1) to imbalance ratio: (n-1)/(n+1)
        imbalance_threshold = (min_ratio - 1) / (min_ratio + 1)
        if abs(ratio) < imbalance_threshold:
            filtered_low_ratio += 1
            continue

        # Determine predicted direction
        direction = "YES" if ratio > 0 else "NO"

        # Create opportunity
        opportunities.append({
            "type": "Imbalance",
            "market": market.get("question", market.get("title", "Unknown"))[:100],
            "_imbalance_ratio": ratio,
            "_direction": direction,
            "_token_ids": token_ids,
            "_market_key": market_key,
            "_layer": 4,  # Layer 4: informed trading
        })

        logger.info(
            "Imbalance found: %s, ratio=%.2f, direction=%s",
            market.get("question", market_key)[:60],
            ratio,
            direction,
        )

    if filtered_no_token_ids:
        logger.info("Filtered %d markets without token IDs.", filtered_no_token_ids)
    if filtered_low_ratio:
        logger.info("Filtered %d markets with imbalance < %.1f:1 ratio.", filtered_low_ratio, min_ratio)

    # Stage 2: Refine with CLOB depth check
    opportunities = _refine_imbalance_with_clob(opportunities)

    logger.info("Found %d order book imbalance opportunities.", len(opportunities))
    return opportunities
