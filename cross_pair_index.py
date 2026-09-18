"""Persistent cross-platform pair index for event-driven Cross arb evaluation.

The periodic ``scan_cross_platform`` only sees opportunities once per
~16-min cycle. Many real Cross(PM_YES + K_NO) arbs come and go inside
that cycle and never reach the executor. This module provides the
data structures and pure logic that an event-driven path needs:

1. ``CrossPair`` — a matched (Polymarket binary, Kalshi binary) tuple
   carrying the token IDs and ticker needed to look up live prices.
2. ``CrossPairIndex.rebuild(...)`` — runs the existing fuzzy matcher
   once over the current market universe and stores ``(platform,
   token_or_ticker) -> [CrossPair]`` for O(1) lookup.
3. ``CrossPairIndex.evaluate(pair, price_cache, min_profit)`` — given
   a pair and the WS price cache the ``on_price_update`` handler
   already maintains, computes the current net profit using fresh
   prices and returns an opportunity dict if it clears ``min_profit``.
   No REST calls — entirely cache-driven.

Phase 1 (this file): the data structures + tests.
Phase 2 (separate PR): wire ``CrossPairIndex.lookup`` + ``evaluate``
into ``continuous.on_price_update`` so a Polymarket or Kalshi WS tick
triggers immediate evaluation of every Cross pair that touches that
token / ticker.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field

from fees import net_profit_cross_platform
from polymarket_api import get_binary_markets
from scans.helpers import _extract_token_ids, _within_resolution_window, _days_to_resolution

logger = logging.getLogger(__name__)

# Maximum age (seconds) of a WS cache entry before evaluate() rejects it.
# 30s is the same threshold helpers.py uses for the existing WS cache.
_DEFAULT_CACHE_MAX_AGE = 30.0


@dataclass
class CrossPair:
    """A matched Polymarket-binary <-> Kalshi-binary cross-platform pair.

    Stores only what's needed to recompute the current net profit on a
    WS price tick — never a snapshot of historical prices, since those
    would go stale instantly.
    """

    poly_token_yes: str
    poly_token_no: str
    kalshi_ticker: str
    market_title: str
    poly_condition_id: str = ""
    days_to_resolution: float | None = None
    inverted: bool = False
    # Keep references to the original market dicts so the executor can
    # reuse existing leg-build logic without re-fetching anything.
    poly_market: dict = field(default_factory=dict, repr=False)
    kalshi_market: dict = field(default_factory=dict, repr=False)


def _read_cached_price(cache: dict, key: tuple[str, str], max_age: float) -> dict | None:
    """Return cached price entry if fresh, else None. Tolerates missing _ts."""
    entry = cache.get(key)
    if not entry:
        return None
    ts = entry.get("_ts", 0)
    if ts and time.time() - ts > max_age:
        return None
    return entry


def _as_probability(value) -> float | None:
    """Normalize a scalar WS value to a 0..1 probability."""
    if value is None or isinstance(value, (dict, list, tuple)):
        return None
    try:
        probability = float(value)
    except (TypeError, ValueError):
        return None
    return probability if 0.0 <= probability <= 1.0 else None


def _kalshi_price(entry: dict, side: str) -> float | None:
    """Extract a side price from a Kalshi WS cache entry.

    The WS handler stores Kalshi prices under multiple possible field
    names depending on the feed shape. Probe the common ones in order.
    """
    keys = (
        ("yes_ask", "yes_price", "yes", "price")
        if side == "yes"
        else ("no_ask", "no_price", "no")
    )
    for key in keys:
        probability = _as_probability(entry.get(key))
        if probability is not None:
            return probability
    return None


def _poly_ask_for_token(cache: dict, token_id: str, max_age: float) -> float | None:
    """Return the best ask for a Polymarket token from the WS cache.

    The Polymarket CLOB WS feed stores asks under ``best_ask`` (or
    falls back to ``price`` for some message shapes).
    """
    entry = _read_cached_price(cache, ("polymarket", token_id), max_age)
    if not entry:
        return None
    for key in ("best_ask", "ask", "price"):
        probability = _as_probability(entry.get(key))
        if probability is not None:
            return probability
    return None


class CrossPairIndex:
    """Maps ``(platform, ticker_or_token) -> [CrossPair]`` for WS-driven lookup.

    Thread-safe. Index is rebuilt periodically by ``rebuild`` and read
    on every WS tick by ``lookup``; both paths take the same lock.
    """

    def __init__(self):
        self._index: dict[tuple[str, str], list[CrossPair]] = {}
        self._pairs: list[CrossPair] = []
        self._last_rebuild_ts: float = 0.0
        self._lock = threading.Lock()

    def rebuild(self, poly_markets: list[dict], kalshi_events: list[dict],
                threshold: float | None = None,
                min_confidence: str = "LOW") -> int:
        """Rebuild the index from the current market universe.

        Uses the same fuzzy/semantic matcher that ``scan_cross_platform``
        uses, so detected pairs are exactly the same set the periodic
        scan would have produced — just made persistent and indexed.

        Returns the number of pairs in the rebuilt index.
        """
        # Imports are local to keep this module importable in tests
        # that don't have the matcher's heavy deps installed.
        from matcher import (match_markets_to_events,
                             match_markets_to_events_semantic, detect_inverted)
        from config import (FUZZY_MATCH_THRESHOLD, SEMANTIC_MATCHING_ENABLED,
                            SEMANTIC_MATCH_THRESHOLD)

        binary_poly = get_binary_markets(poly_markets) if poly_markets else []
        if not binary_poly or not kalshi_events:
            with self._lock:
                self._index = {}
                self._pairs = []
                self._last_rebuild_ts = time.time()
            return 0

        thr = threshold if threshold is not None else (
            SEMANTIC_MATCH_THRESHOLD if SEMANTIC_MATCHING_ENABLED else FUZZY_MATCH_THRESHOLD
        )
        if SEMANTIC_MATCHING_ENABLED:
            matched = match_markets_to_events_semantic(
                binary_poly, kalshi_events,
                threshold=thr, min_confidence=min_confidence,
            )
        else:
            matched = match_markets_to_events(
                binary_poly, kalshi_events,
                threshold=thr, min_confidence=min_confidence,
            )

        new_pairs: list[CrossPair] = []
        new_index: dict[tuple[str, str], list[CrossPair]] = {}
        in_window = 0
        for m in matched:
            poly_mkt = m.get("polymarket")
            kalshi_evt = m.get("kalshi_event")
            if not poly_mkt or not kalshi_evt:
                continue
            # Filter pairs whose Polymarket side is past resolution window.
            # Kalshi side gets filtered when we look up its ticker.
            if not _within_resolution_window(poly_mkt, platform="polymarket"):
                continue
            in_window += 1

            tokens = _extract_token_ids(poly_mkt)
            if len(tokens) < 2:
                continue

            # Pick the *first* Kalshi market under this event — same
            # selection scan_cross_platform makes implicitly via its
            # sequential iteration.
            k_markets = kalshi_evt.get("markets") or []
            if not k_markets:
                continue
            k_mkt = k_markets[0]
            k_ticker = k_mkt.get("ticker", "")
            if not k_ticker:
                continue

            try:
                pm_title = m.get("pm_title") or poly_mkt.get("question") or poly_mkt.get("title", "")
                k_title = m.get("kalshi_title") or kalshi_evt.get("title", "")
                inverted = bool(detect_inverted(pm_title, k_title))
            except Exception:
                inverted = False

            pair = CrossPair(
                poly_token_yes=tokens[0],
                poly_token_no=tokens[1],
                kalshi_ticker=k_ticker,
                market_title=poly_mkt.get("question") or poly_mkt.get("title") or k_mkt.get("title", "?"),
                poly_condition_id=poly_mkt.get("condition_id", ""),
                days_to_resolution=_days_to_resolution(poly_mkt, "polymarket"),
                inverted=inverted,
                poly_market=poly_mkt,
                kalshi_market=k_mkt,
            )
            new_pairs.append(pair)
            new_index.setdefault(("polymarket", tokens[0]), []).append(pair)
            new_index.setdefault(("polymarket", tokens[1]), []).append(pair)
            new_index.setdefault(("kalshi", k_ticker), []).append(pair)

        with self._lock:
            self._index = new_index
            self._pairs = new_pairs
            self._last_rebuild_ts = time.time()

        logger.info(
            "CrossPairIndex rebuilt: %d pairs (%d in resolution window) "
            "across %d index keys",
            len(new_pairs), in_window, len(new_index),
        )
        return len(new_pairs)

    def lookup(self, platform: str, ticker_or_token: str) -> list[CrossPair]:
        """Return all CrossPairs touching this (platform, ticker_or_token).

        Used by the WS price-update handler: every tick can affect 0..N
        pairs in O(1) average lookup time.
        """
        with self._lock:
            return list(self._index.get((platform, ticker_or_token), []))

    def evaluate(self, pair: CrossPair, price_cache: dict, min_profit: float,
                 cache_max_age: float = _DEFAULT_CACHE_MAX_AGE) -> dict | None:
        """Compute the current net profit for a pair using cached WS prices.

        Returns an opportunity dict shaped like ``scan_cross_platform``
        emits, or None if any side is missing/stale or the profit is
        below ``min_profit``. No I/O — purely cache-driven.
        """
        pm_yes = _poly_ask_for_token(price_cache, pair.poly_token_yes, cache_max_age)
        pm_no = _poly_ask_for_token(price_cache, pair.poly_token_no, cache_max_age)
        if pm_yes is None or pm_no is None:
            return None

        k_entry = _read_cached_price(price_cache, ("kalshi", pair.kalshi_ticker), cache_max_age)
        if not k_entry:
            return None
        k_yes = _kalshi_price(k_entry, "yes")
        k_no = _kalshi_price(k_entry, "no")
        if k_yes is None or k_no is None:
            return None

        # Try both directions: PM_YES + K_NO vs PM_NO + K_YES. If the
        # pair was detected as inverted, swap K's sides accordingly.
        if pair.inverted:
            k_yes, k_no = k_no, k_yes

        result1 = net_profit_cross_platform(pm_yes, k_no, "yes", "no")
        result2 = net_profit_cross_platform(pm_no, k_yes, "no", "yes")
        best = result1 if result1["net_profit"] > result2["net_profit"] else result2
        if best["net_profit"] < min_profit:
            return None

        if best is result1:
            total_cost = pm_yes + k_no
            prices_str = f"PM_Y={pm_yes:.3f} K_N={k_no:.3f}"
            opp_type = "Cross(PM_YES + K_NO)"
        else:
            total_cost = pm_no + k_yes
            prices_str = f"PM_N={pm_no:.3f} K_Y={k_yes:.3f}"
            opp_type = "Cross(PM_NO + K_YES)"

        return {
            "type": opp_type,
            "_layer": 1,
            "_source": "ws_cross_pair",  # Distinguish from scan_cross_platform output
            "market": pair.market_title[:60],
            "prices": prices_str,
            "total_cost": f"${total_cost:.4f}",
            "gross_spread": f"{best['gross_spread']:.4f}",
            "fees": f"${best['fees']:.4f}",
            "net_profit": best["net_profit"],
            "net_roi": f"{best['net_profit'] / total_cost * 100:.2f}%" if total_cost > 0 else "0%",
            "_token_ids": [pair.poly_token_yes, pair.poly_token_no],
            "_kalshi_ticker": pair.kalshi_ticker,
            "_kalshi_yes": k_yes,
            "_kalshi_no": k_no,
            "_market_key": f"polymarket-{pair.poly_condition_id}" if pair.poly_condition_id else "",
            "_days_to_resolution": pair.days_to_resolution,
        }

    @property
    def pair_count(self) -> int:
        with self._lock:
            return len(self._pairs)

    @property
    def last_rebuild_ts(self) -> float:
        with self._lock:
            return self._last_rebuild_ts
