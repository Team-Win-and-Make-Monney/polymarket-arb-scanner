"""Tests for ibkr_api.py — IBKR ForecastEx client via ib_insync."""

import pytest
from unittest.mock import MagicMock, patch, PropertyMock

import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# Mock ib_insync module so tests work without it installed
_mock_ib_insync = MagicMock()
if "ib_insync" not in sys.modules:
    sys.modules["ib_insync"] = _mock_ib_insync

import ibkr_api
from ibkr_api import IBKRClient


@pytest.fixture(autouse=True)
def reset_circuit_breaker():
    """Reset ibkr circuit breaker state between tests to prevent state bleed."""
    ibkr_api._circuit.record_success()
    yield
    ibkr_api._circuit.record_success()


@pytest.fixture
def client():
    """Create an IBKRClient with a mocked ib_insync.IB connection."""
    c = IBKRClient()
    c.ib = MagicMock()
    c.ib.isConnected.return_value = True
    c.authenticated = True
    return c


# ---------------------------------------------------------------------------
# Connection
# ---------------------------------------------------------------------------

class TestIBKRConnection:
    def test_login_connects_to_gateway(self):
        mock_ib = MagicMock()
        mock_ib.isConnected.return_value = True
        mock_ib_class = MagicMock(return_value=mock_ib)

        with patch.dict(sys.modules, {"ib_insync": MagicMock(IB=mock_ib_class, LimitOrder=MagicMock)}):
            c = IBKRClient()
            assert c.login(host="127.0.0.1", port=4001, client_id=7) is True
        mock_ib.connect.assert_called_once_with("127.0.0.1", 4001, clientId=7, readonly=False, timeout=10)

    def test_login_fails_when_not_connected(self):
        mock_ib = MagicMock()
        mock_ib.isConnected.return_value = False

        mock_ib_class = MagicMock(return_value=mock_ib)
        with patch.dict(sys.modules, {"ib_insync": MagicMock(IB=mock_ib_class, LimitOrder=MagicMock)}):
            c = IBKRClient()
            assert c.login() is False
        mock_ib.connect.assert_called_once()

    def test_disconnect(self, client):
        client.disconnect()
        client.ib.disconnect.assert_called_once()
        assert client.authenticated is False


# ---------------------------------------------------------------------------
# fetch_all_markets
# ---------------------------------------------------------------------------

class TestFetchAllMarkets:
    def test_groups_by_event(self, client):
        mock_detail_yes = MagicMock()
        mock_detail_yes.contract.conId = 100
        mock_detail_yes.contract.underConId = 1
        mock_detail_yes.contract.localSymbol = "EVT1"
        mock_detail_yes.contract.symbol = "EVT1"
        mock_detail_yes.contract.right = "C"  # Call = YES
        mock_detail_yes.longName = "Will X happen?"
        mock_detail_yes.contract.lastTradeDateOrContractMonth = "20260101"

        mock_detail_no = MagicMock()
        mock_detail_no.contract.conId = 101
        mock_detail_no.contract.underConId = 1
        mock_detail_no.contract.localSymbol = "EVT1"
        mock_detail_no.contract.symbol = "EVT1"
        mock_detail_no.contract.right = "P"  # Put = NO
        mock_detail_no.longName = "Will X happen?"
        mock_detail_no.contract.lastTradeDateOrContractMonth = "20260101"

        # Mock ticker data
        mock_ticker = MagicMock()
        mock_ticker.last = 0.65
        mock_ticker.close = 0.64
        client.ib.ticker.return_value = mock_ticker
        client.ib.reqContractDetails.return_value = [mock_detail_yes, mock_detail_no]

        result = client.fetch_all_markets()
        assert len(result) == 1
        assert result[0]["id"] == "1"
        assert len(result[0]["contracts"]) == 2

    def test_returns_empty_when_not_authenticated(self):
        c = IBKRClient()
        c.authenticated = False
        assert c.fetch_all_markets() == []

    def test_handles_empty_response(self, client):
        client.ib.reqContractDetails.return_value = []
        result = client.fetch_all_markets()
        assert result == []


# ---------------------------------------------------------------------------
# get_market_price
# ---------------------------------------------------------------------------

class TestGetMarketPrice:
    def test_extracts_yes_no_from_contracts(self, client):
        event = {
            "contracts": [
                {"conid": "100", "side": "YES", "price": 0.65},
                {"conid": "101", "side": "NO", "price": 0.35},
            ]
        }
        yes, no = client.get_market_price(event)
        assert yes == 0.65
        assert no == 0.35

    def test_returns_none_for_single_contract(self, client):
        event = {"contracts": [{"conid": "100", "side": "YES", "price": 0.5}]}
        yes, no = client.get_market_price(event)
        assert yes is None
        assert no is None

    def test_prices_are_dollars_not_cents(self, client):
        """Prices should be in 0-1 dollar range (not 0-100 cents)."""
        event = {
            "contracts": [
                {"conid": "100", "side": "YES", "price": 0.70},
                {"conid": "101", "side": "NO", "price": 0.30},
            ]
        }
        yes, no = client.get_market_price(event)
        assert yes == 0.70
        assert no == 0.30


# ---------------------------------------------------------------------------
# place_order — BUY-only constraint
# ---------------------------------------------------------------------------

class TestPlaceOrder:
    def test_buy_order_uses_dollar_price(self, client):
        """Price is passed directly in dollars (no cents conversion)."""
        mock_trade = MagicMock()
        mock_trade.order.orderId = 42
        mock_trade.orderStatus.status = "Submitted"
        mock_trade.orderStatus.filled = 0
        mock_trade.orderStatus.remaining = 5

        mock_contract = MagicMock()
        client._contracts_cache["100"] = mock_contract

        mock_limit_order = MagicMock()
        with patch("ibkr_api.LimitOrder", mock_limit_order, create=True):
            # Need to patch the import inside place_order
            mock_ib_insync = MagicMock()
            mock_ib_insync.LimitOrder.return_value = MagicMock()
            with patch.dict(sys.modules, {"ib_insync": mock_ib_insync}):
                client.ib.placeOrder.return_value = mock_trade
                result = client.place_order("100", 5, 0.65)

        assert result is not None
        assert result["orderId"] == "42"
        assert result["status"] == "Submitted"
        mock_ib_insync.LimitOrder.assert_called_once_with("BUY", 5, 0.65)

    def test_fails_when_not_authenticated(self):
        c = IBKRClient()
        c.authenticated = False
        result = c.place_order("100", 5, 0.65)
        assert result is None

    def test_fails_for_unknown_conid(self, client):
        """Should fail if conid is not in the contract cache."""
        result = client.place_order("999", 5, 0.65)
        assert result is None


# ---------------------------------------------------------------------------
# get_order_status
# ---------------------------------------------------------------------------

class TestGetOrderStatus:
    def test_returns_order_data(self, client):
        mock_trade = MagicMock()
        mock_trade.order.orderId = 42
        mock_trade.orderStatus.status = "Filled"
        mock_trade.orderStatus.filled = 5
        mock_trade.orderStatus.remaining = 0
        mock_trade.orderStatus.avgFillPrice = 0.65
        client.ib.trades.return_value = [mock_trade]

        result = client.get_order_status("42")
        assert result["status"] == "Filled"
        assert result["filled"] == 5

    def test_returns_none_when_not_found(self, client):
        client.ib.trades.return_value = []
        result = client.get_order_status("999")
        assert result is None

    def test_returns_none_when_not_authenticated(self):
        c = IBKRClient()
        result = c.get_order_status("42")
        assert result is None


# ---------------------------------------------------------------------------
# cancel_order
# ---------------------------------------------------------------------------

class TestCancelOrder:
    def test_cancels_open_order(self, client):
        mock_trade = MagicMock()
        mock_trade.order.orderId = 42
        client.ib.openTrades.return_value = [mock_trade]

        result = client.cancel_order("42")
        assert result is True
        client.ib.cancelOrder.assert_called_once_with(mock_trade.order)

    def test_returns_false_when_not_found(self, client):
        client.ib.openTrades.return_value = []
        result = client.cancel_order("999")
        assert result is False


# ---------------------------------------------------------------------------
# get_balance
# ---------------------------------------------------------------------------

class TestGetBalance:
    def test_returns_available_funds(self, client):
        mock_av = MagicMock()
        mock_av.tag = "AvailableFunds"
        mock_av.currency = "USD"
        mock_av.value = "5000.50"
        client.ib.managedAccounts.return_value = ["DU12345"]
        client.ib.accountValues.return_value = [mock_av]

        result = client.get_balance()
        assert result == 5000.50

    def test_returns_none_when_not_authenticated(self):
        c = IBKRClient()
        result = c.get_balance()
        assert result is None

    def test_falls_back_to_buying_power(self, client):
        mock_av = MagicMock()
        mock_av.tag = "BuyingPower"
        mock_av.currency = "USD"
        mock_av.value = "3000.00"
        client.ib.managedAccounts.return_value = ["DU12345"]
        client.ib.accountValues.return_value = [mock_av]

        result = client.get_balance()
        assert result == 3000.00
