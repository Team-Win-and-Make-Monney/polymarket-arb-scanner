"""Tests for scans/helpers.py — capital efficiency scoring and resolution filtering."""

import pytest
import sys, os
from datetime import datetime, timezone, timedelta
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from scans.helpers import capital_efficiency_score, _within_resolution_window, filter_dust, _days_to_resolution


class TestCapitalEfficiencyScore:
    def test_high_roi_deep_book_scores_highest(self):
        opp = {"net_profit": 0.10, "total_cost": "$0.9000", "_clob_depth": 100.0}
        # ROI = 0.10/0.90 = 11.1%, depth capped at 50 -> score = 0.111 * 50 = ~5.56
        score = capital_efficiency_score(opp)
        assert score == pytest.approx(0.10 / 0.90 * 50, rel=1e-3)

    def test_none_depth_does_not_crash_the_priority_sort(self):
        opp = {"net_profit": 0.02, "total_cost": "$0.9500", "_clob_depth": None}
        assert capital_efficiency_score(opp) == pytest.approx(0.02 / 0.95)

    def test_low_roi_scores_lower(self):
        opp = {"net_profit": 0.005, "total_cost": "$0.9950", "_clob_depth": 100.0}
        # ROI = 0.005/0.995 = 0.50%, depth capped at 50 -> score = ~0.251
        score = capital_efficiency_score(opp)
        assert score == pytest.approx(0.005 / 0.995 * 50, rel=1e-3)

    def test_shallow_book_reduces_score(self):
        opp = {"net_profit": 0.10, "total_cost": "$0.9000", "_clob_depth": 10.0}
        # ROI = 11.1%, depth = 10 (not capped) -> score = 0.111 * 10 = ~1.11
        score = capital_efficiency_score(opp)
        assert score == pytest.approx(0.10 / 0.90 * 10, rel=1e-3)

    def test_depth_capped_at_50(self):
        opp_a = {"net_profit": 0.10, "total_cost": "$0.9000", "_clob_depth": 50.0}
        opp_b = {"net_profit": 0.10, "total_cost": "$0.9000", "_clob_depth": 500.0}
        # Both should score the same since depth caps at 50
        assert capital_efficiency_score(opp_a) == pytest.approx(capital_efficiency_score(opp_b))

    def test_zero_cost_returns_zero(self):
        opp = {"net_profit": 0.10, "total_cost": "$0", "_clob_depth": 100.0}
        assert capital_efficiency_score(opp) == 0.0

    def test_zero_profit_returns_zero(self):
        opp = {"net_profit": 0.0, "total_cost": "$0.9500", "_clob_depth": 100.0}
        assert capital_efficiency_score(opp) == 0.0

    def test_negative_profit_returns_zero(self):
        opp = {"net_profit": -0.01, "total_cost": "$0.9500", "_clob_depth": 100.0}
        assert capital_efficiency_score(opp) == 0.0

    def test_numeric_total_cost(self):
        opp = {"net_profit": 0.05, "total_cost": 0.95, "_clob_depth": 30.0}
        score = capital_efficiency_score(opp)
        assert score == pytest.approx(0.05 / 0.95 * 30, rel=1e-3)

    def test_zero_depth_uses_one(self):
        """Zero depth doesn't zero out the score — ranks by ROI alone."""
        opp = {"net_profit": 0.10, "total_cost": "$0.9000", "_clob_depth": 0}
        score = capital_efficiency_score(opp)
        assert score == pytest.approx(0.10 / 0.90 * 1, rel=1e-3)

    def test_missing_depth_uses_one(self):
        opp = {"net_profit": 0.10, "total_cost": "$0.9000"}
        score = capital_efficiency_score(opp)
        assert score == pytest.approx(0.10 / 0.90 * 1, rel=1e-3)

    def test_ordering_high_roi_deep_beats_low_roi(self):
        """High ROI + deep book should score higher than low ROI + deep book."""
        high_roi = {"net_profit": 0.10, "total_cost": "$0.9000", "_clob_depth": 50.0}
        low_roi = {"net_profit": 0.01, "total_cost": "$0.9900", "_clob_depth": 50.0}
        assert capital_efficiency_score(high_roi) > capital_efficiency_score(low_roi)

    def test_invalid_total_cost_returns_zero(self):
        opp = {"net_profit": 0.10, "total_cost": "invalid", "_clob_depth": 50.0}
        assert capital_efficiency_score(opp) == 0.0


class TestWithinResolutionWindow:
    """Tests for _within_resolution_window() market date filtering."""

    def _future_iso(self, days: int) -> str:
        """Return an ISO 8601 datetime string `days` from now."""
        dt = datetime.now(timezone.utc) + timedelta(days=days)
        return dt.isoformat()

    def test_polymarket_within_window(self):
        market = {"endDateIso": self._future_iso(3)}
        assert _within_resolution_window(market, max_days=7, platform="polymarket") is True

    def test_polymarket_outside_window(self):
        market = {"endDateIso": self._future_iso(30)}
        assert _within_resolution_window(market, max_days=7, platform="polymarket") is False

    def test_kalshi_close_time_within_window(self):
        market = {"close_time": self._future_iso(2)}
        assert _within_resolution_window(market, max_days=7, platform="kalshi") is True

    def test_kalshi_close_time_outside_window(self):
        market = {"close_time": self._future_iso(60)}
        assert _within_resolution_window(market, max_days=7, platform="kalshi") is False

    def test_kalshi_fallback_to_expected_expiration(self):
        market = {"expected_expiration_time": self._future_iso(5)}
        assert _within_resolution_window(market, max_days=7, platform="kalshi") is True

    def test_kalshi_close_time_preferred_over_expiration(self):
        # close_time is within window, expected_expiration is far out — should use close_time
        market = {
            "close_time": self._future_iso(3),
            "expected_expiration_time": self._future_iso(90),
        }
        assert _within_resolution_window(market, max_days=7, platform="kalshi") is True

    def test_no_date_returns_false(self):
        assert _within_resolution_window({}, max_days=7, platform="polymarket") is False
        assert _within_resolution_window({}, max_days=7, platform="kalshi") is False

    def test_unparseable_date_returns_false(self):
        market = {"endDateIso": "not-a-date"}
        assert _within_resolution_window(market, max_days=7, platform="polymarket") is False

    def test_z_suffix_parsed(self):
        dt = datetime.now(timezone.utc) + timedelta(days=2)
        market = {"endDateIso": dt.strftime("%Y-%m-%dT%H:%M:%SZ")}
        assert _within_resolution_window(market, max_days=7, platform="polymarket") is True

    def test_zero_max_days_disables_filter(self):
        market = {"endDateIso": self._future_iso(365)}
        assert _within_resolution_window(market, max_days=0, platform="polymarket") is True

    def test_negative_max_days_disables_filter(self):
        market = {"endDateIso": self._future_iso(365)}
        assert _within_resolution_window(market, max_days=-1, platform="polymarket") is True

    def test_exactly_on_cutoff(self):
        # Market resolving at exactly 7 days should be included (<=)
        market = {"endDateIso": self._future_iso(7)}
        assert _within_resolution_window(market, max_days=7, platform="polymarket") is True

    def test_uses_config_default(self):
        """When max_days is None, uses MAX_RESOLUTION_DAYS from config."""
        market = {"endDateIso": self._future_iso(5)}
        with patch("scans.helpers.MAX_RESOLUTION_DAYS", 7):
            assert _within_resolution_window(market, platform="polymarket") is True
        with patch("scans.helpers.MAX_RESOLUTION_DAYS", 3):
            assert _within_resolution_window(market, platform="polymarket") is False

    def test_naive_datetime_treated_as_utc(self):
        dt = datetime.now(timezone.utc) + timedelta(days=2)
        naive_str = dt.strftime("%Y-%m-%dT%H:%M:%S")  # no tz info
        market = {"endDateIso": naive_str}
        assert _within_resolution_window(market, max_days=7, platform="polymarket") is True

    def test_already_resolved_returns_false(self):
        """Markets that resolved in the past should be rejected."""
        market = {"endDateIso": (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()}
        assert _within_resolution_window(market, max_days=7, platform="polymarket") is False

    def test_already_resolved_kalshi_returns_false(self):
        """Kalshi markets that already closed should be rejected."""
        market = {"close_time": (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()}
        assert _within_resolution_window(market, max_days=7, platform="kalshi") is False

    def test_resolves_today_returns_true(self):
        """A market resolving in 1 hour (today) should be included."""
        market = {"endDateIso": (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()}
        assert _within_resolution_window(market, max_days=7, platform="polymarket") is True


class TestFilterDust:
    def test_removes_below_threshold(self):
        opps = [
            {"net_profit": 0.10, "market": "A"},
            {"net_profit": 0.03, "market": "B"},
            {"net_profit": 0.06, "market": "C"},
        ]
        result = filter_dust(opps, min_amount=0.05)
        assert len(result) == 2
        assert all(o["net_profit"] >= 0.05 for o in result)

    def test_keeps_all_above_threshold(self):
        opps = [
            {"net_profit": 0.10, "market": "A"},
            {"net_profit": 0.20, "market": "B"},
        ]
        result = filter_dust(opps, min_amount=0.05)
        assert len(result) == 2

    def test_empty_list_returns_empty(self):
        assert filter_dust([], min_amount=0.05) == []

    def test_default_threshold_from_config(self):
        """Uses MIN_PROFIT_AMOUNT from config when no min_amount given."""
        opps = [{"net_profit": 0.01}, {"net_profit": 0.10}]
        with patch("scans.helpers.MIN_PROFIT_AMOUNT", 0.05):
            result = filter_dust(opps)
        assert len(result) == 1


class TestDaysToResolution:
    def _future_iso(self, days: float) -> str:
        dt = datetime.now(timezone.utc) + timedelta(days=days)
        return dt.isoformat()

    def test_polymarket_date(self):
        market = {"endDateIso": self._future_iso(3)}
        days = _days_to_resolution(market, "polymarket")
        assert days is not None
        assert 2.9 < days < 3.1

    def test_kalshi_date(self):
        market = {"close_time": self._future_iso(5)}
        days = _days_to_resolution(market, "kalshi")
        assert days is not None
        assert 4.9 < days < 5.1

    def test_no_date_returns_none(self):
        assert _days_to_resolution({}, "polymarket") is None

    def test_past_date_floors_at_001(self):
        market = {"endDateIso": self._future_iso(-1)}
        days = _days_to_resolution(market, "polymarket")
        assert days == pytest.approx(0.01)


class TestTimeWeightedScoring:
    def test_short_duration_scores_higher(self):
        """Same ROI/depth, but 2-day market should score higher than 7-day."""
        short = {"net_profit": 0.10, "total_cost": "$0.9000", "_clob_depth": 50.0, "_days_to_resolution": 2.0}
        long = {"net_profit": 0.10, "total_cost": "$0.9000", "_clob_depth": 50.0, "_days_to_resolution": 7.0}
        assert capital_efficiency_score(short) > capital_efficiency_score(long)

    def test_no_resolution_uses_base_score(self):
        """Without _days_to_resolution, uses the original base score."""
        opp = {"net_profit": 0.10, "total_cost": "$0.9000", "_clob_depth": 50.0}
        score = capital_efficiency_score(opp)
        expected = (0.10 / 0.90) * 50
        assert score == pytest.approx(expected, rel=1e-3)

    def test_resolution_none_uses_base_score(self):
        opp = {"net_profit": 0.10, "total_cost": "$0.9000", "_clob_depth": 50.0, "_days_to_resolution": None}
        score = capital_efficiency_score(opp)
        expected = (0.10 / 0.90) * 50
        assert score == pytest.approx(expected, rel=1e-3)

    def test_one_day_resolution_matches_base(self):
        """1-day resolution divides by 1, so score equals base score."""
        opp = {"net_profit": 0.10, "total_cost": "$0.9000", "_clob_depth": 50.0, "_days_to_resolution": 1.0}
        score = capital_efficiency_score(opp)
        expected = (0.10 / 0.90) * 50 / 1.0
        assert score == pytest.approx(expected, rel=1e-3)
