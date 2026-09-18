"""API credential health checker — probes auth status every 30 minutes."""

import asyncio
import logging
import time
from tenacity import retry, stop_after_attempt, wait_exponential

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Health check endpoints: cheap operations to validate auth
# ---------------------------------------------------------------------------

HEALTH_ENDPOINTS = {
    "polymarket": {"method": "fetch_all_markets", "args": {"limit": 1}},
    "kalshi": {"method": "fetch_all_events", "args": {"limit": 1}},
    "betfair": {"method": "list_event_types", "args": {}},
    "smarkets": {"method": "fetch_all_markets", "args": {}},
    "sxbet": {"method": "health_check", "args": {}},
    "matchbook": {"method": "fetch_all_events", "args": {}},
    "gemini": {"method": "fetch_all_markets", "args": {"status": "active"}},
    "ibkr": {"method": "fetch_all_markets", "args": {}},
}


class CredentialHealthChecker:
    """Probes API credentials every 30 minutes to detect auth issues early.

    Features:
    - Per-platform health checks with 10-second timeout
    - Retry logic: 2 attempts with exponential backoff
    - Tracks consecutive failures per platform
    - Fires CRITICAL alert after 3 consecutive failures
    - Distinguishes between timeout (INFO) and auth failure (WARNING)
    - Rate-limited alerts: max 1 per platform per 5-minute window
    - Pre-expiry alerts for time-limited tokens (24 hours before)
    """

    def __init__(
        self,
        platform_clients: dict,
        alert_manager,
        interval_seconds: int = 1800,
    ):
        """
        Args:
            platform_clients: Dict mapping platform_name → client instance
            alert_manager: AlertManager instance for firing alerts
            interval_seconds: Check interval in seconds (default 30 min = 1800s)
        """
        self.clients = platform_clients
        self.alert_manager = alert_manager
        self.interval = interval_seconds

        # Last successful health check timestamp per platform
        self._last_check: dict[str, float] = {}

        # Consecutive failure count per platform
        self._consecutive_failures: dict[str, int] = {}

        # Last alert timestamp per platform (for rate limiting)
        self._last_alert_time: dict[str, float] = {}

    async def check_all_platforms(self) -> dict[str, bool]:
        """Check health of all platforms.

        Returns:
            dict mapping platform_name → True (healthy) or False (unhealthy)
        """
        results = {}
        tasks = []

        for platform_name in self.clients:
            task = self._check_platform_health(platform_name)
            tasks.append((platform_name, task))

        for platform_name, task in tasks:
            try:
                is_healthy = await task
                results[platform_name] = is_healthy

                if is_healthy:
                    # Reset failure count on success
                    self._consecutive_failures[platform_name] = 0
                    self._last_check[platform_name] = time.time()
                else:
                    # Increment failure count
                    self._consecutive_failures[platform_name] = \
                        self._consecutive_failures.get(platform_name, 0) + 1

                    # Fire alert after 3 consecutive failures
                    fail_count = self._consecutive_failures[platform_name]
                    if fail_count == 3:
                        self._fire_credential_alert(
                            platform_name,
                            "CRITICAL",
                            f"Credential health check failed 3 times: {platform_name}",
                        )
                    elif fail_count == 1:
                        # Log first failure for debugging
                        logger.warning(
                            "Credential health check failed for %s (attempt %d)",
                            platform_name,
                            fail_count,
                        )

            except Exception as e:
                logger.error(
                    "Credential check exception for %s: %s",
                    platform_name,
                    e,
                    exc_info=False,
                )
                results[platform_name] = False
                self._consecutive_failures[platform_name] = \
                    self._consecutive_failures.get(platform_name, 0) + 1

        ok_count = sum(1 for v in results.values() if v)
        total = len(results)
        logger.info("Credential health check complete: %d/%d platforms OK", ok_count, total)

        return results

    @retry(
        stop=stop_after_attempt(2),
        wait=wait_exponential(multiplier=0.5, min=1, max=3),
        reraise=True,
    )
    async def _check_platform_health(self, platform: str) -> bool:
        """Probe a single platform's auth status.

        Args:
            platform: Platform name (key in self.clients)

        Returns:
            True if auth succeeded, False otherwise
        """
        client = self.clients.get(platform)
        if not client:
            logger.warning("No client found for platform: %s", platform)
            return False

        endpoint_info = HEALTH_ENDPOINTS.get(platform)
        if not endpoint_info:
            logger.warning("No health endpoint defined for platform: %s", platform)
            return False

        method_name = endpoint_info["method"]
        method = getattr(client, method_name, None)
        if not method:
            logger.warning(
                "Client %s has no method %s",
                platform,
                method_name,
            )
            return False

        # Call method with timeout
        args = endpoint_info["args"]
        try:
            result = await asyncio.wait_for(
                self._async_call(method, **args),
                timeout=10.0,
            )
            return result is not None
        except asyncio.TimeoutError:
            self._fire_credential_alert(
                platform,
                "INFO",
                f"Credential health check timeout for {platform}",
            )
            raise  # Re-raise so retry decorator can retry on timeout
        except Exception as e:
            error_msg = str(e).lower()
            if any(
                keyword in error_msg
                for keyword in ["unauthorized", "forbidden", "invalid", "auth"]
            ):
                self._fire_credential_alert(
                    platform,
                    "WARNING",
                    f"Credential health check auth failure for {platform}: {e}",
                )
            else:
                logger.debug(
                    "Platform %s health check error (not auth): %s",
                    platform,
                    e,
                )
            raise  # Re-raise so retry decorator can retry

    async def _async_call(self, func, **kwargs):
        """Wrapper to call a sync function asynchronously."""
        loop = asyncio.get_event_loop()
        # Use functools.partial to bind kwargs before passing to executor
        import functools
        partial_func = functools.partial(func, **kwargs)
        return await loop.run_in_executor(None, partial_func)

    def _fire_credential_alert(self, platform: str, severity: str, message: str):
        """Fire a credential health alert with rate limiting.

        Rate limit: max 1 alert per platform per 5-minute window.

        Args:
            platform: Platform name
            severity: "INFO", "WARNING", or "CRITICAL"
            message: Alert message
        """
        now = time.time()
        last_alert = self._last_alert_time.get(platform, 0)

        # 5-minute cooldown between alerts for same platform
        if now - last_alert < 300:
            logger.debug(
                "Alert rate-limited for %s (last alert %.0f seconds ago)",
                platform,
                now - last_alert,
            )
            return

        try:
            from alerting import AlertType
            alert_type = AlertType.CREDENTIAL_FAILURE \
                if hasattr(AlertType, "CREDENTIAL_FAILURE") \
                else "CREDENTIAL_FAILURE"
        except (ImportError, AttributeError):
            alert_type = "CREDENTIAL_FAILURE"

        self.alert_manager.alert(
            alert_type,
            severity,
            message,
            {"platform": platform},
        )

        self._last_alert_time[platform] = now
        logger.info("Credential alert fired for %s (%s): %s", platform, severity, message)

    def _check_token_expiry(self, platform: str) -> bool:
        """Check if a time-limited token is expiring within 24 hours.

        Currently supports:
        - Betfair SSO tokens
        - Smarkets session tokens

        Returns:
            True if expiry check passed (no action needed), False if alert fired
        """
        try:
            import config

            now = time.time()
            expiry_window = 86400  # 24 hours

            # Check Betfair token expiry
            if platform == "betfair":
                betfair_expiry = getattr(config, "BETFAIR_TOKEN_EXPIRY_TIMESTAMP", 0)
                if betfair_expiry > 0 and betfair_expiry < now + expiry_window:
                    hours_remaining = (betfair_expiry - now) / 3600
                    self._fire_credential_alert(
                        platform,
                        "CRITICAL",
                        f"Betfair credential expires in {hours_remaining:.1f} hours",
                    )
                    return False
                return True

            # Check Smarkets session expiry
            if platform == "smarkets":
                smarkets_expiry = getattr(config, "SMARKETS_SESSION_EXPIRY_TIMESTAMP", 0)
                if smarkets_expiry > 0 and smarkets_expiry < now + expiry_window:
                    hours_remaining = (smarkets_expiry - now) / 3600
                    self._fire_credential_alert(
                        platform,
                        "CRITICAL",
                        f"Smarkets session expires in {hours_remaining:.1f} hours",
                    )
                    return False
                return True

            # Other platforms don't have time-limited tokens
            return True

        except Exception as e:
            logger.warning("Error checking token expiry for %s: %s", platform, e)
            return True
