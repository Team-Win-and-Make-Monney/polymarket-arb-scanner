"""Fréchet-Bound Logical / Conditional Arbitrage Scan.

Scans for coherence violations between implying / nested prediction markets:
    A ⊆ B  ⟹  P(A) ≤ P(B) must hold.

When P(A) > P(B), a locked Dutch book is constructed by:
    BUY YES on B (superset)
    BUY NO on A (subset)

Payoff structure across all reachable states (A ⊆ B forbids A=1, B=0):
    A=1, B=1: YES_B pays $1, NO_A pays $0  ⟹  $1.00
    A=0, B=1: YES_B pays $1, NO_A pays $1  ⟹  $2.00
    A=0, B=0: YES_B pays $0, NO_A pays $1  ⟹  $1.00

Minimum guaranteed payout is $1.00 regardless of outcome.
Cost = P(YES_B) + P(NO_A) = P(B) + (1 - P(A)) = 1 + P(B) - P(A) < $1.00.
Risk-free profit ≥ P(A) - P(B) - fees.
"""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor, as_completed

from fees import net_profit_frechet_implication
from pair_relations import discover_subset_pairs
from polymarket_api import parse_outcome_prices
from scans.helpers import (
    _extract_token_ids,
    _fetch_clob_for_market,
    _days_to_resolution,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Stage 1: Fast Mid-Price Candidate Detection
# ---------------------------------------------------------------------------

def scan_frechet(
    markets: list[dict] | dict[str, dict],
    pairs: list[dict] | None = None,
    min_profit: float = 0.01,
    min_violation: float = 0.02,
    manual_rules: list[dict] | None = None,
    funnel=None,
    platform: str = "polymarket",
) -> list[dict]:
    """Scan for Fréchet implication bounds violations P(A) > P(B) for A ⊆ B.

    Args:
        markets: List or dictionary of market dictionaries.
        pairs: Optional pre-discovered subset pairs. If None, runs discovery.
        min_profit: Minimum net profit required (USD per share).
        min_violation: Minimum raw probability gap P(A) - P(B) to consider.
        manual_rules: Optional list of curated subset/superset rule dicts.
        funnel: Optional ScanFunnelTracker for recording pipeline metrics.
        platform: Target venue ("polymarket" or "kalshi").

    Returns:
        List of candidate opportunity dictionaries.
    """
    if pairs is None:
        pairs = discover_subset_pairs(
            markets,
            manual_rules=manual_rules,
            same_platform_only=True,
            platform=platform,
        )

    if funnel:
        funnel.record_screened(len(pairs))

    candidates: list[dict] = []

    for pair in pairs:
        sub_mkt = pair["sub"]
        sup_mkt = pair["sup"]
        mkt_platform = pair.get("platform", platform)

        # 1. Extract YES probability for both legs
        p_a: float | None = None
        p_b: float | None = None

        if mkt_platform == "polymarket":
            sub_prices = parse_outcome_prices(sub_mkt)
            sup_prices = parse_outcome_prices(sup_mkt)
            if sub_prices and len(sub_prices) >= 1:
                p_a = sub_prices[0]
            if sup_prices and len(sup_prices) >= 1:
                p_b = sup_prices[0]
        elif mkt_platform == "kalshi":
            p_a = sub_mkt.get("yes_bid") or sub_mkt.get("yes_price") or sub_mkt.get("price")
            p_b = sup_mkt.get("yes_ask") or sup_mkt.get("yes_price") or sup_mkt.get("price")

        if p_a is None or p_b is None:
            continue

        # Skip degenerate prices near 0 or 1
        if p_a <= 0.001 or p_b <= 0.001 or p_a >= 0.999 or p_b >= 0.999:
            continue

        # Check violation: P(A) > P(B)
        spread = p_a - p_b
        if spread < min_violation:
            continue

        if funnel:
            funnel.record_mid_candidates(1)

        # Run fee & payout model
        category = sub_mkt.get("category") or sup_mkt.get("category")
        result = net_profit_frechet_implication(
            p_a=p_a,
            p_b=p_b,
            platform=mkt_platform,
            category=category,
        )

        net_profit = result["net_profit"]
        if net_profit < min_profit:
            if funnel:
                funnel.record_fee_dropped(1)
            continue

        # Extract tokens/tickers for execution
        sub_tokens = _extract_token_ids(sub_mkt) if mkt_platform == "polymarket" else []
        sup_tokens = _extract_token_ids(sup_mkt) if mkt_platform == "polymarket" else []

        buy_yes_token = sup_tokens[0] if len(sup_tokens) > 0 else ""
        buy_no_token = sub_tokens[1] if len(sub_tokens) > 1 else ""

        sub_title = (
            sub_mkt.get("title")
            or sub_mkt.get("question")
            or sub_mkt.get("ticker", "Sub")
        )
        sup_title = (
            sup_mkt.get("title")
            or sup_mkt.get("question")
            or sup_mkt.get("ticker", "Sup")
        )

        days_sub = _days_to_resolution(sub_mkt, platform=mkt_platform)
        days_sup = _days_to_resolution(sup_mkt, platform=mkt_platform)
        if days_sub is not None and days_sup is not None:
            days_to_res = max(days_sub, days_sup)
        else:
            days_to_res = days_sub if days_sub is not None else days_sup

        frechet_cids = [
            mkt.get("conditionId") or mkt.get("condition_id")
            for mkt in (sub_mkt, sup_mkt)
            if mkt and (mkt.get("conditionId") or mkt.get("condition_id"))
        ]

        candidates.append({
            "type": "FrechetArb",
            "_layer": 1,
            "market": f"{sub_title[:28]} ⊆ {sup_title[:28]}",
            "prices": f"P(A)={p_a:.3f} > P(B)={p_b:.3f}",
            "total_cost": f"${result['total_cost']:.4f}",
            "net_profit": net_profit,
            "net_roi": result["net_roi"],
            "confidence": pair.get("confidence", 1.0),
            "_platform": mkt_platform,
            "_source": pair.get("source", "strike"),
            "_sub_market": sub_mkt,
            "_sup_market": sup_mkt,
            "_buy_yes_market": sup_mkt,
            "_buy_yes_token": buy_yes_token,
            "_buy_no_market": sub_mkt,
            "_buy_no_token": buy_no_token,
            "_buy_yes_ticker": sup_mkt.get("ticker", ""),
            "_buy_no_ticker": sub_mkt.get("ticker", ""),
            "_p_a": p_a,
            "_p_b": p_b,
            "_token_ids": [buy_yes_token, buy_no_token] if buy_yes_token and buy_no_token else [],
            "_condition_ids": frechet_cids,
            "_condition_id": frechet_cids[0] if frechet_cids else "",
            "_days_to_resolution": days_to_res,
            "_clob_depth": None,
        })

    return candidates


# ---------------------------------------------------------------------------
# Stage 2: CLOB Orderbook Refinement
# ---------------------------------------------------------------------------

def _refine_frechet_with_clob(
    candidates: list[dict],
    min_profit: float = 0.01,
    price_cache: dict | None = None,
    funnel=None,
) -> list[dict]:
    """Stage 2: Re-check Fréchet candidates using actual CLOB ask prices.

    For Polymarket:
      Cost to buy YES on B is sup_clob['yes_ask'].
      Cost to buy NO on A is sub_clob['no_ask'].
      Total cost = yes_ask_B + no_ask_A.
      Effective P(B) = yes_ask_B; effective P(A) = 1.0 - no_ask_A.
    """
    if not candidates:
        return candidates

    logger.info("Refining %d Fréchet candidates with CLOB ask prices...", len(candidates))

    # Pre-fetch Polymarket CLOB books in parallel
    poly_tasks = {}
    for opp in candidates:
        if opp.get("_platform") == "polymarket":
            sub_m = opp.get("_sub_market")
            sup_m = opp.get("_sup_market")
            if sub_m:
                poly_tasks[id(sub_m)] = sub_m
            if sup_m:
                poly_tasks[id(sup_m)] = sup_m

    clob_cache: dict[int, dict] = {}
    if poly_tasks:
        with ThreadPoolExecutor(max_workers=4) as pool:
            futures = {
                pool.submit(_fetch_clob_for_market, m, price_cache): mid
                for mid, m in poly_tasks.items()
            }
            for future in as_completed(futures):
                mid = futures[future]
                try:
                    _, clob = future.result()
                    if clob:
                        clob_cache[mid] = clob
                except Exception as e:
                    logger.debug("CLOB fetch failed for Frechet refinement: %s", e)

    refined: list[dict] = []

    for opp in candidates:
        if funnel:
            funnel.record_clob_evaluated(1)

        mkt_platform = opp.get("_platform", "polymarket")
        if mkt_platform == "polymarket":
            sub_m = opp.get("_sub_market")
            sup_m = opp.get("_sup_market")
            sub_clob = clob_cache.get(id(sub_m)) if sub_m else None
            sup_clob = clob_cache.get(id(sup_m)) if sup_m else None

            if not sub_clob or not sup_clob:
                if funnel:
                    funnel.record_clob_dropped(1)
                continue

            b_yes_ask = sup_clob.get("yes_ask")
            a_no_ask = sub_clob.get("no_ask")

            if b_yes_ask is None or a_no_ask is None or b_yes_ask <= 0 or a_no_ask <= 0:
                if funnel:
                    funnel.record_clob_dropped(1)
                continue

            # Effective probabilities from executable ask prices
            p_b_ask = b_yes_ask
            p_a_implied = 1.0 - a_no_ask

            category = (sub_m.get("category") if sub_m else None) or (sup_m.get("category") if sup_m else None)
            result = net_profit_frechet_implication(
                p_a=p_a_implied,
                p_b=p_b_ask,
                platform="polymarket",
                category=category,
            )

            if result["net_profit"] < min_profit:
                if funnel:
                    funnel.record_clob_dropped(1)
                continue

            depth_b = sup_clob.get("yes_ask_size") or sup_clob.get("yes_depth") or 0.0
            depth_a = sub_clob.get("no_ask_size") or sub_clob.get("no_depth") or 0.0
            # Unknown depth is 0, never None: every consumer (priority sort, --min-depth,
            # RiskManager depth gate) compares it numerically, and 0 fails closed.
            exec_depth = min(depth_b, depth_a) if (depth_b > 0 and depth_a > 0) else 0.0

            opp_copy = dict(opp)
            opp_copy["_p_a"] = p_a_implied
            opp_copy["_p_b"] = p_b_ask
            opp_copy["prices"] = f"Ask(B)={p_b_ask:.3f} + Ask(¬A)={a_no_ask:.3f}"
            opp_copy["total_cost"] = f"${result['total_cost']:.4f}"
            opp_copy["net_profit"] = result["net_profit"]
            opp_copy["net_roi"] = result["net_roi"]
            opp_copy["_clob_depth"] = exec_depth
            opp_copy["_clob_refined"] = True

            if funnel:
                funnel.record_surfaced(1)

            refined.append(opp_copy)

        elif mkt_platform == "kalshi":
            # For Kalshi, mid prices from REST are executable snapshot quotes
            if funnel:
                funnel.record_surfaced(1)
            refined.append(opp)

    return refined
