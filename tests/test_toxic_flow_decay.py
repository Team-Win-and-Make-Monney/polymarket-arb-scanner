"""Tests for ToxicFlowDetector decay, fill velocity, and dynamic quote tuning."""

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from market_maker import QuoteEngine, ToxicFlowDetector


# ---------------------------------------------------------------------------
# ToxicFlowDetector Unit Tests
# ---------------------------------------------------------------------------


class TestToxicFlowDetectorDecay:
    """Tests for exponential time decay in ToxicFlowDetector."""

    def test_initial_state_clean(self):
        detector = ToxicFlowDetector(decay_half_life_seconds=60.0)
        assert detector.get_toxicity("mkt-1") == 0.0
        assert detector.get_spread_multiplier("mkt-1") == 1.0
        assert detector.get_size_multiplier("mkt-1") == 1.0
        assert detector.get_fill_velocity("mkt-1") == 0
        assert detector.is_velocity_burst("mkt-1") is False
        assert detector.should_pause("mkt-1") is False

    def test_adverse_fills_increase_toxicity(self):
        detector = ToxicFlowDetector(decay_half_life_seconds=60.0)
        t0 = 1000.0

        # At least 3 fills required for toxicity calculation
        # 3 adverse fills: buy at 0.50 with mid dropping to 0.40
        detector.record_fill("mkt-1", "bid", 0.50, 10.0, 0.40, timestamp=t0)
        detector.record_fill("mkt-1", "bid", 0.50, 10.0, 0.40, timestamp=t0)
        detector.record_fill("mkt-1", "bid", 0.50, 10.0, 0.40, timestamp=t0)

        tox = detector.get_toxicity("mkt-1", now=t0)
        assert tox == 1.0

    def test_favorable_fills_do_not_increase_toxicity(self):
        detector = ToxicFlowDetector(decay_half_life_seconds=60.0)
        t0 = 1000.0

        # 3 favorable fills: buy at 0.50 with mid rising to 0.60
        detector.record_fill("mkt-1", "bid", 0.50, 10.0, 0.60, timestamp=t0)
        detector.record_fill("mkt-1", "bid", 0.50, 10.0, 0.60, timestamp=t0)
        detector.record_fill("mkt-1", "bid", 0.50, 10.0, 0.60, timestamp=t0)

        assert detector.get_toxicity("mkt-1", now=t0) == 0.0

    def test_exponential_time_decay(self):
        half_life = 60.0
        detector = ToxicFlowDetector(decay_half_life_seconds=half_life)
        t0 = 1000.0

        # Record 3 adverse fills at t0
        detector.record_fill("mkt-1", "bid", 0.50, 10.0, 0.40, timestamp=t0)
        detector.record_fill("mkt-1", "bid", 0.50, 10.0, 0.40, timestamp=t0)
        detector.record_fill("mkt-1", "bid", 0.50, 10.0, 0.40, timestamp=t0)

        initial_tox = detector.get_toxicity("mkt-1", now=t0)
        assert initial_tox == 1.0

        # Record a fresh benign/favorable fill at t0 + half_life
        t1 = t0 + half_life
        detector.record_fill("mkt-1", "bid", 0.50, 10.0, 0.60, timestamp=t1)

        # Older adverse fills decay with half-life while fresh fill has full weight
        decayed_tox = detector.get_toxicity("mkt-1", now=t1)
        assert decayed_tox < initial_tox
        # 3 adverse weights are 0.5 each (total 1.5), 1 benign weight is 1.0 (total 2.5). 1.5 / 2.5 = 0.60
        assert pytest.approx(decayed_tox, abs=0.05) == 0.60

        # As time marches further forward (5 half lives later), old fills decay down towards 0
        t5 = t0 + (5 * half_life)
        decayed_5 = detector.get_toxicity("mkt-1", now=t5)
        assert decayed_5 < initial_tox

    def test_disabled_flag_bypasses_controls(self, monkeypatch):
        import config as config_mod
        monkeypatch.setattr(config_mod, "MM_TOXIC_FLOW_ENABLED", False)

        detector = ToxicFlowDetector()
        t0 = 1000.0
        detector.record_fill("mkt-1", "bid", 0.50, 10.0, 0.40, timestamp=t0)
        detector.record_fill("mkt-1", "bid", 0.50, 10.0, 0.40, timestamp=t0)
        detector.record_fill("mkt-1", "bid", 0.50, 10.0, 0.40, timestamp=t0)

        assert detector.get_spread_multiplier("mkt-1", now=t0) == 1.0
        assert detector.get_size_multiplier("mkt-1", now=t0) == 1.0
        assert detector.should_pause("mkt-1", now=t0) is False


class TestToxicFlowFillVelocity:
    """Tests for fill velocity and burst detection."""

    def test_fill_velocity_counts_within_window(self):
        window = 30.0
        detector = ToxicFlowDetector(fill_velocity_window_seconds=window)
        t0 = 1000.0

        detector.record_fill("mkt-1", "bid", 0.50, 10.0, 0.50, timestamp=t0)
        detector.record_fill("mkt-1", "ask", 0.50, 10.0, 0.50, timestamp=t0 + 5.0)

        assert detector.get_fill_velocity("mkt-1", now=t0 + 10.0) == 2
        # Beyond window from first fill
        assert detector.get_fill_velocity("mkt-1", now=t0 + 32.0) == 1
        # Beyond window from all fills
        assert detector.get_fill_velocity("mkt-1", now=t0 + 40.0) == 0

    def test_burst_detection_threshold(self):
        window = 30.0
        burst_thresh = 3
        detector = ToxicFlowDetector(
            fill_velocity_window_seconds=window,
            fill_velocity_burst_threshold=burst_thresh,
        )
        t0 = 1000.0

        detector.record_fill("mkt-1", "bid", 0.50, 10.0, 0.50, timestamp=t0)
        detector.record_fill("mkt-1", "ask", 0.50, 10.0, 0.50, timestamp=t0 + 2.0)
        assert detector.is_velocity_burst("mkt-1", now=t0 + 5.0) is False

        # 3rd fill triggers burst
        detector.record_fill("mkt-1", "bid", 0.50, 10.0, 0.50, timestamp=t0 + 4.0)
        assert detector.is_velocity_burst("mkt-1", now=t0 + 5.0) is True

        # After window passes, burst clears
        assert detector.is_velocity_burst("mkt-1", now=t0 + 36.0) is False


class TestToxicFlowMultipliers:
    """Tests for spread widening and size tapering multipliers."""

    @pytest.fixture(autouse=True)
    def enable_toxic_flow(self, monkeypatch):
        import config as config_mod
        monkeypatch.setattr(config_mod, "MM_TOXIC_FLOW_ENABLED", True)

    def test_spread_multiplier_increases_with_toxicity_and_burst(self):
        detector = ToxicFlowDetector(
            toxicity_spread_factor=1.5,
            fill_velocity_window_seconds=30.0,
            fill_velocity_burst_threshold=3,
        )
        t0 = 1000.0

        # Baseline clean
        assert detector.get_spread_multiplier("mkt-1", now=t0) == 1.0

        # Record 3 adverse fills spaced out beyond burst window to avoid burst bonus
        detector.record_fill("mkt-1", "bid", 0.50, 10.0, 0.35, timestamp=t0 - 80.0)
        detector.record_fill("mkt-1", "bid", 0.50, 10.0, 0.35, timestamp=t0 - 40.0)
        detector.record_fill("mkt-1", "bid", 0.50, 10.0, 0.35, timestamp=t0)

        tox = detector.get_toxicity("mkt-1", now=t0)
        assert tox > 0.0
        assert detector.is_velocity_burst("mkt-1", now=t0) is False

        mult = detector.get_spread_multiplier("mkt-1", now=t0)
        expected = round(1.0 + (tox * 1.5), 2)
        assert mult == expected
        assert mult > 1.0

        # Now add 2 more fills within 2 seconds to trigger burst
        detector.record_fill("mkt-1", "bid", 0.50, 10.0, 0.35, timestamp=t0 + 1.0)
        detector.record_fill("mkt-1", "bid", 0.50, 10.0, 0.35, timestamp=t0 + 2.0)
        assert detector.is_velocity_burst("mkt-1", now=t0 + 3.0) is True

        burst_mult = detector.get_spread_multiplier("mkt-1", now=t0 + 3.0)
        # Should have +0.5 bonus for burst
        assert burst_mult >= 1.5

    def test_size_multiplier_decreases_with_toxicity_and_burst(self):
        detector = ToxicFlowDetector(
            size_taper_factor=0.4,
            min_size_fraction=0.2,
            fill_velocity_window_seconds=30.0,
            fill_velocity_burst_threshold=3,
        )
        t0 = 1000.0

        # Baseline clean
        assert detector.get_size_multiplier("mkt-1", now=t0) == 1.0

        # Record 3 adverse fills spaced out to avoid burst
        detector.record_fill("mkt-1", "bid", 0.50, 10.0, 0.35, timestamp=t0 - 80.0)
        detector.record_fill("mkt-1", "bid", 0.50, 10.0, 0.35, timestamp=t0 - 40.0)
        detector.record_fill("mkt-1", "bid", 0.50, 10.0, 0.35, timestamp=t0)

        tox = detector.get_toxicity("mkt-1", now=t0)
        size_mult = detector.get_size_multiplier("mkt-1", now=t0)
        expected = round(max(0.2, 1.0 - (tox * 0.4)), 2)
        assert size_mult == expected
        assert size_mult < 1.0
        assert size_mult == 0.6

        # Trigger burst with 2 more fills -> size clamped further by 0.5
        detector.record_fill("mkt-1", "bid", 0.50, 10.0, 0.35, timestamp=t0 + 1.0)
        detector.record_fill("mkt-1", "bid", 0.50, 10.0, 0.35, timestamp=t0 + 2.0)
        burst_size_mult = detector.get_size_multiplier("mkt-1", now=t0 + 3.0)
        assert burst_size_mult == 0.3
        assert burst_size_mult >= 0.2  # respects min_size_fraction


class TestQuoteEngineToxicitySpreadMultiplier:
    """Tests for QuoteEngine integration with toxicity spread multiplier."""

    def test_quote_engine_widens_spread_with_multiplier(self):
        engine = QuoteEngine(min_spread=0.04)
        mid = 0.50

        # Baseline quotes (multiplier 1.0)
        baseline = engine.calculate_quotes(mid, toxicity_spread_multiplier=1.0)
        baseline_spread = baseline["ask"] - baseline["bid"]
        assert baseline["toxicity_spread_multiplier"] == 1.0

        # Widened quotes (multiplier 1.5)
        widened = engine.calculate_quotes(mid, toxicity_spread_multiplier=1.5)
        widened_spread = widened["ask"] - widened["bid"]
        assert widened["toxicity_spread_multiplier"] == 1.5
        assert widened_spread > baseline_spread
        assert pytest.approx(widened_spread, abs=1e-4) == baseline_spread * 1.5
