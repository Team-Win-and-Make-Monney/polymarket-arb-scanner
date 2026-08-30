"""Tests for risk_manager.py — risk gates and position sizing."""

import pytest
from unittest.mock import MagicMock

import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from risk_manager import RiskManager


@pytest.fixture
def default_config():
    return {
        "max_trade_size": 5.0,
        "daily_loss_limit": 25.0,
        "max_open_positions": 25,
        "min_liquidity": 25.0,
        "min_liquidity_high_roi": 10.0,
        "min_net_roi": 0,
        "allow_better_reentry": True,
        "reentry_improvement_threshold": 0.20,
    }


@pytest.fixture
def rm(default_config):
    return RiskManager(default_config)


@pytest.fixture
def mock_db():
    db = MagicMock()
    db.get_daily_pnl.return_value = 0.0
    db.get_open_positions_count.return_value = 0
    db.is_market_active.return_value = False
    db.get_active_market_expected_pnl.return_value = None
    return db


@pytest.fixture
def valid_opportunity():
    return {
        "type": "Binary",
        "market": "Will X happen?",
        "total_cost": "$0.9500",
        "net_profit": 0.05,
        "net_roi": "5.26%",
        "_clob_depth": 100.0,
    }


# ---------------------------------------------------------------------------
# Daily P&L limit enforcement
# ---------------------------------------------------------------------------

class TestDailyPnlLimit:
    def test_allows_when_within_limit(self, rm, mock_db, valid_opportunity):
        mock_db.get_daily_pnl.return_value = -10.0
        allowed, reason = rm.check(valid_opportunity, mock_db)
        assert allowed is True

    def test_blocks_when_limit_hit(self, rm, mock_db, valid_opportunity):
        mock_db.get_daily_pnl.return_value = -26.0
        allowed, reason = rm.check(valid_opportunity, mock_db)
        assert allowed is False
        assert "Daily loss limit" in reason

    def test_blocks_at_exact_limit(self, rm, mock_db, valid_opportunity):
        mock_db.get_daily_pnl.return_value = -25.01
        allowed, reason = rm.check(valid_opportunity, mock_db)
        assert allowed is False

    def test_allows_at_boundary(self, rm, mock_db, valid_opportunity):
        mock_db.get_daily_pnl.return_value = -25.0
        allowed, reason = rm.check(valid_opportunity, mock_db)
        # -25.0 is NOT < -25.0, so should be allowed
        assert allowed is True


# ---------------------------------------------------------------------------
# Open position limit
# ---------------------------------------------------------------------------

class TestOpenPositionLimit:
    def test_allows_under_limit(self, rm, mock_db, valid_opportunity):
        mock_db.get_open_positions_count.return_value = 5
        allowed, _ = rm.check(valid_opportunity, mock_db)
        assert allowed is True

    def test_blocks_at_max(self, rm, mock_db, valid_opportunity):
        mock_db.get_open_positions_count.return_value = 25
        allowed, reason = rm.check(valid_opportunity, mock_db)
        assert allowed is False
        assert "Max open positions" in reason

    def test_blocks_over_max(self, rm, mock_db, valid_opportunity):
        mock_db.get_open_positions_count.return_value = 30
        allowed, reason = rm.check(valid_opportunity, mock_db)
        assert allowed is False


# ---------------------------------------------------------------------------
# Balance checks
# ---------------------------------------------------------------------------

class TestBalanceChecks:
    def test_pm_only_sufficient_balance(self, rm, mock_db, valid_opportunity):
        balances = {"polymarket": 10.0}
        allowed, _ = rm.check(valid_opportunity, mock_db, balances)
        assert allowed is True

    def test_pm_only_insufficient_balance(self, rm, mock_db, valid_opportunity):
        # total_cost=$0.95, trade_cost=min(5.0, 0.95)=0.95; balance 0.50 < 0.95
        balances = {"polymarket": 0.50}
        allowed, reason = rm.check(valid_opportunity, mock_db, balances)
        assert allowed is False
        assert "Insufficient Polymarket balance" in reason

    def test_kalshi_only_sufficient_balance(self, rm, mock_db, valid_opportunity):
        valid_opportunity["type"] = "KalshiBinary"
        balances = {"kalshi": 10.0}
        allowed, _ = rm.check(valid_opportunity, mock_db, balances)
        assert allowed is True

    def test_kalshi_only_insufficient_balance(self, rm, mock_db, valid_opportunity):
        valid_opportunity["type"] = "KalshiBinary"
        # trade_cost=min(5.0, 0.95)=0.95; balance 0.50 < 0.95
        balances = {"kalshi": 0.50}
        allowed, reason = rm.check(valid_opportunity, mock_db, balances)
        assert allowed is False
        assert "Insufficient Kalshi balance" in reason

    def test_cross_platform_sufficient(self, rm, mock_db, valid_opportunity):
        valid_opportunity["type"] = "Cross"
        balances = {"polymarket": 5.0, "kalshi": 5.0}
        allowed, _ = rm.check(valid_opportunity, mock_db, balances)
        assert allowed is True

    def test_cross_platform_pm_insufficient(self, rm, mock_db, valid_opportunity):
        valid_opportunity["type"] = "Cross"
        # trade_cost=min(5.0, 0.95)=0.95; need trade_cost/2=0.475 on each side
        balances = {"polymarket": 0.20, "kalshi": 5.0}
        allowed, reason = rm.check(valid_opportunity, mock_db, balances)
        assert allowed is False
        assert "Insufficient Polymarket balance" in reason

    def test_cross_platform_kalshi_insufficient(self, rm, mock_db, valid_opportunity):
        valid_opportunity["type"] = "Cross"
        # trade_cost=0.95; need 0.475 on each side; kalshi=0.20 < 0.475
        balances = {"polymarket": 5.0, "kalshi": 0.20}
        allowed, reason = rm.check(valid_opportunity, mock_db, balances)
        assert allowed is False
        assert "Insufficient Kalshi balance" in reason

    def test_no_balances_provided_skips_check(self, rm, mock_db, valid_opportunity):
        allowed, _ = rm.check(valid_opportunity, mock_db, balances=None)
        assert allowed is True

    def test_total_cost_as_numeric(self, rm, mock_db, valid_opportunity):
        valid_opportunity["total_cost"] = 0.95
        balances = {"polymarket": 10.0}
        allowed, _ = rm.check(valid_opportunity, mock_db, balances)
        assert allowed is True


# ---------------------------------------------------------------------------
# Order book depth minimum — tiered by ROI
# ---------------------------------------------------------------------------

class TestOrderBookDepth:
    def test_sufficient_depth(self, rm, mock_db, valid_opportunity):
        valid_opportunity["_clob_depth"] = 100.0
        allowed, _ = rm.check(valid_opportunity, mock_db)
        assert allowed is True

    def test_insufficient_depth_standard(self, rm, mock_db, valid_opportunity):
        # ROI ~5.26% (>5%), so uses min_liquidity_high_roi=10
        # With low ROI, should use standard threshold
        valid_opportunity["net_profit"] = 0.005  # ROI ~0.53% < 5%
        valid_opportunity["_clob_depth"] = 20.0
        allowed, reason = rm.check(valid_opportunity, mock_db)
        assert allowed is False
        assert "Insufficient depth" in reason

    def test_zero_depth(self, rm, mock_db, valid_opportunity):
        valid_opportunity["_clob_depth"] = 0
        allowed, reason = rm.check(valid_opportunity, mock_db)
        assert allowed is False

    def test_high_roi_uses_lower_depth_threshold(self, rm, mock_db, valid_opportunity):
        """High ROI (>5%) opportunities use min_liquidity_high_roi (10) instead of min_liquidity (25)."""
        valid_opportunity["net_profit"] = 0.06  # ROI = 0.06/0.95 = 6.3% > 5%
        valid_opportunity["_clob_depth"] = 15.0  # Between 10 and 25
        allowed, _ = rm.check(valid_opportunity, mock_db)
        assert allowed is True

    def test_low_roi_uses_standard_depth_threshold(self, rm, mock_db, valid_opportunity):
        """Low ROI (<=5%) uses standard min_liquidity (25) threshold."""
        valid_opportunity["net_profit"] = 0.01  # ROI = 0.01/0.95 = 1.05% <= 5%
        valid_opportunity["_clob_depth"] = 15.0  # Between 10 and 25
        allowed, reason = rm.check(valid_opportunity, mock_db)
        assert allowed is False
        assert "Insufficient depth" in reason

    def test_high_roi_below_high_roi_threshold(self, rm, mock_db, valid_opportunity):
        """Even high ROI is blocked if depth is below the lower threshold."""
        valid_opportunity["net_profit"] = 0.10  # ROI = 10.5% >> 5%
        valid_opportunity["_clob_depth"] = 5.0  # Below min_liquidity_high_roi (10)
        allowed, reason = rm.check(valid_opportunity, mock_db)
        assert allowed is False
        assert "Insufficient depth" in reason


# ---------------------------------------------------------------------------
# Net ROI minimum (disabled by default when min_net_roi=0)
# ---------------------------------------------------------------------------

class TestNetRoiMinimum:
    def test_roi_check_disabled_when_zero(self, rm, mock_db, valid_opportunity):
        """Default min_net_roi=0 disables the ROI check entirely."""
        valid_opportunity["net_profit"] = 0.001
        valid_opportunity["total_cost"] = "$0.9500"
        allowed, _ = rm.check(valid_opportunity, mock_db)
        # ROI = 0.001/0.95 = 0.1% would normally fail, but min_net_roi=0 disables check
        assert allowed is True

    def test_roi_check_enabled_when_set(self, default_config, mock_db, valid_opportunity):
        """When min_net_roi > 0, the ROI check is active."""
        default_config["min_net_roi"] = 0.01
        rm = RiskManager(default_config)
        valid_opportunity["net_profit"] = 0.001
        valid_opportunity["total_cost"] = "$0.9500"
        allowed, reason = rm.check(valid_opportunity, mock_db)
        assert allowed is False
        assert "ROI too low" in reason

    def test_sufficient_roi_passes_when_enabled(self, default_config, mock_db, valid_opportunity):
        default_config["min_net_roi"] = 0.01
        rm = RiskManager(default_config)
        # net_profit=0.05, total_cost=0.95 -> roi=5.26% > 1%
        allowed, _ = rm.check(valid_opportunity, mock_db)
        assert allowed is True

    def test_zero_cost_skips_roi_check(self, rm, mock_db, valid_opportunity):
        valid_opportunity["total_cost"] = "$0"
        valid_opportunity["net_profit"] = 0.0
        valid_opportunity["_clob_depth"] = 100.0
        allowed, _ = rm.check(valid_opportunity, mock_db)
        assert allowed is True


# ---------------------------------------------------------------------------
# Market dedup check with smart re-entry
# ---------------------------------------------------------------------------

class TestMarketDedup:
    def test_new_market_allowed(self, rm, mock_db, valid_opportunity):
        mock_db.is_market_active.return_value = False
        allowed, _ = rm.check(valid_opportunity, mock_db)
        assert allowed is True

    def test_active_market_blocked(self, rm, mock_db, valid_opportunity):
        mock_db.is_market_active.return_value = True
        mock_db.get_active_market_expected_pnl.return_value = 0.05
        # New opportunity has same profit (0.05), not 20% better
        allowed, reason = rm.check(valid_opportunity, mock_db)
        assert allowed is False
        assert "Already trading" in reason

    def test_better_reentry_allowed(self, rm, mock_db, valid_opportunity):
        """When new opportunity is 20%+ better, allow re-entry."""
        mock_db.is_market_active.return_value = True
        mock_db.get_active_market_expected_pnl.return_value = 0.03
        valid_opportunity["net_profit"] = 0.05  # 0.05 > 0.03 * 1.20 = 0.036 -> allowed
        allowed, _ = rm.check(valid_opportunity, mock_db)
        assert allowed is True

    def test_marginal_improvement_blocked(self, rm, mock_db, valid_opportunity):
        """When improvement is less than 20%, block re-entry."""
        mock_db.is_market_active.return_value = True
        mock_db.get_active_market_expected_pnl.return_value = 0.045
        valid_opportunity["net_profit"] = 0.05  # 0.05 > 0.045 * 1.20 = 0.054 -> False
        allowed, reason = rm.check(valid_opportunity, mock_db)
        assert allowed is False
        assert "Already trading" in reason

    def test_reentry_disabled(self, default_config, mock_db, valid_opportunity):
        """When allow_better_reentry=False, always block duplicates."""
        default_config["allow_better_reentry"] = False
        rm = RiskManager(default_config)
        mock_db.is_market_active.return_value = True
        valid_opportunity["net_profit"] = 1.0  # Way better but reentry disabled
        allowed, reason = rm.check(valid_opportunity, mock_db)
        assert allowed is False
        assert "Already trading" in reason

    def test_reentry_with_no_existing_pnl(self, rm, mock_db, valid_opportunity):
        """When existing position has None expected_pnl, block re-entry."""
        mock_db.is_market_active.return_value = True
        mock_db.get_active_market_expected_pnl.return_value = None
        allowed, reason = rm.check(valid_opportunity, mock_db)
        assert allowed is False
        assert "Already trading" in reason


# ---------------------------------------------------------------------------
# clamp_size
# ---------------------------------------------------------------------------

class TestClampSize:
    def test_clamped_to_max_trade_size(self, rm):
        result = rm.clamp_size(desired_size=10.0, depth=100.0, balance=100.0)
        assert result == 5.0  # max_trade_size is 5.0

    def test_clamped_to_depth(self, rm):
        result = rm.clamp_size(desired_size=5.0, depth=3.0, balance=100.0)
        assert result == 3.0

    def test_clamped_to_balance(self, rm):
        result = rm.clamp_size(desired_size=5.0, depth=100.0, balance=2.0)
        assert result == 2.0

    def test_zero_depth_not_used(self, rm):
        # depth=0 means the depth constraint doesn't apply (if depth > 0)
        result = rm.clamp_size(desired_size=5.0, depth=0, balance=100.0)
        assert result == 5.0

    def test_none_balance(self, rm):
        result = rm.clamp_size(desired_size=5.0, depth=100.0, balance=None)
        assert result == 5.0

    def test_all_constraints_apply(self, rm):
        # Should use the smallest of desired, max_trade, depth, balance
        result = rm.clamp_size(desired_size=10.0, depth=2.0, balance=1.5)
        assert result == 1.5

    def test_never_negative(self, rm):
        result = rm.clamp_size(desired_size=-1.0, depth=100.0, balance=100.0)
        assert result == 0.0

    def test_zero_balance_skips_balance_constraint(self, rm):
        # balance=0 means balance > 0 is False, so balance constraint is skipped
        result = rm.clamp_size(desired_size=5.0, depth=100.0, balance=0)
        assert result == 5.0


# ---------------------------------------------------------------------------
# Dynamic trade sizing
# ---------------------------------------------------------------------------

class TestDynamicSizing:
    def test_high_roi_deep_book_scales_up(self, rm):
        """High ROI + deep book should produce larger size (up to max_trade_size)."""
        opp = {"net_profit": 0.10, "total_cost": "$0.9000", "_clob_depth": 200.0}
        # ROI = 0.10/0.90 = 11.1%; size = 5 * (1 + 0.111 * 0.5 * 20) = 5 * 2.111 = 10.56
        # Capped at 50% of depth=100, then capped at max_trade_size=5
        size = rm.calculate_dynamic_size(opp, aggressiveness=0.5)
        assert size == pytest.approx(5.0)  # Capped by max_trade_size

    def test_low_roi_near_base_size(self, rm):
        """Low ROI should produce near-base size."""
        opp = {"net_profit": 0.005, "total_cost": "$0.9950", "_clob_depth": 200.0}
        # ROI = 0.005/0.995 = 0.50%; size = 5 * (1 + 0.005 * 0.5 * 20) = 5 * 1.05 = 5.25
        # Capped by max_trade_size=5
        size = rm.calculate_dynamic_size(opp, aggressiveness=0.5)
        assert size == pytest.approx(5.0)  # Capped

    def test_shallow_book_caps_at_half_depth(self, rm):
        """Size is capped at 50% of available depth."""
        rm.max_trade_size = 50.0  # Raise max to test depth cap
        opp = {"net_profit": 0.10, "total_cost": "$0.9000", "_clob_depth": 20.0}
        # ROI = 11.1%; size = 50 * (1 + 0.111 * 0.5 * 20) = 50 * 2.111 = 105.56
        # Capped at 50% of depth=10, then capped at max_trade_size=50
        size = rm.calculate_dynamic_size(opp, aggressiveness=0.5)
        assert size == pytest.approx(10.0)  # 50% of depth

    def test_zero_profit_returns_base_size(self, rm):
        opp = {"net_profit": 0, "total_cost": "$0.9000", "_clob_depth": 100.0}
        size = rm.calculate_dynamic_size(opp, aggressiveness=0.5)
        assert size == rm.max_trade_size

    def test_zero_cost_returns_base_size(self, rm):
        opp = {"net_profit": 0.10, "total_cost": "$0", "_clob_depth": 100.0}
        size = rm.calculate_dynamic_size(opp, aggressiveness=0.5)
        assert size == rm.max_trade_size

    def test_aggressiveness_zero_returns_base(self, rm):
        """With aggressiveness=0, dynamic sizing doesn't scale up."""
        opp = {"net_profit": 0.10, "total_cost": "$0.9000", "_clob_depth": 100.0}
        size = rm.calculate_dynamic_size(opp, aggressiveness=0)
        assert size == rm.max_trade_size
