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
        handler._handle_pause_post.assert_not_called()

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
