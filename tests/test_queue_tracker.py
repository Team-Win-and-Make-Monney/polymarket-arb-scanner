"""Tests for QueuePositionTracker and microstructure fill probability estimation.

Verifies FIFO orderbook queue tracking, trade depletion, Poisson hazard-rate
fill probability estimation, queue preservation decisions, and MM pilot integration.
"""

from __future__ import annotations

import pytest

from queue_tracker import QueuePositionTracker
from tests.test_mm_pilot import FakeKalshiClient, TICKER, build_pilot, make_book, live_config


@pytest.fixture
def clock():
    return [1_000_000.0]


@pytest.fixture
def pilot_env(monkeypatch):
    from inventory_balancer import reset_inventory_balancer
    reset_inventory_balancer()
    cfg = live_config()
    monkeypatch.setattr(cfg, "MM_KALSHI_PILOT_ENABLED", True)
    monkeypatch.setattr(cfg, "MM_TOXIC_FLOW_ENABLED", True)
    monkeypatch.setattr(cfg, "MM_VOLATILITY_ADJUSTED_ENABLED", True)
    monkeypatch.setattr(cfg, "MM_AUTO_HEDGE_ENABLED", True)
    monkeypatch.setattr(cfg, "MM_QUEUE_TRACKER_ENABLED", True)
    monkeypatch.setattr(cfg, "MM_QUEUE_PRESERVATION_ENABLED", True)
    yield cfg
    reset_inventory_balancer()


class TestQueuePositionTracker:
    """Unit tests for the QueuePositionTracker standalone engine."""

    def test_placement_records_queue_and_initial_depth(self) -> None:
        tracker = QueuePositionTracker(time_fn=lambda: 1000.0)
        book = {
            "yes_bids": [(0.49, 100.0)],
            "no_bids": [(0.49, 100.0)],
        }
        q_pos = tracker.record_placement(
            order_id="ord_1",
            ticker=TICKER,
            side="yes",
            action="buy",
            count=10,
            price=0.49,
            purpose="quote_bid",
            book=book,
        )

        assert q_pos.order_id == "ord_1"
        assert q_pos.initial_depth_ahead == 100.0
        assert q_pos.estimated_queue_ahead == 100.0
        assert q_pos.last_book_depth == 100.0
        assert 0.0 <= q_pos.fill_probability <= 1.0

        pos_dict = tracker.get_queue_position("ord_1")
        assert pos_dict is not None
        assert pos_dict["order_id"] == "ord_1"
        assert pos_dict["count"] == 10

    def test_update_book_depletes_queue_ahead_on_cancellations(self) -> None:
        tracker = QueuePositionTracker(time_fn=lambda: 1000.0)
        book1 = {
            "yes_bids": [(0.49, 100.0)],
            "no_bids": [(0.49, 100.0)],
        }
        tracker.record_placement(
            order_id="ord_1",
            ticker=TICKER,
            side="yes",
            action="buy",
            count=10,
            price=0.49,
            purpose="quote_bid",
            book=book1,
        )

        # Depth drops from 100 to 60 -> 40 contracts ahead were cancelled/filled
        book2 = {
            "yes_bids": [(0.49, 60.0)],
            "no_bids": [(0.49, 100.0)],
        }
        tracker.update_book(TICKER, book2)

        pos = tracker.get_queue_position("ord_1")
        assert pos is not None
        assert pos["estimated_queue_ahead"] == 60.0

        # Depth increases to 80 -> new arrivals sit BEHIND us in FIFO, queue_ahead does not increase
        book3 = {
            "yes_bids": [(0.49, 80.0)],
            "no_bids": [(0.49, 100.0)],
        }
        tracker.update_book(TICKER, book3)

        pos = tracker.get_queue_position("ord_1")
        assert pos is not None
        assert pos["estimated_queue_ahead"] == 60.0

    def test_record_trade_depletes_queue_ahead(self) -> None:
        tracker = QueuePositionTracker(time_fn=lambda: 1000.0)
        book = {
            "yes_bids": [(0.49, 50.0)],
            "no_bids": [(0.49, 50.0)],
        }
        tracker.record_placement(
            order_id="ord_1",
            ticker=TICKER,
            side="yes",
            action="buy",
            count=10,
            price=0.49,
            purpose="quote_bid",
            book=book,
        )

        # Trade print of 20 contracts at 0.49
        tracker.record_trade(TICKER, price=0.49, count=20, timestamp=1001.0)

        pos = tracker.get_queue_position("ord_1")
        assert pos is not None
        assert pos["estimated_queue_ahead"] == 30.0

        # Velocity check: 20 contracts in 60s window
        velocity = tracker.get_trade_velocity(TICKER, window_sec=60.0)
        assert velocity == pytest.approx(20.0 / 60.0)

    def test_record_fill_and_completion(self) -> None:
        tracker = QueuePositionTracker(time_fn=lambda: 1000.0)
        tracker.record_placement(
            order_id="ord_1",
            ticker=TICKER,
            side="yes",
            action="buy",
            count=10,
            price=0.49,
            purpose="quote_bid",
            book={"yes_bids": [(0.49, 20.0)]},
        )

        # Partial fill of 4 contracts: queue ahead resets to 0.0 because our order is active at touch
        tracker.record_fill("ord_1", count=4)
        pos = tracker.get_queue_position("ord_1")
        assert pos is not None
        assert pos["count"] == 6
        assert pos["estimated_queue_ahead"] == 0.0

        # Remaining fill of 6 contracts: order is completed and popped
        tracker.record_fill("ord_1", count=6)
        assert tracker.get_queue_position("ord_1") is None

    def test_record_cancel(self) -> None:
        tracker = QueuePositionTracker(time_fn=lambda: 1000.0)
        tracker.record_placement(
            order_id="ord_1",
            ticker=TICKER,
            side="yes",
            action="buy",
            count=10,
            price=0.49,
            purpose="quote_bid",
        )
        assert tracker.get_queue_position("ord_1") is not None
        tracker.record_cancel("ord_1")
        assert tracker.get_queue_position("ord_1") is None

    def test_should_preserve_quote_priority(self) -> None:
        tracker = QueuePositionTracker(
            enabled=True,
            preservation_enabled=True,
            resize_tolerance=0.20,
            max_queue_ahead=50,
            min_fill_probability=0.01,
            time_fn=lambda: 1000.0,
        )
        book = {
            "yes_bids": [(0.49, 10.0)],
            "no_bids": [(0.49, 10.0)],
        }
        tracker.record_placement(
            order_id="ord_1",
            ticker=TICKER,
            side="yes",
            action="buy",
            count=10,
            price=0.49,
            purpose="quote_bid",
            book=book,
        )

        # Target matches price 0.49 and count 10
        ok, reason, meta = tracker.should_preserve_quote(
            order_id="ord_1",
            target_price=0.49,
            target_count=10,
            book=book,
        )
        assert ok is True
        assert reason == "preserve_priority"
        assert meta["order_id"] == "ord_1"
        assert meta["count"] == 10
        assert meta["price"] == 0.49

    def test_should_preserve_quote_rejects_price_change(self) -> None:
        tracker = QueuePositionTracker(time_fn=lambda: 1000.0)
        tracker.record_placement(
            order_id="ord_1",
            ticker=TICKER,
            side="yes",
            action="buy",
            count=10,
            price=0.49,
            purpose="quote_bid",
        )
        ok, reason, _ = tracker.should_preserve_quote(
            order_id="ord_1",
            target_price=0.48,
            target_count=10,
        )
        assert ok is False
        assert "price_changed" in reason

    def test_should_preserve_quote_rejects_size_drift(self) -> None:
        tracker = QueuePositionTracker(resize_tolerance=0.20, time_fn=lambda: 1000.0)
        tracker.record_placement(
            order_id="ord_1",
            ticker=TICKER,
            side="yes",
            action="buy",
            count=10,
            price=0.49,
            purpose="quote_bid",
        )
        # 10 * 0.20 = 2 contracts tolerance (max 12, min 8) -> 15 contracts rejects
        ok, reason, _ = tracker.should_preserve_quote(
            order_id="ord_1",
            target_price=0.49,
            target_count=15,
        )
        assert ok is False
        assert "size_diff_exceeded" in reason

    def test_should_preserve_quote_rejects_buried_in_queue(self) -> None:
        tracker = QueuePositionTracker(max_queue_ahead=50, time_fn=lambda: 1000.0)
        book = {
            "yes_bids": [(0.49, 150.0)],
            "no_bids": [(0.49, 10.0)],
        }
        tracker.record_placement(
            order_id="ord_1",
            ticker=TICKER,
            side="yes",
            action="buy",
            count=10,
            price=0.49,
            purpose="quote_bid",
            book=book,
        )
        ok, reason, _ = tracker.should_preserve_quote(
            order_id="ord_1",
            target_price=0.49,
            target_count=10,
            book=book,
        )
        assert ok is False
        assert "buried_in_queue" in reason

    def test_should_preserve_quote_rejects_low_fill_probability(self) -> None:
        tracker = QueuePositionTracker(
            min_fill_probability=0.9999,  # unreachably high threshold
            time_fn=lambda: 1000.0,
        )
        tracker.record_placement(
            order_id="ord_1",
            ticker=TICKER,
            side="yes",
            action="buy",
            count=10,
            price=0.49,
            purpose="quote_bid",
        )
        ok, reason, _ = tracker.should_preserve_quote(
            order_id="ord_1",
            target_price=0.49,
            target_count=10,
        )
        assert ok is False
        assert "low_fill_probability" in reason

    def test_status_summary(self) -> None:
        tracker = QueuePositionTracker(time_fn=lambda: 1000.0)
        tracker.record_placement(
            order_id="ord_1",
            ticker=TICKER,
            side="yes",
            action="buy",
            count=10,
            price=0.49,
            purpose="quote_bid",
        )
        status = tracker.get_status()
        assert status["enabled"] is True
        assert status["active_orders_count"] == 1
        assert len(status["orders"]) == 1
        assert status["orders"][0]["order_id"] == "ord_1"


class TestQueueTrackerPilotIntegration:
    """Integration tests verifying QueuePositionTracker wired into KalshiMMPilot."""

    def test_pilot_preserves_quote_when_price_and_size_match(self, pilot_env, clock) -> None:
        # Use book with depth=50 (< MM_MAX_QUEUE_AHEAD=100) so quote is not buried
        client = FakeKalshiClient(books={TICKER: make_book(yes_bid=0.49, no_bid=0.49, yes_qty=50.0, no_qty=50.0)})
        pilot = build_pilot(clock, client=client, selection=[TICKER])

        # Initial placement
        placed_1 = pilot.refresh_market(TICKER)
        assert len(placed_1) == 2
        cancels_before = client.cancel_order_calls

        # Advance clock slightly and refresh again with identical orderbook
        clock[0] += 5.0
        placed_2 = pilot.refresh_market(TICKER)

        # Quotes are preserved: no cancels sent, no new placements
        assert len(placed_2) == 0
        assert client.cancel_order_calls == cancels_before
        assert len(pilot.resting_orders(TICKER)) == 2

        # Check audit trail has G11c_queue_preservation pass decisions
        pres_decisions = [
            d for d in pilot._decisions if d.get("gate") == "G11c_queue_preservation" and d.get("decision") == "pass"
        ]
        assert len(pres_decisions) >= 2

    def test_pilot_replaces_quote_when_price_changes(self, pilot_env, clock) -> None:
        client = FakeKalshiClient(books={TICKER: make_book(yes_bid=0.49, no_bid=0.49, yes_qty=50.0, no_qty=50.0)})
        pilot = build_pilot(clock, client=client, selection=[TICKER])

        placed_1 = pilot.refresh_market(TICKER)
        assert len(placed_1) == 2
        cancels_before = client.cancel_order_calls

        # Shift book within price band [0.45, 0.55] so target price changes (e.g. yes_bid 0.49 -> 0.46)
        shifted_book = make_book(yes_bid=0.46, no_bid=0.52, yes_qty=50.0, no_qty=50.0)
        client.books[TICKER] = shifted_book
        pilot.update_book(TICKER, shifted_book)

        placed_2 = pilot.refresh_market(TICKER)
        # Price changed: cancel and replacement occurs
        assert client.cancel_order_calls > cancels_before
        assert len(placed_2) > 0

    def test_pilot_ws_trade_depletes_queue_ahead(self, pilot_env, clock) -> None:
        client = FakeKalshiClient(books={TICKER: make_book(yes_bid=0.49, no_bid=0.49, yes_qty=50.0, no_qty=50.0)})
        pilot = build_pilot(clock, client=client, selection=[TICKER])
        placed = pilot.refresh_market(TICKER)
        assert len(placed) == 2
        bid_oid = [o["order_id"] for o in pilot.resting_orders(TICKER) if o["purpose"] == "quote_bid"][0]

        # Feed WS trade print at bid price for 50 contracts (clears the 50 contracts ahead)
        pilot.on_ws_trade(TICKER, price=0.49, count=50, timestamp=clock[0])

        q_pos = pilot.get_queue_tracker_status()
        assert q_pos["active_orders_count"] == 2
        ord_info = [o for o in q_pos["orders"] if o["order_id"] == bid_oid][0]
        # Queue ahead was depleted to 0.0
        assert ord_info["estimated_queue_ahead"] == 0.0

    def test_pilot_status_includes_queue_tracker(self, pilot_env, clock) -> None:
        client = FakeKalshiClient()
        pilot = build_pilot(clock, client=client, selection=[TICKER])
        pilot.refresh_market(TICKER)

        status = pilot.get_status()
        assert "queue_tracker" in status
        q_stat = status["queue_tracker"]
        assert q_stat["enabled"] is True
        assert q_stat["active_orders_count"] == 2

    def test_continuous_routes_ws_trade_to_pilot(self, pilot_env, clock) -> None:
        from continuous import _route_kalshi_ws_to_mm_pilot
        client = FakeKalshiClient(books={TICKER: make_book(yes_bid=0.49, no_bid=0.49, yes_qty=50.0, no_qty=50.0)})
        pilot = build_pilot(clock, client=client, selection=[TICKER])
        pilot.refresh_market(TICKER)

        ws_msg = {
            "type": "trade",
            "price": 0.49,
            "count": 25,
            "ts": clock[0],
        }
        _route_kalshi_ws_to_mm_pilot(pilot, "kalshi", TICKER, ws_msg, 0.49)
        q_pos = pilot.get_queue_tracker_status()
        bid_oid = [o["order_id"] for o in pilot.resting_orders(TICKER) if o["purpose"] == "quote_bid"][0]
        ord_info = [o for o in q_pos["orders"] if o["order_id"] == bid_oid][0]
        assert ord_info["estimated_queue_ahead"] == 25.0

    def test_pilot_skips_recreation_when_buried(self, pilot_env, clock) -> None:
        # Start with normal book
        client = FakeKalshiClient(books={TICKER: make_book(yes_bid=0.49, no_bid=0.49, yes_qty=50.0, no_qty=50.0)})
        pilot = build_pilot(clock, client=client, selection=[TICKER])
        pilot.refresh_market(TICKER)
        assert len(pilot.resting_orders(TICKER)) == 2

        # A massive wall of 500 contracts arrives at the bid level (> max_queue_ahead 100)
        wall_book = make_book(yes_bid=0.49, no_bid=0.49, yes_qty=500.0, no_qty=50.0)
        client.books[TICKER] = wall_book
        pilot.update_book(TICKER, wall_book)

        # Refresh pulls the buried bid without immediately recreating it at the same unfillable price level
        pilot.refresh_market(TICKER)
        resting = {o["purpose"]: o for o in pilot.resting_orders(TICKER)}
        # Bid order was cancelled and NOT recreated behind the wall; ask was preserved
        assert "quote_bid" not in resting
        assert "quote_ask" in resting
