"""Kalshi WS feed must start when scan #1 had nothing to subscribe (mm-pilot)."""

from __future__ import annotations

import asyncio
import importlib
import os
import sys
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# Import continuous against SDK mocks, then restore every sys.modules entry this
# touched, including continuous itself, so mocked bindings cannot leak into other
# test files through the module cache. The module object stays usable here.
_modules_to_mock = ["kalshi_api", "polymarket_api", "display", "recovery"]
_saved_modules = {name: sys.modules[name] for name in [*_modules_to_mock, "continuous"] if name in sys.modules}
for _mod_name in _modules_to_mock:
    sys.modules[_mod_name] = MagicMock()
sys.modules.pop("continuous", None)

continuous = importlib.import_module("continuous")

for _mod_name in [*_modules_to_mock, "continuous"]:
    if _mod_name in _saved_modules:
        sys.modules[_mod_name] = _saved_modules[_mod_name]
    else:
        sys.modules.pop(_mod_name, None)


def _task(done=True, cancelled=False, exc=None):
    task = MagicMock()
    task.done.return_value = done
    task.cancelled.return_value = cancelled
    task.exception.return_value = exc
    return task


def _feed_manager():
    import ws_feeds

    fm = ws_feeds.FeedManager(on_price_update=lambda *a, **kw: None)
    fm.kalshi_api_key_id = "kid"
    fm.kalshi_private_key = object()
    return fm, ws_feeds


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
        fm, ws_feeds = _feed_manager()

        async def scenario():
            ws_task = asyncio.ensure_future(fm.run())
            await asyncio.wait([ws_task])  # no subscriptions yet: run() returns immediately
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


class TestSyncWsFeedsScanLoop:
    """The scan loop's WS step, driven through two iterations as in --mode mm-pilot."""

    def test_empty_first_scan_then_pilot_ticker_starts_kalshi_feed(self):
        fm, ws_feeds = _feed_manager()
        kalshi_client = object()

        async def scenario():
            # Scan #1: pilot has not selected markets yet, so nothing to subscribe.
            ws_task = continuous._sync_ws_feeds(fm, None, 1, [], [], kalshi_client)
            await asyncio.wait([ws_task])
            # Scan #2: selection landed; the pilot ticker rides the Kalshi set.
            with patch.object(fm, "update_subscriptions", wraps=fm.update_subscriptions) as update, \
                 patch.object(fm, "start_kalshi_feed_late", wraps=fm.start_kalshi_feed_late) as start_late, \
                 patch.object(ws_feeds.asyncio, "create_task") as create_task:
                same_task = continuous._sync_ws_feeds(fm, ws_task, 2, [], ["KXPILOT-1"], kalshi_client)
            create_task.call_args[0][0].close()  # avoid un-awaited coroutine warning
            return ws_task, same_task, update, start_late, create_task

        ws_task, same_task, update, start_late, create_task = asyncio.run(scenario())
        assert same_task is ws_task
        update.assert_called_once_with(kalshi_tickers=["KXPILOT-1"])
        start_late.assert_called_once_with()
        assert create_task.call_count == 1
        assert fm._kalshi_task_started is True

    def test_first_scan_with_tickers_keeps_the_normal_start(self):
        fm = MagicMock()
        fm.run = MagicMock(return_value=None)
        with patch.object(continuous.asyncio, "create_task", return_value=_task(done=False)) as create_task:
            ws_task = continuous._sync_ws_feeds(fm, None, 1, ["tok"], ["KXA"], object())
        create_task.assert_called_once()
        fm.subscribe_polymarket.assert_called_once_with(["tok"])
        fm.subscribe_kalshi.assert_called_once_with(["KXA"])
        # A running task later takes the ordinary update branch, not the late-start fallback.
        continuous._sync_ws_feeds(fm, ws_task, 2, ["tok"], ["KXA", "KXB"], object())
        fm.update_subscriptions.assert_called_once_with(poly_token_ids=["tok"], kalshi_tickers=["KXA", "KXB"])
