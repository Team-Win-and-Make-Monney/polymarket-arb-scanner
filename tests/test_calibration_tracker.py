"""Concurrency regression tests for calibration cache invalidation."""

import threading

import calibration_tracker
from calibration_tracker import CalibrationTracker


class TestCalibrationCacheConcurrency:
    def test_cache_uses_internal_keys(self, tmp_path, monkeypatch):
        monkeypatch.setattr(calibration_tracker, "CALIBRATION_WEIGHTING_ENABLED", True)
        tracker = CalibrationTracker(tmp_path / "calibration.db")
        monkeypatch.setattr(tracker, "get_platform_brier_score", lambda *_args: 0.10)

        assert tracker.get_weight_multiplier("kalshi") == 1.5
        assert set(tracker._in_memory_cache["kalshi:all"]) == {
            "_weight", "_brier", "_expires",
        }
        assert tracker.get_weight_multiplier("kalshi") == 1.5

    def test_inflight_weight_calculation_cannot_restore_invalidated_cache(
        self, tmp_path, monkeypatch,
    ):
        monkeypatch.setattr(calibration_tracker, "CALIBRATION_WEIGHTING_ENABLED", True)
        tracker = CalibrationTracker(tmp_path / "calibration.db")
        calculation_started = threading.Event()
        release_calculation = threading.Event()

        def delayed_brier(_platform, _category=None, _lookback_days=365):
            calculation_started.set()
            release_calculation.wait(timeout=2)
            return 0.10

        monkeypatch.setattr(tracker, "get_platform_brier_score", delayed_brier)
        thread = threading.Thread(
            target=tracker.get_weight_multiplier,
            args=("kalshi",),
        )
        thread.start()
        assert calculation_started.wait(timeout=1)

        tracker.record_resolution("kalshi", "market-1", 0.5, 1)
        release_calculation.set()
        thread.join(timeout=2)

        assert not thread.is_alive()
        assert tracker._in_memory_cache == {}
