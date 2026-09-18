"""Tests for matchbook_api.py — Matchbook Exchange API client."""

import pytest
from unittest.mock import MagicMock, patch

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import matchbook_api
from matchbook_api import MatchbookClient


@pytest.fixture(autouse=True)
def reset_circuit_breaker():
    """Reset matchbook circuit breaker state between tests to prevent state bleed."""
    matchbook_api._circuit.record_success()
    yield
    matchbook_api._circuit.record_success()


@pytest.fixture
def client():
    """Authenticated client with mocked session."""
    c = MatchbookClient()
    c.session = MagicMock()
    c.token = "tok-abc"
    c.authenticated = True
    return c


@pytest.fixture(autouse=True)
def _no_rate_limit():
    """Disable rate-limiting sleeps in tests."""
    with patch.object(matchbook_api, "_rate_limit"):
        yield


@pytest.fixture(autouse=True)
def _dry_run_off():
    """Default DRY_RUN off so live-path tests (circuit, post) exercise the
    network branch; the DRY_RUN tests re-patch it to True."""
    with patch("config.DRY_RUN", False):
        yield


# ---------------------------------------------------------------------------
# TestMatchbookDryRun (audit S12)
# ---------------------------------------------------------------------------

class TestMatchbookDryRun:
    """DRY_RUN must be honored at the client boundary — no live network call."""

    def test_place_order_dry_run_does_not_post(self, client):
        with patch("config.DRY_RUN", True):
            result = client.place_order("mkt1", "run1", "back", 2.0, 10.0)
        assert result is not None and result["dry_run"] is True
        assert result["id"] == "DRY_RUN"          # caller reads resp["id"] for the order id
        client.session.post.assert_not_called()

    def test_cancel_order_dry_run_does_not_delete(self, client):
        with patch("config.DRY_RUN", True):
            assert client.cancel_order("offer-1") is True
        client.session.delete.assert_not_called()


# ---------------------------------------------------------------------------
# TestMatchbookLogin
# ---------------------------------------------------------------------------

class TestMatchbookLogin:
    """Login / authentication tests."""

    def test_login_success(self):
        c = MatchbookClient()
        c.session = MagicMock()
        resp = MagicMock(status_code=200)
        resp.json.return_value = {"session-token": "tok-123"}
        c.session.post.return_value = resp

        assert c.login("user", "pass") is True
        assert c.authenticated is True
        assert c.token == "tok-123"

    def test_login_missing_credentials(self):
        c = MatchbookClient()
        with patch.dict("os.environ", {}, clear=True):
            assert c.login(None, None) is False
        assert c.authenticated is False

    def test_login_no_session_token_in_response(self):
        c = MatchbookClient()
        c.session = MagicMock()
        resp = MagicMock(status_code=200)
        resp.json.return_value = {"user-id": "abc"}
        c.session.post.return_value = resp

        assert c.login("user", "pass") is False
        assert c.authenticated is False

    def test_login_request_exception(self):
        import requests
        c = MatchbookClient()
        c.session = MagicMock()
        c.session.post.side_effect = requests.RequestException("timeout")

        assert c.login("user", "pass") is False
        assert c.authenticated is False

    def test_login_env_var_fallback(self):
        c = MatchbookClient()
        c.session = MagicMock()
        resp = MagicMock(status_code=200)
        resp.json.return_value = {"session-token": "env-tok"}
        c.session.post.return_value = resp

        env = {"MATCHBOOK_USERNAME": "envuser", "MATCHBOOK_PASSWORD": "envpass"}
        with patch.dict("os.environ", env, clear=True):
            assert c.login() is True
        assert c.token == "env-tok"

    def test_login_bad_status_code(self):
        c = MatchbookClient()
        c.session = MagicMock()
        resp = MagicMock(status_code=401)
        c.session.post.return_value = resp

        assert c.login("user", "pass") is False


# ---------------------------------------------------------------------------
# TestMatchbookMarketPrice
# ---------------------------------------------------------------------------

class TestMatchbookMarketPrice:
    """get_market_price decimal-odds-to-probability conversion."""

    def test_back_and_lay_conversion(self, client):
        """Back 2.0 -> yes=0.5, Lay 2.5 -> no=0.6."""
        market = {"prices": [
            {"side": "back", "odds": 2.0},
            {"side": "lay", "odds": 2.5},
        ]}
        yes, no = client.get_market_price(market)
        assert abs(yes - 0.5) < 1e-9
        assert abs(no - 0.6) < 1e-9

    def test_best_back_highest_odds(self, client):
        market = {"prices": [
            {"side": "back", "odds": 2.0},
            {"side": "back", "odds": 3.0},
        ]}
        yes, no = client.get_market_price(market)
        # Best back is 3.0 -> 1/3 ≈ 0.333
        assert abs(yes - 1.0 / 3.0) < 1e-9

    def test_best_lay_lowest_odds(self, client):
        market = {"prices": [
            {"side": "lay", "odds": 4.0},
            {"side": "lay", "odds": 2.5},
        ]}
        yes, no = client.get_market_price(market)
        # Best lay is 2.5 -> no = 1 - 1/2.5 = 0.6
        assert abs(no - 0.6) < 1e-9

    def test_filters_odds_lte_one(self, client):
        market = {"prices": [
            {"side": "back", "odds": 1.0},
            {"side": "lay", "odds": 0.5},
        ]}
        yes, no = client.get_market_price(market)
        assert yes is None
        assert no is None

    def test_empty_prices(self, client):
        market = {"prices": []}
        yes, no = client.get_market_price(market)
        assert yes is None
        assert no is None

    def test_missing_prices_key(self, client):
        yes, no = client.get_market_price({"runners": []})
        assert yes is None
        assert no is None

    def test_derives_no_from_yes_only(self, client):
        market = {"prices": [{"side": "back", "odds": 4.0}]}
        yes, no = client.get_market_price(market)
        assert abs(yes - 0.25) < 1e-9
        assert abs(no - 0.75) < 1e-9


# ---------------------------------------------------------------------------
# TestMatchbookOrders
# ---------------------------------------------------------------------------

class TestMatchbookOrders:
    """Order placement, status, cancel, balance, market status."""

    def test_place_order_success(self, client):
        resp = MagicMock(status_code=201)
        resp.json.return_value = {"id": "offer-1", "status": "open"}
        client.session.post.return_value = resp

        result = client.place_order("m1", "r1", "back", 2.5, 10.0)
        assert result["id"] == "offer-1"

    def test_place_order_failure(self, client):
        resp = MagicMock(status_code=400)
        resp.text = "Bad Request"
        client.session.post.return_value = resp

        assert client.place_order("m1", "r1", "back", 2.5, 10.0) is None

    def test_get_order_status(self, client):
        client.session.request.return_value = MagicMock(
            status_code=200, json=lambda: {"id": "o1", "status": "matched"}
        )
        result = client.get_order_status("o1")
        assert result["status"] == "matched"

    def test_cancel_order_success(self, client):
        client.session.delete.return_value = MagicMock(status_code=200)
        assert client.cancel_order("o1") is True

    def test_cancel_order_failure(self, client):
        client.session.delete.return_value = MagicMock(status_code=404)
        assert client.cancel_order("o1") is False

    def test_get_balance(self, client):
        client.session.request.return_value = MagicMock(
            status_code=200, json=lambda: {"balance": 500.0}
        )
        assert client.get_balance() == 500.0

    def test_get_balance_fallback_key(self, client):
        """Falls back to 'available-balance' when 'balance' is missing."""
        client.session.request.return_value = MagicMock(
            status_code=200, json=lambda: {"available-balance": 250.0}
        )
        assert client.get_balance() == 250.0

    def test_get_market_status(self, client):
        client.session.request.return_value = MagicMock(
            status_code=200, json=lambda: {"id": 1, "status": "open"}
        )
        result = client.get_market_status(1)
        assert result["status"] == "open"


# ---------------------------------------------------------------------------
# TestMatchbookFetchData
# ---------------------------------------------------------------------------

class TestMatchbookFetchData:
    """Event/market data fetching with pagination."""

    def test_fetch_all_events_single_page(self, client):
        client.session.request.return_value = MagicMock(
            status_code=200,
            json=lambda: {"events": [{"id": 1}, {"id": 2}], "total": 2},
        )
        events = client.fetch_all_events()
        assert len(events) == 2

    def test_fetch_all_events_pagination(self, client):
        page1 = {"events": [{"id": i} for i in range(500)], "total": 600}
        page2 = {"events": [{"id": i} for i in range(500, 600)], "total": 600}

        client.session.request.side_effect = [
            MagicMock(status_code=200, json=lambda p=page1: p),
            MagicMock(status_code=200, json=lambda p=page2: p),
        ]
        events = client.fetch_all_events()
        assert len(events) == 600

    def test_fetch_event_markets(self, client):
        client.session.request.return_value = MagicMock(
            status_code=200, json=lambda: {"markets": [{"id": "m1"}]}
        )
        markets = client.fetch_event_markets(1)
        assert len(markets) == 1

    def test_list_runners_requires_event_id(self, client):
        assert client.list_runners("m1", event_id=None) == []

    def test_list_runners_success(self, client):
        client.session.request.return_value = MagicMock(
            status_code=200, json=lambda: {"runners": [{"id": "r1"}]}
        )
        runners = client.list_runners("m1", event_id=1)
        assert runners[0]["id"] == "r1"

    def test_fetch_all_markets_combines(self, client):
        with patch.object(client, "fetch_all_events",
                          return_value=[{"id": 1, "name": "E1"}]):
            with patch.object(client, "fetch_event_markets",
                              return_value=[{"id": "m1"}]):
                markets = client.fetch_all_markets()
        assert len(markets) == 1
        assert markets[0]["_event"]["id"] == 1


# ---------------------------------------------------------------------------
# TestMatchbookAuthGuard
# ---------------------------------------------------------------------------

class TestMatchbookAuthGuard:
    """Methods gracefully return empty/None when not authenticated."""

    def _unauthed(self):
        c = MatchbookClient()
        c.session = MagicMock()
        c.authenticated = False
        return c

    def test_place_order_unauthed(self):
        assert self._unauthed().place_order("m", "r", "back", 2.0, 5) is None

    def test_get_balance_unauthed(self):
        assert self._unauthed().get_balance() is None

    def test_get_order_status_unauthed(self):
        assert self._unauthed().get_order_status("o1") is None

    def test_cancel_order_unauthed(self):
        assert self._unauthed().cancel_order("o1") is False


# ---------------------------------------------------------------------------
# TestMatchbookRequest
# ---------------------------------------------------------------------------

class TestMatchbookRequest:
    """Low-level _request helper tests."""

    def test_accepts_200(self, client):
        client.session.request.return_value = MagicMock(
            status_code=200, json=lambda: {"ok": True}
        )
        assert client._request("GET", "/test")["ok"] is True

    def test_accepts_201(self, client):
        client.session.request.return_value = MagicMock(
            status_code=201, json=lambda: {"created": True}
        )
        assert client._request("POST", "/test")["created"] is True

    def test_returns_none_on_404(self, client):
        client.session.request.return_value = MagicMock(
            status_code=404, text="Not Found"
        )
        assert client._request("GET", "/missing") is None


# ---------------------------------------------------------------------------
# Circuit breaker on order methods (audit C-3/B42)
# ---------------------------------------------------------------------------

class TestMatchbookCircuitBreaker:
    def test_place_order_skips_when_circuit_open(self, client, monkeypatch):
        monkeypatch.setattr(matchbook_api._circuit, "is_open", lambda: True)
        assert client.place_order("mkt", "run", "back", 2.0, 5.0) is None
        client.session.post.assert_not_called()

    def test_cancel_order_skips_when_circuit_open(self, client, monkeypatch):
        monkeypatch.setattr(matchbook_api._circuit, "is_open", lambda: True)
        assert client.cancel_order("offer-1") is False
        client.session.delete.assert_not_called()

    def test_place_order_records_failure_on_non_2xx(self, client, monkeypatch):
        monkeypatch.setattr(matchbook_api._circuit, "is_open", lambda: False)
        recorded = []
        monkeypatch.setattr(matchbook_api._circuit, "record_failure", lambda: recorded.append(1))
        client.session.post.return_value = MagicMock(status_code=500, text="err")
        client.place_order("mkt", "run", "back", 2.0, 5.0)
        assert recorded  # non-2xx records a circuit failure
