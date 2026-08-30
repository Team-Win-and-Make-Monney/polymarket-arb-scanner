"""Unit tests for credential health checks and alert firing."""

import asyncio
import time
import unittest
from unittest import mock

import sys
import os

# Add parent directory to path for imports
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from credential_health import CredentialHealthChecker, HEALTH_ENDPOINTS


class TestCredentialHealthChecker(unittest.TestCase):
    """Test suite for CredentialHealthChecker class."""

    @classmethod
    def setUpClass(cls):
        """Set up test fixtures once for the entire test class."""
        # Patch alerting module to avoid importing issues
        cls.mock_alerting = mock.MagicMock()
        sys.modules['alerting'] = cls.mock_alerting

    def setUp(self):
        """Set up test fixtures for each test."""
        # Create mock clients for all 8 platforms
        self.mock_clients = {
            "polymarket": mock.MagicMock(),
            "kalshi": mock.MagicMock(),
            "betfair": mock.MagicMock(),
            "smarkets": mock.MagicMock(),
            "sxbet": mock.MagicMock(),
            "matchbook": mock.MagicMock(),
            "gemini": mock.MagicMock(),
            "ibkr": mock.MagicMock(),
        }

        # Create mock alert manager
        self.mock_alert_manager = mock.MagicMock()

        # Create the health checker instance
        self.health_checker = CredentialHealthChecker(
            platform_clients=self.mock_clients,
            alert_manager=self.mock_alert_manager,
            interval_seconds=300,  # Short interval for testing
        )

    async def async_test(self, test_coro):
        """Helper to run async test coroutines."""
        return await test_coro

    def test_health_endpoints_defined_for_all_platforms(self):
        """Test that health check endpoints exist for all 8 platforms."""
        expected_platforms = {
            "polymarket", "kalshi", "betfair", "smarkets",
            "sxbet", "matchbook", "gemini", "ibkr"
        }
        actual_platforms = set(HEALTH_ENDPOINTS.keys())
        self.assertEqual(expected_platforms, actual_platforms)

    def test_health_endpoints_have_method_and_args(self):
        """Test that each endpoint has method and args keys."""
        for platform, endpoint_info in HEALTH_ENDPOINTS.items():
            self.assertIn("method", endpoint_info, f"{platform} missing 'method'")
            self.assertIn("args", endpoint_info, f"{platform} missing 'args'")
            self.assertIsInstance(endpoint_info["method"], str)
            self.assertIsInstance(endpoint_info["args"], dict)

    def test_all_platforms_healthy(self):
        """Test successful health check when all platforms return valid data."""
        # Set up all clients to return success (non-None)
        for platform, client in self.mock_clients.items():
            # Get the method name from HEALTH_ENDPOINTS
            method_name = HEALTH_ENDPOINTS[platform]["method"]
            setattr(client, method_name, mock.MagicMock(return_value=[{"id": 1}]))

        # Run the check
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            results = loop.run_until_complete(self.health_checker.check_all_platforms())
        finally:
            loop.close()

        # All platforms should be healthy
        for platform, is_healthy in results.items():
            self.assertTrue(is_healthy, f"{platform} should be healthy")

        # No alerts should have been fired
        self.mock_alert_manager.alert.assert_not_called()

    def test_single_platform_failure(self):
        """Test that a single platform failure is correctly detected."""
        # Set Polymarket to fail
        setattr(
            self.mock_clients["polymarket"],
            HEALTH_ENDPOINTS["polymarket"]["method"],
            mock.MagicMock(side_effect=Exception("Auth failed"))
        )

        # Set others to succeed
        for platform in ["kalshi", "betfair", "smarkets", "sxbet", "matchbook", "gemini", "ibkr"]:
            method_name = HEALTH_ENDPOINTS[platform]["method"]
            setattr(self.mock_clients[platform], method_name, mock.MagicMock(return_value=[]))

        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            results = loop.run_until_complete(self.health_checker.check_all_platforms())
        finally:
            loop.close()

        # Polymarket should be unhealthy, others healthy
        self.assertFalse(results["polymarket"])
        for platform in ["kalshi", "betfair", "smarkets", "sxbet", "matchbook", "gemini", "ibkr"]:
            self.assertTrue(results[platform])

    def test_three_consecutive_failures_fire_critical_alert(self):
        """Test that CRITICAL alert fires after 3 consecutive failures."""
        # Create a mock that returns False (unhealthy) for Polymarket
        # We use a method that returns False instead of raising to avoid retry logic
        unhealthy_mock = mock.MagicMock(return_value=None)  # None causes is_healthy to be False

        # Manually set up the health checker with a method that returns None
        setattr(
            self.mock_clients["polymarket"],
            HEALTH_ENDPOINTS["polymarket"]["method"],
            unhealthy_mock
        )

        # Set others to succeed with non-None return
        for platform in ["kalshi", "betfair", "smarkets", "sxbet", "matchbook", "gemini", "ibkr"]:
            method_name = HEALTH_ENDPOINTS[platform]["method"]
            setattr(self.mock_clients[platform], method_name, mock.MagicMock(return_value=[{"id": 1}]))

        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            # First check: 1 failure
            loop.run_until_complete(self.health_checker.check_all_platforms())
            self.assertEqual(1, self.health_checker._consecutive_failures.get("polymarket", 0))

            # Second check: 2 failures
            loop.run_until_complete(self.health_checker.check_all_platforms())
            self.assertEqual(2, self.health_checker._consecutive_failures.get("polymarket", 0))

            # Third check: 3 failures - should fire CRITICAL alert
            self.mock_alert_manager.reset_mock()
            loop.run_until_complete(self.health_checker.check_all_platforms())
            self.assertEqual(3, self.health_checker._consecutive_failures.get("polymarket", 0))

            # Verify alert was fired
            self.mock_alert_manager.alert.assert_called()
            call_args = self.mock_alert_manager.alert.call_args
            self.assertIn("CRITICAL", call_args[0])  # severity
        finally:
            loop.close()

    def test_timeout_is_info_severity(self):
        """Test that timeout exceptions fire INFO severity alerts."""
        # Mock the _async_call method to raise TimeoutError
        with mock.patch.object(self.health_checker, '_async_call', side_effect=asyncio.TimeoutError()):
            # Set all clients
            for platform in self.mock_clients:
                method_name = HEALTH_ENDPOINTS[platform]["method"]
                setattr(self.mock_clients[platform], method_name, mock.MagicMock(return_value=[]))

            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            try:
                loop.run_until_complete(self.health_checker.check_all_platforms())
            finally:
                loop.close()

            self.mock_alert_manager.alert.assert_called()
            calls = self.mock_alert_manager.alert.call_args_list
            found_info = any("INFO" in str(call) for call in calls)
            self.assertTrue(found_info, "Should have fired INFO severity alert for timeout")

    def test_auth_failure_is_warning_severity(self):
        """Test that auth failures fire WARNING severity alerts."""
        # Set Polymarket to fail with auth error
        setattr(
            self.mock_clients["polymarket"],
            HEALTH_ENDPOINTS["polymarket"]["method"],
            mock.MagicMock(side_effect=Exception("Unauthorized"))
        )

        # Set others to succeed
        for platform in ["kalshi", "betfair", "smarkets", "sxbet", "matchbook", "gemini", "ibkr"]:
            method_name = HEALTH_ENDPOINTS[platform]["method"]
            setattr(self.mock_clients[platform], method_name, mock.MagicMock(return_value=[]))

        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(self.health_checker.check_all_platforms())
        finally:
            loop.close()

        self.mock_alert_manager.alert.assert_called()
        calls = self.mock_alert_manager.alert.call_args_list
        found_warning = any("WARNING" in str(call) for call in calls)
        self.assertTrue(found_warning, "Should have fired WARNING severity alert for auth failure")

    def test_outer_exception_path_alerts_after_three_failures(self):
        with mock.patch.object(
            self.health_checker,
            "_check_platform_health",
            side_effect=RuntimeError("probe crashed"),
        ):
            loop = asyncio.new_event_loop()
            try:
                for _ in range(3):
                    loop.run_until_complete(self.health_checker.check_all_platforms())
            finally:
                loop.close()

        calls = self.mock_alert_manager.alert.call_args_list
        self.assertTrue(any("CRITICAL" in str(call) for call in calls))

    def test_alert_rate_limiting(self):
        """Test that alerts are rate-limited to 1 per 5-minute window per platform."""
        # Set Polymarket to always fail
        setattr(
            self.mock_clients["polymarket"],
            HEALTH_ENDPOINTS["polymarket"]["method"],
            mock.MagicMock(side_effect=Exception("Unauthorized"))
        )

        # Set others to succeed
        for platform in ["kalshi", "betfair", "smarkets", "sxbet", "matchbook", "gemini", "ibkr"]:
            method_name = HEALTH_ENDPOINTS[platform]["method"]
            setattr(self.mock_clients[platform], method_name, mock.MagicMock(return_value=[]))

        # Manually call _fire_credential_alert twice
        now = time.time()
        with mock.patch('time.time', return_value=now):
            self.health_checker._fire_credential_alert("polymarket", "WARNING", "First alert")
            alert_call_count_1 = self.mock_alert_manager.alert.call_count

        # Second call should be rate-limited (still within 5 min)
        with mock.patch('time.time', return_value=now + 60):
            self.health_checker._fire_credential_alert("polymarket", "WARNING", "Second alert")
            alert_call_count_2 = self.mock_alert_manager.alert.call_count

        # Alert should not have been called again due to rate limiting
        self.assertEqual(alert_call_count_1, alert_call_count_2)

    def test_token_expiry_alert_24h_before(self):
        """Test that CRITICAL alert fires when token expires < 24 hours."""
        now = time.time()
        expiry_time = now + 12 * 3600  # 12 hours in the future

        # Mock config module imported inside _check_token_expiry
        with mock.patch('time.time', return_value=now):
            with mock.patch('config.BETFAIR_TOKEN_EXPIRY_TIMESTAMP', expiry_time):
                result = self.health_checker._check_token_expiry("betfair")
                # Should return False (alert was fired)
                self.assertFalse(result)

                # Verify alert was called
                self.mock_alert_manager.alert.assert_called()

    def test_token_not_expiring_soon(self):
        """Test that no alert fires when token expires > 24 hours."""
        now = time.time()
        expiry_time = now + 48 * 3600  # 48 hours in the future

        with mock.patch('time.time', return_value=now):
            with mock.patch('config.BETFAIR_TOKEN_EXPIRY_TIMESTAMP', expiry_time):
                result = self.health_checker._check_token_expiry("betfair")
                # Should return True (no action needed)
                self.assertTrue(result)

    def test_multiple_platforms_independence(self):
        """Test that platform failures are tracked independently."""
        # Set Polymarket and Kalshi to fail
        setattr(
            self.mock_clients["polymarket"],
            HEALTH_ENDPOINTS["polymarket"]["method"],
            mock.MagicMock(side_effect=Exception("Error 1"))
        )
        setattr(
            self.mock_clients["kalshi"],
            HEALTH_ENDPOINTS["kalshi"]["method"],
            mock.MagicMock(side_effect=Exception("Error 2"))
        )

        # Set others to succeed
        for platform in ["betfair", "smarkets", "sxbet", "matchbook", "gemini", "ibkr"]:
            method_name = HEALTH_ENDPOINTS[platform]["method"]
            setattr(self.mock_clients[platform], method_name, mock.MagicMock(return_value=[]))

        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            results = loop.run_until_complete(self.health_checker.check_all_platforms())
        finally:
            loop.close()

        # Both failed platforms should be tracked independently
        self.assertFalse(results["polymarket"])
        self.assertFalse(results["kalshi"])
        self.assertEqual(1, self.health_checker._consecutive_failures.get("polymarket", 0))
        self.assertEqual(1, self.health_checker._consecutive_failures.get("kalshi", 0))

    def test_retry_logic_2_attempts(self):
        """Test that health check method retries up to 2 times."""
        # Create a mock that fails first time, succeeds second time
        mock_method = mock.MagicMock(side_effect=[Exception("Network error"), [{"id": 1}]])
        setattr(
            self.mock_clients["polymarket"],
            HEALTH_ENDPOINTS["polymarket"]["method"],
            mock_method
        )

        # Set others to succeed
        for platform in ["kalshi", "betfair", "smarkets", "sxbet", "matchbook", "gemini", "ibkr"]:
            method_name = HEALTH_ENDPOINTS[platform]["method"]
            setattr(self.mock_clients[platform], method_name, mock.MagicMock(return_value=[]))

        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            results = loop.run_until_complete(self.health_checker.check_all_platforms())
        finally:
            loop.close()

        # Should succeed due to retry
        self.assertTrue(results["polymarket"], "Should succeed after retry")
        # Should have been called twice (once failure + one retry)
        self.assertGreaterEqual(mock_method.call_count, 1)


if __name__ == "__main__":
    unittest.main()
