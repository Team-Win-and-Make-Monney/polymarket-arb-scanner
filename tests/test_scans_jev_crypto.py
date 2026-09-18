"""Unit tests for scans.jev_crypto module."""

from __future__ import annotations

import json
import os
import sys
import unittest
from unittest.mock import MagicMock, patch

# Stub external dependencies if not installed in current environment
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

from scans.jev_crypto import (
    _extract_strike_and_asset,
    scan_jev_crypto,
)


class TestScansJevCrypto(unittest.TestCase):
    """Test suite for Jev crypto scanning and refinement logic."""

    def test_extract_strike_and_asset(self):
        """Test asset, strike price, and direction extraction."""
        asset, strike, direction = _extract_strike_and_asset("Will Bitcoin reach $90,000 by December 31, 2026?")
        self.assertEqual(asset, "BTC")
        self.assertEqual(strike, 90000.0)
        self.assertEqual(direction, "reach")

        asset, strike, direction = _extract_strike_and_asset("Will Ethereum dip to $2,500 by end of month?")
        self.assertEqual(asset, "ETH")
        self.assertEqual(strike, 2500.0)
        self.assertEqual(direction, "dip")

        asset, strike, direction = _extract_strike_and_asset("Will Solana reach $180 by July?")
        self.assertEqual(asset, "SOL")
        self.assertEqual(strike, 180.0)
        self.assertEqual(direction, "reach")

        asset, strike, direction = _extract_strike_and_asset("Will Ripple / XRP reach $2.50 by end of year?")
        self.assertEqual(asset, "XRP")
        self.assertEqual(strike, 2.5)
        self.assertEqual(direction, "reach")

        asset, strike, direction = _extract_strike_and_asset("Will Donald Trump win the election?")
        self.assertIsNone(asset)
        self.assertIsNone(strike)
        self.assertIsNone(direction)

        # Word boundary protection: words containing 'sol' or 'eth' shouldn't falsely trigger
        asset, strike, direction = _extract_strike_and_asset("Will dispute resolution conclude by $50?")
        self.assertIsNone(asset)
        asset, strike, direction = _extract_strike_and_asset("Whether method yields under $10?")
        self.assertIsNone(asset)

    @patch("scans.jev_crypto.JEV_CRYPTO_ENABLED", False)
    def test_disabled_scan_returns_empty(self):
        """Test scan returns empty list when JEV_CRYPTO_ENABLED is False."""
        res = scan_jev_crypto({"m1": {"question": "Will Bitcoin reach $90,000?"}})
        self.assertEqual(res, [])

    @patch("scans.jev_crypto._fetch_clob_for_market")
    @patch("scans.jev_crypto.JEV_CRYPTO_ENABLED", False)
    def test_force_override_runs_when_flag_false(self, mock_clob):
        """Test force=True bypasses JEV_CRYPTO_ENABLED=False."""
        mock_clob.return_value = {
            "yes_ask": 0.40, "no_ask": 0.60, "yes_ask_size": 500.0, "no_ask_size": 500.0
        }
        mock_client = MagicMock()
        mock_client.is_available.return_value = True
        mock_client.query_decisions.return_value = {
            "answers": {
                "strike_probability": {"type": "noul", "noul": 0.70},
                "recommended_action": {"type": "choice", "choice": "buy_yes", "confidence": 0.85},
                "tail_risk": {"type": "score", "score": 1.0},
                "conviction": {"type": "score", "score": 1.5},
            }
        }
        mock_market = {
            "question": "Will Bitcoin reach $90,000 by December 31, 2026?",
            "outcomePrices": json.dumps(["0.40", "0.60"]),
            "clobTokenIds": ["token_yes", "token_no"],
            "endDate": "2026-12-31T23:59:59Z",
        }
        spot_prices = {"BTC": {"price": 81000.0, "change_24h": 3.5, "vwap_24h": 80500.0}}
        opps = scan_jev_crypto(
            markets_by_key={"btc-90k": mock_market},
            spot_prices=spot_prices,
            min_profit=0.01,
            jev_client=mock_client,
            force=True,
        )
        self.assertEqual(len(opps), 1)

    @patch("scans.jev_crypto._fetch_clob_for_market")
    @patch("scans.jev_crypto.JEV_CRYPTO_ENABLED", True)
    def test_successful_opportunity_emission(self, mock_clob):
        """Test opportunity emission when Jev signals mispricing with high confidence."""
        mock_clob.return_value = {
            "yes_ask": 0.40,
            "no_ask": 0.60,
            "yes_ask_size": 250.0,
            "no_ask_size": 300.0,
        }

        mock_client = MagicMock()
        mock_client.is_available.return_value = True
        mock_client.query_decisions.return_value = {
            "answers": {
                "strike_probability": {"type": "noul", "noul": 0.65},
                "recommended_action": {
                    "type": "choice",
                    "choice": "buy_yes",
                    "confidence": 0.82,
                },
                "tail_risk": {"type": "score", "score": 1.1},
                "conviction": {"type": "score", "score": 1.5},
            }
        }

        mock_market = {
            "question": "Will Bitcoin reach $90,000 by December 31, 2026?",
            "outcomePrices": json.dumps(["0.40", "0.60"]),
            "clobTokenIds": ["token_yes", "token_no"],
            "endDate": "2026-12-31T23:59:59Z",
        }

        spot_prices = {
            "BTC": {"price": 81000.0, "change_24h": 3.5, "vwap_24h": 80500.0}
        }

        opps = scan_jev_crypto(
            markets_by_key={"btc-90k": mock_market},
            spot_prices=spot_prices,
            min_profit=0.01,
            jev_client=mock_client,
            force=True,
        )

        self.assertEqual(len(opps), 1)
        opp = opps[0]
        self.assertEqual(opp["type"], "JevCrypto")
        self.assertEqual(opp["_action"], "buy_yes")
        self.assertEqual(opp["_clob_depth"], 250.0)
        self.assertAlmostEqual(opp["_model_prob"], 0.65)
        self.assertAlmostEqual(opp["_confidence"], 0.82)
        self.assertGreater(opp["net_profit"], 0.0)

    @patch("scans.jev_crypto._fetch_clob_for_market")
    @patch("scans.jev_crypto.JEV_CRYPTO_ENABLED", True)
    def test_pass_action_filtered(self, mock_clob):
        """Test that pass_fair choices are filtered out without emitting trades."""
        mock_clob.return_value = {
            "yes_ask": 0.55,
            "no_ask": 0.45,
            "yes_ask_size": 200.0,
            "no_ask_size": 200.0,
        }

        mock_client = MagicMock()
        mock_client.is_available.return_value = True
        mock_client.query_decisions.return_value = {
            "answers": {
                "strike_probability": {"type": "noul", "noul": 0.50},
                "recommended_action": {
                    "type": "choice",
                    "choice": "pass_fair",
                    "confidence": 0.75,
                },
                "tail_risk": {"type": "score", "score": 1.0},
                "conviction": {"type": "score", "score": 0.5},
            }
        }

        mock_market = {
            "question": "Will Bitcoin reach $90,000 by December 31, 2026?",
            "outcomePrices": json.dumps(["0.50", "0.50"]),
            "clobTokenIds": ["token_yes", "token_no"],
            "endDate": "2026-12-31T23:59:59Z",
        }

        spot_prices = {
            "BTC": {"price": 81000.0, "change_24h": 3.5, "vwap_24h": 80500.0}
        }

        opps = scan_jev_crypto(
            markets_by_key={"btc-90k": mock_market},
            spot_prices=spot_prices,
            min_profit=0.01,
            jev_client=mock_client,
            force=True,
        )

        self.assertEqual(len(opps), 0)

    @patch("scans.jev_crypto._fetch_clob_for_market")
    @patch("scans.jev_crypto.JEV_CRYPTO_ENABLED", True)
    def test_db_logging_decision(self, mock_clob):
        """Test that every evaluated decision is recorded in TradeDB."""
        mock_clob.return_value = {
            "yes_ask": 0.45,
            "no_ask": 0.55,
            "yes_ask_size": 100.0,
            "no_ask_size": 100.0,
        }

        mock_client = MagicMock()
        mock_client.is_available.return_value = True
        mock_client.query_decisions.return_value = {
            "answers": {
                "strike_probability": {"type": "noul", "noul": 0.50},
                "recommended_action": {"type": "choice", "choice": "pass_fair", "confidence": 0.80},
                "tail_risk": {"type": "score", "score": 1.0},
                "conviction": {"type": "score", "score": 0.2},
            }
        }

        mock_market = {
            "question": "Will Solana reach $150 by December 31, 2026?",
            "outcomePrices": json.dumps(["0.45", "0.55"]),
            "clobTokenIds": ["sol_yes", "sol_no"],
            "endDate": "2026-12-31T23:59:59Z",
        }

        mock_db = MagicMock()

        scan_jev_crypto(
            markets_by_key={"sol-150": mock_market},
            spot_prices={"SOL": {"price": 125.0, "change_24h": 2.0, "vwap_24h": 124.0}},
            min_profit=0.01,
            jev_client=mock_client,
            db=mock_db,
            force=True,
        )

        self.assertTrue(mock_db.record_jev_decision.called)
        call_kwargs = mock_db.record_jev_decision.call_args[1]
        self.assertEqual(call_kwargs["asset"], "SOL")
        self.assertEqual(call_kwargs["strike"], 150.0)
        self.assertEqual(call_kwargs["action"], "pass_fair")
        self.assertEqual(call_kwargs["spot"], 125.0)


if __name__ == "__main__":
    unittest.main()
