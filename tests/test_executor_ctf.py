"""Unit tests for CTF execution legs, revalidation, and dry-run simulation in executor.py."""

from __future__ import annotations

import os
import sys
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ctf_api import CTFClient
from db import TradeDB
from executor import ArbitrageExecutor
from risk_manager import RiskManager


class TestExecutorCTF:
    """Test suite for executor CTF leg construction, revalidation, and simulation."""

    def _setup_executor(self, dry_run: bool = True) -> ArbitrageExecutor:
        import sys
        import executor as executor_mod
        curr_class = getattr(sys.modules.get("executor"), "ArbitrageExecutor", ArbitrageExecutor)
        db = MagicMock(spec=TradeDB)
        risk = MagicMock(spec=RiskManager)
        ctf_client = CTFClient(dry_run=dry_run)
        return curr_class(
            pm_trader=None,
            kalshi_client=None,
            db=db,
            risk_manager=risk,
            dry_run=dry_run,
            ctf_client=ctf_client,
        )

    def test_build_legs_ctf_merge(self) -> None:
        """Verify _build_legs creates YES BUY, NO BUY, and CTF MERGE legs."""
        executor = self._setup_executor()
        opp = {
            "type": "CTFMerge",
            "prices": "Y=0.420 N=0.480",
            "total_cost": "$0.9000",
            "net_profit": 0.05,
            "_token_ids": ["token_yes_123", "token_no_456"],
            "_condition_id": "0x1122334455667788990011223344556677889900112233445566778899001122",
        }

        # Sized at $18.00 -> 20 contracts at $0.90 unit cost
        legs = executor._build_legs(opp, size=18.0)
        assert len(legs) == 3

        # Leg 1: BUY YES on Polymarket
        assert legs[0]["platform"] == "polymarket"
        assert legs[0]["side"] == "BUY"
        assert legs[0]["token"] == "yes"
        assert legs[0]["price"] == 0.42
        assert legs[0]["_contracts"] == 20

        # Leg 2: BUY NO on Polymarket
        assert legs[1]["platform"] == "polymarket"
        assert legs[1]["side"] == "BUY"
        assert legs[1]["token"] == "no"
        assert legs[1]["price"] == 0.48
        assert legs[1]["_contracts"] == 20

        # Leg 3: MERGE on-chain
        assert legs[2]["platform"] == "polymarket_ctf"
        assert legs[2]["action"] == "merge"
        assert legs[2]["condition_id"] == "0x1122334455667788990011223344556677889900112233445566778899001122"
        assert legs[2]["_contracts"] == 20
        assert legs[2]["price"] == 1.0

    def test_build_legs_ctf_mint(self) -> None:
        """Verify _build_legs creates CTF SPLIT, YES SELL, and NO SELL legs."""
        executor = self._setup_executor()
        opp = {
            "type": "CTFMint",
            "prices": "Y=0.550 N=0.530",
            "total_cost": "$1.0000",
            "net_profit": 0.06,
            "_token_ids": ["token_yes_123", "token_no_456"],
            "_condition_id": "0x2233445566778899001122334455667788990011223344556677889900112233",
        }

        # Sized at $20.00 -> 20 contracts at $1.00 unit cost
        legs = executor._build_legs(opp, size=20.0)
        assert len(legs) == 3

        # Leg 1: SPLIT on-chain
        assert legs[0]["platform"] == "polymarket_ctf"
        assert legs[0]["action"] == "split"
        assert legs[0]["condition_id"] == "0x2233445566778899001122334455667788990011223344556677889900112233"
        assert legs[0]["_contracts"] == 20
        assert legs[0]["price"] == 1.0

        # Leg 2: SELL YES on Polymarket
        assert legs[1]["platform"] == "polymarket"
        assert legs[1]["side"] == "SELL"
        assert legs[1]["token"] == "yes"
        assert legs[1]["price"] == 0.55
        assert legs[1]["_contracts"] == 20

        # Leg 3: SELL NO on Polymarket
        assert legs[2]["platform"] == "polymarket"
        assert legs[2]["side"] == "SELL"
        assert legs[2]["token"] == "no"
        assert legs[2]["price"] == 0.53
        assert legs[2]["_contracts"] == 20

    def test_supports_concurrent_rejects_polymarket_ctf(self) -> None:
        """Verify _supports_concurrent returns False for legs containing polymarket_ctf."""
        executor = self._setup_executor()
        legs_with_ctf = [
            {"platform": "polymarket", "side": "BUY", "price": 0.45},
            {"platform": "polymarket_ctf", "action": "merge", "price": 1.0},
        ]
        assert executor._supports_concurrent(legs_with_ctf) is False

        legs_without_ctf = [
            {"platform": "polymarket", "side": "BUY", "price": 0.45},
            {"platform": "kalshi", "side": "BUY", "price": 0.50},
        ]
        assert executor._supports_concurrent(legs_without_ctf) is True

    def test_revalidate_ctf_merge_success_and_failure(self) -> None:
        """Verify _revalidate_ctf for CTFMerge passes when asks < 1.00 and fails when asks rise."""
        import executor as executor_mod
        import polymarket_api
        executor = self._setup_executor()
        opp = {
            "type": "CTFMerge",
            "prices": "Y=0.420 N=0.450",
            "total_cost": "$0.8700",
            "net_profit": 0.08,
            "_token_ids": ["token_yes_123", "token_no_456"],
        }
        mock_book = {"bids": [], "asks": []}
        with patch.object(executor_mod, "fetch_order_book", return_value=mock_book), \
             patch.object(executor_mod, "get_best_bid_ask") as mock_gba, \
             patch.object(polymarket_api, "fetch_order_book", return_value=mock_book), \
             patch.object(polymarket_api, "get_best_bid_ask", mock_gba):
            # 1. Asks remain low (0.42 and 0.45) -> passes
            mock_gba.side_effect = [
                {"ask": 0.42, "bid": 0.40},
                {"ask": 0.45, "bid": 0.43},
            ]
            passed, profit, reason = executor._revalidate_ctf(opp, 0.08, price_cache=None)
            assert passed is True
            assert reason == "passed"

            # 2. Asks rise to 0.52 and 0.51 (sum 1.03) -> profit degrades -> fails
            mock_gba.side_effect = [
                {"ask": 0.52, "bid": 0.50},
                {"ask": 0.51, "bid": 0.49},
            ]
            passed, profit, reason = executor._revalidate_ctf(opp, 0.08, price_cache=None)
            assert passed is False
            assert reason == "profit_below_floor"

    def test_revalidate_ctf_mint_success_and_failure(self) -> None:
        """Verify _revalidate_ctf for CTFMint passes when bids > 1.00 and fails when bids fall."""
        import executor as executor_mod
        import polymarket_api
        executor = self._setup_executor()
        opp = {
            "type": "CTFMint",
            "prices": "Y=0.550 N=0.530",
            "total_cost": "$1.0000",
            "net_profit": 0.04,
            "_token_ids": ["token_yes_123", "token_no_456"],
        }
        mock_book = {"bids": [], "asks": []}
        with patch.object(executor_mod, "fetch_order_book", return_value=mock_book), \
             patch.object(executor_mod, "get_best_bid_ask") as mock_gba, \
             patch.object(polymarket_api, "fetch_order_book", return_value=mock_book), \
             patch.object(polymarket_api, "get_best_bid_ask", mock_gba):
            # 1. Bids remain high (0.55 and 0.53) -> passes
            mock_gba.side_effect = [
                {"ask": 0.57, "bid": 0.55},
                {"ask": 0.55, "bid": 0.53},
            ]
            passed, profit, reason = executor._revalidate_ctf(opp, 0.04, price_cache=None)
            assert passed is True
            assert reason == "passed"

            # 2. Bids fall to 0.48 and 0.47 (sum 0.95) -> fails
            mock_gba.side_effect = [
                {"ask": 0.50, "bid": 0.48},
                {"ask": 0.49, "bid": 0.47},
            ]
            passed, profit, reason = executor._revalidate_ctf(opp, 0.04, price_cache=None)
            assert passed is False
            assert reason == "profit_below_floor"

    def test_execute_single_leg_dry_run_simulation(self) -> None:
        """Verify _execute_single_leg simulates CTF calldata build in dry-run mode."""
        executor = self._setup_executor(dry_run=True)
        leg = {
            "platform": "polymarket_ctf",
            "action": "merge",
            "condition_id": "0x" + "11" * 32,
            "_contracts": 10,
            "price": 1.0,
        }
        opp = {"type": "CTFMerge", "market": "Test market"}

        # Enable polymarket in whitelist so guard passes
        with patch("executor.ENABLED_EXECUTION_PLATFORMS", frozenset({"polymarket", "kalshi"})):
            success, tx_id, fill_price = executor._execute_single_leg(leg, 10.0, opp)
            assert success is True
            assert tx_id is not None
            assert tx_id.startswith("0x")
            assert fill_price == 1.0
            assert leg["_fill_price"] == 1.0
            assert leg["_fill_qty"] == 10

    def test_execute_single_leg_live_fails_closed_in_phase_4a(self) -> None:
        """Phase 4a constraint: live execution must raise NotImplementedError."""
        executor = self._setup_executor(dry_run=False)
        leg = {
            "platform": "polymarket_ctf",
            "action": "merge",
            "condition_id": "0x" + "11" * 32,
            "_contracts": 10,
            "price": 1.0,
        }
        opp = {"type": "CTFMerge", "market": "Test market"}

        with patch("executor.ENABLED_EXECUTION_PLATFORMS", frozenset({"polymarket", "kalshi"})):
            with pytest.raises(NotImplementedError):
                executor._execute_single_leg(leg, 10.0, opp)
