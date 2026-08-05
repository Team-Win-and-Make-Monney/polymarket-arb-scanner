"""Polymarket API client for Gamma API and CLOB."""

import json
import logging
import math
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


def _install_clob_proxy(proxy_url: str) -> None:
    """Route py-clob-client-v2's httpx transport through proxy_url.

    py-clob-client-v2 uses a module-level httpx client that otherwise bypasses
    POLYMARKET_PROXY_URL (critical for US/MI geoblock on CLOB writes). This is an
    undocumented internal; fail closed if the SDK layout changes rather than
    silently sending unproxied signed requests.
    """
    if not hasattr(_clob_http, "_http_client"):
        raise RuntimeError(
            "py-clob-client-v2 no longer exposes http_helpers.helpers._http_client; "
            "POLYMARKET_PROXY_URL cannot be enforced — refusing to start with an "
            "unproxied CLOB write path")
    # Atomic replacement: construct the new client first; only after successful
    # construction close the old transport (guarded) and swap, so a construction
    # failure never leaves the SDK without a working proxied client.
    new_client = httpx.Client(http2=True, proxy=proxy_url)
    previous = _clob_http._http_client
    if previous is not None:
        try:
            previous.close()
        except Exception:
            logger.debug("Failed to close previous CLOB httpx client", exc_info=True)
    _clob_http._http_client = new_client


if _proxy_url:
    # Gamma REST proxying only — never raises. The fail-closed CLOB write-path
    # injection (_install_clob_proxy) runs in PolymarketTrader.__init__ so a
    # missing SDK internal aborts only when the authenticated write path is
    # actually constructed, not any dry-run/read-only import of this module.
    _session.proxies = {"http": _proxy_url, "https": _proxy_url}
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


def _fetch_gamma_keyset(resource: str, order: str, limit: int, max_pages: int) -> list[dict]:
    """Fetch a volume-ranked, bounded Gamma universe with opaque cursors."""
    rows_by_id: dict[str, dict] = {}
    cursor = None

    for _ in range(max_pages):
        params = {
            "limit": limit,
            "active": "true",
            "closed": "false",
            "order": order,
            "ascending": "false",
        }
        if cursor:
            params["after_cursor"] = cursor
        try:
            resp = _get_with_retry(f"{GAMMA_BASE}/{resource}/keyset", params=params)
            payload = resp.json()
        except (requests.RequestException, json.JSONDecodeError, _RateLimitError) as exc:
            logger.warning("Polymarket %s keyset request failed: %s", resource, exc)
            break

        if not isinstance(payload, dict):
            logger.warning("Polymarket %s keyset returned non-object payload", resource)
            break
        page = payload.get(resource)
        if not isinstance(page, list):
            logger.warning("Polymarket %s keyset payload missing %s list", resource, resource)
            break
        for row in page:
            if isinstance(row, dict):
                row_id = row.get("id")
                key = str(row_id if row_id is not None else row.get("conditionId") or "")
                if key:
                    rows_by_id[key] = row

        cursor = payload.get("next_cursor")
        if not page or not cursor:
            break
    else:
        logger.info(
            "Polymarket %s universe bounded at %d pages (%d unique rows, order=%s desc)",
            resource, max_pages, len(rows_by_id), order,
        )

    return list(rows_by_id.values())


def fetch_all_markets(limit: int = 500, max_pages: int = 20) -> list[dict]:
    """Fetch the top active markets by numeric lifetime volume.

    Gamma rejects deep offset pagination and the active universe exceeds
    25,000 rows. The scanner intentionally bounds each cycle to ``max_pages``
    economically relevant markets using the official keyset cursor.
    """
    return _fetch_gamma_keyset("markets", "volumeNum", limit, max_pages)


def fetch_events(limit: int = 500, max_pages: int = 20) -> list[dict]:
    """Fetch the top active events by numeric lifetime volume."""
    return _fetch_gamma_keyset("events", "volume", limit, max_pages)


def _normalize_sampling_market(market: dict) -> dict | None:
    """Convert a CLOB sampling-market row to the scanner's Gamma shape."""
    condition_id = market.get("condition_id")
    rewards = market.get("rewards")
    tokens = market.get("tokens")
    if not condition_id or not isinstance(rewards, dict) or not isinstance(tokens, list):
        return None
    try:
        min_size = float(rewards["min_size"])
        max_spread = float(rewards["max_spread"]) / 100.0
        daily_rate = sum(
            float(rate.get("rewards_daily_rate") or 0)
            for rate in (rewards.get("rates") or [])
            if isinstance(rate, dict)
        )
    except (KeyError, TypeError, ValueError):
        return None
    if min_size <= 0 or max_spread <= 0 or daily_rate <= 0:
        return None

    outcome_order = {"yes": 0, "no": 1}
    normalized_tokens = sorted(
        (token for token in tokens if isinstance(token, dict) and token.get("token_id")),
        key=lambda token: outcome_order.get(str(token.get("outcome", "")).lower(), 2),
    )
    if len(normalized_tokens) < 2:
        return None
    tags = market.get("tags") or []
    category = str(tags[0]).lower() if isinstance(tags, list) and tags else ""
    return {
        "conditionId": condition_id,
        "question": market.get("question") or condition_id,
        "category": category,
        "active": bool(market.get("active", True)),
        "closed": bool(market.get("closed", False)),
        "acceptingOrders": bool(market.get("accepting_orders", True)),
        "outcomePrices": [token.get("price") for token in normalized_tokens[:2]],
        "clobTokenIds": json.dumps([token["token_id"] for token in normalized_tokens[:2]]),
        "incentives": {
            "min_incentive_size": min_size,
            "max_incentive_spread": max_spread,
            "pool_size_usdc": daily_rate,
            "reward_daily_rate_usdc": daily_rate,
        },
        "_sampling_market": True,
    }


def fetch_reward_markets(max_pages: int = 20) -> list[dict]:
    """Fetch and normalize the complete current CLOB reward-market feed."""
    markets_by_condition: dict[str, dict] = {}
    cursor = None

    for _ in range(max_pages):
        params = {"next_cursor": cursor} if cursor else None
        try:
            resp = _get_with_retry(f"{CLOB_BASE}/sampling-markets", params=params)
            payload = resp.json()
        except (requests.RequestException, json.JSONDecodeError, _RateLimitError) as exc:
            logger.warning("Polymarket sampling-markets request failed: %s", exc)
            break
        if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
            logger.warning("Polymarket sampling-markets returned an invalid payload")
            break
        for raw_market in payload["data"]:
            if not isinstance(raw_market, dict):
                continue
            market = _normalize_sampling_market(raw_market)
            if market:
                markets_by_condition[market["conditionId"]] = market
        cursor = payload.get("next_cursor")
        if not payload["data"] or not cursor or cursor == "LTE=":
            break
    else:
        logger.warning(
            "Polymarket sampling-markets hit max_pages=%d (%d unique markets)",
            max_pages, len(markets_by_condition),
        )

    logger.info("Fetched %d current Polymarket reward markets.", len(markets_by_condition))
    return list(markets_by_condition.values())


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
                 funder: str | None = None, signature_type: int = 0,
                 execution_enabled: bool = False):
        """Initialise the CLOB trading client.

        Args:
            private_key: Ethereum private key for signing orders.
            chain_id: Polygon chain ID (default 137 mainnet).
            funder: Proxy/funder/deposit wallet that holds collateral.
                Required when the signing key differs from the funded address
                (Magic, browser proxy, or deposit-wallet flow).
            signature_type: 0 = EOA, 1 = email/Magic,
                2 = Polymarket Gnosis Safe (browser proxy wallet),
                3 = POLY_1271 (EIP-1271 smart-contract wallet).
            execution_enabled: Internal code-level breaker. Production callers
                must leave this false while international Polymarket is
                public-data/shadow-only.

        Raises:
            ValueError: if signature_type is not one of 0, 1, 2, 3.
        """
        if signature_type not in (0, 1, 2, 3):
            raise ValueError(
                f"signature_type must be one of 0 (EOA), 1 (email/Magic), "
                f"2 (Gnosis Safe), 3 (POLY_1271); got {signature_type!r}")
        self.execution_enabled = bool(execution_enabled)
        if not self.execution_enabled:
            raise PermissionError(
                "Authenticated Polymarket trading is disabled: international "
                "Polymarket is public-data/shadow-only"
            )
        # Fail-closed proxy injection belongs to the authenticated write path:
        # install (or abort) here, before the CLOB client exists, so read-only
        # imports of this module never hard-fail on the SDK internal check.
        proxy_url = os.getenv("POLYMARKET_PROXY_URL")
        if proxy_url:
            _install_clob_proxy(proxy_url)
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
        """Get collateral balance available for trading (USDC / pUSD, 6 decimals).

        NOTE: the 6-decimal (1e6) unit assumption is carried over from V1 and
        must be confirmed against a real py-clob-client-v2 balance fixture
        before any live activation (plan 08, Phase A acceptance).
        """
        try:
            resp = self.client.get_balance_allowance(
                BalanceAllowanceParams(asset_type=AssetType.COLLATERAL)
            )
            if not isinstance(resp, dict) or "balance" not in resp:
                logger.error("Polymarket get_balance: unexpected response schema: %r", resp)
                return None
            try:
                balance = float(resp["balance"])
            except (TypeError, ValueError):
                logger.error("Polymarket get_balance: malformed balance value: %r", resp["balance"])
                return None
            if math.isnan(balance) or math.isinf(balance) or balance < 0:
                logger.error("Polymarket get_balance: non-finite/negative balance: %r", resp["balance"])
                return None
            return balance / 1e6
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
        expiration: int | None = None,
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
            expiration: Unix timestamp (seconds) when a GTD order expires.
                Required (non-zero) for GTD; ignored otherwise.

        Returns:
            Order response dict or None on failure.

        Raises:
            ValueError: if order_type is GTD and expiration is missing or 0.
        """
        if not getattr(self, "execution_enabled", False):
            logger.critical(
                "Blocked Polymarket order: international Polymarket is "
                "public-data/shadow-only"
            )
            return None
        if str(order_type).upper() == "GTD" and not expiration:
            raise ValueError("order_type=GTD requires a non-zero expiration timestamp")
        try:
            ot = _ORDER_TYPE_MAP.get(str(order_type).upper())
            if ot is None:
                logger.error(
                    "Unknown order_type %r — refusing to place order (allowed: %s)",
                    order_type, ", ".join(sorted(_ORDER_TYPE_MAP)))
                return None
            order_kwargs: dict = dict(
                token_id=token_id,
                price=price,
                size=size,
                side=side.upper(),
            )
            if ot == _ORDER_TYPE_MAP["GTD"]:
                order_kwargs["expiration"] = int(expiration)
            order_args = OrderArgs(**order_kwargs)
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
            # Fail closed: only a dict response whose canceled list explicitly
            # names this order ID counts as a confirmed cancel. Anything else
            # (None, non-dict, empty list, other IDs) is treated as not canceled.
            if not isinstance(resp, dict):
                logger.warning(
                    "Polymarket cancel_order: non-dict response for %s: %r — treating as not canceled",
                    order_id, resp)
                return False
            canceled = resp.get("canceled") or resp.get("cancelled")
            if not isinstance(canceled, list) or order_id not in canceled:
                logger.warning(
                    "Polymarket cancel_order: %s not confirmed in canceled list %r",
                    order_id, canceled)
                return False
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
