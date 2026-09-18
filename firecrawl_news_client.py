"""Firecrawl web-search news client wrapper, duck-type compatible with FinnhubNewsClient.

Alternative/supplemental news source for scans/news_snipe.py — fills the
specced-but-unbuilt news_monitor.py slot (.planning/research/ARCHITECTURE.md
Pattern 6) using Firecrawl's /v2/search instead of GDELT/NewsAPI.ai/RSS.
Disabled by default; see FIRECRAWL_NEWS_ENABLED in config.py.
"""

import logging
import os
import time
from datetime import datetime, timezone

import requests
from requests.adapters import HTTPAdapter
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type

logger = logging.getLogger(__name__)


class _FirecrawlNewsError(Exception):
    """Raised on Firecrawl API errors."""
    pass


class _RateLimitError(Exception):
    """Raised on HTTP 429 rate limit from Firecrawl."""
    pass


class FirecrawlNewsClient:
    """Wrapper for Firecrawl's /v2/search, duck-typed to match FinnhubNewsClient.

    Implements the same fetch_company_news(symbol, from_date, to_date) -> list[dict]
    interface that scans/news_snipe.py expects, so it can be passed anywhere a
    FinnhubNewsClient is accepted. Each call spends Firecrawl search credits
    (2 credits per call); this is a live per-call query, not a monitor poll.
    """

    def __init__(self, api_key: str):
        """Initialize Firecrawl news client.

        Args:
            api_key: Firecrawl API key from environment or config.

        Raises:
            ValueError: If api_key is empty or invalid format.
        """
        if not api_key:
            raise ValueError("FIRECRAWL_API_KEY is required but not set")

        self.api_key = api_key
        self.base_url = "https://api.firecrawl.dev/v2"

        # Session reuse for HTTP
        self._session = requests.Session()
        self._session.mount("https://", HTTPAdapter(pool_connections=2, pool_maxsize=10))

        self._request_timeout = float(os.getenv("FIRECRAWL_NEWS_REQUEST_TIMEOUT", "15.0"))

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
        """Fetch recent news for a market/topic via Firecrawl web search.

        Args:
            symbol: Market key or search topic (e.g. a Kalshi/Polymarket
                condition_id-derived label, or a plain-language topic string —
                unlike Finnhub this is not required to be a stock ticker).
            from_date: From date in YYYY-MM-DD format (used to filter results
                client-side; Firecrawl search does not take an explicit range).
            to_date: To date in YYYY-MM-DD format (currently unused — reserved
                for future date-range search support).

        Returns:
            List of news dicts with keys: headline, summary, url, datetime
            (numeric epoch seconds, matching the FinnhubNewsClient shape that
            scans/news_snipe.py expects for its staleness filter).

        Raises:
            _RateLimitError: If rate limited (429).
            _FirecrawlNewsError: On other API errors.
            ValueError: If API key is invalid.
        """
        payload = {
            "query": symbol,
            "limit": 10,
            "sources": ["news", "web"],
        }

        try:
            resp = self._session.post(
                f"{self.base_url}/search",
                json=payload,
                headers={"Authorization": f"Bearer {self.api_key}"},
                timeout=self._request_timeout,
            )

            if resp.status_code == 429:
                logger.warning("Firecrawl rate limited (429), will retry")
                raise _RateLimitError("Rate limit exceeded")

            if resp.status_code == 401:
                logger.error("Firecrawl authentication failed — check FIRECRAWL_API_KEY")
                raise ValueError("Invalid Firecrawl API key")

            resp.raise_for_status()

            body = resp.json()
            results = body.get("data", {}).get("news") or body.get("data", {}).get("web") or []

            headlines = []
            cutoff = self._parse_date_epoch(from_date)
            for item in results:
                published_epoch = self._extract_epoch(item)
                if published_epoch is None:
                    continue
                if cutoff is not None and published_epoch < cutoff:
                    continue
                headlines.append({
                    "headline": item.get("title", ""),
                    "summary": item.get("description", "") or item.get("snippet", ""),
                    "url": item.get("url", ""),
                    "datetime": published_epoch,
                })

            logger.info("Fetched %d news items for %s via Firecrawl search", len(headlines), symbol)
            return headlines

        except (requests.ConnectionError, requests.Timeout) as e:
            logger.warning("Firecrawl API unavailable, will retry: %s", str(e))
            raise

        except _RateLimitError:
            raise

        except ValueError:
            raise

        except Exception as e:
            logger.error("Firecrawl API error for %s: %s", symbol, str(e))
            raise _FirecrawlNewsError(f"Failed to fetch news for {symbol}") from e

    @staticmethod
    def _parse_date_epoch(date_str: str) -> float | None:
        """Parse a YYYY-MM-DD date string into epoch seconds (UTC midnight)."""
        try:
            dt = datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
            return dt.timestamp()
        except (ValueError, TypeError):
            return None

    @staticmethod
    def _extract_epoch(item: dict) -> float | None:
        """Best-effort extraction of a published-date epoch from a search result item."""
        for key in ("publishedDate", "published_date", "date"):
            raw = item.get(key)
            if not raw:
                continue
            try:
                return datetime.fromisoformat(raw.replace("Z", "+00:00")).timestamp()
            except (ValueError, TypeError, AttributeError):
                continue
        return None
