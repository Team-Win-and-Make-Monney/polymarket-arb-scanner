"""Tests for superforecaster_api — Metaculus title lookup + aggregation wiring.

Covers the audit finding: get_aggregated_expert_forecast used to call a
method that never existed on MetaculusClient, so the Metaculus signal was
silently dead behind a broad except.
"""

import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest
from unittest.mock import MagicMock

import superforecaster_api
from superforecaster_api import SuperforecasterClient, _metaculus_prediction_by_title


class TestMetaculusPredictionByTitle:
    def test_none_when_no_search_results(self):
        client = MagicMock()
        client.search_questions.return_value = []
        assert _metaculus_prediction_by_title(client, "Will X happen?") is None
        client.get_question_prediction.assert_not_called()

    def test_returns_prediction_for_confident_match(self):
        client = MagicMock()
        client.search_questions.return_value = [
            {"id": 7, "title": "Will X happen?"},
            {"id": 8, "title": "Something entirely unrelated"},
        ]
        client.get_question_prediction.return_value = 0.62
        result = _metaculus_prediction_by_title(client, "Will X happen?")
        assert result == 0.62
        client.get_question_prediction.assert_called_once_with(7)

    def test_none_when_best_match_below_threshold(self):
        client = MagicMock()
        client.search_questions.return_value = [
            {"id": 9, "title": "Completely different topic about weather"},
        ]
        assert _metaculus_prediction_by_title(client, "Will BTC exceed $200k?") is None
        client.get_question_prediction.assert_not_called()

    def test_skips_malformed_results(self):
        client = MagicMock()
        client.search_questions.return_value = [
            {"title": "Will X happen?"},          # no id
            {"id": 3},                            # no title
            "not-a-dict",                          # non-dict entry
            {"id": 5, "title": 12345},             # non-string title
            {"id": 4, "title": "Will X happen?"},
        ]
        client.get_question_prediction.return_value = 0.4
        assert _metaculus_prediction_by_title(client, "Will X happen?") == 0.4
        client.get_question_prediction.assert_called_once_with(4)


class TestAggregatedExpertForecastMetaculusWiring:
    def _client_without_expert_sources(self):
        client = SuperforecasterClient(gjo_api_key="", infer_api_key="")
        client.get_expert_forecast = MagicMock(return_value=None)
        return client

    def test_metaculus_signal_flows_into_aggregate(self):
        sf = self._client_without_expert_sources()
        metaculus = MagicMock()
        metaculus.search_questions.return_value = [
            {"id": 11, "title": "Will X happen?"},
        ]
        metaculus.get_question_prediction.return_value = 0.7
        result = sf.get_aggregated_expert_forecast(
            "Will X happen?", metaculus_client=metaculus)
        assert result is not None
        assert result["probability"] == pytest.approx(0.7)
        assert result["num_sources"] == 1

    def test_metaculus_exception_swallowed_and_none_without_other_sources(self):
        sf = self._client_without_expert_sources()
        metaculus = MagicMock()
        metaculus.search_questions.side_effect = RuntimeError("api down")
        assert sf.get_aggregated_expert_forecast(
            "Will X happen?", metaculus_client=metaculus) is None

    def test_no_metaculus_client_and_no_experts_returns_none(self):
        sf = self._client_without_expert_sources()
        assert sf.get_aggregated_expert_forecast("Will X happen?") is None
