"""Gemini Predictions API client with HMAC-SHA384 authentication."""

import base64
import hashlib
import hmac
import json
import logging
import os
import threading
import time

import requests
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type

from config import GEMINI_RATE_LIMIT
from rate_limiter import PlatformCircuitBreaker
from url_guard import assert_public_url

logger = logging.getLogger(__name__)


class _RateLimitError(Exception):
    """Raised when Gemini returns HTTP 429 — triggers tenacity retry."""
    pass


GEMINI_BASE_URL = os.getenv("GEMINI_BASE_URL", "https://api.gemini.com")

# Rate limiting (thread-safe)
_last_request_time = 0
_rate_lock = threading.Lock()

# Nonce generation: Gemini requires strictly-monotonic nonces per API key.
# Millisecond precision + counter guarantees uniqueness under concurrent calls.
_nonce_lock = threading.Lock()
_last_nonce = 0


def _next_nonce() -> str:
    """Return a strictly-monotonic nonce as string.

    Gemini's server enforces a 30-second window check that interprets the
    nonce as seconds regardless of its magnitude, so we use second-precision
    and bump by 1 on same-second collisions. This stays correct under
    concurrent calls while keeping nonces within the server window.
    """
    global _last_nonce
    with _nonce_lock:
        now_s = int(time.time())
        nxt = max(now_s, _last_nonce + 1)
        _last_nonce = nxt
    return str(nxt)


# HARDEN-04: circuit breaker — opens after 3 consecutive failures, resets after 30s
_circuit = PlatformCircuitBreaker("gemini", fail_limit=3, reset_timeout=30.0)


def _rate_limit():
    global _last_request_time
    with _rate_lock:
        now = time.time()
        elapsed = now - _last_request_time
        if elapsed < GEMINI_RATE_LIMIT:
            time.sleep(GEMINI_RATE_LIMIT - elapsed)
        _last_request_time = time.time()


class GeminiClient:
    """Gemini Predictions API client with HMAC-SHA384 signed authentication.

    Auth uses three headers:
    - X-GEMINI-APIKEY: the API key
    - X-GEMINI-PAYLOAD: base64-encoded JSON with ``request`` path + ``nonce``
    - X-GEMINI-SIGNATURE: hex HMAC-SHA384 of the payload using the API secret
    """

    def __init__(self):
        self.session = requests.Session()
        proxy_url = os.getenv("GEMINI_PROXY_URL")
        if proxy_url:
            self.session.proxies = {"http": proxy_url, "https": proxy_url}
        self.api_key = None
        self.api_secret = None
        self.authenticated = False
        self.base_url = assert_public_url(GEMINI_BASE_URL, env_name="GEMINI_BASE_URL", allow_http=False)
        self._account = None  # Set to "primary" for master API keys
        # Markets cache: shared between binary + multi scans within a cycle.
        self._markets_cache: list[dict] | None = None
        self._markets_cache_key: tuple | None = None
        self._markets_cache_ts: float = 0.0
        self._markets_cache_ttl: float = float(os.getenv("GEMINI_MARKETS_CACHE_TTL", "20"))
        self._markets_cache_lock = threading.Lock()

    def login(self, api_key: str = None, api_secret: str = None) -> bool:
        """Store credentials and verify connectivity.

        Args:
            api_key: Gemini API key (falls back to GEMINI_API_KEY env var).
            api_secret: Gemini API secret (falls back to GEMINI_API_SECRET env var).

        Returns:
            True if credentials are valid and API is reachable.
        """
        self.api_key = api_key or os.getenv("GEMINI_API_KEY")
        self.api_secret = api_secret or os.getenv("GEMINI_API_SECRET")

        if not self.api_key or not self.api_secret:
            logger.error("Gemini credentials not provided")
            return False

        # Master API keys require account param on all private requests
        if self.api_key.startswith("master-"):
            self._account = "primary"

        # Verify credentials by calling /v1/balances (returns [] if unfunded)
        try:
            result = self._private_request("/v1/balances")
            if isinstance(result, list):
                self.authenticated = True
                logger.info("Gemini authenticated successfully.")
                return True
            logger.error("Gemini auth verification failed: unexpected response %s", result)
            return False
        except Exception as e:
            logger.error("Gemini login failed: %s", e)
            return False

    def _sign_request(self, endpoint: str, payload_data: dict = None) -> dict:
        """Build signed headers for a private API request.

        Args:
            endpoint: API path (e.g. ``/v1/balances``).
            payload_data: Additional fields to include in the signed payload.

        Returns:
            Dict of signed headers.
        """
        payload_data = payload_data or {}
        payload_data["request"] = endpoint
        payload_data["nonce"] = _next_nonce()
        if self._account and "account" not in payload_data:
            payload_data["account"] = self._account

        payload_json = json.dumps(payload_data)
        payload_b64 = base64.b64encode(payload_json.encode("utf-8"))
        signature = hmac.new(
            self.api_secret.encode("utf-8"),
            payload_b64,
            hashlib.sha384,
        ).hexdigest()

        return {
            "Content-Type": "text/plain",
            "Content-Length": "0",
            "X-GEMINI-APIKEY": self.api_key,
            "X-GEMINI-PAYLOAD": payload_b64.decode("utf-8"),
            "X-GEMINI-SIGNATURE": signature,
            "Cache-Control": "no-cache",
        }

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=1, max=10),
        retry=retry_if_exception_type((_RateLimitError, requests.ConnectionError, requests.Timeout)),
        reraise=True,
    )
    def _private_request(self, endpoint: str, payload_data: dict = None) -> dict | None:
        """Make an authenticated POST request (Gemini private endpoints use POST).

        Args:
            endpoint: API path (e.g. ``/v1/balances``).
            payload_data: Additional fields for the signed payload.

        Returns:
            Response JSON or None on failure.
        """
        if _circuit.is_open():
            raise _RateLimitError("Circuit open -- gemini in backoff")
        _rate_limit()
        headers = self._sign_request(endpoint, payload_data)
        try:
            url = f"{self.base_url}{endpoint}"
            resp = self.session.post(url, headers=headers, timeout=30)
            if resp.status_code == 200:
                _circuit.record_success()
                return resp.json()
            if resp.status_code == 429:
                logger.warning("Gemini rate limited on %s, retrying...", endpoint)
                raise _RateLimitError(f"Gemini 429 on {endpoint}")
            logger.warning("Gemini %s returned %s: %s",
                           endpoint, resp.status_code, resp.text[:200])
            return None
        except (requests.exceptions.ConnectionError, _RateLimitError):
            _circuit.record_failure()
            raise
        except requests.RequestException as exc:
            logger.warning("Gemini %s failed: %s", endpoint, exc)
            return None

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=1, max=10),
        retry=retry_if_exception_type((_RateLimitError, requests.ConnectionError, requests.Timeout)),
        reraise=True,
    )
    def _public_request(self, endpoint: str, params: dict = None) -> dict | list | None:
        """Make an unauthenticated GET request.

        Args:
            endpoint: API path (e.g. ``/v1/prediction-markets/events``).
            params: Query parameters.

        Returns:
            Response JSON or None on failure.
        """
        _rate_limit()
        try:
            url = f"{self.base_url}{endpoint}"
            resp = self.session.get(url, params=params, timeout=30)
            if resp.status_code == 200:
                return resp.json()
            if resp.status_code == 429:
                logger.warning("Gemini rate limited on %s, retrying...", endpoint)
                raise _RateLimitError(f"Gemini 429 on {endpoint}")
            logger.warning("Gemini GET %s returned %s: %s",
                           endpoint, resp.status_code, resp.text[:200])
            return None
        except requests.exceptions.ConnectionError:
            raise
        except requests.RequestException as exc:
            logger.warning("Gemini GET %s failed: %s", endpoint, exc)
            return None

    def fetch_all_markets(self, status: str = "active", category: str = None) -> list[dict]:
        """Fetch all prediction market events with pagination.

        Caches results in-memory for ``GEMINI_MARKETS_CACHE_TTL`` seconds
        (default 20s) so binary + multi scans in the same cycle share one
        fetch. Cache is keyed by (status, category).

        Args:
            status: Filter by event status (default ``"active"``).
            category: Optional category filter.

        Returns:
            Normalized list of event dicts with contracts.
        """
        cache_key = (status, category)
        with self._markets_cache_lock:
            if (
                self._markets_cache is not None
                and self._markets_cache_key == cache_key
                and (time.time() - self._markets_cache_ts) < self._markets_cache_ttl
            ):
                return self._markets_cache

        all_events = []
        offset = 0
        limit = 100

        while True:
            params = {"status": status, "limit": limit, "offset": offset}
            if category:
                params["category"] = category

            data = self._public_request("/v1/prediction-markets/events", params=params)
            if not data:
                break

            events = data if isinstance(data, list) else data.get("data", data.get("events", []))
            if not events:
                break

            for event in events:
                normalized = self._normalize_event(event)
                if normalized:
                    all_events.append(normalized)

            if len(events) < limit:
                break
            offset += limit

        logger.info("Fetched %d Gemini prediction market events.", len(all_events))
        with self._markets_cache_lock:
            self._markets_cache = all_events
            self._markets_cache_key = cache_key
            self._markets_cache_ts = time.time()
        return all_events

    def invalidate_markets_cache(self) -> None:
        """Force the next fetch_all_markets call to re-fetch from the API."""
        with self._markets_cache_lock:
            self._markets_cache = None
            self._markets_cache_ts = 0.0

    def _normalize_event(self, event: dict) -> dict | None:
        """Normalize a Gemini event into a standard format.

        Gemini binary events expose a single contract whose ``prices`` object
        carries both yes and no buy prices; the NO side trades through the
        same instrumentSymbol with ``outcome="no"``. This normalizer
        synthesizes explicit yes/no contracts for binary events so the rest
        of the scanner can treat binary events uniformly.

        Returns:
            Dict with keys: id, title, type, category, contracts, status.
        """
        event_id = event.get("eventTicker") or event.get("id") or event.get("ticker", "")
        title = event.get("title") or event.get("name", "")
        if not event_id or not title:
            return None

        event_type = event.get("type") or ("binary" if len(event.get("contracts", [])) == 2 else "categorical")

        contracts = []
        raw_contracts = event.get("contracts", event.get("markets", []))

        if event_type == "binary" and len(raw_contracts) == 1:
            # Gemini binary events: synthesize yes + no contracts from the
            # single contract's prices.buy.{yes,no}.
            c = raw_contracts[0]
            prices_obj = c.get("prices") if isinstance(c.get("prices"), dict) else {}
            buy = prices_obj.get("buy") if isinstance(prices_obj.get("buy"), dict) else {}

            def _to_float(raw):
                try:
                    return float(raw) if raw is not None else None
                except (ValueError, TypeError):
                    return None

            yes_price = _to_float(buy.get("yes")) or _to_float(prices_obj.get("bestAsk"))
            no_price = _to_float(buy.get("no"))
            if yes_price is not None and no_price is None:
                no_price = round(1.0 - yes_price, 6)

            symbol = c.get("instrumentSymbol") or c.get("symbol", "")
            contract_id = c.get("contractId") or c.get("id", "")

            contracts.append({
                "id": contract_id,
                "label": "Yes",
                "price": yes_price,
                "instrumentSymbol": symbol,
                "outcome": "yes",
                "_prices": prices_obj,
            })
            contracts.append({
                "id": contract_id,  # Same contract, NO leg trades through same symbol
                "label": "No",
                "price": no_price,
                "instrumentSymbol": symbol,
                "outcome": "no",
                "_prices": prices_obj,
            })
        else:
            for c in raw_contracts:
                # Extract price from nested prices object or flat fields
                price = None
                prices_obj = c.get("prices")
                if isinstance(prices_obj, dict):
                    # Prefer bestAsk for buy-side scanning, fall back to lastTradePrice
                    raw = prices_obj.get("bestAsk") or prices_obj.get("lastTradePrice")
                    if raw is not None:
                        try:
                            price = float(raw)
                        except (ValueError, TypeError):
                            pass
                if price is None:
                    raw = c.get("lastPrice") or c.get("price")
                    if raw is not None:
                        try:
                            price = float(raw)
                        except (ValueError, TypeError):
                            pass

                contract = {
                    "id": c.get("contractId") or c.get("id", ""),
                    "label": c.get("label") or c.get("title") or c.get("outcome", ""),
                    "price": price,
                    "instrumentSymbol": c.get("instrumentSymbol") or c.get("symbol", ""),
                    "outcome": c.get("outcome", ""),
                }
                if isinstance(prices_obj, dict):
                    contract["_prices"] = prices_obj
                contracts.append(contract)

        return {
            "id": event_id,
            "title": title,
            "type": event_type,
            "category": event.get("category", ""),
            "contracts": contracts,
            "status": event.get("status", "active"),
        }

    def get_market_price(self, event: dict) -> tuple[float | None, float | None]:
        """Extract YES/NO prices from a normalized Gemini event.

        For binary events: returns (yes_price, no_price) directly from contracts.
        For categorical: returns (None, None) — handled by multi scan.

        Args:
            event: Normalized event dict from ``fetch_all_markets()``.

        Returns:
            (yes_price, no_price) in 0-1 range, or (None, None).
        """
        contracts = event.get("contracts", [])
        if len(contracts) != 2:
            return None, None

        yes_price = None
        no_price = None

        for c in contracts:
            label = (c.get("label") or c.get("outcome") or "").lower()
            price = c.get("price")
            if price is None:
                continue
            price = float(price)
            if "yes" in label:
                yes_price = price
            elif "no" in label:
                no_price = price

        # If labels don't contain yes/no, assume first=yes, second=no
        if yes_price is None and no_price is None and len(contracts) == 2:
            p0 = contracts[0].get("price")
            p1 = contracts[1].get("price")
            if p0 is not None and p1 is not None:
                yes_price = float(p0)
                no_price = float(p1)

        return yes_price, no_price

    def get_order_book(self, instrument_symbol: str, limit: int = 50) -> dict | None:
        """Fetch order book for an instrument.

        Args:
            instrument_symbol: Gemini instrument symbol (e.g. ``"BTCPRED-YES"``).
            limit: Max number of price levels.

        Returns:
            Dict with ``bids`` and ``asks`` lists, or None.
        """
        data = self._public_request(f"/v1/book/{instrument_symbol}",
                                     params={"limit_bids": limit, "limit_asks": limit})
        if not data:
            return None

        return {
            "bids": [
                {"price": float(b.get("price", 0)), "amount": float(b.get("amount", 0))}
                for b in data.get("bids", [])
            ],
            "asks": [
                {"price": float(a.get("price", 0)), "amount": float(a.get("amount", 0))}
                for a in data.get("asks", [])
            ],
        }

    def place_order(
        self,
        symbol: str,
        side: str,
        outcome: str,
        quantity: int,
        price: float,
        time_in_force: str = "immediate-or-cancel",
    ) -> dict | None:
        """Place a prediction market order.

        Args:
            symbol: Instrument symbol.
            side: ``"buy"`` or ``"sell"``.
            outcome: ``"yes"`` or ``"no"``.
            quantity: Number of contracts.
            price: Limit price (0-1).
            time_in_force: ``"immediate-or-cancel"`` or ``"good-til-cancelled"``.

        Returns:
            Order response dict or None on failure.
        """
        if not self.authenticated:
            logger.error("Gemini: must login before placing orders")
            return None

        payload = {
            "symbol": symbol,
            "side": side,
            "outcome": outcome,
            "quantity": str(quantity),
            "price": str(price),
            "type": "exchange limit",
            "options": [time_in_force],
        }
        return self._private_request("/v1/prediction-markets/order", payload_data=payload)

    def get_order_status(self, order_id: str) -> dict | None:
        """Get the status of an order.

        Checks active orders first, then history for filled/cancelled.

        Args:
            order_id: Order ID to look up.

        Returns:
            Order dict or None.
        """
        if not self.authenticated:
            return None

        # Check active orders
        active = self._private_request("/v1/prediction-markets/orders/active")
        if active and isinstance(active, list):
            for order in active:
                if str(order.get("orderId", "")) == str(order_id):
                    return order

        # Check order history
        history = self._private_request("/v1/prediction-markets/orders/history",
                                         payload_data={"limit": 50})
        if history and isinstance(history, list):
            for order in history:
                if str(order.get("orderId", "")) == str(order_id):
                    return order

        return None

    def cancel_order(self, order_id: str) -> bool:
        """Cancel an open order.

        Args:
            order_id: Order ID to cancel.

        Returns:
            True if cancellation succeeded.
        """
        if not self.authenticated:
            return False

        resp = self._private_request("/v1/prediction-markets/order/cancel",
                                      payload_data={"orderId": order_id})
        if resp and resp.get("isCancelled"):
            return True
        return resp is not None

    def get_balance(self) -> float | None:
        """Get available USD balance.

        Returns:
            Balance in dollars or None on failure.
        """
        data = self._private_request("/v1/balances")
        if not data or not isinstance(data, list):
            return None

        for entry in data:
            if entry.get("currency", "").upper() == "USD":
                return float(entry.get("available", entry.get("amount", 0)))

        return None

    def withdraw_usdc(self, address: str, amount: float) -> dict | None:
        """Withdraw USDC to an external address (Strategy #18).

        Calls Gemini's ``/v1/withdraw/usdc`` endpoint. Caller is responsible
        for verifying the destination address (e.g. the Polymarket proxy
        wallet) and applying treasury risk gates upstream.

        Args:
            address: External wallet address to receive USDC.
            amount: Amount in dollars (Gemini accepts a string).

        Returns:
            Gemini response dict (typically includes ``txHash`` and
            ``destination``), or None on failure.
        """
        if not address:
            logger.warning("withdraw_usdc called with empty destination address")
            return None
        if amount <= 0:
            logger.warning("withdraw_usdc called with non-positive amount: %s",
                           amount)
            return None
        payload = {
            "address": address,
            "amount": f"{amount:.6f}",
        }
        return self._private_request("/v1/withdraw/usdc",
                                      payload_data=payload)

    def get_positions(self) -> list[dict]:
        """Get open prediction market positions.

        Returns:
            List of position dicts.
        """
        if not self.authenticated:
            return []
        data = self._private_request("/v1/prediction-markets/positions")
        if data and isinstance(data, list):
            return data
        return []

    def get_market_status(self, event_ticker: str) -> dict | None:
        """Get event status for settlement detection.

        Args:
            event_ticker: Gemini event ticker.

        Returns:
            Event dict with status info, or None.
        """
        return self._public_request(f"/v1/prediction-markets/events/{event_ticker}")
