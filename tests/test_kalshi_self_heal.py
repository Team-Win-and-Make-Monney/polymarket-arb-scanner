"""Tests for the Kalshi auth self-heal path (boot retry + runtime re-auth).

Covers the 2026-07-23 incident class: a deploy that boots during Kalshi's
daily maintenance window must not permanently degrade the run.
"""

import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest
from unittest.mock import MagicMock, patch

import kalshi_api
from kalshi_api import build_client_from_env, kalshi_creds_configured


_ENV_VARS = ("KALSHI_API_KEY_ID", "KALSHI_PRIVATE_KEY_PATH", "KALSHI_PRIVATE_KEY_BASE64")


class TestKalshiCredsConfigured:
    @pytest.fixture(autouse=True)
    def clean_env(self, monkeypatch):
        for var in _ENV_VARS:
            monkeypatch.delenv(var, raising=False)

    def test_false_when_env_empty(self):
        assert kalshi_creds_configured() is False

    def test_false_with_key_id_only(self, monkeypatch):
        monkeypatch.setenv("KALSHI_API_KEY_ID", "kid")
        assert kalshi_creds_configured() is False

    def test_false_with_key_material_but_no_id(self, monkeypatch):
        monkeypatch.setenv("KALSHI_PRIVATE_KEY_BASE64", "blob")
        assert kalshi_creds_configured() is False

    def test_true_with_base64(self, monkeypatch):
        monkeypatch.setenv("KALSHI_API_KEY_ID", "kid")
        monkeypatch.setenv("KALSHI_PRIVATE_KEY_BASE64", "blob")
        assert kalshi_creds_configured() is True

    def test_true_with_path(self, monkeypatch):
        monkeypatch.setenv("KALSHI_API_KEY_ID", "kid")
        monkeypatch.setenv("KALSHI_PRIVATE_KEY_PATH", "/tmp/key.pem")
        assert kalshi_creds_configured() is True


class TestBuildClientFromEnv:
    @pytest.fixture(autouse=True)
    def env(self, monkeypatch):
        for var in _ENV_VARS:
            monkeypatch.delenv(var, raising=False)
        monkeypatch.setenv("KALSHI_API_KEY_ID", "kid")
        monkeypatch.setenv("KALSHI_PRIVATE_KEY_BASE64", "b64blob")

    def test_returns_none_without_creds(self, monkeypatch):
        monkeypatch.delenv("KALSHI_API_KEY_ID")
        with patch.object(kalshi_api.KalshiClient, "login_with_api_key") as login:
            assert build_client_from_env() is None
            login.assert_not_called()

    def test_returns_client_on_success(self):
        with patch.object(kalshi_api.KalshiClient, "login_with_api_key",
                          return_value=True) as login:
            client = build_client_from_env()
        assert isinstance(client, kalshi_api.KalshiClient)
        login.assert_called_once_with("kid", private_key_base64="b64blob")

    def test_returns_none_on_failure(self):
        with patch.object(kalshi_api.KalshiClient, "login_with_api_key",
                          return_value=False):
            assert build_client_from_env() is None

    def test_uses_expanded_path_when_no_base64(self, monkeypatch):
        monkeypatch.delenv("KALSHI_PRIVATE_KEY_BASE64")
        monkeypatch.setenv("KALSHI_PRIVATE_KEY_PATH", "~/keys/kalshi.pem")
        with patch.object(kalshi_api.KalshiClient, "login_with_api_key",
                          return_value=True) as login:
            client = build_client_from_env()
        assert client is not None
        _, kwargs = login.call_args
        assert kwargs["private_key_path"] == os.path.expanduser("~/keys/kalshi.pem")

    def test_retries_until_success(self):
        with patch.object(kalshi_api.KalshiClient, "login_with_api_key",
                          side_effect=[False, True]) as login, \
                patch.object(kalshi_api.time, "sleep") as sleep:
            client = build_client_from_env(attempts=3, retry_wait=20.0)
        assert client is not None
        assert login.call_count == 2
        sleep.assert_called_once_with(20.0)

    def test_exhausts_attempts_and_returns_none(self):
        with patch.object(kalshi_api.KalshiClient, "login_with_api_key",
                          side_effect=[False, False, False]) as login, \
                patch.object(kalshi_api.time, "sleep") as sleep:
            assert build_client_from_env(attempts=3, retry_wait=5.0) is None
        assert login.call_count == 3
        assert sleep.call_count == 2

    def test_fresh_client_per_attempt(self):
        # A verify-ping failure must not leave a stale half-initialized client
        # reused across attempts.
        instances = []
        real_init = kalshi_api.KalshiClient.__init__

        def tracking_init(self, *a, **kw):
            real_init(self, *a, **kw)
            instances.append(self)

        with patch.object(kalshi_api.KalshiClient, "__init__", tracking_init), \
                patch.object(kalshi_api.KalshiClient, "login_with_api_key",
                             side_effect=[False, True]), \
                patch.object(kalshi_api.time, "sleep"):
            client = build_client_from_env(attempts=2, retry_wait=0.0)
        assert client is instances[-1]
        assert len(instances) == 2


class TestHealKalshiClient:
    """continuous.heal_kalshi_client rewires all dependents on success."""

    @pytest.fixture(autouse=True)
    def _import_continuous(self):
        import continuous
        self.continuous = continuous

    def test_failure_returns_none_and_touches_nothing(self):
        platform_clients = {}
        with patch.object(self.continuous, "build_client_from_env",
                          return_value=None):
            result = self.continuous.heal_kalshi_client(
                MagicMock(), platform_clients, MagicMock(), MagicMock())
        assert result is None
        assert "kalshi" not in platform_clients

    def test_success_rewires_executor_platform_map_and_hedger(self):
        healed = MagicMock(name="healed_client")
        executor = MagicMock()
        hedger = MagicMock()
        notifier = MagicMock()
        platform_clients = {}
        with patch.object(self.continuous, "build_client_from_env",
                          return_value=healed):
            result = self.continuous.heal_kalshi_client(
                executor, platform_clients, hedger, notifier)
        assert result is healed
        assert executor.kalshi_client is healed
        assert platform_clients["kalshi"] is healed
        assert hedger.kalshi_client is healed
        notifier.notify_text.assert_called_once()

    def test_success_without_hedger_or_notifier(self):
        healed = MagicMock(name="healed_client")
        with patch.object(self.continuous, "build_client_from_env",
                          return_value=healed):
            result = self.continuous.heal_kalshi_client(
                MagicMock(), {}, None, None)
        assert result is healed

    def test_notifier_exception_does_not_block_heal(self):
        healed = MagicMock(name="healed_client")
        notifier = MagicMock()
        notifier.notify_text.side_effect = RuntimeError("webhook down")
        with patch.object(self.continuous, "build_client_from_env",
                          return_value=healed):
            result = self.continuous.heal_kalshi_client(
                MagicMock(), {}, None, notifier)
        assert result is healed


class TestStartKalshiFeedLate:
    def _manager(self):
        import ws_feeds
        fm = ws_feeds.FeedManager(on_price_update=lambda *a, **kw: None)
        fm._running = True
        fm._kalshi_tickers = ["KXTEST-26"]
        fm.kalshi_api_key_id = "kid"
        fm.kalshi_private_key = object()
        return fm, ws_feeds

    def test_starts_once_then_idempotent(self):
        fm, ws_feeds = self._manager()
        fm._pending_kalshi_subs = list(fm._kalshi_tickers)
        with patch.object(ws_feeds.asyncio, "create_task") as create_task:
            assert fm.start_kalshi_feed_late() is True
            assert fm.start_kalshi_feed_late() is False
        assert create_task.call_count == 1
        # Initial connect subscribes everything in _kalshi_tickers; queued
        # pending subs must be dropped or the same tickers get subscribed twice.
        assert fm._pending_kalshi_subs == []
        create_task.call_args[0][0].close()  # avoid un-awaited coroutine warning

    def test_requires_running_loop_flag(self):
        fm, ws_feeds = self._manager()
        fm._running = False
        with patch.object(ws_feeds.asyncio, "create_task") as create_task:
            assert fm.start_kalshi_feed_late() is False
        create_task.assert_not_called()

    def test_requires_tickers_and_creds(self):
        fm, ws_feeds = self._manager()
        fm._kalshi_tickers = []
        assert fm.start_kalshi_feed_late() is False
        fm._kalshi_tickers = ["KXTEST-26"]
        fm.kalshi_private_key = None
        assert fm.start_kalshi_feed_late() is False

    def test_noop_when_run_already_started_kalshi(self):
        fm, ws_feeds = self._manager()
        fm._kalshi_task_started = True
        with patch.object(ws_feeds.asyncio, "create_task") as create_task:
            assert fm.start_kalshi_feed_late() is False
        create_task.assert_not_called()
