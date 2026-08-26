"""Reddit API client for social sentiment signals.

Provides access to Reddit's API for fetching posts and comments from
prediction market and finance-related subreddits. Used by strategy #39
(Social Sentiment Signals) and #42 (Insider Pattern Detection).

This is a READ-ONLY client — no posting, no voting.
"""

import logging
import os
import re
import threading
import time
from datetime import datetime, timedelta

import requests

from config import (
    REDDIT_CLIENT_ID,
    REDDIT_CLIENT_SECRET,
)

logger = logging.getLogger(__name__)

_VALID_SUBREDDIT = re.compile(r"[A-Za-z0-9_-]{1,50}\Z")
_VALID_SEARCH_SORTS = frozenset({"relevance", "hot", "top", "new", "comments"})
_VALID_LIST_SORTS = frozenset({"hot", "new", "top", "rising"})

REDDIT_API_URL = "https://oauth.reddit.com"
REDDIT_AUTH_URL = "https://www.reddit.com/api/v1/access_token"

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


RELEVANT_SUBREDDITS = [
    "polymarket",
    "Kalshi",
    "predictionmarkets",
    "wallstreetbets",
    "stocks",
    "investing",
    "cryptocurrency",
    "bitcoin",
    "politics",
    "PoliticalDiscussion",
]


class RedditClient:
    """Reddit API client for sentiment signal extraction."""

    def __init__(
        self,
        client_id: str | None = None,
        client_secret: str | None = None,
    ):
        """Initialize the Reddit client.

        Args:
            client_id: Reddit app client ID. Falls back to REDDIT_CLIENT_ID.
            client_secret: Reddit app client secret. Falls back to
                REDDIT_CLIENT_SECRET.
        """
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": "arbgrid:v1.0.0 (by /u/arbgrid_bot)",
        })
        self.base_url = REDDIT_API_URL
        self.authenticated = False
        self._access_token: str | None = None
        self._token_expires: float = 0
        self._cache: dict[str, dict] = {}
        self._cache_ttl = 300.0
        self._lock = threading.Lock()

        self._client_id = client_id or REDDIT_CLIENT_ID
        self._client_secret = client_secret or REDDIT_CLIENT_SECRET

    def _authenticate(self) -> bool:
        """Obtain OAuth2 access token."""
        if not self._client_id or not self._client_secret:
            logger.debug("Reddit credentials not configured")
            return False

        if self._access_token and time.time() < self._token_expires:
            return True

        try:
            resp = requests.post(
                REDDIT_AUTH_URL,
                auth=(self._client_id, self._client_secret),
                data={"grant_type": "client_credentials"},
                headers={"User-Agent": "arbgrid:v1.0.0 (by /u/arbgrid_bot)"},
                timeout=30,
            )

            if resp.status_code != 200:
                logger.warning("Reddit auth failed: %d", resp.status_code)
                return False

            data = resp.json()
            self._access_token = data.get("access_token")
            if not isinstance(self._access_token, str) or not self._access_token.strip():
                self._access_token = None
                self.authenticated = False
                logger.warning("Reddit auth response did not include an access token")
                return False
            expires_in = data.get("expires_in", 3600)
            self._token_expires = time.time() + expires_in - 60

            self.session.headers.update({
                "Authorization": f"Bearer {self._access_token}",
            })
            self.authenticated = True
            return True

        except requests.RequestException as e:
            logger.error("Reddit auth error: %s", e)
            return False

    def is_authenticated(self) -> bool:
        """Check if client can authenticate."""
        return bool(self._client_id and self._client_secret)

    def search_posts(
        self,
        query: str,
        subreddit: str | None = None,
        sort: str = "relevance",
        time_filter: str = "week",
        limit: int = 100,
    ) -> list[dict]:
        """Search for posts matching a query.

        Args:
            query: Search query string.
            subreddit: Limit to specific subreddit (None = all).
            sort: Sort order (relevance, hot, top, new, comments).
            time_filter: Time range (hour, day, week, month, year, all).
            limit: Maximum posts to return (max 100).

        Returns:
            List of post dicts with title, selftext, score, num_comments,
            created_utc, subreddit.
        """
        if not self._authenticate():
            return []

        if subreddit is not None and not _VALID_SUBREDDIT.fullmatch(subreddit):
            logger.warning("Invalid subreddit name rejected")
            return []
        if sort not in _VALID_SEARCH_SORTS:
            logger.warning("Invalid Reddit search sort rejected: %s", sort)
            return []

        _rate_limit()

        endpoint = (
            f"{self.base_url}/r/{subreddit}/search"
            if subreddit
            else f"{self.base_url}/search"
        )

        params = {
            "q": query,
            "sort": sort,
            "t": time_filter,
            "limit": min(limit, 100),
            "restrict_sr": "true" if subreddit else "false",
        }

        try:
            resp = self.session.get(endpoint, params=params, timeout=30)

            if resp.status_code == 429:
                logger.warning("Reddit rate limit hit")
                time.sleep(10)
                return []

            if resp.status_code != 200:
                logger.warning("Reddit search failed: %d", resp.status_code)
                return []

            data = resp.json()
            posts = []
            for child in data.get("data", {}).get("children", []):
                post = child.get("data", {})
                posts.append({
                    "id": post.get("id"),
                    "title": post.get("title", ""),
                    "selftext": post.get("selftext", ""),
                    "score": post.get("score", 0),
                    "upvote_ratio": post.get("upvote_ratio", 0.5),
                    "num_comments": post.get("num_comments", 0),
                    "created_utc": post.get("created_utc", 0),
                    "subreddit": post.get("subreddit", ""),
                    "permalink": post.get("permalink", ""),
                })

            return posts

        except requests.RequestException as e:
            logger.error("Reddit API error: %s", e)
            return []

    def get_subreddit_posts(
        self,
        subreddit: str,
        sort: str = "hot",
        limit: int = 25,
    ) -> list[dict]:
        """Get posts from a subreddit.

        Args:
            subreddit: Subreddit name (without r/).
            sort: Sort order (hot, new, top, rising).
            limit: Maximum posts to return.

        Returns:
            List of post dicts.
        """
        if not self._authenticate():
            return []

        if not _VALID_SUBREDDIT.fullmatch(subreddit):
            logger.warning("Invalid subreddit name rejected")
            return []
        if sort not in _VALID_LIST_SORTS:
            logger.warning("Invalid Reddit listing sort rejected: %s", sort)
            return []

        _rate_limit()

        try:
            resp = self.session.get(
                f"{self.base_url}/r/{subreddit}/{sort}",
                params={"limit": min(limit, 100)},
                timeout=30,
            )

            if resp.status_code != 200:
                logger.warning("Reddit subreddit fetch failed: %d", resp.status_code)
                return []

            data = resp.json()
            posts = []
            for child in data.get("data", {}).get("children", []):
                post = child.get("data", {})
                posts.append({
                    "id": post.get("id"),
                    "title": post.get("title", ""),
                    "selftext": post.get("selftext", ""),
                    "score": post.get("score", 0),
                    "upvote_ratio": post.get("upvote_ratio", 0.5),
                    "num_comments": post.get("num_comments", 0),
                    "created_utc": post.get("created_utc", 0),
                    "subreddit": subreddit,
                })

            return posts

        except requests.RequestException as e:
            logger.error("Reddit API error: %s", e)
            return []

    def analyze_sentiment(self, posts: list[dict]) -> dict:
        """Analyze sentiment from a list of Reddit posts.

        Uses upvote ratio and keyword matching for sentiment estimation.

        Args:
            posts: List of post dicts from search_posts().

        Returns:
            Dict with sentiment_score (-1 to 1), positive_count,
            negative_count, neutral_count, total_engagement.
        """
        positive_keywords = {
            "bullish", "moon", "buy", "long", "winning", "yes", "confident",
            "guaranteed", "surge", "rally", "breakthrough", "victory",
            "success", "calls", "puts", "yolo", "diamond hands",
        }
        negative_keywords = {
            "bearish", "crash", "sell", "short", "no", "unlikely", "fail",
            "lose", "doubt", "collapse", "plunge", "drop", "defeat",
            "failure", "impossible", "scam", "fraud",
        }

        positive_score = 0.0
        negative_score = 0.0
        neutral_score = 0.0
        total_engagement = 0

        for post in posts:
            title = post.get("title", "").lower()
            text = post.get("selftext", "").lower()
            combined = f"{title} {text}"

            score = post.get("score", 0)
            comments = post.get("num_comments", 0)
            upvote_ratio = post.get("upvote_ratio", 0.5)

            engagement = max(1, score + comments)
            total_engagement += engagement

            weight = (engagement / 100) * (0.5 + upvote_ratio)

            pos_matches = sum(1 for kw in positive_keywords if kw in combined)
            neg_matches = sum(1 for kw in negative_keywords if kw in combined)

            if pos_matches > neg_matches:
                positive_score += weight
            elif neg_matches > pos_matches:
                negative_score += weight
            else:
                upvote_sentiment = (upvote_ratio - 0.5) * 2
                if upvote_sentiment > 0.2:
                    positive_score += weight * 0.5
                elif upvote_sentiment < -0.2:
                    negative_score += weight * 0.5
                else:
                    neutral_score += weight

        total = positive_score + negative_score + neutral_score
        if total == 0:
            return {
                "sentiment_score": 0.0,
                "positive_count": 0,
                "negative_count": 0,
                "neutral_count": 0,
                "total_engagement": 0,
                "sample_size": 0,
            }

        sentiment_score = (positive_score - negative_score) / total

        return {
            "sentiment_score": sentiment_score,
            "positive_count": int(positive_score),
            "negative_count": int(negative_score),
            "neutral_count": int(neutral_score),
            "total_engagement": total_engagement,
            "sample_size": len(posts),
        }

    def get_market_sentiment(
        self,
        market_title: str,
        subreddits: list[str] | None = None,
    ) -> dict | None:
        """Get sentiment analysis for a prediction market topic.

        Searches relevant subreddits for discussions about the market.

        Args:
            market_title: Title/description of the market to analyze.
            subreddits: List of subreddits to search (default: RELEVANT_SUBREDDITS).

        Returns:
            Sentiment dict or None if insufficient data.
        """
        cache_key = f"reddit_sentiment:{market_title}"

        with self._lock:
            if cache_key in self._cache:
                entry = self._cache[cache_key]
                if time.time() - entry["timestamp"] < self._cache_ttl:
                    return entry["data"]

        subreddits = subreddits or RELEVANT_SUBREDDITS
        all_posts = []

        keywords = self._extract_keywords(market_title)
        if not keywords:
            return None

        query = " ".join(keywords[:5])

        for subreddit in subreddits[:5]:
            posts = self.search_posts(
                query=query,
                subreddit=subreddit,
                time_filter="week",
                limit=25,
            )
            all_posts.extend(posts)

        if len(all_posts) < 5:
            return None

        sentiment = self.analyze_sentiment(all_posts)

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
            "this", "that", "it", "they", "we", "you", "what", "when",
        }

        words = market_title.lower().split()
        keywords = [
            w.strip("?.,!\"'")
            for w in words
            if w.lower() not in stop_words and len(w) > 2
        ]

        return keywords

    def get_activity_spike(
        self,
        subreddit: str,
        lookback_hours: float = 24.0,
    ) -> dict | None:
        """Detect unusual activity in a subreddit.

        Args:
            subreddit: Subreddit to monitor.
            lookback_hours: Hours to analyze.

        Returns:
            Dict with recent_posts, average_score, is_spike.
        """
        posts = self.get_subreddit_posts(subreddit, sort="new", limit=100)
        if not posts:
            return None

        now = time.time()
        cutoff = now - (lookback_hours * 3600)

        recent = [p for p in posts if p.get("created_utc", 0) > cutoff]
        if len(recent) < 3:
            return None

        avg_score = sum(p.get("score", 0) for p in recent) / len(recent)
        avg_comments = sum(p.get("num_comments", 0) for p in recent) / len(recent)

        older = [p for p in posts if p.get("created_utc", 0) <= cutoff]
        if older:
            baseline_score = sum(p.get("score", 0) for p in older) / len(older)
        else:
            baseline_score = avg_score

        is_spike = avg_score > baseline_score * 2 if baseline_score > 0 else False

        return {
            "recent_posts": len(recent),
            "average_score": avg_score,
            "average_comments": avg_comments,
            "baseline_score": baseline_score,
            "is_spike": is_spike,
        }


_client: RedditClient | None = None


def get_reddit_client() -> RedditClient:
    """Get or create the module-level RedditClient instance."""
    global _client
    if _client is None:
        _client = RedditClient()
    return _client
