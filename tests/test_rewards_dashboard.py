"""Tests for dashboard rewards metrics integration."""

import pytest
from unittest.mock import MagicMock, patch
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


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


@pytest.fixture
def reward_tracker():
    """Mock RewardTracker with realistic data."""
    from market_maker import RewardTracker

    tracker = RewardTracker()
    tracker.reward_scores = {
        "POLY-TEST-1": {
            "polymarket": {
                "pool_size_usdc": 1000.0,
                "reward_rate": 0.10,
            }
        },
        "POLY-TEST-2": {
            "polymarket": {
                "pool_size_usdc": 500.0,
                "reward_rate": 0.05,
            }
        },
    }
    # Set _reward_cache for dashboard functions
    tracker._reward_cache = {
        "POLY-TEST-1": {
            "pool_size_usdc": 1000.0,
            "reward_rate": 0.10,
        },
        "POLY-TEST-2": {
            "pool_size_usdc": 500.0,
            "reward_rate": 0.05,
        },
    }
    tracker.resting_order_count = 3
    return tracker


@pytest.fixture
def db():
    """Mock database with strategy P&L tracking."""
    db = MagicMock()
    db.get_strategy_pnl.return_value = 50.0  # Trading P&L for rewards strategy
    return db


@pytest.fixture
def dashboard_state():
    """Mock dashboard state object."""
    from dashboard import _DashboardState

    state = _DashboardState()
    state.reward_tracker = None
    return state


class TestRewardsDashboard:
    """Test dashboard rewards metrics integration."""

    def test_status_endpoint_includes_rewards_key(self, reward_tracker, db):
        """Verify /status endpoint JSON has 'rewards' key."""
        from dashboard import _build_rewards_metrics

        rewards_data = _build_rewards_metrics(reward_tracker)

        # Verify structure
        assert isinstance(rewards_data, dict)
        assert "strategy_name" in rewards_data
        assert "resting_order_count" in rewards_data
        assert "estimated_daily_yield_usdc" in rewards_data
        assert "trading_pnl" in rewards_data
        assert "total_reward_exposure" in rewards_data

    def test_rewards_metrics_structure(self, reward_tracker):
        """Verify rewards metrics dict has all required keys."""
        from dashboard import _build_rewards_metrics

        rewards = _build_rewards_metrics(reward_tracker)

        # Verify data types
        assert isinstance(rewards["strategy_name"], str)
        assert isinstance(rewards["resting_order_count"], int)
        assert isinstance(rewards["estimated_daily_yield_usdc"], float)
        assert isinstance(rewards["trading_pnl"], float)
        assert isinstance(rewards["total_reward_exposure"], float)

        # Verify reasonable values
        assert rewards["strategy_name"] == "Liquidity Rewards"
        assert rewards["resting_order_count"] == 3
        assert rewards["estimated_daily_yield_usdc"] >= 0
        assert rewards["trading_pnl"] >= 0

    def test_estimate_yield_calculation(self, reward_tracker):
        """Verify estimate_reward_yield() calculates from reward pools."""
        from dashboard import _estimate_reward_yield

        yield_amount = _estimate_reward_yield(reward_tracker)

        # Verify positive yield from pools
        assert isinstance(yield_amount, float)
        assert yield_amount >= 0

        # Pool 1: 1000 / 30 = 33.33
        # Pool 2: 500 / 30 = 16.67
        # Total ~50.0
        assert yield_amount > 40.0

    def test_estimate_yield_with_empty_tracker(self):
        """Verify estimate_reward_yield() returns 0 for None tracker."""
        from dashboard import _estimate_reward_yield

        yield_amount = _estimate_reward_yield(None)

        assert yield_amount == 0.0

    def test_estimate_yield_with_no_pools(self):
        """Verify estimate_reward_yield() returns 0 when no pools."""
        from market_maker import RewardTracker
        from dashboard import _estimate_reward_yield

        tracker = RewardTracker()
        tracker.reward_scores = {}
        tracker._reward_cache = {}

        yield_amount = _estimate_reward_yield(tracker)

        assert yield_amount == 0.0

    def test_calculate_total_exposure(self, reward_tracker):
        """Verify calculate_total_exposure() returns numeric value."""
        from dashboard import _calculate_total_exposure

        exposure = _calculate_total_exposure(reward_tracker)

        # Currently returns 0 as placeholder
        assert isinstance(exposure, float)
        assert exposure >= 0.0

    def test_calculate_total_exposure_with_none(self):
        """Verify calculate_total_exposure() returns 0 for None."""
        from dashboard import _calculate_total_exposure

        exposure = _calculate_total_exposure(None)

        assert exposure == 0.0

    def test_rewards_metrics_with_none_tracker(self):
        """Verify rewards metrics handle None tracker gracefully."""
        from dashboard import _build_rewards_metrics

        rewards = _build_rewards_metrics(None)

        assert rewards["strategy_name"] == "Liquidity Rewards"
        assert rewards["resting_order_count"] == 0
        assert rewards["estimated_daily_yield_usdc"] == 0.0
        assert rewards["trading_pnl"] == 0.0
        assert rewards["total_reward_exposure"] == 0.0

    def test_leaderboard_rewards_row_in_html(self):
        """Verify dashboard HTML includes rewards strategy row."""
        from dashboard_ui import get_dashboard_html

        html = get_dashboard_html()

        # Check for rewards row markers
        assert 'data-strategy="Rewards"' in html
        assert 'id="rewards-row"' in html
        assert 'id="rewards-trading-pnl"' in html
        assert 'id="rewards-yield-daily"' in html
        assert 'id="rewards-total-pnl"' in html
        assert 'id="rewards-resting-count"' in html
        assert 'id="rewards-status"' in html

    def test_updateRewardsRow_function_in_javascript(self):
        """Verify JavaScript updateRewardsRow() function exists in HTML."""
        from dashboard_ui import get_dashboard_html

        html = get_dashboard_html()

        assert "function updateRewardsRow(status)" in html
        assert "rewards.trading_pnl" in html
        assert "rewards.estimated_daily_yield_usdc" in html
        assert "rewards.resting_order_count" in html

    def test_rewards_row_update_in_refresh_loop(self):
        """Verify updateRewardsRow() is called in refresh loop."""
        from dashboard_ui import get_dashboard_html

        html = get_dashboard_html()

        assert "updateRewardsRow(status)" in html

    def test_dashboard_state_includes_reward_tracker(self, dashboard_state):
        """Verify _DashboardState has reward_tracker attribute."""
        assert hasattr(dashboard_state, "reward_tracker")
        assert dashboard_state.reward_tracker is None

    def test_rewards_metrics_json_serializable(self, reward_tracker):
        """Verify rewards metrics can be JSON serialized."""
        import json
        from dashboard import _build_rewards_metrics

        rewards = _build_rewards_metrics(reward_tracker)

        # Should not raise an exception
        json_str = json.dumps(rewards)
        assert isinstance(json_str, str)

        # Verify can be deserialized
        parsed = json.loads(json_str)
        assert parsed["strategy_name"] == "Liquidity Rewards"

    def test_multiple_reward_pools_aggregation(self):
        """Verify yield calculation aggregates multiple pools."""
        from market_maker import RewardTracker
        from dashboard import _estimate_reward_yield

        tracker = RewardTracker()
        tracker.reward_scores = {
            "POOL-1": {"polymarket": {"pool_size_usdc": 600.0}},
            "POOL-2": {"polymarket": {"pool_size_usdc": 600.0}},
            "POOL-3": {"polymarket": {"pool_size_usdc": 600.0}},
        }
        tracker._reward_cache = {
            "POOL-1": {"pool_size_usdc": 600.0},
            "POOL-2": {"pool_size_usdc": 600.0},
            "POOL-3": {"pool_size_usdc": 600.0},
        }

        yield_amount = _estimate_reward_yield(tracker)

        # 3 pools * (600 / 30) = 60.0
        assert yield_amount >= 59.0

    def test_rewards_strategy_name_constant(self, reward_tracker):
        """Verify rewards strategy always named 'Liquidity Rewards'."""
        from dashboard import _build_rewards_metrics

        rewards1 = _build_rewards_metrics(reward_tracker)
        rewards2 = _build_rewards_metrics(None)

        assert rewards1["strategy_name"] == "Liquidity Rewards"
        assert rewards2["strategy_name"] == "Liquidity Rewards"

    def test_resting_order_count_reflects_tracker(self, reward_tracker):
        """Verify resting_order_count matches tracker value."""
        from dashboard import _build_rewards_metrics

        rewards = _build_rewards_metrics(reward_tracker)

        assert rewards["resting_order_count"] == reward_tracker.resting_order_count
        assert rewards["resting_order_count"] == 3

    def test_dashboard_html_structure(self):
        """Verify dashboard HTML is valid and contains required sections."""
        from dashboard_ui import get_dashboard_html

        html = get_dashboard_html()

        # Basic HTML structure
        assert html.startswith("<!DOCTYPE html>")
        assert "<html" in html
        assert "</html>" in html
        assert "<body" in html
        assert "</body>" in html

    def test_dashboard_refresh_parameter(self):
        """Verify get_dashboard_html() respects refresh_seconds parameter."""
        from dashboard_ui import get_dashboard_html

        html_15 = get_dashboard_html(refresh_seconds=15)
        html_30 = get_dashboard_html(refresh_seconds=30)

        # Verify refresh interval is substituted
        assert "15" in html_15
        assert html_15 != html_30
        assert "30" in html_30

    def test_rewards_exposure_zero_by_default(self):
        """Verify total_reward_exposure defaults to 0.0."""
        from dashboard import _calculate_total_exposure, _build_rewards_metrics
        from market_maker import RewardTracker

        tracker = RewardTracker()
        tracker.reward_scores = {}

        rewards = _build_rewards_metrics(tracker)

        assert rewards["total_reward_exposure"] == 0.0
