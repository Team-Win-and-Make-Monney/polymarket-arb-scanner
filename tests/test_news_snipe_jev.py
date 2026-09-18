"""Unit tests for Jev-enhanced News Sniping."""

from __future__ import annotations

import os
import sys
import unittest
from unittest.mock import MagicMock

for mod in [
    "dotenv",
    "thefuzz",
    "httpx",
    "requests",
    "requests.adapters",
    "tenacity",
    "cryptography",
    "cryptography.hazmat",
    "cryptography.hazmat.primitives",
    "cryptography.hazmat.primitives.asymmetric",
    "kalshi_api",
    "py_clob_client_v2",
    "py_clob_client_v2.client",
    "py_clob_client_v2.clob_types",
    "py_clob_client_v2.http_helpers",
    "py_clob_client_v2.http_helpers.helpers",
]:
    if mod not in sys.modules:
        sys.modules[mod] = MagicMock()

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scans.news_snipe import (
    _score_sentiment,
    _score_sentiment_with_jev,
)


class TestNewsSnipeJev(unittest.TestCase):
    """Test suite for Jev semantic sentiment scoring in news sniping."""

    def test_keyword_fallback_when_jev_unavailable(self):
        """Test fallback to keyword scoring when client is unavailable."""
        mock_client = MagicMock()
        mock_client.is_available.return_value = False

        res = _score_sentiment_with_jev(
            "FDA approved new vaccine",
            "Full approval granted",
            "Will FDA approve new vaccine?",
            client=mock_client,
        )
        self.assertEqual(res["sentiment"], "YES")
        self.assertEqual(res["source"], "keyword")

    def test_jev_resolves_yes_sentiment(self):
        """Test Jev accurately resolves YES outcome."""
        mock_client = MagicMock()
        mock_client.is_available.return_value = True
        mock_client.query_decisions.return_value = {
            "answers": {
                "outcome_resolution": {
                    "type": "choice",
                    "choice": "resolves_yes",
                    "confidence": 0.92,
                }
            }
        }

        res = _score_sentiment_with_jev(
            "Regulators give green light to spot ETF",
            "SEC confirms registration effective immediately",
            "Will SEC approve spot ETF?",
            client=mock_client,
        )
        self.assertEqual(res["sentiment"], "YES")
        self.assertAlmostEqual(res["confidence"], 0.92)
        self.assertEqual(res["source"], "jev")

    def test_jev_resolves_no_sentiment(self):
        """Test Jev accurately resolves NO outcome."""
        mock_client = MagicMock()
        mock_client.is_available.return_value = True
        mock_client.query_decisions.return_value = {
            "answers": {
                "outcome_resolution": {
                    "type": "choice",
                    "choice": "resolves_no",
                    "confidence": 0.88,
                }
            }
        }

        res = _score_sentiment_with_jev(
            "Court rejects bid to overturn merger block",
            "Appeal denied by unanimous panel",
            "Will merger close before year end?",
            client=mock_client,
        )
        self.assertEqual(res["sentiment"], "NO")
        self.assertAlmostEqual(res["confidence"], 0.88)
        self.assertEqual(res["source"], "jev")

    def test_jev_filters_neutral_speculation(self):
        """Test Jev filters out neutral/inconclusive news that keyword matching might misclassify."""
        mock_client = MagicMock()
        mock_client.is_available.return_value = True
        mock_client.query_decisions.return_value = {
            "answers": {
                "outcome_resolution": {
                    "type": "choice",
                    "choice": "neutral_unclear",
                    "confidence": 0.70,
                }
            }
        }

        res = _score_sentiment_with_jev(
            "Analyst speculates approval could happen next month",
            "Market waits for official confirmation",
            "Will ETF be approved by Friday?",
            client=mock_client,
        )
        self.assertIsNone(res["sentiment"])
        self.assertEqual(res["confidence"], 0.0)
        self.assertEqual(res["source"], "jev")


if __name__ == "__main__":
    unittest.main()
