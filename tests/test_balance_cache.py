"""Tests for executor.py balance caching, parallel fetching, and preflight fixes."""

import pytest
from unittest.mock import MagicMock, patch
import time
import threading

import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from db import TradeDB
from risk_manager import RiskManager


# Mock external API modules before importing executor
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


# ---------------------------------------------------------------------------
# Parallel balance fetching
# ---------------------------------------------------------------------------

class TestParallelBalanceFetching:
    def test_fetches_polymarket_and_kalshi_in_parallel(self, ArbitrageExecutor, db, risk_manager):
        """Both PM and Kalshi balances should be fetched for Cross opp types."""
        pm_trader = MagicMock()
        pm_trader.get_balance.return_value = 100.0
        kalshi_client = MagicMock()
        kalshi_client.get_balance.return_value = 50.0

        executor = ArbitrageExecutor(
            pm_trader=pm_trader, kalshi_client=kalshi_client,
            db=db, risk_manager=risk_manager, dry_run=True,
        )
        balances = executor._fetch_balances("Cross(PM_YES + K_NO)")
        assert balances["polymarket"] == 100.0
        assert balances["kalshi"] == 50.0

    def test_fetches_only_kalshi_for_kalshi_binary(self, ArbitrageExecutor, db, risk_manager):
        """KalshiBinary only needs Kalshi balance."""
        pm_trader = MagicMock()
        pm_trader.get_balance.return_value = 100.0
        kalshi_client = MagicMock()
        kalshi_client.get_balance.return_value = 50.0

        executor = ArbitrageExecutor(
            pm_trader=pm_trader, kalshi_client=kalshi_client,
            db=db, risk_manager=risk_manager, dry_run=True,
        )
        balances = executor._fetch_balances("KalshiBinary")
        assert "kalshi" in balances
        # KalshiBinary does NOT need polymarket
        assert "polymarket" not in balances

    def test_fetches_only_polymarket_for_binary(self, ArbitrageExecutor, db, risk_manager):
        """Binary only needs Polymarket balance."""
        pm_trader = MagicMock()
        pm_trader.get_balance.return_value = 100.0
        kalshi_client = MagicMock()

        executor = ArbitrageExecutor(
            pm_trader=pm_trader, kalshi_client=kalshi_client,
            db=db, risk_manager=risk_manager, dry_run=True,
        )
        balances = executor._fetch_balances("Binary")
        assert "polymarket" in balances
        # Binary should NOT fetch kalshi
        assert "kalshi" not in balances

    def test_concurrent_execution_with_delays(self, ArbitrageExecutor, db, risk_manager):
        """Balance fetches should run concurrently, not sequentially."""
        delay_seconds = 0.15
        call_times = {}

        def make_delayed_balance(name, value):
            def delayed():
                call_times[f"{name}_start"] = time.time()
                time.sleep(delay_seconds)
                call_times[f"{name}_end"] = time.time()
                return value
            return delayed

        pm_trader = MagicMock()
        pm_trader.get_balance = make_delayed_balance("pm", 100.0)
        kalshi_client = MagicMock()
        kalshi_client.get_balance = make_delayed_balance("kalshi", 50.0)

        executor = ArbitrageExecutor(
            pm_trader=pm_trader, kalshi_client=kalshi_client,
            db=db, risk_manager=risk_manager, dry_run=True,
        )

        start = time.time()
        balances = executor._fetch_balances("Cross(PM_YES + K_NO)")
        elapsed = time.time() - start

        assert balances["polymarket"] == 100.0
        assert balances["kalshi"] == 50.0
        # If parallel, elapsed should be ~delay_seconds, not ~2*delay_seconds
        assert elapsed < delay_seconds * 1.8, (
            f"Expected parallel execution (~{delay_seconds}s) but took {elapsed:.2f}s"
        )

    def test_handles_failed_balance_fetch(self, ArbitrageExecutor, db, risk_manager):
        """A failing balance fetch for one platform shouldn't block others."""
        pm_trader = MagicMock()
        pm_trader.get_balance.return_value = 100.0
        kalshi_client = MagicMock()
        kalshi_client.get_balance.side_effect = Exception("API error")

        executor = ArbitrageExecutor(
            pm_trader=pm_trader, kalshi_client=kalshi_client,
            db=db, risk_manager=risk_manager, dry_run=True,
        )
        balances = executor._fetch_balances("Cross(PM_YES + K_NO)")
        assert balances["polymarket"] == 100.0
        assert "kalshi" not in balances

    def test_returns_none_when_no_clients(self, ArbitrageExecutor, db, risk_manager):
        """With no clients configured, should return None."""
        executor = ArbitrageExecutor(
            pm_trader=None, kalshi_client=None,
            db=db, risk_manager=risk_manager, dry_run=True,
        )
        result = executor._fetch_balances("Binary")
        assert result is None

    def test_exchange_clients_included_for_cross(self, ArbitrageExecutor, db, risk_manager):
        """Cross opp type should include exchange client balances."""
        pm_trader = MagicMock()
        pm_trader.get_balance.return_value = 100.0
        kalshi_client = MagicMock()
        kalshi_client.get_balance.return_value = 50.0
        betfair_client = MagicMock()
        betfair_client.get_balance.return_value = 200.0

        executor = ArbitrageExecutor(
            pm_trader=pm_trader, kalshi_client=kalshi_client,
            betfair_client=betfair_client,
            db=db, risk_manager=risk_manager, dry_run=True,
        )
        balances = executor._fetch_balances("Cross(PO-BE)")
        assert "betfair" in balances
        assert balances["betfair"] == 200.0


# ---------------------------------------------------------------------------
# Balance cache with TTL
# ---------------------------------------------------------------------------

class TestBalanceCache:
    def test_cache_hit_avoids_refetch(self, ArbitrageExecutor, db, risk_manager):
        """Second call within TTL should return cached result."""
        pm_trader = MagicMock()
        pm_trader.get_balance.return_value = 100.0

        executor = ArbitrageExecutor(
            pm_trader=pm_trader, kalshi_client=None,
            db=db, risk_manager=risk_manager, dry_run=True,
        )

        # First call fetches
        b1 = executor._get_cached_balances("Binary")
        assert b1["polymarket"] == 100.0
        assert pm_trader.get_balance.call_count == 1

        # Second call within TTL should use cache
        b2 = executor._get_cached_balances("Binary")
        assert b2["polymarket"] == 100.0
        assert pm_trader.get_balance.call_count == 1  # no extra call

    def test_cache_expires_after_ttl(self, ArbitrageExecutor, db, risk_manager):
        """After TTL expires, should re-fetch."""
        pm_trader = MagicMock()
        pm_trader.get_balance.return_value = 100.0

        executor = ArbitrageExecutor(
            pm_trader=pm_trader, kalshi_client=None,
            db=db, risk_manager=risk_manager, dry_run=True,
        )

        # First call
        executor._get_cached_balances("Binary")
        assert pm_trader.get_balance.call_count == 1

        # Expire the cache by backdating timestamp
        executor._balance_cache_ts = time.time() - 20.0

        # Should re-fetch
        executor._get_cached_balances("Binary")
        assert pm_trader.get_balance.call_count == 2

    def test_cache_invalidated_on_type_change(self, ArbitrageExecutor, db, risk_manager):
        """Different opp_type should invalidate cache."""
        pm_trader = MagicMock()
        pm_trader.get_balance.return_value = 100.0
        kalshi_client = MagicMock()
        kalshi_client.get_balance.return_value = 50.0

        executor = ArbitrageExecutor(
            pm_trader=pm_trader, kalshi_client=kalshi_client,
            db=db, risk_manager=risk_manager, dry_run=True,
        )

        executor._get_cached_balances("Binary")
        call_count_pm = pm_trader.get_balance.call_count

        # Different type should trigger refetch
        executor._get_cached_balances("KalshiBinary")
        assert kalshi_client.get_balance.call_count >= 1

    def test_invalidate_balance_cache(self, ArbitrageExecutor, db, risk_manager):
        """invalidate_balance_cache() should clear all cached state."""
        pm_trader = MagicMock()
        pm_trader.get_balance.return_value = 100.0

        executor = ArbitrageExecutor(
            pm_trader=pm_trader, kalshi_client=None,
            db=db, risk_manager=risk_manager, dry_run=True,
        )

        executor._get_cached_balances("Binary")
        assert pm_trader.get_balance.call_count == 1

        executor.invalidate_balance_cache()

        # After invalidation, should re-fetch
        executor._get_cached_balances("Binary")
        assert pm_trader.get_balance.call_count == 2

    def test_invalidate_clears_all_state(self, ArbitrageExecutor, db, risk_manager):
        """Invalidation should reset cache dict, timestamp, and type."""
        pm_trader = MagicMock()
        pm_trader.get_balance.return_value = 100.0

        executor = ArbitrageExecutor(
            pm_trader=pm_trader, kalshi_client=None,
            db=db, risk_manager=risk_manager, dry_run=True,
        )

        executor._get_cached_balances("Binary")
        assert executor._balance_cache != {}
        assert executor._balance_cache_ts > 0
        assert executor._balance_cache_type == "Binary"

        executor.invalidate_balance_cache()
        assert executor._balance_cache == {}
        assert executor._balance_cache_ts == 0.0
        assert executor._balance_cache_type == ""

    def test_inflight_fetch_cannot_restore_invalidated_cache(self, ArbitrageExecutor, db, risk_manager):
        executor = ArbitrageExecutor(
            pm_trader=None, kalshi_client=None,
            db=db, risk_manager=risk_manager, dry_run=True,
        )
        fetch_started = threading.Event()
        release_fetch = threading.Event()

        def delayed_fetch(_opp_type):
            fetch_started.set()
            release_fetch.wait(timeout=2)
            return {"polymarket": 100.0}

        executor._fetch_balances = delayed_fetch
        result = []
        thread = threading.Thread(
            target=lambda: result.append(executor._get_cached_balances("Binary")),
        )
        thread.start()
        assert fetch_started.wait(timeout=1)
        executor.invalidate_balance_cache()
        release_fetch.set()
        thread.join(timeout=2)

        assert not thread.is_alive()
        assert result == [None]
        assert executor._balance_cache == {}
        assert executor._balance_cache_ts == 0.0

    def test_cache_returns_none_when_fetch_returns_none(self, ArbitrageExecutor, db, risk_manager):
        """If _fetch_balances returns None, cache should not store it."""
        executor = ArbitrageExecutor(
            pm_trader=None, kalshi_client=None,
            db=db, risk_manager=risk_manager, dry_run=True,
        )
        result = executor._get_cached_balances("Binary")
        assert result is None
        assert executor._balance_cache == {}


# ---------------------------------------------------------------------------
# Preflight balance check (cross-platform correctness)
# ---------------------------------------------------------------------------

class TestPreflightBalance:
    def test_per_leg_budget_cross_uses_min_balance(self, ArbitrageExecutor, db, risk_manager):
        """Cross-platform arbs should use the smaller of the two platform balances."""
        pm_trader = MagicMock()
        kalshi_client = MagicMock()

        executor = ArbitrageExecutor(
            pm_trader=pm_trader, kalshi_client=kalshi_client,
            db=db, risk_manager=risk_manager, dry_run=True,
        )

        balances = {"polymarket": 100.0, "kalshi": 30.0}
        opp = {
            "type": "Cross(PM_YES + K_NO)",
            "_token_ids": ["tok_yes", "tok_no"],
        }
        budget = executor._per_leg_budget("Cross(PM_YES + K_NO)", opp, balances)
        # Cross has 1 leg per platform, budget = min(100, 30) / 1 = 30
        assert budget == 30.0

    def test_per_leg_budget_binary_divides_by_two(self, ArbitrageExecutor, db, risk_manager):
        """Binary arb has 2 legs on same platform — budget = balance / 2."""
        executor = ArbitrageExecutor(
            pm_trader=MagicMock(), kalshi_client=None,
            db=db, risk_manager=risk_manager, dry_run=True,
        )
        balances = {"polymarket": 100.0}
        opp = {"type": "Binary", "_token_ids": ["a", "b"]}
        budget = executor._per_leg_budget("Binary", opp, balances)
        assert budget == 50.0

    def test_per_leg_budget_kalshi_binary_divides_by_two(self, ArbitrageExecutor, db, risk_manager):
        """KalshiBinary has 2 legs on Kalshi — budget = balance / 2."""
        executor = ArbitrageExecutor(
            pm_trader=None, kalshi_client=MagicMock(),
            db=db, risk_manager=risk_manager, dry_run=True,
        )
        balances = {"kalshi": 80.0}
        opp = {"type": "KalshiBinary", "_kalshi_ticker": "T1"}
        budget = executor._per_leg_budget("KalshiBinary", opp, balances)
        assert budget == 40.0

    def test_per_leg_budget_none_when_no_balances(self, ArbitrageExecutor, db, risk_manager):
        """Should return None when no balances available."""
        executor = ArbitrageExecutor(
            pm_trader=None, kalshi_client=None,
            db=db, risk_manager=risk_manager, dry_run=True,
        )
        budget = executor._per_leg_budget("Binary", {}, None)
        assert budget is None

    def test_per_leg_budget_zero_when_balance_is_zero(self, ArbitrageExecutor, db, risk_manager):
        """Should return 0 when platform balance is zero."""
        executor = ArbitrageExecutor(
            pm_trader=MagicMock(), kalshi_client=None,
            db=db, risk_manager=risk_manager, dry_run=True,
        )
        balances = {"polymarket": 0.0}
        opp = {"type": "Binary", "_token_ids": ["a", "b"]}
        budget = executor._per_leg_budget("Binary", opp, balances)
        assert budget == 0.0

    def test_per_leg_budget_exchange_platforms(self, ArbitrageExecutor, db, risk_manager):
        """Exchange platform opp types should use their own balance."""
        executor = ArbitrageExecutor(
            pm_trader=None, kalshi_client=None,
            db=db, risk_manager=risk_manager, dry_run=True,
        )
        for platform, opp_type in [
            ("betfair", "BetfairBackAll"),
            ("smarkets", "SmarketsBackLay"),
            ("gemini", "GeminiBinary"),
            ("ibkr", "IBKRBinary"),
        ]:
            balances = {platform: 75.0}
            opp = {"type": opp_type}
            budget = executor._per_leg_budget(opp_type, opp, balances)
            assert budget is not None
            assert budget > 0, f"{opp_type} budget should be > 0"
