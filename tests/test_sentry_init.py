"""Tests for sentry_init.py — credential-safety of the Sentry integration.

Venue credentials (API keys, private keys) are in scope during request
signing. If Sentry captured exception-frame locals, a signing error would
ship those credentials off-box. These tests pin the fail-closed init kwargs
and the before_send scrubber.
"""

import sys
import os
import types
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# Stub sentry_sdk before the module-scope sentry_init import (repo
# convention: external SDKs are stubbed via sys.modules so the suite never
# depends on the real dependency being installed).
_sentry_stub = types.ModuleType("sentry_sdk")
_sentry_stub.init = lambda **kwargs: None
sys.modules["sentry_sdk"] = _sentry_stub

import sentry_init  # noqa: E402


class TestInitKwargs:
    def _init_kwargs(self, monkeypatch):
        monkeypatch.setenv("SENTRY_DSN", "https://key@example.ingest.sentry.io/1")
        # init_sentry() short-circuits under pytest; remove the guard var so
        # we can exercise the real init path against a mocked sentry_sdk.
        monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
        with patch.object(sentry_init.sentry_sdk, "init") as mock_init:
            sentry_init.init_sentry()
        assert mock_init.call_count == 1
        return mock_init.call_args.kwargs

    def test_local_variables_never_captured(self, monkeypatch):
        """include_local_variables must be explicitly False — the SDK default
        (True) ships exception-frame locals, including venue creds in scope
        during signing."""
        kwargs = self._init_kwargs(monkeypatch)
        assert kwargs.get("include_local_variables") is False

    def test_before_send_scrubber_installed(self, monkeypatch):
        kwargs = self._init_kwargs(monkeypatch)
        assert kwargs.get("before_send") is sentry_init._scrub_event

    def test_pii_disabled(self, monkeypatch):
        kwargs = self._init_kwargs(monkeypatch)
        assert kwargs.get("send_default_pii") is False

    def test_release_prefers_explicit_value(self, monkeypatch):
        monkeypatch.setenv("SENTRY_RELEASE", "release-override")
        monkeypatch.setenv("RAILWAY_GIT_COMMIT_SHA", "railway-sha")
        kwargs = self._init_kwargs(monkeypatch)
        assert kwargs.get("release") == "release-override"

    def test_release_uses_railway_commit(self, monkeypatch):
        monkeypatch.delenv("SENTRY_RELEASE", raising=False)
        monkeypatch.setenv("RAILWAY_GIT_COMMIT_SHA", "railway-sha")
        kwargs = self._init_kwargs(monkeypatch)
        assert kwargs.get("release") == "railway-sha"

    def test_no_dsn_no_init(self, monkeypatch):
        monkeypatch.delenv("SENTRY_DSN", raising=False)
        monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
        with patch.object(sentry_init.sentry_sdk, "init") as mock_init:
            sentry_init.init_sentry()
        mock_init.assert_not_called()


class TestScrubEvent:
    def _event_with_vars(self, frame_vars):
        return {
            "exception": {
                "values": [
                    {"stacktrace": {"frames": [{"vars": dict(frame_vars)}]}},
                ],
            },
        }

    def test_secret_shaped_vars_scrubbed(self):
        event = self._event_with_vars({
            "kalshi_api_key": "fake-key-value",
            "private_key_pem": "fake-pem-value",
            "auth_token": "fake-token-value",
            "password": "fake-password-value",
            "signature": "fake-signature-value",
        })
        out = sentry_init._scrub_event(event, {})
        frame_vars = out["exception"]["values"][0]["stacktrace"]["frames"][0]["vars"]
        for name in frame_vars:
            assert frame_vars[name] == sentry_init._SCRUBBED, name

    def test_benign_vars_untouched(self):
        event = self._event_with_vars({"ticker": "KX-1", "price": "0.42"})
        out = sentry_init._scrub_event(event, {})
        frame_vars = out["exception"]["values"][0]["stacktrace"]["frames"][0]["vars"]
        assert frame_vars == {"ticker": "KX-1", "price": "0.42"}

    def test_event_without_exception_passthrough(self):
        event = {"message": "plain log"}
        assert sentry_init._scrub_event(event, {}) == event

    def test_frames_without_vars_passthrough(self):
        event = {
            "exception": {"values": [{"stacktrace": {"frames": [{"function": "f"}]}}]},
        }
        out = sentry_init._scrub_event(event, {})
        assert out["exception"]["values"][0]["stacktrace"]["frames"][0] == {"function": "f"}
