"""Cross-Date / Nested Temporal Arbitrage Scan (Plan 03).

Scans for date-based monotonicity violations in cumulative 'by-deadline' prediction markets:
    For D_early < D_late:  Event(D_early) ⊆ Event(D_late) ⟹ P(D_early) ≤ P(D_late).

When P(D_early) > P(D_late), constructs a locked Dutch book:
    BUY YES on later deadline D_late (superset)
    BUY NO on earlier deadline D_early (negation of subset)

Guaranteed minimum payout across all reachable states is $1.00.
Cost = P(YES_late) + P(NO_early) < $1.00.
Risk-free edge = (P(early) - P(late)) - fees.
"""

from __future__ import annotations

import logging

from fees import net_profit_frechet_implication
from kalshi_ticker import find_temporal_pairs
from scans.helpers import _days_to_resolution

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Stage 1: Fast Mid-Price Candidate Detection
# ---------------------------------------------------------------------------

def scan_temporal_arb(
    markets: list[dict] | dict[str, dict],
    pairs: list[dict] | None = None,
    min_profit: float = 0.01,
    min_violation: float = 0.02,
    funnel=None,
) -> list[dict]:
    """Scan for cross-date monotonicity bound violations P(D_early) > P(D_late).

    Args:
        markets: List or dictionary of Kalshi market dictionaries.
        pairs: Optional pre-discovered temporal pairs. If None, runs discovery.
        min_profit: Minimum net profit required (USD per contract/share).
        min_violation: Minimum raw probability gap P(early) - P(late) to consider.
        funnel: Optional ScanFunnelTracker for recording pipeline metrics.

    Returns:
        List of candidate opportunity dictionaries.
    """
    if pairs is None:
        pairs = find_temporal_pairs(markets)

    if funnel:
        funnel.record_screened(len(pairs))

    candidates: list[dict] = []

    for pair in pairs:
        early_mkt = pair["sub"]
        late_mkt = pair["sup"]

        # Extract YES probability/price for both deadlines
        # For Kalshi: yes_bid / yes_price for early; yes_ask / yes_price for late
        p_early = early_mkt.get("yes_bid") or early_mkt.get("yes_price") or early_mkt.get("price")
        p_late = late_mkt.get("yes_ask") or late_mkt.get("yes_price") or late_mkt.get("price")

        if p_early is None or p_late is None:
            continue

        # Skip degenerate prices near boundary
        if p_early <= 0.001 or p_late <= 0.001 or p_early >= 0.999 or p_late >= 0.999:
            continue

        spread = p_early - p_late
        if spread < min_violation:
            continue

        if funnel:
            funnel.record_mid_candidates(1)

        result = net_profit_frechet_implication(
            p_a=p_early,
            p_b=p_late,
            platform="kalshi",
        )

        net_profit = result["net_profit"]
        if net_profit < min_profit:
            if funnel:
                funnel.record_fee_dropped(1)
            continue

        days_early = _days_to_resolution(early_mkt, platform="kalshi")
        days_late = _days_to_resolution(late_mkt, platform="kalshi")
        if days_early is not None and days_late is not None:
            days_to_res = max(days_early, days_late)
        else:
            days_to_res = days_late if days_late is not None else days_early

        early_ticker = pair["early_ticker"]
        late_ticker = pair["late_ticker"]
        dt_early = pair["dt_early"]
        dt_late = pair["dt_late"]

        candidates.append({
            "type": "TemporalArb",
            "_layer": 1,
            "market": f"{early_ticker} (early) ⊆ {late_ticker} (late)",
            "prices": f"P(Early)={p_early:.3f} > P(Late)={p_late:.3f}",
            "total_cost": f"${result['total_cost']:.4f}",
            "net_profit": net_profit,
            "net_roi": result["net_roi"],
            "confidence": 1.0,
            "_platform": "kalshi",
            "_source": "temporal",
            "_asset": pair["asset"],
            "_strike": pair["strike"],
            "_direction": pair["direction"],
            "_early_ticker": early_ticker,
            "_late_ticker": late_ticker,
            "_dt_early": dt_early.isoformat() if hasattr(dt_early, "isoformat") else str(dt_early),
            "_dt_late": dt_late.isoformat() if hasattr(dt_late, "isoformat") else str(dt_late),
            "_sub_market": early_mkt,
            "_sup_market": late_mkt,
            "_buy_yes_market": late_mkt,
            "_buy_yes_ticker": late_ticker,
            "_buy_no_market": early_mkt,
            "_buy_no_ticker": early_ticker,
            "_p_early": p_early,
            "_p_late": p_late,
            "_p_a": p_early,
            "_p_b": p_late,
            "_days_to_resolution": days_to_res,
            "_clob_depth": None,
        })

    return candidates


# ---------------------------------------------------------------------------
# Stage 2: CLOB Orderbook Refinement
# ---------------------------------------------------------------------------

def _refine_temporal_with_clob(
    candidates: list[dict],
    min_profit: float = 0.01,
    kalshi_client=None,
    funnel=None,
) -> list[dict]:
    """Stage 2: Re-check temporal arbitrage candidates using real CLOB order books.

    Cost to buy YES on later deadline is late_clob['yes_ask'].
    Cost to buy NO on earlier deadline is early_clob['no_ask'].
    Total cost = yes_ask_late + no_ask_early.
    Effective P(Late) = yes_ask_late; effective P(Early) = 1.0 - no_ask_early.
    """
    if not candidates:
        return candidates

    from kalshi_api import parse_orderbook, best_yes_ask, best_no_ask

    refined: list[dict] = []

    for cand in candidates:
        if funnel:
            funnel.record_clob_evaluated(1)

        late_ticker = cand.get("_late_ticker") or cand.get("_buy_yes_ticker", "")
        early_ticker = cand.get("_early_ticker") or cand.get("_buy_no_ticker", "")

        late_yes_ask: float | None = None
        early_no_ask: float | None = None
        late_depth: float = 0.0
        early_depth: float = 0.0

        if kalshi_client:
            try:
                book_late = kalshi_client.fetch_order_book(late_ticker)
                if book_late:
                    parsed_late = parse_orderbook(book_late)
                    yes_tup = best_yes_ask(parsed_late)
                    if yes_tup is not None:
                        late_yes_ask, late_depth = yes_tup
            except Exception as e:
                logger.debug("Failed to fetch Kalshi orderbook for late ticker %s: %s", late_ticker, e)

            try:
                book_early = kalshi_client.fetch_order_book(early_ticker)
                if book_early:
                    parsed_early = parse_orderbook(book_early)
                    no_tup = best_no_ask(parsed_early)
                    if no_tup is not None:
                        early_no_ask, early_depth = no_tup
            except Exception as e:
                logger.debug("Failed to fetch Kalshi orderbook for early ticker %s: %s", early_ticker, e)

        # Fallback to pre-populated book or ask prices in market dict
        if late_yes_ask is None:
            late_mkt = cand.get("_sup_market", {})
            late_book = late_mkt.get("orderbook")
            if late_book:
                parsed_late = parse_orderbook(late_book)
                yes_tup = best_yes_ask(parsed_late)
                if yes_tup is not None:
                    late_yes_ask, late_depth = yes_tup
            else:
                late_yes_ask = late_mkt.get("yes_ask")

        if early_no_ask is None:
            early_mkt = cand.get("_sub_market", {})
            early_book = early_mkt.get("orderbook")
            if early_book:
                parsed_early = parse_orderbook(early_book)
                no_tup = best_no_ask(parsed_early)
                if no_tup is not None:
                    early_no_ask, early_depth = no_tup
            else:
                early_no_ask = early_mkt.get("no_ask")
                if early_no_ask is None and early_mkt.get("yes_bid") is not None:
                    early_no_ask = round(1.0 - float(early_mkt["yes_bid"]), 4)

        if late_yes_ask is None or early_no_ask is None:
            if funnel:
                funnel.record_clob_dropped(1)
            continue

        effective_p_early = round(1.0 - early_no_ask, 4)
        effective_p_late = late_yes_ask

        result = net_profit_frechet_implication(
            p_a=effective_p_early,
            p_b=effective_p_late,
            platform="kalshi",
        )

        net_profit = result["net_profit"]
        if net_profit < min_profit:
            if funnel:
                funnel.record_clob_dropped(1)
            continue

        # Unknown depth is 0, never None: downstream depth checks compare numerically.
        clob_depth = min(late_depth, early_depth) if (late_depth > 0 and early_depth > 0) else 0.0

        cand["_kalshi_late_yes"] = late_yes_ask
        cand["_kalshi_early_no"] = early_no_ask
        cand["_p_late"] = late_yes_ask
        cand["_p_early"] = effective_p_early
        cand["prices"] = f"Ask(Late YES)={late_yes_ask:.3f} + Ask(Early NO)={early_no_ask:.3f}"
        cand["total_cost"] = f"${result['total_cost']:.4f}"
        cand["net_profit"] = net_profit
        cand["net_roi"] = result["net_roi"]
        cand["_clob_depth"] = clob_depth

        if funnel:
            funnel.record_surfaced(1)

        refined.append(cand)

    return refined
