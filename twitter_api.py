"""Twitter/X API client for social sentiment signals.

Provides access to Twitter's v2 API for fetching tweets and sentiment
analysis related to prediction market topics. Used by strategy #39
(Social Sentiment Signals) and #42 (Insider Pattern Detection).

This is a READ-ONLY client — no posting, no engagement.
"""

import logging
import os
import threading
import time
from collections import defaultdict
from datetime import datetime, timedelta

import requests

from config import (
    TWITTER_API_KEY,
    TWITTER_API_SECRET,
    TWITTER_BEARER_TOKEN,
)

logger = logging.getLogger(__name__)

TWITTER_API_URL = "https://api.twitter.com/2"

_last_request_time = 0
_rate_lock = threading.Lock()
MIN_REQUEST_INTERVAL = 1.0


def _rate_limit():
    """Enforce minimum interval between requests (thread-safe)."""
    global _last_request_time
    with _rate_lock:
        now = time.time()
        elapsed = now - _last_request_time
        if elapsed < MIN_REQUEST_INTERVAL:
            time.sleep(MIN_REQUEST_INTERVAL - elapsed)
        _last_request_time = time.time()


class TwitterClient:
    """Twitter v2 API client for sentiment signal extraction."""

    def __init__(self, bearer_token: str | None = None):
        """Initialize the Twitter client.

        Args:
            bearer_token: Twitter API bearer token. Falls back to
                TWITTER_BEARER_TOKEN env var.
        """
        self.session = requests.Session()
        self.base_url = TWITTER_API_URL
        self.authenticated = False
        self._cache: dict[str, dict] = {}
        self._cache_ttl = 300.0
        self._lock = threading.Lock()

        token = bearer_token or TWITTER_BEARER_TOKEN
        if token:
            self.session.headers.update({
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            })
            self.authenticated = True

    def is_authenticated(self) -> bool:
        """Check if client has valid credentials."""
        return self.authenticated

    def search_tweets(
        self,
        query: str,
        max_results: int = 100,
        start_time: datetime | None = None,
        end_time: datetime | None = None,
    ) -> list[dict]:
        """Search for recent tweets matching a query.

        Args:
            query: Twitter search query (supports operators).
            max_results: Maximum tweets to return (10-100).
            start_time: Start of time range (default: 7 days ago).
            end_time: End of time range (default: now).

        Returns:
            List of tweet dicts with id, text, author_id, created_at,
            and public_metrics.
        """
        if not self.authenticated:
            logger.warning("Twitter client not authenticated")
            return []

        _rate_limit()

        params = {
            "query": query,
            "max_results": min(max_results, 100),
            "tweet.fields": "author_id,created_at,public_metrics,context_annotations",
        }

        if start_time:
            params["start_time"] = start_time.strftime("%Y-%m-%dT%H:%M:%SZ")
        if end_time:
            params["end_time"] = end_time.strftime("%Y-%m-%dT%H:%M:%SZ")

        try:
            resp = self.session.get(
                f"{self.base_url}/tweets/search/recent",
                params=params,
                timeout=30,
            )

            if resp.status_code == 429:
                logger.warning("Twitter rate limit hit, backing off")
                time.sleep(15)
                return []

            if resp.status_code != 200:
                logger.warning("Twitter search failed: %d %s", resp.status_code, resp.text)
                return []

            data = resp.json()
            return data.get("data", [])

        except requests.RequestException as e:
            logger.error("Twitter API error: %s", e)
            return []

    def get_tweet_counts(
        self,
        query: str,
        granularity: str = "hour",
        start_time: datetime | None = None,
        end_time: datetime | None = None,
    ) -> list[dict]:
        """Get tweet volume counts over time for a query.

        Args:
            query: Twitter search query.
            granularity: "minute", "hour", or "day".
            start_time: Start of time range.
            end_time: End of time range.

        Returns:
            List of {start, end, tweet_count} dicts.
        """
        if not self.authenticated:
            return []

        _rate_limit()

        params = {
            "query": query,
            "granularity": granularity,
        }

        if start_time:
            params["start_time"] = start_time.strftime("%Y-%m-%dT%H:%M:%SZ")
        if end_time:
            params["end_time"] = end_time.strftime("%Y-%m-%dT%H:%M:%SZ")

        try:
            resp = self.session.get(
                f"{self.base_url}/tweets/counts/recent",
                params=params,
                timeout=30,
            )

            if resp.status_code != 200:
                logger.warning("Twitter counts failed: %d", resp.status_code)
                return []

            data = resp.json()
            return data.get("data", [])

        except requests.RequestException as e:
            logger.error("Twitter API error: %s", e)
            return []

    def analyze_sentiment(self, tweets: list[dict]) -> dict:
        """Analyze sentiment from a list of tweets.

        Uses a simple keyword-based approach. For production, integrate
        a proper NLP model or sentiment API.

        Args:
            tweets: List of tweet dicts from search_tweets().

        Returns:
            Dict with sentiment_score (-1 to 1), positive_count,
            negative_count, neutral_count, total_engagement.
        """
        positive_keywords = {
            "bullish", "moon", "pump", "buy", "long", "winning", "yes",
            "confident", "certain", "definitely", "guaranteed", "surge",
            "rally", "soar", "breakthrough", "victory", "success",
        }
        negative_keywords = {
            "bearish", "dump", "sell", "short", "crash", "no", "unlikely",
            "fail", "lose", "losing", "doubt", "uncertain", "collapse",
            "plunge", "drop", "defeat", "failure", "impossible",
        }

        positive_count = 0
        negative_count = 0
        neutral_count = 0
        total_engagement = 0

        for tweet in tweets:
            text = tweet.get("text", "").lower()
            metrics = tweet.get("public_metrics", {})
            engagement = (
                metrics.get("retweet_count", 0) +
                metrics.get("like_count", 0) +
                metrics.get("reply_count", 0)
            )
            total_engagement += engagement

            weight = 1 + (engagement / 100)

            pos_matches = sum(1 for kw in positive_keywords if kw in text)
            neg_matches = sum(1 for kw in negative_keywords if kw in text)

            if pos_matches > neg_matches:
                positive_count += weight
            elif neg_matches > pos_matches:
                negative_count += weight
            else:
                neutral_count += weight

        total = positive_count + negative_count + neutral_count
        if total == 0:
            return {
                "sentiment_score": 0.0,
                "positive_count": 0,
                "negative_count": 0,
                "neutral_count": 0,
                "total_engagement": 0,
                "sample_size": 0,
            }

        sentiment_score = (positive_count - negative_count) / total

        return {
            "sentiment_score": sentiment_score,
            "positive_count": int(positive_count),
            "negative_count": int(negative_count),
            "neutral_count": int(neutral_count),
            "total_engagement": total_engagement,
            "sample_size": len(tweets),
        }

    def get_market_sentiment(
        self,
        market_title: str,
        lookback_hours: float = 24.0,
    ) -> dict | None:
        """Get sentiment analysis for a prediction market topic.

        Args:
            market_title: Title/description of the market to analyze.
            lookback_hours: Hours of tweets to analyze.

        Returns:
            Sentiment dict or None if insufficient data.
        """
        cache_key = f"sentiment:{market_title}"

        with self._lock:
            if cache_key in self._cache:
                entry = self._cache[cache_key]
                if time.time() - entry["timestamp"] < self._cache_ttl:
                    return entry["data"]

        keywords = self._extract_keywords(market_title)
        if not keywords:
            return None

        # Quote extracted terms so market text cannot inject Twitter operators.
        query = " OR ".join(
            f'"{keyword.replace(chr(92), "").replace(chr(34), "")}"'
            for keyword in keywords[:5]
        )
        start_time = datetime.utcnow() - timedelta(hours=lookback_hours)

        tweets = self.search_tweets(
            query=query,
            max_results=100,
            start_time=start_time,
        )

        if len(tweets) < 10:
            return None

        sentiment = self.analyze_sentiment(tweets)

        with self._lock:
            self._cache[cache_key] = {
                "data": sentiment,
                "timestamp": time.time(),
            }

        return sentiment

    def _extract_keywords(self, market_title: str) -> list[str]:
        """Extract searchable keywords from a market title."""
        stop_words = {
            "the", "a", "an", "is", "are", "will", "be", "to", "in", "on",
            "at", "for", "of", "and", "or", "by", "with", "from", "as",
        }

        words = market_title.lower().split()
        keywords = [w for w in words if w not in stop_words and len(w) > 2]

        important = []
        for word in keywords:
            if any(c.isupper() for c in market_title.split() if word in c.lower()):
                important.append(word)

        return important + [k for k in keywords if k not in important]

    def get_volume_spike(
        self,
        query: str,
        lookback_hours: float = 24.0,
        spike_threshold: float = 2.0,
    ) -> dict | None:
        """Detect unusual volume spikes for a topic.

        Args:
            query: Search query.
            lookback_hours: Hours to analyze.
            spike_threshold: Multiplier above average to flag as spike.

        Returns:
            Dict with is_spike, current_volume, average_volume, spike_ratio
            or None if insufficient data.
        """
        end_time = datetime.utcnow()
        start_time = end_time - timedelta(hours=lookback_hours)

        counts = self.get_tweet_counts(
            query=query,
            granularity="hour",
            start_time=start_time,
            end_time=end_time,
        )

        if len(counts) < 3:
            return None

        volumes = [c.get("tweet_count", 0) for c in counts]
        avg_volume = sum(volumes[:-1]) / len(volumes[:-1]) if len(volumes) > 1 else 0
        current_volume = volumes[-1] if volumes else 0

        if avg_volume == 0:
            spike_ratio = float("inf") if current_volume > 0 else 0
        else:
            spike_ratio = current_volume / avg_volume

        return {
            "is_spike": spike_ratio >= spike_threshold,
            "current_volume": current_volume,
            "average_volume": avg_volume,
            "spike_ratio": spike_ratio,
        }


_client: TwitterClient | None = None


def get_twitter_client() -> TwitterClient:
    """Get or create the module-level TwitterClient instance."""
    global _client
    if _client is None:
        _client = TwitterClient()
    return _client
