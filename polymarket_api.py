"""Polymarket API client for Gamma API and CLOB."""

import json
import logging
import os
import threading
import time

import httpx
import requests
from requests.adapters import HTTPAdapter
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type

from config import PM_RATE_LIMIT
from rate_limiter import PlatformCircuitBreaker

logger = logging.getLogger(__name__)

from py_clob_client_v2.client import ClobClient
from py_clob_client_v2.clob_types import (
    AssetType,
    BalanceAllowanceParams,
    OrderArgs,
    OrderPayload,
    OrderType,
    PartialCreateOrderOptions,
)
import py_clob_client_v2.http_helpers.helpers as _clob_http

GAMMA_BASE = "https://gamma-api.polymarket.com"
CLOB_BASE = "https://clob.polymarket.com"

# Rate limiting (thread-safe)
_last_request_time = 0
_rate_lock = threading.Lock()

# Proxy support — Gamma REST (requests) + authenticated CLOB writes (httpx in py-clob-client-v2)
_session = requests.Session()
_proxy_url = os.getenv("POLYMARKET_PROXY_URL")
if _proxy_url:
    _session.proxies = {"http": _proxy_url, "https": _proxy_url}
    # py-clob-client-v2 uses a module-level httpx client that otherwise bypasses
    # POLYMARKET_PROXY_URL (critical for US/MI geoblock on CLOB writes).
    _clob_http._http_client = httpx.Client(http2=True, proxy=_proxy_url)
_session.mount("https://", HTTPAdapter(pool_connections=2, pool_maxsize=10))

_ORDER_TYPE_MAP = {
    "GTC": OrderType.GTC,
    "FOK": OrderType.FOK,
    "FAK": OrderType.FAK,
    "GTD": OrderType.GTD,
}

class _RateLimitError(Exception):
    """Raised on HTTP 429 to trigger retry."""
    pass


# HARDEN-04: circuit breaker — opens after 3 consecutive failures, resets after 30s
_circuit = PlatformCircuitBreaker("polymarket", fail_limit=3, reset_timeout=30.0)


def _rate_limit():
    global _last_request_time
    with _rate_lock:
        now = time.time()
        elapsed = now - _last_request_time
        if elapsed < PM_RATE_LIMIT:
            time.sleep(PM_RATE_LIMIT - elapsed)
        _last_request_time = time.time()


@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=1, max=10),
    retry=retry_if_exception_type((_RateLimitError, requests.ConnectionError, requests.Timeout)),
    reraise=True,
)
def _get_with_retry(url: str, params: dict = None, timeout: int = 30) -> requests.Response:
    """GET request with retry on 429, connection errors, and timeouts."""
    if _circuit.is_open():
        raise _RateLimitError("Circuit open -- polymarket in backoff")
    _rate_limit()
    try:
        resp = _session.get(url, params=params, timeout=timeout)
        if resp.status_code == 429:
            raise _RateLimitError(f"Rate limited: {url}")
        resp.raise_for_status()
        _circuit.record_success()
        return resp
    except Exception:
        _circuit.record_failure()
        raise


def fetch_all_markets(limit: int = 500, max_pages: int = 20) -> list[dict]:
    """Fetch all active markets from the Gamma API with pagination."""
    all_markets = []
    offset = 0

    for _ in range(max_pages):
        params = {
            "limit": limit,
            "offset": offset,
            "active": "true",
            "closed": "false",
        }
        try:
            resp = _get_with_retry(f"{GAMMA_BASE}/markets", params=params)
            markets = resp.json()
        except (requests.RequestException, json.JSONDecodeError, _RateLimitError) as e:
            logger.warning("Polymarket markets request failed at offset %s: %s", offset, e)
            break

        if not markets:
            break

        all_markets.extend(markets)
        offset += limit

        if len(markets) < limit:
            break

    return all_markets


def fetch_events(limit: int = 500, max_pages: int = 20) -> list[dict]:
    """Fetch events from the Gamma API (for grouping multi-outcome markets)."""
    all_events = []
    offset = 0

    for _ in range(max_pages):
        params = {
            "limit": limit,
            "offset": offset,
            "active": "true",
            "closed": "false",
        }
        try:
            resp = _get_with_retry(f"{GAMMA_BASE}/events", params=params)
            events = resp.json()
        except (requests.RequestException, json.JSONDecodeError, _RateLimitError) as e:
            logger.warning("Polymarket events request failed at offset %s: %s", offset, e)
            break

        if not events:
            break

        all_events.extend(events)
        offset += limit

        if len(events) < limit:
            break

    return all_events


def fetch_order_book(token_id: str) -> dict | None:
    """Fetch order book from CLOB for a given token ID."""
    try:
        resp = _get_with_retry(f"{CLOB_BASE}/book", params={"token_id": token_id}, timeout=15)
        return resp.json()
    except (requests.RequestException, json.JSONDecodeError, _RateLimitError) as e:
        logger.warning("Order book request failed for %s: %s", token_id, e)
        return None


def get_best_bid_ask(order_book: dict) -> dict:
    """Extract best bid/ask price and size from a CLOB order book response.

    Returns dict with keys: bid, bid_size, ask, ask_size (all float or None).
    """
    result = {"bid": None, "bid_size": None, "ask": None, "ask_size": None}
    bids = order_book.get("bids", [])
    asks = order_book.get("asks", [])
    if bids:
        best_bid = bids[0]  # Highest bid first
        result["bid"] = float(best_bid.get("price", 0))
        result["bid_size"] = float(best_bid.get("size", 0))
    if asks:
        best_ask = asks[0]  # Lowest ask first
        result["ask"] = float(best_ask.get("price", 0))
        result["ask_size"] = float(best_ask.get("size", 0))
    return result


def get_clob_prices(market: dict) -> dict | None:
    """Fetch CLOB order books for a binary market's YES and NO tokens.

    Uses the clobTokenIds field to get both token order books.
    Returns dict: {yes_ask, yes_ask_size, no_ask, no_ask_size,
                   yes_bid, yes_bid_size, no_bid, no_bid_size} or None.
    """
    token_ids_raw = market.get("clobTokenIds")
    if not token_ids_raw:
        return None
    try:
        if isinstance(token_ids_raw, str):
            token_ids = json.loads(token_ids_raw)
        else:
            token_ids = token_ids_raw
    except (json.JSONDecodeError, ValueError):
        return None

    if isinstance(token_ids, (int, float)):
        return None
    if not token_ids or len(token_ids) < 2:
        return None

    yes_token, no_token = token_ids[0], token_ids[1]

    yes_book = fetch_order_book(yes_token)
    no_book = fetch_order_book(no_token)

    if not yes_book or not no_book:
        return None

    yes_data = get_best_bid_ask(yes_book)
    no_data = get_best_bid_ask(no_book)

    return {
        "yes_ask": yes_data["ask"],
        "yes_ask_size": yes_data["ask_size"],
        "no_ask": no_data["ask"],
        "no_ask_size": no_data["ask_size"],
        "yes_bid": yes_data["bid"],
        "yes_bid_size": yes_data["bid_size"],
        "no_bid": no_data["bid"],
        "no_bid_size": no_data["bid_size"],
    }


def parse_outcome_prices(market: dict) -> list[float] | None:
    """Parse outcomePrices from a market dict. Returns list of floats or None."""
    raw = market.get("outcomePrices")
    if not raw:
        return None
    try:
        if isinstance(raw, str):
            prices = json.loads(raw)
        else:
            prices = raw
        return [float(p) for p in prices]
    except (json.JSONDecodeError, ValueError, TypeError):
        return None


def get_binary_markets(markets: list[dict]) -> list[dict]:
    """Filter for binary (YES/NO) markets that are not negRisk."""
    binary = []
    for m in markets:
        if m.get("negRisk"):
            continue
        outcomes = m.get("outcomes")
        if isinstance(outcomes, str):
            try:
                outcomes = json.loads(outcomes)
            except json.JSONDecodeError:
                continue
        if outcomes and len(outcomes) == 2:
            prices = parse_outcome_prices(m)
            if prices and len(prices) == 2:
                binary.append(m)
    return binary


def get_negrisk_events(events: list[dict]) -> list[dict]:
    """Filter events where ALL markets are negRisk with parseable prices.

    An incomplete outcome set (some markets missing negRisk or prices)
    is not a guaranteed arb — we'd be buying into a partial set.
    """
    negrisk_events = []
    for event in events:
        markets = event.get("markets", [])
        if len(markets) < 2:
            continue
        # Require ALL markets in the event to be negRisk with valid prices
        all_negrisk = all(m.get("negRisk") for m in markets)
        all_priced = all(parse_outcome_prices(m) for m in markets)
        if not (all_negrisk and all_priced):
            continue
        negrisk_events.append(event)
    return negrisk_events


class PolymarketTrader:
    """CLOB trading client for Polymarket using py-clob-client-v2."""

    def __init__(self, private_key: str, chain_id: int = 137,
                 funder: str | None = None, signature_type: int = 0):
        """Initialise the CLOB trading client.

        Args:
            private_key: Ethereum private key for signing orders.
            chain_id: Polygon chain ID (default 137 mainnet).
            funder: Proxy/funder/deposit wallet that holds collateral.
                Required when the signing key differs from the funded address
                (Magic, browser proxy, or deposit-wallet flow).
            signature_type: 0 = EOA, 1 = email/Magic, 2 = browser proxy,
                3 = deposit wallet (POLYMARKET_SIGNATURE_TYPE=3).
        """
        kwargs: dict = dict(
            host=CLOB_BASE,
            key=private_key,
            chain_id=chain_id,
        )
        if funder:
            kwargs["funder"] = funder
        # Always pass explicit signature_type when non-default so type 3 is not dropped.
        if signature_type:
            kwargs["signature_type"] = signature_type
        self.client = ClobClient(**kwargs)
        # Derive or create L2 API creds for authenticated trading endpoints
        self.client.set_api_creds(self.client.create_or_derive_api_key())

    def get_balance(self) -> float | None:
        """Get collateral balance available for trading (USDC / pUSD, 6 decimals)."""
        try:
            resp = self.client.get_balance_allowance(
                BalanceAllowanceParams(asset_type=AssetType.COLLATERAL)
            )
            if resp and "balance" in resp:
                return float(resp["balance"]) / 1e6
            return None
        except Exception as e:
            logger.error("Polymarket get_balance failed: %s", e)
            return None

    def place_order(
        self,
        token_id: str,
        side: str,
        price: float,
        size: float,
        neg_risk: bool = False,
        tick_size: str = "0.01",
        order_type: str = "GTC",
    ) -> dict | None:
        """Place an order on the Polymarket CLOB.

        Args:
            token_id: The CLOB token ID (YES or NO token)
            side: "BUY" or "SELL"
            price: Price per share (0.01-0.99)
            size: Number of shares (not dollars)
            neg_risk: Whether this is a negRisk market
            tick_size: Market tick size ("0.1", "0.01", "0.001", "0.0001")
            order_type: "GTC", "FOK", "FAK", or "GTD" (default GTC)

        Returns:
            Order response dict or None on failure.
        """
        try:
            ot = _ORDER_TYPE_MAP.get(str(order_type).upper(), OrderType.GTC)
            order_args = OrderArgs(
                token_id=token_id,
                price=price,
                size=size,
                side=side.upper(),
            )
            options = PartialCreateOrderOptions(
                tick_size=tick_size,
                neg_risk=neg_risk,
            )
            resp = self.client.create_and_post_order(
                order_args, options, order_type=ot,
            )
            if resp and resp.get("success"):
                return resp
            if resp:
                logger.error("Polymarket place_order unsuccessful: %s", resp)
            return None
        except Exception as e:
            logger.error("Polymarket place_order failed: %s", e)
            return None

    def cancel_order(self, order_id: str) -> bool:
        """Cancel an open order."""
        try:
            resp = self.client.cancel_order(OrderPayload(orderID=order_id))
            if resp is None:
                return False
            if isinstance(resp, dict):
                canceled = resp.get("canceled") or resp.get("cancelled") or []
                if isinstance(canceled, list):
                    return order_id in canceled or any(order_id == str(item) for item in canceled)
                return bool(canceled)
            return True
        except Exception as e:
            logger.warning("Polymarket cancel_order failed for %s: %s", order_id, e)
            return False

    def get_order_status(self, order_id: str) -> dict | None:
        """Get the status of a specific order.

        Returns dict with 'status' key (e.g. 'matched', 'live', 'canceled') or None.
        """
        try:
            resp = self.client.get_order(order_id)
            if resp:
                return resp
            return None
        except Exception as e:
            logger.warning("Polymarket get_order_status failed for %s: %s", order_id, e)
            return None

    def get_orders(self) -> list[dict]:
        """Get open orders."""
        try:
            resp = self.client.get_open_orders()
            if isinstance(resp, list):
                return resp
            return resp.get("orders", []) if resp else []
        except Exception as e:
            logger.error("Polymarket get_orders failed: %s", e)
            return []
