"""Tests for reward tracking infrastructure (RewardTracker, KalshiRewardTracker, config)."""

import pytest
from unittest.mock import MagicMock, patch
import time
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from db import TradeDB
from market_maker import RewardTracker, KalshiRewardTracker


# ---------------------------------------------------------------------------
# TestRewardTracker (Polymarket reward tracking)
# ---------------------------------------------------------------------------

class TestRewardTracker:
    """Test RewardTracker for Polymarket reward metadata caching and spread optimization."""

    def test_update_and_get_polymarket_reward(self):
        """Update reward metadata, verify retrieval."""
        rt = RewardTracker()
        market_key = "0x123abc"
        reward_data = {
            "min_incentive_size": 5.0,
            "max_incentive_spread": 0.05,
            "pool_size_usdc": 500.0,
            "status": "active"
        }

        rt.update_polymarket_reward(market_key, reward_data)
        retrieved = rt.get_polymarket_reward(market_key)

        assert retrieved is not None
        assert retrieved["max_incentive_spread"] == 0.05
        assert retrieved["pool_size_usdc"] == 500.0

    def test_reward_cache_ttl(self):
        """Verify cache expires after TTL."""
        rt = RewardTracker()
        market_key = "0x456def"
        reward_data = {"max_incentive_spread": 0.08, "pool_size_usdc": 1000.0}

        # Update with 1-second TTL
        rt.update_polymarket_reward(market_key, reward_data, ttl_seconds=1.0)

        # Immediately available
        assert rt.get_polymarket_reward(market_key) is not None

        # Wait for expiry
        time.sleep(1.1)

        # Now expired
        assert rt.get_polymarket_reward(market_key) is None

    def test_calculate_optimal_reward_spread(self):
        """Calculate spreads within max_incentive_spread."""
        rt = RewardTracker()
        market_key = "0x789ghi"
        reward_data = {
            "min_incentive_size": 10.0,
            "max_incentive_spread": 0.05,
            "pool_size_usdc": 500.0,
            "status": "active"
        }

        rt.update_polymarket_reward(market_key, reward_data)
        spread_info = rt.calculate_optimal_reward_spread(market_key, 0.50, inventory=0.0)

        assert spread_info is not None
        assert "bid" in spread_info
        assert "ask" in spread_info
        assert "spread" in spread_info
        assert spread_info["reward_optimized"] is True

        # Spread should be tighter than max
        assert spread_info["spread"] < 0.05

        # Bid/ask should be around mid-price
        assert 0.40 < spread_info["bid"] < 0.60
        assert 0.40 < spread_info["ask"] < 0.60
        assert spread_info["ask"] > spread_info["bid"]

    def test_inventory_skew(self):
        """Bid/ask adjust when inventory > 0."""
        rt = RewardTracker()
        market_key = "0xtest"
        reward_data = {"max_incentive_spread": 0.05, "pool_size_usdc": 500.0}

        rt.update_polymarket_reward(market_key, reward_data)

        # No inventory
        flat = rt.calculate_optimal_reward_spread(market_key, 0.50, inventory=0.0)

        # Long inventory (positive)
        long = rt.calculate_optimal_reward_spread(market_key, 0.50, inventory=100.0)

        # When long, ask should be lower (skew down to sell faster)
        assert long["ask"] <= flat["ask"]

    def test_no_reward_data(self):
        """Returns None when no reward metadata."""
        rt = RewardTracker()
        assert rt.get_polymarket_reward("0xnodata") is None
        assert rt.calculate_optimal_reward_spread("0xnodata", 0.50) is None


# ---------------------------------------------------------------------------
# TestKalshiRewardTracker (Kalshi local reward tracking)
# ---------------------------------------------------------------------------

class TestKalshiRewardTracker:
    """Test KalshiRewardTracker for local order logging and reward estimation."""

    def test_log_order_placed(self):
        """Order placed, stored in _active_orders and DB."""
        db = TradeDB(':memory:')
        krt = KalshiRewardTracker(db)

        krt.log_order_placed(
            order_id="order_1",
            market_key="NVDA_250101",
            size=10.0,
            price=0.50,
            mid_price=0.50,
            side="buy"
        )

        active = krt.get_active_orders()
        assert len(active) == 1
        assert active[0]["order_id"] == "order_1"
        assert active[0]["market_key"] == "NVDA_250101"
        assert active[0]["size"] == 10.0

        # Verify DB persistence
        cursor = db.conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM reward_metrics WHERE event='placed'")
        assert cursor.fetchone()[0] == 1

        db.close()

    def test_log_order_cancelled(self):
        """Order cancelled, resting_seconds calculated."""
        db = TradeDB(':memory:')
        krt = KalshiRewardTracker(db)

        krt.log_order_placed(
            order_id="order_2",
            market_key="TSLA_250101",
            size=5.0,
            price=0.45,
            mid_price=0.50,
            side="sell"
        )

        # Simulate some resting time
        time.sleep(0.1)

        krt.log_order_cancelled("order_2")

        # Order should be removed from active
        active = krt.get_active_orders()
        assert len(active) == 0

        # Verify DB has cancellation event with resting_seconds
        cursor = db.conn.cursor()
        cursor.execute("SELECT resting_seconds FROM reward_metrics WHERE event='cancelled'")
        result = cursor.fetchone()
        assert result is not None
        assert result[0] >= 0  # At least 0 seconds

        db.close()

    def test_resting_seconds_accuracy(self):
        """Time between placed/cancelled is correct."""
        db = TradeDB(':memory:')
        krt = KalshiRewardTracker(db)

        with patch("market_maker.time.time", side_effect=[1000.0, 1000.0, 1001.1, 1001.1]):
            krt.log_order_placed(
                order_id="order_3",
                market_key="SPY_250101",
                size=20.0,
                price=0.55,
                mid_price=0.50,
                side="buy"
            )
            krt.log_order_cancelled("order_3")

        # Check DB for resting_seconds
        cursor = db.conn.cursor()
        cursor.execute("SELECT resting_seconds FROM reward_metrics WHERE event='cancelled'")
        resting = cursor.fetchone()[0]

        assert resting == 1

        db.close()

    def test_estimate_daily_reward(self):
        """Reward estimate based on resting time/spread."""
        db = TradeDB(':memory:')
        krt = KalshiRewardTracker(db)

        # Place an order
        krt.log_order_placed(
            order_id="order_4",
            market_key="QQQ_250101",
            size=15.0,
            price=0.485,
            mid_price=0.50,
            side="buy"
        )

        # Estimate should be positive (order resting)
        with patch("market_maker.time.time", return_value=time.time() + 86400):
            estimate = krt.estimate_daily_reward("QQQ_250101")
        assert estimate == pytest.approx(0.485, rel=0.01)

        # No orders on different market
        estimate_none = krt.estimate_daily_reward("XYZ_250101")
        assert estimate_none == 0.0

        db.close()

    def test_multiple_orders(self):
        """Track multiple orders independently."""
        db = TradeDB(':memory:')
        krt = KalshiRewardTracker(db)

        # Place multiple orders
        for i in range(3):
            krt.log_order_placed(
                order_id=f"order_{i}",
                market_key=f"STOCK_{i}",
                size=float(10 + i),
                price=0.50,
                mid_price=0.50,
                side="buy"
            )

        # All should be active
        active = krt.get_active_orders()
        assert len(active) == 3

        # Cancel one
        krt.log_order_cancelled("order_1")

        # Now 2 active
        active = krt.get_active_orders()
        assert len(active) == 2

        db.close()


# ---------------------------------------------------------------------------
# TestRewardConfig (configuration variables)
# ---------------------------------------------------------------------------

class TestRewardConfig:
    """Test REWARDS_* configuration variables."""

    def test_config_rewards_enabled(self):
        """Read REWARDS_ENABLED from environment."""
        import config
        # Default should be False (safe for local dev)
        assert config.REWARDS_ENABLED is False

    def test_config_rewards_max_exposure(self):
        """Read REWARDS_MAX_EXPOSURE, verify default."""
        import config
        assert config.REWARDS_MAX_EXPOSURE == 200.0

    def test_config_rewards_min_size(self):
        """Read REWARDS_MIN_SIZE, verify default."""
        import config
        assert config.REWARDS_MIN_SIZE == 5.0

    def test_config_rewards_max_spread(self):
        """Read REWARDS_MAX_SPREAD, verify default."""
        import config
        assert config.REWARDS_MAX_SPREAD == 0.05

    def test_config_rewards_poll_interval(self):
        """Read REWARDS_POLL_INTERVAL, verify default."""
        import config
        assert config.REWARDS_POLL_INTERVAL == 60

    def test_config_rewards_min_resting_time(self):
        """Read REWARDS_MIN_RESTING_TIME, verify default."""
        import config
        assert config.REWARDS_MIN_RESTING_TIME == 300

    def test_config_defaults(self):
        """All REWARDS_* variables have sensible defaults."""
        import config
        assert config.REWARDS_ENABLED in (True, False)
        assert config.REWARDS_MAX_EXPOSURE > 0
        assert config.REWARDS_MIN_SIZE > 0
        assert 0 < config.REWARDS_MAX_SPREAD < 1.0
        assert config.REWARDS_POLL_INTERVAL > 0
        assert config.REWARDS_MIN_RESTING_TIME > 0


# ---------------------------------------------------------------------------
# TestRewardDatabaseSchema (database persistence)
# ---------------------------------------------------------------------------

class TestRewardDatabaseSchema:
    """Test reward metrics table and trades table columns."""

    def test_reward_metrics_table_created(self):
        """reward_metrics table exists."""
        db = TradeDB(':memory:')
        cursor = db.conn.cursor()
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='reward_metrics'")
        assert cursor.fetchone() is not None
        db.close()

    def test_trades_table_has_reward_columns(self):
        """trades.reward_score and reward_yield_usdc exist."""
        db = TradeDB(':memory:')
        cursor = db.conn.cursor()
        cursor.execute("PRAGMA table_info(trades)")
        columns = [row[1] for row in cursor.fetchall()]

        assert "reward_score" in columns
        assert "reward_yield_usdc" in columns
        db.close()

    def test_log_reward_metric(self):
        """Insert into reward_metrics without error."""
        db = TradeDB(':memory:')

        db.log_reward_metric(
            platform="kalshi",
            market_key="TEST_250101",
            order_id="test_order",
            event="placed",
            size=10.0,
            spread=0.03,
            resting_seconds=0
        )

        cursor = db.conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM reward_metrics")
        count = cursor.fetchone()[0]
        assert count == 1

        # Verify fields
        cursor.execute("SELECT platform, market_key, event, size, spread FROM reward_metrics")
        row = cursor.fetchone()
        assert row[0] == "kalshi"
        assert row[1] == "TEST_250101"
        assert row[2] == "placed"
        assert row[3] == 10.0
        assert row[4] == 0.03

        db.close()

    def test_reward_metrics_schema(self):
        """reward_metrics table has correct schema."""
        db = TradeDB(':memory:')
        cursor = db.conn.cursor()
        cursor.execute("PRAGMA table_info(reward_metrics)")
        columns = {row[1]: row[2] for row in cursor.fetchall()}

        assert columns["id"] == "INTEGER"
        assert columns["platform"] == "TEXT"
        assert columns["market_key"] == "TEXT"
        assert columns["order_id"] == "TEXT"
        assert columns["event"] == "TEXT"
        assert columns["size"] == "REAL"
        assert columns["spread"] == "REAL"
        assert columns["resting_seconds"] == "INTEGER"
        assert columns["timestamp"] == "INTEGER"

        db.close()
