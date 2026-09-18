"""Superforecaster API client — read-only access to expert forecasts.

Aggregates predictions from known superforecasters and expert
forecasting communities for Strategy #40 (Expert Divergence).

Sources:
- Good Judgment Open: Public forecasts from GJO forecasters
- Metaculus: Already integrated via metaculus_api.py
- INFER: Intelligence community forecasting
"""

import logging
import os
import time
from typing import Any

from thefuzz import fuzz

from config import FUZZY_MATCH_THRESHOLD
from url_guard import assert_public_url

logger = logging.getLogger(__name__)

GJO_API_URL = os.getenv("GJO_API_URL", "https://www.gjopen.com/api/v1")
GJO_API_KEY = os.getenv("GJO_API_KEY", "")
INFER_API_URL = os.getenv("INFER_API_URL", "https://www.infer-pub.com/api/v1")
INFER_API_KEY = os.getenv("INFER_API_KEY", "")

EXPERT_CACHE_TTL = float(os.getenv("EXPERT_CACHE_TTL", "600"))


class SuperforecasterClient:
    """Client for aggregating superforecaster predictions."""

    def __init__(
        self,
        gjo_api_key: str = "",
        infer_api_key: str = "",
        cache_ttl: float = EXPERT_CACHE_TTL,
    ):
        """Initialize the client.

        Args:
            gjo_api_key: Good Judgment Open API key.
            infer_api_key: INFER API key.
            cache_ttl: Cache TTL in seconds.
        """
        self.gjo_api_key = gjo_api_key or GJO_API_KEY
        self.infer_api_key = infer_api_key or INFER_API_KEY
        self.cache_ttl = cache_ttl
        # SSRF guard: GJO_API_URL / INFER_API_URL are env-configurable and receive
        # Bearer-token requests — refuse endpoints that resolve to internal hosts.
        assert_public_url(GJO_API_URL, env_name="GJO_API_URL", allow_http=False)
        assert_public_url(INFER_API_URL, env_name="INFER_API_URL", allow_http=False)
        self._cache: dict[str, dict] = {}
        self._cache_timestamps: dict[str, float] = {}

    def _get_cached(self, key: str) -> dict | None:
        """Get cached forecast data."""
        if key not in self._cache:
            return None
        if time.time() > self._cache_timestamps.get(key, 0):
            del self._cache[key]
            del self._cache_timestamps[key]
            return None
        return self._cache[key]

    def _set_cached(self, key: str, data: dict) -> None:
        """Set cached forecast data."""
        self._cache[key] = data
        self._cache_timestamps[key] = time.time() + self.cache_ttl

    def search_gjo_questions(
        self,
        query: str,
        limit: int = 5,
    ) -> list[dict]:
        """Search Good Judgment Open for matching questions.

        Args:
            query: Search query string.
            limit: Maximum results to return.

        Returns:
            List of question dicts with id, title, probability.
        """
        if not self.gjo_api_key:
            logger.debug("GJO API key not configured")
            return []

        cache_key = f"gjo_search:{query}"
        cached = self._get_cached(cache_key)
        if cached:
            return cached.get("results", [])

        try:
            import requests
            headers = {"Authorization": f"Bearer {self.gjo_api_key}"}
            resp = requests.get(
                f"{GJO_API_URL}/questions/search",
                params={"q": query, "limit": limit},
                headers=headers,
                timeout=10,
            )
            resp.raise_for_status()
            data = resp.json()

            results = []
            for q in data.get("questions", []):
                results.append({
                    "id": q.get("id"),
                    "title": q.get("title", ""),
                    "probability": q.get("community_prediction", {}).get("probability"),
                    "superforecaster_prediction": q.get("superforecaster_prediction", {}).get("probability"),
                    "num_forecasters": q.get("num_forecasters", 0),
                    "source": "gjo",
                })

            self._set_cached(cache_key, {"results": results})
            return results

        except Exception as e:
            logger.debug("GJO search failed: %s", e)
            return []

    def search_infer_questions(
        self,
        query: str,
        limit: int = 5,
    ) -> list[dict]:
        """Search INFER for matching questions.

        Args:
            query: Search query string.
            limit: Maximum results to return.

        Returns:
            List of question dicts with id, title, probability.
        """
        if not self.infer_api_key:
            logger.debug("INFER API key not configured")
            return []

        cache_key = f"infer_search:{query}"
        cached = self._get_cached(cache_key)
        if cached:
            return cached.get("results", [])

        try:
            import requests
            headers = {"Authorization": f"Bearer {self.infer_api_key}"}
            resp = requests.get(
                f"{INFER_API_URL}/questions/search",
                params={"q": query, "limit": limit},
                headers=headers,
                timeout=10,
            )
            resp.raise_for_status()
            data = resp.json()

            results = []
            for q in data.get("questions", []):
                results.append({
                    "id": q.get("id"),
                    "title": q.get("title", ""),
                    "probability": q.get("consensus_probability"),
                    "num_forecasters": q.get("num_forecasts", 0),
                    "source": "infer",
                })

            self._set_cached(cache_key, {"results": results})
            return results

        except Exception as e:
            logger.debug("INFER search failed: %s", e)
            return []

    def get_expert_forecast(
        self,
        market_title: str,
    ) -> dict | None:
        """Get expert forecast for a market title.

        Searches across all configured sources and returns the
        best-matched forecast with highest confidence.

        Args:
            market_title: Market title to search for.

        Returns:
            Dict with probability, confidence, source, or None.
        """
        cache_key = f"expert:{market_title}"
        cached = self._get_cached(cache_key)
        if cached:
            return cached

        forecasts = []

        gjo_results = self.search_gjo_questions(market_title, limit=3)
        for r in gjo_results:
            if r.get("superforecaster_prediction") is not None:
                forecasts.append({
                    "probability": r["superforecaster_prediction"],
                    "source": "gjo_superforecasters",
                    "num_forecasters": r.get("num_forecasters", 0),
                    "title": r.get("title", ""),
                })
            elif r.get("probability") is not None:
                forecasts.append({
                    "probability": r["probability"],
                    "source": "gjo_community",
                    "num_forecasters": r.get("num_forecasters", 0),
                    "title": r.get("title", ""),
                })

        infer_results = self.search_infer_questions(market_title, limit=3)
        for r in infer_results:
            if r.get("probability") is not None:
                forecasts.append({
                    "probability": r["probability"],
                    "source": "infer",
                    "num_forecasters": r.get("num_forecasters", 0),
                    "title": r.get("title", ""),
                })

        if not forecasts:
            return None

        forecasts.sort(key=lambda f: f.get("num_forecasters", 0), reverse=True)
        best = forecasts[0]

        result = {
            "probability": best["probability"],
            "source": best["source"],
            "num_forecasters": best.get("num_forecasters", 0),
            "confidence": min(0.90, 0.50 + (best.get("num_forecasters", 0) / 200)),
            "matched_title": best.get("title", ""),
        }

        self._set_cached(cache_key, result)
        return result

    def get_aggregated_expert_forecast(
        self,
        market_title: str,
        metaculus_client=None,
    ) -> dict | None:
        """Get weighted aggregate of expert forecasts.

        Combines superforecaster sources with Metaculus for
        a more robust consensus estimate.

        Args:
            market_title: Market title to search for.
            metaculus_client: Optional MetaculusClient for additional signal.

        Returns:
            Dict with probability, confidence, sources, or None.
        """
        forecasts = []
        weights = []

        expert_forecast = self.get_expert_forecast(market_title)
        if expert_forecast:
            forecasts.append(expert_forecast["probability"])
            weight = 2.0 if "superforecaster" in expert_forecast["source"] else 1.0
            weights.append(weight)

        if metaculus_client:
            try:
                metaculus_prob = _metaculus_prediction_by_title(
                    metaculus_client, market_title)
                if metaculus_prob is not None:
                    forecasts.append(metaculus_prob)
                    weights.append(1.5)
            except Exception as e:
                logger.debug("Metaculus lookup failed: %s", e)

        if not forecasts:
            return None

        weighted_prob = sum(f * w for f, w in zip(forecasts, weights)) / sum(weights)

        return {
            "probability": weighted_prob,
            "num_sources": len(forecasts),
            "confidence": min(0.85, 0.50 + (len(forecasts) * 0.15)),
        }


def _metaculus_prediction_by_title(metaculus_client, market_title: str) -> float | None:
    """Resolve a market title to a Metaculus community probability.

    Replaces a call to a method that never existed on MetaculusClient
    (``get_community_prediction_by_title``) — that signal path was silently
    dead because the AttributeError was swallowed by the caller's broad
    except. Searches open questions, fuzzy-verifies the best title match
    against FUZZY_MATCH_THRESHOLD, and returns its community median.

    Args:
        metaculus_client: MetaculusClient instance.
        market_title: Platform market title to match.

    Returns:
        Community probability (0-1), or None when no confident match exists.
    """
    matches = metaculus_client.search_questions(market_title, limit=10)
    if not matches:
        return None

    best_id = None
    best_score = 0
    for question in matches:
        if not isinstance(question, dict):
            continue
        title = question.get("title")
        question_id = question.get("id")
        if not isinstance(title, str) or not title or question_id is None:
            continue
        score = fuzz.token_sort_ratio(market_title.lower(), title.lower())
        if score > best_score:
            best_score = score
            best_id = question_id

    if best_id is None or best_score < FUZZY_MATCH_THRESHOLD:
        return None
    return metaculus_client.get_question_prediction(best_id)
