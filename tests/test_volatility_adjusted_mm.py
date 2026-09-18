"""Tests for Strategy VolatilityAdjustedMM — volatility-scaled MM spreads.

Covers:
- scan_volatility_adjusted_mm returns [] when MM_VOLATILITY_ADJUSTED_ENABLED
  is false (default).
- scan_volatility_adjusted_mm returns [] when no market_keys are supplied.
- scan_volatility_adjusted_mm returns [] when the tracker reports the base
  multiplier (no volatility adjustment needed) for every market.
- scan_volatility_adjusted_mm emits a VolatilityAdjustedMM opp with the
  expected shape for markets whose multiplier exceeds the base.
- QuoteEngine.calculate_quotes consults the volatility tracker when a
  market_key is passed (integration check that the runtime hook is wired).
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


@pytest.fixture(autouse=True)
def _isolate_modules():
    for mod in ("scans.volatility_adjusted_mm",):
        sys.modules.pop(mod, None)
    yield


class _FakeTracker:
    """In-memory VolatilityTracker stand-in."""

    def __init__(self, multipliers: dict[str, float] | None = None,
                 volatilities: dict[str, float] | None = None):
        self._multipliers = multipliers or {}
        self._volatilities = volatilities or {}

    def get_spread_multiplier(self, market_key, base_multiplier=1.0,
                              max_multiplier=3.0):
        return self._multipliers.get(market_key, base_multiplier)

    def get_volatility(self, market_key):
        return self._volatilities.get(market_key, 0.0)


class TestScanVolMMFlagGate:
    def test_returns_empty_when_flag_disabled(self, monkeypatch):
        import config as config_mod
        monkeypatch.setattr(config_mod, "MM_VOLATILITY_ADJUSTED_ENABLED", False)
        from scans.volatility_adjusted_mm import scan_volatility_adjusted_mm
        tracker = _FakeTracker(multipliers={"m": 2.5})
        assert scan_volatility_adjusted_mm(["m"], tracker=tracker) == []


class TestScanVolMMEmptyInputs:
    def test_returns_empty_when_no_market_keys(self, monkeypatch):
        import config as config_mod
        monkeypatch.setattr(config_mod, "MM_VOLATILITY_ADJUSTED_ENABLED", True)
        from scans.volatility_adjusted_mm import scan_volatility_adjusted_mm
        tracker = _FakeTracker()
        assert scan_volatility_adjusted_mm([], tracker=tracker) == []
        assert scan_volatility_adjusted_mm(None, tracker=tracker) == []


class TestScanVolMMNoElevation:
    def test_returns_empty_when_all_markets_at_base(self, monkeypatch):
        import config as config_mod
        monkeypatch.setattr(config_mod, "MM_VOLATILITY_ADJUSTED_ENABLED", True)
        from scans.volatility_adjusted_mm import scan_volatility_adjusted_mm
        tracker = _FakeTracker(multipliers={"calm-1": 1.0, "calm-2": 1.0})
        assert scan_volatility_adjusted_mm(["calm-1", "calm-2"],
                                            tracker=tracker) == []


class TestScanVolMMEmitsOpp:
    def test_emits_opp_for_elevated_multiplier(self, monkeypatch):
        import config as config_mod
        monkeypatch.setattr(config_mod, "MM_VOLATILITY_ADJUSTED_ENABLED", True)
        from scans.volatility_adjusted_mm import scan_volatility_adjusted_mm
        tracker = _FakeTracker(
            multipliers={"calm": 1.0, "wild": 2.4},
            volatilities={"calm": 0.001, "wild": 0.045},
        )
        opps = scan_volatility_adjusted_mm(["calm", "wild"], tracker=tracker)
        assert len(opps) == 1
        opp = opps[0]
        assert opp["type"] == "VolatilityAdjustedMM"
        assert opp["_market_key"] == "wild"
        assert opp["_spread_multiplier"] == 2.4
        assert opp["_volatility"] == 0.045
        assert opp["_layer"] == 3


class TestQuoteEngineVolatilityHook:
    def test_calculate_quotes_consults_volatility_tracker_when_market_key_given(
        self, monkeypatch,
    ):
        """QuoteEngine.calculate_quotes must read get_spread_multiplier when
        a market_key is supplied. We confirm by widening the multiplier and
        observing a wider spread.
        """
        import market_maker as mm_mod
        import config as config_mod
        monkeypatch.setattr(config_mod, "MM_VOLATILITY_ADJUSTED_ENABLED", True)

        wide_tracker = _FakeTracker(multipliers={"vol-market": 3.0})
        monkeypatch.setattr(mm_mod, "_volatility_tracker", wide_tracker)
        monkeypatch.setattr(mm_mod, "get_volatility_tracker",
                            lambda: wide_tracker)

        engine = mm_mod.QuoteEngine(min_spread=0.04)
        base = engine.calculate_quotes(0.50)
        widened = engine.calculate_quotes(0.50, market_key="vol-market")
        assert widened["spread"] > base["spread"]


# ---------------------------------------------------------------------------
# Finding #9: VolatilityTracker.has_min_samples — distinguishes "genuinely
# calm" from "not enough data yet" so a safety gate (mm_pilot's G8) can fail
# closed on the latter instead of trusting get_volatility()'s 0.0 default.
# Fail-before: has_min_samples did not exist on VolatilityTracker at all.
# ---------------------------------------------------------------------------

class TestVolatilityTrackerHasMinSamples:
    def test_false_before_min_samples_recorded(self):
        from market_maker import VolatilityTracker
        tracker = VolatilityTracker(min_samples=5)
        for _ in range(4):
            tracker.record_price("m1", 0.50)
        assert tracker.has_min_samples("m1") is False
        # get_volatility agrees this is "no reading yet" — has_min_samples
        # must reflect the SAME threshold get_volatility uses internally.
        assert tracker.get_volatility("m1") == 0.0

    def test_true_at_exactly_min_samples(self):
        from market_maker import VolatilityTracker
        tracker = VolatilityTracker(min_samples=5)
        for _ in range(5):
            tracker.record_price("m1", 0.50)
        assert tracker.has_min_samples("m1") is True

    def test_false_for_never_seen_market(self):
        from market_maker import VolatilityTracker
        tracker = VolatilityTracker(min_samples=5)
        assert tracker.has_min_samples("never-seen") is False

    def test_true_does_not_mean_nonzero_volatility(self):
        """A market can have enough samples AND genuinely be calm (constant
        price) — has_min_samples and "reads as calm" are independent axes;
        this just confirms enough-samples alone doesn't force a false
        positive on volatility."""
        from market_maker import VolatilityTracker
        tracker = VolatilityTracker(min_samples=3)
        for _ in range(5):
            tracker.record_price("m1", 0.50)  # constant price, zero variance
        assert tracker.has_min_samples("m1") is True
        assert tracker.get_volatility("m1") == 0.0
