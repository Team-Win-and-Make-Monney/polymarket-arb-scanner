"""Tests for anomaly detection in alerting.py (MONITOR-03).

Tests:
  - AlertType enum has LOSS_SPIKE and ZERO_OPP_PERIOD members
  - check_loss_spike: guard against false positives with < 10 trades
  - check_loss_spike: fires CRITICAL when loss > 3x rolling average (10+ trades)
  - check_loss_spike: does NOT fire when loss < 3x rolling average
  - check_zero_opp_period: fires WARNING after 5+ consecutive empty scans
  - check_zero_opp_period: resets counter when opportunities are found
"""

import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest
from alerting import AlertManager, AlertType, Severity


# ---------------------------------------------------------------------------
# TestAlertTypeEnumMembers
# ---------------------------------------------------------------------------

class TestAlertTypeEnumMembers:
    def test_loss_spike_enum_member_exists(self):
        assert AlertType.LOSS_SPIKE == "LOSS_SPIKE"

    def test_zero_opp_period_enum_member_exists(self):
        assert AlertType.ZERO_OPP_PERIOD == "ZERO_OPP_PERIOD"

    def test_existing_members_still_present(self):
        """Ensure new members didn't displace existing ones."""
        assert AlertType.EXECUTION_FAILURE == "EXECUTION_FAILURE"
        assert AlertType.LOSS_STREAK == "LOSS_STREAK"
        assert AlertType.DAILY_LOSS_LIMIT == "DAILY_LOSS_LIMIT"
        assert AlertType.WS_DISCONNECT == "WS_DISCONNECT"


# ---------------------------------------------------------------------------
# TestLossSpike
# ---------------------------------------------------------------------------

class TestLossSpike:
    def _make_am(self) -> AlertManager:
        """AlertManager with no rate limiting for deterministic tests."""
        return AlertManager(rate_limit_seconds=0)

    def test_returns_false_when_fewer_than_10_trades(self):
        """Guard clause: fewer than 10 trade losses → never fire."""
        am = self._make_am()
        # Add 9 losses to the rolling window
        for _ in range(9):
            am.record_trade_result(-1.0)
        # Even a huge loss should not fire yet
        result = am.check_loss_spike(100.0)
        assert result is False

    def test_returns_false_with_zero_trades(self):
        """No history at all → never fire."""
        am = self._make_am()
        result = am.check_loss_spike(999.0)
        assert result is False

    def test_fires_critical_when_loss_exceeds_3x_average(self):
        """With 10+ losses in window, a loss > 3x avg fires CRITICAL."""
        am = self._make_am()
        # Populate window with 10 moderate losses (avg = 1.0)
        for _ in range(10):
            am.record_trade_result(-1.0)
        # Loss of 4.0 is 4x the average of 1.0 → should fire
        result = am.check_loss_spike(4.0)
        assert result is True
        recent = am.get_recent_alerts()
        spike_alerts = [a for a in recent if a["type"] == "LOSS_SPIKE"]
        assert len(spike_alerts) >= 1
        assert spike_alerts[0]["severity"] == "CRITICAL"

    def test_fires_exactly_at_3x_boundary(self):
        """Loss exactly equal to 3x avg should NOT fire (must be strictly greater)."""
        am = self._make_am()
        for _ in range(10):
            am.record_trade_result(-2.0)  # avg = 2.0
        # 3x = 6.0 exactly — implementation uses >, so this should NOT fire
        result = am.check_loss_spike(6.0)
        # At exactly 3x it does NOT fire (strictly greater than 3x)
        # Note: depends on implementation — loss_amount > 3 * avg is strict
        assert result is False

    def test_does_not_fire_when_loss_below_3x_average(self):
        """Loss < 3x avg must NOT fire."""
        am = self._make_am()
        for _ in range(10):
            am.record_trade_result(-2.0)  # avg = 2.0
        result = am.check_loss_spike(5.9)  # < 6.0 (3x avg)
        assert result is False

    def test_context_includes_loss_and_avg(self):
        """Alert context dict must include 'loss', 'avg', and 'ratio' keys."""
        am = self._make_am()
        for _ in range(10):
            am.record_trade_result(-1.0)
        am.check_loss_spike(5.0)
        recent = am.get_recent_alerts()
        spike_alerts = [a for a in recent if a["type"] == "LOSS_SPIKE"]
        assert spike_alerts, "LOSS_SPIKE alert not found"
        ctx = spike_alerts[0]["details"]
        assert "loss" in ctx
        assert "avg" in ctx
        assert "ratio" in ctx
        assert ctx["ratio"] == pytest.approx(5.0)

    def test_record_trade_result_ignores_profitable_trades(self):
        """Positive profit (winning trade) should not be stored in loss window."""
        am = self._make_am()
        # Record 10 wins — window should still be empty for losses
        for _ in range(10):
            am.record_trade_result(5.0)  # positive = win
        # Window is empty, so guard clause triggers
        result = am.check_loss_spike(100.0)
        assert result is False

    def test_deque_maxlen_limits_window(self):
        """_trade_losses deque should have maxlen=20, so very old losses drop off."""
        am = self._make_am()
        # Fill 20 slots with large losses to set high avg
        for _ in range(20):
            am.record_trade_result(-100.0)
        # Now add enough smaller losses to push old ones out
        # After 20 more records, all 20 slots have -1.0
        for _ in range(20):
            am.record_trade_result(-1.0)
        # avg should now be ~1.0 (old 100s pushed out)
        # A loss of 10 is 10x avg of 1 — should fire
        result = am.check_loss_spike(10.0)
        assert result is True


# ---------------------------------------------------------------------------
# TestZeroOppPeriod
# ---------------------------------------------------------------------------

class TestZeroOppPeriod:
    def _make_am(self) -> AlertManager:
        return AlertManager(rate_limit_seconds=0)

    def test_does_not_fire_below_5_consecutive_empty(self):
        """4 consecutive zero-opp scans should not fire."""
        am = self._make_am()
        results = [am.check_zero_opp_period(0) for _ in range(4)]
        assert all(result is False for result in results)

    def test_fires_warning_at_5_consecutive_empty(self):
        """5 consecutive zero-opp scans should fire WARNING."""
        am = self._make_am()
        for _ in range(4):
            am.check_zero_opp_period(0)
        result = am.check_zero_opp_period(0)  # 5th empty scan
        assert result is True
        recent = am.get_recent_alerts()
        zero_alerts = [a for a in recent if a["type"] == "ZERO_OPP_PERIOD"]
        assert len(zero_alerts) >= 1
        assert zero_alerts[0]["severity"] == "WARNING"

    def test_fires_warning_beyond_5_consecutive(self):
        """More than 5 consecutive empty scans should still trigger alerts."""
        am = self._make_am()
        results = []
        for _ in range(7):
            results.append(am.check_zero_opp_period(0))
        # At least the 5th, 6th, 7th should attempt to alert
        # Rate limit is 0 so they all fire
        fired = [r for r in results if r is True]
        assert len(fired) >= 1

    def test_resets_counter_when_opps_found(self):
        """Finding opportunities resets the zero-opp counter."""
        am = self._make_am()
        # Build up 4 empty scans
        for _ in range(4):
            am.check_zero_opp_period(0)
        # A scan with opportunities found resets counter
        reset_result = am.check_zero_opp_period(3)
        assert reset_result is False

        # Now the next 4 consecutive empties should NOT trigger (counter reset)
        for _ in range(4):
            result = am.check_zero_opp_period(0)
        assert result is False

    def test_resets_counter_and_allows_new_alert_cycle(self):
        """After a reset, 5 new consecutive empties should fire again."""
        am = self._make_am()
        # Fire the first alert
        for _ in range(5):
            am.check_zero_opp_period(0)
        # Reset
        am.check_zero_opp_period(10)
        # 5 more empties
        result = None
        for _ in range(5):
            result = am.check_zero_opp_period(0)
        assert result is True

    def test_context_includes_consecutive_count(self):
        """Alert context should include consecutive_empty_scans."""
        am = self._make_am()
        for _ in range(5):
            am.check_zero_opp_period(0)
        recent = am.get_recent_alerts()
        zero_alerts = [a for a in recent if a["type"] == "ZERO_OPP_PERIOD"]
        assert zero_alerts, "ZERO_OPP_PERIOD alert not found"
        ctx = zero_alerts[0]["details"]
        assert "consecutive_empty_scans" in ctx
        assert ctx["consecutive_empty_scans"] >= 5
