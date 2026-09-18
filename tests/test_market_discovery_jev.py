"""Unit tests for Jev cross-venue market equivalence judge."""

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
    "kalshi_api",
    "anthropic",
]:
    if mod not in sys.modules:
        sys.modules[mod] = MagicMock()

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from market_discovery import CandidatePair, JevJudge
from matcher import verify_cross_platform_equivalence_jev


class TestMarketDiscoveryJev(unittest.TestCase):
    """Test suite for Jev-based equivalence judge and matcher gate."""

    def test_jev_judge_identical_pair(self):
        """Test JevJudge returns equivalent=True when Jev detects identical resolution."""
        mock_client = MagicMock()
        mock_client.is_available.return_value = True
        mock_client.query_decisions.return_value = {
            "answers": {
                "resolution_equivalence": {
                    "type": "choice",
                    "choice": "identical",
                    "confidence": 0.95,
                },
                "equivalence_probability": {"type": "noul", "noul": 0.96},
            }
        }

        judge = JevJudge(client=mock_client)
        pair = CandidatePair(
            pair_id="p1",
            venue_a="polymarket",
            question_a="Will Bitcoin hit $100k before 2027?",
            venue_b="kalshi",
            question_b="Bitcoin above $100,000 by Dec 31, 2026?",
        )

        judgment = judge.judge_pair(pair)
        self.assertTrue(judgment.equivalent)
        self.assertAlmostEqual(judgment.confidence, 0.95)
        self.assertIn("identical", judgment.reasoning)

    def test_jev_judge_divergent_pair(self):
        """Test JevJudge returns equivalent=False when edge-case divergence exists."""
        mock_client = MagicMock()
        mock_client.is_available.return_value = True
        mock_client.query_decisions.return_value = {
            "answers": {
                "resolution_equivalence": {
                    "type": "choice",
                    "choice": "divergent",
                    "confidence": 0.85,
                },
                "equivalence_probability": {"type": "noul", "noul": 0.40},
            }
        }

        judge = JevJudge(client=mock_client)
        pair = CandidatePair(
            pair_id="p2",
            venue_a="polymarket",
            question_a="Will TikTok be banned in the US in 2025?",
            venue_b="kalshi",
            question_b="Will TikTok be sold by ByteDance by Dec 2025?",
        )

        judgment = judge.judge_pair(pair)
        self.assertFalse(judgment.equivalent)

    def test_verify_cross_platform_equivalence_jev(self):
        """Test matcher gate verification helper."""
        mock_client = MagicMock()
        mock_client.is_available.return_value = True
        mock_client.query_decisions.return_value = {
            "answers": {
                "is_equivalent": {
                    "type": "choice",
                    "choice": "identical",
                    "confidence": 0.90,
                },
                "probability": {"type": "noul", "noul": 0.92},
            }
        }

        equiv, conf, reason = verify_cross_platform_equivalence_jev(
            {"question": "Will Fed cut rates in Dec 2026?"},
            {"title": "Fed rate cut at December 2026 meeting?"},
            "polymarket",
            "kalshi",
            client=mock_client,
        )
        self.assertTrue(equiv)
        self.assertAlmostEqual(conf, 0.90)


if __name__ == "__main__":
    unittest.main()
