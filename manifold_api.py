"""Manifold Markets API client for community probability estimates.

Manifold Markets provides prediction market probabilities as a read-only
signal source. The public REST API at https://api.manifold.markets/v0
works without authentication. An optional API key enables higher rate limits
and access to authenticated endpoints.

This is a READ-ONLY client -- no trading, no order placement.
"""

import logging
import os
import re
import threading
import time

import requests
from tenacity import (
    retry,
    stop_after_attempt,
    wait_exponential,
    retry_if_exception_type,
)

logger = logging.getLogger(__name__)

MANIFOLD_BASE_URL = "https://api.manifold.markets/v0"

# Rate limiting (thread-safe)
_last_request_time = 0
_rate_lock = threading.Lock()
MIN_REQUEST_INTERVAL = 0.1  # 100ms between requests


# ---------------------------------------------------------------------------
# Rate limiting
# ---------------------------------------------------------------------------

def _rate_limit():
    """Enforce minimum interval between requests (thread-safe)."""
    global _last_request_time
    with _rate_lock:
        now = time.time()
        elapsed = now - _last_request_time
        if elapsed < MIN_REQUEST_INTERVAL:
            time.sleep(MIN_REQUEST_INTERVAL - elapsed)
        _last_request_time = time.time()


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------

class _RateLimitError(Exception):
    """Raised when Manifold returns HTTP 429 -- triggers tenacity retry."""
    pass


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------

class ManifoldClient:
    """Manifold Markets public API client (read-only).

    All public endpoints work without an API key. Providing one enables
    higher rate limits and access to authenticated endpoints (not used here).
    """

    def __init__(self, api_key: str | None = None):
        self.session = requests.Session()
        self.session.headers.update({
            "Accept": "application/json",
        })
        self.base_url = MANIFOLD_BASE_URL

        api_key = api_key or os.getenv("MANIFOLD_API_KEY")
        if api_key:
            self.session.headers.update({
                "Authorization": f"Key {api_key}",
            })
            logger.info("Manifold client initialized with API key")
        else:
            logger.info("Manifold client initialized (no API key, public access)")

    # -------------------------------------------------------------------
    # Internal request helpers
    # -------------------------------------------------------------------

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=1, max=10),
        retry=retry_if_exception_type(
            (_RateLimitError, requests.ConnectionError, requests.Timeout)
        ),
        reraise=True,
    )
    def _get(self, endpoint: str, params: dict | None = None) -> dict | list | None:
        """Make a GET request with rate limiting and retries.

        Args:
            endpoint: API path relative to base URL (e.g. ``/markets``).
            params: Query parameters.

        Returns:
            Response JSON (dict or list) or None on failure.
        """
        _rate_limit()
        try:
            url = f"{self.base_url}{endpoint}"
            resp = self.session.get(url, params=params, timeout=30)
            if resp.status_code == 200:
                return resp.json()
            if resp.status_code == 429:
                logger.warning("Manifold rate limited on %s, retrying...", endpoint)
                raise _RateLimitError(f"Manifold 429 on {endpoint}")
            logger.warning("Manifold GET %s returned %s: %s",
                           endpoint, resp.status_code, resp.text[:200])
            return None
        except requests.exceptions.ConnectionError:
            raise
        except requests.exceptions.Timeout:
            raise
        except requests.RequestException as exc:
            logger.warning("Manifold GET %s failed: %s", endpoint, exc)
            return None

    # -------------------------------------------------------------------
    # Public methods
    # -------------------------------------------------------------------

    def fetch_markets(self, limit: int = 100, sort: str = "liquidity") -> list[dict]:
        """Fetch markets sorted by the given criterion.

        Args:
            limit: Maximum number of markets to return (default 100).
            sort: Sort order. Common values: ``"liquidity"``, ``"newest"``,
                  ``"score"``, ``"close-date"``.

        Returns:
            List of market dicts. Empty list on failure.
        """
        params = {"limit": limit, "sort": sort}
        data = self._get("/markets", params=params)
        if not data or not isinstance(data, list):
            return []
        logger.info("Fetched %d Manifold markets (sort=%s)", len(data), sort)
        return data

    def fetch_market(self, market_id: str) -> dict | None:
        """Fetch a single market by its ID.

        Args:
            market_id: Manifold market ID.

        Returns:
            Market dict or None if not found.
        """
        if not isinstance(market_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", market_id):
            logger.warning("Rejected invalid Manifold market ID")
            return None
        data = self._get(f"/market/{market_id}")
        if not data or not isinstance(data, dict):
            return None
        return data

    def search_markets(self, query: str, limit: int = 20) -> list[dict]:
        """Search for markets by text query.

        Args:
            query: Search string to match against market titles.
            limit: Maximum results to return (default 20).

        Returns:
            List of matching market dicts. Empty list on failure.
        """
        params = {"term": query, "limit": limit}
        data = self._get("/search-markets", params=params)
        if not data or not isinstance(data, list):
            return []
        logger.info("Manifold search '%s' returned %d markets", query, len(data))
        return data

    def get_probability(self, market_id: str) -> float | None:
        """Get the current probability estimate for a binary market.

        Args:
            market_id: Manifold market ID.

        Returns:
            Probability (0-1) or None if the market is not found or not
            a binary market.
        """
        market = self.fetch_market(market_id)
        if not market:
            return None
        prob = market.get("probability")
        if prob is None:
            return None
        try:
            return float(prob)
        except (ValueError, TypeError):
            logger.debug("Invalid probability value for market %s: %s",
                         market_id, prob)
            return None

    def get_market_by_slug(self, slug: str) -> dict | None:
        """Fetch a market by its URL slug.

        Args:
            slug: Market slug (the URL-friendly identifier, e.g.
                  ``"will-bitcoin-reach-100k-by-2025"``).

        Returns:
            Market dict or None if not found.
        """
        if not isinstance(slug, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,200}", slug):
            logger.warning("Rejected invalid Manifold market slug")
            return None
        data = self._get(f"/slug/{slug}")
        if not data or not isinstance(data, dict):
            return None
        return data
