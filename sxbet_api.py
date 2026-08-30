"""SX Bet Exchange API client for market data and trading."""

from collections.abc import Mapping
import logging
import os
import threading
import time

import requests

from config import SXBET_RATE_LIMIT
from rate_limiter import PlatformCircuitBreaker
from url_guard import assert_public_url

logger = logging.getLogger(__name__)

# Base URL: overridable via env var for reverse-proxy routing (e.g. Singapore)
SXBET_API_URL = assert_public_url(
    os.getenv("SXBET_API_BASE_URL", "https://api.sx.bet"),
    env_name="SXBET_API_BASE_URL",
    allow_http=False,
)

# Rate limiting (thread-safe)
_last_request_time = 0
_rate_lock = threading.Lock()

# HARDEN-04: circuit breaker — opens after 3 consecutive failures, resets after 30s
_circuit = PlatformCircuitBreaker("sxbet", fail_limit=3, reset_timeout=30.0)

# The API accepts up to 1,000 hashes semantically, but its HTTP edge returns
# 414 once the encoded request URI approaches 16 KiB. Two hundred 32-byte
# hashes stay below that live-verified limit with room for query overhead.
_MAX_ORDER_MARKETS_PER_REQUEST = 200


class _RateLimitError(Exception):
    """Raised when circuit is open — prevents further requests during backoff."""


def _orders_to_taker_book(orders: list[dict]) -> dict[str, list[dict]]:
    """Normalize maker orders into the prices and sizes available to a taker.

    SX Bet reports ``percentageOdds`` from the maker's perspective. A taker
    receives the complementary odds and bets the opposite outcome.
    """
    outcome_one = []
    outcome_two = []

    for order in orders:
        odds_raw = int(order.get("percentageOdds", 0))
        if odds_raw <= 0:
            continue

        maker_prob = odds_raw / 1e20
        if maker_prob <= 0 or maker_prob >= 1:
            continue

        remaining_maker = max(
            0,
            int(order.get("totalBetSize", 0))
            - int(order.get("fillAmount", 0))
            - int(order.get("pendingFillAmount", 0)),
        )
        taker_size = remaining_maker * (1 / maker_prob - 1) / 1e6
        entry = {"price": 1 - maker_prob, "size": taker_size}

        if order.get("isMakerBettingOutcomeOne", True):
            outcome_two.append(entry)
        else:
            outcome_one.append(entry)

    outcome_one.sort(key=lambda entry: entry["price"])
    outcome_two.sort(key=lambda entry: entry["price"])
    return {"bids": outcome_one, "asks": outcome_two}


def _rate_limit():
    global _last_request_time
    with _rate_lock:
        now = time.time()
        elapsed = now - _last_request_time
        if elapsed < SXBET_RATE_LIMIT:
            time.sleep(SXBET_RATE_LIMIT - elapsed)
        _last_request_time = time.time()


class SXBetClient:
    """SX Bet Exchange API client.

    Public market-data reads are unauthenticated. Account reads require the
    ``x-sx-api-key`` header, while trading additionally requires a wallet
    signature.
    """

    def __init__(self):
        self.session = requests.Session()
        # Note: SXBET_PROXY_URL (forward proxy) is deprecated in favor of
        # SXBET_API_BASE_URL (reverse proxy). Kept for backward compat.
        proxy_url = os.getenv("SXBET_PROXY_URL", "").strip().strip('"')
        if proxy_url:
            proxy_url = assert_public_url(proxy_url, env_name="SXBET_PROXY_URL")
            self.session.proxies = {"http": proxy_url, "https": proxy_url}
        self.session.headers.update({
            "Accept": "application/json",
            "Content-Type": "application/json",
        })
        proxy_token = os.getenv("SXBET_PROXY_TOKEN", "").strip()
        if SXBET_API_URL.rstrip("/") != "https://api.sx.bet" and proxy_token:
            self.session.headers["X-Proxy-Token"] = proxy_token
        self.api_key: str | None = None
        self.wallet_address: str | None = None
        self.authenticated = False

    def login(self, api_key: str | None = None, private_key: str | None = None) -> bool:
        """Connect to SX Bet and verify API reachability.

        Public SX Bet market-data endpoints require no authentication. The
        API key authenticates account reads and is distinct from the optional
        ``SXBET_WALLET_ADDRESS`` identifier. Private keys are deliberately not
        loaded until signed EIP-712 order support exists.

        Args:
            api_key: Account API key. Falls back to ``SXBET_API_KEY``.
            private_key: Ignored. Reserved for a future signed-order client.

        Returns:
            True if the SX Bet API is reachable.
        """
        self.api_key = api_key or os.getenv("SXBET_API_KEY")
        self.wallet_address = os.getenv("SXBET_WALLET_ADDRESS")
        if private_key or os.getenv("SXBET_PRIVATE_KEY"):
            logger.warning(
                "SXBET_PRIVATE_KEY is ignored while trading remains read-only"
            )

        # Verify by fetching sports list (no auth header needed)
        _rate_limit()
        try:
            resp = self.session.get(f"{SXBET_API_URL}/sports", timeout=15)
            if resp.status_code == 200:
                self.authenticated = True
                logger.info("SX Bet connected")
                return True
            logger.error("SX Bet API reachability check failed: %s", resp.status_code)
            return False
        except requests.RequestException as exc:
            logger.error("SX Bet API request failed: %s", exc)
            return False

    def _request(self, method: str, endpoint: str, params: dict = None,
                 json_data: dict = None) -> dict | None:
        """Make an API request.

        SX Bet read endpoints are unauthenticated.  No API key header is
        sent for normal requests.

        Args:
            method: HTTP method (GET, POST, etc.).
            endpoint: API path relative to base URL.
            params: Query parameters.
            json_data: JSON body payload.

        Returns:
            Response JSON or None on failure.
        """
        if _circuit.is_open():
            raise _RateLimitError("Circuit open -- sxbet in backoff")
        _rate_limit()
        try:
            url = f"{SXBET_API_URL}{endpoint}"
            resp = self.session.request(method, url, params=params,
                                        json=json_data, timeout=30)
            if resp.status_code == 200:
                _circuit.record_success()
                return resp.json()
            logger.warning("SX Bet %s %s returned %s: %s",
                           method, endpoint, resp.status_code, resp.text[:200])
            _circuit.record_failure()
            return None
        except requests.RequestException as exc:
            logger.warning("SX Bet %s %s failed: %s", method, endpoint, exc)
            _circuit.record_failure()
            return None

    def fetch_all_markets(self) -> list[dict]:
        """Fetch active markets from SX Bet with pagination and deduplication.

        Uses GET /markets/active with pagination (nextKey) to fetch all
        active markets. Deduplicates on marketHash since the API may return
        the same market under multiple sport categories.

        Returns:
            List of unique market dicts.
        """
        all_markets = []
        seen_hashes: set[str] = set()
        next_key = None

        while True:
            params: dict = {"pageSize": 50}
            if next_key:
                params["paginationKey"] = next_key

            market_data = self._request("GET", "/markets/active", params=params)
            if not market_data or "data" not in market_data:
                break

            data = market_data["data"]
            if isinstance(data, dict):
                markets_list = data.get("markets", [])
                next_key = data.get("nextKey")
            else:
                markets_list = data if isinstance(data, list) else []
                next_key = None

            if not markets_list:
                break

            for market in markets_list:
                if not isinstance(market, dict):
                    continue
                mh = market.get("marketHash", "")
                if mh and mh not in seen_hashes:
                    seen_hashes.add(mh)
                    all_markets.append(market)

            # Stop if no more pages
            if not next_key:
                break

        logger.info("SX Bet: fetched %d unique active markets", len(all_markets))
        return all_markets

    def health_check(self) -> dict | None:
        """Probe the cheap public sports endpoint for service reachability."""
        return self._request("GET", "/sports")

    def get_market_price(self, market: dict) -> tuple[float | None, float | None]:
        """Extract best back/lay prices as YES/NO probabilities.

        SX Bet prices are already in the 0-1 probability range.

        Args:
            market: Market dict with a ``marketHash`` key.

        Returns:
            (yes_price, no_price) in probability terms (0-1), or (None, None).
        """
        market_hash = market.get("marketHash", "")
        if not market_hash:
            return None, None

        orderbook = self.get_orderbook(market_hash)
        if not orderbook:
            return None, None

        yes_price = orderbook["bids"][0]["price"] if orderbook["bids"] else None
        no_price = orderbook["asks"][0]["price"] if orderbook["asks"] else None
        return yes_price, no_price

    def list_runners(self, market_hash: str) -> list[dict]:
        """Return outcomes for a market from its cached market data.

        SX Bet markets are binary (outcomeOne / outcomeTwo), so this returns
        a synthetic list matching the scan module's expectations.

        Args:
            market_hash: SX Bet market hash.

        Returns:
            List of outcome dicts with 'name' key.
        """
        # SX Bet binary markets embed outcome names in the market dict itself.
        # Multi-outcome markets are not supported on SX Bet.
        return [{"name": "Outcome 1"}, {"name": "Outcome 2"}]

    def get_orderbook(self, market_hash: str) -> dict | None:
        """Fetch active resting orders for a market (order book equivalent).

        Uses GET /orders?marketHashes={hash} since SX Bet has no dedicated
        orderbook endpoint.

        Args:
            market_hash: SX Bet market hash.

        Returns:
            Dict with 'bids' and 'asks' lists (normalized from raw orders),
            or None on failure.
        """
        data = self._request("GET", "/orders", params={"marketHashes": market_hash})
        if not data or "data" not in data:
            return None

        orders = data["data"]
        return _orders_to_taker_book(orders)

    def get_orderbooks_batch(self, market_hashes: list[str], batch_size: int = 200) -> dict[str, dict]:
        """Fetch order books for multiple markets in batched API calls.

        Sends comma-separated marketHashes to GET /orders to minimize API calls.
        Returns a dict mapping market_hash → orderbook (bids/asks).
        """
        result: dict[str, dict] = {}
        safe_batch_size = min(max(1, batch_size), _MAX_ORDER_MARKETS_PER_REQUEST)
        for i in range(0, len(market_hashes), safe_batch_size):
            batch = market_hashes[i:i + safe_batch_size]
            hashes_param = ",".join(batch)
            try:
                data = self._request("GET", "/orders", params={"marketHashes": hashes_param})
            except _RateLimitError as exc:
                logger.warning("SX Bet order-book batch fetch stopped: %s", exc)
                break
            if not data or "data" not in data:
                continue

            # Group orders by marketHash
            by_market: dict[str, list] = {}
            for order in data["data"]:
                mh = order.get("marketHash", "")
                if mh not in by_market:
                    by_market[mh] = []
                by_market[mh].append(order)

            # Parse each market's maker orders into executable taker prices.
            for mh, orders in by_market.items():
                result[mh] = _orders_to_taker_book(orders)

            # Markets with no orders get empty books
            for mh in batch:
                if mh not in result:
                    result[mh] = {"bids": [], "asks": []}

        return result

    def place_order(self, market_hash: str, outcome_id: str, side: str,
                    price: float, size: float) -> dict | None:
        """Place an order on SX Bet.

        NOTE: This sends unsigned JSON — real SX Bet trading requires
        Ethereum wallet signatures (EIP-191/EIP-712) which are not yet
        implemented.  Orders will be rejected by the API.  SX Bet is
        effectively read-only until order signing is added.

        Args:
            market_hash: SX Bet market hash.
            outcome_id: Outcome/runner ID.
            side: 'buy' or 'sell'.
            price: Price in probability (0-1).
            size: Stake amount.

        Returns:
            Order response dict or None on failure.
        """
        logger.error(
            "SX Bet order rejected locally: EIP-712 signing is not implemented"
        )
        return None

    def get_balance(self) -> float | None:
        """Get available account balance.

        The v3 balance endpoint requires the account API key in the
        ``x-sx-api-key`` header. ``availableAmount`` is denominated in the
        token's six-decimal base units.

        Returns:
            Balance as float or None on failure.
        """
        if not self.authenticated or not self.api_key:
            return None

        _rate_limit()
        try:
            resp = self.session.get(
                f"{SXBET_API_URL}/user/balance-v3",
                headers={"x-sx-api-key": self.api_key},
                timeout=15,
            )
            if resp.status_code == 200:
                payload = resp.json()
                if not isinstance(payload, Mapping):
                    logger.debug("SX Bet balance response root was not an object")
                    return None
                data = payload.get("data")
                if not isinstance(data, Mapping):
                    logger.debug("SX Bet balance response data was not an object")
                    return None
                balances = data.get("balances")
                if not isinstance(balances, list) or not balances:
                    logger.debug("SX Bet balance response contained no balances")
                    return None
                available = sum(float(row["availableAmount"]) for row in balances)
                return available / 1_000_000
            logger.debug("SX Bet balance unavailable: %s", resp.status_code)
            return None
        except (KeyError, TypeError, ValueError, requests.RequestException) as exc:
            logger.debug("SX Bet balance request failed: %s", exc)
            return None

    def get_order_status(self, order_id: str) -> dict | None:
        """Get status of a specific order.

        Args:
            order_id: SX Bet order ID.

        Returns:
            Order dict or None.
        """
        if not self.authenticated:
            return None
        return self._request("GET", f"/orders/{order_id}")

    def get_market_status(self, market_hash: str) -> dict | None:
        """Get the current status of a market.

        Args:
            market_hash: SX Bet market hash.

        Returns:
            Market dict with status info, or None on failure.
        """
        return self._request("GET", f"/markets/{market_hash}")

    def cancel_order(self, order_id: str) -> bool:
        """Cancel an order.

        Args:
            order_id: SX Bet order ID.

        Returns:
            True if cancellation succeeded.
        """
        if not self.authenticated or not self.api_key:
            return False
        _rate_limit()
        try:
            resp = self.session.delete(
                f"{SXBET_API_URL}/orders/{order_id}",
                timeout=30,
            )
            return resp.status_code in (200, 204)
        except requests.RequestException as exc:
            logger.warning("SX Bet cancel_order failed: %s", exc)
            return False
