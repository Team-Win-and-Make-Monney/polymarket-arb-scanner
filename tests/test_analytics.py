"""Tests for scripts/analytics.py — DuckDB-based per-strategy P&L analytics."""

import json
import pytest
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch, MagicMock

import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from scripts.analytics import get_strategy_metrics


class TestDuckDBAnalytics(unittest.TestCase):
    """Test suite for DuckDB analytics query and metrics calculation."""

    @pytest.fixture(autouse=True)
    def temp_db(self):
        """Create a temporary SQLite database with test data."""
        self.temp_file = tempfile.NamedTemporaryFile(delete=False, suffix=".db")
        self.db_path = self.temp_file.name
        self.temp_file.close()

        # Create tables matching TradeDB schema
        conn = sqlite3.connect(self.db_path)
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS opportunities (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                type TEXT NOT NULL,
                market TEXT NOT NULL,
                prices TEXT,
                total_cost REAL,
                net_profit REAL,
                net_roi REAL,
                depth REAL,
                action TEXT NOT NULL
            );
        """)
        conn.commit()
        conn.close()

        yield

        # Cleanup
        Path(self.db_path).unlink(missing_ok=True)

    def test_empty_database_returns_empty_list(self):
        """Test 1: Empty database returns empty list (no strategies)."""
        result = get_strategy_metrics(db_path=self.db_path, lookback_days=7)
        assert result == []

    def test_single_strategy_with_5_trades(self):
        """Test 2: Single strategy with 5 trades (3 wins) returns correct metrics."""
        conn = sqlite3.connect(self.db_path)
        now = datetime.now(timezone.utc)

        # Insert 5 trades for strategy "binary" with 3 wins (profit > 0)
        trades_data = [
            (now.isoformat(), "binary", "Market A", None, 1.0, 0.05, 0.05, 1.0, "executed"),
            ((now - timedelta(hours=1)).isoformat(), "binary", "Market B", None, 1.0, 0.03, 0.03, 1.0, "executed"),
            ((now - timedelta(hours=2)).isoformat(), "binary", "Market C", None, 1.0, -0.02, -0.02, 1.0, "executed"),
            ((now - timedelta(hours=3)).isoformat(), "binary", "Market D", None, 1.0, 0.04, 0.04, 1.0, "executed"),
            ((now - timedelta(hours=4)).isoformat(), "binary", "Market E", None, 1.0, -0.01, -0.01, 1.0, "executed"),
        ]

        for trade in trades_data:
            conn.execute(
                "INSERT INTO opportunities (timestamp, type, market, prices, total_cost, net_profit, net_roi, depth, action) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                trade
            )
        conn.commit()
        conn.close()

        result = get_strategy_metrics(db_path=self.db_path, lookback_days=7)

        assert len(result) == 1
        assert result[0]["strategy"] == "binary"
        assert result[0]["trade_count"] == 5
        assert result[0]["wins"] == 3
        assert result[0]["win_rate"] == pytest.approx(0.6, abs=0.01)
        assert result[0]["annual_sharpe"] == "N/A"  # < 20 trades

    def test_strategy_with_20_plus_trades_returns_numeric_sharpe(self):
        """Test 3: Strategy with 20+ trades returns numeric Sharpe (not N/A)."""
        conn = sqlite3.connect(self.db_path)
        now = datetime.now(timezone.utc)

        # Insert 25 trades with varying profits
        for i in range(25):
            profit = 0.05 if i % 2 == 0 else -0.03
            timestamp = (now - timedelta(hours=i)).isoformat()
            conn.execute(
                "INSERT INTO opportunities (timestamp, type, market, prices, total_cost, net_profit, net_roi, depth, action) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (timestamp, "cross", f"Market {i}", None, 1.0, profit, profit, 1.0, "executed")
            )
        conn.commit()
        conn.close()

        result = get_strategy_metrics(db_path=self.db_path, lookback_days=7)

        assert len(result) == 1
        assert result[0]["trade_count"] == 25
        # Sharpe should be numeric, not "N/A"
        assert isinstance(result[0]["annual_sharpe"], (int, float))
        assert result[0]["annual_sharpe"] != "N/A"

    def test_sharpe_calculation_uses_mean_over_volatility(self):
        """Test 4: Sharpe = mean / standard deviation * sqrt(252)."""
        conn = sqlite3.connect(self.db_path)
        now = datetime.now(timezone.utc)

        profits = [0.01, 0.03] * 10
        for i in range(20):
            timestamp = (now - timedelta(hours=i)).isoformat()
            conn.execute(
                "INSERT INTO opportunities (timestamp, type, market, prices, total_cost, net_profit, net_roi, depth, action) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (timestamp, "kalshi", f"Market {i}", None, 1.0, profits[i], profits[i], 1.0, "executed")
            )
        conn.commit()
        conn.close()

        result = get_strategy_metrics(db_path=self.db_path, lookback_days=7)

        assert len(result) == 1
        assert result[0]["annual_sharpe"] == pytest.approx(2.0 * (252 ** 0.5), rel=0.001)

    def test_max_drawdown_calculation(self):
        """Test 5: Max drawdown = peak cumulative PnL - trough cumulative PnL."""
        conn = sqlite3.connect(self.db_path)
        now = datetime.now(timezone.utc)

        # Create trades with predictable cumulative PnL (ordered by timestamp ascending):
        # Trade 1 (oldest): +0.03 -> cumsum = 0.03
        # Trade 2: -0.02 -> cumsum = 0.01 (trough)
        # Trade 3: -0.08 -> cumsum = -0.07
        # Trade 4: +0.05 -> cumsum = -0.02
        # Trade 5 (most recent): +0.10 -> cumsum = 0.08
        # Max drawdown is the largest peak-to-later-trough decline: 0.10.

        trades = [
            ((now - timedelta(hours=4)).isoformat(), 0.03),
            ((now - timedelta(hours=3)).isoformat(), -0.02),
            ((now - timedelta(hours=2)).isoformat(), -0.08),
            ((now - timedelta(hours=1)).isoformat(), 0.05),
            (now.isoformat(), 0.10),
        ]

        for timestamp, profit in trades:
            conn.execute(
                "INSERT INTO opportunities (timestamp, type, market, prices, total_cost, net_profit, net_roi, depth, action) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (timestamp, "negrisk", "Market", None, 1.0, profit, profit, 1.0, "executed")
            )
        conn.commit()
        conn.close()

        result = get_strategy_metrics(db_path=self.db_path, lookback_days=7)

        assert len(result) == 1
        assert result[0]["max_drawdown"] == pytest.approx(0.10, abs=0.001)

    def test_max_drawdown_includes_same_timestamp_loss(self):
        """Stable id ordering must expose losses among timestamp peers."""
        conn = sqlite3.connect(self.db_path)
        timestamp = datetime.now(timezone.utc).isoformat()
        trades = [(0.10, "first"), (-0.08, "second"), (0.02, "third")]
        for profit, market in trades:
            conn.execute(
                "INSERT INTO opportunities (timestamp, type, market, prices, total_cost, net_profit, "
                "net_roi, depth, action) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (timestamp, "same-ts", market, None, 1.0, profit, profit, 1.0, "executed"),
            )
        conn.commit()
        conn.close()

        result = get_strategy_metrics(db_path=self.db_path, lookback_days=7)

        assert len(result) == 1
        assert result[0]["max_drawdown"] == pytest.approx(0.08, abs=0.001)

    def test_cutoff_timestamp_7_days_before_now(self):
        """Test 6: Cutoff is exactly 7 days before now; trades outside window excluded."""
        conn = sqlite3.connect(self.db_path)
        now = datetime.now(timezone.utc)

        # Insert trade within 7-day window
        within_window = (now - timedelta(days=3)).isoformat()
        conn.execute(
            "INSERT INTO opportunities (timestamp, type, market, prices, total_cost, net_profit, net_roi, depth, action) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (within_window, "spread", "Market A", None, 1.0, 0.05, 0.05, 1.0, "executed")
        )

        # Insert trade outside 7-day window
        outside_window = (now - timedelta(days=10)).isoformat()
        conn.execute(
            "INSERT INTO opportunities (timestamp, type, market, prices, total_cost, net_profit, net_roi, depth, action) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (outside_window, "spread", "Market B", None, 1.0, 0.03, 0.03, 1.0, "executed")
        )

        conn.commit()
        conn.close()

        result = get_strategy_metrics(db_path=self.db_path, lookback_days=7)

        assert len(result) == 1
        assert result[0]["trade_count"] == 1  # Only the within-window trade counted
        assert result[0]["strategy"] == "spread"

    def test_results_sorted_by_total_pnl_descending(self):
        """Test 7: Results sorted by total_pnl descending."""
        conn = sqlite3.connect(self.db_path)
        now = datetime.now(timezone.utc)

        # Insert multiple strategies with different total PnLs
        strategies = [
            ("kalshi", 0.02, 3),    # total = 0.06
            ("binary", 0.10, 1),    # total = 0.10
            ("cross", -0.01, 5),    # total = -0.05
        ]

        for strategy, profit, count in strategies:
            for i in range(count):
                timestamp = (now - timedelta(hours=len(conn.execute("SELECT * FROM opportunities").fetchall()))).isoformat()
                conn.execute(
                    "INSERT INTO opportunities (timestamp, type, market, prices, total_cost, net_profit, net_roi, depth, action) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (timestamp, strategy, f"Market {i}", None, 1.0, profit, profit, 1.0, "executed")
                )
        conn.commit()
        conn.close()

        result = get_strategy_metrics(db_path=self.db_path, lookback_days=7)

        assert len(result) == 3
        # Verify sorted by total_pnl descending: 0.10, 0.06, -0.05
        assert result[0]["total_pnl"] == pytest.approx(0.10, abs=0.001)
        assert result[1]["total_pnl"] == pytest.approx(0.06, abs=0.001)
        assert result[2]["total_pnl"] == pytest.approx(-0.05, abs=0.001)

    def test_fallback_to_empty_list_on_db_error(self):
        """Test: Fallback to empty list if DB is inaccessible."""
        # Try to access a non-existent DB
        result = get_strategy_metrics(db_path="/nonexistent/path/trades.db", lookback_days=7)
        assert result == []

    def test_action_filtering_includes_executed_filled_dry_run(self):
        """Test: Only trades with action in ('executed', 'filled', 'dry_run') are counted."""
        conn = sqlite3.connect(self.db_path)
        now = datetime.now(timezone.utc)

        # Insert trades with different actions
        actions = [
            ("executed", 0.05),
            ("filled", 0.03),
            ("dry_run", 0.02),
            ("pending", 0.04),  # Should be excluded
            ("failed", 0.01),   # Should be excluded
        ]

        for action, profit in actions:
            conn.execute(
                "INSERT INTO opportunities (timestamp, type, market, prices, total_cost, net_profit, net_roi, depth, action) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (now.isoformat(), "kalshi", "Market", None, 1.0, profit, profit, 1.0, action)
            )
        conn.commit()
        conn.close()

        result = get_strategy_metrics(db_path=self.db_path, lookback_days=7)

        assert len(result) == 1
        # Only 3 trades should be counted (executed, filled, dry_run)
        assert result[0]["trade_count"] == 3
        assert result[0]["total_pnl"] == pytest.approx(0.10, abs=0.001)

    def test_zero_trades_returns_na_metrics(self):
        """Test: Strategy with 0 trades shows trade_count=0 with N/A metrics."""
        conn = sqlite3.connect(self.db_path)
        now = datetime.now(timezone.utc)

        # Insert trades from a different time window (outside 7-day lookback)
        for i in range(3):
            timestamp = (now - timedelta(days=10 + i)).isoformat()
            conn.execute(
                "INSERT INTO opportunities (timestamp, type, market, prices, total_cost, net_profit, net_roi, depth, action) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (timestamp, "binary", f"Market {i}", None, 1.0, 0.05, 0.05, 1.0, "executed")
            )
        conn.commit()
        conn.close()

        # Query with 7-day window - should return no results (trades are outside window)
        result = get_strategy_metrics(db_path=self.db_path, lookback_days=7)

        # Outside window, so no results
        assert result == []


if __name__ == "__main__":
    unittest.main()
