"""Input assembly for the cross-platform convergence scan.

Replaces the inline block in continuous.py that re-ran a full fuzzy match of
every Polymarket market against every flat Kalshi market on each scan cycle
(~166s per scan, 66% of total scan time) even though the configured
``min_platforms`` made an opportunity impossible with the available sources.
"""

import logging

from .helpers import _within_resolution_window

logger = logging.getLogger(__name__)

# The platform price sources this builder can currently populate. Update when
# a new source is wired in — the short-circuit below depends on it.
_AVAILABLE_SOURCES = 2  # polymarket + kalshi


class ConvergenceMatchCache:
    """Cross-cycle cache of Polymarket→Kalshi title matches.

    Market titles are static, so fuzzy-match results are stable: cache the
    matched Kalshi ticker (or None for a confirmed non-match) per Polymarket
    condition_id, and only run the expensive matcher for unseen markets.
    A periodic full refresh picks up newly listed Kalshi markets.
    """

    def __init__(self, refresh_interval: float = 1800.0):
        self.refresh_interval = refresh_interval
        self.matched: dict[str, str | None] = {}
        self.last_full_refresh: float = 0.0


def _pm_yes_price(market: dict) -> float | None:
    for token in market.get("tokens", []):
        if token.get("outcome", "").lower() == "yes":
            price = token.get("price")
            if price is not None:
                return float(price)
    return None


def build_convergence_matched(
    poly_markets: list[dict],
    kalshi_events: list[dict],
    cache: ConvergenceMatchCache,
    *,
    matcher_fn,
    min_confidence,
    min_platforms: int,
    threshold: int = 72,
    now: float,
) -> list[dict]:
    """Build ``matched_markets`` input for ``scan_convergence``.

    Returns [] without matching when the available price sources cannot meet
    ``min_platforms`` — the scan could never emit an opportunity, so the
    fuzzy match would be pure dead compute.
    """
    if _AVAILABLE_SOURCES < min_platforms:
        logger.debug(
            "Convergence input build skipped: %d price sources < min_platforms=%d",
            _AVAILABLE_SOURCES, min_platforms,
        )
        return []
    if not poly_markets or not kalshi_events:
        return []

    kflat = [
        m
        for evt in kalshi_events
        for m in evt.get("markets", [evt])
        if _within_resolution_window(m, platform="kalshi")
    ]
    if not kflat:
        return []

    if now - cache.last_full_refresh >= cache.refresh_interval:
        cache.matched.clear()
        cache.last_full_refresh = now

    unseen = [
        m for m in poly_markets
        if m.get("condition_id") and m["condition_id"] not in cache.matched
    ]
    if unseen:
        matches = matcher_fn(
            unseen, kflat, "polymarket", "kalshi",
            threshold=threshold, min_confidence=min_confidence,
        )
        found: dict[str, str] = {}
        for match in matches:
            cid = match.get("market_a", {}).get("condition_id")
            ticker = match.get("market_b", {}).get("ticker")
            if cid and ticker:
                found[cid] = ticker
        for market in unseen:
            cid = market["condition_id"]
            cache.matched[cid] = found.get(cid)

    kalshi_by_ticker = {m.get("ticker"): m for m in kflat if m.get("ticker")}

    matched_markets = []
    for market in poly_markets:
        cid = market.get("condition_id")
        if not cid:
            continue
        ticker = cache.matched.get(cid)
        if not ticker:
            continue
        kalshi_market = kalshi_by_ticker.get(ticker)
        if not kalshi_market:
            continue
        pm_yes = _pm_yes_price(market)
        kalshi_yes = kalshi_market.get("yes_ask")
        if kalshi_yes is None:
            kalshi_yes = kalshi_market.get("yes_price")
        if pm_yes is None or kalshi_yes is None:
            continue
        kalshi_yes = float(kalshi_yes)
        if kalshi_yes > 1:
            kalshi_yes /= 100.0
        matched_markets.append({
            "market_key": cid,
            "title": market.get("question") or market.get("title", cid),
            "platform_prices": {
                "polymarket": {"yes": pm_yes, "no": 1.0 - pm_yes},
                "kalshi": {"yes": kalshi_yes, "no": 1.0 - kalshi_yes},
            },
        })
    return matched_markets
