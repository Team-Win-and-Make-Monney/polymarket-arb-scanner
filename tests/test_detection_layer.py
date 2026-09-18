"""Tests for the detection layer: FeedHealthTracker wiring, degradation
alerts, Sentry Crons heartbeat, and /status platform health exposure.

Closes the meta-gap that let the 2026-07-23 Kalshi degradation run 31h
unnoticed: FeedHealthTracker existed with zero callers, /healthz only
proved process liveness, and no heartbeat paged on a stalled loop.
"""

import sys
import os
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest
from unittest.mock import MagicMock, patch


class TestFeedHealthTrackerAlertTransitions:
    def _tracker(self, threshold=0.05):
        from ws_feeds import FeedHealthTracker
        return FeedHealthTracker(stale_threshold_seconds=threshold)

    def test_outage_fires_unhealthy_callback_once(self):
        tracker = self._tracker()
        events = []
        tracker.register_health_callback(lambda p, ok: events.append((p, ok)))
        tracker.record_message("kalshi")
        time.sleep(0.08)
        tracker.check_outages()
        tracker.check_outages()  # second check must not re-fire
        assert events == [("kalshi", False)]

    def test_recovery_fires_healthy_callback(self):
        tracker = self._tracker()
        events = []
        tracker.register_health_callback(lambda p, ok: events.append((p, ok)))
        tracker.record_message("kalshi")
        time.sleep(0.08)
        tracker.check_outages()
        tracker.record_message("kalshi")
        assert events == [("kalshi", False), ("kalshi", True)]

    def test_healthy_platform_fires_nothing(self):
        tracker = self._tracker(threshold=60.0)
        events = []
        tracker.register_health_callback(lambda p, ok: events.append((p, ok)))
        tracker.record_message("polymarket")
        result = tracker.check_outages()
        assert events == []
        assert result["polymarket"]["in_outage"] is False

    def test_callback_exception_does_not_break_tracker(self):
        tracker = self._tracker()

        def bad_callback(p, ok):
            raise RuntimeError("boom")

        tracker.register_health_callback(bad_callback)
        tracker.record_message("kalshi")
        time.sleep(0.08)
        tracker.check_outages()  # must not raise
        health = tracker.get_platform_health("kalshi")
        assert health["in_outage"] is True


class TestScanHeartbeat:
    def test_noop_without_dsn(self, monkeypatch):
        monkeypatch.delenv("SENTRY_DSN", raising=False)
        import sentry_init
        with patch("sentry_sdk.crons.capture_checkin") as checkin:
            sentry_init.capture_scan_heartbeat("ok")
        checkin.assert_not_called()

    def test_noop_under_pytest_even_with_dsn(self, monkeypatch):
        # PYTEST_CURRENT_TEST is set by pytest itself; DSN alone must not send.
        monkeypatch.setenv("SENTRY_DSN", "https://k@o.ingest.sentry.io/1")
        import sentry_init
        with patch("sentry_sdk.crons.capture_checkin") as checkin:
            sentry_init.capture_scan_heartbeat("ok")
        checkin.assert_not_called()

    def test_checkin_sent_with_dsn(self, monkeypatch):
        monkeypatch.setenv("SENTRY_DSN", "https://k@o.ingest.sentry.io/1")
        monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
        import sentry_init
        with patch("sentry_sdk.crons.capture_checkin") as checkin:
            sentry_init.capture_scan_heartbeat("error")
        checkin.assert_called_once()
        kwargs = checkin.call_args.kwargs
        assert kwargs["monitor_slug"] == "arbgrid-scan-loop"
        assert kwargs["status"] == "error"
        assert kwargs["monitor_config"]["schedule"]["type"] == "interval"

    def test_sdk_failure_swallowed(self, monkeypatch):
        monkeypatch.setenv("SENTRY_DSN", "https://k@o.ingest.sentry.io/1")
        monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
        import sentry_init
        with patch("sentry_sdk.crons.capture_checkin",
                   side_effect=RuntimeError("sentry down")):
            sentry_init.capture_scan_heartbeat("ok")  # must not raise


class TestDashboardPlatformHealth:
    def test_state_serializes_platform_health(self):
        import dashboard
        state = dashboard._DashboardState()
        assert state.platform_health == {}
        state.platform_health = {
            "feeds": {"kalshi": {"healthy": False, "last_message_ago_s": 400.0}},
            "clients": {"kalshi": True, "gemini": True},
        }
        payload = state.to_dict()
        assert payload["platform_health"]["feeds"]["kalshi"]["healthy"] is False
        assert payload["platform_health"]["clients"]["gemini"] is True
