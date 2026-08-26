"""Tests for kalshi_api.py — Kalshi API client with RSA-PSS authentication."""

import sys
import time
from pathlib import Path
from unittest.mock import MagicMock, patch, PropertyMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import kalshi_api
from kalshi_api import KalshiClient, _rate_limit, _RateLimitError, KALSHI_BASE_URL, KALSHI_API_PATH


@pytest.fixture(autouse=True)
def reset_circuit_breaker():
    """Reset kalshi circuit breaker state between tests to prevent state bleed."""
    kalshi_api._circuit.record_success()
    yield
    kalshi_api._circuit.record_success()


@pytest.fixture
def client():
    """Return a KalshiClient with mocked session and fake key material."""
    c = KalshiClient()
    c.session = MagicMock()
    c.api_key_id = "test-key-id"
    c.private_key = MagicMock()
    # _sign_pss calls private_key.sign() — return deterministic bytes
    c.private_key.sign.return_value = b"fakesignature"
    return c


def _mock_response(status_code=200, json_data=None, text=""):
    """Build a mock requests.Response."""
    resp = MagicMock()
    resp.status_code = status_code
    resp.json.return_value = json_data or {}
    resp.text = text
    return resp


# ---------------------------------------------------------------------------
# TestKalshiLogin
# ---------------------------------------------------------------------------

class TestKalshiLogin:
    """Login / authentication flows."""

    def test_login_with_pem_file(self, tmp_path):
        """login_with_api_key loads key from PEM file path."""
        c = KalshiClient()
        c.session = MagicMock()
        fake_key = MagicMock()
        fake_key.sign.return_value = b"sig"
        ok_resp = _mock_response(200)
        c.session.request.return_value = ok_resp
        with patch("kalshi_api._load_private_key", return_value=fake_key) as lp:
            result = c.login_with_api_key("kid", private_key_path="/tmp/key.pem")
        lp.assert_called_once_with("/tmp/key.pem")
        assert result is True

    def test_login_with_base64_key(self):
        """login_with_api_key loads key from base64 string."""
        c = KalshiClient()
        c.session = MagicMock()
        fake_key = MagicMock()
        fake_key.sign.return_value = b"sig"
        ok_resp = _mock_response(200)
        c.session.request.return_value = ok_resp
        with patch("kalshi_api._load_private_key_from_base64", return_value=fake_key) as lb:
            result = c.login_with_api_key("kid", private_key_base64="AAAA")
        lb.assert_called_once_with("AAAA")
        assert result is True

    def test_login_fails_no_key_provided(self):
        """Returns False when neither path nor base64 given."""
        c = KalshiClient()
        result = c.login_with_api_key("kid")
        assert result is False

    def test_login_fails_bad_key_file(self):
        """Returns False when PEM file not found."""
        c = KalshiClient()
        with patch("kalshi_api._load_private_key", side_effect=FileNotFoundError):
            result = c.login_with_api_key("kid", private_key_path="/no/such.pem")
        assert result is False

    def test_login_fails_auth_check_non_200(self):
        """Returns False when /exchange/status returns non-200."""
        c = KalshiClient()
        c.session = MagicMock()
        fake_key = MagicMock()
        fake_key.sign.return_value = b"sig"
        c.session.request.return_value = _mock_response(403)
        with patch("kalshi_api._load_private_key", return_value=fake_key):
            result = c.login_with_api_key("kid", private_key_path="/tmp/k.pem")
        assert result is False

    def test_login_fails_auth_check_none_response(self):
        """Returns False when _request returns None."""
        c = KalshiClient()
        c.session = MagicMock()
        fake_key = MagicMock()
        fake_key.sign.return_value = b"sig"
        import requests as _req
        c.session.request.side_effect = _req.RequestException("fail")
        with patch("kalshi_api._load_private_key", return_value=fake_key):
            result = c.login_with_api_key("kid", private_key_path="/tmp/k.pem")
        assert result is False


# ---------------------------------------------------------------------------
# TestKalshiRateLimit
# ---------------------------------------------------------------------------

class TestKalshiRateLimit:
    """Rate-limiting behaviour."""

    def test_rate_limit_sleeps_when_called_fast(self):
        """_rate_limit sleeps if called faster than KALSHI_RATE_LIMIT."""
        import kalshi_api
        kalshi_api._last_request_time = time.time()  # "just called"
        with patch("kalshi_api.time.sleep") as mock_sleep:
            _rate_limit()
        # Should have slept some positive amount
        assert mock_sleep.called
        slept = mock_sleep.call_args[0][0]
        assert slept > 0

    def test_rate_limit_no_sleep_when_enough_gap(self):
        """No sleep when enough time has passed."""
        import kalshi_api
        kalshi_api._last_request_time = time.time() - 10
        with patch("kalshi_api.time.sleep") as mock_sleep:
            _rate_limit()
        mock_sleep.assert_not_called()


# ---------------------------------------------------------------------------
# TestKalshiRequest
# ---------------------------------------------------------------------------

class TestKalshiRequest:
    """Low-level _request method."""

    def test_builds_correct_url_and_headers(self, client):
        """URL is base + api_path + path; auth headers are set."""
        client.session.request.return_value = _mock_response(200)
        resp = client._request("GET", "/markets")
        call_args = client.session.request.call_args
        assert call_args[0][0] == "GET"
        assert call_args[0][1] == KALSHI_BASE_URL + KALSHI_API_PATH + "/markets"
        headers = call_args[1]["headers"]
        assert "KALSHI-ACCESS-KEY" in headers
        assert headers["KALSHI-ACCESS-KEY"] == "test-key-id"

    @patch("kalshi_api._rate_limit")
    def test_retry_on_429(self, mock_rl, client):
        """HTTP 429 raises _RateLimitError (tenacity retries)."""
        client.session.request.return_value = _mock_response(429)
        with pytest.raises(_RateLimitError):
            client._request("GET", "/markets")
        # tenacity retried 3 times
        assert client.session.request.call_count == 3

    @patch("kalshi_api._rate_limit")
    def test_retry_on_connection_error(self, mock_rl, client):
        """ConnectionError is retried then re-raised."""
        import requests as _req
        client.session.request.side_effect = _req.ConnectionError("down")
        with pytest.raises(_req.ConnectionError):
            client._request("GET", "/markets")
        assert client.session.request.call_count == 3

    @patch("kalshi_api._rate_limit")
    def test_returns_none_on_generic_request_exception(self, mock_rl, client):
        """Non-retryable RequestException returns None."""
        import requests as _req
        client.session.request.side_effect = _req.RequestException("weird")
        result = client._request("GET", "/markets")
        assert result is None


# ---------------------------------------------------------------------------
# TestKalshiMarketPrice
# ---------------------------------------------------------------------------

class TestKalshiMarketPrice:
    """get_market_price pure-logic tests."""

    def test_dollar_fields_preferred(self, client):
        """Uses yes_ask_dollars / no_ask_dollars when present."""
        market = {"yes_ask_dollars": 0.62, "no_ask_dollars": 0.40}
        yes, no = client.get_market_price(market)
        assert yes == 0.62
        assert no == 0.40

    def test_cent_fallback(self, client):
        """Falls back to yes_ask/no_ask cent fields divided by 100."""
        market = {"yes_ask": 55, "no_ask": 47}
        yes, no = client.get_market_price(market)
        assert yes == pytest.approx(0.55)
        assert no == pytest.approx(0.47)

    def test_returns_none_when_missing(self, client):
        """Returns (None, None) when no price fields exist."""
        yes, no = client.get_market_price({})
        assert yes is None
        assert no is None

    def test_dollar_zero_falls_through_to_cents(self, client):
        """Dollar fields of 0 are invalid — falls to cent fields."""
        market = {"yes_ask_dollars": 0, "no_ask_dollars": 0.40, "yes_ask": 30, "no_ask": 70}
        yes, no = client.get_market_price(market)
        assert yes == pytest.approx(0.30)
        assert no == pytest.approx(0.70)

    def test_dollar_fields_invalid_type(self, client):
        """Non-numeric dollar fields fall through to cents."""
        market = {"yes_ask_dollars": "bad", "no_ask_dollars": 0.5, "yes_ask": 20, "no_ask": 80}
        yes, no = client.get_market_price(market)
        assert yes == pytest.approx(0.20)
        assert no == pytest.approx(0.80)

    def test_partial_cent_fields_return_none(self, client):
        """If only one cent field present, returns (None, None)."""
        market = {"yes_ask": 40}
        yes, no = client.get_market_price(market)
        assert yes is None
        assert no is None


# ---------------------------------------------------------------------------
# TestKalshiOrders
# ---------------------------------------------------------------------------

class TestKalshiOrders:
    """Order placement, status, cancel, balance, positions."""

    @patch("kalshi_api._rate_limit")
    def test_place_order_success(self, mock_rl, client):
        """place_order returns parsed JSON on 200."""
        client.session.request.return_value = _mock_response(
            200, {"order": {"id": "o1", "status": "resting"}}
        )
        result = client.place_order("TICK", "yes", "buy", 5, 0.60)
        assert result == {"order": {"id": "o1", "status": "resting"}}
        body = client.session.request.call_args[1]["json"]
        assert body["yes_price"] == 60
        assert "no_price" not in body

    @patch("kalshi_api._rate_limit")
    def test_place_order_no_side_price(self, mock_rl, client):
        """Placing a 'no' order sets no_price instead of yes_price."""
        client.session.request.return_value = _mock_response(201, {"order": {"id": "o2"}})
        result = client.place_order("TICK", "no", "buy", 3, 0.35)
        body = client.session.request.call_args[1]["json"]
        assert body["no_price"] == 35
        assert "yes_price" not in body

    @patch("kalshi_api._rate_limit")
    def test_place_order_returns_none_on_400(self, mock_rl, client):
        """Non-success HTTP returns None."""
        client.session.request.return_value = _mock_response(400, text="bad request")
        result = client.place_order("TICK", "yes", "buy", 1, 0.50)
        assert result is None

    @patch("kalshi_api._rate_limit")
    def test_place_order_returns_none_on_exception(self, mock_rl, client):
        """Exception during request returns None."""
        import requests as _req
        client.session.request.side_effect = _req.ConnectionError("down")
        result = client.place_order("TICK", "yes", "buy", 1, 0.50)
        assert result is None

    @patch("kalshi_api._rate_limit")
    def test_place_order_blocks_disallowed_ticker_at_client_boundary(self, mock_rl, client):
        result = client.place_order("KXNFLGAME-26AUG18", "yes", "buy", 1, 0.50)

        assert result is None
        client.session.request.assert_not_called()

    @patch("kalshi_api._rate_limit")
    def test_place_order_allows_reducing_disallowed_ticker(self, mock_rl, client):
        client.session.request.return_value = _mock_response(201, {"order": {"id": "flatten"}})

        result = client.place_order(
            "KXNFLGAME-26AUG18", "yes", "sell", 1, 0.50, reducing=True,
        )

        assert result == {"order": {"id": "flatten"}}
        client.session.request.assert_called_once()

    @patch("kalshi_api._rate_limit")
    def test_get_order_status_success(self, mock_rl, client):
        """get_order_status returns order dict."""
        client.session.request.return_value = _mock_response(
            200, {"order": {"order_id": "o1", "status": "filled"}}
        )
        result = client.get_order_status("o1")
        assert result["status"] == "filled"

    @patch("kalshi_api._rate_limit")
    def test_get_order_status_returns_none_on_failure(self, mock_rl, client):
        """get_order_status returns None on non-200."""
        client.session.request.return_value = _mock_response(404)
        result = client.get_order_status("o1")
        assert result is None

    @patch("kalshi_api._rate_limit")
    def test_cancel_order_success(self, mock_rl, client):
        """cancel_order returns True on 200/204."""
        client.session.request.return_value = _mock_response(204)
        assert client.cancel_order("o1") is True

    @patch("kalshi_api._rate_limit")
    def test_cancel_order_failure(self, mock_rl, client):
        """cancel_order returns False on error status."""
        client.session.request.return_value = _mock_response(400)
        assert client.cancel_order("o1") is False

    @patch("kalshi_api._rate_limit")
    def test_get_balance_converts_cents_to_dollars(self, mock_rl, client):
        """get_balance divides cents by 100."""
        client.session.request.return_value = _mock_response(200, {"balance": 5432})
        result = client.get_balance()
        assert result == pytest.approx(54.32)

    @patch("kalshi_api._rate_limit")
    def test_get_balance_returns_none_on_failure(self, mock_rl, client):
        """get_balance returns None on non-200."""
        client.session.request.return_value = _mock_response(500)
        assert client.get_balance() is None

    @patch("kalshi_api._rate_limit")
    def test_get_positions_success(self, mock_rl, client):
        """get_positions returns market_positions list."""
        positions = [{"ticker": "T1", "position": 10}]
        client.session.request.return_value = _mock_response(200, {"market_positions": positions})
        assert client.get_positions() == positions

    @patch("kalshi_api._rate_limit")
    def test_get_positions_returns_empty_on_failure(self, mock_rl, client):
        """get_positions returns [] on error."""
        client.session.request.return_value = _mock_response(500)
        assert client.get_positions() == []

    @patch("kalshi_api._rate_limit")
    def test_get_positions_raises_on_error_when_opted_in(self, mock_rl, client):
        """Finding #4 support: raise_on_error=True must not silently return
        [] on failure — a bare [] is indistinguishable from confirmed-flat
        to the MM pilot's startup reconciliation gate."""
        from kalshi_api import KalshiPortfolioQueryError
        client.session.request.return_value = _mock_response(500)
        with pytest.raises(KalshiPortfolioQueryError):
            client.get_positions(raise_on_error=True)

    @patch("kalshi_api._rate_limit")
    def test_get_positions_success_with_raise_on_error_true(self, mock_rl, client):
        """raise_on_error=True must not change the success path."""
        positions = [{"ticker": "T1", "position": 10}]
        client.session.request.return_value = _mock_response(200, {"market_positions": positions})
        assert client.get_positions(raise_on_error=True) == positions

    @patch("kalshi_api._rate_limit")
    def test_get_positions_walks_all_pages(self, mock_rl, client):
        """Codex round-2 finding: get_positions used to fetch a single page
        (limit=200) and ignore the documented cursor field entirely — an
        account with a pilot-market position on page 2 would never see it.
        Mirrors the pagination already proven correct for get_fills/
        get_open_orders/get_settlements in this same file."""
        page1 = _mock_response(200, {
            "market_positions": [{"ticker": "T1", "position_fp": "1"}],
            "cursor": "abc",
        })
        page2 = _mock_response(200, {
            "market_positions": [{"ticker": "T2", "position_fp": "2"}],
            "cursor": "",
        })
        client.session.request.side_effect = [page1, page2]
        result = client.get_positions()
        assert [p["ticker"] for p in result] == ["T1", "T2"]
        assert client.session.request.call_count == 2

    @patch("kalshi_api._rate_limit")
    def test_get_positions_raises_on_second_page_failure_when_opted_in(
            self, mock_rl, client):
        """A failure on page 2+ must ALSO raise when opted in, not return
        the page-1 partial result as if it were complete — same ambiguity
        as a first-page failure, just discovered later."""
        from kalshi_api import KalshiPortfolioQueryError
        page1 = _mock_response(200, {
            "market_positions": [{"ticker": "T1", "position_fp": "1"}],
            "cursor": "abc",
        })
        page2 = _mock_response(500)
        client.session.request.side_effect = [page1, page2]
        with pytest.raises(KalshiPortfolioQueryError):
            client.get_positions(raise_on_error=True)

    @patch("kalshi_api._rate_limit")
    def test_get_positions_second_page_failure_returns_partial_when_not_opted_in(
            self, mock_rl, client):
        """Default (raise_on_error=False) keeps the original silent-partial
        behavior — matches get_fills's convention exactly."""
        page1 = _mock_response(200, {
            "market_positions": [{"ticker": "T1", "position_fp": "1"}],
            "cursor": "abc",
        })
        page2 = _mock_response(500)
        client.session.request.side_effect = [page1, page2]
        result = client.get_positions()
        assert [p["ticker"] for p in result] == ["T1"]

    @patch("kalshi_api._rate_limit")
    def test_get_positions_stops_at_max_pages(self, mock_rl, client):
        """Pagination is bounded — an endlessly-cursoring response can't
        spin forever. Default raise_on_error=False keeps this silent."""
        page = _mock_response(200, {
            "market_positions": [{"ticker": "T1", "position_fp": "1"}],
            "cursor": "always-more",
        })
        client.session.request.return_value = page
        result = client.get_positions(max_pages=3)
        assert client.session.request.call_count == 3
        assert len(result) == 3

    @patch("kalshi_api._rate_limit")
    def test_get_positions_raises_when_max_pages_exhausted_with_live_cursor(
            self, mock_rl, client):
        """Codex round-3 finding: every page fetch here SUCCEEDS (unlike
        the earlier failure-mid-pagination tests) but the cursor is STILL
        non-empty after the last one allowed by max_pages — more positions
        genuinely exist beyond what was fetched. This must be exactly as
        ambiguous to a raise_on_error=True caller as an HTTP failure would
        be, not silently returned as if it were a confirmed-complete
        result."""
        from kalshi_api import KalshiPortfolioQueryError
        page = _mock_response(200, {
            "market_positions": [{"ticker": "T1", "position_fp": "1"}],
            "cursor": "always-more",  # never terminates on its own
        })
        client.session.request.return_value = page
        with pytest.raises(KalshiPortfolioQueryError):
            client.get_positions(max_pages=3, raise_on_error=True)
        assert client.session.request.call_count == 3

    @patch("kalshi_api._rate_limit")
    def test_get_positions_max_pages_with_cursor_finally_empty_does_not_raise(
            self, mock_rl, client):
        """Sanity check: if the LAST page (at exactly max_pages) happens to
        have an empty cursor, that's genuine completion, not exhaustion —
        must not raise even with raise_on_error=True."""
        page1 = _mock_response(200, {
            "market_positions": [{"ticker": "T1", "position_fp": "1"}],
            "cursor": "more",
        })
        page2 = _mock_response(200, {
            "market_positions": [{"ticker": "T2", "position_fp": "1"}],
            "cursor": "",  # done, right at the max_pages boundary
        })
        client.session.request.side_effect = [page1, page2]
        result = client.get_positions(max_pages=2, raise_on_error=True)
        assert [p["ticker"] for p in result] == ["T1", "T2"]


# ---------------------------------------------------------------------------
# Finding #3 / #4: get_fills / get_open_orders raise_on_error contract.
# Fail-before: get_fills had no raise_on_error parameter at all (mm_pilot's
# poll_fills / reconcile calling it with raise_on_error=True would hit a
# TypeError), and a page-fetch failure was always silently swallowed into
# whatever fills had been accumulated so far — ambiguous partial vs. empty.
# get_open_orders did not exist at all.
# ---------------------------------------------------------------------------

class TestKalshiPortfolioQueryRaiseOnError:
    @patch("kalshi_api._rate_limit")
    def test_get_fills_default_preserves_silent_partial_return(self, mock_rl, client):
        """Default raise_on_error=False must be byte-for-byte the original
        behavior for existing callers (kalshi_vip.py) — partial results on
        a mid-pagination failure, no exception."""
        page1 = _mock_response(200, {"fills": [{"trade_id": "t1"}], "cursor": "abc"})
        page2 = _mock_response(500)
        client.session.request.side_effect = [page1, page2]
        result = client.get_fills()
        assert result == [{"trade_id": "t1"}]

    @patch("kalshi_api._rate_limit")
    def test_get_fills_raises_on_first_page_failure_when_opted_in(self, mock_rl, client):
        from kalshi_api import KalshiPortfolioQueryError
        client.session.request.return_value = _mock_response(500)
        with pytest.raises(KalshiPortfolioQueryError):
            client.get_fills(raise_on_error=True)

    @patch("kalshi_api._rate_limit")
    def test_get_fills_raises_on_later_page_failure_when_opted_in(self, mock_rl, client):
        """A failure on page 2+ must ALSO raise, not return the page-1
        partial result as if it were the complete/confirmed list."""
        from kalshi_api import KalshiPortfolioQueryError
        page1 = _mock_response(200, {"fills": [{"trade_id": "t1"}], "cursor": "abc"})
        page2 = _mock_response(500)
        client.session.request.side_effect = [page1, page2]
        with pytest.raises(KalshiPortfolioQueryError):
            client.get_fills(raise_on_error=True)

    @patch("kalshi_api._rate_limit")
    def test_get_fills_success_with_raise_on_error_true(self, mock_rl, client):
        client.session.request.return_value = _mock_response(
            200, {"fills": [{"trade_id": "t1"}], "cursor": ""})
        result = client.get_fills(raise_on_error=True)
        assert result == [{"trade_id": "t1"}]

    @patch("kalshi_api._rate_limit")
    def test_get_fills_raises_when_max_pages_exhausted_with_live_cursor(
            self, mock_rl, client):
        """Codex round-3 finding: every page fetch succeeds but the cursor
        is STILL non-empty after the last page allowed by max_pages — more
        fills genuinely exist for this min_ts window beyond what was
        fetched. Must raise under raise_on_error=True exactly like an HTTP
        failure would, not silently return a partial fill list that looks
        confirmed-complete."""
        from kalshi_api import KalshiPortfolioQueryError
        page = _mock_response(200, {
            "fills": [{"trade_id": "t1"}], "cursor": "always-more",
        })
        client.session.request.return_value = page
        with pytest.raises(KalshiPortfolioQueryError):
            client.get_fills(max_pages=3, raise_on_error=True)
        assert client.session.request.call_count == 3

    @patch("kalshi_api._rate_limit")
    def test_get_fills_max_pages_exhausted_without_raise_on_error_is_silent(
            self, mock_rl, client):
        """Default raise_on_error=False preserves the original
        silent-partial-return behavior even for the max-pages-exhausted
        case — only opting in changes anything."""
        page = _mock_response(200, {
            "fills": [{"trade_id": "t1"}], "cursor": "always-more",
        })
        client.session.request.return_value = page
        result = client.get_fills(max_pages=3)
        assert len(result) == 3

    @patch("kalshi_api._rate_limit")
    def test_get_open_orders_single_page(self, mock_rl, client):
        client.session.request.return_value = _mock_response(
            200, {"orders": [{"order_id": "o1"}, {"order_id": "o2"}], "cursor": ""})
        result = client.get_open_orders()
        assert len(result) == 2

    @patch("kalshi_api._rate_limit")
    def test_get_open_orders_pagination(self, mock_rl, client):
        page1 = _mock_response(200, {"orders": [{"order_id": "o1"}], "cursor": "xyz"})
        page2 = _mock_response(200, {"orders": [{"order_id": "o2"}], "cursor": ""})
        client.session.request.side_effect = [page1, page2]
        result = client.get_open_orders()
        assert len(result) == 2
        assert client.session.request.call_count == 2

    @patch("kalshi_api._rate_limit")
    def test_get_open_orders_always_raises_on_failure(self, mock_rl, client):
        """Unlike get_fills/get_positions, get_open_orders has no
        pre-existing caller relying on silent-empty — it always raises."""
        from kalshi_api import KalshiPortfolioQueryError
        client.session.request.return_value = _mock_response(500)
        with pytest.raises(KalshiPortfolioQueryError):
            client.get_open_orders()

    @patch("kalshi_api._rate_limit")
    def test_get_open_orders_filters_by_ticker_param(self, mock_rl, client):
        client.session.request.return_value = _mock_response(
            200, {"orders": [{"order_id": "o1"}], "cursor": ""})
        client.get_open_orders(ticker="KXTEST-26DEC31")
        params = client.session.request.call_args[1]["params"]
        assert params["ticker"] == "KXTEST-26DEC31"
        assert params["status"] == "resting"

    @patch("kalshi_api._rate_limit")
    def test_get_open_orders_raises_when_max_pages_exhausted_with_live_cursor(
            self, mock_rl, client):
        """Codex round-3 finding: every page fetch succeeds but the cursor
        is STILL non-empty after the last page allowed by max_pages — more
        resting orders genuinely exist beyond what was fetched.
        get_open_orders always raises on ambiguity (no raise_on_error
        flag) — this case must be no exception."""
        from kalshi_api import KalshiPortfolioQueryError
        page = _mock_response(200, {
            "orders": [{"order_id": "o1"}], "cursor": "always-more",
        })
        client.session.request.return_value = page
        with pytest.raises(KalshiPortfolioQueryError):
            client.get_open_orders(max_pages=3)
        assert client.session.request.call_count == 3


# ---------------------------------------------------------------------------
# TestKalshiFetchData
# ---------------------------------------------------------------------------

class TestKalshiFetchData:
    """Event/market/order-book fetching."""

    @patch("kalshi_api._rate_limit")
    def test_fetch_all_events_single_page(self, mock_rl, client):
        """Single page of events with no cursor returns all."""
        client.session.request.return_value = _mock_response(200, {
            "events": [{"event_ticker": "E1"}, {"event_ticker": "E2"}],
            "cursor": "",
        })
        result = client.fetch_all_events()
        assert len(result) == 2

    @patch("kalshi_api._rate_limit")
    def test_fetch_all_events_pagination(self, mock_rl, client):
        """Multiple pages are fetched until empty cursor."""
        page1 = _mock_response(200, {"events": [{"event_ticker": "E1"}], "cursor": "abc"})
        page2 = _mock_response(200, {"events": [{"event_ticker": "E2"}], "cursor": ""})
        client.session.request.side_effect = [page1, page2]
        result = client.fetch_all_events()
        assert len(result) == 2
        assert client.session.request.call_count == 2

    @patch("kalshi_api._rate_limit")
    def test_fetch_all_events_stops_on_error(self, mock_rl, client):
        """Stops pagination on non-200 response."""
        client.session.request.return_value = _mock_response(500)
        result = client.fetch_all_events()
        assert result == []

    @patch("kalshi_api._rate_limit")
    def test_fetch_all_events_passes_with_nested_markets_by_default(self, mock_rl, client):
        """Default fetch_all_events sends with_nested_markets=true."""
        client.session.request.return_value = _mock_response(200, {
            "events": [{"event_ticker": "E1", "markets": [{"ticker": "M1"}]}],
            "cursor": "",
        })
        client.fetch_all_events()
        sent_params = client.session.request.call_args.kwargs["params"]
        assert sent_params.get("with_nested_markets") == "true"

    @patch("kalshi_api._rate_limit")
    def test_fetch_all_events_omits_with_nested_when_disabled(self, mock_rl, client):
        """with_nested_markets=False omits the param entirely."""
        client.session.request.return_value = _mock_response(200, {
            "events": [{"event_ticker": "E1"}],
            "cursor": "",
        })
        client.fetch_all_events(with_nested_markets=False)
        sent_params = client.session.request.call_args.kwargs["params"]
        assert "with_nested_markets" not in sent_params

    @patch("kalshi_api._rate_limit")
    def test_fetch_markets_for_event_success(self, mock_rl, client):
        """Returns markets list for given event."""
        markets = [{"ticker": "M1"}, {"ticker": "M2"}]
        client.session.request.return_value = _mock_response(200, {"markets": markets})
        result = client.fetch_markets_for_event("EVT1")
        assert len(result) == 2

    @patch("kalshi_api._rate_limit")
    def test_fetch_markets_for_event_returns_empty_on_error(self, mock_rl, client):
        """Returns [] on non-200."""
        client.session.request.return_value = _mock_response(404)
        assert client.fetch_markets_for_event("EVT1") == []

    @patch("kalshi_api._rate_limit")
    def test_fetch_market_success(self, mock_rl, client):
        """Returns the unwrapped market dict on 200."""
        client.session.request.return_value = _mock_response(
            200, {"market": {"ticker": "TICK", "status": "settled", "result": "yes"}}
        )
        result = client.fetch_market("TICK")
        assert result == {"ticker": "TICK", "status": "settled", "result": "yes"}

    @patch("kalshi_api._rate_limit")
    def test_fetch_market_requests_correct_path(self, mock_rl, client):
        """Calls GET /markets/{ticker} (not the account-scoped settlements endpoint)."""
        client.session.request.return_value = _mock_response(200, {"market": {}})
        client.fetch_market("KXEARNINGSMENTIONBA-26JUL01")
        call_args = client.session.request.call_args
        assert call_args[0][0] == "GET"
        assert call_args[0][1] == KALSHI_BASE_URL + KALSHI_API_PATH + "/markets/KXEARNINGSMENTIONBA-26JUL01"

    @patch("kalshi_api._rate_limit")
    def test_fetch_market_returns_none_on_error(self, mock_rl, client):
        """Returns None on non-200 (e.g. 404 for an unknown ticker)."""
        client.session.request.return_value = _mock_response(404)
        assert client.fetch_market("NOPE") is None

    def test_fetch_market_returns_none_when_request_raises(self, client):
        client._request = MagicMock(side_effect=RuntimeError("transport failed"))
        assert client.fetch_market("NOPE") is None

    @patch("kalshi_api._rate_limit")
    def test_fetch_market_handles_unwrapped_response(self, mock_rl, client):
        """Some responses may not nest under 'market' — falls back to the raw dict."""
        client.session.request.return_value = _mock_response(
            200, {"ticker": "TICK", "status": "active"}
        )
        result = client.fetch_market("TICK")
        assert result == {"ticker": "TICK", "status": "active"}

    @patch("kalshi_api._rate_limit")
    def test_fetch_settled_markets_single_page(self, mock_rl, client):
        """Single page of settled markets with no cursor returns all."""
        client.session.request.return_value = _mock_response(200, {
            "markets": [{"ticker": "M1", "result": "yes"}, {"ticker": "M2", "result": "no"}],
            "cursor": "",
        })
        result = client.fetch_settled_markets(min_close_ts=1000)
        assert len(result) == 2

    @patch("kalshi_api._rate_limit")
    def test_fetch_settled_markets_pagination(self, mock_rl, client):
        """Multiple pages are fetched until an empty cursor."""
        page1 = _mock_response(200, {"markets": [{"ticker": "M1"}], "cursor": "abc"})
        page2 = _mock_response(200, {"markets": [{"ticker": "M2"}], "cursor": ""})
        client.session.request.side_effect = [page1, page2]
        result = client.fetch_settled_markets(min_close_ts=1000)
        assert len(result) == 2
        assert client.session.request.call_count == 2

    @patch("kalshi_api._rate_limit")
    def test_fetch_settled_markets_raises_on_request_failure(self, mock_rl, client):
        """A failed request (even the very first page) raises rather than
        silently returning a partial (here: empty) list as if it were
        complete -- a caller advancing a time watermark off a silently
        partial list could skip markets forever."""
        client.session.request.return_value = _mock_response(500)
        with pytest.raises(RuntimeError):
            client.fetch_settled_markets(min_close_ts=1000)

    def test_fetch_settled_markets_translates_request_exception(self, client):
        client._request = MagicMock(side_effect=RuntimeError("transport failed"))
        with pytest.raises(RuntimeError, match="0 markets fetched"):
            client.fetch_settled_markets(min_close_ts=1000)

    @patch("kalshi_api._rate_limit")
    def test_fetch_settled_markets_raises_on_mid_pagination_failure(self, mock_rl, client):
        """Page 1 succeeds (more data signaled via a live cursor); page 2
        fails -- must raise, not return page 1's markets as if complete."""
        page1 = _mock_response(200, {"markets": [{"ticker": "M1"}], "cursor": "abc"})
        page2 = _mock_response(500)
        client.session.request.side_effect = [page1, page2]
        with pytest.raises(RuntimeError):
            client.fetch_settled_markets(min_close_ts=1000)

    @patch("kalshi_api._rate_limit")
    def test_fetch_settled_markets_raises_when_page_budget_exhausted(self, mock_rl, client):
        """Every page returns a live cursor -- pagination never naturally
        terminates within max_pages, so this must raise rather than return
        a silently-truncated list."""
        page = _mock_response(200, {"markets": [{"ticker": "M1"}], "cursor": "still-more"})
        client.session.request.return_value = page
        with pytest.raises(RuntimeError):
            client.fetch_settled_markets(min_close_ts=1000, max_pages=3)
        assert client.session.request.call_count == 3

    @patch("kalshi_api._rate_limit")
    def test_fetch_settled_markets_continues_through_empty_live_cursor(self, mock_rl, client):
        page1 = _mock_response(200, {"markets": [], "cursor": "abc"})
        page2 = _mock_response(200, {"markets": [{"ticker": "M2"}], "cursor": ""})
        client.session.request.side_effect = [page1, page2]
        assert client.fetch_settled_markets(min_close_ts=1000) == [{"ticker": "M2"}]
        assert client.session.request.call_count == 2

    @patch("kalshi_api._rate_limit")
    def test_fetch_settled_markets_sends_status_and_min_close_ts(self, mock_rl, client):
        """Params include status=settled and the caller's min_close_ts watermark."""
        client.session.request.return_value = _mock_response(200, {"markets": [], "cursor": ""})
        client.fetch_settled_markets(min_close_ts=1735000000)
        sent_params = client.session.request.call_args.kwargs["params"]
        assert sent_params["status"] == "settled"
        assert sent_params["min_close_ts"] == 1735000000

    @patch("kalshi_api._rate_limit")
    def test_fetch_candlesticks_success(self, mock_rl, client):
        """Returns the unwrapped candlesticks list on 200."""
        candles = [{"end_period_ts": 1000, "price": {"close_dollars": "0.2200"}}]
        client.session.request.return_value = _mock_response(200, {"candlesticks": candles})
        result = client.fetch_candlesticks("KXEARNINGSMENTIONBA", "KXEARNINGSMENTIONBA-26Q2", 100, 200)
        assert result == candles

    @patch("kalshi_api._rate_limit")
    def test_fetch_candlesticks_requests_correct_path_and_params(self, mock_rl, client):
        """Calls GET /series/{series}/markets/{ticker}/candlesticks with the time window."""
        client.session.request.return_value = _mock_response(200, {"candlesticks": []})
        client.fetch_candlesticks("KXEARNINGSMENTIONBA", "KXEARNINGSMENTIONBA-26Q2", 100, 200, period_interval=60)
        call_args = client.session.request.call_args
        assert call_args[0][0] == "GET"
        assert call_args[0][1] == (
            KALSHI_BASE_URL + KALSHI_API_PATH
            + "/series/KXEARNINGSMENTIONBA/markets/KXEARNINGSMENTIONBA-26Q2/candlesticks"
        )
        sent_params = call_args.kwargs["params"]
        assert sent_params == {"start_ts": 100, "end_ts": 200, "period_interval": 60}

    @patch("kalshi_api._rate_limit")
    def test_fetch_candlesticks_returns_none_on_error(self, mock_rl, client):
        """Returns None (the failure sentinel) on non-200 (e.g. wrong series
        ticker -> 404) -- NOT [], which must mean "succeeded, no data"."""
        client.session.request.return_value = _mock_response(404)
        assert client.fetch_candlesticks("BADSERIES", "TICK", 100, 200) is None

    @patch("kalshi_api._rate_limit")
    def test_fetch_candlesticks_returns_empty_list_on_success_with_no_data(self, mock_rl, client):
        """A 200 OK with zero candles (e.g. the market didn't exist yet in
        this window) is a genuinely empty list, distinct from None -- a
        successful request that found nothing, not a failed request."""
        client.session.request.return_value = _mock_response(200, {"candlesticks": []})
        result = client.fetch_candlesticks("KXEARNINGSMENTIONBA", "TICK", 100, 200)
        assert result == []
        assert result is not None

    @patch("kalshi_api._rate_limit")
    def test_fetch_candlesticks_returns_none_on_connection_error_after_retries(self, mock_rl, client):
        """_request retries ConnectionError internally (tenacity) and
        re-raises once exhausted (reraise=True) -- fetch_candlesticks must
        catch that and convert it to the same None failure sentinel, not
        let it propagate uncaught and crash the whole OOS cycle."""
        import requests as _req
        client.session.request.side_effect = _req.ConnectionError("down")
        assert client.fetch_candlesticks("S", "TICK", 100, 200) is None

    @patch("kalshi_api._rate_limit")
    def test_fetch_candlesticks_returns_none_on_rate_limit_exhausted(self, mock_rl, client):
        """Repeated 429s exhaust tenacity's retries and re-raise
        _RateLimitError (reraise=True) -- also must convert to None, not
        propagate."""
        client.session.request.return_value = _mock_response(429)
        assert client.fetch_candlesticks("S", "TICK", 100, 200) is None

    @patch("kalshi_api._rate_limit")
    def test_fetch_order_book_success(self, mock_rl, client):
        """Returns parsed order-book JSON on 200."""
        book = {"orderbook": {"yes": [[55, 100]], "no": [[45, 200]]}}
        client.session.request.return_value = _mock_response(200, book)
        result = client.fetch_order_book("TICK")
        assert result == book

    @patch("kalshi_api._rate_limit")
    def test_fetch_order_book_returns_none_on_error(self, mock_rl, client):
        """Returns None on non-200."""
        client.session.request.return_value = _mock_response(500)
        assert client.fetch_order_book("TICK") is None


# ---------------------------------------------------------------------------
# TestKalshiOrderBookDepth
# ---------------------------------------------------------------------------

class TestKalshiOrderBookDepth:
    """get_order_book_depth derives ask sizes from the inverse-side bids.

    Kalshi orderbooks contain BIDS only:
      - yes_bids = bids on YES (someone wants to BUY YES)
      - no_bids  = bids on NO (someone wants to BUY NO)
    A NO bid at $0.45 is equivalent to a YES ask at $0.55 with the same size,
    so yes_ask_size = best_no_bid.size and vice versa.
    Sort order: ASCENDING; best bid is entries[-1].
    """

    def test_list_entries_legacy_cents(self, client):
        """Legacy schema (cents-int): yes_ask_size derives from no_bids[-1]."""
        # NO bid at 45¢ (size 80) -> YES ask = 1 - 0.45 = $0.55, depth 80
        # YES bid at 55¢ (size 120) -> NO ask = $0.45, depth 120
        book = {"orderbook": {"yes": [[55, 120]], "no": [[45, 80]]}}
        with patch.object(client, "fetch_order_book", return_value=book):
            result = client.get_order_book_depth("TICK")
        assert result["yes_ask_size"] == 80
        assert result["no_ask_size"] == 120

    def test_dict_entries_quantity_key(self, client):
        book = {"orderbook": {"yes": [{"price": 55, "quantity": 50}], "no": [{"price": 45, "quantity": 30}]}}
        with patch.object(client, "fetch_order_book", return_value=book):
            result = client.get_order_book_depth("TICK")
        assert result["yes_ask_size"] == 30   # from no_bid depth
        assert result["no_ask_size"] == 50    # from yes_bid depth

    def test_dict_entries_size_key(self, client):
        book = {"orderbook": {"yes": [{"price": 55, "size": 25}], "no": [{"price": 45, "size": 15}]}}
        with patch.object(client, "fetch_order_book", return_value=book):
            result = client.get_order_book_depth("TICK")
        assert result["yes_ask_size"] == 15
        assert result["no_ask_size"] == 25

    def test_empty_book(self, client):
        """Empty order book sides return 0 sizes."""
        book = {"orderbook": {"yes": [], "no": []}}
        with patch.object(client, "fetch_order_book", return_value=book):
            result = client.get_order_book_depth("TICK")
        assert result["yes_ask_size"] == 0
        assert result["no_ask_size"] == 0

    def test_returns_none_when_no_book(self, client):
        with patch.object(client, "fetch_order_book", return_value=None):
            result = client.get_order_book_depth("TICK")
        assert result is None

    def test_top_level_keys_no_orderbook_wrapper(self, client):
        """Handles book where yes/no are at top level (no 'orderbook' key)."""
        book = {"yes": [[60, 200]], "no": [[40, 150]]}
        with patch.object(client, "fetch_order_book", return_value=book):
            result = client.get_order_book_depth("TICK")
        # yes_ask_size = no_bid depth (150), no_ask_size = yes_bid depth (200)
        assert result["yes_ask_size"] == 150
        assert result["no_ask_size"] == 200

    def test_current_api_schema_dollars(self, client):
        """Current Kalshi schema: orderbook_fp with dollar strings."""
        book = {"orderbook_fp": {
            "yes_dollars": [["0.4500", "100.00"], ["0.5500", "120.00"]],
            "no_dollars":  [["0.3000", "50.00"],  ["0.4500", "80.00"]],
        }}
        with patch.object(client, "fetch_order_book", return_value=book):
            result = client.get_order_book_depth("TICK")
        # Ascending sort, best=last. yes_ask_size from best NO bid (size 80).
        assert result["yes_ask_size"] == 80
        assert result["no_ask_size"] == 120


class TestParseOrderbook:
    """parse_orderbook handles current and legacy schemas, returns ascending-sorted floats."""

    def test_current_schema_dollar_strings(self):
        from kalshi_api import parse_orderbook
        book = {"orderbook_fp": {
            "yes_dollars": [["0.0100", "5192.00"]],
            "no_dollars":  [["0.0100", "33348.00"], ["0.9900", "9999.00"]],
        }}
        parsed = parse_orderbook(book)
        assert parsed["yes_bids"] == [(0.01, 5192.0)]
        assert parsed["no_bids"] == [(0.01, 33348.0), (0.99, 9999.0)]

    def test_legacy_schema_cents(self):
        from kalshi_api import parse_orderbook
        book = {"orderbook": {"yes": [[55, 120]], "no": [[45, 80]]}}
        parsed = parse_orderbook(book)
        assert parsed["yes_bids"] == [(0.55, 120.0)]
        assert parsed["no_bids"] == [(0.45, 80.0)]

    def test_none_input(self):
        from kalshi_api import parse_orderbook
        assert parse_orderbook(None) == {"yes_bids": [], "no_bids": []}

    def test_empty_book(self):
        from kalshi_api import parse_orderbook
        assert parse_orderbook({}) == {"yes_bids": [], "no_bids": []}

    def test_real_btc_fixture_round_trip(self):
        """Validate against the actual API response captured 2026-04-26."""
        from pathlib import Path
        import json
        from kalshi_api import parse_orderbook, best_yes_bid, best_no_bid, best_yes_ask, best_no_ask
        fixture_path = Path(__file__).resolve().parent / "fixtures" / "kalshi_orderbook_two_sided.json"
        sample = json.loads(fixture_path.read_text())
        parsed = parse_orderbook(sample["response"])
        # Real data: 1 YES bid at $0.01, 26 NO bids ascending from $0.01 to $0.96
        assert len(parsed["yes_bids"]) == 1
        assert len(parsed["no_bids"]) == 26
        # Sort order: ascending, best=last
        prices = [p for p, _ in parsed["no_bids"]]
        assert prices == sorted(prices), "no_bids must be sorted ascending"
        # best_no_bid = (0.96, ...); best_yes_ask = 1 - 0.96 = 0.04
        nb = best_no_bid(parsed)
        assert nb is not None and nb[0] == pytest.approx(0.96)
        ya = best_yes_ask(parsed)
        assert ya is not None and ya[0] == pytest.approx(0.04)


class TestBestAskBidHelpers:
    def test_best_yes_ask_when_no_no_bids(self):
        from kalshi_api import best_yes_ask
        assert best_yes_ask({"yes_bids": [(0.5, 100)], "no_bids": []}) is None

    def test_best_no_ask_when_no_yes_bids(self):
        from kalshi_api import best_no_ask
        assert best_no_ask({"yes_bids": [], "no_bids": [(0.5, 100)]}) is None

    def test_best_bid_returns_last_element(self):
        from kalshi_api import best_yes_bid
        bids = [(0.10, 5.0), (0.20, 10.0), (0.30, 15.0)]
        assert best_yes_bid({"yes_bids": bids, "no_bids": []}) == (0.30, 15.0)

    def test_yes_ask_inverts_no_bid_price(self):
        from kalshi_api import best_yes_ask
        result = best_yes_ask({"yes_bids": [], "no_bids": [(0.40, 100.0)]})
        assert result[0] == pytest.approx(0.60)
        assert result[1] == 100.0


# ---------------------------------------------------------------------------
# TestFetchIncentivePrograms
# ---------------------------------------------------------------------------

class TestFetchIncentivePrograms:
    """LIP pool list via GET /incentive_programs (verified live 2026-06-11)."""

    def _client(self):
        c = KalshiClient()
        c.authenticated = True
        return c

    def test_normalizes_period_reward_to_dollars(self):
        c = self._client()
        page = {"incentive_programs": [
            {"market_ticker": "KXCPI-26JUN", "period_reward": 1150000,
             "discount_factor_bps": 5000},
        ], "next_cursor": None}
        with patch.object(c, "_request", return_value=_mock_response(200, page)):
            progs = c.fetch_incentive_programs()
        assert len(progs) == 1
        assert progs[0]["period_reward_dollars"] == pytest.approx(115.0)

    def test_paginates_until_cursor_exhausted(self):
        c = self._client()
        p1 = {"incentive_programs": [{"market_ticker": "A", "period_reward": 400000}],
              "next_cursor": "abc"}
        p2 = {"incentive_programs": [{"market_ticker": "B", "period_reward": 100000}],
              "next_cursor": None}
        with patch.object(c, "_request",
                          side_effect=[_mock_response(200, p1), _mock_response(200, p2)]) as req:
            progs = c.fetch_incentive_programs()
        assert [p["market_ticker"] for p in progs] == ["A", "B"]
        assert req.call_count == 2
        # Second call must carry the cursor
        assert req.call_args_list[1][1]["params"]["cursor"] == "abc"

    def test_default_cap_covers_current_program_volume(self):
        """Current active LIP inventory exceeds the old 20-page cap."""
        c = self._client()
        pages = []
        for index in range(22):
            pages.append(_mock_response(200, {
                "incentive_programs": [{
                    "market_ticker": f"M{index}",
                    "period_reward": 10000,
                }],
                "next_cursor": f"cursor-{index}" if index < 21 else None,
            }))
        with patch.object(c, "_request", side_effect=pages) as req:
            progs = c.fetch_incentive_programs()
        assert len(progs) == 22
        assert req.call_count == 22

    def test_passes_status_and_type_filters(self):
        c = self._client()
        page = {"incentive_programs": [], "next_cursor": None}
        with patch.object(c, "_request", return_value=_mock_response(200, page)) as req:
            c.fetch_incentive_programs(status="active", incentive_type="liquidity")
        params = req.call_args[1]["params"]
        assert params["status"] == "active"
        assert params["type"] == "liquidity"

    def test_returns_empty_on_failure(self):
        c = self._client()
        with patch.object(c, "_request", return_value=_mock_response(500)):
            assert c.fetch_incentive_programs() == []
        with patch.object(c, "_request", return_value=None):
            assert c.fetch_incentive_programs() == []

    def test_discards_partial_results_on_later_page_failure(self):
        c = self._client()
        first = {
            "incentive_programs": [{"market_ticker": "A", "period_reward": 400000}],
            "next_cursor": "abc",
        }
        with patch.object(c, "_request", side_effect=[_mock_response(200, first), None]):
            assert c.fetch_incentive_programs() == []

    def test_discards_partial_results_when_page_cap_exhausted(self):
        c = self._client()
        page = {
            "incentive_programs": [{"market_ticker": "A", "period_reward": 400000}],
            "next_cursor": "still-more",
        }
        with patch.object(c, "_request", return_value=_mock_response(200, page)):
            assert c.fetch_incentive_programs(max_pages=1) == []

    def test_missing_period_reward_defaults_zero(self):
        c = self._client()
        page = {"incentive_programs": [{"market_ticker": "X"}], "next_cursor": None}
        with patch.object(c, "_request", return_value=_mock_response(200, page)):
            progs = c.fetch_incentive_programs()
        assert progs[0]["period_reward_dollars"] == 0.0
