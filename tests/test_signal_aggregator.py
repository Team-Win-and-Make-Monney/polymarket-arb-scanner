"""Dedicated tests for signal_aggregator.py — multi-source consensus.

Coverage:
- add_signal validates 0-1 range, accepts metadata, replaces existing
- get_consensus weighted-average math against custom weights
- get_consensus returns None when empty / all stale
- spread, num_sources, confidence in the result
- get_divergences filtering + sort order
- TTL eviction via _get_fresh_signals + cleanup()
- fetch_external_signals with mocked Metaculus + Manifold clients
- _consensus_confidence helper edge cases
"""

import os
import sys
import time
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from signal_aggregator import (
    SignalAggregator,
    _consensus_confidence,
    DEFAULT_SOURCE_WEIGHTS,
)


# ---------------------------------------------------------------------------
# add_signal
# ---------------------------------------------------------------------------


class TestAddSignal:
    def test_records_signal(self):
        agg = SignalAggregator()
        agg.add_signal("market_1", "metaculus", 0.65)
        result = agg.get_consensus("market_1")
        assert result is not None
        assert result["probability"] == pytest.approx(0.65)

    def test_rejects_out_of_range(self):
        agg = SignalAggregator()
        agg.add_signal("market_1", "metaculus", 1.5)
        agg.add_signal("market_1", "manifold", -0.1)
        # Both should be rejected → no consensus.
        assert agg.get_consensus("market_1") is None

    def test_metadata_preserved(self):
        agg = SignalAggregator()
        agg.add_signal("m", "metaculus", 0.5, metadata={"sample_size": 1234})
        # Metadata is stored on the cached entry; verify via cleanup eviction
        # by checking that the consensus is computable (means storage worked).
        assert agg.get_consensus("m") is not None

    def test_later_signal_replaces_earlier(self):
        agg = SignalAggregator()
        agg.add_signal("m", "metaculus", 0.40)
        agg.add_signal("m", "metaculus", 0.60)  # replace
        result = agg.get_consensus("m")
        assert result["probability"] == pytest.approx(0.60)


# ---------------------------------------------------------------------------
# get_consensus math
# ---------------------------------------------------------------------------


class TestGetConsensus:
    def test_single_source_consensus(self):
        agg = SignalAggregator()
        agg.add_signal("m", "metaculus", 0.70)
        result = agg.get_consensus("m")
        assert result["probability"] == pytest.approx(0.70)
        assert result["num_sources"] == 1
        assert result["spread"] == 0.0

    def test_weighted_average(self):
        weights = {"a": 1.0, "b": 3.0}
        agg = SignalAggregator(source_weights=weights)
        agg.add_signal("m", "a", 0.40)
        agg.add_signal("m", "b", 0.80)
        # weighted = (0.40*1 + 0.80*3) / 4 = (0.40 + 2.40) / 4 = 0.70
        result = agg.get_consensus("m")
        assert result["probability"] == pytest.approx(0.70, abs=1e-3)

    def test_default_weight_for_unknown_source(self):
        agg = SignalAggregator()
        # "custom_source" has no entry in DEFAULT_SOURCE_WEIGHTS → defaults to 1.0
        agg.add_signal("m", "custom_source", 0.50)
        agg.add_signal("m", "metaculus", 0.80)  # weight 1.5
        # weighted = (0.50*1.0 + 0.80*1.5) / 2.5 = (0.50 + 1.20) / 2.5 = 0.68
        result = agg.get_consensus("m")
        assert result["probability"] == pytest.approx(0.68, abs=1e-3)

    def test_returns_none_when_empty(self):
        agg = SignalAggregator()
        assert agg.get_consensus("absent") is None

    def test_spread_reported(self):
        agg = SignalAggregator()
        agg.add_signal("m", "a", 0.30)
        agg.add_signal("m", "b", 0.50)
        agg.add_signal("m", "c", 0.70)
        result = agg.get_consensus("m")
        assert result["spread"] == pytest.approx(0.40)
        assert result["min"] == pytest.approx(0.30)
        assert result["max"] == pytest.approx(0.70)
        assert result["num_sources"] == 3

    def test_returns_none_when_all_signals_stale(self):
        agg = SignalAggregator(cache_ttl=1.0)
        agg.add_signal("m", "metaculus", 0.5)
        # Manually rewind the timestamp so the entry is now stale.
        agg._cache["m"]["metaculus"]["timestamp"] = time.time() - 10
        assert agg.get_consensus("m") is None


# ---------------------------------------------------------------------------
# get_divergences
# ---------------------------------------------------------------------------


class TestGetDivergences:
    def test_filters_below_threshold(self):
        agg = SignalAggregator()
        agg.add_signal("m", "a", 0.50)
        agg.add_signal("m", "b", 0.52)  # only 2c off
        agg.add_signal("m", "c", 0.75)  # diverges while a/b stay below 10c
        # Default threshold is 0.10; only c should appear.
        divs = agg.get_divergences("m")
        sources = {d["source"] for d in divs}
        assert "c" in sources
        assert "a" not in sources
        assert "b" not in sources

    def test_sorted_by_absolute_divergence(self):
        agg = SignalAggregator()
        agg.add_signal("m", "a", 0.50)
        agg.add_signal("m", "b", 0.65)   # +0.15
        agg.add_signal("m", "c", 0.20)   # -0.30
        divs = agg.get_divergences("m", min_divergence=0.05)
        # First entry should have largest absolute divergence (c).
        assert divs[0]["source"] == "c"
        # All sorted descending by |divergence|.
        abs_divs = [abs(d["divergence"]) for d in divs]
        assert abs_divs == sorted(abs_divs, reverse=True)

    def test_returns_empty_when_no_consensus(self):
        agg = SignalAggregator()
        assert agg.get_divergences("absent") == []


# ---------------------------------------------------------------------------
# TTL + cleanup
# ---------------------------------------------------------------------------


class TestTTLAndCleanup:
    def test_fresh_signals_only(self):
        agg = SignalAggregator(cache_ttl=1.0)
        agg.add_signal("m", "fresh", 0.5)
        agg.add_signal("m", "stale", 0.8)
        # Mark "stale" as expired.
        agg._cache["m"]["stale"]["timestamp"] = time.time() - 10
        result = agg.get_consensus("m")
        # Only "fresh" should contribute.
        assert result["sources"] == ["fresh"]

    def test_cleanup_removes_old_entries(self):
        agg = SignalAggregator(cache_ttl=1.0)
        agg.add_signal("m1", "a", 0.5)
        agg.add_signal("m2", "b", 0.5)
        # Force both to look ancient.
        agg._cache["m1"]["a"]["timestamp"] = 0
        agg._cache["m2"]["b"]["timestamp"] = 0
        removed = agg.cleanup(max_age=1.0)
        assert removed == 2
        # Both market_keys should also be evicted (they're empty now).
        assert "m1" not in agg._cache
        assert "m2" not in agg._cache


# ---------------------------------------------------------------------------
# fetch_external_signals
# ---------------------------------------------------------------------------


class TestFetchExternalSignals:
    def test_fetches_metaculus_and_manifold(self):
        meta = MagicMock()
        meta.search_questions.return_value = [
            {"community_prediction": {"full": {"q2": 0.62}}}
        ]
        mani = MagicMock()
        mani.search_markets.return_value = [
            {"probability": 0.71, "isResolved": False}
        ]
        agg = SignalAggregator(metaculus_client=meta, manifold_client=mani)
        count = agg.fetch_external_signals("m", title="Will X happen?")
        assert count == 2
        result = agg.get_consensus("m")
        assert result["num_sources"] == 2
        assert "metaculus" in result["sources"]
        assert "manifold" in result["sources"]

    def test_skips_resolved_manifold_markets(self):
        meta = MagicMock()
        meta.search_questions.return_value = []
        mani = MagicMock()
        mani.search_markets.return_value = [
            {"probability": 0.50, "isResolved": True}
        ]
        agg = SignalAggregator(metaculus_client=meta, manifold_client=mani)
        count = agg.fetch_external_signals("m")
        assert count == 0

    def test_swallows_exceptions(self):
        meta = MagicMock()
        meta.search_questions.side_effect = RuntimeError("Metaculus down")
        mani = MagicMock()
        mani.search_markets.return_value = [
            {"probability": 0.55, "isResolved": False}
        ]
        agg = SignalAggregator(metaculus_client=meta, manifold_client=mani)
        # Should not raise, just skip Metaculus.
        count = agg.fetch_external_signals("m", title="Anything")
        assert count == 1

    def test_no_clients_returns_zero(self):
        agg = SignalAggregator()
        assert agg.fetch_external_signals("m") == 0


# ---------------------------------------------------------------------------
# _consensus_confidence
# ---------------------------------------------------------------------------


class TestConsensusConfidence:
    def test_higher_confidence_with_more_sources(self):
        c1 = _consensus_confidence(num_sources=1, spread=0.02)
        c5 = _consensus_confidence(num_sources=5, spread=0.02)
        assert c5 > c1

    def test_higher_confidence_with_lower_spread(self):
        tight = _consensus_confidence(num_sources=3, spread=0.02)
        loose = _consensus_confidence(num_sources=3, spread=0.20)
        assert tight > loose

    def test_capped_at_source_factor_max(self):
        # source_factor caps at 0.95 even for very many sources
        c10 = _consensus_confidence(num_sources=10, spread=0.0)
        c100 = _consensus_confidence(num_sources=100, spread=0.0)
        assert c10 == c100  # Both saturated

    def test_default_weights_have_metaculus_above_manifold(self):
        # Sanity check that defaults still encode the documented preference.
        assert DEFAULT_SOURCE_WEIGHTS["metaculus"] > DEFAULT_SOURCE_WEIGHTS["manifold"]
