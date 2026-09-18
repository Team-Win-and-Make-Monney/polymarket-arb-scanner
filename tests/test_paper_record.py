"""Paper-record tracker: daily digest + window-completion alert (2026-07-21).

The 7-day paper-trading window had no tracker, no end trigger, and no
notification — it existed only as a convention. This module makes the window
explicit: a daily webhook digest of what the dry-run pipeline detected, and a
one-time completion alert when the window closes.
"""
import sys
import os
import tempfile
import time
from unittest.mock import MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from db import TradeDB
from paper_record import PaperRecordTracker

DAY = 86400.0
START = time.time() - 0.5 * 86400  # window opened half a day ago;
# rows logged "now" fall inside it, and boundary times stay relative to START


import pytest


@pytest.fixture
def paper_db(tmp_path):
    db = TradeDB(str(tmp_path / "paper.db"))
    db.log_opportunity("KalshiMulti(4)", "Fed Combo", "0.6,0.29", 0.91, 0.04, 0.033, 151.0, "skipped:gas_threshold")
    db.log_opportunity("KalshiMulti(4)", "Fed Combo", "0.6,0.29", 0.91, 0.04, 0.033, 151.0, "dry_run")
    db.log_opportunity("CrossPlatform", "X vs Y", "0.5,0.4", 0.90, 0.06, 0.066, 80.0, "dry_run")
    yield db
    db.conn.close()


def _notifier():
    notifier = MagicMock()
    notifier.notify_text.return_value = True
    return notifier


class TestDigest:
    def test_daily_digest_sent_once_per_day_boundary(self, paper_db):
        notifier = _notifier()
        tracker = PaperRecordTracker(paper_db, notifier, window_start=START, window_days=7)
        tracker.on_day_boundary(now=START + 1 * DAY)
        assert notifier.notify_text.call_count == 1
        # Same boundary again: no duplicate.
        tracker.on_day_boundary(now=START + 1 * DAY + 60)
        assert notifier.notify_text.call_count == 1
        tracker.on_day_boundary(now=START + 2 * DAY)
        assert notifier.notify_text.call_count == 2

    def test_digest_summarises_types_actions_and_day_count(self, paper_db):
        notifier = _notifier()
        tracker = PaperRecordTracker(paper_db, notifier, window_start=START, window_days=7)
        tracker.on_day_boundary(now=START + 2 * DAY)
        msg = notifier.notify_text.call_args[0][0]
        assert "day 2/7" in msg.lower()
        assert "KalshiMulti(4)" in msg
        assert "CrossPlatform" in msg
        assert "skipped:gas_threshold" in msg
        assert "dry_run" in msg

    def test_notifier_failure_does_not_raise(self, paper_db):
        notifier = MagicMock()
        notifier.notify_text.side_effect = RuntimeError("webhook down")
        tracker = PaperRecordTracker(paper_db, notifier, window_start=START, window_days=7)
        tracker.on_day_boundary(now=START + 1 * DAY)  # must not raise


class TestCompletion:
    def test_completion_alert_fires_once_at_window_end(self, paper_db):
        notifier = _notifier()
        tracker = PaperRecordTracker(paper_db, notifier, window_start=START, window_days=7)
        tracker.on_day_boundary(now=START + 7 * DAY)
        msgs = [c[0][0] for c in notifier.notify_text.call_args_list]
        assert any("complete" in m.lower() for m in msgs)
        # Later boundaries: digest may continue but no second completion.
        tracker.on_day_boundary(now=START + 8 * DAY)
        completions = [m for c in notifier.notify_text.call_args_list for m in [c[0][0]] if "complete" in m.lower()]
        assert len(completions) == 1

    def test_no_completion_before_window_end(self, paper_db):
        notifier = _notifier()
        tracker = PaperRecordTracker(paper_db, notifier, window_start=START, window_days=7)
        tracker.on_day_boundary(now=START + 6 * DAY)
        msgs = [c[0][0] for c in notifier.notify_text.call_args_list]
        assert not any("complete" in m.lower() for m in msgs)


class TestReviewHardening:
    def test_null_roi_rows_do_not_break_digest(self, paper_db):
        db = paper_db
        with db._lock:
            db.conn.execute(
                "INSERT INTO opportunities (timestamp, type, market, prices, total_cost,"
                " net_profit, net_roi, depth, action) VALUES (?,?,?,?,?,?,NULL,?,?)",
                ("2099-01-01T00:00:00+00:00", "LegacyType", "old", "", 0.5, 0.01, 10.0, "dry_run"))
            db.conn.commit()
        # Make the NULL row visible: window covers everything.
        notifier = _notifier()
        tracker = PaperRecordTracker(db, notifier, window_start=START, window_days=7)
        tracker.on_day_boundary(now=START + 1 * DAY)
        assert notifier.notify_text.call_count == 1

    def test_failed_completion_send_retries_next_boundary(self, paper_db):
        notifier = MagicMock()
        notifier.notify_text.side_effect = [True, RuntimeError("down"), True, True]
        tracker = PaperRecordTracker(paper_db, notifier, window_start=START, window_days=7)
        tracker.on_day_boundary(now=START + 7 * DAY)   # digest ok, completion fails
        tracker.on_day_boundary(now=START + 8 * DAY)   # digest ok, completion retried
        completions = [c[0][0] for c in notifier.notify_text.call_args_list
                       if "complete" in c[0][0].lower()]
        assert len(completions) == 2  # one failed attempt + one delivered
        # Delivered now; day 9 must not send a third.
        tracker.on_day_boundary(now=START + 9 * DAY)
        completions = [c[0][0] for c in notifier.notify_text.call_args_list
                       if "complete" in c[0][0].lower()]
        assert len(completions) == 2

    def test_window_days_clamped_to_minimum_one(self, paper_db):
        tracker = PaperRecordTracker(paper_db, _notifier(), window_start=START, window_days=0)
        assert tracker.window_days == 1


class TestDisabled:
    def test_zero_window_start_disables_tracker(self, paper_db):
        notifier = _notifier()
        tracker = PaperRecordTracker(paper_db, notifier, window_start=0.0, window_days=7)
        tracker.on_day_boundary(now=START + 1 * DAY)
        notifier.notify_text.assert_not_called()

    def test_none_notifier_is_safe(self, paper_db):
        tracker = PaperRecordTracker(paper_db, None, window_start=START, window_days=7)
        tracker.on_day_boundary(now=START + 1 * DAY)  # must not raise
