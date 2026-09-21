"""Tests for limitless_api.py — Limitless Predictions API client."""

import os
import sys
from unittest.mock import MagicMock, patch

import pytest
import requests

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import limitless_api
from limitless_api import LimitlessClient

TEST_PRIVATE_KEY = "0x" + "1" * 64
TEST_ADDRESS = "0x19E7E376E7C213B7E7e7e46cc70A5dD086DAff2A"


@pytest.fixture(autouse=True)
def reset_limitless_circuit_breaker():
    """Reset limitless circuit breaker state between tests to prevent bleed."""
    limitless_api._circuit.record_success()
    yield
    limitless_api._circuit.record_success()


@pytest.fixture
def unauthed_client():
    return LimitlessClient()


@pytest.fixture
def authed_client():
    c = LimitlessClient()
    c.dry_run = False
    c.login(api_key="test-api-key", private_key=TEST_PRIVATE_KEY)
    return c


# ---------------------------------------------------------------------------
# Auth Tests
# ---------------------------------------------------------------------------


class TestLimitlessAuth:
    def test_login_fails_without_api_key(self, unauthed_client):
        with patch.dict(os.environ, {}, clear=True):
            result = unauthed_client.login(None, None)
        assert result is False
        assert unauthed_client.authenticated is False

    def test_login_succeeds_with_api_key(self, unauthed_client):
        result = unauthed_client.login(api_key="test-key")
        assert result is True
        assert unauthed_client.authenticated is True
        assert unauthed_client.session.headers.get("X-API-KEY") == "test-key"

    def test_login_with_private_key_derives_address(self, unauthed_client):
        result = unauthed_client.login(api_key="test-key", private_key=TEST_PRIVATE_KEY)
        assert result is True
        assert unauthed_client.authenticated is True
        assert unauthed_client._account_address.lower() == TEST_ADDRESS.lower()

    def test_login_fails_with_invalid_private_key(self, unauthed_client):
        result = unauthed_client.login(api_key="test-key", private_key="invalid-key")
        assert result is False
        assert unauthed_client.authenticated is False


# ---------------------------------------------------------------------------
# Market & Book Tests
# ---------------------------------------------------------------------------


class TestLimitlessMarkets:
    def test_fetch_all_markets_normalizes_data(self, authed_client):
        raw_response = {
            "markets": [
                {
                    "id": "mkt-123",
                    "title": "Will Bitcoin break 100k?",
                    "category": "Crypto",
                    "status": "active",
                    "outcomes": [
                        {"price": 0.65, "name": "Yes"},
                        {"price": 0.35, "name": "No"},
                    ],
                    "volumeUsd": 125000.0,
                    "rewardProgram": {
                        "pool_size_usdc": 500.0,
                        "min_incentive_size": 10.0,
                        "max_incentive_spread": 0.04,
                    },
                }
            ]
        }
        with patch.object(authed_client, "_public_request", return_value=raw_response):
            markets = authed_client.fetch_all_markets()

        assert len(markets) == 1
        m = markets[0]
        assert m["id"] == "mkt-123"
        assert m["title"] == "Will Bitcoin break 100k?"
        assert m["platform"] == "limitless"
        assert m["yes_price"] == 0.65
        assert m["no_price"] == 0.35
        assert m["volume"] == 125000.0
        assert m["reward_pool_usdc"] == 500.0

    def test_fetch_all_markets_uses_cache(self, authed_client):
        with patch.object(authed_client, "_public_request", return_value={"markets": []}) as mock_req:
            authed_client.fetch_all_markets()
            authed_client.fetch_all_markets()
            assert mock_req.call_count == 1

    def test_get_order_book_parses_bids_asks(self, authed_client):
        raw_book = {
            "bids": [{"price": 0.58, "amount": 100.0}],
            "asks": [{"price": 0.62, "amount": 150.0}],
        }
        with patch.object(authed_client, "_public_request", return_value=raw_book):
            book = authed_client.get_order_book("mkt-123")

        assert book is not None
        assert len(book["bids"]) == 1
        assert book["bids"][0]["price"] == 0.58
        assert book["bids"][0]["amount"] == 100.0
        assert len(book["asks"]) == 1
        assert book["asks"][0]["price"] == 0.62

    def test_get_reward_program_parses_fields(self, authed_client):
        raw_rewards = {
            "min_incentive_size": 5.0,
            "max_incentive_spread": 0.03,
            "pool_size_usdc": 250.0,
            "active": True,
        }
        with patch.object(authed_client, "_public_request", return_value=raw_rewards):
            prog = authed_client.get_reward_program("mkt-123")

        assert prog is not None
        assert prog["min_incentive_size"] == 5.0
        assert prog["max_incentive_spread"] == 0.03
        assert prog["pool_size_usdc"] == 250.0
        assert prog["active"] is True


# ---------------------------------------------------------------------------
# Order Placement & Cancellation
# ---------------------------------------------------------------------------


class TestLimitlessOrders:
    def test_dry_run_generates_synthetic_order(self, unauthed_client):
        unauthed_client.dry_run = True
        resp = unauthed_client.place_order(
            market_id="mkt-999",
            side="buy",
            outcome="yes",
            quantity=10,
            price=0.50,
        )
        assert resp is not None
        assert resp["dry_run"] is True
        assert resp["order_id"].startswith("dry_limitless_mkt-999_buy_")
        assert resp["status"] == "resting"

    def test_live_order_without_private_key_returns_none(self):
        c = LimitlessClient()
        c.dry_run = False
        c.login(api_key="valid-key", private_key=None)
        assert c.authenticated is True
        resp = c.place_order("mkt-1", "buy", "yes", 10, 0.50)
        assert resp is None

    def test_live_order_eip712_signs_and_posts(self, authed_client):
        mock_post = MagicMock(return_value={"order_id": "ord-live-001", "status": "open"})
        with patch.object(authed_client, "_private_request", mock_post):
            resp = authed_client.place_order(
                market_id="mkt-123",
                side="buy",
                outcome="yes",
                quantity=20,
                price=0.55,
                time_in_force="gtc",
            )

        assert resp == {"order_id": "ord-live-001", "status": "open"}
        assert mock_post.call_count == 1
        call_args = mock_post.call_args
        endpoint = call_args[0][0]
        payload = call_args[1]["payload_data"]

        assert endpoint == "/orders"
        assert "order" in payload
        assert "signature" in payload
        assert payload["signature"].startswith("0x")
        assert len(payload["signature"]) == 132  # 65 bytes in hex + 0x

        # Check order struct values
        order = payload["order"]
        assert order["marketId"] == "mkt-123"
        assert order["side"] == 0  # buy
        assert order["outcome"] == 0  # yes
        assert order["price"] == 550000  # 0.55 * 1e6
        assert order["quantity"] == 20000000  # 20 * 1e6

    def test_cancel_dry_run_order_succeeds(self, unauthed_client):
        assert unauthed_client.cancel_order("dry_limitless_123") is True

    def test_cancel_live_order_calls_delete(self, authed_client):
        with patch.object(authed_client, "_private_request", return_value={"success": True}) as mock_del:
            assert authed_client.cancel_order("ord-123") is True
            mock_del.assert_called_once_with("/orders/ord-123", method="DELETE")

    def test_get_balance_returns_float(self, authed_client):
        with patch.object(authed_client, "_private_request", return_value={"balance": "1250.50"}):
            bal = authed_client.get_balance()
            assert bal == 1250.50


# ---------------------------------------------------------------------------
# Circuit Breaker Protection
# ---------------------------------------------------------------------------


class TestLimitlessCircuitBreaker:
    def test_circuit_trips_after_failures(self, authed_client):
        # 3 consecutive network exceptions
        with patch.object(authed_client.session, "get", side_effect=requests.RequestException("boom")):
            for _ in range(3):
                authed_client._public_request("/markets")

        assert limitless_api._circuit.is_open() is True
        # Further calls should be rejected without calling session.get
        with patch.object(authed_client.session, "get") as mock_get:
            res = authed_client._public_request("/markets")
            assert res is None
            mock_get.assert_not_called()
