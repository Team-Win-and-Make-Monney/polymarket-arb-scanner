"""Tests for new Layer 2-5 strategy modules."""

import pytest
from unittest.mock import MagicMock, patch
import sys
import os
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


# ---------------------------------------------------------------------------
# PriceTracker tests
# ---------------------------------------------------------------------------

class TestPriceTracker:
    def test_update_and_get_price(self):
        from price_tracker import PriceTracker
        tracker = PriceTracker()
        tracker.update("polymarket", "market_1", 0.55)
        result = tracker.get_price("polymarket", "market_1")
        assert result is not None
        price, ts = result
        assert price == 0.55
        assert ts > 0

    def test_get_price_nonexistent(self):
        from price_tracker import PriceTracker
        tracker = PriceTracker()
        result = tracker.get_price("polymarket", "nonexistent")
        assert result is None

    def test_get_all_prices(self):
        from price_tracker import PriceTracker
        tracker = PriceTracker()
        tracker.update("polymarket", "mkt", 0.50)
        tracker.update("kalshi", "mkt", 0.52)
        all_prices = tracker.get_all_prices("mkt")
        assert "polymarket" in all_prices
        assert "kalshi" in all_prices
        assert all_prices["polymarket"][0] == 0.50
        assert all_prices["kalshi"][0] == 0.52

    def test_detect_stale_no_data(self):
        from price_tracker import PriceTracker
        tracker = PriceTracker()
        result = tracker.detect_stale_opportunities("mkt")
        assert result == []

    def test_cleanup_removes_old_entries(self):
        from price_tracker import PriceTracker
        tracker = PriceTracker()
        tracker.update("polymarket", "mkt", 0.50)
        # Force the timestamp to be old — internal structure is {market_key: {platform: (price, ts)}}
        with tracker._lock:
            tracker._prices["mkt"]["polymarket"] = (0.50, time.time() - 600)
        tracker.cleanup(max_age_seconds=300)
        assert tracker.get_price("polymarket", "mkt") is None


# ---------------------------------------------------------------------------
# Stale price scan tests
# ---------------------------------------------------------------------------

class TestStalePriceScan:
    def test_empty_input(self):
        from scans.stale import scan_stale_prices
        from price_tracker import PriceTracker
        tracker = PriceTracker()
        result = scan_stale_prices(tracker, [])
        assert result == []

    def test_returns_list(self):
        from scans.stale import scan_stale_prices
        from price_tracker import PriceTracker
        tracker = PriceTracker()
        result = scan_stale_prices(tracker, [], min_profit=0.001)
        assert isinstance(result, list)


# ---------------------------------------------------------------------------
# Resolution sniping scan tests
# ---------------------------------------------------------------------------

class TestResolutionSnipeScan:
    def test_empty_markets(self):
        from scans.resolution import scan_resolution_snipes
        result = scan_resolution_snipes([])
        assert result == []

    def test_returns_list(self):
        from scans.resolution import scan_resolution_snipes
        result = scan_resolution_snipes([], platform="polymarket")
        assert isinstance(result, list)

    def test_near_resolution_check(self):
        from scans.resolution import _is_near_resolution
        # Already resolved
        assert not _is_near_resolution({"status": "settled"})
        # Determination pending
        assert _is_near_resolution({"status": "determination_pending"})

    def test_extract_outcome_prices_empty(self):
        from scans.resolution import _extract_outcome_prices
        result = _extract_outcome_prices({}, "polymarket")
        assert result == {}

    def test_extract_outcome_prices_polymarket(self):
        from scans.resolution import _extract_outcome_prices
        market = {
            "tokens": [
                {"outcome": "Yes", "price": 0.96},
                {"outcome": "No", "price": 0.04},
            ]
        }
        result = _extract_outcome_prices(market, "polymarket")
        assert result["yes"] == 0.96
        assert result["no"] == 0.04


# ---------------------------------------------------------------------------
# Convergence scan tests
# ---------------------------------------------------------------------------

class TestConvergenceScan:
    def test_empty_markets(self):
        from scans.convergence import scan_convergence
        result = scan_convergence([])
        assert result == []

    def test_too_few_platforms(self):
        from scans.convergence import scan_convergence
        matched = [{
            "market_key": "test",
            "title": "Test",
            "platform_prices": {
                "polymarket": {"yes": 0.50},
                "kalshi": {"yes": 0.55},
            },
        }]
        result = scan_convergence(matched, min_platforms=3)
        assert result == []

    def test_detects_divergence(self):
        from scans.convergence import scan_convergence
        matched = [{
            "market_key": "test_mkt",
            "title": "Test Market",
            "platform_prices": {
                "polymarket": {"yes": 0.50},
                "kalshi": {"yes": 0.51},
                "betfair": {"yes": 0.52},
                "smarkets": {"yes": 0.70},  # Outlier
            },
        }]
        result = scan_convergence(matched, min_divergence=0.05, min_platforms=3, min_profit=0.001)
        assert len(result) > 0
        assert result[0]["type"] == "ConvergenceOpp"
        assert result[0]["_platform"] == "smarkets"

    def test_confidence_calculation(self):
        from scans.convergence import _convergence_confidence
        conf = _convergence_confidence(4, 0.10, 0.50)
        assert 0 < conf < 1


# ---------------------------------------------------------------------------
# SignalAggregator tests
# ---------------------------------------------------------------------------

class TestSignalAggregator:
    def test_add_and_get_consensus(self):
        from signal_aggregator import SignalAggregator
        agg = SignalAggregator()
        agg.add_signal("mkt1", "metaculus", 0.60)
        agg.add_signal("mkt1", "manifold", 0.65)
        consensus = agg.get_consensus("mkt1")
        assert consensus is not None
        assert 0.60 <= consensus["probability"] <= 0.65
        assert consensus["num_sources"] == 2

    def test_no_signals_returns_none(self):
        from signal_aggregator import SignalAggregator
        agg = SignalAggregator()
        assert agg.get_consensus("missing") is None

    def test_invalid_probability_rejected(self):
        from signal_aggregator import SignalAggregator
        agg = SignalAggregator()
        agg.add_signal("mkt1", "bad_source", 1.5)  # Invalid
        assert agg.get_consensus("mkt1") is None

    def test_divergence_detection(self):
        from signal_aggregator import SignalAggregator
        agg = SignalAggregator()
        agg.add_signal("mkt1", "metaculus", 0.60)
        agg.add_signal("mkt1", "manifold", 0.62)
        agg.add_signal("mkt1", "polymarket", 0.40)  # Divergent
        divs = agg.get_divergences("mkt1", min_divergence=0.10)
        assert len(divs) > 0
        assert divs[0]["source"] == "polymarket"

    def test_cleanup_removes_stale(self):
        from signal_aggregator import SignalAggregator
        agg = SignalAggregator(cache_ttl=1.0)
        agg.add_signal("mkt1", "test", 0.50)
        # Force staleness
        with agg._lock:
            agg._cache["mkt1"]["test"]["timestamp"] = time.time() - 100
        removed = agg.cleanup(max_age=1.0)
        assert removed > 0
        assert agg.get_consensus("mkt1") is None

    def test_source_weights(self):
        from signal_aggregator import SignalAggregator
        agg = SignalAggregator(source_weights={"source_a": 2.0, "source_b": 1.0})
        agg.add_signal("mkt", "source_a", 0.80)
        agg.add_signal("mkt", "source_b", 0.20)
        consensus = agg.get_consensus("mkt")
        # Weighted: (0.80*2 + 0.20*1) / 3 = 0.60
        assert consensus is not None
        assert abs(consensus["probability"] - 0.60) < 0.01


# ---------------------------------------------------------------------------
# MarketMaker tests
# ---------------------------------------------------------------------------

class TestMarketMaker:
    def test_quote_engine_basic(self):
        from market_maker import QuoteEngine
        engine = QuoteEngine(min_spread=0.04)
        quotes = engine.calculate_quotes(0.50)
        assert quotes["bid"] < 0.50
        assert quotes["ask"] > 0.50
        assert quotes["spread"] >= 0.04

    def test_quote_engine_inventory_skew(self):
        from market_maker import QuoteEngine
        engine = QuoteEngine(min_spread=0.04, inventory_skew_factor=0.5)
        # Long inventory -> lower quotes to sell
        long_quotes = engine.calculate_quotes(0.50, inventory=25.0, max_inventory=50.0)
        neutral_quotes = engine.calculate_quotes(0.50, inventory=0.0, max_inventory=50.0)
        assert long_quotes["bid"] < neutral_quotes["bid"]
        assert long_quotes["ask"] < neutral_quotes["ask"]

    def test_inventory_tracker_basic(self):
        from market_maker import InventoryTracker
        tracker = InventoryTracker(max_per_market=100, max_total=500)
        tracker.update("mkt1", "polymarket", 10.0)
        assert tracker.get_position("mkt1") == 10.0
        assert tracker.get_position("mkt1", "polymarket") == 10.0
        assert tracker.get_total_exposure() == 10.0

    def test_inventory_tracker_limits(self):
        from market_maker import InventoryTracker
        tracker = InventoryTracker(max_per_market=20, max_total=100)
        tracker.update("mkt1", "polymarket", 15.0)
        assert tracker.can_trade("mkt1", 5.0)
        assert not tracker.can_trade("mkt1", 10.0)  # Would exceed per-market limit

    def test_inventory_needs_hedge(self):
        from market_maker import InventoryTracker
        tracker = InventoryTracker(max_per_market=100)
        tracker.update("mkt1", "polymarket", 85.0)
        assert tracker.needs_hedge("mkt1")
        tracker2 = InventoryTracker(max_per_market=100)
        tracker2.update("mkt1", "polymarket", 50.0)
        assert not tracker2.needs_hedge("mkt1")

    def test_quote_manager_place_and_cancel(self):
        from market_maker import QuoteManager
        mgr = QuoteManager()
        oid = mgr.place_quote("polymarket", "mkt1", "bid", 0.48, 5.0)
        assert oid is not None
        orders = mgr.get_active_orders("mkt1")
        assert len(orders) == 1
        assert mgr.cancel_quote(oid)
        assert len(mgr.get_active_orders("mkt1")) == 0

    def test_quote_manager_retains_order_when_exchange_cancel_fails(self):
        from market_maker import QuoteManager
        mgr = QuoteManager()
        oid = "live_order_1"
        mgr._active_orders[oid] = {
            "platform": "polymarket", "market_key": "mkt1", "side": "bid",
            "price": 0.48, "size": 5.0, "status": "resting",
            "placed_at": time.time(),
        }
        trader = MagicMock()
        trader.cancel_order.side_effect = RuntimeError("venue unavailable")

        assert mgr.cancel_all() == 0
        assert [order["order_id"] for order in mgr.get_active_orders()] == [oid]
        assert mgr.cancel_all(trader=trader) == 0
        assert [order["order_id"] for order in mgr.get_active_orders()] == [oid]

        trader.cancel_order.side_effect = None
        trader.cancel_order.return_value = True
        assert mgr.cancel_all(trader=trader) == 1
        assert mgr.get_active_orders() == []

    def test_market_maker_add_and_generate(self):
        from market_maker import MarketMaker
        mm = MarketMaker(min_spread=0.04, quote_size=5.0, dry_run=True)
        mm.add_market("mkt1", "polymarket", 0.50)
        opps = mm.generate_opportunities()
        assert len(opps) > 0
        assert opps[0]["type"] == "MarketMake"
        assert opps[0]["_bid_price"] < 0.50
        assert opps[0]["_ask_price"] > 0.50

    def test_market_maker_refresh_quotes(self):
        from market_maker import MarketMaker
        mm = MarketMaker(min_spread=0.04, quote_size=5.0, dry_run=True)
        mm.add_market("mkt1", "polymarket", 0.50)
        new_quotes = mm.refresh_quotes()
        assert len(new_quotes) == 2  # bid + ask

    def test_market_maker_status(self):
        from market_maker import MarketMaker
        mm = MarketMaker(dry_run=True)
        mm.add_market("mkt1", "polymarket", 0.50)
        status = mm.get_status()
        assert status["active_markets"] == 1
        assert status["dry_run"] is True

    def test_market_maker_stop(self):
        from market_maker import MarketMaker
        mm = MarketMaker(dry_run=True)
        mm.add_market("mkt1", "polymarket", 0.50)
        mm.refresh_quotes()
        mm.stop()
        assert len(mm.quote_manager.get_active_orders()) == 0


# ---------------------------------------------------------------------------
# ManifoldClient tests
# ---------------------------------------------------------------------------

class TestManifoldClient:
    def test_init_default(self):
        from manifold_api import ManifoldClient
        client = ManifoldClient()
        assert client.base_url == "https://api.manifold.markets/v0"

    def test_init_with_api_key(self):
        from manifold_api import ManifoldClient
        client = ManifoldClient(api_key="test_key")
        assert "Authorization" in client.session.headers


# ---------------------------------------------------------------------------
# PositionSizer tests
# ---------------------------------------------------------------------------

class TestPositionSizer:
    def test_kelly_size_basic(self):
        from position_sizer import PositionSizer
        sizer = PositionSizer(bankroll=1000.0)
        # Edge of 10%, odds of 1:1 -> kelly = 10%
        fraction = sizer.kelly_size(0.10, 1.0)
        assert abs(fraction - 0.10) < 0.001

    def test_kelly_size_zero_edge(self):
        from position_sizer import PositionSizer
        sizer = PositionSizer(bankroll=1000.0)
        fraction = sizer.kelly_size(0.0, 1.0)
        assert fraction == 0.0

    def test_size_for_pure_arb(self):
        from position_sizer import PositionSizer
        sizer = PositionSizer(bankroll=1000.0, kelly_fraction=0.5, max_fraction=0.25)
        opp = {"type": "Binary", "net_roi": 0.05, "total_cost": "$0.95"}
        size = sizer.size_for_opportunity(opp)
        assert size > 0
        assert size <= 1000.0 * 0.25  # Max fraction cap

    def test_size_capped_at_max_fraction(self):
        from position_sizer import PositionSizer
        sizer = PositionSizer(bankroll=100.0, kelly_fraction=1.0, max_fraction=0.10)
        opp = {"type": "Binary", "net_roi": 0.50, "total_cost": "$0.50"}
        size = sizer.size_for_opportunity(opp)
        assert size <= 10.0  # 100 * 0.10

    def test_update_bankroll(self):
        from position_sizer import PositionSizer
        sizer = PositionSizer(bankroll=1000.0)
        sizer.update_bankroll(2000.0)
        assert sizer.bankroll == 2000.0


# ---------------------------------------------------------------------------
# Executor new types integration tests
# ---------------------------------------------------------------------------

class TestExecutorNewTypes:
    """Test that executor handles new opportunity types without crashing."""

    @pytest.fixture(autouse=True)
    def setup_executor(self):
        """Create a minimal executor for testing."""
        # Mock all the platform clients
        self.mock_db = MagicMock()
        self.mock_risk = MagicMock()
        self.mock_risk.check.return_value = (True, "")
        self.mock_risk.clamp_size.return_value = 5.0
        self.mock_risk.calculate_dynamic_size.return_value = 5.0

        from executor import ArbitrageExecutor
        self.executor = ArbitrageExecutor(
            pm_trader=None,
            kalshi_client=None,
            db=self.mock_db,
            risk_manager=self.mock_risk,
            dry_run=True,
        )

    def test_stale_price_build_legs(self):
        opp = {
            "type": "StalePriceOpp",
            "_platform": "polymarket",
            "_direction": "BUY_YES",
            "_trade_price": 0.45,
            "_token_ids": ["tok_yes", "tok_no"],
        }
        with patch("executor.ENABLED_EXECUTION_PLATFORMS", frozenset({"polymarket"})):
            legs = self.executor._build_legs(opp, 5.0)
        assert len(legs) == 1
        assert legs[0]["platform"] == "polymarket"

    def test_resolution_snipe_build_legs(self):
        opp = {
            "type": "ResolutionSnipeOpp",
            "_platform": "kalshi",
            "_direction": "BUY_YES",
            "_trade_price": 0.96,
            "_kalshi_ticker": "TEST-TICKER",
        }
        legs = self.executor._build_legs(opp, 5.0)
        assert len(legs) == 1
        assert legs[0]["platform"] == "kalshi"

    def test_convergence_build_legs(self):
        opp = {
            "type": "ConvergenceOpp",
            "_platform": "polymarket",
            "_direction": "BUY_NO",
            "_trade_price": 0.30,
            "_token_ids": ["tok_yes", "tok_no"],
        }
        with patch("executor.ENABLED_EXECUTION_PLATFORMS", frozenset({"polymarket"})):
            legs = self.executor._build_legs(opp, 5.0)
        assert len(legs) == 1
        assert legs[0]["token"] == "no"

    def test_market_make_build_legs(self):
        opp = {
            "type": "MarketMake",
            "_platform": "polymarket",
            "_bid_price": 0.48,
            "_ask_price": 0.52,
            "_market_key": "test_mkt",
        }
        with patch("executor.ENABLED_EXECUTION_PLATFORMS", frozenset({"polymarket"})):
            legs = self.executor._build_legs(opp, 5.0)
        assert len(legs) == 2
        assert legs[0]["_mm_side"] == "bid"
        assert legs[1]["_mm_side"] == "ask"

    def test_revalidate_stale_returns_true(self):
        opp = {"type": "StalePriceOpp", "net_profit": 0.01}
        assert self.executor._revalidate(opp) is True

    def test_revalidate_resolution_returns_true(self):
        opp = {"type": "ResolutionSnipeOpp", "net_profit": 0.01}
        assert self.executor._revalidate(opp) is True

    def test_revalidate_convergence_returns_true(self):
        opp = {"type": "ConvergenceOpp", "net_profit": 0.01}
        assert self.executor._revalidate(opp) is True

    def test_revalidate_mm_returns_true(self):
        opp = {"type": "MarketMake", "net_profit": 0.01}
        assert self.executor._revalidate(opp) is True
