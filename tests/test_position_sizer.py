"""Dedicated tests for position_sizer.py — Kelly + strategy-aware sizing.

Coverage:
- kelly_size formula (positive edge, zero edge, negative edge, division-by-zero)
- get_edge_estimate per opportunity layer (pure arb / near-arb / market-make / informed)
- _extract_net_roi handles both numeric and "5.23%" string formats
- _extract_confidence handles numeric, HIGH/MEDIUM/LOW labels, missing
- _extract_spread_width fallback chain
- _platform_min_order across single + cross-platform variants
- size_for_opportunity end-to-end:
    * pure arb uses full Kelly fraction
    * near-arb scales by 75%
    * market making uses spread-based fraction
    * informed trading uses confidence label map
    * max_fraction cap is respected
    * platform minimum-order floor is respected
    * skip-trade path when min order exceeds max position
- update_bankroll + constructor validation
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import position_sizer as ps_mod
from position_sizer import PositionSizer


# ---------------------------------------------------------------------------
# Constructor + bankroll
# ---------------------------------------------------------------------------


class TestConstructor:
    def test_rejects_negative_bankroll(self):
        with pytest.raises(ValueError):
            PositionSizer(bankroll=-1.0)

    def test_rejects_invalid_max_fraction(self):
        with pytest.raises(ValueError):
            PositionSizer(bankroll=100.0, max_fraction=0)
        with pytest.raises(ValueError):
            PositionSizer(bankroll=100.0, max_fraction=1.5)

    def test_rejects_invalid_kelly_fraction(self):
        with pytest.raises(ValueError):
            PositionSizer(bankroll=100.0, kelly_fraction=0)
        with pytest.raises(ValueError):
            PositionSizer(bankroll=100.0, kelly_fraction=2.0)

    def test_zero_bankroll_returns_zero_size(self):
        sizer = PositionSizer(bankroll=0.0)
        assert sizer.size_for_opportunity({"type": "Binary", "net_roi": 0.05}) == 0.0


class TestBankrollUpdate:
    def test_update_bankroll_changes_state(self):
        sizer = PositionSizer(bankroll=100.0)
        sizer.update_bankroll(250.0)
        assert sizer.bankroll == 250.0

    def test_update_bankroll_rejects_negative(self):
        sizer = PositionSizer(bankroll=100.0)
        with pytest.raises(ValueError):
            sizer.update_bankroll(-1.0)


# ---------------------------------------------------------------------------
# kelly_size formula
# ---------------------------------------------------------------------------


class TestKellySize:
    def test_basic_kelly(self):
        sizer = PositionSizer(bankroll=100.0)
        # f* = edge/odds = 0.05/0.05 = 1.0 (clamped)
        assert sizer.kelly_size(0.05, 0.05) == 1.0

    def test_negative_edge_returns_zero(self):
        sizer = PositionSizer(bankroll=100.0)
        assert sizer.kelly_size(-0.01, 0.10) == 0.0

    def test_zero_odds_returns_zero(self):
        sizer = PositionSizer(bankroll=100.0)
        assert sizer.kelly_size(0.05, 0.0) == 0.0

    def test_clamped_to_one(self):
        sizer = PositionSizer(bankroll=100.0)
        # If edge >> odds, the formula would exceed 1; must be clamped.
        assert sizer.kelly_size(0.20, 0.05) == 1.0


# ---------------------------------------------------------------------------
# Edge estimate
# ---------------------------------------------------------------------------


class TestEdgeEstimate:
    def setup_method(self):
        self.sizer = PositionSizer(bankroll=100.0)

    def test_pure_arb_uses_net_roi_directly(self):
        opp = {"type": "Binary", "net_roi": 0.05}
        assert self.sizer.get_edge_estimate(opp) == 0.05

    def test_near_arb_discounts_by_default_confidence(self):
        # No confidence field → fallback _NEAR_ARB_CONFIDENCE = 0.75
        opp = {"type": "StalePriceOpp", "net_roi": 0.10}
        assert self.sizer.get_edge_estimate(opp) == pytest.approx(0.075)

    def test_near_arb_explicit_numeric_confidence(self):
        opp = {"type": "ResolutionSnipeOpp", "net_roi": 0.10, "confidence": 0.5}
        assert self.sizer.get_edge_estimate(opp) == pytest.approx(0.05)

    def test_near_arb_label_confidence_high(self):
        opp = {"type": "StalePriceOpp", "net_roi": 0.10, "confidence": "HIGH"}
        # HIGH = 0.90
        assert self.sizer.get_edge_estimate(opp) == pytest.approx(0.09)

    def test_market_make_uses_net_roi(self):
        opp = {"type": "MarketMake", "net_roi": 0.03}
        assert self.sizer.get_edge_estimate(opp) == 0.03

    def test_informed_uses_signal_strength_and_divergence(self):
        opp = {
            "type": "EventDivergence",
            "net_roi": 0.05,
            "_signal_strength": 0.6,
            "_divergence": 0.10,
        }
        assert self.sizer.get_edge_estimate(opp) == pytest.approx(0.06)

    def test_informed_falls_back_to_default_signal(self):
        # Default _DEFAULT_SIGNAL_STRENGTH = 0.50; divergence falls back to abs(net_roi)
        opp = {"type": "Convergence", "net_roi": 0.10}
        assert self.sizer.get_edge_estimate(opp) == pytest.approx(0.05)

    def test_unknown_type_falls_back_to_net_roi(self):
        opp = {"type": "Whatever", "net_roi": 0.04}
        assert self.sizer.get_edge_estimate(opp) == 0.04

    def test_negative_edge_clamped_to_zero(self):
        opp = {"type": "Binary", "net_roi": -0.05}
        assert self.sizer.get_edge_estimate(opp) == 0.0


# ---------------------------------------------------------------------------
# Format helpers
# ---------------------------------------------------------------------------


class TestExtractNetRoi:
    def test_numeric_pass_through(self):
        assert PositionSizer._extract_net_roi({"net_roi": 0.05}) == 0.05

    def test_string_with_percent(self):
        # "5.23%" → 0.0523
        assert PositionSizer._extract_net_roi({"net_roi": "5.23%"}) == pytest.approx(0.0523)

    def test_string_with_sub_one_percent(self):
        # "0.50%" must be 0.005, not 0.50 (which would be 50% ROI)
        assert PositionSizer._extract_net_roi({"net_roi": "0.50%"}) == pytest.approx(0.0050)
        assert PositionSizer._extract_net_roi({"net_roi": "0.15%"}) == pytest.approx(0.0015)
        assert PositionSizer._extract_net_roi({"net_roi": "0.99%"}) == pytest.approx(0.0099)

    def test_string_with_exact_one_percent(self):
        # "1.00%" must be 0.01, not 1.0 (which would be 100% ROI)
        assert PositionSizer._extract_net_roi({"net_roi": "1.00%"}) == pytest.approx(0.01)

    def test_string_with_negative_percent(self):
        assert PositionSizer._extract_net_roi({"net_roi": "-0.50%"}) == pytest.approx(-0.0050)
        assert PositionSizer._extract_net_roi({"net_roi": "-5.23%"}) == pytest.approx(-0.0523)

    def test_string_with_zero_percent(self):
        assert PositionSizer._extract_net_roi({"net_roi": "0%"}) == 0.0
        assert PositionSizer._extract_net_roi({"net_roi": "0.00%"}) == 0.0

    def test_string_without_percent_already_decimal(self):
        # "0.05" → 0.05
        assert PositionSizer._extract_net_roi({"net_roi": "0.05"}) == 0.05

    def test_invalid_string_returns_zero(self):
        assert PositionSizer._extract_net_roi({"net_roi": "garbage"}) == 0.0

    def test_missing_returns_zero(self):
        assert PositionSizer._extract_net_roi({}) == 0.0


class TestExtractConfidence:
    def test_numeric_pass_through(self):
        assert PositionSizer._extract_confidence({"confidence": 0.6}) == 0.6

    def test_numeric_clamped(self):
        assert PositionSizer._extract_confidence({"confidence": 1.5}) == 1.0
        assert PositionSizer._extract_confidence({"confidence": -0.5}) == 0.0

    def test_label_high(self):
        assert PositionSizer._extract_confidence({"confidence": "HIGH"}) == 0.90

    def test_label_low(self):
        assert PositionSizer._extract_confidence({"confidence": "LOW"}) == 0.50

    def test_missing_returns_default(self):
        assert PositionSizer._extract_confidence({}) == ps_mod._NEAR_ARB_CONFIDENCE


class TestExtractSpreadWidth:
    def test_explicit_field(self):
        assert PositionSizer._extract_spread_width({"_spread_width": 0.04}) == 0.04

    def test_dollar_string(self):
        assert PositionSizer._extract_spread_width({"gross_spread": "$0.0234"}) == pytest.approx(0.0234)

    def test_falls_back_to_net_roi(self):
        assert PositionSizer._extract_spread_width({"net_roi": 0.05}) == 0.05


class TestPlatformMinOrder:
    def test_single_polymarket(self):
        assert ps_mod._platform_min_order({"type": "Binary"}) == 0.01

    def test_single_betfair(self):
        assert ps_mod._platform_min_order({"type": "BetfairBackAll"}) == 2.50

    def test_explicit_platforms_takes_max(self):
        # Multi-cross with PM (0.01) and Smarkets (6.25) → max = 6.25
        opp = {"type": "MultiCross", "_platforms": ["polymarket", "smarkets"]}
        assert ps_mod._platform_min_order(opp) == 6.25

    def test_cross_platform_uses_legs(self):
        opp = {
            "type": "Cross",
            "_platform_a": "polymarket",
            "_platform_b": "matchbook",
        }
        # max(0.01, 5.50) = 5.50
        assert ps_mod._platform_min_order(opp) == 5.50

    def test_unknown_falls_back(self):
        assert ps_mod._platform_min_order({"type": "Unknown"}) == 0.01


# ---------------------------------------------------------------------------
# size_for_opportunity end-to-end
# ---------------------------------------------------------------------------


class TestSizeForOpportunity:
    def test_zero_edge_returns_zero(self):
        sizer = PositionSizer(bankroll=1000.0)
        assert sizer.size_for_opportunity({"type": "Binary", "net_roi": 0}) == 0.0

    def test_pure_arb_respects_max_fraction_cap(self):
        sizer = PositionSizer(bankroll=1000.0, max_fraction=0.10, kelly_fraction=1.0)
        opp = {"type": "Binary", "net_roi": 0.05}
        size = sizer.size_for_opportunity(opp)
        # Capped at 10% of $1000 = $100
        assert size == pytest.approx(100.0)

    def test_kelly_fraction_scales_size(self):
        sizer_full = PositionSizer(bankroll=1000.0, max_fraction=1.0, kelly_fraction=1.0)
        sizer_half = PositionSizer(bankroll=1000.0, max_fraction=1.0, kelly_fraction=0.5)
        opp = {"type": "Binary", "net_roi": 0.05}
        full = sizer_full.size_for_opportunity(opp)
        half = sizer_half.size_for_opportunity(opp)
        assert half == pytest.approx(full * 0.5)

    def test_near_arb_smaller_than_pure_arb(self):
        sizer = PositionSizer(bankroll=1000.0, max_fraction=1.0, kelly_fraction=1.0)
        pure = sizer.size_for_opportunity({"type": "Binary", "net_roi": 0.05})
        near = sizer.size_for_opportunity({"type": "StalePriceOpp", "net_roi": 0.05})
        assert near < pure  # near-arb has the 0.75 strategy multiplier

    def test_informed_label_high_larger_than_low(self):
        sizer = PositionSizer(bankroll=1000.0, max_fraction=1.0, kelly_fraction=1.0)
        high = sizer.size_for_opportunity({
            "type": "EventDivergence", "net_roi": 0.05,
            "_divergence": 0.10, "_signal_strength": 0.5,
            "confidence": "HIGH",
        })
        low = sizer.size_for_opportunity({
            "type": "EventDivergence", "net_roi": 0.05,
            "_divergence": 0.10, "_signal_strength": 0.5,
            "confidence": "LOW",
        })
        assert high > low

    def test_min_order_floor_applied(self):
        # Small bankroll, low Kelly fraction → computed size below the
        # Betfair $2.50 floor. Max position = $10 (bankroll × 1.0) is still
        # >= $2.50 so the floor is applied (not skipped).
        sizer = PositionSizer(bankroll=10.0, max_fraction=1.0, kelly_fraction=0.10)
        opp = {"type": "BetfairBackAll", "net_roi": 0.05}
        size = sizer.size_for_opportunity(opp)
        # Pure-arb Kelly saturates at 1.0 → fraction 1.0 × 1.0 × 0.10 = 0.10
        # → raw size $1.00, below $2.50 floor, max position $10 → floor wins.
        assert size == pytest.approx(2.50)

    def test_skip_when_min_exceeds_max_position(self):
        # Tiny bankroll, big platform min.
        sizer = PositionSizer(bankroll=10.0, max_fraction=0.10, kelly_fraction=1.0)
        opp = {"type": "BetfairBackAll", "net_roi": 0.05}
        # Max position = 10 * 0.10 = $1; Betfair min = $2.50 → skip.
        assert sizer.size_for_opportunity(opp) == 0.0
