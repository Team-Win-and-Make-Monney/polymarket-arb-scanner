"""Limitless Predictions API client with EIP-712 order signing.

Integrates with Limitless Exchange (Base CLOB) for market discovery,
orderbook depth, reward program data, and EIP-712 signed order placement.
"""

import logging
import os
import threading
import time

import requests

from eip712_signer import normalize_private_key, sign_order
from rate_limiter import PlatformCircuitBreaker
from url_guard import assert_public_url

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants & Circuit Breaker
# ---------------------------------------------------------------------------

LIMITLESS_BASE_URL = os.getenv("LIMITLESS_BASE_URL", "https://api.limitless.exchange")
LIMITLESS_RATE_LIMIT = float(os.getenv("LIMITLESS_RATE_LIMIT", "0.2"))  # 5 req/sec

# Circuit breaker: opens after 3 consecutive failures, resets after 30s
_circuit = PlatformCircuitBreaker("limitless", fail_limit=3, reset_timeout=30.0)


def _rate_limit() -> None:
    """Enforce rate limit between outgoing HTTP requests."""
    LimitlessClient._rate_limit()


# ---------------------------------------------------------------------------
# LimitlessClient
# ---------------------------------------------------------------------------


class LimitlessClient:
    """Client for Limitless prediction market CLOB on Base.

    Supports reading public markets and orderbooks, querying liquidity reward
    programs, and submitting EIP-712 typed-data signed limit orders.
    """

    _last_request_time: float = 0.0
    _rate_lock = threading.Lock()

    @classmethod
    def _rate_limit(cls) -> None:
        """Enforce rate limit between outgoing HTTP requests."""
        with cls._rate_lock:
            now = time.time()
            elapsed = now - cls._last_request_time
            if elapsed < LIMITLESS_RATE_LIMIT:
                time.sleep(LIMITLESS_RATE_LIMIT - elapsed)
            cls._last_request_time = time.time()

    def __init__(self, base_url: str | None = None):
        self.session = requests.Session()
        proxy_url = os.getenv("LIMITLESS_PROXY_URL")
        if proxy_url:
            self.session.proxies = {"http": proxy_url, "https": proxy_url}

        raw_url = base_url or LIMITLESS_BASE_URL
        self.base_url = assert_public_url(raw_url, env_name="LIMITLESS_BASE_URL", allow_http=False)

        self.api_key: str | None = None
        self.private_key: str | None = None
        self.authenticated: bool = False
        self.dry_run: bool = os.getenv("DRY_RUN", "true").lower() in ("true", "1", "yes")

        self.chain_id: int = int(os.getenv("LIMITLESS_CHAIN_ID", "8453"))  # Base mainnet
        self.exchange_contract: str = os.getenv("LIMITLESS_EXCHANGE_CONTRACT", "")
        self._account_address: str | None = None

        # Caching for markets scan keyed by fetch limit
        self._markets_cache: dict[int, list[dict]] = {}
        self._markets_cache_ts: dict[int, float] = {}
        self._markets_cache_ttl: float = float(os.getenv("LIMITLESS_MARKETS_CACHE_TTL", "30.0"))
        self._markets_cache_lock = threading.Lock()

    def login(self, api_key: str | None = None, private_key: str | None = None) -> bool:
        """Configure credentials and verify authentication.

        Args:
            api_key: Optional API key. Falls back to LIMITLESS_API_KEY.
            private_key: Optional Ethereum private key for EIP-712 order signing.
                Falls back to LIMITLESS_PRIVATE_KEY.

        Returns:
            True if credentials are valid and stored, False otherwise.
        """
        resolved_key = api_key or os.getenv("LIMITLESS_API_KEY", "").strip()
        resolved_pk = private_key or os.getenv("LIMITLESS_PRIVATE_KEY", "").strip()

        if not resolved_key:
            logger.warning("Limitless login failed: no API key provided")
            self.authenticated = False
            return False

        self.api_key = resolved_key
        self.session.headers.update({
            "X-API-KEY": self.api_key,
            "Content-Type": "application/json",
        })

        if resolved_pk:
            try:
                norm_pk = normalize_private_key(resolved_pk)
                from eth_account import Account
                account = Account.from_key(norm_pk)
                self.private_key = norm_pk
                self._account_address = account.address
                logger.info("Limitless authenticated with address %s", self._account_address)
            except (ValueError, TypeError) as exc:
                logger.error("Limitless login invalid private key: %s", exc)
                self.authenticated = False
                return False

        self.authenticated = True
        return True

    # -----------------------------------------------------------------------
    # Request Helpers
    # -----------------------------------------------------------------------

    def _public_request(self, endpoint: str, params: dict | None = None) -> dict | list | None:
        """Send a GET request to a public endpoint with circuit breaker protection."""
        if _circuit.is_open():
            logger.warning("Limitless circuit breaker open — request to %s rejected", endpoint)
            return None

        _rate_limit()
        url = f"{self.base_url}{endpoint}"
        try:
            resp = self.session.get(url, params=params, timeout=10)
            if resp.status_code == 200:
                _circuit.record_success()
                return resp.json()
            elif resp.status_code == 404:
                return None
            elif resp.status_code == 429:
                _circuit.record_failure()
                logger.warning("Limitless rate limited (429) on %s", endpoint)
                return None
            elif resp.status_code >= 500:
                _circuit.record_failure()
                logger.warning("Limitless server error %d on %s: %s",
                               resp.status_code, endpoint, resp.text[:200])
                return None
            else:
                logger.warning("Limitless public request failed %d on %s: %s",
                               resp.status_code, endpoint, resp.text[:200])
                return None
        except requests.RequestException as exc:
            _circuit.record_failure()
            logger.warning("Limitless public request exception on %s: %s", endpoint, exc)
            return None

    def _private_request(
        self,
        endpoint: str,
        method: str = "POST",
        payload_data: dict | None = None,
        params: dict | None = None,
    ) -> dict | list | None:
        """Send an authenticated request with circuit breaker protection."""
        if not self.authenticated:
            logger.error("Limitless: cannot make private request without authentication")
            return None

        if _circuit.is_open():
            logger.warning("Limitless circuit breaker open — private request to %s rejected", endpoint)
            return None

        _rate_limit()
        url = f"{self.base_url}{endpoint}"
        try:
            if method.upper() == "POST":
                resp = self.session.post(url, json=payload_data, params=params, timeout=10)
            elif method.upper() == "DELETE":
                resp = self.session.delete(url, params=params, timeout=10)
            elif method.upper() == "GET":
                resp = self.session.get(url, params=params, timeout=10)
            else:
                logger.error("Limitless unsupported private HTTP method: %s", method)
                return None

            if resp.status_code in (200, 201):
                _circuit.record_success()
                return resp.json()
            elif resp.status_code == 204:
                _circuit.record_success()
                return {"success": True}
            elif resp.status_code == 429:
                _circuit.record_failure()
                logger.warning("Limitless rate limited (429) on private %s", endpoint)
                return None
            else:
                _circuit.record_failure()
                logger.warning("Limitless private request failed %d on %s: %s",
                               resp.status_code, endpoint, resp.text[:200])
                return None
        except requests.RequestException as exc:
            _circuit.record_failure()
            logger.warning("Limitless private request exception on %s: %s", endpoint, exc)
            return None

    # -----------------------------------------------------------------------
    # Market Data
    # -----------------------------------------------------------------------

    def fetch_all_markets(self, limit: int = 100) -> list[dict]:
        """Fetch all active prediction markets, normalized for scanner consumption.

        Args:
            limit: Maximum number of markets to return.

        Returns:
            List of standardized market dicts.
        """
        now = time.time()
        with self._markets_cache_lock:
            cached_data = self._markets_cache.get(limit)
            cached_ts = self._markets_cache_ts.get(limit, 0.0)
            if cached_data is not None and (now - cached_ts) < self._markets_cache_ttl:
                return cached_data

        data = self._public_request("/markets", params={"limit": limit})
        if not data:
            return []

        raw_markets = data.get("markets", data) if isinstance(data, dict) else data
        if not isinstance(raw_markets, list):
            return []

        normalized = []
        for raw in raw_markets:
            if not isinstance(raw, dict):
                continue
            norm = self._normalize_market(raw)
            if norm:
                normalized.append(norm)

        with self._markets_cache_lock:
            self._markets_cache[limit] = normalized
            self._markets_cache_ts[limit] = now

        return normalized

    def _normalize_market(self, raw: dict) -> dict | None:
        """Normalize a raw Limitless market dictionary into scanner schema."""
        market_id = str(raw.get("id") or raw.get("marketId") or raw.get("slug") or "")
        title = raw.get("title") or raw.get("question") or ""
        if not market_id or not title:
            return None

        # Outcomes / prices
        outcomes = raw.get("outcomes") or []
        yes_price = None
        no_price = None
        if isinstance(outcomes, list) and len(outcomes) >= 2:
            try:
                p0 = outcomes[0].get("price") if isinstance(outcomes[0], dict) else None
                p1 = outcomes[1].get("price") if isinstance(outcomes[1], dict) else None
                if p0 is not None:
                    yes_price = float(p0)
                if p1 is not None:
                    no_price = float(p1)
            except (TypeError, ValueError) as exc:
                logger.debug("Failed parsing outcome prices in market %s: %s", market_id, exc)

        if yes_price is None and raw.get("yesPrice") is not None:
            try:
                yes_price = float(raw["yesPrice"])
            except (TypeError, ValueError) as exc:
                logger.debug("Failed parsing yesPrice in market %s: %s", market_id, exc)
        if no_price is None and raw.get("noPrice") is not None:
            try:
                no_price = float(raw["noPrice"])
            except (TypeError, ValueError) as exc:
                logger.debug("Failed parsing noPrice in market %s: %s", market_id, exc)

        # Reward metadata if attached to market
        reward_info = raw.get("rewardProgram") or raw.get("rewards") or {}
        pool_usdc = 0.0
        if isinstance(reward_info, dict):
            try:
                pool_usdc = float(reward_info.get("pool_size_usdc") or reward_info.get("dailyRate") or 0.0)
            except (TypeError, ValueError) as exc:
                logger.debug("Failed parsing pool_size_usdc in market %s: %s", market_id, exc)

        return {
            "id": market_id,
            "market_id": market_id,
            "title": title,
            "question": title,
            "platform": "limitless",
            "category": raw.get("category", ""),
            "status": raw.get("status", "active"),
            "yes_price": yes_price,
            "no_price": no_price,
            "volume": float(raw.get("volume") or raw.get("volumeUsd") or 0.0),
            "reward_pool_usdc": pool_usdc,
            "reward_program": reward_info if isinstance(reward_info, dict) else {},
            "_raw": raw,
        }

    def get_order_book(self, market_id: str, limit: int = 50) -> dict | None:
        """Fetch order book for a market.

        Args:
            market_id: Identifier of the market.
            limit: Maximum levels per side.

        Returns:
            Dict with 'bids' and 'asks' lists of {'price': float, 'amount': float},
            or None on failure.
        """
        data = self._public_request(f"/markets/{market_id}/book", params={"limit": limit})
        if not data or not isinstance(data, dict):
            return None

        raw_bids = data.get("bids", [])
        raw_asks = data.get("asks", [])

        bids = []
        for b in raw_bids:
            try:
                price = float(b.get("price", 0)) if isinstance(b, dict) else float(b[0])
                amount = float(b.get("amount") or b.get("size") or 0) if isinstance(b, dict) else float(b[1])
                bids.append({"price": price, "amount": amount})
            except (IndexError, TypeError, ValueError):
                continue

        asks = []
        for a in raw_asks:
            try:
                price = float(a.get("price", 0)) if isinstance(a, dict) else float(a[0])
                amount = float(a.get("amount") or a.get("size") or 0) if isinstance(a, dict) else float(a[1])
                asks.append({"price": price, "amount": amount})
            except (IndexError, TypeError, ValueError):
                continue

        return {"bids": bids, "asks": asks}

    def get_reward_program(self, market_id: str) -> dict | None:
        """Fetch liquidity reward program configuration for a market.

        Args:
            market_id: Market identifier.

        Returns:
            Reward program dict or None if no active program exists.
        """
        data = self._public_request(f"/markets/{market_id}/rewards")
        if not data or not isinstance(data, dict):
            return None

        try:
            min_size = float(data.get("min_incentive_size") or data.get("minSize") or 5.0)
            max_spread = float(data.get("max_incentive_spread") or data.get("maxSpread") or 0.05)
            pool_size = float(data.get("pool_size_usdc") or data.get("dailyRate") or 0.0)
            active = bool(data.get("active", pool_size > 0))
        except (TypeError, ValueError):
            return None

        return {
            "market_id": market_id,
            "min_incentive_size": min_size,
            "max_incentive_spread": max_spread,
            "pool_size_usdc": pool_size,
            "daily_rate_usdc": pool_size,
            "active": active,
        }

    # -----------------------------------------------------------------------
    # Orders & Execution
    # -----------------------------------------------------------------------

    def build_order_struct(
        self,
        market_id: str,
        side: str,
        outcome: str,
        quantity: float | int,
        price: float,
        salt: int | None = None,
        expiration: int | None = None,
    ) -> tuple[dict, dict]:
        """Build EIP-712 domain and order struct for signing.

        Args:
            market_id: Market ID.
            side: "buy" (0) or "sell" (1).
            outcome: "yes" (0) or "no" (1).
            quantity: Number of contracts.
            price: Limit price (0-1).
            salt: Optional salt for uniqueness.
            expiration: Unix timestamp in seconds for order expiry.

        Returns:
            Tuple of (domain_dict, order_struct_dict).
        """
        domain = {
            "name": "Limitless Exchange",
            "version": "1",
            "chainId": self.chain_id,
            "verifyingContract": self.exchange_contract,
        }

        maker = self._account_address or "0x0000000000000000000000000000000000000000"
        side_code = 0 if side.lower() in ("buy", "bid") else 1
        outcome_code = 0 if outcome.lower() == "yes" else 1

        order_struct = {
            "maker": maker,
            "marketId": str(market_id),
            "side": side_code,
            "outcome": outcome_code,
            "price": int(round(price * 1_000_000)),
            "quantity": int(round(quantity * 1_000_000)),
            "salt": salt if salt is not None else int(time.time() * 1000),
            "expiration": expiration if expiration is not None else int(time.time() + 86400),
        }

        return domain, order_struct

    def place_order(
        self,
        market_id: str,
        side: str,
        outcome: str,
        quantity: float | int,
        price: float,
        time_in_force: str = "gtc",
    ) -> dict | None:
        """Place a limit order on Limitless.

        In dry-run mode (or when unauthenticated), returns a synthetic order response.
        In live mode, signs order with EIP-712 typed data and posts to Limitless API.

        Args:
            market_id: Market ID.
            side: "buy" or "sell" (or "bid"/"ask").
            outcome: "yes" or "no".
            quantity: Number of contracts.
            price: Limit price (0-1).
            time_in_force: "gtc", "ioc", or "fok".

        Returns:
            Order dict on success, None on failure.
        """
        # Dry-run returns synthetic order response
        if self.dry_run:
            order_id = f"dry_limitless_{market_id}_{side}_{int(time.time() * 1000)}"
            logger.info(
                "Limitless DRY-RUN order: %s %s %s @ %.4f qty=%.2f -> %s",
                side, outcome, market_id, price, quantity, order_id,
            )
            return {
                "order_id": order_id,
                "id": order_id,
                "market_id": market_id,
                "side": side,
                "outcome": outcome,
                "quantity": quantity,
                "price": price,
                "time_in_force": time_in_force,
                "status": "resting",
                "dry_run": True,
            }

        # Fail closed in live mode if not authenticated
        if not self.authenticated:
            logger.error("Limitless: live order placement requires authenticated client")
            return None

        # Live mode requires both authentication and private key
        if not self.private_key:
            logger.error("Limitless: live order placement requires private key for EIP-712 signing")
            return None

        # Live mode requires valid non-zero verifyingContract address
        if not self.exchange_contract or self.exchange_contract == "0x0000000000000000000000000000000000000000":
            logger.error("Limitless: live order placement requires valid non-zero verifyingContract address")
            return None

        domain, order_struct = self.build_order_struct(
            market_id=market_id,
            side=side,
            outcome=outcome,
            quantity=quantity,
            price=price,
        )

        try:
            signature = sign_order(domain, order_struct, self.private_key)
        except Exception as exc:
            logger.error("Limitless failed to sign EIP-712 order: %s", exc)
            return None

        payload = {
            "order": order_struct,
            "signature": signature,
            "timeInForce": time_in_force,
        }

        resp = self._private_request("/orders", method="POST", payload_data=payload)
        if not resp or not isinstance(resp, dict):
            logger.warning("Limitless place_order failed for market %s", market_id)
            return None

        return resp

    def cancel_order(self, order_id: str) -> bool:
        """Cancel an open order on Limitless.

        Args:
            order_id: Order identifier.

        Returns:
            True if cancelled or synthetic dry-run, False otherwise.
        """
        if str(order_id).startswith("dry_"):
            logger.debug("Limitless dry-run cancel: %s", order_id)
            return True

        if not self.authenticated:
            logger.warning("Limitless: cannot cancel live order %s without authentication", order_id)
            return False

        resp = self._private_request(f"/orders/{order_id}", method="DELETE")
        return bool(resp and (resp.get("success") or resp.get("status") in ("cancelled", "canceled", "ok")))

    def get_balance(self) -> float | None:
        """Fetch USDC cash balance.

        Returns:
            Balance in USDC as float, or None on failure.
        """
        if not self.authenticated:
            return None

        resp = self._private_request("/portfolio/balance", method="GET")
        if not resp or not isinstance(resp, dict):
            return None

        try:
            return float(resp.get("balance") or resp.get("usdcBalance") or 0.0)
        except (TypeError, ValueError) as exc:
            logger.debug("Failed to parse Limitless balance: %s", exc)
            return None
