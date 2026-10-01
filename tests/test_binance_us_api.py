"""Boundary, authentication, and data-quality tests for Binance.US reporting."""

import hashlib
import hmac
import os
from pathlib import Path
import subprocess
import sys
from unittest.mock import Mock, patch

import pytest
import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import binance_us_api as api


class TestBinanceUSReadOnly:
    @pytest.fixture
    def http_get(self):
        with patch.object(api.requests, "get") as http_get:
            yield http_get

    def response(self, payload, status=200):
        return Mock(status_code=status, json=Mock(return_value=payload))

    def test_only_time_and_signed_account_get_and_no_uid(self, http_get):
        http_get.side_effect = [
            self.response({"serverTime": 1790870400000}),
            self.response({"uid": 999, "canTrade": True, "balances": [
                {"asset": "USD", "free": "1900.00000000", "locked": "100.00000000"},
                {"asset": "BTC", "free": "0.00000001", "locked": "0"},
                {"asset": "ETH", "free": "0", "locked": "0"},
            ]}),
        ]
        snapshot = api.account_snapshot("k" * 64, "s" * 64)
        first, second = http_get.call_args_list
        assert first.args == ("https://api.binance.us/api/v3/time",)
        assert first.kwargs["headers"] == {}
        assert second.args == ("https://api.binance.us/api/v3/account",)
        assert second.kwargs["headers"] == {"X-MBX-APIKEY": "k" * 64}
        query = "timestamp=1790870400000&recvWindow=5000"
        expected = hmac.new(b"s" * 64, query.encode(), hashlib.sha256).hexdigest()
        assert second.kwargs["params"] == f"{query}&signature={expected}"
        assert all(call.kwargs["allow_redirects"] is False for call in http_get.call_args_list)
        assert snapshot["balances"] == [
            {"asset": "USD", "free": "1900.00000000", "locked": "100.00000000"},
            {"asset": "BTC", "free": "0.00000001", "locked": "0"},
        ]
        assert "uid" not in snapshot
        assert snapshot["account_flags"]["canTrade"] is True
        assert snapshot["account_flags"]["canWithdraw"] is None
        assert snapshot["promotion_eligibility"] == "not_available_from_account_endpoint"

    @pytest.mark.parametrize("path", ["/api/v3/order", "/sapi/v1/capital/withdraw/apply", "https://example.com"])
    def test_disallowed_route_never_reaches_network(self, http_get, path):
        with pytest.raises(api.BinanceUSReadError, match="allowlist"):
            api._get_json(path)
        http_get.assert_not_called()

    @pytest.mark.parametrize("secret", ["", "short", "s" * 63, "s" * 64 + "\n"])
    def test_incomplete_auth_sends_nothing(self, http_get, secret):
        with pytest.raises(api.BinanceUSReadError, match="pair"):
            api.account_snapshot("k" * 64, secret)
        http_get.assert_not_called()

    @pytest.mark.parametrize("status", [301, 302, 307, 308])
    def test_redirect_refused(self, http_get, status):
        http_get.return_value = self.response({}, status)
        with pytest.raises(api.BinanceUSReadError, match="redirect refused"):
            api.server_time()
        http_get.return_value.close.assert_called_once()
        assert http_get.call_count == 1

    @pytest.mark.parametrize("status,code", [(401, -2015), (429, -1003), (418, -1003), (200, -1022)])
    def test_api_error_is_sanitized_and_not_retried(self, http_get, status, code):
        http_get.return_value = self.response({"code": code, "msg": "PRIVATE_SECRET_AND_SIGNED_URL"}, status)
        with pytest.raises(api.BinanceUSReadError) as error:
            api.server_time()
        assert str(code) in str(error.value)
        assert "PRIVATE" not in str(error.value)
        assert http_get.call_count == 1

    def test_network_error_does_not_echo_signed_url(self, http_get):
        http_get.side_effect = requests.ConnectionError("https://api.binance.us/?signature=PRIVATE")
        with pytest.raises(api.BinanceUSReadError) as error:
            api.server_time()
        assert "PRIVATE" not in str(error.value)
        assert error.value.__suppress_context__ is True

    @pytest.mark.parametrize("payload", [{}, {"serverTime": True}, {"serverTime": "123"}, {"serverTime": -1}])
    def test_bad_time_prevents_account_request(self, http_get, payload):
        http_get.return_value = self.response(payload)
        with pytest.raises(api.BinanceUSReadError, match="server time"):
            api.account_snapshot("k" * 64, "s" * 64)
        assert http_get.call_count == 1

    @pytest.mark.parametrize("value", ["NaN", "Infinity", "-1", "1e99999", 1, None])
    def test_invalid_balances_are_not_reported_as_zero(self, value):
        with pytest.raises(api.BinanceUSReadError, match="balance row"):
            api._balances({"balances": [{"asset": "USD", "free": value, "locked": "0"}]})

    def test_missing_balances_and_duplicate_assets_fail(self):
        row = {"asset": "USD", "free": "1", "locked": "0"}
        for payload in [{}, {"balances": None}, {"balances": [row, row]}]:
            with pytest.raises(api.BinanceUSReadError):
                api._balances(payload)

    def test_environment_pair_does_not_fall_back_to_keychain(self):
        with patch.dict(os.environ, {"BINANCE_US_API_KEY": "k" * 64}, clear=True), patch.object(api.subprocess, "run") as run:
            with pytest.raises(api.BinanceUSReadError):
                api.load_auth("env")
            run.assert_not_called()

    def test_complete_environment_pair(self):
        with patch.dict(os.environ, {"BINANCE_US_API_KEY": "k" * 64, "BINANCE_US_API_SECRET": "s" * 64}, clear=True):
            assert api.load_auth("env") == ("k" * 64, "s" * 64)

    def test_keychain_pair_read_without_values_in_command(self):
        with patch.object(api.subprocess, "run", side_effect=[
            subprocess.CompletedProcess([], 0, b"k" * 64 + b"\n"),
            subprocess.CompletedProcess([], 0, b"s" * 64 + b"\n"),
        ]) as run:
            assert api.load_auth("keychain") == ("k" * 64, "s" * 64)
            assert "k" * 64 not in str(run.call_args_list)
            assert "s" * 64 not in str(run.call_args_list)

    def test_missing_keychain_secret_stops_before_network(self, http_get, capsys):
        with patch.object(api.subprocess, "run", side_effect=[
            subprocess.CompletedProcess([], 0, b"k" * 64 + b"\n"),
            subprocess.CompletedProcess([], 44, b"", b"PRIVATE"),
        ]):
            assert api.main(["--source", "keychain"]) == 1
        http_get.assert_not_called()
        assert "PRIVATE" not in capsys.readouterr().err

    def test_public_check_does_not_load_auth(self, http_get):
        http_get.return_value = self.response({"serverTime": 1790870400000})
        with patch.object(api, "load_auth") as auth:
            assert api.main(["--public-check"]) == 0
            auth.assert_not_called()
