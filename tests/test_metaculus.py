"""Tests for the current, read-only Metaculus Posts API client."""

import os
import sys
from unittest.mock import MagicMock, patch

import pytest
import requests

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from metaculus_api import (  # noqa: E402
    MIN_REQUEST_INTERVAL,
    METACULUS_API_URL,
    MetaculusClient,
    _community_probability,
    _normalize_post,
    _rate_limit,
)


def _binary_post(post_id: int = 10, question_id: int = 20, probability: float = 0.63) -> dict:
    return {
        "id": post_id,
        "title": "Will the test event happen?",
        "nr_forecasters": 44,
        "question": {
            "id": question_id,
            "title": "Will the test event happen?",
            "type": "binary",
            "aggregations": {
                "recency_weighted": {
                    "latest": {"centers": [probability], "forecaster_count": 42}
                }
            },
        },
    }


# ---------------------------------------------------------------------------
# Authentication and requests
# ---------------------------------------------------------------------------

class TestMetaculusLogin:
    def test_uses_current_posts_endpoint_and_token(self):
        client = MetaculusClient()
        response = MagicMock(status_code=200)

        with patch.object(client.session, "get", return_value=response) as request:
            with patch("metaculus_api._rate_limit"):
                result = client.login("test-token")

        assert result is True
        assert client.authenticated is True
        assert client.base_url == METACULUS_API_URL
        assert client.session.headers["Authorization"] == "Token test-token"
        assert request.call_args.args[0] == f"{METACULUS_API_URL}/posts/"
        assert request.call_args.kwargs["params"]["with_cp"] == "true"

    def test_rejects_missing_token_without_network_call(self):
        client = MetaculusClient()

        with patch.dict(os.environ, {}, clear=True):
            with patch.object(client.session, "get") as request:
                result = client.login()

        assert result is False
        assert client.authenticated is False
        request.assert_not_called()

    def test_falls_back_to_environment_token(self):
        client = MetaculusClient()
        response = MagicMock(status_code=200)

        with patch.dict(os.environ, {"METACULUS_API_KEY": "env-token"}):
            with patch.object(client.session, "get", return_value=response):
                with patch("metaculus_api._rate_limit"):
                    assert client.login() is True

        assert client.session.headers["Authorization"] == "Token env-token"

    @pytest.mark.parametrize("status_code", [401, 403, 500])
    def test_rejects_http_error(self, status_code):
        client = MetaculusClient()
        response = MagicMock(status_code=status_code)

        with patch.object(client.session, "get", return_value=response):
            with patch("metaculus_api._rate_limit"):
                assert client.login("bad-token") is False

        assert client.authenticated is False

    def test_rejects_request_exception(self):
        client = MetaculusClient()

        with patch.object(client.session, "get", side_effect=requests.RequestException("timeout")):
            with patch("metaculus_api._rate_limit"):
                assert client.login("token") is False


class TestMetaculusRequest:
    def test_blocks_request_before_login(self):
        client = MetaculusClient()

        with patch.object(client.session, "request") as request:
            assert client._request("GET", "/posts/") is None

        request.assert_not_called()

    def test_returns_json_after_login(self):
        client = MetaculusClient()
        client.authenticated = True
        response = MagicMock(status_code=200)
        response.json.return_value = {"results": [_binary_post()]}

        with patch.object(client.session, "request", return_value=response):
            with patch("metaculus_api._rate_limit") as limiter:
                result = client._request("GET", "/posts/")

        assert result == {"results": [_binary_post()]}
        limiter.assert_called_once()

    def test_returns_none_on_http_error(self):
        client = MetaculusClient()
        client.authenticated = True
        response = MagicMock(status_code=500, text="server error")

        with patch.object(client.session, "request", return_value=response):
            with patch("metaculus_api._rate_limit"):
                assert client._request("GET", "/posts/") is None

    def test_returns_none_on_request_exception(self):
        client = MetaculusClient()
        client.authenticated = True

        with patch.object(client.session, "request", side_effect=requests.RequestException("down")):
            with patch("metaculus_api._rate_limit"):
                assert client._request("GET", "/posts/") is None


# ---------------------------------------------------------------------------
# Response normalization and lookup
# ---------------------------------------------------------------------------

class TestPostNormalization:
    def test_normalizes_binary_question_for_existing_consumers(self):
        question = _normalize_post(_binary_post())

        assert question["id"] == 20
        assert question["_post_id"] == 10
        assert question["number_of_forecasters"] == 44
        assert question["community_prediction"]["full"]["q2"] == pytest.approx(0.63)

    def test_uses_aggregation_forecaster_count_as_fallback(self):
        post = _binary_post()
        post.pop("nr_forecasters")

        question = _normalize_post(post)

        assert question["number_of_forecasters"] == 42

    def test_skips_non_binary_and_group_posts(self):
        post = _binary_post()
        post["question"]["type"] = "numeric"

        assert _normalize_post(post) is None
        assert _normalize_post({"id": 1, "group_of_questions": []}) is None

    def test_missing_community_prediction_is_allowed(self):
        post = _binary_post()
        post["question"]["aggregations"] = None

        question = _normalize_post(post)

        assert "community_prediction" not in question
        assert _community_probability(post["question"]) is None


class TestFetchActiveQuestions:
    def test_uses_documented_post_filters_and_tracks_id_mapping(self):
        client = MetaculusClient()

        with patch.object(client, "_request", return_value={"results": [_binary_post()]}) as request:
            result = client.fetch_active_questions(limit=50, offset=5, category="politics")

        assert result[0]["id"] == 20
        assert client._post_ids[20] == 10
        request.assert_called_once_with(
            "GET",
            "/posts/",
            params={
                "statuses": ["open"],
                "forecast_type": ["binary"],
                "with_cp": "true",
                "limit": 50,
                "offset": 5,
                "categories": ["politics"],
            },
        )

    def test_skips_unsupported_posts_and_handles_failure(self):
        client = MetaculusClient()
        numeric = _binary_post()
        numeric["question"]["type"] = "numeric"

        with patch.object(client, "_request", return_value={"results": [numeric]}):
            assert client.fetch_active_questions() == []
        with patch.object(client, "_request", return_value=None):
            assert client.fetch_active_questions() == []


class TestQuestionLookup:
    def test_details_returns_none_when_feed_cannot_resolve_post(self):
        client = MetaculusClient()

        with patch.object(client, "fetch_active_questions", return_value=[]):
            with patch.object(client, "_request") as request:
                assert client.get_question_details(20) is None

        request.assert_not_called()

    def test_details_resolves_uncached_question_through_posts_feed(self):
        client = MetaculusClient()
        normalized = _normalize_post(_binary_post(post_id=10, question_id=20))

        with patch.object(client, "fetch_active_questions", return_value=[normalized]):
            with patch.object(client, "_request", return_value=_binary_post()) as request:
                result = client.get_question_details(20)

        assert result["id"] == 20
        request.assert_called_once_with("GET", "/posts/10/")

    def test_details_uses_known_post_id(self):
        client = MetaculusClient()
        client._post_ids[20] = 10

        with patch.object(client, "_request", return_value=_binary_post()) as request:
            result = client.get_question_details(20)

        assert result["id"] == 20
        request.assert_called_once_with("GET", "/posts/10/")

    def test_prediction_reads_normalized_community_value(self):
        client = MetaculusClient()

        with patch.object(client, "get_question_details", return_value=_normalize_post(_binary_post())):
            assert client.get_question_prediction(20) == pytest.approx(0.63)

    def test_prediction_returns_none_when_access_tier_omits_cp(self):
        client = MetaculusClient()
        question = _normalize_post(_binary_post())
        question.pop("community_prediction")

        with patch.object(client, "get_question_details", return_value=question):
            assert client.get_question_prediction(20) is None

    def test_search_ranks_locally_without_undocumented_api_param(self):
        client = MetaculusClient()
        first = _normalize_post(_binary_post(post_id=1, question_id=1))
        second = dict(first, id=2, title="Will inflation fall below two percent?")

        with patch.object(client, "fetch_active_questions", return_value=[second, first]) as fetch:
            result = client.search_questions("Will the test event happen?", limit=1)

        assert result == [first]
        fetch.assert_called_once_with(limit=200)


# ---------------------------------------------------------------------------
# Rate limiting
# ---------------------------------------------------------------------------

class TestRateLimit:
    def test_interval_is_one_second(self):
        assert MIN_REQUEST_INTERVAL == 1.0

    def test_rate_limit_sleeps_for_remaining_interval(self):
        with patch("metaculus_api.time.time", side_effect=[100.5, 101.0]):
            with patch("metaculus_api.time.sleep") as sleep:
                with patch("metaculus_api._last_request_time", 100.0):
                    _rate_limit()

        sleep.assert_called_once_with(pytest.approx(0.5))
