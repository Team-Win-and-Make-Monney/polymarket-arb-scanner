"""Smarkets Exchange API client for market data and trading."""

import logging
import os
import threading
import time

import requests
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type

from config import SMARKETS_RATE_LIMIT
from rate_limiter import PlatformCircuitBreaker
from url_guard import assert_public_url

logger = logging.getLogger(__name__)


class _RateLimitError(Exception):
    """Raised when Smarkets returns HTTP 429 — triggers tenacity retry."""


SMARKETS_API_URL = "https://api.smarkets.com/v3"
SMARKETS_AUTH_URL = "https://api.smarkets.com/v3/sessions/"

# Rate limiting (thread-safe)
_last_request_time = 0
_rate_lock = threading.Lock()

# HARDEN-04: circuit breaker — opens after 3 consecutive failures, resets after 30s
_circuit = PlatformCircuitBreaker("smarkets", fail_limit=3, reset_timeout=30.0)


def _rate_limit():
    global _last_request_time
    with _rate_lock:
        now = time.time()
        elapsed = now - _last_request_time
        if elapsed < SMARKETS_RATE_LIMIT:
            time.sleep(SMARKETS_RATE_LIMIT - elapsed)
        _last_request_time = time.time()


class SmarketsClient:
    """Smarkets Exchange API client."""

    def __init__(self):
        self.session = requests.Session()
        proxy_url = os.getenv("SMARKETS_PROXY_URL")
        if proxy_url:
            proxy_url = assert_public_url(proxy_url, env_name="SMARKETS_PROXY_URL")
            self.session.proxies = {"http": proxy_url, "https": proxy_url}
        self.token = None
        self.authenticated = False

    def login(self, api_key: str = None) -> bool:
        """Authenticate with Smarkets API token.

        Smarkets uses API keys (OAuth tokens) for auth.
        Falls back to SMARKETS_API_KEY env var.

        Args:
            api_key: Smarkets API key/token.

        Returns:
            True if authentication succeeded.
        """
        api_key = api_key or os.getenv("SMARKETS_API_KEY")
        if not api_key:
            logger.error("Smarkets API key not provided")
            return False

        self.token = api_key
        self.session.headers.update({
            "Authorization": f"Bearer {api_key}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        })

        # Verify token by fetching account info
        _rate_limit()
        try:
            resp = self.session.get(f"{SMARKETS_API_URL}/accounts/self/", timeout=15)
            if resp.status_code == 200:
                self.authenticated = True
                return True
            logger.error("Smarkets auth failed: %s", resp.status_code)
            return False
        except requests.RequestException as exc:
            logger.error("Smarkets auth request failed: %s", exc)
            return False

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=1, max=10),
        retry=retry_if_exception_type((requests.ConnectionError, requests.Timeout, _RateLimitError)),
        reraise=True,
    )
    def _request(
        self,
        method: str,
        endpoint: str,
        params: dict | None = None,
        json_data: dict | None = None,
        success_codes: tuple[int, ...] = (200,),
        expect_json: bool = True,
    ) -> dict | None:
        """Make an authenticated API request.

        Args:
            method: HTTP method (GET, POST, DELETE, etc.).
            endpoint: API path relative to base URL.
            params: Query parameters.
            json_data: JSON body payload.
            success_codes: Status codes treated as success. Defaults to
                (200,). POST endpoints that return 201 (place_order) or
                DELETE endpoints that return 204 (cancel_order) widen
                this. PR G — previously these bypassed the retry
                decorator entirely.
            expect_json: When False, return ``{}`` on success without
                attempting ``resp.json()``. Used by DELETE 204 responses
                that have no body.

        Returns:
            Response JSON (or ``{}`` for non-JSON 2xx) on success, or
            None on failure.
        """
        if not self.authenticated:
            logger.error("Smarkets: must login before making API calls")
            return None

        if _circuit.is_open():
            raise _RateLimitError("Circuit open -- smarkets in backoff")
        _rate_limit()
        try:
            url = f"{SMARKETS_API_URL}{endpoint}"
            resp = self.session.request(method, url, params=params,
                                        json=json_data, timeout=30)
            if resp.status_code in success_codes:
                _circuit.record_success()
                if not expect_json:
                    return {}
                try:
                    return resp.json()
                except ValueError:
                    # Empty body on a 2xx — treat as success but no payload.
                    return {}
            if resp.status_code == 429:
                logger.warning("Smarkets rate limited on %s %s, retrying...", method, endpoint)
                raise _RateLimitError(f"Smarkets 429 on {method} {endpoint}")
            logger.warning("Smarkets %s %s returned %s: %s",
                           method, endpoint, resp.status_code, resp.text[:200])
            return None
        except (requests.ConnectionError, requests.Timeout, _RateLimitError):
            _circuit.record_failure()
            raise  # Let tenacity retry these
        except requests.RequestException as exc:
            logger.warning("Smarkets %s %s failed: %s", method, endpoint, exc)
            return None

    def fetch_all_markets(self) -> list[dict]:
        """Fetch active markets from Smarkets.

        Smarkets organizes markets under events. Fetches politics/current affairs
        events and their markets for prediction market relevance.

        Returns:
            List of market dicts, each with an attached ``_event`` parent.
        """
        all_markets = []

        # Fetch events (politics type)
        data = self._request("GET", "/events/", params={
            "state": "upcoming,live",
            "type_domain": "politics",
            "type_scope": "single_event",
            "limit": 100,
            "sort": "id",
        })

        if not data or "events" not in data:
            return all_markets

        events = data.get("events", [])
        for event in events:
            event_id = event.get("id")
            if not event_id:
                continue

            # Fetch markets for this event
            market_data = self._request("GET", f"/events/{event_id}/markets/")
            if market_data and "markets" in market_data:
                for market in market_data["markets"]:
                    market["_event"] = event  # attach parent event
                    all_markets.append(market)

        return all_markets

    def get_market_prices(self, market_id: str) -> dict | None:
        """Fetch current prices (quotes) for a market.

        Args:
            market_id: Smarkets market ID.

        Returns:
            Dict with runner quotes or None.
        """
        data = self._request("GET", f"/markets/{market_id}/quotes/")
        return data

    def get_market_price(self, market: dict) -> tuple[float | None, float | None]:
        """Extract best back/lay prices as YES/NO probabilities.

        Smarkets returns prices as percentage (0-100). Convert to 0-1.
        For binary markets, returns (yes_price, no_price) in probability terms.

        Args:
            market: Market dict containing at least an ``id`` key.

        Returns:
            (yes_price, no_price) in probability terms (0-1), or (None, None).
        """
        market_id = market.get("id", "")
        if not market_id:
            return None, None

        quotes = self.get_market_prices(market_id)
        if not quotes:
            return None, None

        # Smarkets quotes format: list of contract quotes with bid/offer
        contracts = quotes.get("quotes", [])
        if not contracts:
            return None, None

        contract = contracts[0] if isinstance(contracts, list) else contracts

        # Prices in Smarkets are percentage (e.g., 45 = 45%)
        best_back = contract.get("best_available_to_back")
        best_lay = contract.get("best_available_to_lay")

        if not best_back and not best_lay:
            return None, None

        yes_price = None
        no_price = None

        if best_back:
            price_pct = best_back.get("price")
            if price_pct and float(price_pct) > 0:
                yes_price = float(price_pct) / 100.0

        if best_lay:
            price_pct = best_lay.get("price")
            if price_pct and float(price_pct) > 0:
                no_price = 1.0 - (float(price_pct) / 100.0)

        if yes_price is not None and no_price is None:
            no_price = 1.0 - yes_price
        elif no_price is not None and yes_price is None:
            yes_price = 1.0 - no_price

        return yes_price, no_price

    def list_runners(self, market_id: str) -> list[dict]:
        """Fetch runners/contracts for a market with price data.

        Args:
            market_id: Smarkets market ID.

        Returns:
            List of contract/runner dicts.
        """
        data = self._request("GET", f"/markets/{market_id}/contracts/")
        if data and "contracts" in data:
            return data["contracts"]
        return []

    def place_order(self, market_id: str, contract_id: str, side: str,
                    price: float, quantity: float) -> dict | None:
        """Place an order on Smarkets.

        Routed through ``self._request`` so 429 retries + circuit
        breaker apply (PR G fix — previously bypassed both via direct
        ``self.session.post``).

        Args:
            market_id: Smarkets market ID.
            contract_id: Contract/runner ID.
            side: 'buy' or 'sell'.
            price: Price in probability (0-1), converted to basis points for API.
            quantity: Stake amount.

        Returns:
            Order response dict or None on failure.
        """
        return self._request(
            "POST",
            "/orders/",
            json_data={
                "market_id": market_id,
                "contract_id": contract_id,
                "side": side,
                "price": str(int(price * 10000)),  # Smarkets uses basis points
                "quantity": str(int(quantity * 100)),  # In cents
                "type": "limit",
            },
            success_codes=(200, 201),
        )

    def get_balance(self) -> float | None:
        """Get available account balance.

        Returns:
            Balance in dollars (converted from cents) or None.
        """
        if not self.authenticated:
            return None
        data = self._request("GET", "/accounts/self/")
        if data:
            return float(data.get("available_balance", 0)) / 100.0  # cents to dollars
        return None

    def get_order_status(self, order_id: str) -> dict | None:
        """Get status of a specific order.

        Args:
            order_id: Smarkets order ID.

        Returns:
            Order dict or None.
        """
        if not self.authenticated:
            return None
        return self._request("GET", f"/orders/{order_id}/")

    def get_market_status(self, market_id: str) -> dict | None:
        """Get the current status of a market (open, settled, closed).

        Args:
            market_id: Smarkets market ID.

        Returns:
            Market dict with state info, or None on failure.
        """
        return self._request("GET", f"/markets/{market_id}/")

    def cancel_order(self, order_id: str) -> bool:
        """Cancel an order.

        Routed through ``self._request`` (PR G fix). Smarkets returns
        204 on a successful DELETE so we widen ``success_codes`` and
        skip JSON decoding.

        Args:
            order_id: Smarkets order ID.

        Returns:
            True if cancellation succeeded.
        """
        result = self._request(
            "DELETE",
            f"/orders/{order_id}/",
            success_codes=(200, 204),
            expect_json=False,
        )
        return result is not None
