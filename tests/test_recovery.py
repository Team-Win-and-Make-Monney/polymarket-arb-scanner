"""Tests for recovery.py — crash recovery for continuous mode."""

import pytest
from unittest.mock import MagicMock, patch
from recovery import reconcile_orphaned_positions, _check_order_status, _reconcile_pending_trades


class TestReconcileOrphanedPositions:
    def test_no_pending_trades(self):
        db = MagicMock()
        db.get_pending_trades.return_value = []
        db.get_open_positions.return_value = []
        reconcile_orphaned_positions(db)
        db.get_pending_trades.assert_called_once()
        db.get_open_positions.assert_called_once()

    def test_calls_reconcile_for_pending_trades(self):
        db = MagicMock()
        db.get_pending_trades.return_value = [
            {"id": 1, "platform": "polymarket", "order_id": "abc123"},
        ]
        db.get_open_positions.return_value = []
        with patch("recovery._reconcile_pending_trades") as mock_reconcile:
            reconcile_orphaned_positions(db)
            mock_reconcile.assert_called_once()

    def test_logs_open_positions(self):
        db = MagicMock()
        db.get_pending_trades.return_value = []
        db.get_open_positions.return_value = [{"id": 1}]
        # Should not raise
        reconcile_orphaned_positions(db)


class TestReconcilePendingTrades:
    def test_no_order_id_marks_failed(self):
        db = MagicMock()
        trades = [{"id": 1, "platform": "polymarket", "order_id": None}]
        _reconcile_pending_trades(db, trades)
        db.update_trade_status.assert_called_once_with(1, "failed")

    def test_filled_order_marked_filled(self):
        db = MagicMock()
        trades = [{"id": 1, "platform": "polymarket", "order_id": "abc"}]
        with patch("recovery._check_order_status", return_value="filled"):
            _reconcile_pending_trades(db, trades)
        db.update_trade_status.assert_called_once_with(1, "filled")

    def test_canceled_order_marked_failed(self):
        db = MagicMock()
        trades = [{"id": 1, "platform": "kalshi", "order_id": "abc"}]
        with patch("recovery._check_order_status", return_value="canceled"):
            _reconcile_pending_trades(db, trades)
        db.update_trade_status.assert_called_once_with(1, "failed")

    def test_unknown_order_marked_orphaned(self):
        db = MagicMock()
        trades = [{"id": 1, "platform": "betfair", "order_id": "abc"}]
        with patch("recovery._check_order_status", return_value="unknown"):
            _reconcile_pending_trades(db, trades)
        db.update_trade_status.assert_called_once_with(1, "orphaned")

    def test_pending_order_not_updated(self):
        db = MagicMock()
        trades = [{"id": 1, "platform": "polymarket", "order_id": "abc"}]
        with patch("recovery._check_order_status", return_value="pending"):
            _reconcile_pending_trades(db, trades)
        db.update_trade_status.assert_not_called()

    def test_kalshi_client_order_reference_resolves_before_status_check(self):
        db = MagicMock()
        trades = [{
            "id": 1,
            "platform": "kalshi",
            "order_id": "client:durable-id",
        }]
        kalshi = MagicMock()
        kalshi.find_order_by_client_order_id.return_value = {
            "order_id": "venue-order-1",
            "status": "executed",
        }
        kalshi.get_order_status.return_value = {"status": "executed"}

        _reconcile_pending_trades(db, trades, kalshi_client=kalshi)

        db.set_trade_order_id.assert_called_once_with(1, "venue-order-1")
        db.update_trade_status.assert_called_once_with(1, "filled")

    def test_unresolved_kalshi_client_order_reference_blocks_retry(self):
        db = MagicMock()
        trades = [{
            "id": 1,
            "platform": "kalshi",
            "order_id": "client:durable-id",
        }]
        kalshi = MagicMock()
        kalshi.find_order_by_client_order_id.return_value = None

        _reconcile_pending_trades(db, trades, kalshi_client=kalshi)

        db.update_trade_status.assert_called_once_with(1, "orphaned")
        kalshi.get_order_status.assert_not_called()


class TestCheckOrderStatus:
    def test_polymarket_matched(self):
        pm_trader = MagicMock()
        pm_trader.get_order_status.return_value = {"status": "matched"}
        result = _check_order_status("polymarket", "abc", pm_trader=pm_trader)
        assert result == "filled"

    def test_polymarket_live(self):
        pm_trader = MagicMock()
        pm_trader.get_order_status.return_value = {"status": "live"}
        result = _check_order_status("polymarket", "abc", pm_trader=pm_trader)
        assert result == "pending"

    def test_polymarket_canceled(self):
        pm_trader = MagicMock()
        pm_trader.get_order_status.return_value = {"status": "canceled"}
        result = _check_order_status("polymarket", "abc", pm_trader=pm_trader)
        assert result == "canceled"

    def test_polymarket_expired(self):
        pm_trader = MagicMock()
        pm_trader.get_order_status.return_value = {"status": "expired"}
        result = _check_order_status("polymarket", "abc", pm_trader=pm_trader)
        assert result == "canceled"

    def test_polymarket_no_response(self):
        pm_trader = MagicMock()
        pm_trader.get_order_status.return_value = None
        result = _check_order_status("polymarket", "abc", pm_trader=pm_trader)
        assert result == "unknown"

    def test_polymarket_no_client(self):
        result = _check_order_status("polymarket", "abc")
        assert result == "unknown"

    def test_kalshi_executed(self):
        kalshi = MagicMock()
        kalshi.get_order_status.return_value = {"status": "executed"}
        result = _check_order_status("kalshi", "abc", kalshi_client=kalshi)
        assert result == "filled"

    def test_kalshi_resting(self):
        kalshi = MagicMock()
        kalshi.get_order_status.return_value = {"status": "resting"}
        result = _check_order_status("kalshi", "abc", kalshi_client=kalshi)
        assert result == "pending"

    def test_kalshi_canceled(self):
        kalshi = MagicMock()
        kalshi.get_order_status.return_value = {"status": "canceled"}
        result = _check_order_status("kalshi", "abc", kalshi_client=kalshi)
        assert result == "canceled"

    def test_matchbook_matched(self):
        mb = MagicMock()
        mb.get_order_status.return_value = {"status": "matched"}
        result = _check_order_status("matchbook", "123", matchbook_client=mb)
        assert result == "filled"

    def test_matchbook_cancelled(self):
        mb = MagicMock()
        mb.get_order_status.return_value = {"status": "cancelled"}
        result = _check_order_status("matchbook", "123", matchbook_client=mb)
        assert result == "canceled"

    def test_matchbook_open(self):
        mb = MagicMock()
        mb.get_order_status.return_value = {"status": "open"}
        result = _check_order_status("matchbook", "123", matchbook_client=mb)
        assert result == "pending"

    def test_betfair_execution_complete(self):
        bf = MagicMock()
        bf.get_order_status.return_value = {"status": "EXECUTION_COMPLETE"}
        result = _check_order_status("betfair", "abc", betfair_client=bf)
        assert result == "filled"

    def test_betfair_executable(self):
        bf = MagicMock()
        bf.get_order_status.return_value = {"status": "EXECUTABLE"}
        result = _check_order_status("betfair", "abc", betfair_client=bf)
        assert result == "pending"

    def test_betfair_cancelled(self):
        bf = MagicMock()
        bf.get_order_status.return_value = {"status": "CANCELLED"}
        result = _check_order_status("betfair", "abc", betfair_client=bf)
        assert result == "canceled"

    def test_matchbook_settled(self):
        mb = MagicMock()
        mb.get_order_status.return_value = {"status": "settled"}
        result = _check_order_status("matchbook", "abc", matchbook_client=mb)
        assert result == "filled"

    def test_matchbook_no_response(self):
        mb = MagicMock()
        mb.get_order_status.return_value = None
        result = _check_order_status("matchbook", "abc", matchbook_client=mb)
        assert result == "unknown"

    def test_exception_returns_unknown(self):
        pm_trader = MagicMock()
        pm_trader.get_order_status.side_effect = Exception("API error")
        result = _check_order_status("polymarket", "abc", pm_trader=pm_trader)
        assert result == "unknown"

    def test_unknown_platform(self):
        result = _check_order_status("unknown_platform", "abc")
        assert result == "unknown"
