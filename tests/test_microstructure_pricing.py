"""Tests for microstructure_pricing.py — predictive volatility & Poisson hazard rate pricing."""

import math
import sys
import os
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from microstructure_pricing import (
    MicroVolatilityTracker,
    HazardRateEstimator,
    AvellanedaStoikovEngine,
    MicrostructurePricingResult,
)


# ---------------------------------------------------------------------------
# TestMicroVolatilityTracker
# ---------------------------------------------------------------------------


class TestMicroVolatilityTracker:
    def test_initial_state_returns_floor(self):
        tracker = MicroVolatilityTracker(floor_vol=0.01)
        assert tracker.get_volatility("KXTEST") == 0.01
        assert tracker.has_min_samples("KXTEST") is False
        assert tracker.get_regime("KXTEST") == "calm"

    def test_volatility_increases_with_price_shock(self):
        clock = [1000.0]
        tracker = MicroVolatilityTracker(halflife_seconds=30.0, min_samples=2, time_fn=lambda: clock[0])

        tracker.record_price("KXTEST", 0.50, timestamp=1000.0)
        tracker.record_price("KXTEST", 0.51, timestamp=1001.0)
        vol_calm = tracker.get_volatility("KXTEST")

        # Large jump: 0.51 -> 0.70
        tracker.record_price("KXTEST", 0.70, timestamp=1002.0)
        vol_jump = tracker.get_volatility("KXTEST")

        assert vol_jump > vol_calm
        assert tracker.has_min_samples("KXTEST") is True

    def test_volatility_decays_when_price_stabilizes(self):
        tracker = MicroVolatilityTracker(halflife_seconds=10.0, min_samples=2)

        tracker.record_price("KXTEST", 0.50, timestamp=100.0)
        tracker.record_price("KXTEST", 0.70, timestamp=101.0)
        high_vol = tracker.get_volatility("KXTEST")

        # 10 flat ticks over 60 seconds (6 half-lives)
        for t in range(102, 162, 6):
            tracker.record_price("KXTEST", 0.70, timestamp=float(t))

        decayed_vol = tracker.get_volatility("KXTEST")
        assert decayed_vol < high_vol

    def test_volatility_regimes(self):
        tracker = MicroVolatilityTracker(min_samples=2)
        tracker.record_price("T_CALM", 0.50, timestamp=1.0)
        tracker.record_price("T_CALM", 0.5001, timestamp=2.0)
        assert tracker.get_regime("T_CALM") == "calm"

        tracker.record_price("T_SPIKE", 0.20, timestamp=1.0)
        tracker.record_price("T_SPIKE", 0.80, timestamp=2.0)
        assert tracker.get_regime("T_SPIKE") in ("elevated", "extreme")

    def test_invalid_prices_ignored(self):
        tracker = MicroVolatilityTracker()
        tracker.record_price("KXTEST", -0.5)
        tracker.record_price("KXTEST", 0.0)
        assert tracker.has_min_samples("KXTEST") is False


# ---------------------------------------------------------------------------
# TestHazardRateEstimator
# ---------------------------------------------------------------------------


class TestHazardRateEstimator:
    def test_insufficient_trades_returns_default_kappa(self):
        estimator = HazardRateEstimator(default_kappa=12.0, min_trades=3)
        arrival, kappa = estimator.estimate_hazard("KXTEST")
        assert arrival == 0.1
        assert kappa == 12.0

    def test_tight_trades_yield_higher_kappa(self):
        estimator = HazardRateEstimator(window_seconds=60.0, min_trades=3)
        # Trades landing 1 cent from mid price -> high kappa (dense liquidity)
        for i in range(10):
            estimator.record_trade("KXTEST", price=0.51, count=10, mid_price=0.50, timestamp=100.0 + i)

        arrival, kappa = estimator.estimate_hazard("KXTEST", timestamp=110.0)
        assert arrival > 0.0
        # Average distance 0.01 -> 1 / 0.01 = 100 damped toward default -> high kappa >= 15.0
        assert kappa >= 15.0

    def test_deep_trades_yield_lower_kappa(self):
        estimator = HazardRateEstimator(window_seconds=60.0, min_trades=3)
        # Trades landing 10 cents from mid price -> low kappa (sweeps deep into book)
        for i in range(20):
            estimator.record_trade("KXTEST", price=0.60, count=10, mid_price=0.50, timestamp=100.0 + i)

        arrival, kappa = estimator.estimate_hazard("KXTEST", timestamp=120.0)
        assert arrival > 0.0
        # Average distance 0.10 -> 1 / 0.10 = 10 -> lower kappa than tight trades
        assert kappa <= 12.0

    def test_purges_stale_trades(self):
        clock = [200.0]
        estimator = HazardRateEstimator(window_seconds=30.0, min_trades=3, time_fn=lambda: clock[0])

        estimator.record_trade("KXTEST", price=0.51, count=5, mid_price=0.50, timestamp=100.0)
        estimator.record_trade("KXTEST", price=0.51, count=5, mid_price=0.50, timestamp=110.0)
        estimator.record_trade("KXTEST", price=0.51, count=5, mid_price=0.50, timestamp=120.0)

        # At timestamp 200, all trades from 100-120 are older than cutoff (200 - 30 = 170)
        arrival, kappa = estimator.estimate_hazard("KXTEST")
        assert arrival == 0.1
        assert kappa == estimator.default_kappa


# ---------------------------------------------------------------------------
# TestAvellanedaStoikovEngine
# ---------------------------------------------------------------------------


class TestAvellanedaStoikovEngine:
    def test_flat_inventory_centers_reservation_price_at_mid(self):
        engine = AvellanedaStoikovEngine(risk_aversion_gamma=0.20)
        res = engine.calculate_pricing("KXTEST", mid_price=0.50, inventory=0.0)
        assert res.reservation_price == pytest.approx(0.50)
        assert res.optimal_bid < res.reservation_price < res.optimal_ask

    def test_long_inventory_skews_reservation_price_down(self):
        engine = AvellanedaStoikovEngine(risk_aversion_gamma=0.20)
        # Give price shock to establish volatility
        engine.record_price("KXTEST", 0.48, timestamp=1.0)
        engine.record_price("KXTEST", 0.52, timestamp=2.0)

        res = engine.calculate_pricing("KXTEST", mid_price=0.50, inventory=40.0, max_inventory=50.0)
        # Reservation price must be below mid to discourage buying and encourage selling
        assert res.reservation_price < 0.50
        assert res.optimal_bid < 0.50
        assert res.optimal_ask < 0.50 + res.half_spread

    def test_short_inventory_skews_reservation_price_up(self):
        engine = AvellanedaStoikovEngine(risk_aversion_gamma=0.20)
        engine.record_price("KXTEST", 0.48, timestamp=1.0)
        engine.record_price("KXTEST", 0.52, timestamp=2.0)

        res = engine.calculate_pricing("KXTEST", mid_price=0.50, inventory=-40.0, max_inventory=50.0)
        # Reservation price must be above mid to encourage buying and discourage selling
        assert res.reservation_price > 0.50
        assert res.optimal_bid > 0.50 - res.half_spread

    def test_spread_widens_in_volatile_conditions(self):
        engine_calm = AvellanedaStoikovEngine()
        engine_calm.record_price("KXTEST", 0.500, timestamp=1.0)
        engine_calm.record_price("KXTEST", 0.501, timestamp=2.0)
        engine_calm.record_price("KXTEST", 0.500, timestamp=3.0)
        res_calm = engine_calm.calculate_pricing("KXTEST", mid_price=0.50)

        engine_shock = AvellanedaStoikovEngine()
        engine_shock.record_price("KXTEST", 0.30, timestamp=1.0)
        engine_shock.record_price("KXTEST", 0.70, timestamp=2.0)
        engine_shock.record_price("KXTEST", 0.35, timestamp=3.0)
        res_shock = engine_shock.calculate_pricing("KXTEST", mid_price=0.50)

        assert res_shock.half_spread > res_calm.half_spread
        assert res_shock.micro_volatility > res_calm.micro_volatility

    def test_half_spread_clamped_to_safety_bounds(self):
        engine = AvellanedaStoikovEngine(
            min_half_spread_cents=2.0,
            max_half_spread_cents=8.0,
        )
        res = engine.calculate_pricing("KXTEST", mid_price=0.50)
        assert 0.02 <= res.half_spread <= 0.08

    def test_adaptive_sizing_scales_down_under_high_volatility(self):
        engine = AvellanedaStoikovEngine(adaptive_sizing_enabled=True)
        engine.record_price("KXTEST", 0.20, timestamp=1.0)
        engine.record_price("KXTEST", 0.80, timestamp=2.0)
        engine.record_price("KXTEST", 0.30, timestamp=3.0)
        res = engine.calculate_pricing("KXTEST", mid_price=0.50)

        # High volatility -> multiplier reduced
        assert res.sizing_multiplier < 1.0

    def test_get_status_contains_diagnostics(self):
        engine = AvellanedaStoikovEngine()
        status = engine.get_status("KXTEST")
        assert "gamma" in status
        assert "min_half_spread_cents" in status
        assert "micro_volatility" in status
        assert "hazard_kappa" in status
        assert "volatility_regime" in status

    def test_pricing_result_to_dict(self):
        result = MicrostructurePricingResult(
            ticker="KXTEST",
            mid=0.50,
            reservation_price=0.49,
            optimal_bid=0.46,
            optimal_ask=0.52,
            half_spread=0.03,
            micro_volatility=0.04,
            hazard_kappa=12.0,
            arrival_intensity=0.5,
            volatility_regime="normal",
            sizing_multiplier=1.1,
        )
        d = result.to_dict()
        assert d["ticker"] == "KXTEST"
        assert d["mid"] == 0.50
        assert d["reservation_price"] == 0.49
        assert d["half_spread"] == 0.03
        assert d["volatility_regime"] == "normal"
