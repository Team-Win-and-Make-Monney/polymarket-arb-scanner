"""Tests for the first-class Stage 2 refiner in scans/correlated.py.

Covers:
- CLOB fetch via injected ``fetch_clob`` (no network)
- Liquidity floor on both legs
- Spread-collapse gate (live vs Stage 1)
- Layer 4 floor on absolute spread retracement
- Both-legs-required gate
- Auto/manual provenance pass-through

Module-reference pattern (``import scans.correlated as cm``) so
``patch.object`` works regardless of cross-test sys.modules state.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import scans.correlated as cm  # noqa: E402

_refine = cm._refine_correlated_with_depth


# ---------------------------------------------------------------------------
# Fixture builders
# ---------------------------------------------------------------------------


def _make_opp(
    *,
    long_key: str = "long-mkt",
    short_key: str = "short-mkt",
    long_price: float = 0.40,
    short_price: float = 0.55,
    spread: float | None = None,
    pair_source: str = "manual",
):
    if spread is None:
        spread = abs(short_price - long_price) / max(long_price, short_price)
    return {
        "type": "Correlated",
        "_layer": 4,
        "market": "Long vs Short",
        "_long_leg": long_key,
        "_long_leg_name": "Long",
        "_long_price": long_price,
        "_short_leg": short_key,
        "_short_leg_name": "Short",
        "_short_price": short_price,
        "spread": spread,
        "_token_ids_a": ["tok_long_yes", "tok_long_no"],
        "_token_ids_b": ["tok_short_yes", "tok_short_no"],
        "_market_key_a": long_key,
        "_market_key_b": short_key,
        "_long_market": {"id": long_key, "clobTokenIds": '["tok_long_yes","tok_long_no"]'},
        "_short_market": {"id": short_key, "clobTokenIds": '["tok_short_yes","tok_short_no"]'},
        "_pair_source": pair_source,
    }


def _book(*, ask: float | None, bid: float | None,
          ask_size: float = 100.0, bid_size: float = 100.0):
    return {
        "yes_ask": ask,
        "yes_bid": bid,
        "yes_ask_size": ask_size,
        "yes_bid_size": bid_size,
        "no_ask": None, "no_bid": None,
        "no_ask_size": 0, "no_bid_size": 0,
    }


def _fake_fetch(books_by_key: dict):
    """Return a fetch_clob that mimics scans.helpers._fetch_clob_for_market."""
    def fetch(market, _price_cache=None):
        key = (market or {}).get("id")
        return market, books_by_key.get(key)
    return fetch


# ---------------------------------------------------------------------------
# Both-legs-required gate
# ---------------------------------------------------------------------------


class TestBothLegsRequired:
    def test_drops_when_long_leg_missing(self):
        opp = _make_opp()
        fetch = _fake_fetch({
            "long-mkt": None,
            "short-mkt": _book(ask=0.55, bid=0.55),
        })
        out = _refine([opp], fetch_clob=fetch, min_liquidity=10)
        assert out == []

    def test_drops_when_short_leg_missing(self):
        opp = _make_opp()
        fetch = _fake_fetch({
            "long-mkt": _book(ask=0.40, bid=0.40),
            "short-mkt": None,
        })
        out = _refine([opp], fetch_clob=fetch, min_liquidity=10)
        assert out == []

    def test_drops_when_long_ask_missing(self):
        opp = _make_opp()
        fetch = _fake_fetch({
            "long-mkt": _book(ask=None, bid=0.40),
            "short-mkt": _book(ask=0.55, bid=0.55),
        })
        out = _refine([opp], fetch_clob=fetch, min_liquidity=10)
        assert out == []

    def test_drops_when_short_bid_missing(self):
        opp = _make_opp()
        fetch = _fake_fetch({
            "long-mkt": _book(ask=0.40, bid=0.40),
            "short-mkt": _book(ask=0.55, bid=None),
        })
        out = _refine([opp], fetch_clob=fetch, min_liquidity=10)
        assert out == []


# ---------------------------------------------------------------------------
# Liquidity floor
# ---------------------------------------------------------------------------


class TestLiquidityFloor:
    def test_drops_when_long_depth_below_floor(self):
        opp = _make_opp()
        fetch = _fake_fetch({
            "long-mkt": _book(ask=0.40, bid=0.40, ask_size=2.0),
            "short-mkt": _book(ask=0.55, bid=0.55, bid_size=200.0),
        })
        out = _refine([opp], fetch_clob=fetch, min_liquidity=10)
        assert out == []

    def test_drops_when_short_depth_below_floor(self):
        opp = _make_opp()
        fetch = _fake_fetch({
            "long-mkt": _book(ask=0.40, bid=0.40, ask_size=200.0),
            "short-mkt": _book(ask=0.55, bid=0.55, bid_size=2.0),
        })
        out = _refine([opp], fetch_clob=fetch, min_liquidity=10)
        assert out == []

    def test_keeps_when_both_legs_meet_floor(self):
        opp = _make_opp()
        fetch = _fake_fetch({
            "long-mkt": _book(ask=0.40, bid=0.40, ask_size=200.0),
            "short-mkt": _book(ask=0.55, bid=0.55, bid_size=200.0),
        })
        out = _refine(
            [opp], fetch_clob=fetch, min_liquidity=10,
            max_spread_collapse=0.50,  # be permissive on collapse for this test
        )
        assert len(out) == 1
        assert out[0]["_long_depth"] == 200.0
        assert out[0]["_short_depth"] == 200.0


# ---------------------------------------------------------------------------
# Spread collapse gate
# ---------------------------------------------------------------------------


class TestSpreadCollapse:
    def test_drops_when_spread_collapsed_past_max(self):
        # Stage 1 spread of 0.27 (long=0.40 vs short=0.55).
        # Live ask=0.50, live bid=0.51 → live spread ~0.02 → collapse ~93%.
        opp = _make_opp(long_price=0.40, short_price=0.55)
        fetch = _fake_fetch({
            "long-mkt": _book(ask=0.50, bid=0.49, ask_size=200.0),
            "short-mkt": _book(ask=0.52, bid=0.51, bid_size=200.0),
        })
        out = _refine(
            [opp], fetch_clob=fetch, min_liquidity=10,
            max_spread_collapse=0.20,
        )
        assert out == []

    def test_keeps_when_spread_collapse_within_tolerance(self):
        # Stage 1 spread 0.27. Live spread ~0.20 — collapse ~26%. Allow 50%.
        opp = _make_opp(long_price=0.40, short_price=0.55, spread=0.273)
        fetch = _fake_fetch({
            "long-mkt": _book(ask=0.40, bid=0.39, ask_size=200.0),
            "short-mkt": _book(ask=0.51, bid=0.50, bid_size=200.0),
        })
        out = _refine(
            [opp], fetch_clob=fetch, min_liquidity=10,
            max_spread_collapse=0.50,
        )
        assert len(out) == 1
        assert "_live_spread" in out[0]
        assert "_spread_collapse" in out[0]


# ---------------------------------------------------------------------------
# Provenance + survivor fields
# ---------------------------------------------------------------------------


class TestSurvivorFields:
    def test_writes_live_quote_fields(self):
        opp = _make_opp()
        fetch = _fake_fetch({
            "long-mkt": _book(ask=0.41, bid=0.40, ask_size=300.0),
            "short-mkt": _book(ask=0.55, bid=0.54, bid_size=250.0),
        })
        out = _refine(
            [opp], fetch_clob=fetch,
            min_liquidity=10, max_spread_collapse=0.50,
        )
        assert len(out) == 1
        r = out[0]
        assert r["_long_ask"] == 0.41
        assert r["_short_bid"] == 0.54
        assert r["_long_depth"] == 300.0
        assert r["_short_depth"] == 250.0
        assert "_live_spread" in r
        assert "_spread_collapse" in r

    def test_preserves_pair_source_marker(self):
        opp = _make_opp(pair_source="auto")
        fetch = _fake_fetch({
            "long-mkt": _book(ask=0.41, bid=0.40, ask_size=300.0),
            "short-mkt": _book(ask=0.55, bid=0.54, bid_size=250.0),
        })
        out = _refine(
            [opp], fetch_clob=fetch, min_liquidity=10, max_spread_collapse=0.50,
        )
        assert out[0]["_pair_source"] == "auto"


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


class TestEdgeCases:
    def test_empty_input_returns_empty(self):
        assert _refine([]) == []

    def test_swallows_fetch_exceptions(self):
        opp = _make_opp()
        def boom(*_a, **_k):
            raise RuntimeError("CLOB down")
        out = _refine([opp], fetch_clob=boom, min_liquidity=10)
        assert out == []


# ---------------------------------------------------------------------------
# Long/short vs a/b orientation (audit #77)
# ---------------------------------------------------------------------------


class TestSwappedLegOrientation:
    """Stage 1 assigns market B as the long leg when B is underpriced.

    The pre-fetch used to key ``_long_market`` under ``_market_key_a`` and
    ``_short_market`` under ``_market_key_b`` unconditionally, so when B was
    the long leg the two books were swapped at lookup time. Fetch tasks must
    be keyed by the long/short mapping, not a/b.
    """

    def _make_swapped_opp(self):
        """Opp where market B is the LONG leg (B was underpriced)."""
        return {
            "type": "Correlated",
            "_layer": 4,
            "market": "A vs B",
            "_long_leg": "mkt-b",
            "_long_leg_name": "B",
            "_long_price": 0.40,
            "_short_leg": "mkt-a",
            "_short_leg_name": "A",
            "_short_price": 0.55,
            "spread": abs(0.55 - 0.40) / 0.55,
            "_token_ids_a": ["tok_a_yes", "tok_a_no"],
            "_token_ids_b": ["tok_b_yes", "tok_b_no"],
            "_market_key_a": "mkt-a",
            "_market_key_b": "mkt-b",
            "_pair_source": "manual",
            "_long_market": {"id": "mkt-b", "clobTokenIds": '["tok_b_yes","tok_b_no"]'},
            "_short_market": {"id": "mkt-a", "clobTokenIds": '["tok_a_yes","tok_a_no"]'},
        }

    def test_books_not_swapped_when_b_is_long(self):
        opp = self._make_swapped_opp()
        # Long (B) book: ask available, bid missing. Short (A) book: bid
        # available, ask missing. If the books get swapped, the long leg has
        # no ask and the short leg no bid -> the opp is wrongly dropped.
        fetch = _fake_fetch({
            "mkt-b": _book(ask=0.41, bid=None, ask_size=300.0),
            "mkt-a": _book(ask=None, bid=0.54, bid_size=250.0),
        })
        out = _refine(
            [opp], fetch_clob=fetch,
            min_liquidity=10, max_spread_collapse=0.50,
        )
        assert len(out) == 1
        r = out[0]
        assert r["_long_ask"] == 0.41   # from market B's book (the long leg)
        assert r["_short_bid"] == 0.54  # from market A's book (the short leg)
        assert r["_long_depth"] == 300.0
        assert r["_short_depth"] == 250.0
