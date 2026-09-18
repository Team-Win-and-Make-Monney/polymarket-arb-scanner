"""Tests for ws_feeds.py — WebSocket message handling in FeedManager."""

import asyncio
import sys
import os

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from unittest.mock import MagicMock

# Mock kalshi_api module before importing ws_feeds.
# Save the original so we can restore it after import to avoid polluting
# other test modules that need the real kalshi_api.
_original_kalshi = sys.modules.get("kalshi_api")
mock_kalshi = MagicMock()
mock_kalshi._sign_pss = MagicMock(return_value="fake_sig")
mock_kalshi._load_private_key = MagicMock(return_value=None)
mock_kalshi.KALSHI_BASE_URL = "https://api.elections.kalshi.com"
mock_kalshi.KALSHI_API_PATH = "/trade-api/v2"
sys.modules["kalshi_api"] = mock_kalshi

from ws_feeds import FeedManager, BetfairFeed, RECONNECT_DELAY, RECONNECT_MAX_DELAY

# Restore the original kalshi_api module so other test files aren't polluted
if _original_kalshi is not None:
    sys.modules["kalshi_api"] = _original_kalshi
else:
    del sys.modules["kalshi_api"]


class TestBetfairReadLine:
    """Audit S13: an oversized / incomplete stream message must not OOM — it is
    converted to a ConnectionError that the maintain-loop's reconnect handles."""

    def _feed(self) -> BetfairFeed:
        return BetfairFeed(
            app_key="k", session_token="t",
            market_ids=["1.123"], on_price_update=MagicMock(),
            cache=MagicMock(),
        )

    def test_limit_overrun_raises_connectionerror(self):
        feed = self._feed()
        feed._reader = MagicMock()

        async def _boom(_sep):
            raise asyncio.LimitOverrunError("line too long", 0)

        feed._reader.readuntil = _boom
        with pytest.raises(ConnectionError, match="exceeded"):
            asyncio.run(feed._read_line())

    def test_incomplete_read_raises_connectionerror(self):
        feed = self._feed()
        feed._reader = MagicMock()

        async def _boom(_sep):
            raise asyncio.IncompleteReadError(b"partial", 100)

        feed._reader.readuntil = _boom
        with pytest.raises(ConnectionError, match="closed mid-message"):
            asyncio.run(feed._read_line())


def _make_feed(mock_callback: MagicMock) -> FeedManager:
    """Create a FeedManager with a mock callback and no real credentials."""
    return FeedManager(on_price_update=mock_callback)


# ---------------------------------------------------------------------------
# _handle_kalshi_message
# ---------------------------------------------------------------------------


class TestHandleKalshiMessage:
    def test_orderbook_snapshot_calls_on_price_update(self):
        cb = MagicMock()
        fm = _make_feed(cb)

        data = {
            "type": "orderbook_snapshot",
            "msg": {
                "market_ticker": "PRES-2028-REP",
                "yes": [[50, 100]],
                "no": [[50, 100]],
            },
        }
        fm._handle_kalshi_message(data)

        cb.assert_called_once()
        args = cb.call_args[0]
        assert args[0] == "kalshi"
        assert args[1] == "PRES-2028-REP"
        # Normalised payload includes parsed best ask prices
        assert args[2]["market_ticker"] == "PRES-2028-REP"
        assert args[2]["yes_ask"] == 0.50
        assert args[2]["no_ask"] == 0.50
        assert args[2]["yes_ask_size"] == 100
        assert args[2]["no_ask_size"] == 100

    def test_orderbook_delta_calls_on_price_update(self):
        cb = MagicMock()
        fm = _make_feed(cb)

        data = {
            "type": "orderbook_delta",
            "msg": {
                "market_ticker": "PRES-2028-DEM",
                "price": 45,
                "delta": -5,
            },
        }
        fm._handle_kalshi_message(data)

        cb.assert_called_once()
        args = cb.call_args[0]
        assert args[0] == "kalshi"
        assert args[1] == "PRES-2028-DEM"
        # Delta without yes/no ladder still preserves raw fields
        assert args[2]["market_ticker"] == "PRES-2028-DEM"
        assert args[2]["price"] == 45

    def test_unknown_type_does_not_call(self):
        cb = MagicMock()
        fm = _make_feed(cb)

        data = {
            "type": "subscription_ack",
            "msg": {"market_ticker": "PRES-2028-REP"},
        }
        fm._handle_kalshi_message(data)

        cb.assert_not_called()

    def test_missing_ticker_does_not_call(self):
        cb = MagicMock()
        fm = _make_feed(cb)

        # Valid type but msg has no market_ticker key
        data = {
            "type": "orderbook_snapshot",
            "msg": {"yes": [[50, 100]]},
        }
        fm._handle_kalshi_message(data)

        cb.assert_not_called()

    def test_empty_ticker_does_not_call(self):
        cb = MagicMock()
        fm = _make_feed(cb)

        data = {
            "type": "orderbook_delta",
            "msg": {"market_ticker": "", "price": 50},
        }
        fm._handle_kalshi_message(data)

        cb.assert_not_called()

    def test_missing_msg_key_does_not_call(self):
        cb = MagicMock()
        fm = _make_feed(cb)

        data = {"type": "orderbook_snapshot"}
        fm._handle_kalshi_message(data)

        cb.assert_not_called()


# ---------------------------------------------------------------------------
# _handle_polymarket_message
# ---------------------------------------------------------------------------


class TestHandlePolymarketMessage:
    def test_list_of_events_calls_for_each(self):
        cb = MagicMock()
        fm = _make_feed(cb)

        events = [
            {"asset_id": "token_aaa", "price": "0.55"},
            {"asset_id": "token_bbb", "price": "0.45"},
            {"asset_id": "token_ccc", "price": "0.30"},
        ]
        fm._handle_polymarket_message(events)

        assert cb.call_count == 3
        cb.assert_any_call("polymarket", "token_aaa", events[0])
        cb.assert_any_call("polymarket", "token_bbb", events[1])
        cb.assert_any_call("polymarket", "token_ccc", events[2])

    def test_single_dict_event_calls_once(self):
        cb = MagicMock()
        fm = _make_feed(cb)

        event = {"asset_id": "token_xyz", "price": "0.60"}
        fm._handle_polymarket_message(event)

        cb.assert_called_once_with("polymarket", "token_xyz", event)

    def test_event_missing_asset_id_does_not_call(self):
        cb = MagicMock()
        fm = _make_feed(cb)

        event = {"price": "0.60", "outcome": "Yes"}
        fm._handle_polymarket_message(event)

        cb.assert_not_called()

    def test_empty_asset_id_does_not_call(self):
        cb = MagicMock()
        fm = _make_feed(cb)

        event = {"asset_id": "", "price": "0.60"}
        fm._handle_polymarket_message(event)

        cb.assert_not_called()

    def test_list_with_mixed_valid_invalid(self):
        """Only events with a non-empty asset_id should trigger a callback."""
        cb = MagicMock()
        fm = _make_feed(cb)

        events = [
            {"asset_id": "token_good", "price": "0.50"},
            {"price": "0.40"},  # missing asset_id
            {"asset_id": "", "price": "0.30"},  # empty asset_id
            {"asset_id": "token_also_good", "price": "0.70"},
        ]
        fm._handle_polymarket_message(events)

        assert cb.call_count == 2
        cb.assert_any_call("polymarket", "token_good", events[0])
        cb.assert_any_call("polymarket", "token_also_good", events[3])

    def test_empty_list_does_not_call(self):
        cb = MagicMock()
        fm = _make_feed(cb)

        fm._handle_polymarket_message([])

        cb.assert_not_called()


# ---------------------------------------------------------------------------
# Reconnect backoff constants
# ---------------------------------------------------------------------------


class TestReconnectConstants:
    def test_reconnect_delay_is_5(self):
        assert RECONNECT_DELAY == 5

    def test_reconnect_max_delay_is_60(self):
        assert RECONNECT_MAX_DELAY == 60

    def test_max_delay_greater_than_initial(self):
        assert RECONNECT_MAX_DELAY > RECONNECT_DELAY


# ---------------------------------------------------------------------------
# update_subscriptions (dynamic subscription management)
# ---------------------------------------------------------------------------


class TestUpdateSubscriptions:
    def test_adds_new_poly_tokens(self):
        cb = MagicMock()
        fm = _make_feed(cb)
        fm._poly_token_ids = ["existing_token"]

        fm.update_subscriptions(poly_token_ids=["new_token_1", "new_token_2"])

        assert "new_token_1" in fm._poly_token_ids
        assert "new_token_2" in fm._poly_token_ids
        assert "existing_token" in fm._poly_token_ids
        assert "new_token_1" in fm._pending_poly_subs
        assert "new_token_2" in fm._pending_poly_subs

    def test_adds_new_kalshi_tickers(self):
        cb = MagicMock()
        fm = _make_feed(cb)
        fm._kalshi_tickers = ["EXISTING-TICKER"]

        fm.update_subscriptions(kalshi_tickers=["NEW-TICKER-1", "NEW-TICKER-2"])

        assert "NEW-TICKER-1" in fm._kalshi_tickers
        assert "NEW-TICKER-2" in fm._kalshi_tickers
        assert "EXISTING-TICKER" in fm._kalshi_tickers
        assert "NEW-TICKER-1" in fm._pending_kalshi_subs
        assert "NEW-TICKER-2" in fm._pending_kalshi_subs

    def test_skips_existing_poly_tokens(self):
        cb = MagicMock()
        fm = _make_feed(cb)
        fm._poly_token_ids = ["token_a", "token_b"]

        fm.update_subscriptions(poly_token_ids=["token_a", "token_c"])

        # token_a should not be duplicated
        assert fm._poly_token_ids.count("token_a") == 1
        assert "token_c" in fm._poly_token_ids
        assert "token_a" not in fm._pending_poly_subs
        assert "token_c" in fm._pending_poly_subs

    def test_skips_existing_kalshi_tickers(self):
        cb = MagicMock()
        fm = _make_feed(cb)
        fm._kalshi_tickers = ["TICK-A"]

        fm.update_subscriptions(kalshi_tickers=["TICK-A", "TICK-B"])

        assert fm._kalshi_tickers.count("TICK-A") == 1
        assert "TICK-B" in fm._kalshi_tickers
        assert "TICK-A" not in fm._pending_kalshi_subs
        assert "TICK-B" in fm._pending_kalshi_subs

    def test_skips_empty_tokens(self):
        cb = MagicMock()
        fm = _make_feed(cb)
        fm._poly_token_ids = []

        fm.update_subscriptions(poly_token_ids=["", None, "valid_token"])

        assert "valid_token" in fm._poly_token_ids
        assert len(fm._pending_poly_subs) == 1

    def test_no_args_does_nothing(self):
        cb = MagicMock()
        fm = _make_feed(cb)
        fm._poly_token_ids = ["a"]
        fm._kalshi_tickers = ["B"]

        fm.update_subscriptions()

        assert fm._pending_poly_subs == []
        assert fm._pending_kalshi_subs == []


# ---------------------------------------------------------------------------
# _handle_kalshi_message — orderbook semantics (audit C-1)
# ---------------------------------------------------------------------------


class TestKalshiOrderbookSemantics:
    """Kalshi WS ``yes``/``no`` ladders are BID ladders ([price_cents, qty],
    ascending). The executable YES ask is 100 - best NO bid, and vice versa.
    Deltas mutate a per-ticker book; they do not carry full ladders."""

    SNAPSHOT = {
        "type": "orderbook_snapshot",
        "msg": {
            "market_ticker": "KXTEST-A",
            "yes": [[30, 10], [40, 20]],
            "no": [[45, 50], [55, 80]],
        },
    }

    def test_snapshot_derives_asks_from_opposite_bids(self):
        cb = MagicMock()
        fm = _make_feed(cb)
        fm._handle_kalshi_message(self.SNAPSHOT)

        payload = cb.call_args[0][2]
        # best NO bid 55c -> YES ask 45c, size = that NO bid's qty
        assert payload["yes_ask"] == pytest.approx(0.45)
        assert payload["yes_ask_size"] == 80
        # best YES bid 40c -> NO ask 60c
        assert payload["no_ask"] == pytest.approx(0.60)
        assert payload["no_ask_size"] == 20

    def test_delta_updates_book_and_recomputes_asks(self):
        cb = MagicMock()
        fm = _make_feed(cb)
        fm._handle_kalshi_message(self.SNAPSHOT)
        cb.reset_mock()

        fm._handle_kalshi_message({
            "type": "orderbook_delta",
            "msg": {"market_ticker": "KXTEST-A", "side": "no", "price": 60, "delta": 40},
        })
        payload = cb.call_args[0][2]
        # new best NO bid 60c -> YES ask 40c with the delta's qty
        assert payload["yes_ask"] == pytest.approx(0.40)
        assert payload["yes_ask_size"] == 40

    def test_delta_removing_best_level_falls_back(self):
        cb = MagicMock()
        fm = _make_feed(cb)
        fm._handle_kalshi_message(self.SNAPSHOT)
        cb.reset_mock()

        fm._handle_kalshi_message({
            "type": "orderbook_delta",
            "msg": {"market_ticker": "KXTEST-A", "side": "no", "price": 55, "delta": -80},
        })
        payload = cb.call_args[0][2]
        # 55c level emptied -> best NO bid back to 45c -> YES ask 55c
        assert payload["yes_ask"] == pytest.approx(0.55)
        assert payload["yes_ask_size"] == 50

    def test_delta_without_snapshot_yields_no_phantom_asks(self):
        cb = MagicMock()
        fm = _make_feed(cb)
        fm._handle_kalshi_message({
            "type": "orderbook_delta",
            "msg": {"market_ticker": "KXNOSNAP", "side": "yes", "price": 45, "delta": 5},
        })
        payload = cb.call_args[0][2]
        # only a one-sided partial book exists; NO side unknown -> yes_ask None
        assert payload["yes_ask"] is None

    def test_empty_side_reports_none_not_worst_price(self):
        cb = MagicMock()
        fm = _make_feed(cb)
        fm._handle_kalshi_message({
            "type": "orderbook_snapshot",
            "msg": {"market_ticker": "KXONESIDE", "yes": [[40, 20]], "no": []},
        })
        payload = cb.call_args[0][2]
        assert payload["yes_ask"] is None
        assert payload["yes_ask_size"] == 0
        assert payload["no_ask"] == pytest.approx(0.60)

    def test_deltas_on_both_sides_before_snapshot_publish_none(self):
        cb = MagicMock()
        fm = _make_feed(cb)
        # Deltas alone can never establish a complete book — no phantom asks.
        for side, price in (("yes", 40), ("no", 55)):
            fm._handle_kalshi_message({
                "type": "orderbook_delta",
                "msg": {"market_ticker": "KXDELTAONLY", "side": side, "price": price, "delta": 10},
            })
        payload = cb.call_args[0][2]
        assert payload["yes_ask"] is None
        assert payload["no_ask"] is None
        assert payload["yes_ask_size"] == 0
        assert payload["no_ask_size"] == 0

    def test_reset_kalshi_books_requires_fresh_snapshot(self):
        cb = MagicMock()
        fm = _make_feed(cb)
        fm._handle_kalshi_message(self.SNAPSHOT)
        fm._reset_kalshi_books()
        cb.reset_mock()

        # Post-reconnect delta must not resurrect the stale pre-reset book.
        fm._handle_kalshi_message({
            "type": "orderbook_delta",
            "msg": {"market_ticker": "KXTEST-A", "side": "no", "price": 60, "delta": 40},
        })
        payload = cb.call_args[0][2]
        assert payload["yes_ask"] is None
        assert payload["no_ask"] is None
