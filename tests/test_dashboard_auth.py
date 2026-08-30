"""Dashboard auth hardening tests (audit S06 fail-closed + S14 constant-time)."""
from __future__ import annotations

import base64
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import dashboard


def _handler(auth_header: str | None = None) -> MagicMock:
    h = MagicMock()
    h.headers = {"Authorization": auth_header} if auth_header else {}
    return h


def _basic(user: str, pwd: str) -> str:
    return "Basic " + base64.b64encode(f"{user}:{pwd}".encode()).decode()


class TestDashboardAuth:
    # -----------------------------------------------------------------------
    # S06 — fail closed when DASHBOARD_PASS is unset
    # -----------------------------------------------------------------------

    def test_post_denied_when_pass_unset(self):
        # Reads stay open, but state-changing POSTs (kill-switch, resume, purge,
        # fund-transfer) fail closed without a configured password.
        handler = MagicMock()
        handler.path = "/api/pause"
        handler.headers = {"Content-Length": "0"}
        with patch("config.DASHBOARD_PASS", ""), patch("dashboard._send_401") as m401:
            dashboard._Handler.do_POST(handler)
        m401.assert_called_once()


class TestDashboardPostOrigin:
    def test_rejects_simple_form_content_type(self):
        handler = MagicMock()
        handler.headers = {"Content-Type": "text/plain"}
        with patch("dashboard._send_json") as send_json:
            assert dashboard._check_post_origin(handler) is False
        assert send_json.call_args.args[2] == 415

    def test_rejects_cross_site_origin(self):
        handler = MagicMock()
        handler.headers = {
            "Content-Type": "application/json",
            "Host": "127.0.0.1:8080",
            "Origin": "https://attacker.example",
        }
        with patch("dashboard._send_json") as send_json:
            assert dashboard._check_post_origin(handler) is False
        assert send_json.call_args.args[2] == 403

    def test_accepts_same_origin_json(self):
        handler = MagicMock()
        handler.headers = {
            "Content-Type": "application/json; charset=utf-8",
            "Host": "127.0.0.1:8080",
            "Origin": "http://127.0.0.1:8080",
        }
        assert dashboard._check_post_origin(handler) is True

    # -----------------------------------------------------------------------
    # S14 — constant-time credential comparison, correct accept/reject
    # -----------------------------------------------------------------------

    def test_accepts_correct_credentials(self):
        with patch("config.DASHBOARD_PASS", "s3cret"), patch("config.DASHBOARD_USER", "admin"):
            assert dashboard._check_auth(_handler(_basic("admin", "s3cret"))) is True

    def test_rejects_wrong_password(self):
        with patch("config.DASHBOARD_PASS", "s3cret"), patch("config.DASHBOARD_USER", "admin"):
            assert dashboard._check_auth(_handler(_basic("admin", "WRONG"))) is False

    def test_rejects_wrong_user(self):
        with patch("config.DASHBOARD_PASS", "s3cret"), patch("config.DASHBOARD_USER", "admin"):
            assert dashboard._check_auth(_handler(_basic("attacker", "s3cret"))) is False

    def test_rejects_missing_header(self):
        with patch("config.DASHBOARD_PASS", "s3cret"), patch("config.DASHBOARD_USER", "admin"):
            assert dashboard._check_auth(_handler()) is False
