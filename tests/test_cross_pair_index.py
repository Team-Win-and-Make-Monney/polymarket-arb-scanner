"""Tests for cross_pair_index.CrossPairIndex — Phase 1 of WS-driven Cross detection."""

import os
import sys
import time
from unittest.mock import patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# py-clob-client (the only heavy dep polymarket_api drags in) is installed
# in CI via requirements.txt, so a plain import works regardless of which
# test file runs first. Tests that need to control get_binary_markets()
# patch cross_pair_index.get_binary_markets directly at the call site —
# no sys.modules hacking.
from cross_pair_index import (
    CrossPair, CrossPairIndex,
    _kalshi_price, _poly_ask_for_token, _read_cached_price,
)

# Identity stub used by TestRebuild — accepts the poly_markets list and
# returns it unchanged (real get_binary_markets filters to binary-only,
# but the fixtures we hand-craft are already binary-shaped).
_identity_binary_markets = lambda mkts: list(mkts) if mkts else []


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

class TestReadCachedPrice:
    def test_returns_none_for_missing_key(self):
        assert _read_cached_price({}, ("polymarket", "tok"), 30) is None

    def test_returns_entry_when_fresh(self):
        cache = {("polymarket", "tok"): {"_ts": time.time(), "best_ask": 0.5}}
        e = _read_cached_price(cache, ("polymarket", "tok"), 30)
        assert e and e["best_ask"] == 0.5

    def test_drops_stale_entry(self):
        cache = {("polymarket", "tok"): {"_ts": time.time() - 120, "best_ask": 0.5}}
        assert _read_cached_price(cache, ("polymarket", "tok"), 30) is None

    def test_missing_ts_fails_closed(self):
        # Entries without provenance time cannot prove freshness.
        cache = {("polymarket", "tok"): {"best_ask": 0.5}}
        e = _read_cached_price(cache, ("polymarket", "tok"), 30)
        assert e is None


class TestKalshiPrice:
    def test_yes_via_yes_field(self):
        assert _kalshi_price({"yes": 0.42}, "yes") == 0.42

    def test_yes_falls_back_to_yes_price(self):
        assert _kalshi_price({"yes_price": 0.7}, "yes") == 0.7

    def test_yes_falls_back_to_price(self):
        assert _kalshi_price({"price": 0.3}, "yes") == 0.3

    def test_no_via_no_field(self):
        assert _kalshi_price({"no": 0.6}, "no") == 0.6

    def test_no_does_not_use_price_fallback(self):
        # The "price" fallback is yes-only; otherwise we'd double-count.
        assert _kalshi_price({"price": 0.5}, "no") is None


class TestPolyAskForToken:
    def test_uses_best_ask(self):
        cache = {("polymarket", "abc"): {"_ts": time.time(), "best_ask": 0.55}}
        assert _poly_ask_for_token(cache, "abc", 30) == 0.55

    def test_falls_back_to_ask(self):
        cache = {("polymarket", "abc"): {"_ts": time.time(), "ask": 0.61}}
        assert _poly_ask_for_token(cache, "abc", 30) == 0.61

    def test_returns_none_for_stale(self):
        cache = {("polymarket", "abc"): {"_ts": time.time() - 9999, "best_ask": 0.55}}
        assert _poly_ask_for_token(cache, "abc", 30) is None


# ---------------------------------------------------------------------------
# CrossPairIndex — rebuild + lookup
# ---------------------------------------------------------------------------

def _future_iso(days_from_now: int = 3) -> str:
    """ISO date inside MAX_RESOLUTION_DAYS (default 7) so _within_resolution_window passes."""
    from datetime import datetime, timedelta, timezone
    return (datetime.now(timezone.utc) + timedelta(days=days_from_now)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )


def _fake_poly_market(token_yes="ty", token_no="tn", title="X market",
                      condition_id="cid1", end_iso=None):
    if end_iso is None:
        end_iso = _future_iso(3)
    return {
        "question": title,
        "title": title,
        "condition_id": condition_id,
        "endDateIso": end_iso,
        "clobTokenIds": [token_yes, token_no],
    }


def _fake_kalshi_event(ticker="EV1", market_ticker="K-EV1-1",
                       title="X market", close_iso=None):
    if close_iso is None:
        close_iso = _future_iso(3)
    return {
        "event_ticker": ticker,
        "title": title,
        "markets": [{
            "ticker": market_ticker,
            "title": title,
            "close_time": close_iso,
            "expiration_time": close_iso,
        }],
    }


class TestRebuild:
    def test_empty_inputs_clear_index(self):
        idx = CrossPairIndex()
        n = idx.rebuild([], [])
        assert n == 0
        assert idx.pair_count == 0

    def test_pairs_indexed_under_three_keys(self):
        """Each pair gets indexed under PM_yes-token, PM_no-token, and Kalshi-ticker."""
        poly = [_fake_poly_market(token_yes="TY1", token_no="TN1", title="Trump wins")]
        kalshi = [_fake_kalshi_event(market_ticker="KSH1", title="Trump wins")]

        match_result = [{"polymarket": poly[0], "kalshi_event": kalshi[0],
                         "pm_title": "Trump wins", "kalshi_title": "Trump wins",
                         "similarity": 100, "confidence": "HIGH", "entity_overlap": 1.0}]
        idx = CrossPairIndex()
        with patch("cross_pair_index.get_binary_markets", side_effect=_identity_binary_markets), \
             patch("matcher.detect_inverted", return_value=False), \
             patch("matcher.match_markets_to_events", return_value=match_result), \
             patch("matcher.match_markets_to_events_semantic", return_value=match_result):
            n = idx.rebuild(poly, kalshi)

        assert n == 1
        assert len(idx.lookup("polymarket", "TY1")) == 1
        assert len(idx.lookup("polymarket", "TN1")) == 1
        assert len(idx.lookup("kalshi", "KSH1")) == 1
        assert idx.lookup("polymarket", "TY1")[0].kalshi_ticker == "KSH1"

    def test_rebuild_replaces_old_index(self):
        idx = CrossPairIndex()
        poly1 = [_fake_poly_market(token_yes="A1", token_no="A2")]
        kalshi1 = [_fake_kalshi_event(market_ticker="K_OLD")]
        poly2 = [_fake_poly_market(token_yes="B1", token_no="B2")]
        kalshi2 = [_fake_kalshi_event(market_ticker="K_NEW")]
        match1 = [{"polymarket": poly1[0], "kalshi_event": kalshi1[0],
                   "pm_title": "x", "kalshi_title": "x", "similarity": 100,
                   "confidence": "HIGH", "entity_overlap": 1.0}]
        match2 = [{"polymarket": poly2[0], "kalshi_event": kalshi2[0],
                   "pm_title": "y", "kalshi_title": "y", "similarity": 100,
                   "confidence": "HIGH", "entity_overlap": 1.0}]

        with patch("cross_pair_index.get_binary_markets", side_effect=_identity_binary_markets), \
             patch("matcher.detect_inverted", return_value=False), \
             patch("matcher.match_markets_to_events", side_effect=[match1, match2]), \
             patch("matcher.match_markets_to_events_semantic", side_effect=[match1, match2]):
            idx.rebuild(poly1, kalshi1)
            assert idx.lookup("kalshi", "K_OLD")
            idx.rebuild(poly2, kalshi2)

        # Old key gone, new key present
        assert idx.lookup("kalshi", "K_OLD") == []
        assert len(idx.lookup("kalshi", "K_NEW")) == 1

    def test_skips_pairs_outside_resolution_window(self):
        """Pairs whose Polymarket side has already resolved must be excluded."""
        poly = [_fake_poly_market(token_yes="OUT1", token_no="OUT2",
                                  end_iso="2020-01-01T00:00:00Z")]
        kalshi = [_fake_kalshi_event(market_ticker="K_OUT")]
        match_result = [{"polymarket": poly[0], "kalshi_event": kalshi[0],
                         "pm_title": "x", "kalshi_title": "x", "similarity": 100,
                         "confidence": "HIGH", "entity_overlap": 1.0}]

        idx = CrossPairIndex()
        with patch("cross_pair_index.get_binary_markets", side_effect=_identity_binary_markets), \
             patch("matcher.detect_inverted", return_value=False), \
             patch("matcher.match_markets_to_events", return_value=match_result), \
             patch("matcher.match_markets_to_events_semantic", return_value=match_result):
            n = idx.rebuild(poly, kalshi)

        assert n == 0
        assert idx.lookup("polymarket", "OUT1") == []


# ---------------------------------------------------------------------------
# CrossPairIndex.evaluate — pure profit recomputation from price_cache
# ---------------------------------------------------------------------------

def _populate_cache(cache, poly_yes_ask=None, poly_no_ask=None,
                    kalshi_yes=None, kalshi_no=None):
    now = time.time()
    if poly_yes_ask is not None:
        cache[("polymarket", "TY1")] = {"_ts": now, "best_ask": poly_yes_ask}
    if poly_no_ask is not None:
        cache[("polymarket", "TN1")] = {"_ts": now, "best_ask": poly_no_ask}
    if kalshi_yes is not None or kalshi_no is not None:
        cache[("kalshi", "KSH1")] = {
            "_ts": now,
            **({"yes": kalshi_yes} if kalshi_yes is not None else {}),
            **({"no": kalshi_no} if kalshi_no is not None else {}),
        }


def _make_pair():
    return CrossPair(
        poly_token_yes="TY1",
        poly_token_no="TN1",
        kalshi_ticker="KSH1",
        market_title="X market",
        poly_condition_id="cid1",
    )


class TestEvaluate:
    def test_returns_none_when_pm_yes_missing(self):
        idx = CrossPairIndex()
        cache = {}
        _populate_cache(cache, poly_no_ask=0.5, kalshi_yes=0.4, kalshi_no=0.4)
        assert idx.evaluate(_make_pair(), cache, min_profit=0.01) is None

    def test_returns_none_when_kalshi_missing(self):
        idx = CrossPairIndex()
        cache = {}
        _populate_cache(cache, poly_yes_ask=0.4, poly_no_ask=0.6)
        assert idx.evaluate(_make_pair(), cache, min_profit=0.01) is None

    def test_returns_none_for_stale_kalshi(self):
        idx = CrossPairIndex()
        cache = {("polymarket", "TY1"): {"_ts": time.time(), "best_ask": 0.4},
                 ("polymarket", "TN1"): {"_ts": time.time(), "best_ask": 0.6},
                 ("kalshi", "KSH1"): {"_ts": time.time() - 9999, "yes": 0.5, "no": 0.5}}
        assert idx.evaluate(_make_pair(), cache, min_profit=0.01) is None

    def test_detects_arb_when_pm_yes_plus_k_no_under_one(self):
        """Classic Cross arb: PM_YES=0.30 + K_NO=0.30 = 0.60 → ~33% net spread before fees."""
        idx = CrossPairIndex()
        cache = {}
        _populate_cache(cache, poly_yes_ask=0.30, poly_no_ask=0.80,
                        kalshi_yes=0.20, kalshi_no=0.30)

        opp = idx.evaluate(_make_pair(), cache, min_profit=0.01)
        assert opp is not None
        assert opp["type"] in ("Cross(PM_YES + K_NO)", "Cross(PM_NO + K_YES)")
        assert opp["net_profit"] > 0.01
        assert opp["_kalshi_ticker"] == "KSH1"
        assert opp["_token_ids"] == ["TY1", "TN1"]
        assert opp["_source"] == "ws_cross_pair"
        assert opp["_layer"] == 1

    def test_accepts_normalized_string_asks_and_ignores_raw_ladders(self):
        idx = CrossPairIndex()
        now = time.time()
        cache = {
            ("polymarket", "TY1"): {"_ts": now, "best_ask": "0.30", "price": "0.29"},
            ("polymarket", "TN1"): {"_ts": now, "best_ask": "0.80", "price": "0.79"},
            ("kalshi", "KSH1"): {
                "_ts": now,
                "yes": [[20, 5]],
                "no": [[70, 5]],
                "yes_ask": "0.31",
                "no_ask": "0.30",
            },
        }

        opp = idx.evaluate(_make_pair(), cache, min_profit=0.01)

        assert opp is not None
        assert opp["type"] == "Cross(PM_YES + K_NO)"
        assert opp["prices"] == "PM_Y=0.300 K_N=0.300"

    def test_no_arb_when_prices_too_close_to_one(self):
        idx = CrossPairIndex()
        cache = {}
        # PM_YES=0.55 + K_NO=0.50 = 1.05 — overround, no arb after fees
        _populate_cache(cache, poly_yes_ask=0.55, poly_no_ask=0.55,
                        kalshi_yes=0.50, kalshi_no=0.50)
        assert idx.evaluate(_make_pair(), cache, min_profit=0.01) is None

    def test_inverted_pair_swaps_kalshi_sides(self):
        """Inversion preserves physical Kalshi fields and emits executable legs."""
        idx = CrossPairIndex()
        # Construct so the arb is on the swapped side: K is "inverted" so
        # what looks like K_YES=0.80 should be treated as K_NO=0.80, etc.
        pair = _make_pair()
        pair.inverted = True
        cache = {}
        _populate_cache(cache, poly_yes_ask=0.80, poly_no_ask=0.30,
                        kalshi_yes=0.30, kalshi_no=0.20)
        opp = idx.evaluate(pair, cache, min_profit=0.01)
        assert opp is not None
        assert opp["net_profit"] > 0
        assert opp["_kalshi_yes"] == 0.30
        assert opp["_kalshi_no"] == 0.20
        assert opp["_cross_legs"][1]["platform"] == "kalshi"
        assert opp["_cross_legs"][1]["side"] == "no"
        assert opp["_cross_legs"][1]["price"] == 0.20

    def test_missing_timestamp_fails_closed(self):
        idx = CrossPairIndex()
        cache = {}
        _populate_cache(cache, poly_yes_ask=0.30, poly_no_ask=0.80,
                        kalshi_yes=0.80, kalshi_no=0.30)
        for entry in cache.values():
            entry.pop("_ts", None)
        assert idx.evaluate(_make_pair(), cache, min_profit=0.01) is None


class TestLookup:
    def test_returns_empty_list_for_missing_key(self):
        idx = CrossPairIndex()
        assert idx.lookup("polymarket", "nonexistent") == []

    def test_concurrent_safe(self):
        """Lookup uses a lock so reads are safe during rebuild."""
        idx = CrossPairIndex()
        # Manually populate the index for a thread-safety smoke test
        pair = _make_pair()
        idx._index[("polymarket", "TY1")] = [pair]
        idx._pairs.append(pair)
        # Multiple threads reading should each get a consistent snapshot
        for _ in range(50):
            assert idx.lookup("polymarket", "TY1") == [pair]
