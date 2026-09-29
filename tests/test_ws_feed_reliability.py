"""Regression tests for WS feed reliability (Kalshi fixed-point schema, sequence
gaps, liveness vs book changes, and the dedicated feed thread).

Background (2026-09-28 production logs, deployment 88358b0): the Kalshi feed
logged DEGRADED ~2-3 minutes after every fresh subscription and "recovered" only
when a new subscription produced a snapshot, because the handler read the
legacy integer-cent fields while Kalshi now sends ``*_dollars_fp`` /
``price_dollars`` / ``delta_fp``. Polymarket closed the socket with 1013
"slow consumer" because the scan loop blocks the event loop that read it.
"""

import asyncio
import os
import sys
import threading
import time

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from unittest.mock import MagicMock

_original_kalshi = sys.modules.get("kalshi_api")
mock_kalshi = MagicMock()
mock_kalshi._sign_pss = MagicMock(return_value="fake_sig")
mock_kalshi._load_private_key = MagicMock(return_value=None)
sys.modules["kalshi_api"] = mock_kalshi

import ws_feeds  # noqa: E402

# One import style: tests also monkeypatch attributes on the module itself.
FeedManager = ws_feeds.FeedManager
FeedHealthTracker = ws_feeds.FeedHealthTracker
KalshiBookInvalid = ws_feeds.KalshiBookInvalid
KalshiSequenceGap = ws_feeds.KalshiSequenceGap
_kalshi_dollars_to_cents = ws_feeds._kalshi_dollars_to_cents

if _original_kalshi is not None:
    sys.modules["kalshi_api"] = _original_kalshi
else:
    del sys.modules["kalshi_api"]


def _fp_snapshot(ticker="KXFP-A", sid=None, seq=None):
    data = {
        "type": "orderbook_snapshot",
        "msg": {
            "market_ticker": ticker,
            "yes_dollars_fp": [["0.3000", "10.00"], ["0.4000", "20.00"]],
            "no_dollars_fp": [["0.4500", "50.00"], ["0.5500", "80.00"]],
        },
    }
    if sid is not None:
        data["sid"] = sid
        data["seq"] = seq
    return data


def _fp_delta(ticker="KXFP-A", side="no", price="0.5500", delta="-80.00", sid=None, seq=None):
    data = {
        "type": "orderbook_delta",
        "msg": {"market_ticker": ticker, "price_dollars": price, "delta_fp": delta, "side": side},
    }
    if sid is not None:
        data["sid"] = sid
        data["seq"] = seq
    return data


# ---------------------------------------------------------------------------
# Fixed-point schema
# ---------------------------------------------------------------------------


class TestKalshiFixedPointSchema:
    def test_snapshot_parses_dollar_fp_ladders(self):
        cb = MagicMock()
        fm = FeedManager(on_price_update=cb, use_feed_thread=False)
        fm._handle_kalshi_message(_fp_snapshot())

        payload = cb.call_args[0][2]
        assert payload["yes_ask"] == pytest.approx(0.45)
        assert payload["yes_ask_size"] == 80
        assert payload["no_ask"] == pytest.approx(0.60)
        assert payload["no_ask_size"] == 20
        book = fm.get_kalshi_orderbook("KXFP-A")["orderbook"]
        assert book["yes"] == [[30, 10.0], [40, 20.0]]
        assert book["no"] == [[45, 50.0], [55, 80.0]]

    def test_fp_delta_updates_book(self):
        """Before the fix every fp delta was silently dropped."""
        cb = MagicMock()
        fm = FeedManager(on_price_update=cb, use_feed_thread=False)
        fm._handle_kalshi_message(_fp_snapshot())
        cb.reset_mock()

        fm._handle_kalshi_message(_fp_delta())  # remove best NO bid 55c

        cb.assert_called_once()
        payload = cb.call_args[0][2]
        assert payload["yes_ask"] == pytest.approx(0.55)
        assert payload["yes_ask_size"] == 50
        assert fm.get_kalshi_orderbook("KXFP-A")["orderbook"]["no"] == [[45, 50.0]]

    def test_fp_delta_adds_level(self):
        cb = MagicMock()
        fm = FeedManager(on_price_update=cb, use_feed_thread=False)
        fm._handle_kalshi_message(_fp_snapshot())
        fm._handle_kalshi_message(_fp_delta(side="yes", price="0.4200", delta="7.00"))
        payload = cb.call_args[0][2]
        assert payload["no_ask"] == pytest.approx(0.58)
        assert payload["no_ask_size"] == 7

    def test_subcent_price_is_kept_exact(self):
        assert _kalshi_dollars_to_cents("0.4200") == 42
        assert _kalshi_dollars_to_cents("0.0450") == pytest.approx(4.5)
        assert _kalshi_dollars_to_cents("1.5000") is None
        assert _kalshi_dollars_to_cents("-0.01") is None
        assert _kalshi_dollars_to_cents("bad") is None
        assert _kalshi_dollars_to_cents("NaN") is None

    def test_unparsable_delta_on_live_book_fails_closed(self):
        """An unknown level change must not leave the old book being served."""
        cb = MagicMock()
        fm = FeedManager(on_price_update=cb, use_feed_thread=False)
        fm._handle_kalshi_message(_fp_snapshot(sid=4, seq=1))
        cb.reset_mock()
        with pytest.raises(KalshiBookInvalid):
            fm._handle_kalshi_message(_fp_delta(price="garbage", sid=4, seq=2))
        assert fm.get_kalshi_orderbook("KXFP-A") is None
        assert 4 not in fm._kalshi_seq
        payload = cb.call_args[0][2]
        assert payload["_invalidated"] is True
        assert payload["yes_ask"] is None and payload["no_ask"] is None

    def test_unparsable_delta_without_book_publishes_nothing_executable(self):
        cb = MagicMock()
        fm = FeedManager(on_price_update=cb, use_feed_thread=False)
        fm._handle_kalshi_message(_fp_delta(price="garbage"))
        payload = cb.call_args[0][2]
        assert payload["yes_ask"] is None and payload["no_ask"] is None

    def test_fractional_deltas_that_cancel_remove_the_level(self):
        """0.1 + 0.2 - 0.3 is 5.6e-17 in binary floats: the level must be gone,
        not left with a phantom size."""
        cb = MagicMock()
        fm = FeedManager(on_price_update=cb, use_feed_thread=False)
        fm._handle_kalshi_message(_fp_snapshot())
        for delta in ("0.10", "0.20", "-0.30"):
            fm._handle_kalshi_message(_fp_delta(side="yes", price="0.4200", delta=delta))
        assert fm.get_kalshi_orderbook("KXFP-A")["orderbook"]["yes"] == [[30, 10.0], [40, 20.0]]
        payload = cb.call_args[0][2]
        assert payload["no_ask"] == pytest.approx(0.60)
        assert payload["no_ask_size"] == 20

    def test_smallest_positive_quantity_is_kept_exactly(self):
        cb = MagicMock()
        fm = FeedManager(on_price_update=cb, use_feed_thread=False)
        fm._handle_kalshi_message(_fp_snapshot())
        fm._handle_kalshi_message(_fp_delta(side="yes", price="0.4200", delta="0.01"))
        assert cb.call_args[0][2]["no_ask_size"] == 0.01
        # 80.00 - 79.99 leaves exactly 0.01, not 0.00999... or 0.
        fm._handle_kalshi_message(_fp_delta(side="no", price="0.5500", delta="-79.99"))
        payload = cb.call_args[0][2]
        assert payload["yes_ask"] == pytest.approx(0.45)
        assert payload["yes_ask_size"] == 0.01
        assert fm.get_kalshi_orderbook("KXFP-A")["orderbook"]["no"] == [[45, 50.0], [55, 0.01]]

    def test_zero_quantity_snapshot_levels_dropped(self):
        cb = MagicMock()
        fm = FeedManager(on_price_update=cb, use_feed_thread=False)
        data = _fp_snapshot()
        data["msg"]["no_dollars_fp"].append(["0.6000", "0.00"])
        fm._handle_kalshi_message(data)
        assert cb.call_args[0][2]["yes_ask"] == pytest.approx(0.45)

    def test_error_message_is_logged_not_applied(self, caplog):
        cb = MagicMock()
        fm = FeedManager(on_price_update=cb, use_feed_thread=False)
        fm._handle_kalshi_message({"type": "error", "msg": {"code": 6, "msg": "Already subscribed"}})
        cb.assert_not_called()
        assert "Already subscribed" in caplog.text


# ---------------------------------------------------------------------------
# Sequence validation
# ---------------------------------------------------------------------------


class TestKalshiSequence:
    def test_in_order_deltas_apply(self):
        cb = MagicMock()
        fm = FeedManager(on_price_update=cb, use_feed_thread=False)
        fm._handle_kalshi_message(_fp_snapshot(sid=7, seq=1))
        fm._handle_kalshi_message(_fp_delta(sid=7, seq=2))
        fm._handle_kalshi_message(_fp_delta(side="yes", price="0.4200", delta="1.00", sid=7, seq=3))
        assert fm.get_kalshi_orderbook("KXFP-A") is not None

    def test_gap_drops_book_and_raises(self):
        cb = MagicMock()
        fm = FeedManager(on_price_update=cb, use_feed_thread=False)
        fm._handle_kalshi_message(_fp_snapshot(sid=7, seq=1))
        cb.reset_mock()
        with pytest.raises(KalshiSequenceGap):
            fm._handle_kalshi_message(_fp_delta(sid=7, seq=3))
        # Only a non-executable invalidation is published downstream.
        cb.assert_called_once()
        payload = cb.call_args[0][2]
        assert payload["_invalidated"] is True and payload["yes_ask"] is None
        # Never serve a book that missed a delta.
        assert fm.get_kalshi_orderbook("KXFP-A") is None
        assert fm.get_orderbook("kalshi", "KXFP-A") == (None, None)

    def test_duplicate_seq_is_a_gap(self):
        fm = FeedManager(on_price_update=MagicMock(), use_feed_thread=False)
        fm._handle_kalshi_message(_fp_snapshot(sid=7, seq=1))
        fm._handle_kalshi_message(_fp_delta(sid=7, seq=2))
        with pytest.raises(KalshiSequenceGap):
            fm._handle_kalshi_message(_fp_delta(sid=7, seq=2))

    def test_new_snapshot_resets_sequence(self):
        fm = FeedManager(on_price_update=MagicMock(), use_feed_thread=False)
        fm._handle_kalshi_message(_fp_snapshot(sid=7, seq=1))
        with pytest.raises(KalshiSequenceGap):
            fm._handle_kalshi_message(_fp_delta(sid=7, seq=5))
        fm._handle_kalshi_message(_fp_snapshot(sid=8, seq=1))
        fm._handle_kalshi_message(_fp_delta(sid=8, seq=2))
        assert fm.get_kalshi_orderbook("KXFP-A")["orderbook"]["no"] == [[45, 50.0]]

    def test_sids_are_independent(self):
        fm = FeedManager(on_price_update=MagicMock(), use_feed_thread=False)
        fm._handle_kalshi_message(_fp_snapshot("KXA", sid=1, seq=1))
        fm._handle_kalshi_message(_fp_snapshot("KXB", sid=2, seq=1))
        fm._handle_kalshi_message(_fp_delta("KXA", sid=1, seq=2))
        fm._handle_kalshi_message(_fp_delta("KXB", sid=2, seq=2))

    def test_reset_clears_sequence_state(self):
        fm = FeedManager(on_price_update=MagicMock(), use_feed_thread=False)
        fm._handle_kalshi_message(_fp_snapshot(sid=7, seq=1))
        fm._reset_kalshi_books()
        assert fm._kalshi_seq == {}
        # After a reconnect the old sid is unknown: a stray delta is not applied.
        fm._handle_kalshi_message(_fp_delta(sid=7, seq=9))
        assert fm.get_kalshi_orderbook("KXFP-A") is None

    def test_run_kalshi_reconnects_after_gap(self, monkeypatch):
        fm = FeedManager(on_price_update=MagicMock(), use_feed_thread=False)
        calls = []

        async def fake_connect():
            calls.append(1)
            if len(calls) == 1:
                raise KalshiSequenceGap("gap")
            fm._running = False

        async def no_sleep(_s):
            return None

        monkeypatch.setattr(fm, "_connect_kalshi", fake_connect)
        monkeypatch.setattr(ws_feeds.asyncio, "sleep", no_sleep)
        fm._running = True
        asyncio.run(fm._run_kalshi())
        assert len(calls) == 2


# ---------------------------------------------------------------------------
# Liveness vs book change
# ---------------------------------------------------------------------------


class TestFeedLiveness:
    def test_unchanged_delta_counts_as_liveness(self):
        cb = MagicMock()
        alive = MagicMock()
        fm = FeedManager(on_price_update=cb, on_feed_message=alive, use_feed_thread=False)
        fm._handle_kalshi_message(_fp_snapshot())
        cb.reset_mock()
        alive.reset_mock()
        # Removing a level that does not exist changes nothing in the book.
        fm._handle_kalshi_message(_fp_delta(side="yes", price="0.9900", delta="-5.00"))
        cb.assert_not_called()
        alive.assert_called_once_with("kalshi")

    def test_fp_deltas_keep_health_tracker_healthy(self, monkeypatch):
        """The production failure mode: snapshot, then only fp deltas."""
        tracker = FeedHealthTracker(stale_threshold_seconds=120.0)
        fm = FeedManager(on_price_update=MagicMock(), on_feed_message=tracker.record_message,
                         use_feed_thread=False)
        clock = [1_000_000.0]
        monkeypatch.setattr(ws_feeds.time, "time", lambda: clock[0])
        fm._handle_kalshi_message(_fp_snapshot(sid=3, seq=1))
        for i in range(10):
            clock[0] += 60.0
            fm._handle_kalshi_message(_fp_delta(side="yes", price="0.1000", delta="1.00", sid=3, seq=2 + i))
            assert tracker.check_outages()["kalshi"]["in_outage"] is False

    def test_fp_deltas_reach_on_price_update_health_path(self, monkeypatch):
        """Regression for the path reproduced against master 88358b0: health was
        recorded only inside on_price_update, the snapshot called it once (with
        None asks), and every later fp delta was dropped without a callback, so
        the tracker declared an outage while deltas kept arriving."""
        tracker = FeedHealthTracker(stale_threshold_seconds=120.0)
        payloads = []

        def on_price_update(platform, ticker, data):  # master continuous wiring
            payloads.append(data)
            tracker.record_message(platform)

        fm = FeedManager(on_price_update=on_price_update, use_feed_thread=False)
        clock = [1_000_000.0]
        monkeypatch.setattr(ws_feeds.time, "time", lambda: clock[0])
        fm._handle_kalshi_message(_fp_snapshot(sid=3, seq=1))
        assert payloads[-1]["yes_ask"] is not None  # master published None here
        for i in range(5):
            clock[0] += 60.0
            fm._handle_kalshi_message(_fp_delta(side="yes", price="0.3000", delta="5.00", sid=3, seq=2 + i))
            assert tracker.check_outages()["kalshi"]["in_outage"] is False
        assert len(payloads) == 6

    def test_polymarket_messages_count_as_liveness(self):
        alive = MagicMock()
        fm = FeedManager(on_price_update=MagicMock(), on_feed_message=alive, use_feed_thread=False)
        fm._handle_polymarket_message([{"event_type": "best_bid_ask", "asset_id": "t", "best_bid": "0.4"}])
        alive.assert_called_once_with("polymarket")

    def test_liveness_callback_failure_does_not_break_feed(self):
        cb = MagicMock()
        fm = FeedManager(on_price_update=cb, on_feed_message=MagicMock(side_effect=RuntimeError),
                         use_feed_thread=False)
        fm._handle_kalshi_message(_fp_snapshot())
        cb.assert_called_once()


# ---------------------------------------------------------------------------
# Dedicated feed thread
# ---------------------------------------------------------------------------


class TestDedicatedFeedThread:
    def test_feed_keeps_reading_while_caller_loop_blocked(self):
        """Socket processing continues while the caller loop runs blocking code,
        and callbacks still arrive on the caller loop thread, coalesced."""
        delivered = []
        feed_seen = []
        fm = FeedManager(on_price_update=lambda p, k, d: delivered.append(
            (threading.get_ident(), p, k, d["best_bid"])), use_feed_thread=True)

        async def fake_feeds():
            fm._running = True
            for i in range(50):
                fm._handle_polymarket_message(
                    [{"event_type": "best_bid_ask", "asset_id": "tok", "best_bid": str(i / 100)}])
                feed_seen.append(time.monotonic())
                await asyncio.sleep(0.01)

        fm._run_feeds = fake_feeds

        async def main():
            caller = threading.get_ident()
            task = asyncio.create_task(fm.run())
            await asyncio.sleep(0.02)
            blocked_from = time.monotonic()
            time.sleep(0.4)  # synchronous scan stage blocking the caller loop
            blocked_to = time.monotonic()
            await asyncio.gather(task)
            await asyncio.sleep(0.05)
            return caller, blocked_from, blocked_to

        caller, blocked_from, blocked_to = asyncio.run(main())
        fm.stop()

        # The feed kept processing messages during the block.
        assert sum(1 for t in feed_seen if blocked_from < t < blocked_to) >= 10
        # Every callback ran on the caller thread, never on the feed thread.
        assert delivered and all(tid == caller for tid, *_ in delivered)
        # Backlog was coalesced latest-wins, and the final price is the newest.
        assert len(delivered) < 50
        assert delivered[-1][3] == pytest.approx(0.49)

    def test_callback_exception_does_not_stop_dispatch(self):
        seen = []

        def cb(p, k, d):
            seen.append(k)
            if k == "bad":
                raise ValueError("boom")

        fm = FeedManager(on_price_update=cb, use_feed_thread=True)

        async def fake_feeds():
            for key in ("bad", "good"):
                fm._handle_polymarket_message([{"event_type": "best_bid_ask", "asset_id": key}])
            await asyncio.sleep(0)

        fm._run_feeds = fake_feeds

        async def main():
            await fm.run()
            await asyncio.sleep(0.05)

        asyncio.run(main())
        fm.stop()
        assert seen == ["bad", "good"]

    def test_direct_mode_runs_on_caller_loop(self):
        fm = FeedManager(on_price_update=MagicMock(), use_feed_thread=False)
        ran_on = []

        async def fake_feeds():
            ran_on.append(threading.get_ident())

        fm._run_feeds = fake_feeds
        asyncio.run(fm.run())
        assert ran_on == [threading.get_ident()]
        assert fm._feed_thread is None

    def test_stop_shuts_down_feed_thread(self):
        fm = FeedManager(on_price_update=MagicMock(), use_feed_thread=True)

        async def fake_feeds():
            fm._running = True
            while fm._running:
                await asyncio.sleep(0.01)

        fm._run_feeds = fake_feeds

        async def main():
            task = asyncio.create_task(fm.run())
            await asyncio.sleep(0.05)
            thread = fm._feed_thread
            fm.stop()
            # An internal shutdown returns normally; it is not the caller's cancel.
            assert await asyncio.wait_for(task, timeout=2) is None
            return thread

        thread = asyncio.run(main())
        thread.join(timeout=2)
        assert not thread.is_alive()

    def _blocking_feeds(self, fm, started):
        async def fake_feeds():
            fm._running = True
            started.set()
            while fm._running:
                await asyncio.sleep(0.01)
        return fake_feeds

    def test_caller_cancellation_still_propagates(self):
        fm = FeedManager(on_price_update=MagicMock(), use_feed_thread=True)
        started = threading.Event()
        fm._run_feeds = self._blocking_feeds(fm, started)

        async def main():
            task = asyncio.create_task(fm.run())
            await asyncio.get_running_loop().run_in_executor(None, started.wait, 2)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert task.cancelled()

        asyncio.run(main())
        fm.stop()

    def test_caller_cancel_racing_stop_propagates(self):
        """stop() and a caller cancel in the same tick: the caller's intent wins."""
        fm = FeedManager(on_price_update=MagicMock(), use_feed_thread=True)
        started = threading.Event()
        fm._run_feeds = self._blocking_feeds(fm, started)

        async def main():
            task = asyncio.create_task(fm.run())
            await asyncio.get_running_loop().run_in_executor(None, started.wait, 2)
            thread = fm._feed_thread
            fm.stop()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            return thread

        thread = asyncio.run(main())
        thread.join(timeout=2)
        assert not thread.is_alive()

    def test_feed_failure_still_raises(self):
        fm = FeedManager(on_price_update=MagicMock(), use_feed_thread=True)

        async def failing_feeds():
            raise RuntimeError("feed crashed")

        fm._run_feeds = failing_feeds
        with pytest.raises(RuntimeError, match="feed crashed"):
            asyncio.run(fm.run())
        fm.stop()

    def test_late_kalshi_start_runs_on_feed_loop(self, monkeypatch):
        fm = FeedManager(on_price_update=MagicMock(), use_feed_thread=True)
        fm.kalshi_api_key_id = "key"
        fm.kalshi_private_key = object()
        ran_on = []

        async def fake_run_kalshi():
            ran_on.append(threading.current_thread().name)

        monkeypatch.setattr(fm, "_run_kalshi", fake_run_kalshi)

        async def main():
            fm._ensure_feed_loop()
            fm._running = True
            fm.update_subscriptions(kalshi_tickers=["KXA"])
            assert fm.start_kalshi_feed_late() is True
            fut = fm._kalshi_late_task
            await asyncio.wrap_future(fut)

        asyncio.run(main())
        fm.stop()
        assert ran_on == ["ws-feeds"]
        assert fm._pending_kalshi_subs == []


# ---------------------------------------------------------------------------
# Dispatch queue: receipt time, expiry and invalidation (review of #189)
# ---------------------------------------------------------------------------


class _ManualLoop:
    """Stands in for the caller loop: holds scheduled drains until run()."""

    def __init__(self):
        self.scheduled = []

    def call_soon_threadsafe(self, fn, *args):
        self.scheduled.append((fn, args))

    def run(self):
        while self.scheduled:
            fn, args = self.scheduled.pop(0)
            fn(*args)


def _queued_feed(cb, **kw):
    fm = FeedManager(on_price_update=cb, use_feed_thread=True, **kw)
    loop = _ManualLoop()
    fm._dispatch_loop = loop
    return fm, loop


class TestDispatchValidity:
    def test_payload_carries_feed_receipt_time(self, monkeypatch):
        cb = MagicMock()
        fm, loop = _queued_feed(cb)
        clock = [1000.0]
        monkeypatch.setattr(ws_feeds.time, "time", lambda: clock[0])
        fm._handle_kalshi_message(_fp_snapshot())
        clock[0] += 2.0
        loop.run()
        assert cb.call_args[0][2]["_recv_ts"] == 1000.0

    def test_delayed_dispatch_with_no_newer_tick_is_invalidated(self, monkeypatch):
        """Caller loop stalls past the max age and the feed goes quiet: the old
        update must not be published as if it had just arrived, and an
        invalidation stamped now goes in its place."""
        cb = MagicMock()
        fm, loop = _queued_feed(cb)
        fm._dispatch_max_age = 5.0
        clock = [1000.0]
        monkeypatch.setattr(ws_feeds.time, "time", lambda: clock[0])
        fm._handle_kalshi_message(_fp_snapshot())
        clock[0] += 6.0  # stall, no newer tick
        loop.run()
        cb.assert_called_once()
        platform, key, payload = cb.call_args[0]
        assert (platform, key) == ("kalshi", "KXFP-A")
        assert payload["_invalidated"] is True
        assert payload["yes_ask"] is None and payload["no_ask"] is None
        assert payload["_recv_ts"] == 1006.0
        assert fm.dropped_expired_updates == 1
        assert "KXFP-A" not in fm._downstream_keys["kalshi"]

    def test_expired_tick_invalidates_the_older_cached_quote(self, monkeypatch):
        """The caller cache holds an executable quote; the next tick for the key
        expires in the queue. The cache must not keep the older quote."""
        cache = {}
        fm, loop = _queued_feed(_downstream(cache))
        fm._dispatch_max_age = 5.0
        clock = [1000.0]
        monkeypatch.setattr(ws_feeds.time, "time", lambda: clock[0])
        fm._handle_kalshi_message(_fp_snapshot())
        loop.run()
        assert cache[("kalshi", "KXFP-A")]["yes_ask"] == pytest.approx(0.45)
        fm._handle_kalshi_message(_fp_delta())  # queued; the caller loop then stalls
        clock[0] += 6.0
        loop.run()
        entry = cache[("kalshi", "KXFP-A")]
        assert entry["_invalidated"] is True
        assert entry["yes_ask"] is None and entry["no_ask"] is None
        # Polymarket gets its own invalidation shape.
        fm._handle_polymarket_message([{"event_type": "best_bid_ask", "asset_id": "tok", "best_ask": "0.4"}])
        clock[0] += 6.0
        loop.run()
        poly = cache[("polymarket", "tok")]
        assert poly["_invalidated"] is True and poly["best_ask"] is None
        assert poly["_recv_ts"] == clock[0]

    def test_newer_tick_during_expiry_is_delivered_and_stays_tracked(self, monkeypatch):
        """A newer tick queued while the expired one is being handled keeps its
        tracking, and the next drain delivers it over the invalidation."""
        cache = {}
        fm, loop = _queued_feed(_downstream(cache))
        fm._dispatch_max_age = 5.0
        clock = [1000.0]
        monkeypatch.setattr(ws_feeds.time, "time", lambda: clock[0])
        fm._handle_kalshi_message(_fp_snapshot())
        clock[0] += 6.0
        debug = ws_feeds.logger.debug

        def newer_tick_arrives(msg, *args):
            # Runs after the expired entry was popped, before its tracking is cleared.
            if msg.startswith("Expired"):
                fm._handle_kalshi_message(_fp_delta())
            debug(msg, *args)

        monkeypatch.setattr(ws_feeds.logger, "debug", newer_tick_arrives)
        loop.run()
        entry = cache[("kalshi", "KXFP-A")]
        assert not entry.get("_invalidated")
        assert entry["yes_ask"] == pytest.approx(0.55)
        assert "KXFP-A" in fm._downstream_keys["kalshi"]
        # Still tracked, so a later reset invalidates it.
        fm._reset_kalshi_books()
        loop.run()
        assert cache[("kalshi", "KXFP-A")]["_invalidated"] is True

    def test_expired_betfair_tick_replaces_the_cache_entry_with_no_runners(self, monkeypatch):
        """Through the Betfair ingress: an expired tick leaves the continuous-style
        cache entry replaced by a Betfair-shaped invalidation, no runner priced."""
        cache = {}
        fm, loop = _queued_feed(_downstream(cache))
        fm._dispatch_max_age = 5.0
        clock = [1000.0]
        monkeypatch.setattr(ws_feeds.time, "time", lambda: clock[0])
        priced = {"market_id": "1.234", "runners": {111: {"back": [[2.0, 50.0]], "lay": [[2.02, 40.0]]}}}
        fm._on_betfair_update("betfair", "1.234", priced)
        loop.run()
        assert cache[("betfair", "1.234")]["runners"]
        fm._on_betfair_update("betfair", "1.234", {"market_id": "1.234",
                                                   "runners": {111: {"back": [[2.1, 5.0]], "lay": []}}})
        clock[0] += 6.0
        loop.run()
        entry = cache[("betfair", "1.234")]
        assert entry == {"market_id": "1.234", "runners": {}, "_invalidated": True, "_recv_ts": 1006.0}
        assert "1.234" not in fm._downstream_keys["betfair"]

    def test_invalidation_payload_shapes_by_platform(self):
        betfair = FeedManager._invalidation_payload("betfair", "1.9")
        assert betfair == {"market_id": "1.9", "runners": {}, "_invalidated": True}
        kalshi = FeedManager._invalidation_payload("kalshi", "KX")
        assert kalshi["market_ticker"] == "KX" and kalshi["yes_ask"] is None
        poly = FeedManager._invalidation_payload("polymarket", "tok")
        assert poly["asset_id"] == "tok" and poly["best_ask"] is None

    @pytest.mark.parametrize("forged", [1e12, "soon", float("nan"), None, -5.0])
    def test_feed_supplied_receipt_time_is_replaced_at_ingress(self, monkeypatch, forged):
        """A _recv_ts inside a feed event (price_change or pass-through) is
        overwritten with the local receipt time, so a future, non-numeric or NaN
        value can't make an old tick look fresh or break the drain."""
        cache = {}
        fm, loop = _queued_feed(_downstream(cache))
        fm._dispatch_max_age = 5.0
        clock = [1000.0]
        monkeypatch.setattr(ws_feeds.time, "time", lambda: clock[0])
        fm._handle_polymarket_message([
            {"event_type": "price_change", "price_changes": [
                {"asset_id": "tokA", "best_ask": "0.41", "best_bid": "0.39", "_recv_ts": forged}]},
            {"event_type": "last_trade_price", "asset_id": "tokB", "price": "0.5", "_recv_ts": forged},
        ])
        clock[0] += 1.0
        loop.run()  # queued 1s: still dispatched, stamped with the ingress time
        assert cache[("polymarket", "tokA")]["_recv_ts"] == 1000.0
        assert cache[("polymarket", "tokA")]["best_ask"] == pytest.approx(0.41)
        assert cache[("polymarket", "tokB")]["_recv_ts"] == 1000.0
        # A later tick that waits past the max age expires, whatever it claims.
        fm._handle_polymarket_message([{"event_type": "last_trade_price", "asset_id": "tokB",
                                        "price": "0.6", "_recv_ts": forged}])
        clock[0] += 6.0
        loop.run()
        assert cache[("polymarket", "tokB")]["_invalidated"] is True
        assert fm.dropped_expired_updates == 1

    def test_expired_tick_after_a_gap_is_fenced_not_redelivered(self, monkeypatch):
        """Generation fencing still wins: a pre-gap tick that also expired is
        dropped as invalidated, and only the gap's invalidation is delivered."""
        cb = MagicMock()
        fm, loop = _queued_feed(cb)
        fm._dispatch_max_age = 5.0
        clock = [1000.0]
        monkeypatch.setattr(ws_feeds.time, "time", lambda: clock[0])
        fm._handle_kalshi_message(_fp_snapshot(sid=1, seq=1))
        with fm._pending_lock:
            stale_batch = dict(fm._pending_updates)
        with pytest.raises(KalshiSequenceGap):
            fm._handle_kalshi_message(_fp_delta(sid=1, seq=9))
        loop.run()
        with fm._pending_lock:
            fm._pending_updates.update(stale_batch)  # racing re-queue of the pre-gap tick
        clock[0] += 6.0
        fm._drain_price_updates()
        assert fm.dropped_invalidated_updates >= 1
        assert fm.dropped_expired_updates == 0
        assert all(c[0][2].get("_invalidated") for c in cb.call_args_list)

    def test_delayed_dispatch_within_max_age_is_delivered(self, monkeypatch):
        cb = MagicMock()
        fm, loop = _queued_feed(cb)
        fm._dispatch_max_age = 5.0
        clock = [1000.0]
        monkeypatch.setattr(ws_feeds.time, "time", lambda: clock[0])
        fm._handle_kalshi_message(_fp_snapshot())
        clock[0] += 4.0
        loop.run()
        cb.assert_called_once()

    def test_invalidation_is_never_expired(self, monkeypatch):
        cb = MagicMock()
        fm, loop = _queued_feed(cb)
        fm._dispatch_max_age = 5.0
        clock = [1000.0]
        monkeypatch.setattr(ws_feeds.time, "time", lambda: clock[0])
        fm._handle_kalshi_message(_fp_snapshot(sid=1, seq=1))
        with pytest.raises(KalshiSequenceGap):
            fm._handle_kalshi_message(_fp_delta(sid=1, seq=5))
        clock[0] += 60.0
        loop.run()
        cb.assert_called_once()
        assert cb.call_args[0][2]["_invalidated"] is True

    def test_gap_between_enqueue_and_drain_publishes_no_prices(self):
        """A pre-gap executable update still queued must not be delivered."""
        cb = MagicMock()
        fm, loop = _queued_feed(cb)
        fm._handle_kalshi_message(_fp_snapshot(sid=1, seq=1))
        fm._handle_kalshi_message(_fp_delta(sid=1, seq=2))  # queued, executable
        with pytest.raises(KalshiSequenceGap):
            fm._handle_kalshi_message(_fp_delta(sid=1, seq=9))
        loop.run()
        delivered = [c[0][2] for c in cb.call_args_list]
        assert delivered and all(p.get("_invalidated") for p in delivered)
        assert all(p["yes_ask"] is None and p["no_ask"] is None for p in delivered)

    def test_gap_invalidates_update_already_popped_for_delivery(self):
        """Generation check: a batch popped before the gap is still rejected."""
        cb = MagicMock()
        fm, loop = _queued_feed(cb)
        fm._handle_kalshi_message(_fp_snapshot(sid=1, seq=1))
        with fm._pending_lock:
            stale_batch = dict(fm._pending_updates)
        with pytest.raises(KalshiSequenceGap):
            fm._handle_kalshi_message(_fp_delta(sid=1, seq=9))
        with fm._pending_lock:
            fm._pending_updates.update(stale_batch)  # simulate a racing re-queue
        loop.run()
        assert all(c[0][2].get("_invalidated") for c in cb.call_args_list)
        assert fm.dropped_invalidated_updates >= 1

    def test_gap_on_one_ticker_keeps_other_ticker_updates(self):
        cb = MagicMock()
        fm, loop = _queued_feed(cb)
        fm._handle_kalshi_message(_fp_snapshot("KXA", sid=1, seq=1))
        fm._handle_kalshi_message(_fp_snapshot("KXB", sid=2, seq=1))
        with pytest.raises(KalshiSequenceGap):
            fm._handle_kalshi_message(_fp_delta("KXA", sid=1, seq=7))
        loop.run()
        by_ticker = {c[0][1]: c[0][2] for c in cb.call_args_list}
        assert by_ticker["KXA"].get("_invalidated") is True
        assert by_ticker["KXB"]["yes_ask"] == pytest.approx(0.45)

    def test_disconnect_invalidates_queued_kalshi_updates(self, monkeypatch):
        cb = MagicMock()
        fm, loop = _queued_feed(cb)
        fm._handle_kalshi_message(_fp_snapshot(sid=1, seq=1))  # queued

        async def dropped():
            fm._running = False
            raise ConnectionError("socket closed")

        async def no_sleep(_s):
            return None

        monkeypatch.setattr(fm, "_connect_kalshi", dropped)
        monkeypatch.setattr(ws_feeds.asyncio, "sleep", no_sleep)
        fm._running = True
        asyncio.run(fm._run_kalshi())
        loop.run()
        delivered = [c[0][2] for c in cb.call_args_list]
        assert delivered and all(p.get("_invalidated") for p in delivered)
        assert fm.get_kalshi_orderbook("KXFP-A") is None

    def test_polymarket_reset_replaces_queued_update_with_invalidation(self):
        cb = MagicMock()
        fm, loop = _queued_feed(cb)
        fm._handle_polymarket_message([{"event_type": "best_bid_ask", "asset_id": "tok", "best_ask": "0.4"}])
        fm._reset_poly_books()
        loop.run()
        cb.assert_called_once()
        payload = cb.call_args[0][2]
        assert payload["_invalidated"] is True and payload["best_ask"] is None

    def test_reset_without_live_keys_publishes_nothing(self):
        cb = MagicMock()
        fm, loop = _queued_feed(cb)
        fm._reset_poly_books()
        fm._reset_kalshi_books()
        loop.run()
        cb.assert_not_called()

    def test_invalidated_key_is_not_reinvalidated(self):
        cb = MagicMock()
        fm, loop = _queued_feed(cb)
        fm._handle_kalshi_message(_fp_snapshot("KXA"))
        fm._reset_kalshi_books()
        loop.run()
        fm._reset_kalshi_books()
        loop.run()
        assert [c[0][2].get("_invalidated") for c in cb.call_args_list] == [True]


class TestBetfairLiveness:
    def test_betfair_ingress_records_liveness_and_dispatches(self):
        cb = MagicMock()
        alive = MagicMock()
        fm = FeedManager(on_price_update=cb, on_feed_message=alive, use_feed_thread=False)
        fm._on_betfair_update("betfair", "1.23", {"back": 2.0})
        alive.assert_called_once_with("betfair")
        cb.assert_called_once()
        assert cb.call_args[0][:2] == ("betfair", "1.23")

    def test_run_betfair_wires_liveness_callback(self, monkeypatch):
        captured = {}

        class FakeFeed:
            def __init__(self, **kwargs):
                captured.update(kwargs)

            async def connect(self):
                fm._running = False

            def stop(self):
                pass

        fm = FeedManager(on_price_update=MagicMock(), use_feed_thread=False,
                         betfair_app_key="k", betfair_session_token="t")
        monkeypatch.setattr(ws_feeds, "BetfairFeed", FakeFeed)
        fm._running = True
        asyncio.run(fm._run_betfair())
        assert captured["on_price_update"] == fm._on_betfair_update


# ---------------------------------------------------------------------------
# End-to-end invalidation through the runner (second review of #189)
# ---------------------------------------------------------------------------


class _FakeWS:
    """Scripted socket: each item is a message dict, a callable, or an exception."""

    def __init__(self, script):
        self.script = list(script)
        self.sent = []

    async def send(self, msg):
        self.sent.append(msg)

    async def recv(self):
        import json
        while self.script:
            item = self.script.pop(0)
            if isinstance(item, BaseException):
                raise item
            if callable(item):
                item()
                continue
            return json.dumps(item)
        raise ConnectionError("script exhausted")

    async def ping(self):
        return None


def _fake_connect(fm, scripts):
    """websockets.connect stand-in: one scripted socket per connection attempt."""

    class _Ctx:
        def __init__(self, ws):
            self.ws = ws

        async def __aenter__(self):
            return self.ws

        async def __aexit__(self, *exc):
            return False

    def connect(*_a, **_kw):
        if not scripts:
            fm._running = False
            raise ConnectionError("no more connections")
        return _Ctx(_FakeWS(scripts.pop(0)))

    return connect


def _downstream(cache):
    """Mimics continuous.on_price_update: the payload replaces the cache entry."""
    def on_price_update(platform, key, data):
        cache[(platform, key)] = data
    return on_price_update


class TestInvalidationThroughRunner:
    def _kalshi_fm(self, cache):
        fm, loop = _queued_feed(_downstream(cache))
        fm.kalshi_api_key_id = "kid"
        fm.kalshi_private_key = object()
        fm._kalshi_tickers = ["KXA", "KXB"]
        return fm, loop

    def _run(self, fm, runner, monkeypatch, scripts):
        async def no_sleep(_s):
            return None

        monkeypatch.setattr(ws_feeds.websockets, "connect", _fake_connect(fm, scripts))
        monkeypatch.setattr(ws_feeds.asyncio, "sleep", no_sleep)
        fm._running = True
        asyncio.run(runner())

    def test_gap_then_repeated_resets_leave_no_executable_kalshi_quote(self, monkeypatch):
        """handler gap -> _run_kalshi catch -> reset -> reconnect reset -> further
        resets, with the caller loop blocked throughout: when dispatch finally
        runs, every ticker that had an executable quote downstream is invalidated."""
        cache = {}
        fm, loop = self._kalshi_fm(cache)
        scripts = [
            [
                _fp_snapshot("KXA", sid=1, seq=1),
                _fp_snapshot("KXB", sid=2, seq=1),
                loop.run,  # caller loop delivers both executable quotes, then blocks
                _fp_delta("KXA", sid=1, seq=2),  # queued, executable, pre-gap
                _fp_delta("KXA", sid=1, seq=9),  # gap: handler raises
            ],
            [ConnectionError("reset again")],  # reconnect, then another drop
            [ConnectionError("and again")],
        ]
        assert fm._kalshi_tickers == ["KXA", "KXB"]
        self._run(fm, fm._run_kalshi, monkeypatch, scripts)
        loop.run()  # caller loop unblocks
        for ticker in ("KXA", "KXB"):
            entry = cache[("kalshi", ticker)]
            assert entry.get("_invalidated") is True, ticker
            assert entry["yes_ask"] is None and entry["no_ask"] is None

    def test_gap_invalidation_survives_reset_before_any_drain(self, monkeypatch):
        """The gapped ticker is popped before the reset; its queued invalidation
        must not be discarded by the reset's generation bump."""
        cache = {}
        fm, loop = self._kalshi_fm(cache)
        fm._kalshi_tickers = ["KXA"]
        scripts = [[
            _fp_snapshot("KXA", sid=1, seq=1),
            loop.run,
            _fp_delta("KXA", sid=1, seq=5),
        ]]
        self._run(fm, fm._run_kalshi, monkeypatch, scripts)
        loop.run()
        assert cache[("kalshi", "KXA")].get("_invalidated") is True

    def test_new_snapshot_after_reconnect_is_delivered(self, monkeypatch):
        """Invalidation must not shadow fresh data from the next connection."""
        cache = {}
        fm, loop = self._kalshi_fm(cache)
        fm._kalshi_tickers = ["KXA"]
        scripts = [
            [_fp_snapshot("KXA", sid=1, seq=1), loop.run, _fp_delta("KXA", sid=1, seq=5)],
            [_fp_snapshot("KXA", sid=3, seq=1), loop.run, lambda: setattr(fm, "_running", False),
             ConnectionError("done")],
        ]
        self._run(fm, fm._run_kalshi, monkeypatch, scripts)
        entry = cache[("kalshi", "KXA")]
        assert not entry.get("_invalidated")
        assert entry["yes_ask"] == pytest.approx(0.45)

    def test_polymarket_disconnect_invalidates_delivered_quotes(self, monkeypatch):
        cache = {}
        fm, loop = _queued_feed(_downstream(cache))
        fm._poly_token_ids = ["tok1", "tok2"]
        book = {"event_type": "book", "asset_id": "tok1",
                "asks": [{"price": "0.55", "size": "10"}], "bids": [{"price": "0.50", "size": "5"}]}
        scripts = [
            [
                [book, {"event_type": "best_bid_ask", "asset_id": "tok2", "best_ask": "0.47"}],
                loop.run,  # both executable quotes delivered downstream
                {"event_type": "best_bid_ask", "asset_id": "tok1", "best_ask": "0.56"},  # queued
                ConnectionError("1013 slow consumer"),
            ],
            [ConnectionError("reset again")],
        ]
        self._run(fm, fm._run_polymarket, monkeypatch, scripts)
        loop.run()
        for tok in ("tok1", "tok2"):
            entry = cache[("polymarket", tok)]
            assert entry.get("_invalidated") is True, tok
            assert entry["best_ask"] is None and entry["best_bid"] is None
        assert fm.get_polymarket_orderbook("tok1") is None

    def test_invalidated_entries_yield_no_tracking_price(self):
        import importlib
        saved = {n: sys.modules.get(n) for n in ("kalshi_api", "polymarket_api", "display", "recovery", "continuous")}
        for n in ("kalshi_api", "polymarket_api", "display", "recovery"):
            sys.modules[n] = MagicMock()
        sys.modules.pop("continuous", None)
        try:
            continuous = importlib.import_module("continuous")
            fm = FeedManager(on_price_update=MagicMock(), use_feed_thread=False)
            for platform, key in (("kalshi", "KXA"), ("polymarket", "tok")):
                payload = fm._invalidation_payload(platform, key)
                assert continuous._ws_tracking_probability(platform, payload) is None
        finally:
            for n, mod in saved.items():
                if mod is not None:
                    sys.modules[n] = mod
                else:
                    sys.modules.pop(n, None)
