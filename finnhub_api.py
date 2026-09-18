"""Finnhub news API client wrapper with REST and WebSocket support."""

import json
import logging
import os
import requests
import time
from requests.adapters import HTTPAdapter
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type

logger = logging.getLogger(__name__)


class _FinnhubError(Exception):
    """Raised on Finnhub API errors."""
    pass


class _RateLimitError(Exception):
    """Raised on HTTP 429 rate limit from Finnhub."""
    pass


class FinnhubNewsClient:
    """Wrapper for Finnhub REST and WebSocket news APIs.

    Handles authentication, retries, and connection management for fetching
    real-time and historical news headlines.
    """

    def __init__(self, api_key: str):
        """Initialize Finnhub news client.

        Args:
            api_key: Finnhub API key from environment or config.

        Raises:
            ValueError: If api_key is empty or invalid format.
        """
        if not api_key:
            raise ValueError("FINNHUB_API_KEY is required but not set")

        self.api_key = api_key
        self.base_url = "https://finnhub.io/api/v1"
        self.ws_url = "wss://stream.finnhub.io"

        # Session reuse for HTTP
        self._session = requests.Session()
        self._session.mount("https://", HTTPAdapter(pool_connections=2, pool_maxsize=10))
        # API key in the X-Finnhub-Token header, NOT the URL query string
        # (audit S11: query-string keys leak into access / proxy / Sentry logs).
        self._session.headers["X-Finnhub-Token"] = api_key

        self._request_timeout = float(os.getenv("FINNHUB_REQUEST_TIMEOUT", "10.0"))

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=1, max=10),
        retry=retry_if_exception_type((_RateLimitError, requests.ConnectionError, requests.Timeout)),
        reraise=True,
    )
    def fetch_company_news(
        self,
        symbol: str,
        from_date: str,
        to_date: str,
    ) -> list[dict]:
        """Fetch company news via Finnhub REST API.

        Args:
            symbol: Stock symbol (e.g., "AAPL").
            from_date: From date in YYYY-MM-DD format.
            to_date: To date in YYYY-MM-DD format.

        Returns:
            List of news dicts with keys: headline, summary, url, datetime, category, image, etc.

        Raises:
            _RateLimitError: If rate limited (429).
            _FinnhubError: On other API errors.
            ValueError: If API key is invalid.
        """
        params = {
            "symbol": symbol,
            "from": from_date,
            "to": to_date,
        }

        try:
            resp = self._session.get(
                f"{self.base_url}/company-news",
                params=params,
                timeout=self._request_timeout,
            )

            if resp.status_code == 429:
                logger.warning("Finnhub rate limited (429), will retry")
                raise _RateLimitError("Rate limit exceeded")

            if resp.status_code == 401:
                logger.error("Finnhub authentication failed — check FINNHUB_API_KEY")
                raise ValueError("Invalid Finnhub API key")

            resp.raise_for_status()

            news_list = resp.json() if isinstance(resp.json(), list) else resp.json().get("data", [])
            logger.info("Fetched %d news items for %s", len(news_list), symbol)

            return news_list

        except (requests.ConnectionError, requests.Timeout) as e:
            logger.warning("Finnhub API unavailable, will retry: %s", str(e))
            raise

        except _RateLimitError:
            raise

        except Exception as e:
            logger.error("Finnhub API error for %s: %s", symbol, str(e))
            raise _FinnhubError(f"Failed to fetch news for {symbol}") from e

    async def subscribe_news_stream_async(self, callback):
        """Subscribe to real-time news via Finnhub WebSocket.

        NOT USED IN PLAN 2 — stub implementation for Phase 8 Plan 5.

        Args:
            callback: Async callable(headlines_list) to invoke on new news.

        Note:
            Requires asyncio. Connection drops trigger automatic reconnect.
        """
        logger.info("WebSocket subscription stub — not implemented in Plan 2")
        pass
