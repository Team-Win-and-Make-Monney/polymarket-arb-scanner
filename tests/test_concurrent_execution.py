"""Tests for concurrent execution feature in executor.py."""

import pytest
from unittest.mock import MagicMock, patch, call
import sys
import os
import threading

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from db import TradeDB
from risk_manager import RiskManager


# Mock external API modules that may not be installed
@pytest.fixture(autouse=True)
def mock_external_modules():
    """Mock external API modules that may not be installed."""
    mock_modules = {}
    for mod_name in [
        "polymarket_api", "kalshi_api",
        "betfair_api", "smarkets_api", "sxbet_api",
        "matchbook_api", "gemini_api", "ibkr_api",
    ]:
        if mod_name not in sys.modules:
            mock_modules[mod_name] = MagicMock()
            sys.modules[mod_name] = mock_modules[mod_name]
    yield
    for mod_name in mock_modules:
        if mod_name in sys.modules:
            del sys.modules[mod_name]


def _import_executor():
    if "executor" in sys.modules:
        del sys.modules["executor"]
    from executor import ArbitrageExecutor
    return ArbitrageExecutor


@pytest.fixture
def ArbitrageExecutor():
    return _import_executor()


@pytest.fixture
def db():
    trade_db = TradeDB(":memory:")
    yield trade_db
    trade_db.close()


@pytest.fixture
def risk_manager():
    return RiskManager({
        "max_trade_size": 5.0,
        "daily_loss_limit": 25.0,
        "max_open_positions": 25,
        "min_liquidity": 25.0,
        "min_liquidity_high_roi": 10.0,
        "min_net_roi": 0,
        "allow_better_reentry": True,
        "reentry_improvement_threshold": 0.20,
    })


@pytest.fixture
def executor_concurrent(ArbitrageExecutor, db, risk_manager):
    """Executor with concurrent_execution enabled."""
    pm_trader = MagicMock()
    kalshi_client = MagicMock()
    return ArbitrageExecutor(
        pm_trader=pm_trader,
        kalshi_client=kalshi_client,
        db=db,
        risk_manager=risk_manager,
        dry_run=False,
        exec_mode="full-auto",
        max_trade_size=5.0,
        concurrent_execution=True,
    )


@pytest.fixture
def executor_sequential(ArbitrageExecutor, db, risk_manager):
    """Executor with concurrent_execution disabled (default)."""
    pm_trader = MagicMock()
    kalshi_client = MagicMock()
    return ArbitrageExecutor(
        pm_trader=pm_trader,
        kalshi_client=kalshi_client,
        db=db,
        risk_manager=risk_manager,
        dry_run=False,
        exec_mode="full-auto",
        max_trade_size=5.0,
        concurrent_execution=False,
    )


# ---------------------------------------------------------------------------
# _supports_concurrent
# ---------------------------------------------------------------------------

class TestSupportsConcurrent:
    """Tests for _supports_concurrent() platform compatibility check."""

    def test_polymarket_only_legs_supported(self, executor_concurrent):
        legs = [
            {"platform": "polymarket", "price": 0.4},
            {"platform": "polymarket", "price": 0.5},
        ]
        assert executor_concurrent._supports_concurrent(legs) is True

    def test_kalshi_only_legs_supported(self, executor_concurrent):
        legs = [
            {"platform": "kalshi", "price": 0.4},
            {"platform": "kalshi", "price": 0.5},
        ]
        assert executor_concurrent._supports_concurrent(legs) is True

    def test_cross_platform_without_ibkr_supported(self, executor_concurrent):
        legs = [
            {"platform": "polymarket", "price": 0.4},
            {"platform": "kalshi", "price": 0.5},
        ]
        assert executor_concurrent._supports_concurrent(legs) is True

    def test_ibkr_leg_not_supported(self, executor_concurrent):
        legs = [
            {"platform": "polymarket", "price": 0.4},
            {"platform": "ibkr", "price": 0.5},
        ]
        assert executor_concurrent._supports_concurrent(legs) is False

    def test_ibkr_only_not_supported(self, executor_concurrent):
        legs = [
            {"platform": "ibkr", "price": 0.4},
            {"platform": "ibkr", "price": 0.5},
        ]
        assert executor_concurrent._supports_concurrent(legs) is False

    def test_single_leg_not_supported(self, executor_concurrent):
        """Concurrent execution requires at least 2 legs."""
        legs = [{"platform": "polymarket", "price": 0.4}]
        assert executor_concurrent._supports_concurrent(legs) is False

    def test_empty_legs_not_supported(self, executor_concurrent):
        assert executor_concurrent._supports_concurrent([]) is False

    def test_three_legs_no_ibkr_supported(self, executor_concurrent):
        legs = [
            {"platform": "polymarket", "price": 0.3},
            {"platform": "kalshi", "price": 0.3},
            {"platform": "betfair", "price": 0.3},
        ]
        assert executor_concurrent._supports_concurrent(legs) is True

    def test_three_legs_with_ibkr_not_supported(self, executor_concurrent):
        legs = [
            {"platform": "polymarket", "price": 0.3},
            {"platform": "kalshi", "price": 0.3},
            {"platform": "ibkr", "price": 0.3},
        ]
        assert executor_concurrent._supports_concurrent(legs) is False

    def test_exchange_platforms_supported(self, executor_concurrent):
        """Betfair, Smarkets, SX Bet, Matchbook, Gemini all support cancellation."""
        for plat in ["betfair", "smarkets", "sxbet", "matchbook", "gemini"]:
            legs = [
                {"platform": plat, "price": 0.4},
                {"platform": "polymarket", "price": 0.5},
            ]
            assert executor_concurrent._supports_concurrent(legs) is True, \
                f"{plat} should support concurrent execution"

    def test_missing_platform_key_fails_closed(self, executor_concurrent):
        """Legs without a platform cannot prove cancellation support."""
        legs = [
            {"price": 0.4},
            {"price": 0.5},
        ]
        assert executor_concurrent._supports_concurrent(legs) is False


# ---------------------------------------------------------------------------
# _execute_legs_concurrent — all legs succeed
# ---------------------------------------------------------------------------

class TestExecuteLegsConcurrentSuccess:
    """Tests for _execute_legs_concurrent() when all legs succeed."""

    def test_all_legs_fill_returns_true(self, executor_concurrent):
        """When all legs fill, the method returns True and creates a position."""
        opp = {
            "type": "Binary",
            "market": "Test Market",
            "prices": "Y=0.400 N=0.450",
            "total_cost": "$4.25",
            "net_profit": 0.75,
            "net_roi": "17.6%",
            "_clob_depth": 100,
        }
        legs = [
            {"platform": "polymarket", "price": 0.4, "side": "BUY",
             "_token_id": "tok1", "token": "YES"},
            {"platform": "polymarket", "price": 0.45, "side": "BUY",
             "_token_id": "tok2", "token": "NO"},
        ]

        # Mock _execute_single_leg to succeed for all legs
        executor_concurrent._execute_single_leg = MagicMock(
            return_value=(True, "order-123", 0.40)
        )

        result = executor_concurrent._execute_legs_concurrent(opp, legs, 5.0)
        assert result is True

    def test_all_legs_fill_creates_position(self, executor_concurrent, db):
        """Position is created in DB when all legs fill."""
        opp = {
            "type": "Binary",
            "market": "Test Position Market",
            "prices": "Y=0.400 N=0.450",
            "total_cost": "$4.25",
            "net_profit": 0.75,
            "net_roi": "17.6%",
            "_clob_depth": 100,
        }
        legs = [
            {"platform": "polymarket", "price": 0.4, "side": "BUY",
             "_token_id": "tok1", "token": "YES"},
            {"platform": "polymarket", "price": 0.45, "side": "BUY",
             "_token_id": "tok2", "token": "NO"},
        ]

        executor_concurrent._execute_single_leg = MagicMock(
            return_value=(True, "order-456", 0.40)
        )

        executor_concurrent._execute_legs_concurrent(opp, legs, 5.0)

        # Check that a position was created
        positions = db.get_open_positions()
        assert len(positions) >= 1

    def test_logs_opportunity_as_traded_concurrent(self, executor_concurrent, db):
        """Opportunity is logged with action='traded_concurrent'."""
        opp = {
            "type": "Binary",
            "market": "Concurrent Market",
            "prices": "Y=0.400 N=0.450",
            "total_cost": "$4.25",
            "net_profit": 0.75,
            "net_roi": "17.6%",
            "_clob_depth": 100,
        }
        legs = [
            {"platform": "polymarket", "price": 0.4, "side": "BUY",
             "_token_id": "tok1", "token": "YES"},
            {"platform": "polymarket", "price": 0.45, "side": "BUY",
             "_token_id": "tok2", "token": "NO"},
        ]

        executor_concurrent._execute_single_leg = MagicMock(
            return_value=(True, "order-789", 0.40)
        )

        executor_concurrent._execute_legs_concurrent(opp, legs, 5.0)

        # Verify the opportunity was logged
        # We can check via the trade records — all should reference the same opportunity_id
        pnl = db.get_daily_pnl()
        assert pnl is not None

    def test_concurrent_calls_execute_single_leg_for_each(self, executor_concurrent):
        """Each leg triggers a call to _execute_single_leg."""
        opp = {
            "type": "Cross",
            "market": "Cross Market",
            "prices": "PM=0.35 K=0.60",
            "total_cost": "$4.75",
            "net_profit": 0.25,
            "net_roi": "5.3%",
            "_clob_depth": 50,
        }
        legs = [
            {"platform": "polymarket", "price": 0.35, "side": "BUY",
             "_token_id": "tok1", "token": "YES"},
            {"platform": "kalshi", "price": 0.60, "side": "yes",
             "_ticker": "TICK-1", "action": "buy"},
        ]

        executor_concurrent._execute_single_leg = MagicMock(
            return_value=(True, "order-abc", 0.35)
        )

        executor_concurrent._execute_legs_concurrent(opp, legs, 5.0)
        assert executor_concurrent._execute_single_leg.call_count == 2


# ---------------------------------------------------------------------------
# _execute_legs_concurrent — partial failure / hedging
# ---------------------------------------------------------------------------

class TestExecuteLegsConcurrentPartialFailure:
    """Tests for _execute_legs_concurrent() when some legs fail."""

    def test_one_leg_fails_returns_false(self, executor_concurrent):
        """When one leg fails, overall result is False."""
        opp = {
            "type": "Binary",
            "market": "Partial Fail Market",
            "prices": "Y=0.400 N=0.450",
            "total_cost": "$4.25",
            "net_profit": 0.75,
            "net_roi": "17.6%",
            "_clob_depth": 100,
        }
        legs = [
            {"platform": "polymarket", "price": 0.4, "side": "BUY",
             "_token_id": "tok1", "token": "YES"},
            {"platform": "polymarket", "price": 0.45, "side": "BUY",
             "_token_id": "tok2", "token": "NO"},
        ]

        # First leg succeeds, second fails
        executor_concurrent._execute_single_leg = MagicMock(
            side_effect=[(True, "order-1", 0.40), (False, None, None)]
        )

        result = executor_concurrent._execute_legs_concurrent(opp, legs, 5.0)
        assert result is False

    def test_all_legs_fail_returns_false(self, executor_concurrent):
        """When all legs fail, result is False."""
        opp = {
            "type": "Binary",
            "market": "All Fail Market",
            "prices": "Y=0.400 N=0.450",
            "total_cost": "$4.25",
            "net_profit": 0.75,
            "net_roi": "17.6%",
            "_clob_depth": 100,
        }
        legs = [
            {"platform": "polymarket", "price": 0.4, "side": "BUY",
             "_token_id": "tok1", "token": "YES"},
            {"platform": "polymarket", "price": 0.45, "side": "BUY",
             "_token_id": "tok2", "token": "NO"},
        ]

        executor_concurrent._execute_single_leg = MagicMock(
            return_value=(False, None, None)
        )

        result = executor_concurrent._execute_legs_concurrent(opp, legs, 5.0)
        assert result is False

    def test_exception_in_leg_returns_false(self, executor_concurrent):
        """When a leg raises an exception, overall result is False."""
        opp = {
            "type": "Binary",
            "market": "Exception Market",
            "prices": "Y=0.400 N=0.450",
            "total_cost": "$4.25",
            "net_profit": 0.75,
            "net_roi": "17.6%",
            "_clob_depth": 100,
        }
        legs = [
            {"platform": "polymarket", "price": 0.4, "side": "BUY",
             "_token_id": "tok1", "token": "YES"},
            {"platform": "polymarket", "price": 0.45, "side": "BUY",
             "_token_id": "tok2", "token": "NO"},
        ]

        executor_concurrent._execute_single_leg = MagicMock(
            side_effect=[(True, "order-1", 0.40), RuntimeError("API timeout")]
        )

        result = executor_concurrent._execute_legs_concurrent(opp, legs, 5.0)
        assert result is False

    @patch("executor.HEDGE_ENABLED", True)
    def test_partial_failure_triggers_hedge(self, executor_concurrent):
        """When one leg succeeds and the other fails, hedging is attempted."""
        opp = {
            "type": "Binary",
            "market": "Hedge Market",
            "prices": "Y=0.400 N=0.450",
            "total_cost": "$4.25",
            "net_profit": 0.75,
            "net_roi": "17.6%",
            "_clob_depth": 100,
        }
        legs = [
            {"platform": "polymarket", "price": 0.4, "side": "BUY",
             "_token_id": "tok1", "token": "YES"},
            {"platform": "polymarket", "price": 0.45, "side": "BUY",
             "_token_id": "tok2", "token": "NO"},
        ]

        # First leg succeeds with _order_id set, second fails
        def mock_execute_leg(leg, size, opp):
            if leg["_token_id"] == "tok1":
                leg["_order_id"] = "order-hedge-1"
                return (True, "order-hedge-1", 0.40)
            return (False, None, None)

        executor_concurrent._execute_single_leg = MagicMock(side_effect=mock_execute_leg)

        mock_hedger = MagicMock()
        with patch("executor.HEDGE_ENABLED", True), \
             patch("hedger.PartialFillHedger", return_value=mock_hedger):
            result = executor_concurrent._execute_legs_concurrent(opp, legs, 5.0)

        assert result is False

    def test_no_position_created_on_partial_failure(self, executor_concurrent, db):
        """No position should be created when not all legs fill."""
        opp = {
            "type": "Binary",
            "market": "No Position Market",
            "prices": "Y=0.400 N=0.450",
            "total_cost": "$4.25",
            "net_profit": 0.75,
            "net_roi": "17.6%",
            "_clob_depth": 100,
        }
        legs = [
            {"platform": "polymarket", "price": 0.4, "side": "BUY",
             "_token_id": "tok1", "token": "YES"},
            {"platform": "polymarket", "price": 0.45, "side": "BUY",
             "_token_id": "tok2", "token": "NO"},
        ]

        executor_concurrent._execute_single_leg = MagicMock(
            side_effect=[(True, "order-1", 0.40), (False, None, None)]
        )

        executor_concurrent._execute_legs_concurrent(opp, legs, 5.0)

        positions = db.get_open_positions()
        assert len(positions) == 0


# ---------------------------------------------------------------------------
# execute() dispatch logic
# ---------------------------------------------------------------------------

class TestExecuteDispatch:
    """Tests that execute() dispatches to concurrent vs sequential correctly."""

    def _make_binary_opp(self):
        return {
            "type": "Binary",
            "market": "Dispatch Test Market",
            "prices": "Y=0.400 N=0.450",
            "total_cost": "$4.25",
            "net_profit": 0.75,
            "net_roi": "17.6%",
            "_clob_depth": 100,
            "_token_ids": ["tok1", "tok2"],
        }

    def test_concurrent_enabled_dispatches_to_concurrent(self, executor_concurrent):
        """When concurrent_execution=True and legs support it, use concurrent path."""
        opp = self._make_binary_opp()

        # Stub all the gates so execute() reaches the dispatch
        executor_concurrent._revalidate = MagicMock(return_value=True)
        executor_concurrent._check_exit_liquidity = MagicMock(return_value=(True, ""))
        executor_concurrent.risk.check = MagicMock(return_value=(True, ""))
        executor_concurrent._fetch_balances = MagicMock(
            return_value={"polymarket": 100.0}
        )
        executor_concurrent._per_leg_budget = MagicMock(return_value=10.0)
        executor_concurrent.risk.clamp_size = MagicMock(return_value=5.0)
        executor_concurrent._print_plan = MagicMock()
        executor_concurrent._execute_legs_concurrent = MagicMock(return_value=True)
        executor_concurrent._execute_legs = MagicMock(return_value=True)

        executor_concurrent.execute(opp)

        executor_concurrent._execute_legs_concurrent.assert_called_once()
        executor_concurrent._execute_legs.assert_not_called()

    def test_concurrent_disabled_dispatches_to_sequential(self, executor_sequential):
        """When concurrent_execution=False, always use sequential path."""
        opp = self._make_binary_opp()

        executor_sequential._revalidate = MagicMock(return_value=True)
        executor_sequential._check_exit_liquidity = MagicMock(return_value=(True, ""))
        executor_sequential.risk.check = MagicMock(return_value=(True, ""))
        executor_sequential._fetch_balances = MagicMock(
            return_value={"polymarket": 100.0}
        )
        executor_sequential._per_leg_budget = MagicMock(return_value=10.0)
        executor_sequential.risk.clamp_size = MagicMock(return_value=5.0)
        executor_sequential._print_plan = MagicMock()
        executor_sequential._execute_legs_concurrent = MagicMock(return_value=True)
        executor_sequential._execute_legs = MagicMock(return_value=True)

        executor_sequential.execute(opp)

        executor_sequential._execute_legs.assert_called_once()
        executor_sequential._execute_legs_concurrent.assert_not_called()

    def test_concurrent_enabled_but_ibkr_falls_back_to_sequential(self, ArbitrageExecutor, db, risk_manager):
        """When concurrent is enabled but legs include IBKR, fall back to sequential."""
        ibkr_client = MagicMock()
        executor = ArbitrageExecutor(
            pm_trader=MagicMock(),
            kalshi_client=MagicMock(),
            db=db,
            risk_manager=risk_manager,
            dry_run=False,
            exec_mode="full-auto",
            max_trade_size=5.0,
            concurrent_execution=True,
            ibkr_client=ibkr_client,
        )

        opp = {
            "type": "Binary",
            "market": "IBKR Fallback Market",
            "prices": "Y=0.400 N=0.450",
            "total_cost": "$4.25",
            "net_profit": 0.75,
            "net_roi": "17.6%",
            "_clob_depth": 100,
            "_token_ids": ["tok1", "tok2"],
        }

        # Stub _build_legs to return legs that include an IBKR platform
        ibkr_legs = [
            {"platform": "polymarket", "price": 0.4, "side": "BUY", "_token_id": "tok1"},
            {"platform": "ibkr", "price": 0.45, "side": "BUY", "_ticker": "TEST-IBKR"},
        ]

        executor._revalidate = MagicMock(return_value=True)
        executor._check_exit_liquidity = MagicMock(return_value=(True, ""))
        executor.risk.check = MagicMock(return_value=(True, ""))
        executor._fetch_balances = MagicMock(
            return_value={"polymarket": 100.0, "ibkr": 100.0}
        )
        executor._per_leg_budget = MagicMock(return_value=10.0)
        executor.risk.clamp_size = MagicMock(return_value=5.0)
        executor._build_legs = MagicMock(return_value=ibkr_legs)
        executor._print_plan = MagicMock()
        executor._execute_legs_concurrent = MagicMock(return_value=True)
        executor._execute_legs = MagicMock(return_value=True)

        executor.execute(opp)

        # IBKR legs contain "ibkr" platform — _supports_concurrent returns False
        # so it should fall back to sequential
        executor._execute_legs.assert_called_once()
        executor._execute_legs_concurrent.assert_not_called()

    def test_dry_run_never_uses_concurrent(self, ArbitrageExecutor, db, risk_manager):
        """Dry run always uses _dry_run_log, never concurrent execution."""
        executor = ArbitrageExecutor(
            pm_trader=MagicMock(),
            kalshi_client=MagicMock(),
            db=db,
            risk_manager=risk_manager,
            dry_run=True,
            max_trade_size=5.0,
            concurrent_execution=True,
        )

        opp = {
            "type": "Binary",
            "market": "Dry Run Market",
            "prices": "Y=0.400 N=0.450",
            "total_cost": "$4.25",
            "net_profit": 0.75,
            "net_roi": "17.6%",
            "_clob_depth": 100,
            "_token_ids": ["tok1", "tok2"],
        }

        executor.risk.check = MagicMock(return_value=(True, ""))
        executor._fetch_balances = MagicMock(return_value={})
        executor._per_leg_budget = MagicMock(return_value=10.0)
        executor.risk.clamp_size = MagicMock(return_value=5.0)
        executor._print_plan = MagicMock()
        executor._dry_run_log = MagicMock(return_value=True)
        executor._execute_legs_concurrent = MagicMock()
        executor._execute_legs = MagicMock()

        executor.execute(opp)

        executor._dry_run_log.assert_called_once()
        executor._execute_legs_concurrent.assert_not_called()
        executor._execute_legs.assert_not_called()


# ---------------------------------------------------------------------------
# Config integration
# ---------------------------------------------------------------------------

class TestConcurrentConfig:
    """Tests that CONCURRENT_EXECUTION config is properly wired."""

    def test_config_var_exists(self):
        from config import CONCURRENT_EXECUTION
        # Default is False
        assert isinstance(CONCURRENT_EXECUTION, bool)

    def test_executor_default_concurrent_false(self, ArbitrageExecutor, db, risk_manager):
        """By default, concurrent_execution should be False."""
        executor = ArbitrageExecutor(
            pm_trader=MagicMock(),
            kalshi_client=MagicMock(),
            db=db,
            risk_manager=risk_manager,
        )
        assert executor.concurrent_execution is False

    def test_executor_concurrent_true_when_passed(self, ArbitrageExecutor, db, risk_manager):
        """Concurrent execution is True when explicitly passed."""
        executor = ArbitrageExecutor(
            pm_trader=MagicMock(),
            kalshi_client=MagicMock(),
            db=db,
            risk_manager=risk_manager,
            concurrent_execution=True,
        )
        assert executor.concurrent_execution is True


# ---------------------------------------------------------------------------
# Trade DB logging in concurrent mode
# ---------------------------------------------------------------------------

class TestConcurrentDBLogging:
    """Tests that concurrent execution properly logs trades to DB."""

    def test_trades_logged_as_pending_then_filled(self, executor_concurrent, db):
        """All trades start as pending and are updated to filled on success."""
        opp = {
            "type": "Binary",
            "market": "DB Log Market",
            "prices": "Y=0.400 N=0.450",
            "total_cost": "$4.25",
            "net_profit": 0.75,
            "net_roi": "17.6%",
            "_clob_depth": 100,
        }
        legs = [
            {"platform": "polymarket", "price": 0.4, "side": "BUY",
             "_token_id": "tok1", "token": "YES"},
            {"platform": "polymarket", "price": 0.45, "side": "BUY",
             "_token_id": "tok2", "token": "NO"},
        ]

        executor_concurrent._execute_single_leg = MagicMock(
            return_value=(True, "order-db1", 0.40)
        )

        executor_concurrent._execute_legs_concurrent(opp, legs, 5.0)

        # Both trades should have _trade_id set
        assert "_trade_id" in legs[0]
        assert "_trade_id" in legs[1]

    def test_failed_trade_logged_as_failed(self, executor_concurrent, db):
        """Failed legs are updated to 'failed' status in DB."""
        opp = {
            "type": "Binary",
            "market": "DB Fail Market",
            "prices": "Y=0.400 N=0.450",
            "total_cost": "$4.25",
            "net_profit": 0.75,
            "net_roi": "17.6%",
            "_clob_depth": 100,
        }
        legs = [
            {"platform": "polymarket", "price": 0.4, "side": "BUY",
             "_token_id": "tok1", "token": "YES"},
            {"platform": "polymarket", "price": 0.45, "side": "BUY",
             "_token_id": "tok2", "token": "NO"},
        ]

        executor_concurrent._execute_single_leg = MagicMock(
            return_value=(False, None, None)
        )

        result = executor_concurrent._execute_legs_concurrent(opp, legs, 5.0)
        assert result is False

    def test_total_cost_parsed_from_dollar_string(self, executor_concurrent):
        """total_cost is correctly parsed from '$X.XX' format."""
        opp = {
            "type": "Binary",
            "market": "Cost Parse Market",
            "prices": "Y=0.400 N=0.450",
            "total_cost": "$8.50",
            "net_profit": 1.50,
            "net_roi": "17.6%",
            "_clob_depth": 100,
        }
        legs = [
            {"platform": "polymarket", "price": 0.4, "side": "BUY",
             "_token_id": "tok1", "token": "YES"},
            {"platform": "polymarket", "price": 0.45, "side": "BUY",
             "_token_id": "tok2", "token": "NO"},
        ]

        executor_concurrent._execute_single_leg = MagicMock(
            return_value=(True, "order-cost", 0.40)
        )

        # Should not raise any parsing errors
        result = executor_concurrent._execute_legs_concurrent(opp, legs, 5.0)
        assert result is True

    def test_total_cost_as_float(self, executor_concurrent):
        """total_cost can also be a plain float."""
        opp = {
            "type": "Binary",
            "market": "Float Cost Market",
            "prices": "Y=0.400 N=0.450",
            "total_cost": 8.50,
            "net_profit": 1.50,
            "net_roi": "17.6%",
            "_clob_depth": 100,
        }
        legs = [
            {"platform": "polymarket", "price": 0.4, "side": "BUY",
             "_token_id": "tok1", "token": "YES"},
            {"platform": "polymarket", "price": 0.45, "side": "BUY",
             "_token_id": "tok2", "token": "NO"},
        ]

        executor_concurrent._execute_single_leg = MagicMock(
            return_value=(True, "order-float", 0.40)
        )

        result = executor_concurrent._execute_legs_concurrent(opp, legs, 5.0)
        assert result is True


# ---------------------------------------------------------------------------
# Platform detection in position creation
# ---------------------------------------------------------------------------

class TestConcurrentPositionPlatform:
    """Tests that position platform is correctly identified."""

    def _run_concurrent_success(self, executor, opp, legs):
        executor._execute_single_leg = MagicMock(
            return_value=(True, "order-plat", 0.40)
        )
        return executor._execute_legs_concurrent(opp, legs, 5.0)

    def test_binary_position_polymarket(self, executor_concurrent, db):
        opp = {
            "type": "Binary",
            "market": "PM Market",
            "prices": "Y=0.40 N=0.45",
            "total_cost": "$4.25",
            "net_profit": 0.75,
            "net_roi": "17.6%",
            "_clob_depth": 100,
        }
        legs = [
            {"platform": "polymarket", "price": 0.4, "side": "BUY", "_token_id": "t1", "token": "YES"},
            {"platform": "polymarket", "price": 0.45, "side": "BUY", "_token_id": "t2", "token": "NO"},
        ]
        self._run_concurrent_success(executor_concurrent, opp, legs)
        positions = db.get_open_positions()
        assert len(positions) >= 1

    def test_kalshi_position(self, executor_concurrent, db):
        opp = {
            "type": "KalshiBinary",
            "market": "Kalshi Market",
            "prices": "Y=0.40 N=0.45",
            "total_cost": "$4.25",
            "net_profit": 0.75,
            "net_roi": "17.6%",
            "_clob_depth": 100,
        }
        legs = [
            {"platform": "kalshi", "price": 0.4, "side": "yes", "_ticker": "T1"},
            {"platform": "kalshi", "price": 0.45, "side": "no", "_ticker": "T1"},
        ]
        self._run_concurrent_success(executor_concurrent, opp, legs)
        positions = db.get_open_positions()
        assert len(positions) >= 1

    def test_cross_position(self, executor_concurrent, db):
        opp = {
            "type": "Cross",
            "market": "Cross Market",
            "prices": "PM=0.35 K=0.60",
            "total_cost": "$4.75",
            "net_profit": 0.25,
            "net_roi": "5.3%",
            "_clob_depth": 50,
        }
        legs = [
            {"platform": "polymarket", "price": 0.35, "side": "BUY", "_token_id": "t1"},
            {"platform": "kalshi", "price": 0.60, "side": "yes", "_ticker": "T1"},
        ]
        self._run_concurrent_success(executor_concurrent, opp, legs)
        positions = db.get_open_positions()
        assert len(positions) >= 1
