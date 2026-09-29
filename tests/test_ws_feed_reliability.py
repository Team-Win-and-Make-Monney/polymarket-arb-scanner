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
from ws_feeds import (  # noqa: E402
    FeedManager,
    FeedHealthTracker,
    KalshiSequenceGap,
    _kalshi_dollars_to_cents,
)

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

    def test_invalid_fp_delta_leaves_book_unchanged(self):
        cb = MagicMock()
        fm = FeedManager(on_price_update=cb, use_feed_thread=False)
        fm._handle_kalshi_message(_fp_snapshot())
        before = fm.get_kalshi_orderbook("KXFP-A")
        cb.reset_mock()
        fm._handle_kalshi_message(_fp_delta(price="garbage"))
        cb.assert_not_called()
        assert fm.get_kalshi_orderbook("KXFP-A") == before

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
        cb.assert_not_called()
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
            await task
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
            with pytest.raises(asyncio.CancelledError):
                await task
            return thread

        thread = asyncio.run(main())
        thread.join(timeout=2)
        assert not thread.is_alive()

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
