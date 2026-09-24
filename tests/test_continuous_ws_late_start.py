"""Kalshi WS feed must start when scan #1 had nothing to subscribe (mm-pilot)."""

from __future__ import annotations

import asyncio
import os
import sys
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# Mock external SDK-dependent modules before importing continuous
_modules_to_mock = ["kalshi_api", "polymarket_api", "display", "recovery"]
_saved_modules = {name: sys.modules[name] for name in _modules_to_mock if name in sys.modules}
for _mod_name in _modules_to_mock:
    sys.modules[_mod_name] = MagicMock()

import continuous

# Restore saved modules for any sibling test files
for _mod_name in _modules_to_mock:
    if _mod_name in _saved_modules:
        sys.modules[_mod_name] = _saved_modules[_mod_name]
    elif _mod_name in sys.modules:
        del sys.modules[_mod_name]


def _task(done=True, cancelled=False, exc=None):
    task = MagicMock()
    task.done.return_value = done
    task.cancelled.return_value = cancelled
    task.exception.return_value = exc
    return task


class TestStartKalshiWsAfterEmptyRun:
    def test_clean_empty_run_starts_kalshi_with_pilot_tickers(self):
        fm = MagicMock()
        fm.start_kalshi_feed_late.return_value = True
        assert continuous._start_kalshi_ws_after_empty_run(fm, _task(), ["KXPILOT-1"]) is True
        fm.update_subscriptions.assert_called_once_with(kalshi_tickers=["KXPILOT-1"])
        fm.start_kalshi_feed_late.assert_called_once_with()

    def test_running_task_is_left_to_the_normal_update_path(self):
        fm = MagicMock()
        assert continuous._start_kalshi_ws_after_empty_run(fm, _task(done=False), ["KXPILOT-1"]) is False
        fm.update_subscriptions.assert_not_called()
        fm.start_kalshi_feed_late.assert_not_called()

    def test_crashed_or_cancelled_task_keeps_existing_behaviour(self):
        for task in (_task(exc=RuntimeError("boom")), _task(cancelled=True)):
            fm = MagicMock()
            assert continuous._start_kalshi_ws_after_empty_run(fm, task, ["KXPILOT-1"]) is False
            fm.update_subscriptions.assert_not_called()
            fm.start_kalshi_feed_late.assert_not_called()

    def test_nothing_to_subscribe_yet(self):
        fm = MagicMock()
        assert continuous._start_kalshi_ws_after_empty_run(fm, _task(), []) is False
        assert continuous._start_kalshi_ws_after_empty_run(fm, None, ["KXPILOT-1"]) is False
        fm.update_subscriptions.assert_not_called()

    def test_real_feed_manager_starts_once_then_queues_reselections(self):
        import ws_feeds

        fm = ws_feeds.FeedManager(on_price_update=lambda *a, **kw: None)
        fm.kalshi_api_key_id = "kid"
        fm.kalshi_private_key = object()

        async def scenario():
            ws_task = asyncio.create_task(fm.run())
            await ws_task  # no subscriptions yet: run() returns immediately
            with patch.object(ws_feeds.asyncio, "create_task") as create_task:
                started = continuous._start_kalshi_ws_after_empty_run(fm, ws_task, ["KXPILOT-1"])
                again = continuous._start_kalshi_ws_after_empty_run(fm, ws_task, ["KXPILOT-1", "KXPILOT-2"])
            create_task.call_args[0][0].close()  # avoid un-awaited coroutine warning
            return started, again, create_task.call_count

        started, again, creates = asyncio.run(scenario())
        assert started is True
        assert again is False  # already running: no second feed task
        assert creates == 1
        assert fm._kalshi_tickers == ["KXPILOT-1", "KXPILOT-2"]
        assert fm._pending_kalshi_subs == ["KXPILOT-2"]  # re-selection queued for the live socket
