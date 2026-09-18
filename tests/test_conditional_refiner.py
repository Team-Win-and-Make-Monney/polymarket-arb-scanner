"""Tests for _refine_conditional_with_clob in scans/conditional.py.

Audit #77 regression: all three legs were priced from ``yes_ask`` regardless
of ``_direction``, but each direction SELLS at least one leg — sell proceeds
come from the bid, not the ask. Direction semantics
(fees.net_profit_conditional):

- BUY_CONDITIONAL:   buy P(X|Y) + P(Y) at the ask, SELL P(X) at the bid.
- BUY_UNCONDITIONAL: buy P(X) at the ask, SELL P(X|Y) + P(Y) at the bid.
"""

import sys
import os
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import scans.conditional as cond_mod  # noqa: E402

_refine = cond_mod._refine_conditional_with_clob


def _mk_market(key: str) -> dict:
    return {"id": key, "condition_id": key}


def _mk_opp(direction: str) -> dict:
    return {
        "type": "ConditionalArb",
        "market": "test conditional",
        "net_profit": 0.10,
        "_direction": direction,
        "_conditional_market": _mk_market("cond"),
        "_condition_market": _mk_market("y"),
        "_unconditional_market": _mk_market("x"),
        "_p_x_given_y": 0.50,
        "_p_y": 0.50,
        "_p_x": 0.50,
    }


def _book(bid: float, ask: float, ask_size: float = 100,
          bid_size: float = 100) -> dict:
    return {
        "yes_ask": ask, "yes_ask_size": ask_size,
        "yes_bid": bid, "yes_bid_size": bid_size,
        "no_ask": None, "no_ask_size": 0,
        "no_bid": None, "no_bid_size": 0,
    }


def _fake_fetch(books: dict):
    def fetch(market, _price_cache=None):
        return market, books.get(market.get("id"))
    return fetch


class TestDirectionalLegPricing:
    def test_buy_conditional_sells_unconditional_at_bid(self):
        """BUY_CONDITIONAL: P(X|Y), P(Y) from ask; P(X) (sell leg) from bid."""
        books = {
            "cond": _book(bid=0.38, ask=0.42),
            "y": _book(bid=0.48, ask=0.52),
            "x": _book(bid=0.61, ask=0.69),
        }
        opp = _mk_opp("BUY_CONDITIONAL")
        with patch.object(cond_mod, "_fetch_clob_for_market",
                          side_effect=_fake_fetch(books)):
            refined = _refine([opp], {}, min_profit=0.0001)

        assert len(refined) == 1
        out = refined[0]
        assert out["_p_x_given_y"] == 0.42  # buy leg -> ask
        assert out["_p_y"] == 0.52          # buy leg -> ask
        assert out["_p_x"] == 0.61          # SELL leg -> bid, not 0.69

    def test_buy_unconditional_sells_conditional_legs_at_bid(self):
        """BUY_UNCONDITIONAL: P(X) from ask; P(X|Y), P(Y) (sell legs) from bid."""
        books = {
            "cond": _book(bid=0.58, ask=0.66),
            "y": _book(bid=0.88, ask=0.94),
            "x": _book(bid=0.40, ask=0.44),
        }
        opp = _mk_opp("BUY_UNCONDITIONAL")
        with patch.object(cond_mod, "_fetch_clob_for_market",
                          side_effect=_fake_fetch(books)):
            refined = _refine([opp], {}, min_profit=0.0001)

        assert len(refined) == 1
        out = refined[0]
        assert out["_p_x_given_y"] == 0.58  # SELL leg -> bid, not 0.66
        assert out["_p_y"] == 0.88          # SELL leg -> bid, not 0.94
        assert out["_p_x"] == 0.44          # buy leg -> ask

    def test_missing_bid_drops_opp(self):
        """Audit #77 round 2: a sell leg with no live bid is unexecutable —
        there is nothing to sell into — so the opp is DROPPED, never priced
        from the stale Stage-1 value."""
        books = {
            "cond": _book(bid=0.38, ask=0.42),
            "y": _book(bid=0.48, ask=0.52),
            "x": {"yes_ask": 0.69, "yes_ask_size": 100,
                  "yes_bid": None, "yes_bid_size": 0,
                  "no_ask": None, "no_ask_size": 0,
                  "no_bid": None, "no_bid_size": 0},
        }
        opp = _mk_opp("BUY_CONDITIONAL")
        opp["_p_x"] = 0.63
        with patch.object(cond_mod, "_fetch_clob_for_market",
                          side_effect=_fake_fetch(books)):
            refined = _refine([opp], {}, min_profit=0.0001)

        assert refined == []

    def test_missing_book_on_sell_leg_drops_opp(self):
        """No book at all for the sell-leg market -> no bid -> drop."""
        books = {
            "cond": _book(bid=0.38, ask=0.42),
            "y": _book(bid=0.48, ask=0.52),
            # "x" (the BUY_CONDITIONAL sell leg) has no book at all.
        }
        opp = _mk_opp("BUY_CONDITIONAL")
        with patch.object(cond_mod, "_fetch_clob_for_market",
                          side_effect=_fake_fetch(books)):
            refined = _refine([opp], {}, min_profit=0.0001)

        assert refined == []

    def test_clob_depth_uses_bid_size_for_sell_legs(self):
        """Depth per leg must match the traded side: ask size for buy legs,
        bid size for the sell leg."""
        books = {
            "cond": _book(bid=0.38, ask=0.42, ask_size=90, bid_size=5),
            "y": _book(bid=0.48, ask=0.52, ask_size=80, bid_size=5),
            "x": _book(bid=0.61, ask=0.69, ask_size=999, bid_size=7),
        }
        opp = _mk_opp("BUY_CONDITIONAL")
        with patch.object(cond_mod, "_fetch_clob_for_market",
                          side_effect=_fake_fetch(books)):
            refined = _refine([opp], {}, min_profit=0.0001)

        assert len(refined) == 1
        # min(ask 90, ask 80, BID 7) — the old code read the sell leg's ask
        # size (999) and reported min(90, 80, 999) = 80.
        assert refined[0]["_clob_depth"] == 7

    def test_missing_buy_book_forces_zero_depth(self):
        """Stage-1 price fallback must not imply verified executable depth."""
        books = {
            # Conditional buy leg is missing entirely.
            "y": _book(bid=0.48, ask=0.52, ask_size=80),
            "x": _book(bid=0.61, ask=0.69, bid_size=70),
        }
        opp = _mk_opp("BUY_CONDITIONAL")
        with (
            patch.object(
                cond_mod, "_fetch_clob_for_market",
                side_effect=_fake_fetch(books),
            ),
            patch(
                "fees.net_profit_conditional",
                return_value={"net_profit": 0.1, "net_roi": 0.1},
            ),
        ):
            refined = _refine([opp], {}, min_profit=0.0001)

        assert len(refined) == 1
        assert refined[0]["_clob_depth"] == 0
