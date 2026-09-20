"""Tests for detection funnel telemetry tracker and dashboard integration."""

from __future__ import annotations

import sys
from io import BytesIO
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from funnel import FunnelStats, ScanFunnelTracker, get_funnel_tracker, reset_funnel_tracker
from dashboard import state as dashboard_state, _Handler


# ---------------------------------------------------------------------------
# TestFunnelStats
# ---------------------------------------------------------------------------

class TestFunnelStats:
    """Test FunnelStats data structure and serialization."""

    def test_default_values(self):
        stats = FunnelStats()
        assert stats.screened == 0
        assert stats.mid_candidates == 0
        assert stats.clob_evaluated == 0
        assert stats.clob_dropped == 0
        assert stats.fee_dropped == 0
        assert stats.roi_dropped == 0
        assert stats.depth_dropped == 0
        assert stats.surfaced == 0

    def test_to_dict(self):
        stats = FunnelStats(
            screened=100,
            mid_candidates=10,
            clob_evaluated=10,
            clob_dropped=5,
            fee_dropped=3,
            roi_dropped=1,
            depth_dropped=1,
            surfaced=0,
        )
        d = stats.to_dict()
        assert d["screened"] == 100
        assert d["mid_candidates"] == 10
        assert d["clob_evaluated"] == 10
        assert d["clob_dropped"] == 5
        assert d["fee_dropped"] == 3
        assert d["roi_dropped"] == 1
        assert d["depth_dropped"] == 1
        assert d["surfaced"] == 0


# ---------------------------------------------------------------------------
# TestScanFunnelTracker
# ---------------------------------------------------------------------------

class TestScanFunnelTracker:
    """Test ScanFunnelTracker life cycle and metrics accumulation."""

    @pytest.fixture(autouse=True)
    def clean_tracker(self):
        reset_funnel_tracker()
        yield
        reset_funnel_tracker()

    def test_record_metrics_and_summary_log(self):
        tracker = ScanFunnelTracker()
        tracker.start_cycle()

        tracker.record_screened(500)
        tracker.record_mid_candidates(20)
        tracker.record_clob_evaluated(20)
        tracker.record_clob_dropped(15)
        tracker.record_fee_dropped(3)
        tracker.record_roi_dropped(1)
        tracker.record_depth_dropped(1)
        tracker.record_surfaced(0)

        log_str = tracker.summary_log(1)
        assert "Funnel #1:" in log_str
        assert "screened=500" in log_str
        assert "mid_cand=20" in log_str
        assert "clob_drop=15" in log_str
        assert "fee_drop=3" in log_str
        assert "roi_drop=1" in log_str
        assert "depth_drop=1" in log_str
        assert "surfaced=0" in log_str

    def test_cycle_reset_preserves_cumulative(self):
        tracker = ScanFunnelTracker()
        tracker.record_screened(100)
        tracker.record_mid_candidates(5)
        cycle1 = tracker.finish_cycle()
        assert cycle1["screened"] == 100
        assert cycle1["mid_candidates"] == 5

        tracker.start_cycle()
        assert tracker.current_cycle.screened == 0
        assert tracker.cumulative.screened == 100

        tracker.record_screened(200)
        cycle2 = tracker.finish_cycle()
        assert cycle2["screened"] == 200
        assert tracker.cumulative.screened == 300
        assert len(tracker.cycle_history) == 2

    def test_history_cap(self):
        tracker = ScanFunnelTracker()
        for i in range(105):
            tracker.start_cycle()
            tracker.record_screened(i)
            tracker.finish_cycle()

        assert len(tracker.cycle_history) == 100


# ---------------------------------------------------------------------------
# TestDashboardFunnelIntegration
# ---------------------------------------------------------------------------

class TestDashboardFunnelIntegration:
    """Test dashboard state and endpoint exposure for funnel metrics."""

    @pytest.fixture(autouse=True)
    def clean_tracker(self):
        reset_funnel_tracker()
        yield
        reset_funnel_tracker()

    def test_dashboard_state_to_dict_includes_funnel_stats(self):
        dashboard_state.funnel_stats = {"screened": 120, "surfaced": 2}
        d = dashboard_state.to_dict()
        assert "funnel_stats" in d
        assert d["funnel_stats"]["screened"] == 120
        assert d["funnel_stats"]["surfaced"] == 2

    def test_handle_funnel_endpoint(self):
        tracker = get_funnel_tracker()
        tracker.start_cycle()
        tracker.record_screened(50)
        tracker.record_surfaced(1)
        tracker.finish_cycle()

        # Mock HTTP request handling for /api/funnel
        handler = MagicMock(spec=_Handler)
        handler.headers = {}
        handler.path = "/api/funnel"
        handler.wfile = BytesIO()

        # Call real unbound method
        with patch("dashboard._send_json") as mock_send_json:
            _Handler._handle_funnel(handler)
            mock_send_json.assert_called_once()
            args = mock_send_json.call_args[0]
            body = args[1]
            assert "current_cycle" in body
            assert "cumulative" in body
            assert "history" in body
            assert body["cumulative"]["screened"] == 50
            assert body["cumulative"]["surfaced"] == 1


# ---------------------------------------------------------------------------
# TestScansFunnelTracking
# ---------------------------------------------------------------------------

class TestScansFunnelTracking:
    """Test that scan_binary_internal updates funnel tracker."""

    @pytest.fixture(autouse=True)
    def clean_tracker(self):
        reset_funnel_tracker()
        yield
        reset_funnel_tracker()

    def test_scan_binary_records_screened_and_mid_candidates(self):
        from scans.binary import scan_binary_internal
        tracker = get_funnel_tracker()
        tracker.start_cycle()

        market = {
            "question": "Test Market",
            "conditionId": "0x123",
            "outcomes": '["Yes", "No"]',
            "outcomePrices": '["0.40", "0.45"]',
            "volume": "1000",
            "category": "crypto",
            "clobTokenIds": '["tok1", "tok2"]',
        }

        with patch("scans.binary.get_binary_markets", return_value=[market]), \
             patch("scans.binary.parse_outcome_prices", return_value=[0.40, 0.45]), \
             patch("scans.binary._within_resolution_window", return_value=True), \
             patch("scans.binary._refine_binary_with_clob", return_value=[]):
            scan_binary_internal([market], min_profit=0.01, funnel=tracker)

        assert tracker.current_cycle.screened == 1
        assert tracker.current_cycle.mid_candidates == 1
        assert tracker.current_cycle.clob_evaluated == 1
