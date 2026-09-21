"""Tests for QuoteManager live order placement in market_maker.py."""

import os
import sys
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from market_maker import QuoteManager


class TestQuoteManagerPlacement:
    @pytest.fixture
    def qm(self):
        return QuoteManager()

    def test_dry_run_placement_generates_dry_id(self, qm):
        oid = qm.place_quote("limitless", "mkt-1", "bid", 0.50, 10.0, trader=None)
        assert oid is not None
        assert oid.startswith("dry_limitless_mkt-1_bid_")
        orders = qm.get_active_orders("mkt-1")
        assert len(orders) == 1
        assert orders[0]["order_id"] == oid
        assert orders[0]["status"] == "resting"

    def test_live_limitless_quote_placement(self, qm):
        mock_trader = MagicMock()
        mock_trader.place_order.return_value = {"order_id": "limitless-ord-999", "status": "resting"}

        oid = qm.place_quote("limitless", "mkt-xyz", "bid", 0.45, 9.0, trader=mock_trader)
        assert oid == "limitless-ord-999"
        mock_trader.place_order.assert_called_once_with(
            market_id="mkt-xyz",
            side="buy",
            outcome="yes",
            quantity=20,  # 9.0 / 0.45 = 20
            price=0.45,
            time_in_force="gtc",
        )
        orders = qm.get_active_orders("mkt-xyz")
        assert len(orders) == 1
        assert orders[0]["order_id"] == "limitless-ord-999"
        assert orders[0]["platform"] == "limitless"

    def test_live_kalshi_quote_placement(self, qm):
        mock_trader = MagicMock()
        mock_trader.place_order.return_value = {"order_id": "k-ord-123"}

        oid = qm.place_quote("kalshi", "KXBTC-26MAR", "ask", 0.60, 12.0, trader=mock_trader)
        assert oid == "k-ord-123"
        mock_trader.place_order.assert_called_once_with(
            ticker="KXBTC-26MAR",
            side="yes",
            action="sell",
            count=20,  # 12.0 / 0.60 = 20
            price_dollars=0.60,
            time_in_force="gtc",
        )

    def test_live_polymarket_quote_placement(self, qm):
        mock_trader = MagicMock()
        mock_trader.place_order.return_value = {"orderID": "pm-ord-555"}

        oid = qm.place_quote("polymarket", "token-abc", "bid", 0.50, 10.0, trader=mock_trader)
        assert oid == "pm-ord-555"
        mock_trader.place_order.assert_called_once_with(
            token_id="token-abc",
            side="BUY",
            price=0.50,
            size=20.0,
            order_type="GTC",
        )

    def test_live_quote_placement_failure_returns_none(self, qm):
        mock_trader = MagicMock()
        mock_trader.place_order.return_value = None

        oid = qm.place_quote("limitless", "mkt-fail", "bid", 0.50, 10.0, trader=mock_trader)
        assert oid is None
        assert len(qm.get_active_orders("mkt-fail")) == 0

    def test_live_quote_cancel_calls_exchange(self, qm):
        mock_trader = MagicMock()
        mock_trader.place_order.return_value = {"order_id": "ord-live-777"}
        mock_trader.cancel_order.return_value = True

        oid = qm.place_quote("limitless", "mkt-1", "bid", 0.50, 10.0, trader=mock_trader)
        assert oid == "ord-live-777"

        res = qm.cancel_quote(oid, trader=mock_trader)
        assert res is True
        mock_trader.cancel_order.assert_called_once_with("ord-live-777")
        assert len(qm.get_active_orders("mkt-1")) == 0
