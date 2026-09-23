"""Tests for Jev-powered adverse selection & toxic flow shield."""

import os
import sys
import unittest
from unittest.mock import MagicMock, patch

# Ensure project root is in sys.path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Stubs for external dependencies
_INSTALLED_STUBS = []
for mod in [
    "dotenv",
    "httpx",
    "requests",
    "requests.adapters",
    "tenacity",
    "cryptography",
    "cryptography.hazmat",
    "cryptography.hazmat.primitives",
    "cryptography.hazmat.primitives.asymmetric",
    "thefuzz",
    "thefuzz.fuzz",
    "thefuzz.process",
    "kalshi_api",
    "py_clob_client_v2",
    "py_clob_client_v2.client",
    "py_clob_client_v2.clob_types",
    "py_clob_client_v2.http_helpers",
    "py_clob_client_v2.http_helpers.helpers",
]:
    if mod not in sys.modules:
        try:
            __import__(mod)
        except ImportError:
            m = MagicMock()
            m.__path__ = []
            sys.modules[mod] = m
            _INSTALLED_STUBS.append(mod)


def tearDownModule():
    for mod in _INSTALLED_STUBS:
        if mod in sys.modules:
            del sys.modules[mod]


class TestToxicFlowJev(unittest.TestCase):
    """Test suite for Jev-augmented ToxicFlowDetector and toxic_flow_pause scan."""

    def setUp(self):
        from market_maker import ToxicFlowDetector
        self.detector = ToxicFlowDetector(lookback_trades=10, toxicity_threshold=0.60)

    def test_evaluate_spot_toxicity_triggers_pause_on_toxic_flow(self):
        """When Jev returns high adverse selection probability, detector should pause market."""
        mock_client = MagicMock()
        mock_client.is_available.return_value = True
        mock_client.query_decisions.return_value = {
            "answers": {
                "adverse_selection": {"type": "noul", "noul": 0.85},
                "toxicity_score": {"type": "score", "score": 1.8, "confidence": 0.9},
            }
        }

        with patch("config.MM_TOXIC_FLOW_ENABLED", True):
            should_pause, score, reason = self.detector.evaluate_spot_toxicity_with_jev(
                market_key="pm-btc-100k",
                asset="BTC",
                spot_delta_pct=4.2,
                recent_fill_skew=0.8,
                client=mock_client,
            )

            self.assertTrue(should_pause)
            self.assertGreaterEqual(score, 1.4)
            self.assertIn("Jev toxicity", reason)
            self.assertTrue(self.detector.should_pause("pm-btc-100k"))
            self.assertEqual(self.detector.get_pause_reason("pm-btc-100k"), reason)

    def test_evaluate_spot_toxicity_safe_on_benign_move(self):
        """When Jev evaluates spot move as benign, detector should not trigger pause."""
        mock_client = MagicMock()
        mock_client.is_available.return_value = True
        mock_client.query_decisions.return_value = {
            "answers": {
                "adverse_selection": {"type": "noul", "noul": 0.15},
                "toxicity_score": {"type": "score", "score": 0.2, "confidence": 0.95},
            }
        }

        should_pause, score, reason = self.detector.evaluate_spot_toxicity_with_jev(
            market_key="pm-eth-3k",
            asset="ETH",
            spot_delta_pct=0.1,
            recent_fill_skew=0.0,
            client=mock_client,
        )

        self.assertFalse(should_pause)
        self.assertLess(score, 1.0)
        self.assertFalse(self.detector.should_pause("pm-eth-3k"))

    def test_scan_toxic_flow_pause_with_spot_deltas(self):
        """scan_toxic_flow_pause should evaluate spot deltas and surface paused markets."""
        from scans.toxic_flow_pause import scan_toxic_flow_pause

        mock_client = MagicMock()
        mock_client.is_available.return_value = True
        mock_client.query_decisions.return_value = {
            "answers": {
                "adverse_selection": {"type": "noul", "noul": 0.90},
                "toxicity_score": {"type": "score", "score": 1.9, "confidence": 0.92},
            }
        }

        spot_deltas = {
            "pm-sol-200": {
                "asset": "SOL",
                "spot_delta_pct": -5.5,
                "fill_skew": -0.9,
            }
        }

        with patch("config.MM_TOXIC_FLOW_ENABLED", True):
            opps = scan_toxic_flow_pause(
                market_keys=["pm-sol-200"],
                detector=self.detector,
                spot_deltas=spot_deltas,
                jev_client=mock_client,
            )

        self.assertEqual(len(opps), 1)
        opp = opps[0]
        self.assertEqual(opp["type"], "ToxicFlowPause")
        self.assertEqual(opp["_market_key"], "pm-sol-200")
        self.assertIn("Jev toxicity", opp["reason"])


if __name__ == "__main__":
    unittest.main()
