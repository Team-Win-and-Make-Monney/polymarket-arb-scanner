"""Tests for cross-platform inverted pair persistence and execution."""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import time
from db import TradeDB
from risk_manager import RiskManager
from executor import ArbitrageExecutor


# ---------------------------------------------------------------------------
# TestCrossInvertedExecution
# ---------------------------------------------------------------------------

class TestCrossInvertedExecution:
    """Test that inverted cross-platform arbitrage legs trade opposing sides."""

    def _make_executor(self) -> ArbitrageExecutor:
        return ArbitrageExecutor(
            pm_trader=MagicMock(),
            kalshi_client=MagicMock(),
            db=TradeDB(":memory:"),
            risk_manager=RiskManager({
                "max_trade_size": 10.0,
                "daily_loss_limit": 50.0,
                "max_open_positions": 5,
            }),
            dry_run=True,
        )

    def test_normal_pair_buys_pm_yes_and_kalshi_no(self):
        executor = self._make_executor()
        opp = {
            "type": "Cross(PM_YES + K_NO)",
            "prices": "PM_Y=0.400 K_N=0.550",
            "_kalshi_ticker": "KXTEST",
            "_token_ids": ["token_yes_123", "token_no_456"],
            "_inverted": False,
        }

        legs = executor._build_legs(opp, size=10.0)

        assert len(legs) == 2
        pm_leg, k_leg = legs[0], legs[1]
        assert pm_leg["platform"] == "polymarket"
        assert pm_leg["side"] == "BUY"
        assert pm_leg["token"] == "yes"
        assert pm_leg["_token_id"] == "token_yes_123"

        assert k_leg["platform"] == "kalshi"
        assert k_leg["side"] == "no"
        assert k_leg["action"] == "buy"
        assert k_leg["_ticker"] == "KXTEST"

    def test_inverted_pair_buys_pm_yes_and_kalshi_yes(self):
        executor = self._make_executor()
        opp = {
            "type": "Cross(PM_YES + K_NO)",
            "prices": "PM_Y=0.400 K_N=0.550",
            "_kalshi_ticker": "KXTEST",
            "_token_ids": ["token_yes_123", "token_no_456"],
            "_inverted": True,
        }

        legs = executor._build_legs(opp, size=10.0)

        assert len(legs) == 2
        pm_leg, k_leg = legs[0], legs[1]
        assert pm_leg["platform"] == "polymarket"
        assert pm_leg["side"] == "BUY"
        assert pm_leg["token"] == "yes"
        assert pm_leg["_token_id"] == "token_yes_123"

        # Inverted: K_N synthetic price corresponds to Kalshi YES contract
        assert k_leg["platform"] == "kalshi"
        assert k_leg["side"] == "yes"
        assert k_leg["action"] == "buy"
        assert k_leg["_ticker"] == "KXTEST"

    def test_inverted_pair_buys_pm_no_and_kalshi_no(self):
        executor = self._make_executor()
        opp = {
            "type": "Cross(PM_NO + K_YES)",
            "prices": "PM_N=0.600 K_Y=0.350",
            "_kalshi_ticker": "KXTEST",
            "_token_ids": ["token_yes_123", "token_no_456"],
            "_inverted": True,
        }

        legs = executor._build_legs(opp, size=10.0)

        assert len(legs) == 2
        pm_leg, k_leg = legs[0], legs[1]
        assert pm_leg["platform"] == "polymarket"
        assert pm_leg["side"] == "BUY"
        assert pm_leg["token"] == "no"
        assert pm_leg["_token_id"] == "token_no_456"

        # Inverted: K_Y synthetic price corresponds to Kalshi NO contract
        assert k_leg["platform"] == "kalshi"
        assert k_leg["side"] == "no"
        assert k_leg["action"] == "buy"
        assert k_leg["_ticker"] == "KXTEST"


# ---------------------------------------------------------------------------
# TestCrossInvertedRevalidation
# ---------------------------------------------------------------------------

class TestCrossInvertedRevalidation:
    """Test that _revalidate_cross inverts Kalshi book asks when _inverted is True."""

    def _make_executor(self) -> ArbitrageExecutor:
        return ArbitrageExecutor(
            pm_trader=MagicMock(),
            kalshi_client=MagicMock(),
            db=TradeDB(":memory:"),
            risk_manager=RiskManager({
                "max_trade_size": 10.0,
                "daily_loss_limit": 50.0,
                "max_open_positions": 5,
            }),
            dry_run=True,
        )

    def test_revalidate_cross_swaps_kalshi_prices_when_inverted(self):
        executor = self._make_executor()
        now = time.time()
        # Mock price cache with fresh prices
        # Kalshi has yes_ask = 0.40, no_ask = 0.60
        # Inverted means Kalshi YES/NO are inverted relative to PM:
        # so Kalshi yes_ask (0.40) corresponds to synthetic K_NO ask!
        price_cache = {
            ("polymarket", "token_yes_123"): {"best_ask": 0.30, "_stale": False, "_ts": now},
            ("polymarket", "token_no_456"): {"best_ask": 0.70, "_stale": False, "_ts": now},
            ("kalshi", "KXTEST"): {"yes_ask": 0.40, "no_ask": 0.60, "_stale": False, "_ts": now},
        }
        opp = {
            "type": "Cross(PM_YES + K_NO)",
            "prices": "PM_Y=0.300 K_N=0.400",
            "total_cost": "$0.70",
            "_kalshi_ticker": "KXTEST",
            "_token_ids": ["token_yes_123", "token_no_456"],
            "_inverted": True,
            "net_profit": 0.25,
        }

        with patch("config.JEV_CROSS_EQUIVALENCE_ENABLED", False):
            passed, reval_profit, reason = executor._revalidate_cross(opp, 0.25, price_cache)

        assert passed is True
        assert reason == "passed"
        # When inverted, Kalshi yes_ask (0.40) is treated as k_no, so pm_yes (0.30) + k_no (0.40) = 0.70 cost
        assert "PM_Y=0.300" in opp["prices"]
        assert "K_N=0.400" in opp["prices"]
