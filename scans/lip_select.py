"""Kalshi LIP market selector — picks the top-N pools worth quoting.

The Liquidity Incentive Program pays resting orders pro-rata by per-second
score (size x proximity-to-mid). The maker's edge is therefore market
SELECTION: large pools, thin competition, non-sports categories, far enough
from resolution to quote safely.

Pool data comes from GET /incentive_programs (KalshiClient.
fetch_incentive_programs, verified live 2026-08-04). The Kalshi reward scanner
delegates market selection here so discovery and continuous-mode quoting use
the same current incentive-program data and eligibility filters.
"""

import logging
from datetime import datetime, timedelta, timezone

from config import (
    LIP_MIN_POOL,
    LIP_MAX_MARKETS,
    LIP_EXCLUDED_CATEGORIES,
    LIP_PRICE_BAND_LOW,
    LIP_PRICE_BAND_HIGH,
    LIP_MIN_HOURS_REMAINING,
    LIP_DEPTH_PROBE_LIMIT,
)
from .kalshi import _fetch_kalshi_data

logger = logging.getLogger(__name__)


def _parse_iso(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        return dt if dt.utcoffset() is not None else None
    except (AttributeError, TypeError, ValueError):
        return None


def _hours_from_now(ts: str | None) -> float | None:
    dt = _parse_iso(ts)
    if dt is None:
        return None
    return (dt - datetime.now(timezone.utc)).total_seconds() / 3600.0


def _earliest_program_end(left: str | None, right: str | None) -> str | None:
    """Return the earliest valid program end, failing closed on bad input."""
    left_dt = _parse_iso(left)
    right_dt = _parse_iso(right)
    if left_dt is None or right_dt is None:
        return None
    return left if left_dt <= right_dt else right


def _yes_mid_from_asks(yes_ask: float | None, no_ask: float | None) -> float | None:
    """Derive the YES midpoint from the two asks, rejecting invalid/crossed books."""
    if yes_ask is None or no_ask is None:
        return None
    try:
        yes_ask = float(yes_ask)
        no_ask = float(no_ask)
    except (TypeError, ValueError):
        return None
    if not (0 < yes_ask < 1 and 0 < no_ask < 1):
        return None
    yes_bid = 1.0 - no_ask
    if yes_bid > yes_ask:
        return None
    return (yes_bid + yes_ask) / 2.0


def select_lip_markets(kalshi_client, kalshi_data: tuple | None = None,
                       max_markets: int | None = None) -> list[dict]:
    """Rank active LIP pools and return the top-N quotable markets.

    Filters (per docs/plans/02-kalshi-lip-mm-scope.md §2):
      1. pool >= LIP_MIN_POOL dollars
      2. category not in LIP_EXCLUDED_CATEGORIES (sports pools are served by
         contracted MMs and excluded from LIP anyway)
      3. program end AND market close both >= LIP_MIN_HOURS_REMAINING out
      4. mid price inside [LIP_PRICE_BAND_LOW, LIP_PRICE_BAND_HIGH] — tails
         carry binary gap risk disproportionate to reward
      5. competition proxy: resting depth at best on both sides; score =
         pool / (1 + depth) so thin books rank higher

    Returns list of dicts sorted by score desc:
        {ticker, title, pool_dollars, category, mid, competition_depth, score,
         discount_factor_bps, program_end, market_close_hours, price_ranges}
    """
    if not kalshi_client:
        return []
    limit = max_markets or LIP_MAX_MARKETS

    programs = kalshi_client.fetch_incentive_programs(
        status="active", incentive_type="liquidity")
    if not programs:
        logger.info("LIP select: no active liquidity incentive programs.")
        return []

    # Aggregate pools per ticker (a market can carry multiple programs).
    pools: dict[str, dict] = {}
    for p in programs:
        ticker = p.get("market_ticker")
        if not ticker:
            continue
        if ticker not in pools:
            pools[ticker] = {
                "pool_dollars": 0.0,
                "discount_factor_bps": p.get("discount_factor_bps"),
                "program_end": p.get("end_date"),
            }
        else:
            entry = pools[ticker]
            entry["program_end"] = _earliest_program_end(entry["program_end"], p.get("end_date"))
            discounts = [
                value for value in (entry["discount_factor_bps"], p.get("discount_factor_bps"))
                if isinstance(value, (int, float))
            ]
            entry["discount_factor_bps"] = max(discounts) if discounts else None
        entry = pools[ticker]
        entry["pool_dollars"] += p.get("period_reward_dollars", 0.0)

    # Join tickers to market/event metadata (category, close_time, prices).
    if kalshi_data:
        events, markets_by_event, _ = kalshi_data
    else:
        events, markets_by_event, _ = _fetch_kalshi_data(kalshi_client)
    category_by_event = {e.get("event_ticker"): e.get("category", "") for e in events}
    market_meta: dict[str, tuple[dict, str]] = {}
    for event_ticker, markets in markets_by_event.items():
        cat = category_by_event.get(event_ticker, "")
        for m in markets:
            t = m.get("ticker")
            if t:
                market_meta[t] = (m, cat)

    excluded = {c.strip().lower() for c in LIP_EXCLUDED_CATEGORIES if c.strip()}
    candidates = []
    skipped = {"pool": 0, "category": 0, "duration": 0, "band": 0, "unknown": 0}
    for ticker, pool in pools.items():
        if pool["pool_dollars"] < LIP_MIN_POOL:
            skipped["pool"] += 1
            continue
        meta = market_meta.get(ticker)
        if meta is None:
            skipped["unknown"] += 1
            continue
        market, category = meta
        if category.strip().lower() in excluded:
            skipped["category"] += 1
            continue
        prog_hours = _hours_from_now(pool["program_end"])
        close_hours = _hours_from_now(market.get("close_time"))
        if prog_hours is None or close_hours is None or \
           prog_hours < LIP_MIN_HOURS_REMAINING or close_hours < LIP_MIN_HOURS_REMAINING:
            skipped["duration"] += 1
            continue
        yes_ask, no_ask = kalshi_client.get_market_price(market)
        mid = _yes_mid_from_asks(yes_ask, no_ask)
        if mid is None or not (LIP_PRICE_BAND_LOW <= mid <= LIP_PRICE_BAND_HIGH):
            skipped["band"] += 1
            continue
        candidates.append({
            "ticker": ticker,
            "title": market.get("title") or ticker,
            "pool_dollars": round(pool["pool_dollars"], 2),
            "category": category,
            "mid": mid,
            "discount_factor_bps": pool["discount_factor_bps"],
            "program_end": pool["program_end"],
            "market_close_hours": round(close_hours, 1) if close_hours is not None else None,
            "price_ranges": market.get("price_ranges") or [],
        })

    # Competition probe only for the richest pools — book fetches are the
    # expensive step and share the global Kalshi rate limit with scans.
    candidates.sort(key=lambda c: c["pool_dollars"], reverse=True)
    probed = []
    for c in candidates[:LIP_DEPTH_PROBE_LIMIT]:
        depth = kalshi_client.get_order_book_depth(c["ticker"]) or {}
        competition = depth.get("yes_ask_size", 0) + depth.get("no_ask_size", 0)
        c["competition_depth"] = competition
        c["score"] = c["pool_dollars"] / (1.0 + competition)
        probed.append(c)

    probed.sort(key=lambda c: c["score"], reverse=True)
    selected = probed[:limit]
    logger.info(
        "LIP select: %d programs -> %d pooled tickers -> %d candidates -> top %d "
        "(skipped: %s)",
        len(programs), len(pools), len(candidates), len(selected), skipped,
    )
    for c in selected:
        logger.info("  LIP pick: %s pool=$%.0f cat=%s mid=%.2f depth=%d score=%.2f",
                    c["ticker"], c["pool_dollars"], c["category"] or "?",
                    c["mid"], c["competition_depth"], c["score"])
    return selected
