"""Tests for sxbet_api.py — SX Bet Exchange API client."""

import sys
import os
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import sxbet_api
from sxbet_api import SXBetClient, SXBET_API_URL


@pytest.fixture(autouse=True)
def reset_circuit_breaker():
    """Reset sxbet circuit breaker state between tests to prevent state bleed."""
    sxbet_api._circuit.record_success()
    yield
    sxbet_api._circuit.record_success()


@pytest.fixture
def client():
    """Authenticated SXBetClient with mocked session."""
    c = SXBetClient()
    c.api_key = "test_key"
    c.authenticated = True
    c.session = MagicMock()
    return c


# ---------------------------------------------------------------------------
# TestSXBetLogin
# ---------------------------------------------------------------------------

class TestSXBetLogin:
    """Login, env-var fallback, and failure paths."""

    def test_login_success(self):
        c = SXBetClient()
        c.session = MagicMock()
        resp = MagicMock(status_code=200)
        c.session.get.return_value = resp
        assert c.login("my_key") is True
        assert c.authenticated is True
        assert c.api_key == "my_key"
        assert c.wallet_address is None

    def test_login_env_var_fallback(self):
        c = SXBetClient()
        c.session = MagicMock()
        c.session.get.return_value = MagicMock(status_code=200)
        with patch.dict(os.environ, {"SXBET_API_KEY": "env_key"}):
            assert c.login() is True
        assert c.api_key == "env_key"

    def test_login_keeps_wallet_identifier_separate(self):
        c = SXBetClient()
        c.session = MagicMock()
        c.session.get.return_value = MagicMock(status_code=200)
        with patch.dict(os.environ, {"SXBET_WALLET_ADDRESS": "0xwallet"}):
            assert c.login("api-key") is True
        assert c.api_key == "api-key"
        assert c.wallet_address == "0xwallet"

    def test_login_allows_public_mode_without_key(self):
        c = SXBetClient()
        c.session = MagicMock()
        c.session.get.return_value = MagicMock(status_code=200)
        with patch.dict(os.environ, {}, clear=True):
            assert c.login() is True
        assert c.authenticated is True
        assert c.api_key is None

    def test_login_fails_bad_status(self):
        c = SXBetClient()
        c.session = MagicMock()
        c.session.get.return_value = MagicMock(status_code=401)
        assert c.login("bad_key") is False
        assert c.authenticated is False

    def test_login_fails_request_exception(self):
        c = SXBetClient()
        c.session = MagicMock()
        c.session.get.side_effect = Exception("timeout")
        # requests.RequestException is caught; generic Exception propagates
        import requests as req
        c.session.get.side_effect = req.RequestException("timeout")
        assert c.login("key") is False

    def test_reverse_proxy_receives_proxy_token(self):
        with patch.object(sxbet_api, "SXBET_API_URL", "https://proxy.example.com"), \
                patch.dict(os.environ, {"SXBET_PROXY_TOKEN": "test-proxy-token"}):
            c = SXBetClient()
        assert c.session.headers["X-Proxy-Token"] == "test-proxy-token"

    def test_official_api_never_receives_proxy_token(self):
        with patch.object(sxbet_api, "SXBET_API_URL", "https://api.sx.bet"), \
                patch.dict(os.environ, {"SXBET_PROXY_TOKEN": "test-proxy-token"}):
            c = SXBetClient()
        assert "X-Proxy-Token" not in c.session.headers


# ---------------------------------------------------------------------------
# TestSXBetMarketPrice
# ---------------------------------------------------------------------------

class TestSXBetMarketPrice:
    """get_market_price — extracts executable taker prices from orders.

    SX Bet has no dedicated orderbook endpoint. get_market_price calls
    GET /orders?marketHashes={hash} and parses raw orders. Each order has:
      - percentageOdds: maker odds in 10^20 protocol units
      - isMakerBettingOutcomeOne: the maker's outcome
    The taker receives complementary odds on the opposite outcome.
    """

    @staticmethod
    def _order(prob: float, is_outcome_one: bool) -> dict:
        """Build a raw SX Bet order at the given probability and side."""
        return {
            "percentageOdds": str(int(prob * 100 * 10**18)),
            "isMakerBettingOutcomeOne": is_outcome_one,
            "totalBetSize": "0",
            "fillAmount": "0",
        }

    def test_prices_from_bids_and_asks(self, client):
        orders_resp = {"data": [
            self._order(0.65, is_outcome_one=True),   # Taker NO at 0.35
            self._order(0.30, is_outcome_one=False),  # Taker YES at 0.70
        ]}
        with patch.object(client, "_request", return_value=orders_resp):
            yes, no = client.get_market_price({"marketHash": "0xabc"})
        assert yes == pytest.approx(0.70)
        assert no == pytest.approx(0.35)

    def test_maker_outcome_one_only_exposes_taker_outcome_two(self, client):
        orders_resp = {"data": [self._order(0.60, is_outcome_one=True)]}
        with patch.object(client, "_request", return_value=orders_resp):
            yes, no = client.get_market_price({"marketHash": "0xabc"})
        assert yes is None
        assert no == pytest.approx(0.40)

    def test_maker_outcome_two_only_exposes_taker_outcome_one(self, client):
        orders_resp = {"data": [self._order(0.80, is_outcome_one=False)]}
        with patch.object(client, "_request", return_value=orders_resp):
            yes, no = client.get_market_price({"marketHash": "0xabc"})
        assert no is None
        assert yes == pytest.approx(0.20)

    def test_empty_market_hash_returns_none(self, client):
        yes, no = client.get_market_price({"marketHash": ""})
        assert yes is None
        assert no is None

    def test_orderbook_fetch_fails(self, client):
        with patch.object(client, "_request", return_value=None):
            yes, no = client.get_market_price({"marketHash": "0xabc"})
        assert yes is None
        assert no is None


# ---------------------------------------------------------------------------
# TestSXBetOrders
# ---------------------------------------------------------------------------

class TestSXBetOrders:
    """place_order, get_order_status, cancel_order, get_balance."""

    def test_place_order_fails_closed_without_eip712_signing(self, client):
        result = client.place_order("0xhash", "out1", "buy", 0.55, 10.0)
        assert result is None
        client.session.post.assert_not_called()

    def test_place_order_failure(self, client):
        resp = MagicMock(status_code=400, text="bad request")
        client.session.post.return_value = resp
        result = client.place_order("0xhash", "out1", "buy", 0.55, 10.0)
        assert result is None

    def test_get_order_status(self, client):
        with patch.object(client, "_request", return_value={"status": "filled"}):
            result = client.get_order_status("ord1")
        assert result["status"] == "filled"

    def test_cancel_order_success(self, client):
        client.session.delete.return_value = MagicMock(status_code=200)
        assert client.cancel_order("ord1") is True

    def test_cancel_order_failure(self, client):
        client.session.delete.return_value = MagicMock(status_code=500)
        assert client.cancel_order("ord1") is False

    def test_get_balance_uses_balance_key(self, client):
        resp = MagicMock(status_code=200)
        resp.json.return_value = {
            "data": {"balances": [{"availableAmount": "42500000"}]},
        }
        client.session.get.return_value = resp
        assert client.get_balance() == 42.5
        client.session.get.assert_called_once_with(
            f"{SXBET_API_URL}/user/balance-v3",
            headers={"x-sx-api-key": "test_key"},
            timeout=15,
        )

    def test_get_balance_sums_available_token_rows(self, client):
        resp = MagicMock(status_code=200)
        resp.json.return_value = {
            "data": {"balances": [
                {"availableAmount": "99000000"},
                {"availableAmount": "1000000"},
            ]},
        }
        client.session.get.return_value = resp
        assert client.get_balance() == 100.0

    def test_get_balance_fails_closed_on_malformed_schema(self, client):
        resp = MagicMock(status_code=200)
        resp.json.return_value = {"data": {"balances": [{}]}}
        client.session.get.return_value = resp
        assert client.get_balance() is None

    def test_get_balance_fails_closed_when_data_is_list(self, client):
        resp = MagicMock(status_code=200)
        resp.json.return_value = {"data": []}
        client.session.get.return_value = resp
        assert client.get_balance() is None


# ---------------------------------------------------------------------------
# TestSXBetFetchData
# ---------------------------------------------------------------------------

class TestSXBetFetchData:
    """fetch_all_markets, list_runners, get_orderbook, get_market_status."""

    def test_fetch_all_markets_iterates_sports(self, client):
        # New API: GET /markets/active with paginationKey/nextKey, dedup on
        # marketHash. The response wraps markets in data.markets and returns
        # data.nextKey for pagination.
        page1 = {"data": {
            "markets": [{"marketHash": "0xa"}, {"marketHash": "0xb"}],
            "nextKey": "page2",
        }}
        page2 = {"data": {
            "markets": [
                {"marketHash": "0xb"},  # duplicate — should dedupe
                {"marketHash": "0xc"},
            ],
            "nextKey": None,
        }}

        responses = [page1, page2]

        def mock_request(method, endpoint, params=None, json_data=None):
            return responses.pop(0) if responses else None

        with patch.object(client, "_request", side_effect=mock_request):
            result = client.fetch_all_markets()
        assert len(result) == 3
        assert [m["marketHash"] for m in result] == ["0xa", "0xb", "0xc"]

    def test_fetch_all_markets_no_sports(self, client):
        with patch.object(client, "_request", return_value=None):
            assert client.fetch_all_markets() == []

    def test_health_check_uses_cheap_sports_endpoint(self, client):
        with patch.object(client, "_request", return_value={"data": []}) as request:
            assert client.health_check() == {"data": []}
        request.assert_called_once_with("GET", "/sports")

    def test_list_runners(self, client):
        # SX Bet binary markets have synthetic outcome names baked in;
        # list_runners no longer hits the API.
        assert client.list_runners("0xabc") == [
            {"name": "Outcome 1"},
            {"name": "Outcome 2"},
        ]

    def test_list_runners_empty(self, client):
        # Same hardcoded outcomes regardless of network response.
        with patch.object(client, "_request", return_value=None):
            assert client.list_runners("0xabc") == [
                {"name": "Outcome 1"},
                {"name": "Outcome 2"},
            ]

    def test_get_orderbook(self, client):
        # New get_orderbook hits /orders and parses raw orders into
        # bids/asks. Empty orders list yields an empty book.
        with patch.object(client, "_request", return_value={"data": []}):
            assert client.get_orderbook("0xabc") == {"bids": [], "asks": []}

    def test_get_orderbook_converts_maker_odds_to_taker_prices_and_sizes(self, client):
        orders = [
            {
                "percentageOdds": "47375000000000000000",
                "isMakerBettingOutcomeOne": False,
                "totalBetSize": "645730000",
                "fillAmount": "0",
                "pendingFillAmount": "0",
            },
            {
                "percentageOdds": "42000000000000000000",
                "isMakerBettingOutcomeOne": True,
                "totalBetSize": "579620000",
                "fillAmount": "0",
                "pendingFillAmount": "0",
            },
        ]

        with patch.object(client, "_request", return_value={"data": orders}):
            book = client.get_orderbook("0xabc")

        assert book["bids"][0]["price"] == pytest.approx(0.52625)
        assert book["asks"][0]["price"] == pytest.approx(0.58)
        assert book["bids"][0]["size"] == pytest.approx(717.28847, rel=1e-5)

    def test_get_market_price_does_not_invent_missing_taker_side(self, client):
        order = {
            "percentageOdds": "60000000000000000000",
            "isMakerBettingOutcomeOne": False,
            "totalBetSize": "1000000",
            "fillAmount": "0",
            "pendingFillAmount": "0",
        }
        with patch.object(client, "_request", return_value={"data": [order]}):
            yes_price, no_price = client.get_market_price({"marketHash": "0xabc"})

        assert yes_price == pytest.approx(0.4)
        assert no_price is None

    def test_orderbook_batches_stay_below_live_uri_limit(self, client):
        hashes = [f"0x{index:064x}" for index in range(401)]
        with patch.object(client, "_request", return_value={"data": []}) as request:
            client.get_orderbooks_batch(hashes, batch_size=1000)

        assert request.call_count == 3
        requested_counts = [
            len(call.kwargs["params"]["marketHashes"].split(","))
            for call in request.call_args_list
        ]
        assert requested_counts == [200, 200, 1]

    def test_orderbook_batch_circuit_breaker_fails_closed(self, client):
        with patch.object(client, "_request", side_effect=sxbet_api._RateLimitError("circuit open")):
            assert client.get_orderbooks_batch(["0xabc"]) == {}

    def test_get_market_status(self, client):
        with patch.object(client, "_request", return_value={"status": "active"}):
            assert client.get_market_status("0xabc")["status"] == "active"


# ---------------------------------------------------------------------------
# TestSXBetAuthGuard
# ---------------------------------------------------------------------------

class TestSXBetAuthGuard:
    """Public reads work without credentials; account methods fail closed."""

    def setup_method(self):
        self.client = SXBetClient()  # authenticated = False

    def test_public_request_works_without_login(self):
        self.client.session = MagicMock()
        response = MagicMock(status_code=200)
        response.json.return_value = {"data": []}
        self.client.session.request.return_value = response
        assert self.client._request("GET", "/sports") == {"data": []}

    def test_place_order_returns_none(self):
        assert self.client.place_order("h", "o", "buy", 0.5, 1) is None

    def test_get_balance_returns_none(self):
        assert self.client.get_balance() is None

    def test_get_order_status_returns_none(self):
        assert self.client.get_order_status("ord1") is None

    def test_cancel_order_returns_false(self):
        assert self.client.cancel_order("ord1") is False
