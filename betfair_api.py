"""Betfair Exchange API client for market data and trading."""

import logging
import os
import threading
import time

import requests
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type

from config import BETFAIR_RATE_LIMIT
from rate_limiter import PlatformCircuitBreaker
from url_guard import assert_public_url

logger = logging.getLogger(__name__)


class _RateLimitError(Exception):
    """Raised when Betfair returns HTTP 429 — triggers tenacity retry."""


BETFAIR_SSO_URL = "https://identitysso.betfair.com/api/login"
BETFAIR_EXCHANGE_URL = "https://api.betfair.com/exchange/betting/rest/v1.0"
BETFAIR_ACCOUNT_URL = "https://api.betfair.com/exchange/account/rest/v1.0"

# Rate limiting (thread-safe)
_last_request_time = 0
_rate_lock = threading.Lock()

# HARDEN-04: circuit breaker — opens after 3 consecutive failures, resets after 30s
_circuit = PlatformCircuitBreaker("betfair", fail_limit=3, reset_timeout=30.0)


def _rate_limit():
    global _last_request_time
    with _rate_lock:
        now = time.time()
        elapsed = now - _last_request_time
        if elapsed < BETFAIR_RATE_LIMIT:
            time.sleep(BETFAIR_RATE_LIMIT - elapsed)
        _last_request_time = time.time()


class BetfairClient:
    """Betfair Exchange API client with SSO authentication."""

    def __init__(self):
        self.session = requests.Session()
        proxy_url = os.getenv("BETFAIR_PROXY_URL")
        if proxy_url:
            proxy_url = assert_public_url(proxy_url, env_name="BETFAIR_PROXY_URL")
            self.session.proxies = {"http": proxy_url, "https": proxy_url}
        self.api_key = None
        self.ssoid = None
        self.authenticated = False

    def login(
        self,
        username: str = None,
        password: str = None,
        api_key: str = None,
    ) -> bool:
        """Authenticate via Betfair SSO.

        Args:
            username: Betfair username (falls back to BETFAIR_USERNAME env var).
            password: Betfair password (falls back to BETFAIR_PASSWORD env var).
            api_key: Betfair app key (falls back to BETFAIR_APP_KEY, then BETFAIR_API_KEY).

        Returns:
            True if login succeeded.
        """
        username = username or os.getenv("BETFAIR_USERNAME")
        password = password or os.getenv("BETFAIR_PASSWORD")
        api_key = api_key or os.getenv("BETFAIR_APP_KEY") or os.getenv("BETFAIR_API_KEY")

        if not all([username, password, api_key]):
            logger.error("Betfair credentials not provided")
            return False

        self.api_key = api_key
        _rate_limit()
        try:
            resp = self.session.post(
                BETFAIR_SSO_URL,
                data={"username": username, "password": password},
                headers={"X-Application": api_key, "Accept": "application/json"},
                timeout=30,
            )
            if resp.status_code == 200:
                data = resp.json()
                if data.get("status") == "SUCCESS":
                    self.ssoid = data.get("token")
                    self.session.headers.update({
                        "X-Application": self.api_key,
                        "X-Authentication": self.ssoid,
                        "Accept": "application/json",
                        "Content-Type": "application/json",
                    })
                    self.authenticated = True
                    return True
                logger.error("Betfair login failed: %s", data.get('error', 'unknown'))
                return False
            logger.error("Betfair SSO returned status %s", resp.status_code)
            return False
        except requests.RequestException as e:
            logger.error("Betfair login request failed: %s", e)
            return False

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=1, max=10),
        retry=retry_if_exception_type((requests.ConnectionError, requests.Timeout, _RateLimitError)),
        reraise=True,
    )
    def _request(
        self,
        endpoint: str,
        params: dict | None = None,
        body: dict | None = None,
    ) -> dict | None:
        """Make an authenticated request to the Betfair Exchange API.

        Args:
            endpoint: API endpoint name (e.g. 'listEventTypes').
            params: JSON-RPC filter parameters. Used only when ``body`` is
                None — the helper wraps ``params`` as ``{"filter": params}``,
                matching the Betfair Exchange list* convention.
            body: Optional fully-formed JSON body. When supplied, it is sent
                verbatim, bypassing the ``{"filter": ...}`` wrapper. Used by
                non-list endpoints like placeOrders / cancelOrders that have
                their own request schema.

        Returns:
            Response data or None on failure.
        """
        if not self.authenticated:
            logger.error("Betfair: must login before making API calls")
            return None

        if _circuit.is_open():
            raise _RateLimitError("Circuit open -- betfair in backoff")
        _rate_limit()
        try:
            payload = body if body is not None else {"filter": params or {}}
            resp = self.session.post(
                f"{BETFAIR_EXCHANGE_URL}/{endpoint}/",
                json=payload,
                timeout=30,
            )
            if resp.status_code == 200:
                _circuit.record_success()
                return resp.json()
            if resp.status_code == 429:
                logger.warning("Betfair rate limited on %s, retrying...", endpoint)
                raise _RateLimitError(f"Betfair 429 on {endpoint}")
            logger.warning("Betfair %s returned %s: %s", endpoint, resp.status_code, resp.text[:200])
            return None
        except (requests.ConnectionError, requests.Timeout, _RateLimitError):
            _circuit.record_failure()
            raise  # Let tenacity retry these
        except requests.RequestException as e:
            logger.warning("Betfair %s failed: %s", endpoint, e)
            return None

    def list_event_types(self) -> list[dict]:
        """Fetch all event types (sports categories).

        Returns:
            List of event type dicts.
        """
        data = self._request("listEventTypes")
        if data and isinstance(data, list):
            return data
        return []

    def list_events(self, event_type_id: str = None) -> list[dict]:
        """Fetch events, optionally filtered by event type.

        Args:
            event_type_id: Filter by event type (e.g. '7' for horse racing).

        Returns:
            List of event dicts.
        """
        params = {}
        if event_type_id:
            params["eventTypeIds"] = [event_type_id]
        data = self._request("listEvents", params)
        if data and isinstance(data, list):
            return data
        return []

    def list_markets(self, event_id: str) -> list[dict]:
        """Fetch market catalogue for an event.

        Args:
            event_id: Betfair event ID.

        Returns:
            List of market dicts with runner info.
        """
        if not self.authenticated:
            return []

        _rate_limit()
        try:
            resp = self.session.post(
                f"{BETFAIR_EXCHANGE_URL}/listMarketCatalogue/",
                json={
                    "filter": {"eventIds": [event_id]},
                    "maxResults": 100,
                    "marketProjection": [
                        "RUNNER_DESCRIPTION",
                        "MARKET_DESCRIPTION",
                        "EVENT",
                    ],
                },
                timeout=30,
            )
            if resp.status_code == 200:
                return resp.json()
            return []
        except requests.RequestException as e:
            logger.warning("Betfair list_markets failed: %s", e)
            return []

    def list_runners(self, market_id: str) -> list[dict]:
        """Fetch market book with price data for all runners.

        Args:
            market_id: Betfair market ID.

        Returns:
            List of runner dicts with price/size data.
        """
        if not self.authenticated:
            return []

        _rate_limit()
        try:
            resp = self.session.post(
                f"{BETFAIR_EXCHANGE_URL}/listMarketBook/",
                json={
                    "marketIds": [market_id],
                    "priceProjection": {
                        "priceData": ["EX_BEST_OFFERS"],
                    },
                },
                timeout=30,
            )
            if resp.status_code == 200:
                data = resp.json()
                if data and isinstance(data, list) and data:
                    return data[0].get("runners", [])
            return []
        except requests.RequestException as e:
            logger.warning("Betfair list_runners failed for %s: %s", market_id, e)
            return []

    def place_orders(self, market_id: str, instructions: list[dict]) -> dict | None:
        """Place orders on a Betfair market.

        Routed through ``self._request`` so 429/timeout retries and the
        rate-limit circuit breaker apply (PR G fix — previously this
        method bypassed both via a direct ``self.session.post``).

        Args:
            market_id: Betfair market ID.
            instructions: List of order instruction dicts, each with:
                - selectionId: Runner selection ID
                - side: 'BACK' or 'LAY'
                - orderType: 'LIMIT'
                - limitOrder: {size, price, persistenceType}

        Returns:
            Place orders response (with ``status='SUCCESS'``) or None on
            failure (covers HTTP error, retried-out 429, or
            non-SUCCESS status from a 200 response).
        """
        data = self._request(
            "placeOrders",
            body={"marketId": market_id, "instructions": instructions},
        )
        if not data:
            return None
        status = data.get("status")
        if status == "SUCCESS":
            return data
        logger.error(
            "Betfair place_orders non-SUCCESS: %s",
            data.get("errorCode", "unknown"),
        )
        return None

    def get_current_orders(self) -> list[dict]:
        """Get all current unmatched orders.

        Returns:
            List of order dicts.
        """
        if not self.authenticated:
            return []

        _rate_limit()
        try:
            resp = self.session.post(
                f"{BETFAIR_EXCHANGE_URL}/listCurrentOrders/",
                json={},
                timeout=30,
            )
            if resp.status_code == 200:
                data = resp.json()
                return data.get("currentOrders", [])
            return []
        except requests.RequestException as e:
            logger.warning("Betfair get_current_orders failed: %s", e)
            return []

    def get_balance(self) -> float | None:
        """Get available account balance.

        Returns:
            Balance as float or None on failure.
        """
        if not self.authenticated:
            return None

        _rate_limit()
        try:
            resp = self.session.post(
                f"{BETFAIR_ACCOUNT_URL}/getAccountFunds/",
                json={"wallet": "UK"},
                headers={
                    "X-Application": self.api_key,
                    "X-Authentication": self.ssoid,
                    "Accept": "application/json",
                    "Content-Type": "application/json",
                },
                timeout=30,
            )
            if resp.status_code == 200:
                data = resp.json()
                return float(data.get("availableToBetBalance", 0))
            return None
        except requests.RequestException as e:
            logger.warning("Betfair get_balance failed: %s", e)
            return None

    def get_order_status(self, bet_id: str) -> dict | None:
        """Get status of a specific bet/order.

        Args:
            bet_id: Betfair bet ID.

        Returns:
            Order dict or None.
        """
        if not self.authenticated:
            return None

        _rate_limit()
        try:
            resp = self.session.post(
                f"{BETFAIR_EXCHANGE_URL}/listCurrentOrders/",
                json={"betIds": [bet_id]},
                timeout=30,
            )
            if resp.status_code == 200:
                data = resp.json()
                orders = data.get("currentOrders", [])
                return orders[0] if orders else None
            return None
        except requests.RequestException as e:
            logger.warning("Betfair get_order_status failed: %s", e)
            return None

    def list_market_books(self, market_ids: list[str]) -> list[dict]:
        """Fetch market books for multiple markets (batch, max 10 per call).

        Args:
            market_ids: List of Betfair market IDs (max 10).

        Returns:
            List of market book dicts with runner price data.
        """
        if not self.authenticated:
            return []

        # Batch into groups of 10
        results = []
        for i in range(0, len(market_ids), 10):
            batch = market_ids[i:i + 10]
            _rate_limit()
            try:
                resp = self.session.post(
                    f"{BETFAIR_EXCHANGE_URL}/listMarketBook/",
                    json={
                        "marketIds": batch,
                        "priceProjection": {
                            "priceData": ["EX_BEST_OFFERS"],
                        },
                    },
                    timeout=30,
                )
                if resp.status_code == 200:
                    data = resp.json()
                    if isinstance(data, list):
                        results.extend(data)
            except requests.RequestException as e:
                logger.warning("Betfair list_market_books failed for batch %d: %s", i // 10, e)
        return results

    def cancel_orders(self, market_id: str = None, bet_ids: list[str] = None) -> bool:
        """Cancel orders on a market or specific bet IDs.

        Args:
            market_id: Cancel all orders on this market (optional).
            bet_ids: Cancel specific bets (optional).

        Returns:
            True if cancellation succeeded.
        """
        if not self.authenticated:
            return False

        body = {}
        if market_id:
            body["marketId"] = market_id
        if bet_ids:
            body["instructions"] = [{"betId": bid} for bid in bet_ids]

        _rate_limit()
        try:
            resp = self.session.post(
                f"{BETFAIR_EXCHANGE_URL}/cancelOrders/",
                json=body,
                timeout=30,
            )
            if resp.status_code == 200:
                data = resp.json()
                return data.get("status") == "SUCCESS"
            return False
        except requests.RequestException as e:
            logger.warning("Betfair cancel_orders failed: %s", e)
            return False

    def get_market_price(self, market: dict) -> tuple[float | None, float | None]:
        """Extract best back/lay prices from a Betfair market as YES/NO equivalents.

        For prediction market compatibility, back price maps to YES price
        and (1 / lay price) maps to the implied NO probability.

        For a two-runner market, returns the first runner's prices converted
        from decimal odds to probability (0-1) range.

        Args:
            market: Betfair runner dict from list_runners() or a market dict
                    with 'runners' key containing price data.

        Returns:
            (yes_price, no_price) in probability terms (0-1), or (None, None).
        """
        runners = market.get("runners", [market] if "ex" in market else [])
        if not runners:
            return None, None

        runner = runners[0] if isinstance(runners, list) else runners
        ex = runner.get("ex", {})

        back_prices = ex.get("availableToBack", [])
        lay_prices = ex.get("availableToLay", [])

        if not back_prices and not lay_prices:
            return None, None

        # Convert decimal odds to implied probability (0-1)
        yes_price = None
        no_price = None

        if back_prices:
            # Best back odds (highest price available to back)
            best_back = float(back_prices[0].get("price", 0))
            if best_back > 0:
                yes_price = 1.0 / best_back  # Implied probability

        if lay_prices:
            # Best lay odds (lowest price available to lay)
            best_lay = float(lay_prices[0].get("price", 0))
            if best_lay > 0:
                no_price = 1.0 - (1.0 / best_lay)  # Implied NO probability

        # If only one side available, derive the other
        if yes_price is not None and no_price is None:
            no_price = 1.0 - yes_price
        elif no_price is not None and yes_price is None:
            yes_price = 1.0 - no_price

        return yes_price, no_price
