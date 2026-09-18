"""Read-only client for the current Metaculus Posts API.

Metaculus requires authentication for all API requests. Community-prediction
data is access-tier dependent, and commercial use requires a separate written
agreement. The CLI enforces those access gates before constructing this client.
"""

import logging
import os
import threading
import time
from difflib import SequenceMatcher

import requests

logger = logging.getLogger(__name__)

METACULUS_API_URL = "https://www.metaculus.com/api"

_last_request_time = 0
_rate_lock = threading.Lock()
MIN_REQUEST_INTERVAL = 1.0


# ---------------------------------------------------------------------------
# Response normalization
# ---------------------------------------------------------------------------

def _rate_limit():
    """Enforce the minimum request interval in a thread-safe way."""
    global _last_request_time
    with _rate_lock:
        now = time.time()
        elapsed = now - _last_request_time
        if elapsed < MIN_REQUEST_INTERVAL:
            time.sleep(MIN_REQUEST_INTERVAL - elapsed)
        _last_request_time = time.time()


def _community_probability(question: dict) -> float | None:
    """Extract the latest binary community probability from a question."""
    try:
        centers = question["aggregations"]["recency_weighted"]["latest"]["centers"]
        if not centers:
            return None
        probability = float(centers[0])
        return probability if 0 <= probability <= 1 else None
    except (KeyError, IndexError, TypeError, ValueError):
        return None


def _normalize_post(post: dict) -> dict | None:
    """Convert a current Posts API record to the legacy question shape."""
    question = post.get("question")
    if not isinstance(question, dict) or question.get("type") != "binary":
        return None

    normalized = dict(question)
    normalized["id"] = question.get("id")
    normalized["title"] = question.get("title") or post.get("title") or ""
    aggregations = question.get("aggregations") or {}
    recency_weighted = aggregations.get("recency_weighted") or {}
    latest = recency_weighted.get("latest") or {}
    normalized["number_of_forecasters"] = (
        post.get("nr_forecasters")
        or question.get("nr_forecasters")
        or latest.get("forecaster_count", 0)
        or 0
    )
    normalized["_post_id"] = post.get("id") or question.get("post_id")

    probability = _community_probability(question)
    if probability is not None:
        normalized["community_prediction"] = {"full": {"q2": probability}}

    return normalized


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------

class MetaculusClient:
    """Authenticated Metaculus community-prediction client (read-only)."""

    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update({"Accept": "application/json"})
        self.authenticated = False
        self.base_url = METACULUS_API_URL
        self._post_ids: dict[int, int] = {}

    def login(self, api_key: str | None = None) -> bool:
        """Authenticate and verify access to the current Posts API.

        Args:
            api_key: Metaculus API token. Falls back to ``METACULUS_API_KEY``.

        Returns:
            True when the token can access the Posts API, otherwise False.
        """
        api_key = api_key or os.getenv("METACULUS_API_KEY")
        if not api_key:
            logger.warning("Metaculus API token is required.")
            return False

        self.session.headers.update({"Authorization": f"Token {api_key}"})
        _rate_limit()
        try:
            response = self.session.get(
                f"{self.base_url}/posts/",
                params={
                    "statuses": ["open"],
                    "forecast_type": ["binary"],
                    "with_cp": "true",
                    "limit": 1,
                },
                timeout=30,
            )
            if response.status_code == 200:
                self.authenticated = True
                logger.info("Metaculus Posts API access verified.")
                return True
            logger.warning(
                "Metaculus verification failed: HTTP %s", response.status_code
            )
        except requests.RequestException as exc:
            logger.warning("Metaculus verification request failed: %s", exc)
        return False

    def _request(
        self,
        method: str,
        endpoint: str,
        params: dict | None = None,
    ) -> dict | None:
        """Make an authenticated, rate-limited API request."""
        if not self.authenticated:
            logger.warning("Metaculus request blocked before successful login.")
            return None

        _rate_limit()
        try:
            response = self.session.request(
                method,
                f"{self.base_url}{endpoint}",
                params=params,
                timeout=30,
            )
            if response.status_code == 200:
                return response.json()
            logger.warning(
                "Metaculus %s %s returned %s: %s",
                method,
                endpoint,
                response.status_code,
                response.text[:200],
            )
        except requests.RequestException as exc:
            logger.warning("Metaculus %s %s failed: %s", method, endpoint, exc)
        return None

    def fetch_active_questions(
        self,
        limit: int = 200,
        offset: int = 0,
        category: str | None = None,
    ) -> list[dict]:
        """Fetch open binary posts and normalize their nested questions."""
        params = {
            "statuses": ["open"],
            "forecast_type": ["binary"],
            "with_cp": "true",
            "limit": limit,
            "offset": offset,
        }
        if category:
            params["categories"] = [category]

        data = self._request("GET", "/posts/", params=params)
        if not data:
            return []

        questions = []
        for post in data.get("results", []):
            question = _normalize_post(post)
            if question is None:
                continue
            questions.append(question)
            question_id = question.get("id")
            post_id = question.get("_post_id")
            if isinstance(question_id, int) and isinstance(post_id, int):
                self._post_ids[question_id] = post_id
        return questions

    def get_question_prediction(self, question_id: int) -> float | None:
        """Return the latest available community probability for a question."""
        question = self.get_question_details(question_id)
        if not question:
            return None
        try:
            return float(question["community_prediction"]["full"]["q2"])
        except (KeyError, TypeError, ValueError):
            logger.debug("No community prediction for question %s", question_id)
            return None

    def search_questions(self, query: str, limit: int = 50) -> list[dict]:
        """Rank open binary questions locally because Posts has no text filter."""
        questions = self.fetch_active_questions(limit=200)
        normalized_query = query.casefold().strip()
        if not normalized_query:
            return questions[:limit]

        scored = []
        for question in questions:
            title = str(question.get("title", "")).casefold()
            score = SequenceMatcher(None, normalized_query, title).ratio()
            if normalized_query in title:
                score = 1.0
            if score >= 0.45:
                scored.append((score, question))
        scored.sort(key=lambda item: item[0], reverse=True)
        return [question for _, question in scored[:limit]]

    def get_question_details(self, question_id: int) -> dict | None:
        """Fetch and normalize the post containing a question."""
        post_id = self._post_ids.get(question_id)
        if post_id is None:
            for question in self.fetch_active_questions(limit=200):
                if question.get("id") == question_id:
                    post_id = question.get("_post_id")
                    break
        if not isinstance(post_id, int):
            logger.warning(
                "Metaculus post ID could not be resolved for question %s.",
                question_id,
            )
            return None
        post = self._request("GET", f"/posts/{post_id}/")
        if not post:
            return None
        question = _normalize_post(post)
        if (
            question
            and isinstance(question.get("id"), int)
            and isinstance(post_id, int)
        ):
            self._post_ids[question["id"]] = post_id
        return question
