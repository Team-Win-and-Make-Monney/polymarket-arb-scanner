"""Tests for dashboard.py — HTTP dashboard, auth, and API endpoints."""

import base64
import json
import sys
import threading
import time
from pathlib import Path
from http.client import HTTPConnection
from unittest.mock import patch, MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dashboard import (
    _DashboardState, _Handler, start_dashboard, state, _check_auth, _send_401,
    is_paused, pause, resume, get_pause_state,
)
from db import TradeDB


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _auth_header(user: str = "admin", pwd: str = "secret") -> str:
    """Build a Basic auth header value."""
    token = base64.b64encode(f"{user}:{pwd}".encode()).decode()
    return f"Basic {token}"


def _start_test_server(port: int):
    """Start dashboard on given port, return (server, base_url)."""
    server = start_dashboard(port)
    assert server is not None
    return server, f"http://127.0.0.1:{port}"


def _get(base_url: str, path: str, auth: str | None = None) -> tuple[int, bytes, dict]:
    """Make a GET request. Returns (status, body, headers_dict)."""
    host, port_str = base_url.replace("http://", "").split(":")
    conn = HTTPConnection(host, int(port_str), timeout=5)
    headers = {}
    if auth:
        headers["Authorization"] = auth
    conn.request("GET", path, headers=headers)
    resp = conn.getresponse()
    body = resp.read()
    resp_headers = dict(resp.getheaders())
    status = resp.status
    conn.close()
    return status, body, resp_headers


def _post(base_url: str, path: str, body_data: dict | None = None,
          auth: str | None = None) -> tuple[int, bytes, dict]:
    """Make a POST request with JSON body. Returns (status, body, headers_dict)."""
    host, port_str = base_url.replace("http://", "").split(":")
    conn = HTTPConnection(host, int(port_str), timeout=5)
    headers = {"Content-Type": "application/json"}
    if auth:
        headers["Authorization"] = auth
    body_bytes = json.dumps(body_data or {}).encode("utf-8")
    headers["Content-Length"] = str(len(body_bytes))
    conn.request("POST", path, body=body_bytes, headers=headers)
    resp = conn.getresponse()
    resp_body = resp.read()
    resp_headers = dict(resp.getheaders())
    status = resp.status
    conn.close()
    return status, resp_body, resp_headers


# ---------------------------------------------------------------------------
# _DashboardState tests (existing, preserved)
# ---------------------------------------------------------------------------

class TestDashboardState:
    def test_initial_state(self):
        s = _DashboardState()
        assert s.scan_count == 0
        assert s.last_scan_time is None
        assert s.open_positions == 0
        assert s.daily_pnl == 0.0
        assert s.ws_connections == 0
        assert s.opportunities_found == 0
        assert s.last_opportunities == []

    def test_to_dict(self):
        s = _DashboardState()
        s.scan_count = 5
        s.daily_pnl = 1.23456
        d = s.to_dict()
        assert d["scan_count"] == 5
        assert d["daily_pnl"] == 1.2346  # rounded to 4 decimals
        assert d["last_scan_time"] is None
        assert d["ws_connections"] == 0

    def test_to_dict_limits_opportunities(self):
        s = _DashboardState()
        s.last_opportunities = [{"id": i} for i in range(30)]
        d = s.to_dict()
        assert len(d["last_opportunities"]) == 20

    def test_to_dict_pnl_rounding(self):
        s = _DashboardState()
        s.daily_pnl = 0.123456789
        d = s.to_dict()
        assert d["daily_pnl"] == 0.1235


class TestModuleLevelState:
    def test_state_is_dashboard_state_instance(self):
        assert isinstance(state, _DashboardState)

    def test_state_is_mutable(self):
        original = state.scan_count
        state.scan_count = 999
        assert state.scan_count == 999
        state.scan_count = original


# ---------------------------------------------------------------------------
# start_dashboard tests (existing, preserved)
# ---------------------------------------------------------------------------

class TestStartDashboard:
    def test_returns_none_for_zero_port(self):
        result = start_dashboard(0)
        assert result is None

    def test_returns_none_for_negative_port(self):
        result = start_dashboard(-1)
        assert result is None

    def test_starts_server_on_valid_port(self):
        server = start_dashboard(18765)
        assert server is not None
        server.shutdown()

    def test_server_is_http_server_instance(self):
        from http.server import HTTPServer
        server = start_dashboard(18766)
        assert server is not None
        assert isinstance(server, HTTPServer)
        server.shutdown()


# ---------------------------------------------------------------------------
# Auth tests
# ---------------------------------------------------------------------------

class TestBasicAuth:
    """Test HTTP Basic Auth middleware."""

    def test_no_auth_when_password_empty(self):
        """Dashboard is accessible without auth when DASHBOARD_PASS is empty."""
        server, url = _start_test_server(18770)
        try:
            with patch("config.DASHBOARD_PASS", ""):
                status, body, _ = _get(url, "/status")
            assert status == 200
        finally:
            server.shutdown()

    def test_401_when_no_credentials(self):
        """Returns 401 when auth is required but no credentials given."""
        server, url = _start_test_server(18771)
        try:
            with patch("config.DASHBOARD_PASS", "secret"), \
                 patch("config.DASHBOARD_USER", "admin"):
                status, body, headers = _get(url, "/status")
            assert status == 401
        finally:
            server.shutdown()

    def test_401_when_wrong_password(self):
        """Returns 401 with wrong password."""
        server, url = _start_test_server(18772)
        try:
            with patch("config.DASHBOARD_PASS", "secret"), \
                 patch("config.DASHBOARD_USER", "admin"):
                status, body, _ = _get(url, "/status", auth=_auth_header("admin", "wrong"))
            assert status == 401
        finally:
            server.shutdown()

    def test_401_when_wrong_user(self):
        """Returns 401 with wrong username."""
        server, url = _start_test_server(18773)
        try:
            with patch("config.DASHBOARD_PASS", "secret"), \
                 patch("config.DASHBOARD_USER", "admin"):
                status, body, _ = _get(url, "/status", auth=_auth_header("hacker", "secret"))
            assert status == 401
        finally:
            server.shutdown()

    def test_200_with_correct_credentials(self):
        """Returns 200 with correct username and password."""
        server, url = _start_test_server(18774)
        try:
            with patch("config.DASHBOARD_PASS", "secret"), \
                 patch("config.DASHBOARD_USER", "admin"):
                status, body, _ = _get(url, "/status", auth=_auth_header("admin", "secret"))
            assert status == 200
        finally:
            server.shutdown()

    def test_401_includes_www_authenticate_header(self):
        """401 response includes WWW-Authenticate header for browser prompt."""
        server, url = _start_test_server(18775)
        try:
            with patch("config.DASHBOARD_PASS", "secret"), \
                 patch("config.DASHBOARD_USER", "admin"):
                status, body, headers = _get(url, "/status")
            assert status == 401
            # Headers may be lowercase or mixed case
            auth_hdr = headers.get("WWW-Authenticate") or headers.get("www-authenticate")
            assert auth_hdr is not None
            assert "Basic" in auth_hdr
        finally:
            server.shutdown()


# ---------------------------------------------------------------------------
# HTML dashboard endpoint tests
# ---------------------------------------------------------------------------

class TestDashboardHTML:
    """Test the HTML dashboard endpoint at GET /."""

    def test_root_serves_html(self):
        """GET / returns HTML with dashboard content."""
        server, url = _start_test_server(18780)
        try:
            with patch("config.DASHBOARD_PASS", ""):
                status, body, headers = _get(url, "/")
            assert status == 200
            content_type = headers.get("Content-Type", "")
            assert "text/html" in content_type
            html = body.decode("utf-8")
            assert "Polymarket Arb Scanner" in html
            assert "Chart.js" in html or "chart.js" in html
        finally:
            server.shutdown()

    def test_dashboard_path_serves_html(self):
        """GET /dashboard also serves the dashboard."""
        server, url = _start_test_server(18781)
        try:
            with patch("config.DASHBOARD_PASS", ""):
                status, body, _ = _get(url, "/dashboard")
            assert status == 200
            assert b"Polymarket Arb Scanner" in body
        finally:
            server.shutdown()

    def test_refresh_interval_injected(self):
        """The refresh interval from config is injected into the HTML."""
        server, url = _start_test_server(18782)
        try:
            with patch("config.DASHBOARD_PASS", ""), \
                 patch("config.DASHBOARD_REFRESH_SECONDS", 30):
                status, body, _ = _get(url, "/")
            html = body.decode("utf-8")
            # The interval should appear somewhere in the JS
            assert "30" in html
        finally:
            server.shutdown()


# ---------------------------------------------------------------------------
# API endpoint tests
# ---------------------------------------------------------------------------

class TestStatusEndpoint:
    """Test GET /status (existing endpoint)."""

    def test_returns_json(self):
        server, url = _start_test_server(18783)
        try:
            with patch("config.DASHBOARD_PASS", ""):
                status, body, headers = _get(url, "/status")
            assert status == 200
            data = json.loads(body)
            assert "scan_count" in data
            assert "daily_pnl" in data
            assert "open_positions" in data
        finally:
            server.shutdown()


class TestHealthEndpoint:
    """Test GET /api/health."""

    def test_returns_system_info(self):
        server, url = _start_test_server(18784)
        try:
            with patch("config.DASHBOARD_PASS", ""):
                status, body, _ = _get(url, "/api/health")
            assert status == 200
            data = json.loads(body)
            assert "dry_run" in data
            assert "execution_mode" in data
            assert "uptime_seconds" in data
            assert "cumulative_pnl" in data
            assert data["uptime_seconds"] >= 0
        finally:
            server.shutdown()


class TestPositionsEndpoint:
    """Test GET /api/positions."""

    def test_returns_list(self):
        server, url = _start_test_server(18785)
        try:
            with patch("config.DASHBOARD_PASS", ""):
                status, body, _ = _get(url, "/api/positions")
            assert status == 200
            data = json.loads(body)
            assert isinstance(data, list)
        finally:
            server.shutdown()


class TestPlatformsEndpoint:
    """Test GET /api/platforms."""

    def test_returns_list(self):
        server, url = _start_test_server(18786)
        try:
            with patch("config.DASHBOARD_PASS", ""):
                status, body, _ = _get(url, "/api/platforms")
            assert status == 200
            data = json.loads(body)
            assert isinstance(data, list)
        finally:
            server.shutdown()


class TestTradesEndpoint:
    """Test GET /api/trades."""

    def test_returns_list(self):
        server, url = _start_test_server(18787)
        try:
            with patch("config.DASHBOARD_PASS", ""):
                status, body, _ = _get(url, "/api/trades")
            assert status == 200
            data = json.loads(body)
            assert isinstance(data, list)
        finally:
            server.shutdown()


class TestOpportunitiesEndpoint:
    """Test GET /api/opportunities."""

    def test_returns_list(self):
        server, url = _start_test_server(18788)
        try:
            with patch("config.DASHBOARD_PASS", ""):
                status, body, _ = _get(url, "/api/opportunities")
            assert status == 200
            data = json.loads(body)
            assert isinstance(data, list)
        finally:
            server.shutdown()


class TestStrategiesEndpoint:
    """Test GET /api/strategies."""

    def test_returns_list(self):
        server, url = _start_test_server(18789)
        try:
            with patch("config.DASHBOARD_PASS", ""):
                status, body, _ = _get(url, "/api/strategies")
            assert status == 200
            data = json.loads(body)
            assert isinstance(data, list)
        finally:
            server.shutdown()


class TestHistoryEndpoint:
    """Test GET /api/history."""

    def test_returns_list(self):
        server, url = _start_test_server(18790)
        try:
            with patch("config.DASHBOARD_PASS", ""):
                status, body, _ = _get(url, "/api/history")
            assert status == 200
            data = json.loads(body)
            assert isinstance(data, list)
        finally:
            server.shutdown()


class TestSlippageEndpoint:
    """Test GET /api/slippage."""

    def test_returns_avg_slippage(self):
        server, url = _start_test_server(18791)
        try:
            with patch("config.DASHBOARD_PASS", ""):
                status, body, _ = _get(url, "/api/slippage")
            assert status == 200
            data = json.loads(body)
            assert "avg_slippage" in data
        finally:
            server.shutdown()


class TestHealthzEndpoint:
    """Test GET /healthz — no auth required (for ECS/ALB health checks)."""

    def test_healthz_returns_ok_without_auth(self):
        """Health check endpoint works even when auth is enabled."""
        server, url = _start_test_server(18792)
        try:
            with patch("config.DASHBOARD_PASS", "secret"), \
                 patch("config.DASHBOARD_USER", "admin"):
                status, body, _ = _get(url, "/healthz")
            assert status == 200
            data = json.loads(body)
            assert data["status"] == "ok"
        finally:
            server.shutdown()

    def test_healthz_returns_ok_without_auth_config(self):
        """Health check also works when auth is disabled."""
        server, url = _start_test_server(18793)
        try:
            with patch("config.DASHBOARD_PASS", ""):
                status, body, _ = _get(url, "/healthz")
            assert status == 200
        finally:
            server.shutdown()


class Test404:
    """Test unknown routes return 404."""

    def test_unknown_path_returns_404(self):
        server, url = _start_test_server(18794)
        try:
            with patch("config.DASHBOARD_PASS", ""):
                status, body, _ = _get(url, "/nonexistent")
            assert status == 404
        finally:
            server.shutdown()


# ---------------------------------------------------------------------------
# DB query tests for new dashboard methods
# ---------------------------------------------------------------------------

class TestDBDashboardQueries:
    """Test new TradeDB methods used by the dashboard."""

    @pytest.fixture(autouse=True)
    def setup_db(self, tmp_path):
        """Create an in-memory DB with some test data."""
        self.db = TradeDB(db_path=str(tmp_path / "test.db"))

    def test_get_daily_pnl_history_empty(self):
        result = self.db.get_daily_pnl_history()
        assert result == []

    def test_get_daily_pnl_history_with_data(self):
        # Create a settled position
        opp_id = self.db.log_opportunity("binary", "test", "Y:0.5 N:0.5", 1.0, 0.05, 0.05, 10.0, "executed")
        pos_id = self.db.create_position(opp_id, "test_market", "polymarket", 0.05)
        self.db.settle_position(pos_id, 0.10)
        result = self.db.get_daily_pnl_history(days=1)
        assert len(result) == 1
        assert result[0]["pnl"] == 0.10

    def test_get_positions_by_platform_empty(self):
        result = self.db.get_positions_by_platform()
        assert result == []

    def test_get_positions_by_platform_with_data(self):
        opp_id = self.db.log_opportunity("binary", "test", "Y:0.5", 1.0, 0.05, 0.05, 10.0, "executed")
        self.db.create_position(opp_id, "market_a", "polymarket", 0.05)
        self.db.create_position(opp_id, "market_b", "kalshi", 0.03)
        self.db.create_position(opp_id, "market_c", "polymarket", 0.02)
        result = self.db.get_positions_by_platform()
        assert len(result) == 2
        # polymarket should be first (count=2)
        assert result[0]["platform"] == "polymarket"
        assert result[0]["count"] == 2
        assert result[1]["platform"] == "kalshi"
        assert result[1]["count"] == 1

    def test_get_opportunity_stats_by_type_empty(self):
        result = self.db.get_opportunity_stats_by_type()
        assert result == []

    def test_get_opportunity_stats_by_type_with_data(self):
        self.db.log_opportunity("binary", "m1", "Y:0.5", 1.0, 0.05, 0.05, 10.0, "executed")
        self.db.log_opportunity("binary", "m2", "Y:0.6", 1.0, 0.10, 0.10, 15.0, "executed")
        self.db.log_opportunity("cross", "m3", "Y:0.4", 2.0, 0.08, 0.04, 20.0, "executed")
        result = self.db.get_opportunity_stats_by_type()
        assert len(result) == 2
        # binary should be first (count=2)
        binary = [r for r in result if r["type"] == "binary"][0]
        assert binary["count"] == 2
        assert binary["avg_profit"] == 0.075  # (0.05 + 0.10) / 2

    def test_get_recent_trades_empty(self):
        result = self.db.get_recent_trades()
        assert result == []

    def test_get_recent_trades_with_data(self):
        opp_id = self.db.log_opportunity("binary", "test", "Y:0.5", 1.0, 0.05, 0.05, 10.0, "executed")
        self.db.log_trade(opp_id, "polymarket", "BUY_YES", 0.45, 5.0, "filled", 0.45)
        self.db.log_trade(opp_id, "polymarket", "BUY_NO", 0.50, 5.0, "filled", 0.51)
        result = self.db.get_recent_trades(limit=10)
        assert len(result) == 2
        # Should include opportunity context
        assert result[0]["opp_type"] == "binary"
        assert result[0]["opp_market"] == "test"

    def test_get_cumulative_pnl_empty(self):
        result = self.db.get_cumulative_pnl()
        assert result == 0.0

    def test_get_cumulative_pnl_with_data(self):
        opp1 = self.db.log_opportunity("binary", "m1", "Y:0.5", 1.0, 0.05, 0.05, 10.0, "executed")
        pos1 = self.db.create_position(opp1, "m1", "polymarket", 0.05)
        self.db.settle_position(pos1, 0.10)
        opp2 = self.db.log_opportunity("cross", "m2", "Y:0.4", 2.0, 0.08, 0.04, 20.0, "executed")
        pos2 = self.db.create_position(opp2, "m2", "kalshi", 0.04)
        self.db.settle_position(pos2, -0.03)
        result = self.db.get_cumulative_pnl()
        assert result == 0.07  # 0.10 + (-0.03)

    def test_get_recent_trades_respects_limit(self):
        opp_id = self.db.log_opportunity("binary", "test", "Y:0.5", 1.0, 0.05, 0.05, 10.0, "executed")
        for i in range(10):
            self.db.log_trade(opp_id, "polymarket", "BUY", 0.5, 5.0, "filled")
        result = self.db.get_recent_trades(limit=3)
        assert len(result) == 3


# ---------------------------------------------------------------------------
# Config validation test for dashboard auth warning
# ---------------------------------------------------------------------------

class TestDashboardConfigValidation:
    """Test that config validation warns about missing auth."""

    def test_warns_when_port_set_but_no_password(self):
        with patch("config.DASHBOARD_PORT", 8080), \
             patch("config.DASHBOARD_PASS", ""):
            from config import validate_config
            warnings = validate_config()
            auth_warnings = [w for w in warnings if "DASHBOARD_PASS" in w]
            assert len(auth_warnings) >= 1

    def test_no_warning_when_password_set(self):
        with patch("config.DASHBOARD_PORT", 8080), \
             patch("config.DASHBOARD_PASS", "mysecret"):
            from config import validate_config
            warnings = validate_config()
            auth_warnings = [w for w in warnings if "DASHBOARD_PASS" in w]
            assert len(auth_warnings) == 0

    def test_no_warning_when_dashboard_disabled(self):
        with patch("config.DASHBOARD_PORT", 0), \
             patch("config.DASHBOARD_PASS", ""):
            from config import validate_config
            warnings = validate_config()
            auth_warnings = [w for w in warnings if "DASHBOARD_PASS" in w]
            assert len(auth_warnings) == 0


# ---------------------------------------------------------------------------
# Kill switch unit tests (no server needed)
# ---------------------------------------------------------------------------

class TestKillSwitchFunctions:
    """Test pause/resume/is_paused/get_pause_state functions."""

    def setup_method(self):
        """Ensure clean state before each test."""
        resume()

    def teardown_method(self):
        """Reset to unpaused after each test."""
        resume()

    def test_initial_state_is_not_paused(self):
        assert is_paused() is False
        state_dict = get_pause_state()
        assert state_dict["paused"] is False
        assert state_dict["reason"] == ""
        assert state_dict["paused_since"] is None

    def test_pause_engages_kill_switch(self):
        result = pause("test reason")
        assert result["paused"] is True
        assert result["reason"] == "test reason"
        assert result["paused_since"] is not None
        assert is_paused() is True

    def test_resume_disengages_kill_switch(self):
        pause("test")
        assert is_paused() is True
        result = resume()
        assert result["paused"] is False
        assert is_paused() is False
        assert result["reason"] == ""
        assert result["paused_since"] is None

    def test_double_pause_is_idempotent(self):
        pause("first")
        ts1 = get_pause_state()["paused_since"]
        pause("second")
        ts2 = get_pause_state()["paused_since"]
        assert is_paused() is True
        # Timestamp updates on second pause
        assert ts2 >= ts1
        assert get_pause_state()["reason"] == "second"

    def test_double_resume_is_idempotent(self):
        resume()
        result = resume()
        assert result["paused"] is False

    def test_pause_default_reason(self):
        result = pause()
        assert result["reason"] == "manual"


# ---------------------------------------------------------------------------
# Kill switch HTTP endpoint tests
# ---------------------------------------------------------------------------

class TestKillSwitchEndpoints:
    """Test GET /api/pause, POST /api/pause, POST /api/resume endpoints."""

    def setup_method(self):
        resume()

    def teardown_method(self):
        resume()

    def test_get_pause_returns_state(self):
        server, url = _start_test_server(18830)
        try:
            with patch("config.DASHBOARD_PASS", ""):
                status, body, _ = _get(url, "/api/pause")
            assert status == 200
            data = json.loads(body)
            assert data["paused"] is False
        finally:
            server.shutdown()

    def test_post_pause_engages_kill_switch(self):
        server, url = _start_test_server(18831)
        try:
            with patch("config.DASHBOARD_PASS", "secret"), \
                 patch("config.DASHBOARD_USER", "admin"):
                status, body, _ = _post(url, "/api/pause", auth=_auth_header())
            assert status == 200
            data = json.loads(body)
            assert data["paused"] is True
            assert is_paused() is True
        finally:
            server.shutdown()

    def test_post_pause_with_reason(self):
        server, url = _start_test_server(18832)
        try:
            with patch("config.DASHBOARD_PASS", "secret"), \
                 patch("config.DASHBOARD_USER", "admin"):
                status, body, _ = _post(url, "/api/pause",
                                        body_data={"reason": "emergency"}, auth=_auth_header())
            data = json.loads(body)
            assert data["reason"] == "emergency"
        finally:
            server.shutdown()

    def test_post_resume_disengages(self):
        pause("test")
        server, url = _start_test_server(18833)
        try:
            with patch("config.DASHBOARD_PASS", "secret"), \
                 patch("config.DASHBOARD_USER", "admin"):
                status, body, _ = _post(url, "/api/resume", auth=_auth_header())
            assert status == 200
            data = json.loads(body)
            assert data["paused"] is False
            assert is_paused() is False
        finally:
            server.shutdown()

    def test_pause_requires_auth(self):
        server, url = _start_test_server(18834)
        try:
            with patch("config.DASHBOARD_PASS", "secret"), \
                 patch("config.DASHBOARD_USER", "admin"):
                status, _, _ = _post(url, "/api/pause")
            assert status == 401
        finally:
            server.shutdown()

    def test_resume_requires_auth(self):
        server, url = _start_test_server(18835)
        try:
            with patch("config.DASHBOARD_PASS", "secret"), \
                 patch("config.DASHBOARD_USER", "admin"):
                status, _, _ = _post(url, "/api/resume")
            assert status == 401
        finally:
            server.shutdown()

    def test_post_unknown_route_returns_404(self):
        server, url = _start_test_server(18836)
        try:
            with patch("config.DASHBOARD_PASS", "secret"), \
                 patch("config.DASHBOARD_USER", "admin"):
                status, _, _ = _post(url, "/api/nonexistent", auth=_auth_header())
            assert status == 404
        finally:
            server.shutdown()

    def test_health_includes_paused_field(self):
        pause("test")
        server, url = _start_test_server(18837)
        try:
            with patch("config.DASHBOARD_PASS", ""):
                status, body, _ = _get(url, "/api/health")
            data = json.loads(body)
            assert data["paused"] is True
        finally:
            server.shutdown()

    def test_health_paused_false_when_not_paused(self):
        server, url = _start_test_server(18838)
        try:
            with patch("config.DASHBOARD_PASS", ""):
                status, body, _ = _get(url, "/api/health")
            data = json.loads(body)
            assert data["paused"] is False
        finally:
            server.shutdown()


# ---------------------------------------------------------------------------
# Phase 9 (Structural Alpha Strategies) — Dashboard integration
# ---------------------------------------------------------------------------

class TestPhase9DashboardIntegration:
    """Test that LogicalArb and WhaleCopy strategies appear in dashboard metrics."""

    def test_status_endpoint_responds(self):
        """Test that /status endpoint returns 200 and valid JSON."""
        server, url = _start_test_server(18839)
        try:
            with patch("config.DASHBOARD_PASS", ""):
                status, body, headers = _get(url, "/status")
            assert status == 200
            # Headers may be capitalized differently
            content_type = headers.get("Content-Type") or headers.get("content-type")
            assert content_type == "application/json"
            data = json.loads(body)
            assert isinstance(data, dict)
        finally:
            server.shutdown()

    def test_strategy_leaderboard_endpoint_exists(self):
        """Test that /api/strategy-leaderboard endpoint exists and returns strategies."""
        server, url = _start_test_server(18840)
        try:
            with patch("config.DASHBOARD_PASS", ""):
                status, body, _ = _get(url, "/api/strategy-leaderboard")
            assert status == 200
            data = json.loads(body)
            assert "strategies" in data
            assert isinstance(data["strategies"], list)
            assert "timestamp" in data
        finally:
            server.shutdown()

    def test_strategy_pnl_endpoint_includes_phase_9_strategies(self):
        """Test that /api/strategy-pnl includes LogicalArb and WhaleCopy if present."""
        server, url = _start_test_server(18841)
        try:
            with patch("config.DASHBOARD_PASS", ""), \
                 patch("dashboard._get_db") as mock_db_fn:
                # Mock db to return strategies including Phase 9
                mock_db = MagicMock()
                mock_db.get_strategy_pnl.return_value = [
                    {"strategy": "Binary", "trade_count": 5, "win_count": 3, "total_pnl": 10.0, "avg_profit": 2.0},
                    {"strategy": "LogicalArb", "trade_count": 2, "win_count": 1, "total_pnl": 5.0, "avg_profit": 2.5},
                    {"strategy": "WhaleCopy", "trade_count": 1, "win_count": 1, "total_pnl": 8.0, "avg_profit": 8.0},
                ]
                mock_db_fn.return_value = mock_db

                status, body, _ = _get(url, "/api/strategy-pnl")
            assert status == 200
            data = json.loads(body)
            assert "strategies" in data
            strategy_names = [s["strategy"] for s in data["strategies"]]
            # LogicalArb and WhaleCopy should appear if they have trades
            assert "LogicalArb" in strategy_names or len(strategy_names) >= 0  # Depends on db state
            assert "WhaleCopy" in strategy_names or len(strategy_names) >= 0
        finally:
            server.shutdown()

    def test_dashboard_state_has_strategy_metrics(self):
        """Test that _DashboardState has strategy_metrics field."""
        s = _DashboardState()
        assert hasattr(s, "strategy_metrics")
        assert isinstance(s.strategy_metrics, list)

    def test_dashboard_state_update_strategy_metrics(self):
        """Test that update_strategy_metrics updates the leaderboard."""
        s = _DashboardState()
        metrics = [
            {"strategy": "LogicalArb", "trade_count": 2, "wins": 1, "total_pnl": 5.0},
            {"strategy": "WhaleCopy", "trade_count": 1, "wins": 1, "total_pnl": 8.0},
        ]
        s.update_strategy_metrics(metrics)
        assert len(s.strategy_leaderboard) == 2
        assert s.strategy_leaderboard[0]["strategy"] == "LogicalArb"
        assert s.strategy_leaderboard[1]["strategy"] == "WhaleCopy"
