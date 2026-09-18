"""Unit tests for Jev System One Client."""

from __future__ import annotations

import json
import os
import sys
import unittest
from unittest.mock import MagicMock, patch

# Ensure repo root is on sys.path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from jev_client import JevClient, JevError, JevRateLimitError


class TestJevClient(unittest.TestCase):
    """Test suite for JevClient methods and error handling."""

    def setUp(self):
        self.client = JevClient(api_key="test-key", model="typesafe/jev-1.13")

    def test_availability(self):
        """Test is_available checks api_key properly."""
        self.assertTrue(self.client.is_available())
        empty_client = JevClient(api_key="")
        self.assertFalse(empty_client.is_available())

    @patch("urllib.request.urlopen")
    def test_query_decisions_success(self, mock_urlopen):
        """Test successful decision query and JSON decoding."""
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps({
            "model": "typesafe/jev-1.13",
            "answers": {
                "is_urgent": {"type": "noul", "noul": 0.85},
            },
            "usage": {"input_tokens": 100, "output_tokens": 10},
        }).encode("utf-8")
        mock_urlopen.return_value.__enter__.return_value = mock_resp

        res = self.client.query_decisions(
            state="Test state",
            questions={"is_urgent": {"type": "noul", "instructions": "Urgent?"}},
        )
        self.assertIn("answers", res)
        self.assertEqual(res["answers"]["is_urgent"]["noul"], 0.85)

    @patch("urllib.request.urlopen")
    def test_evaluate_noul(self, mock_urlopen):
        """Test evaluate_noul helper returns a float probability."""
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps({
            "answers": {"q": {"type": "noul", "noul": 0.72}}
        }).encode("utf-8")
        mock_urlopen.return_value.__enter__.return_value = mock_resp

        prob = self.client.evaluate_noul("State", "Is it true?")
        self.assertAlmostEqual(prob, 0.72)

    @patch("urllib.request.urlopen")
    def test_evaluate_choice(self, mock_urlopen):
        """Test evaluate_choice helper returns choice, confidence, and distribution."""
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps({
            "answers": {
                "q": {
                    "type": "choice",
                    "choice": "buy_yes",
                    "confidence": 0.88,
                    "probabilities": {"buy_yes": 0.85, "buy_no": 0.10, "pass": 0.05},
                }
            }
        }).encode("utf-8")
        mock_urlopen.return_value.__enter__.return_value = mock_resp

        choice, conf, probs = self.client.evaluate_choice(
            "State", "Action?", {"buy_yes": "Yes", "buy_no": "No", "pass": "Pass"}
        )
        self.assertEqual(choice, "buy_yes")
        self.assertAlmostEqual(conf, 0.88)
        self.assertEqual(len(probs), 3)

    @patch("urllib.request.urlopen")
    def test_evaluate_score(self, mock_urlopen):
        """Test evaluate_score helper returns score and confidence."""
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps({
            "answers": {
                "q": {
                    "type": "score",
                    "score": 1.45,
                    "confidence": 0.65,
                    "probabilities": {"0": 0.1, "1": 0.8, "2": 0.1},
                }
            }
        }).encode("utf-8")
        mock_urlopen.return_value.__enter__.return_value = mock_resp

        score, conf, probs = self.client.evaluate_score(
            "State", "Rate risk", ["Low", "Medium", "High"]
        )
        self.assertAlmostEqual(score, 1.45)
        self.assertAlmostEqual(conf, 0.65)

    def test_missing_api_key_raises_error(self):
        """Test calling query_decisions without API key raises JevError."""
        no_key_client = JevClient(api_key="")
        with self.assertRaises(JevError):
            no_key_client.query_decisions("state", {})


if __name__ == "__main__":
    unittest.main()
