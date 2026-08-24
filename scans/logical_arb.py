"""Combinatorial logical arbitrage detection across related Polymarket markets.

Detects semantic inconsistencies where implied outcomes are priced significantly
lower than the implying outcomes. E.g., "Bitcoin >$100k" implies "Bitcoin >$90k",
so if P(>$100k) > P(>$90k), there's an arbitrage opportunity.

Uses config-driven semantic rules in JSON format to identify related market pairs.
Two-stage detection: Stage 1 scans mid-price candidates, Stage 2 refines with CLOB.
"""

import json
import logging

from config import POLYMARKET_DEFAULT_TAKER_RATE
from fees import net_profit_logical_arb
from scans.helpers import _extract_token_ids
from polymarket_api import fetch_order_book

logger = logging.getLogger(__name__)


def scan_logical_arb(
    markets_by_key: dict,
    logical_arb_rules: list[dict],
    price_threshold: float = 0.05,
) -> list[dict]:
    """Stage 1: Scan for semantic rule violations at mid prices.

    Detects opportunities where an implied outcome is priced significantly lower
    than an implying outcome. E.g., if "Bitcoin >$100k" implies "Bitcoin >$90k",
    and P(>$90k) < P(>$100k) * (1 - price_threshold), then buy >$90k and sell >$100k.

    Args:
        markets_by_key: Dict mapping market keys (e.g., "polymarket-{market_id}") to
                       market dicts with price, clobTokenIds, question, etc.
        logical_arb_rules: List of rule dicts with keys:
                          - if_yes: market_id of the implying market (e.g., ">$100k")
                          - then_yes: market_id of the implied market (e.g., ">$90k")
                          - relationship: "implies" (only one relationship type for now)
        price_threshold: Discount threshold (default 0.05 = 5%). Opportunity when
                        then_price < if_price * (1 - threshold).

    Returns:
        List of opportunity dicts with keys: type, market, if_market_id, then_market_id,
        _if_price, _then_price, _token_ids, _market_key, _layer.
    """
    if not logical_arb_rules:
        return []

    opportunities = []

    # Process each rule
    for rule in logical_arb_rules:
        if_market_id = rule.get("if_yes", "")
        then_market_id = rule.get("then_yes", "")
        relationship = rule.get("relationship", "")

        if not if_market_id or not then_market_id:
            continue

        # Only process "implies" relationships for now
        if relationship != "implies":
            continue

        # Look up markets in the cache
        if_market_key = f"polymarket-{if_market_id}"
        then_market_key = f"polymarket-{then_market_id}"

        if_market = markets_by_key.get(if_market_key)
        then_market = markets_by_key.get(then_market_key)

        if not if_market or not then_market:
            logger.debug("Logical arb: market not found for rule %s -> %s", if_market_id, then_market_id)
            continue

        # Extract mid prices
        if_price = if_market.get("price", 0.5)
        then_price = then_market.get("price", 0.5)

        # Check for opportunity: then_price < if_price * (1 - threshold)
        # Rationale: if "A" implies "B", then P(A) <= P(B). If violated by >threshold,
        # buy B and sell A for arbitrage.
        if then_price < if_price * (1 - price_threshold):
            # Extract token IDs for execution
            token_ids = _extract_token_ids(then_market)
            if_token_ids = _extract_token_ids(if_market)
            if len(token_ids) < 2 or len(if_token_ids) < 2:
                logger.debug("Logical arb: missing executable token IDs for %s -> %s", if_market_id, then_market_id)
                continue

            opportunity = {
                "type": "LogicalArb",
                "market": f"{if_market.get('question', '')} → {then_market.get('question', '')}",
                "if_market_id": if_market_id,
                "then_market_id": then_market_id,
                "_if_price": if_price,
                "_then_price": then_price,
                "_token_ids": token_ids,
                "_if_token_ids": if_token_ids,
                "_market_key": then_market_id,
                "_layer": 4,
            }
            opportunities.append(opportunity)

    # Stage 2: Refine with CLOB depth check
    opportunities = _refine_logical_arb_with_clob(opportunities)

    logger.info("Logical arb: found %d opportunities from %d rules", len(opportunities), len(logical_arb_rules))
    return opportunities


# ---------------------------------------------------------------------------
# Stage 2: CLOB Refinement
# ---------------------------------------------------------------------------


def _refine_logical_arb_with_clob(opportunities: list[dict]) -> list[dict]:
    """Stage 2: Validate prices against live CLOB order book.

    Re-fetches CLOB order book for each opportunity and checks that the ask price
    for the underpriced outcome hasn't blown out >30% from Stage 1 estimate.
    Drops opportunities where spread widened significantly (indicates stale or
    spoofed order book).

    Args:
        opportunities: List of opportunity dicts from scan_logical_arb() with
                      _token_ids and _then_price keys.

    Returns:
        Refined list of opportunities (some may be dropped if spread widened).
    """
    if not opportunities:
        return opportunities

    logger.debug("Refining %d logical arb candidates with CLOB depth check...", len(opportunities))

    refined = []
    for opp in opportunities:
        token_ids = opp.get("_token_ids", [])
        if_token_ids = opp.get("_if_token_ids", [])
        if len(token_ids) < 2 or len(if_token_ids) < 2:
            logger.debug("Dropping logical arb opportunity: incomplete token IDs")
            continue

        stage1_then_price = opp.get("_then_price", 0.0)
        max_ask_price = stage1_then_price * 1.30  # 30% tolerance

        try:
            # Buy the implied market YES and the implying market NO. This
            # synthetic pair pays at least $1 in every logically valid state.
            then_yes_book = fetch_order_book(token_ids[0])
            if_no_book = fetch_order_book(if_token_ids[1])

            if not then_yes_book or not if_no_book:
                logger.debug("Dropping logical arb opportunity: CLOB unavailable")
                continue

            # Get the best ask price from the CLOB
            asks = then_yes_book.get("asks", [])
            if_no_asks = if_no_book.get("asks", [])
            if not asks or not if_no_asks:
                logger.debug("Dropping logical arb opportunity: executable asks unavailable")
                continue

            clob_ask_price = float(asks[0].get("price", stage1_then_price))
            if_no_ask_price = float(if_no_asks[0].get("price", 1.0 - opp.get("_if_price", 0.0)))

            # Check for spread widening: if ask price is >30% higher, drop it
            if clob_ask_price > max_ask_price:
                logger.debug(
                    "Logical arb spread widened: stage1=%.4f, clob_ask=%.4f (>30%% threshold)",
                    stage1_then_price,
                    clob_ask_price,
                )
                continue

            live_profit = net_profit_logical_arb(1.0 - if_no_ask_price, clob_ask_price)
            if live_profit <= 0:
                logger.debug("Logical arb no longer profitable at executable asks: %.4f", live_profit)
                continue

            # Update opportunity with CLOB validated data
            opp["_clob_ask_price"] = clob_ask_price
            opp["_if_no_price"] = if_no_ask_price
            opp["_then_price"] = clob_ask_price
            opp["net_profit"] = live_profit
            opp["_clob_validated"] = True
            refined.append(opp)

        except Exception as e:
            logger.debug("CLOB fetch failed for logical arb refinement: %s", e)

    dropped = len(opportunities) - len(refined)
    if dropped > 0:
        logger.debug("Logical arb: dropped %d candidates in CLOB refinement", dropped)

    return refined
