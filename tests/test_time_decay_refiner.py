"""Tests for the first-class Stage 2 refiner in scans/time_decay.py.

Focused on the new live-fetch behaviour added in PR B:
- Parallel CLOB ask re-fetch via _fetch_clob_for_market
- Live consensus refresh via signal_aggregator.get_consensus()
- Live hours-to-expiry from market.resolutionSource.timestamp

The legacy stored-price path is covered in tests/test_time_decay.py.
"""

import os
import sys
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# Hold a stable module reference and use ``patch.object(td_mod, ...)`` so
# the patch always targets the same module dict that
# ``_refine_time_decay_with_prices`` captured as its ``__globals__``.
# Other test files (test_time_decay.py) pop ``scans.time_decay`` from
# sys.modules between tests; if we use ``patch("scans.time_decay....")``
# the patch would re-import a fresh module dict that the live function
# never references, and the mock would never be seen.
import scans.time_decay as td_mod

_refine_time_decay_with_prices = td_mod._refine_time_decay_with_prices


def _make_opp(market_key="m1", hours=24.0, target=0.95, current=0.90,
              side="YES", prob=0.95):
    return {
        "type": "TimeDecay",
        "market_key": market_key,
        "_hours_to_expiry": hours,
        "_consensus_side": side,
        "_consensus_prob": prob,
        "_target_price": target,
        "_guaranteed_gain": target - current,
        "_current_price": current,
    }


def _make_market(market_key="m1", resolution_ts=None):
    market = {
        "question": f"Will {market_key} happen?",
        "clobTokenIds": '["yes_tok_1", "no_tok_1"]',
    }
    if resolution_ts is not None:
        market["resolutionSource"] = {"timestamp": resolution_ts}
    return market


# ---------------------------------------------------------------------------
# TestLiveCLOBRefetch
# ---------------------------------------------------------------------------


class TestLiveCLOBRefetch:
    """When markets_by_key is supplied the refiner re-fetches CLOB asks."""

    def test_drops_when_live_ask_above_target(self):
        """Live YES ask >= target_price → opportunity dropped."""
        opp = _make_opp(market_key="m1", target=0.95)
        markets = {"m1": _make_market("m1")}
        clob = {"yes_ask": 0.96, "no_ask": 0.04, "yes_ask_size": 100, "no_ask_size": 100}

        with patch.object(td_mod, "_fetch_clob_for_market",
                   return_value=(markets["m1"], clob)) as fetch:
            refined = _refine_time_decay_with_prices(
                [opp], markets_by_key=markets,
            )

        fetch.assert_called_once()
        assert refined == []

    def test_keeps_when_live_ask_below_target(self):
        """Live YES ask < target_price → opportunity retained."""
        opp = _make_opp(market_key="m1", target=0.95)
        markets = {"m1": _make_market("m1")}
        clob = {"yes_ask": 0.93, "no_ask": 0.07, "yes_ask_size": 100, "no_ask_size": 100}

        with patch.object(td_mod, "_fetch_clob_for_market",
                   return_value=(markets["m1"], clob)):
            refined = _refine_time_decay_with_prices(
                [opp], markets_by_key=markets,
            )

        assert len(refined) == 1
        # Refiner should write the live ask back into the opp dict.
        assert refined[0]["_current_price"] == 0.93
        assert refined[0]["_clob_depth"] == 100

    def test_handles_no_side_consensus(self):
        """NO-side opp uses no_ask, not yes_ask."""
        opp = _make_opp(market_key="m1", target=0.95, side="NO", prob=0.05)
        markets = {"m1": _make_market("m1")}
        # YES ask high but NO ask low → opp should survive
        clob = {"yes_ask": 0.97, "no_ask": 0.93, "yes_ask_size": 50, "no_ask_size": 80}

        with patch.object(td_mod, "_fetch_clob_for_market",
                   return_value=(markets["m1"], clob)):
            refined = _refine_time_decay_with_prices(
                [opp], markets_by_key=markets,
            )

        assert len(refined) == 1
        assert refined[0]["_current_price"] == 0.93
        assert refined[0]["_clob_depth"] == 80

    def test_missing_ask_fails_closed(self):
        """A bid is not an executable ask, so a missing ask drops the candidate."""
        opp = _make_opp(market_key="m1", target=0.95)
        markets = {"m1": _make_market("m1")}
        clob = {"yes_ask": None, "no_ask": 0.04, "yes_bid": 0.92, "no_bid": 0.05,
                "yes_ask_size": 0, "no_ask_size": 0,
                "yes_bid_size": 50, "no_bid_size": 50}

        with patch.object(td_mod, "_fetch_clob_for_market",
                   return_value=(markets["m1"], clob)):
            refined = _refine_time_decay_with_prices(
                [opp], markets_by_key=markets,
            )

        assert refined == []

    def test_clob_fetch_failure_fails_closed(self):
        """If the executable book cannot be fetched, the opp is dropped."""
        opp = _make_opp(market_key="m1", target=0.95, current=0.90)
        markets = {"m1": _make_market("m1")}

        with patch.object(td_mod, "_fetch_clob_for_market",
                   side_effect=RuntimeError("CLOB down")):
            refined = _refine_time_decay_with_prices(
                [opp], markets_by_key=markets,
            )

        assert refined == []


# ---------------------------------------------------------------------------
# TestLiveConsensusRefresh
# ---------------------------------------------------------------------------


class TestLiveConsensusRefresh:
    """When signal_aggregator is supplied the refiner refreshes consensus."""

    def test_drops_when_consensus_decayed(self):
        """Live consensus < min_consensus → opportunity dropped."""
        opp = _make_opp(market_key="m1", prob=0.95)
        markets = {"m1": _make_market("m1")}
        clob = {"yes_ask": 0.92, "no_ask": 0.08, "yes_ask_size": 50, "no_ask_size": 50}

        agg = MagicMock()
        agg.get_consensus.return_value = {"probability": 0.85}  # below 0.90 default

        with patch.object(td_mod, "_fetch_clob_for_market",
                   return_value=(markets["m1"], clob)):
            refined = _refine_time_decay_with_prices(
                [opp], markets_by_key=markets, signal_aggregator=agg,
            )

        assert refined == []

    def test_drops_when_consensus_side_flips(self):
        """Live consensus side flipped from YES to NO → drop."""
        opp = _make_opp(market_key="m1", side="YES", prob=0.95)
        markets = {"m1": _make_market("m1")}
        clob = {"yes_ask": 0.92, "no_ask": 0.08, "yes_ask_size": 50, "no_ask_size": 50}

        agg = MagicMock()
        # Still > 0.90 in absolute terms, but on the NO side now.
        agg.get_consensus.return_value = {"probability": 0.10}

        with patch.object(td_mod, "_fetch_clob_for_market",
                   return_value=(markets["m1"], clob)):
            refined = _refine_time_decay_with_prices(
                [opp], markets_by_key=markets, signal_aggregator=agg,
                min_consensus=0.05,  # let absolute prob through; only side check matters
            )

        assert refined == []

    def test_keeps_when_consensus_still_high(self):
        """Live consensus still strong → opportunity retained, prob updated."""
        opp = _make_opp(market_key="m1", prob=0.93)
        markets = {"m1": _make_market("m1")}
        clob = {"yes_ask": 0.92, "no_ask": 0.08, "yes_ask_size": 50, "no_ask_size": 50}

        agg = MagicMock()
        agg.get_consensus.return_value = {"probability": 0.97}

        with patch.object(td_mod, "_fetch_clob_for_market",
                   return_value=(markets["m1"], clob)):
            refined = _refine_time_decay_with_prices(
                [opp], markets_by_key=markets, signal_aggregator=agg,
            )

        assert len(refined) == 1
        assert refined[0]["_consensus_prob"] == 0.97
        assert refined[0]["_target_price"] == 0.97
        assert refined[0]["_guaranteed_gain"] == pytest.approx(0.05)

    def test_drops_when_refreshed_target_falls_below_live_ask(self):
        opp = _make_opp(market_key="m1", prob=0.97, target=0.97)
        markets = {"m1": _make_market("m1")}
        clob = {"yes_ask": 0.94, "no_ask": 0.06, "yes_ask_size": 50, "no_ask_size": 50}
        agg = MagicMock()
        agg.get_consensus.return_value = {"probability": 0.93}

        with patch.object(td_mod, "_fetch_clob_for_market",
                          return_value=(markets["m1"], clob)):
            refined = _refine_time_decay_with_prices(
                [opp], markets_by_key=markets, signal_aggregator=agg,
            )

        assert refined == []

    def test_aggregator_exception_fails_closed(self):
        """If the live consensus cannot be refreshed, the opp is dropped."""
        opp = _make_opp(market_key="m1")
        markets = {"m1": _make_market("m1")}
        clob = {"yes_ask": 0.92, "no_ask": 0.08, "yes_ask_size": 50, "no_ask_size": 50}

        agg = MagicMock()
        agg.get_consensus.side_effect = RuntimeError("aggregator down")

        with patch.object(td_mod, "_fetch_clob_for_market",
                   return_value=(markets["m1"], clob)):
            refined = _refine_time_decay_with_prices(
                [opp], markets_by_key=markets, signal_aggregator=agg,
            )

        assert refined == []


# ---------------------------------------------------------------------------
# TestLiveHoursToExpiry
# ---------------------------------------------------------------------------


class TestLiveHoursToExpiry:
    """Refiner re-derives hours-to-expiry from resolutionSource.timestamp."""

    def test_drops_when_live_hours_below_one(self):
        """Live resolution timestamp < 1h away → drop, even if stored is 24h."""
        # Stored claims 24h, but live resolution is in 30 minutes.
        now = 1_000_000.0
        opp = _make_opp(hours=24.0)
        markets = {"m1": _make_market("m1", resolution_ts=now + 1800)}  # 30min
        clob = {"yes_ask": 0.92, "no_ask": 0.08, "yes_ask_size": 50, "no_ask_size": 50}

        with patch.object(td_mod, "_fetch_clob_for_market",
                   return_value=(markets["m1"], clob)):
            refined = _refine_time_decay_with_prices(
                [opp], markets_by_key=markets, current_time=now,
            )

        assert refined == []

    def test_updates_hours_field_on_refresh(self):
        """Live recomputation overwrites _hours_to_expiry on the opp dict."""
        now = 1_000_000.0
        opp = _make_opp(hours=24.0)
        markets = {"m1": _make_market("m1", resolution_ts=now + 7200)}  # 2h
        clob = {"yes_ask": 0.92, "no_ask": 0.08, "yes_ask_size": 50, "no_ask_size": 50}

        with patch.object(td_mod, "_fetch_clob_for_market",
                   return_value=(markets["m1"], clob)):
            refined = _refine_time_decay_with_prices(
                [opp], markets_by_key=markets, current_time=now,
            )

        assert len(refined) == 1
        assert refined[0]["_hours_to_expiry"] == pytest.approx(2.0)

    @pytest.mark.parametrize("invalid_timestamp", [True, float("nan"), float("inf")])
    def test_invalid_live_resolution_timestamp_fails_closed(
            self, invalid_timestamp):
        opp = _make_opp(hours=24.0)
        markets = {"m1": _make_market("m1", resolution_ts=invalid_timestamp)}
        clob = {"yes_ask": 0.92, "no_ask": 0.08,
                "yes_ask_size": 50, "no_ask_size": 50}

        with patch.object(td_mod, "_fetch_clob_for_market",
                          return_value=(markets["m1"], clob)):
            refined = _refine_time_decay_with_prices(
                [opp], markets_by_key=markets, current_time=1_000_000.0,
            )

        assert refined == []


# ---------------------------------------------------------------------------
# TestBackwardCompat
# ---------------------------------------------------------------------------


class TestBackwardCompat:
    """Without markets_by_key, refiner mirrors the legacy stored-price path."""

    def test_legacy_signature_still_drops_expired(self):
        opp = _make_opp(hours=0.5, current=0.90, target=0.95)
        refined = _refine_time_decay_with_prices([opp])
        assert refined == []

    def test_legacy_signature_still_keeps_profitable(self):
        opp = _make_opp(hours=4.0, current=0.90, target=0.95)
        refined = _refine_time_decay_with_prices(
            [opp], current_prices={"m1": 0.93},
        )
        assert len(refined) == 1

    def test_empty_input_returns_empty(self):
        assert _refine_time_decay_with_prices([]) == []
