"""Tests for fees.py — fee calculators and net profit functions."""

import math
import pytest
from unittest.mock import patch

import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from fees import (
    polymarket_fee,
    polymarket_taker_fee,
    polymarket_maker_rebate,
    kalshi_taker_fee,
    kalshi_maker_fee,
    net_profit_binary_internal,
    net_profit_negrisk_internal,
    net_profit_negrisk_no_side,
    net_profit_kalshi_binary,
    net_profit_kalshi_multi,
    net_profit_cross_platform,
    betfair_commission,
    net_profit_cross_betfair,
    smarkets_commission,
    gemini_fee,
    net_profit_gemini_binary,
    net_profit_gemini_multi,
    net_profit_cross_gemini,
    net_profit_ibkr_binary,
    net_profit_cross_ibkr,
    net_profit_cross_generic,
    _platform_win_fee,
    _platform_entry_fee,
    PLATFORM_FEE_SCHEDULE,
)


# ---------------------------------------------------------------------------
# polymarket_fee
# ---------------------------------------------------------------------------

class TestPolymarketFee:
    def test_zero_when_sell_equals_buy(self):
        assert polymarket_fee(0.50, 0.50) == 0.0

    def test_zero_when_sell_less_than_buy(self):
        assert polymarket_fee(0.80, 0.60) == 0.0

    def test_correct_2_percent_calculation(self):
        # Net winnings = 1.0 - 0.40 = 0.60; fee = 0.02 * 0.60 = 0.012
        assert polymarket_fee(0.40, 1.0) == pytest.approx(0.012)

    def test_small_spread(self):
        # Net winnings = 1.0 - 0.95 = 0.05; fee = 0.02 * 0.05 = 0.001
        assert polymarket_fee(0.95, 1.0) == pytest.approx(0.001)

    def test_default_sell_price_is_one(self):
        assert polymarket_fee(0.40) == pytest.approx(0.012)

    def test_zero_buy_price(self):
        # Net winnings = 1.0 - 0.0 = 1.0; fee = 0.02
        assert polymarket_fee(0.0, 1.0) == pytest.approx(0.02)


# ---------------------------------------------------------------------------
# polymarket_maker_rebate
# ---------------------------------------------------------------------------

class TestPolymarketMakerRebate:
    def test_default_share_is_25_percent_of_taker_fee(self):
        # Politics: taker = 0.04 * 1 * 0.5 * 0.5 = 0.01; rebate = 25% = 0.0025
        assert polymarket_maker_rebate(0.50, 1, category="politics") == pytest.approx(0.0025)

    def test_crypto_share_is_20_percent(self):
        # Crypto: taker = 0.07 * 1 * 0.5 * 0.5 = 0.0175; rebate = 20% = 0.0035
        assert polymarket_maker_rebate(0.50, 1, category="crypto") == pytest.approx(0.0035)

    def test_geopolitics_fee_free_means_zero_rebate(self):
        assert polymarket_maker_rebate(0.50, 100, category="geopolitics") == 0.0

    def test_scales_with_contracts(self):
        one = polymarket_maker_rebate(0.30, 1, category="sports")
        ten = polymarket_maker_rebate(0.30, 10, category="sports")
        assert ten == pytest.approx(10 * one)

    def test_unknown_category_uses_default_taker_rate_and_share(self):
        # Unknown-category fallback is POLYMARKET_DEFAULT_TAKER_RATE (0.04),
        # applied inside polymarket_taker_fee; rebate share 25%.
        expected = 0.25 * polymarket_taker_fee(0.40, 1, category="mystery")
        assert polymarket_maker_rebate(0.40, 1, category="mystery") == pytest.approx(expected)

    def test_extreme_prices_have_zero_rebate(self):
        assert polymarket_maker_rebate(0.0, 1, category="politics") == 0.0
        assert polymarket_maker_rebate(1.0, 1, category="politics") == 0.0


# ---------------------------------------------------------------------------
# kalshi_taker_fee
# ---------------------------------------------------------------------------

class TestKalshiTakerFee:
    def test_per_order_rounding_at_extremes(self):
        # Verified schedule (Feb 5, 2026): round up once per ORDER, no
        # per-contract minimum. price=0.01, 1 contract:
        # ceil(0.07 * 1 * 0.01 * 0.99 * 100) = ceil(0.0693) = 1 cent.
        fee = kalshi_taker_fee(0.01, 1)
        assert fee == pytest.approx(0.01)
        # 100 contracts at 0.05: ceil(0.07 * 100 * 0.0475 * 100) = 34 cents —
        # the old per-contract 2-cent floor would have charged $2.00 (~6x).
        assert kalshi_taker_fee(0.05, 100) == pytest.approx(0.34)

    def test_correct_formula_mid_price(self):
        # price=0.50: ceil(7*0.50*0.50) = ceil(1.75) = 2 cents
        fee = kalshi_taker_fee(0.50, 1)
        assert fee == pytest.approx(0.02)

    def test_higher_price(self):
        # price=0.30: ceil(7*0.30*0.70) = ceil(1.47) = 2 cents
        fee = kalshi_taker_fee(0.30, 1)
        assert fee == pytest.approx(0.02)

    def test_cap_at_175(self):
        # The formula max is at p=0.5 -> 2 cents, never reaches 175 for single contract
        # but verify the cap works with multiple contracts
        fee = kalshi_taker_fee(0.50, 1)
        expected_cents = max(2, math.ceil(7 * 0.50 * 0.50))
        expected_cents = min(expected_cents, 175)
        assert fee == pytest.approx(expected_cents / 100.0)

    def test_multiple_contracts(self):
        # Per-order rounding: 10 contracts at 0.50 ->
        # ceil(0.07 * 10 * 0.25 * 100) = ceil(17.5) = 18 cents.
        # (Per-contract rounding would have given 2 cents x 10 = 20.)
        assert kalshi_taker_fee(0.50, 10) == pytest.approx(0.18)

    def test_zero_price_returns_zero(self):
        assert kalshi_taker_fee(0.0) == 0.0

    def test_one_price_returns_zero(self):
        assert kalshi_taker_fee(1.0) == 0.0

    def test_negative_price_returns_zero(self):
        assert kalshi_taker_fee(-0.1) == 0.0


# ---------------------------------------------------------------------------
# net_profit_binary_internal
# ---------------------------------------------------------------------------

class TestNetProfitBinaryInternal:
    def test_negative_spread(self):
        # 0.55 + 0.50 = 1.05 -> gross = 1.0 - 1.05 = -0.05
        result = net_profit_binary_internal(0.55, 0.50)
        assert result["gross_spread"] == pytest.approx(-0.05)
        assert result["fees"] == 0
        assert result["net_profit"] == pytest.approx(-0.05)

    def test_zero_profit_exact_dollar(self):
        result = net_profit_binary_internal(0.50, 0.50)
        assert result["gross_spread"] == pytest.approx(0.0)
        assert result["fees"] == 0
        assert result["net_profit"] == pytest.approx(0.0)

    def test_positive_profit(self):
        from config import POLYGON_GAS_ESTIMATE
        # yes=0.40, no=0.40 -> total=0.80, gross=0.20
        # March 2026: entry fees on both legs
        # pm_fee_yes = 0.04*0.40*0.60 = 0.0096, pm_fee_no = 0.04*0.40*0.60 = 0.0096
        # total fees = 0.0192 + gas*2
        result = net_profit_binary_internal(0.40, 0.40)
        gas = POLYGON_GAS_ESTIMATE * 2
        expected_fees = polymarket_taker_fee(0.40) + polymarket_taker_fee(0.40)
        assert result["gross_spread"] == pytest.approx(0.20)
        assert result["fees"] == pytest.approx(expected_fees + gas)
        assert result["net_profit"] == pytest.approx(0.20 - expected_fees - gas)

    def test_entry_fees_on_both_legs(self):
        from config import POLYGON_GAS_ESTIMATE
        # yes=0.30, no=0.45 -> total=0.75, gross=0.25
        # March 2026: both legs pay entry fee
        result = net_profit_binary_internal(0.30, 0.45)
        gas = POLYGON_GAS_ESTIMATE * 2
        expected_fees = polymarket_taker_fee(0.30) + polymarket_taker_fee(0.45)
        assert result["fees"] == pytest.approx(expected_fees + gas)

    def test_asymmetric_prices(self):
        from config import POLYGON_GAS_ESTIMATE
        result = net_profit_binary_internal(0.10, 0.80)
        gas = POLYGON_GAS_ESTIMATE * 2
        assert result["gross_spread"] == pytest.approx(0.10)
        expected_fees = polymarket_taker_fee(0.10) + polymarket_taker_fee(0.80)
        assert result["fees"] == pytest.approx(expected_fees + gas)
        assert result["net_profit"] == pytest.approx(0.10 - expected_fees - gas)


# ---------------------------------------------------------------------------
# net_profit_negrisk_internal
# ---------------------------------------------------------------------------

class TestNetProfitNegriskInternal:
    def test_multiple_outcomes_positive(self):
        from config import POLYGON_GAS_ESTIMATE
        # 4 outcomes each at 0.20 -> total=0.80, gross=0.20
        prices = [0.20, 0.20, 0.20, 0.20]
        result = net_profit_negrisk_internal(prices)
        gas = POLYGON_GAS_ESTIMATE * 4
        assert result["gross_spread"] == pytest.approx(0.20)
        # March 2026: each leg pays entry fee
        expected_fees = sum(polymarket_taker_fee(p) for p in prices)
        assert result["fees"] == pytest.approx(expected_fees + gas)
        assert result["net_profit"] == pytest.approx(0.20 - expected_fees - gas)

    def test_no_spread(self):
        prices = [0.25, 0.25, 0.25, 0.25]
        result = net_profit_negrisk_internal(prices)
        assert result["gross_spread"] == pytest.approx(0.0)
        assert result["net_profit"] == pytest.approx(0.0)

    def test_negative_spread(self):
        prices = [0.30, 0.30, 0.30, 0.30]
        result = net_profit_negrisk_internal(prices)
        assert result["gross_spread"] == pytest.approx(-0.20)
        assert result["fees"] == 0

    def test_entry_fees_on_all_legs(self):
        from config import POLYGON_GAS_ESTIMATE
        prices = [0.10, 0.25, 0.30, 0.15]
        result = net_profit_negrisk_internal(prices)
        gas = POLYGON_GAS_ESTIMATE * 4
        # March 2026: sum of entry fees for all legs
        expected_fees = sum(polymarket_taker_fee(p) for p in prices)
        assert result["fees"] == pytest.approx(expected_fees + gas)


# ---------------------------------------------------------------------------
# net_profit_negrisk_no_side
# ---------------------------------------------------------------------------

class TestNetProfitNegriskNoSide:
    def test_profitable_three_outcome(self):
        from config import POLYGON_GAS_ESTIMATE
        # 3 outcomes, buy all NO at 0.50/0.65/0.80 -> sum 1.95 < (N-1)=2, gross 0.05
        no_prices = [0.50, 0.65, 0.80]
        result = net_profit_negrisk_no_side(no_prices)
        gas = POLYGON_GAS_ESTIMATE * 3
        assert result["gross_spread"] == pytest.approx(0.05)
        # March 2026: each NO leg pays entry fee
        expected_fees = sum(polymarket_taker_fee(p) for p in no_prices)
        assert result["fees"] == pytest.approx(expected_fees + gas)
        assert result["net_profit"] == pytest.approx(0.05 - expected_fees - gas)

    def test_payout_is_n_minus_one(self):
        # 4 outcomes all NO at 0.50 -> sum 2.0, payout (N-1)=3 -> gross 1.0
        result = net_profit_negrisk_no_side([0.50, 0.50, 0.50, 0.50])
        assert result["gross_spread"] == pytest.approx(1.0)
        assert result["net_profit"] > 0

    def test_no_edge_at_floor(self):
        # N=2, both NO at 1.0 -> sum 2.0, payout (N-1)=1 -> gross -1.0, fees suppressed
        result = net_profit_negrisk_no_side([1.0, 1.0])
        assert result["gross_spread"] == pytest.approx(-1.0)
        assert result["fees"] == 0

    def test_degenerate_single_outcome(self):
        assert net_profit_negrisk_no_side([0.5]) == {"gross_spread": 0.0, "fees": 0, "net_profit": 0.0}


# ---------------------------------------------------------------------------
# net_profit_kalshi_binary
# ---------------------------------------------------------------------------

class TestNetProfitKalshiBinary:
    def test_positive_spread_with_fees(self):
        # yes=0.40, no=0.40 -> total=0.80, gross=0.20
        result = net_profit_kalshi_binary(0.40, 0.40)
        assert result["gross_spread"] == pytest.approx(0.20)
        expected_fees = kalshi_taker_fee(0.40) + kalshi_taker_fee(0.40)
        assert result["fees"] == pytest.approx(expected_fees)
        assert result["net_profit"] == pytest.approx(0.20 - expected_fees)

    def test_negative_spread(self):
        result = net_profit_kalshi_binary(0.55, 0.50)
        assert result["gross_spread"] == pytest.approx(-0.05)
        assert result["fees"] == 0
        assert result["net_profit"] == pytest.approx(-0.05)

    def test_zero_spread(self):
        result = net_profit_kalshi_binary(0.50, 0.50)
        assert result["gross_spread"] == pytest.approx(0.0)
        assert result["net_profit"] == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# net_profit_kalshi_multi
# ---------------------------------------------------------------------------

class TestNetProfitKalshiMulti:
    def test_multiple_legs_positive(self):
        prices = [0.20, 0.20, 0.20, 0.20]
        result = net_profit_kalshi_multi(prices)
        assert result["gross_spread"] == pytest.approx(0.20)
        expected_fees = sum(kalshi_taker_fee(p) for p in prices)
        assert result["fees"] == pytest.approx(expected_fees)
        assert result["net_profit"] == pytest.approx(0.20 - expected_fees)

    def test_negative_spread(self):
        prices = [0.30, 0.30, 0.30, 0.30]
        result = net_profit_kalshi_multi(prices)
        assert result["gross_spread"] < 0
        assert result["fees"] == 0


# ---------------------------------------------------------------------------
# net_profit_cross_platform
# ---------------------------------------------------------------------------

class TestNetProfitCrossPlatform:
    def test_both_strategies_positive(self):
        # poly=0.30, kalshi=0.30 -> total=0.60, gross=0.40
        result = net_profit_cross_platform(0.30, 0.30, "yes", "no")
        assert result["gross_spread"] == pytest.approx(0.40)
        assert result["net_profit"] > 0

    def test_no_spread(self):
        result = net_profit_cross_platform(0.50, 0.50, "yes", "no")
        assert result["gross_spread"] == pytest.approx(0.0)
        assert result["net_profit"] == pytest.approx(0.0)

    def test_negative_spread(self):
        result = net_profit_cross_platform(0.60, 0.50, "yes", "no")
        assert result["gross_spread"] == pytest.approx(-0.10)
        assert result["net_profit"] == pytest.approx(-0.10)

    def test_fees_are_both_entry_fees(self):
        from config import POLYGON_GAS_ESTIMATE
        # March 2026: both PM and Kalshi pay entry fees (no case distinction)
        poly_price, kalshi_price = 0.30, 0.30
        pm_entry = polymarket_taker_fee(poly_price)
        kalshi_entry = kalshi_taker_fee(kalshi_price, 1)
        expected_fees = pm_entry + kalshi_entry + POLYGON_GAS_ESTIMATE
        result = net_profit_cross_platform(poly_price, kalshi_price, "yes", "no")
        assert result["fees"] == pytest.approx(expected_fees)

    def test_ev_fees_lower_than_worst_case(self):
        """EV fee model should produce lower or equal fees vs worst-case."""
        from fees import _select_fees
        # Case where case1 > case2: EV should be lower than max
        case1, case2, price_a = 0.03, 0.01, 0.40
        ev_fees = _select_fees(case1, case2, price_a)
        worst_fees = max(case1, case2)
        assert ev_fees <= worst_fees

    def test_ev_fees_weight_each_case_by_its_probability(self):
        """Side-A win fees must be weighted by P(A), not P(not A)."""
        from fees import _select_fees

        assert _select_fees(0.03, 0.01, 0.80) == pytest.approx(0.026)


# ---------------------------------------------------------------------------
# betfair_commission
# ---------------------------------------------------------------------------

class TestBetfairCommission:
    def test_default_5_percent(self):
        assert betfair_commission(10.0) == pytest.approx(0.50)

    def test_zero_for_losses(self):
        assert betfair_commission(-5.0) == 0.0
        assert betfair_commission(0.0) == 0.0

    def test_configurable_rate(self):
        assert betfair_commission(10.0, 0.02) == pytest.approx(0.20)
        assert betfair_commission(10.0, 0.10) == pytest.approx(1.00)


# ---------------------------------------------------------------------------
# net_profit_cross_betfair
# ---------------------------------------------------------------------------

class TestNetProfitCrossBetfair:
    def test_positive_spread(self):
        result = net_profit_cross_betfair(0.30, 0.30, "yes", "no")
        assert result["gross_spread"] == pytest.approx(0.40)
        assert result["net_profit"] > 0

    def test_negative_spread(self):
        result = net_profit_cross_betfair(0.60, 0.50, "yes", "no")
        assert result["gross_spread"] < 0
        assert result["fees"] == 0

    @patch("fees.FEE_MODEL", "worst_case")
    def test_worst_case_fees(self):
        from config import POLYGON_GAS_ESTIMATE
        poly_price, bf_price = 0.30, 0.30
        pm_entry_fee = polymarket_taker_fee(poly_price)
        bf_win_fee = betfair_commission(1.0 - bf_price, 0.05)
        # case1 (PM wins): pm_entry only; case2 (BF wins): pm_entry + bf_win_fee
        case1 = pm_entry_fee
        case2 = pm_entry_fee + bf_win_fee
        expected_worst = max(case1, case2) + POLYGON_GAS_ESTIMATE
        result = net_profit_cross_betfair(poly_price, bf_price, "yes", "no")
        assert result["fees"] == pytest.approx(expected_worst)

    def test_custom_commission_rate(self):
        result_default = net_profit_cross_betfair(0.30, 0.30, "yes", "no")
        result_custom = net_profit_cross_betfair(0.30, 0.30, "yes", "no", commission_rate=0.02)
        # Lower commission rate should yield higher net profit
        assert result_custom["net_profit"] >= result_default["net_profit"]


# ---------------------------------------------------------------------------
# Gas fee deduction tests (POLYGON_GAS_ESTIMATE)
# ---------------------------------------------------------------------------

class TestGasFeeDeduction:
    """Verify Polygon gas fees are subtracted from all Polymarket-involving fee functions."""

    def test_binary_internal_includes_gas(self):
        """Binary internal should subtract 2x gas (two PM orders)."""
        from config import POLYGON_GAS_ESTIMATE
        result = net_profit_binary_internal(0.40, 0.40)
        # March 2026: entry fees on both legs
        expected_gas = POLYGON_GAS_ESTIMATE * 2
        expected_pm_fees = polymarket_taker_fee(0.40) + polymarket_taker_fee(0.40)
        assert result["fees"] == pytest.approx(expected_pm_fees + expected_gas)
        assert result["net_profit"] == pytest.approx(0.20 - expected_pm_fees - expected_gas)

    def test_negrisk_internal_includes_gas(self):
        """NegRisk should subtract gas * len(outcomes)."""
        from config import POLYGON_GAS_ESTIMATE
        prices = [0.20, 0.20, 0.20, 0.20]
        result = net_profit_negrisk_internal(prices)
        expected_gas = POLYGON_GAS_ESTIMATE * 4
        expected_pm_fees = sum(polymarket_taker_fee(p) for p in prices)
        assert result["fees"] == pytest.approx(expected_pm_fees + expected_gas)
        assert result["net_profit"] == pytest.approx(0.20 - expected_pm_fees - expected_gas)

    def test_cross_platform_includes_gas(self):
        """Cross-platform (PM vs Kalshi) should subtract 1x gas."""
        from config import POLYGON_GAS_ESTIMATE
        result = net_profit_cross_platform(0.30, 0.30, "yes", "no")
        # Gas should be included in fees
        assert result["fees"] >= POLYGON_GAS_ESTIMATE

    def test_cross_betfair_includes_gas(self):
        """Cross-platform (PM vs Betfair) should subtract 1x gas."""
        from config import POLYGON_GAS_ESTIMATE
        result = net_profit_cross_betfair(0.30, 0.30, "yes", "no")
        assert result["fees"] >= POLYGON_GAS_ESTIMATE

    def test_zero_gas_no_gas_component(self):
        """When POLYGON_GAS_ESTIMATE=0, gas does not inflate fees."""
        with patch("fees.POLYGON_GAS_ESTIMATE", 0.0):
            result = net_profit_binary_internal(0.40, 0.40)
            # Only taker entry fees remain
            expected_fees = polymarket_taker_fee(0.40) + polymarket_taker_fee(0.40)
            assert result["fees"] == pytest.approx(expected_fees)
            assert result["net_profit"] == pytest.approx(0.20 - expected_fees)

    def test_custom_gas_via_env(self):
        """Gas fee should use the configured value."""
        with patch("fees.POLYGON_GAS_ESTIMATE", 0.05):
            result = net_profit_binary_internal(0.40, 0.40)
            pm_fees = polymarket_taker_fee(0.40) + polymarket_taker_fee(0.40)
            # gas=0.05*2=0.10
            assert result["net_profit"] == pytest.approx(0.20 - pm_fees - 0.10)

    def test_kalshi_binary_no_gas(self):
        """Kalshi-only trades should NOT include Polygon gas fees."""
        result = net_profit_kalshi_binary(0.40, 0.40)
        # Only Kalshi taker fees, no gas
        expected_fees = kalshi_taker_fee(0.40) + kalshi_taker_fee(0.40)
        assert result["fees"] == pytest.approx(expected_fees)


# ---------------------------------------------------------------------------
# net_profit_gemini_binary
# ---------------------------------------------------------------------------

class TestGeminiFee:
    def test_symmetric_at_half(self):
        # March 2026: 0.07 * 0.50 * 0.50 = 0.0175 -> ceil(1.75) / 100 = 0.02
        assert gemini_fee(0.50) == pytest.approx(0.02)

    def test_low_price(self):
        # 0.07 * 0.10 * 0.90 = 0.0063 -> ceil(0.63) / 100 = 0.01
        assert gemini_fee(0.10) == pytest.approx(0.01)

    def test_high_price(self):
        # 0.07 * 0.90 * 0.10 = 0.0063 -> ceil(0.63) / 100 = 0.01
        assert gemini_fee(0.90) == pytest.approx(0.01)

    def test_custom_rate(self):
        # 0.40 price with 0.07 rate: 0.07 * 0.40 * 0.60 = 0.0168 -> ceil(1.68) / 100 = 0.02
        assert gemini_fee(0.40) == pytest.approx(0.02)

    def test_custom_rate_maker(self):
        # 0.0175 maker rate, 0.50 price: 0.0175 * 0.25 = 0.004375 -> ceil(0.4375) / 100 = 0.01
        assert gemini_fee(0.50, fee_rate=0.0175) == pytest.approx(0.01)

    def test_boundary_zero(self):
        assert gemini_fee(0.0) == 0.0
        assert gemini_fee(1.0) == 0.0


class TestNetProfitGeminiBinary:
    def test_positive_spread_with_fees(self):
        # 0.40 + 0.40 = 0.80, gross = 0.20
        # March 2026: each leg: 0.07 * 0.40 * 0.60 = 0.0168 -> ceil = 0.02
        # total fees = 0.04
        result = net_profit_gemini_binary(0.40, 0.40)
        expected_fees = gemini_fee(0.40) + gemini_fee(0.40)
        assert result["gross_spread"] == pytest.approx(0.20)
        assert result["fees"] == pytest.approx(expected_fees)
        assert result["net_profit"] == pytest.approx(0.20 - expected_fees)

    def test_negative_spread(self):
        result = net_profit_gemini_binary(0.55, 0.50)
        assert result["gross_spread"] == pytest.approx(-0.05)
        assert result["fees"] == 0
        assert result["net_profit"] == pytest.approx(-0.05)

    def test_configurable_fee_rate(self):
        # With maker fee 0.0175: 0.0175 * 0.40 * 0.60 = 0.0042 -> ceil(0.42) = 0.01
        result = net_profit_gemini_binary(0.40, 0.40, fee_rate=0.0175)
        expected_fees = gemini_fee(0.40, fee_rate=0.0175) + gemini_fee(0.40, fee_rate=0.0175)
        assert result["fees"] == pytest.approx(expected_fees)
        assert result["net_profit"] == pytest.approx(0.20 - expected_fees)

    def test_zero_spread(self):
        result = net_profit_gemini_binary(0.50, 0.50)
        assert result["gross_spread"] == pytest.approx(0.0)
        assert result["net_profit"] == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# net_profit_gemini_multi
# ---------------------------------------------------------------------------

class TestNetProfitGeminiMulti:
    def test_positive_spread_4_outcomes(self):
        prices = [0.20, 0.20, 0.20, 0.20]
        result = net_profit_gemini_multi(prices)
        # Each leg: 0.07 * 0.20 * 0.80 = 0.0112 -> ceil(1.12) / 100 = 0.02
        expected_fees = sum(gemini_fee(p) for p in prices)
        assert result["gross_spread"] == pytest.approx(0.20)
        assert result["fees"] == pytest.approx(expected_fees)
        assert result["net_profit"] == pytest.approx(0.20 - expected_fees)

    def test_negative_spread(self):
        prices = [0.30, 0.30, 0.30, 0.30]
        result = net_profit_gemini_multi(prices)
        assert result["gross_spread"] == pytest.approx(-0.20)
        assert result["fees"] == 0

    def test_configurable_fee_rate(self):
        prices = [0.20, 0.20, 0.20, 0.20]
        # With maker rate 0.0175: 0.0175 * 0.20 * 0.80 = 0.0028 -> ceil(0.28) = 0.01
        result = net_profit_gemini_multi(prices, fee_rate=0.0175)
        expected_fees = sum(gemini_fee(p, fee_rate=0.0175) for p in prices)
        assert result["fees"] == pytest.approx(expected_fees)
        assert result["net_profit"] == pytest.approx(0.20 - expected_fees)


# ---------------------------------------------------------------------------
# net_profit_cross_gemini
# ---------------------------------------------------------------------------

class TestNetProfitCrossGemini:
    def test_positive_spread(self):
        result = net_profit_cross_gemini(0.30, 0.30, "yes", "no")
        assert result["gross_spread"] == pytest.approx(0.40)
        assert result["net_profit"] > 0

    def test_negative_spread(self):
        result = net_profit_cross_gemini(0.60, 0.50, "yes", "no")
        assert result["gross_spread"] == pytest.approx(-0.10)
        assert result["net_profit"] == pytest.approx(-0.10)

    def test_includes_both_entry_fees(self):
        from config import POLYGON_GAS_ESTIMATE
        # March 2026: PM and Gemini both pay entry fees
        result = net_profit_cross_gemini(0.30, 0.30, "yes", "no")
        pm_entry = polymarket_taker_fee(0.30)
        gm_entry = gemini_fee(0.30)
        expected_fees = pm_entry + gm_entry + POLYGON_GAS_ESTIMATE
        assert result["fees"] == pytest.approx(expected_fees)


# ---------------------------------------------------------------------------
# net_profit_ibkr_binary
# ---------------------------------------------------------------------------

class TestNetProfitIBKRBinary:
    def test_positive_spread_zero_fees(self):
        result = net_profit_ibkr_binary(0.40, 0.40)
        assert result["gross_spread"] == pytest.approx(0.20)
        assert result["fees"] == 0
        assert result["net_profit"] == pytest.approx(0.20)

    def test_negative_spread(self):
        result = net_profit_ibkr_binary(0.55, 0.50)
        assert result["gross_spread"] == pytest.approx(-0.05)
        assert result["fees"] == 0
        assert result["net_profit"] == pytest.approx(-0.05)

    def test_profit_equals_gross_spread(self):
        """IBKR has $0.00 fees so net_profit always equals gross_spread."""
        result = net_profit_ibkr_binary(0.30, 0.30)
        assert result["net_profit"] == pytest.approx(result["gross_spread"])


# ---------------------------------------------------------------------------
# net_profit_cross_ibkr
# ---------------------------------------------------------------------------

class TestNetProfitCrossIBKR:
    def test_positive_spread(self):
        result = net_profit_cross_ibkr(0.30, 0.30, "yes", "no")
        assert result["gross_spread"] == pytest.approx(0.40)
        assert result["net_profit"] > 0

    def test_negative_spread(self):
        result = net_profit_cross_ibkr(0.60, 0.50, "yes", "no")
        assert result["gross_spread"] == pytest.approx(-0.10)
        assert result["net_profit"] == pytest.approx(-0.10)

    def test_ibkr_zero_fee_means_only_poly_fees(self):
        from config import POLYGON_GAS_ESTIMATE
        # IBKR fee = 0, only Polymarket fees + gas
        result = net_profit_cross_ibkr(0.30, 0.30, "yes", "no")
        assert result["fees"] >= POLYGON_GAS_ESTIMATE


# ---------------------------------------------------------------------------
# net_profit_cross_generic
# ---------------------------------------------------------------------------

class TestNetProfitCrossGeneric:
    def test_positive_spread_kalshi_betfair(self):
        """Kalshi vs Betfair with profitable spread."""
        result = net_profit_cross_generic(
            0.30, 0.30, "yes", "no", platform_a="kalshi", platform_b="betfair"
        )
        assert result["gross_spread"] == pytest.approx(0.40)
        assert result["net_profit"] > 0
        assert result["fees"] > 0  # Kalshi entry fee + Betfair commission

    def test_negative_spread_returns_negative(self):
        result = net_profit_cross_generic(
            0.60, 0.50, "yes", "no", platform_a="kalshi", platform_b="betfair"
        )
        assert result["gross_spread"] == pytest.approx(-0.10)
        assert result["net_profit"] == pytest.approx(-0.10)

    def test_no_gas_for_non_polymarket_pairs(self):
        """Non-Polymarket pairs should NOT include Polygon gas fees."""
        result = net_profit_cross_generic(
            0.30, 0.30, "yes", "no", platform_a="kalshi", platform_b="sxbet"
        )
        from config import POLYGON_GAS_ESTIMATE
        # Compute expected fees without gas
        kalshi_entry = kalshi_taker_fee(0.30)
        # SX Bet: 0% fees. Kalshi: entry fee only, no win fee.
        # Case A wins: Kalshi win fee (0) + entry fees
        # Case B wins: SX Bet win fee (0) + entry fees
        # Both cases = entry fees only = kalshi_entry
        expected = kalshi_entry
        assert result["fees"] == pytest.approx(expected)
        # Verify no gas component
        assert POLYGON_GAS_ESTIMATE not in [0.0]  # gas > 0
        # fees should not include gas
        assert result["fees"] < result["gross_spread"]

    def test_includes_gas_when_polymarket_involved(self):
        """Polymarket pair should include Polygon gas."""
        from config import POLYGON_GAS_ESTIMATE
        result = net_profit_cross_generic(
            0.30, 0.30, "yes", "no", platform_a="polymarket", platform_b="kalshi"
        )
        assert result["fees"] >= POLYGON_GAS_ESTIMATE

    def test_kalshi_entry_fees_both_sides(self):
        """When both platforms are entry-fee platforms, both fees count."""
        result = net_profit_cross_generic(
            0.40, 0.40, "yes", "no", platform_a="kalshi", platform_b="gemini"
        )
        kalshi_entry = kalshi_taker_fee(0.40)
        gm_entry = gemini_fee(0.40)
        # Entry fees should include both platforms
        assert result["fees"] >= kalshi_entry + gm_entry

    def test_zero_fee_platforms(self):
        """SX Bet vs Matchbook — both 0% fee platforms."""
        result = net_profit_cross_generic(
            0.30, 0.30, "yes", "no", platform_a="sxbet", platform_b="matchbook"
        )
        assert result["gross_spread"] == pytest.approx(0.40)
        assert result["fees"] == 0.0
        assert result["net_profit"] == pytest.approx(0.40)

    @patch("fees.FEE_MODEL", "worst_case")
    def test_betfair_win_fee(self):
        """Betfair vs SX Bet — Betfair charges commission when its side wins."""
        result = net_profit_cross_generic(
            0.30, 0.30, "yes", "no", platform_a="betfair", platform_b="sxbet"
        )
        # When Betfair wins: commission on (1.0 - 0.30) = 5% of 0.70 = 0.035
        bf_win_fee = betfair_commission(1.0 - 0.30)
        assert result["fees"] == pytest.approx(bf_win_fee)

    @patch("fees.FEE_MODEL", "worst_case")
    def test_smarkets_win_fee(self):
        """Smarkets vs IBKR — Smarkets charges 2% when its side wins."""
        result = net_profit_cross_generic(
            0.30, 0.30, "yes", "no", platform_a="smarkets", platform_b="ibkr"
        )
        sm_win_fee = smarkets_commission(1.0 - 0.30)
        assert result["fees"] == pytest.approx(sm_win_fee)

    def test_equal_to_one_returns_zero(self):
        result = net_profit_cross_generic(
            0.50, 0.50, "yes", "no", platform_a="kalshi", platform_b="betfair"
        )
        assert result["gross_spread"] == pytest.approx(0.0)
        assert result["net_profit"] == pytest.approx(0.0)

    def test_exceeds_one_returns_negative(self):
        result = net_profit_cross_generic(
            0.60, 0.50, "yes", "no", platform_a="smarkets", platform_b="betfair"
        )
        assert result["net_profit"] < 0


# ---------------------------------------------------------------------------
# polymarket_taker_fee (2026 dynamic taker model)
# ---------------------------------------------------------------------------

class TestPolymarketTakerFee:
    """Tests for the 2026 Polymarket dynamic taker fee: rate * C * P * (1-P)."""

    def test_symmetric_at_half(self):
        # 0.04 * 1 * 0.50 * 0.50 = 0.01
        assert polymarket_taker_fee(0.50) == pytest.approx(0.01)

    def test_asymmetric_price(self):
        # 0.04 * 0.45 * 0.55 = 0.0099
        assert polymarket_taker_fee(0.45) == pytest.approx(0.0099)

    def test_boundary_zero_price(self):
        # price at 0 -> returns 0.0
        assert polymarket_taker_fee(0.0) == 0.0

    def test_boundary_one_price(self):
        # price at 1 -> returns 0.0
        assert polymarket_taker_fee(1.0) == 0.0

    def test_crypto_category_rate(self):
        # Crypto markets use 0.072 rate
        assert polymarket_taker_fee(0.50, 1, 0.072) == pytest.approx(0.072 * 0.25)

    def test_zero_rate_fee_free(self):
        # Geopolitical markets: fee-free
        assert polymarket_taker_fee(0.50, 1, 0.0) == 0.0

    def test_multiple_contracts(self):
        # 0.04 * 10 * 0.50 * 0.50 = 0.10
        assert polymarket_taker_fee(0.50, 10) == pytest.approx(0.10)

    def test_default_rate_is_004(self):
        # Default rate should be 0.04 (4%)
        from config import POLYMARKET_DEFAULT_TAKER_RATE
        assert POLYMARKET_DEFAULT_TAKER_RATE == 0.04
        assert polymarket_taker_fee(0.50) == pytest.approx(POLYMARKET_DEFAULT_TAKER_RATE * 0.25)


# ---------------------------------------------------------------------------
# kalshi_maker_fee
# ---------------------------------------------------------------------------

class TestKalshiMakerFee:
    """Kalshi maker fee per the verified schedule (effective Feb 5, 2026):
    $0 on most markets; ceil(KALSHI_MAKER_MULTIPLIER * P * (1-P)) per
    contract ONLY on flagged series (KALSHI_MAKER_FEE_SERIES)."""

    FLAGGED = "KXCPI-26JUN"  # KXCPI is in the default flagged-series list

    def test_default_is_free_without_ticker(self):
        # Most Kalshi markets charge makers nothing — and callers that don't
        # pass a ticker must get the schedule's default ($0), not a charge.
        assert kalshi_maker_fee(0.50) == 0.0

    def test_unflagged_ticker_is_free(self):
        assert kalshi_maker_fee(0.50, ticker="KXEPLSPREAD-26MAY19") == 0.0

    def test_flagged_symmetric_at_half(self):
        # ceil(1.75 * 0.50 * 0.50) = ceil(0.4375) = 1 -> max(1, 1) = 1 cent
        assert kalshi_maker_fee(0.50, ticker=self.FLAGGED) == pytest.approx(0.01)

    def test_flagged_multiple_contracts(self):
        # Per-order rounding: ceil(1.75 * 10 * 0.25) = ceil(4.375) = 5 cents
        assert kalshi_maker_fee(0.50, 10, ticker=self.FLAGGED) == pytest.approx(0.05)

    def test_zero_price_returns_zero(self):
        assert kalshi_maker_fee(0.0, ticker=self.FLAGGED) == 0.0

    def test_one_price_returns_zero(self):
        assert kalshi_maker_fee(1.0, ticker=self.FLAGGED) == 0.0

    def test_flagged_minimum_one_cent(self):
        # At a very low probability, formula yields < 1 cent, so min(1, ...) kicks in
        # price=0.01: 1.75 * 0.01 * 0.99 = 0.017325 -> ceil = 1 cent
        fee = kalshi_maker_fee(0.01, 1, ticker=self.FLAGGED)
        assert fee == pytest.approx(0.01)

    def test_flagged_cap_enforced(self):
        # Verify cap is applied (KALSHI_FEE_CAP_CENTS = 175)
        from config import KALSHI_FEE_CAP_CENTS, KALSHI_MAKER_MULTIPLIER
        import math as _math
        fee_cents = max(1, _math.ceil(KALSHI_MAKER_MULTIPLIER * 0.50 * 0.50))
        fee_cents = min(fee_cents, KALSHI_FEE_CAP_CENTS)
        assert kalshi_maker_fee(0.50, 1, ticker=self.FLAGGED) == pytest.approx(fee_cents / 100.0)


class TestKalshiIndexTakerFee:
    """S&P 500 / Nasdaq-100 series use a halved taker coefficient (0.035)."""

    def test_index_series_halved(self):
        from fees import kalshi_taker_fee
        # Standard at 0.50 x100: ceil(0.07*100*0.25*100) = 175 cents = $1.75
        # Index at 0.50 x100:    ceil(0.035*100*0.25*100) = 88 cents = $0.88
        assert kalshi_taker_fee(0.50, 100) == pytest.approx(1.75)
        assert kalshi_taker_fee(0.50, 100, ticker="INX-26JUN10") == pytest.approx(0.88)

    def test_nasdaq_prefix_matches(self):
        from fees import kalshi_taker_fee
        assert kalshi_taker_fee(0.50, 100, ticker="NASDAQ100-26JUN") < kalshi_taker_fee(0.50, 100)

    def test_non_index_unchanged(self):
        from fees import kalshi_taker_fee
        assert kalshi_taker_fee(0.50, 100, ticker="KXCPI-26JUN") == kalshi_taker_fee(0.50, 100)


class TestPolymarketCategoryRates:
    """Category-based Polymarket taker rates, verified 2026-06-10."""

    def test_geopolitics_is_free(self):
        from fees import polymarket_taker_fee
        assert polymarket_taker_fee(0.50, category="geopolitics") == 0.0

    def test_crypto_highest(self):
        from fees import polymarket_taker_fee
        assert polymarket_taker_fee(0.50, category="crypto") == pytest.approx(0.07 * 0.25)

    def test_sports_lowest_nonzero(self):
        from fees import polymarket_taker_fee
        assert polymarket_taker_fee(0.50, category="Sports") == pytest.approx(0.03 * 0.25)

    def test_unknown_category_uses_default(self):
        from fees import polymarket_taker_fee
        from config import POLYMARKET_DEFAULT_TAKER_RATE
        assert polymarket_taker_fee(0.50, category="mystery") == pytest.approx(
            POLYMARKET_DEFAULT_TAKER_RATE * 0.25)

    def test_explicit_rate_wins_over_category(self):
        from fees import polymarket_taker_fee
        assert polymarket_taker_fee(0.50, fee_rate=0.10, category="geopolitics") == pytest.approx(0.025)

    def test_binary_internal_geopolitics_feeless(self):
        from fees import net_profit_binary_internal
        from config import POLYGON_GAS_ESTIMATE
        result = net_profit_binary_internal(0.45, 0.50, category="geopolitics")
        # Only gas remains as cost
        assert result["fees"] == pytest.approx(POLYGON_GAS_ESTIMATE * 2)

    def test_negrisk_internal_category_changes_profit(self):
        from fees import net_profit_negrisk_internal
        cheap = net_profit_negrisk_internal([0.30, 0.30, 0.30], category="geopolitics")
        dear = net_profit_negrisk_internal([0.30, 0.30, 0.30], category="crypto")
        assert cheap["net_profit"] > dear["net_profit"]


# ---------------------------------------------------------------------------
# PLATFORM_FEE_SCHEDULE — all 8 platforms (per D-06, CI enforcement)
# ---------------------------------------------------------------------------

class TestPlatformFeeSchedule:
    """Codify correct 2026 fee rates for all 8 platforms (per D-06).

    CI enforcement: if any platform changes fees, these tests break and force
    an explicit update to both PLATFORM_FEE_SCHEDULE and these tests.
    """

    def test_polymarket_fees(self):
        assert PLATFORM_FEE_SCHEDULE["polymarket"]["taker"] == 0.04
        assert PLATFORM_FEE_SCHEDULE["polymarket"]["maker"] == 0.00

    def test_kalshi_fees(self):
        assert PLATFORM_FEE_SCHEDULE["kalshi"]["taker"] == 0.07
        assert PLATFORM_FEE_SCHEDULE["kalshi"]["maker"] == 0.0175

    def test_betfair_fees(self):
        assert PLATFORM_FEE_SCHEDULE["betfair"]["taker"] == 0.05
        assert PLATFORM_FEE_SCHEDULE["betfair"]["maker"] == 0.05

    def test_smarkets_fees(self):
        assert PLATFORM_FEE_SCHEDULE["smarkets"]["taker"] == 0.02
        assert PLATFORM_FEE_SCHEDULE["smarkets"]["maker"] == 0.02

    def test_sxbet_fees(self):
        assert PLATFORM_FEE_SCHEDULE["sxbet"]["taker"] == 0.00
        assert PLATFORM_FEE_SCHEDULE["sxbet"]["maker"] == 0.00

    def test_matchbook_fees(self):
        assert PLATFORM_FEE_SCHEDULE["matchbook"]["taker"] == 0.00
        assert PLATFORM_FEE_SCHEDULE["matchbook"]["maker"] == 0.00

    def test_gemini_fees(self):
        assert PLATFORM_FEE_SCHEDULE["gemini"]["taker"] == 0.07
        assert PLATFORM_FEE_SCHEDULE["gemini"]["maker"] == 0.0175

    def test_ibkr_fees(self):
        assert PLATFORM_FEE_SCHEDULE["ibkr"]["taker"] == 0.00
        assert PLATFORM_FEE_SCHEDULE["ibkr"]["maker"] == 0.00

    def test_all_eight_platforms_present(self):
        expected = {"polymarket", "kalshi", "betfair", "smarkets", "sxbet", "matchbook", "gemini", "ibkr"}
        assert set(PLATFORM_FEE_SCHEDULE.keys()) == expected


# ---------------------------------------------------------------------------
# _CROSS_FEE_FUNCS coverage
# ---------------------------------------------------------------------------

class TestCrossFeeFuncsMap:
    def test_all_28_pairs_registered(self):
        """All C(8,2) = 28 platform pairs should have fee functions."""
        from scans.cross import _CROSS_FEE_FUNCS
        assert len(_CROSS_FEE_FUNCS) == 28

    def test_polymarket_pairs_use_dedicated_functions(self):
        """Polymarket pairs should use their hand-tuned implementations."""
        from scans.cross import _CROSS_FEE_FUNCS
        assert _CROSS_FEE_FUNCS[("polymarket", "kalshi")] is net_profit_cross_platform
        assert _CROSS_FEE_FUNCS[("polymarket", "betfair")] is net_profit_cross_betfair
        assert _CROSS_FEE_FUNCS[("polymarket", "gemini")] is net_profit_cross_gemini
        assert _CROSS_FEE_FUNCS[("polymarket", "ibkr")] is net_profit_cross_ibkr

    def test_non_polymarket_pairs_are_callable(self):
        """All non-Polymarket pairs should be callable with 4 args."""
        from scans.cross import _CROSS_FEE_FUNCS
        non_pm = {k: v for k, v in _CROSS_FEE_FUNCS.items()
                  if "polymarket" not in k}
        assert len(non_pm) == 21  # C(7,2) = 21
        for (pa, pb), fn in non_pm.items():
            result = fn(0.40, 0.40, "yes", "no")
            assert "net_profit" in result, f"Missing net_profit for {pa}-{pb}"
            assert "fees" in result, f"Missing fees for {pa}-{pb}"
            assert result["net_profit"] > 0, f"Expected profit for {pa}-{pb} at 0.40+0.40"

    def test_symmetric_lookup(self):
        """Both (a,b) and reversed lookup should find the same pair."""
        from scans.cross import _CROSS_FEE_FUNCS
        # kalshi-betfair should be findable
        key = ("kalshi", "betfair")
        assert key in _CROSS_FEE_FUNCS or ("betfair", "kalshi") in _CROSS_FEE_FUNCS


# ---------------------------------------------------------------------------
# Regression tests for the directional-strategy fee bug:
# the buggy formulas computed `fee_per_contract * size_in_dollars`, which is
# off by a factor of `price`. At size=$1 (1 contract) the bug is invisible,
# but at realistic sizes ($10+) it underestimates the fee significantly.
# ---------------------------------------------------------------------------

class TestDirectionalStrategyFeesScaleWithContracts:
    def test_imbalance_polymarket_fee_scales_with_contracts(self):
        """A $20 trade at price 0.50 buys 40 contracts; fee must reflect 40
        contracts, not 1. Buggy code computed `fee(P,1) * size` = ~$0.10,
        correct is ~$0.40."""
        from fees import net_profit_imbalance, polymarket_taker_fee
        # entry 0.50, exit 0.55, size=$20 → 40 contracts
        result = net_profit_imbalance(0.50, 0.55, 20.0, "polymarket")
        # gross = 20 * 0.05 = $1.00
        # entry_fee = 0.04 * 40 * 0.5 * 0.5 = $0.40
        # exit_fee  = 0.04 * 40 * 0.55 * 0.45 = $0.396
        # gas      = ~negligible
        # net      ≈ 1.00 - 0.40 - 0.396 = ~$0.20
        # Buggy net would have been ≈ 1.00 - 0.01 - 0.0099 = ~$0.98
        assert result < 0.5, f"Imbalance net ${result:.3f} too high — fee likely under-counted"
        assert result > 0.0
        # Sanity: fee for 40 contracts at 0.50 is exactly 4x larger than 10 contracts at 0.50
        assert polymarket_taker_fee(0.50, 40) == pytest.approx(0.04 * 40 * 0.25)

    def test_imbalance_gemini_uses_canonical_formula(self):
        """Gemini imbalance must use the P*(1-P) formula, not min(P, 1-P).
        At P=0.5 they differ by 2x."""
        from fees import net_profit_imbalance, gemini_fee
        # entry 0.50, exit 0.50 (no price change → loss = pure fees)
        result = net_profit_imbalance(0.50, 0.50, 20.0, "gemini")
        # contracts = 40
        # Canonical fee per contract at 0.50: rate * 0.5 * 0.5, ceiled to cent
        # Buggy fee per dollar at 0.50: min(0.5, 0.5) * rate = rate * 0.5
        # Result must match canonical, not buggy
        expected_entry_fee = gemini_fee(0.50, contracts=40)
        expected_exit_fee = gemini_fee(0.50, contracts=40)
        expected = 0.0 - expected_entry_fee - expected_exit_fee
        assert result == pytest.approx(expected, abs=0.01)

    def test_news_snipe_polymarket_fee_scales_with_contracts(self):
        from fees import net_profit_news_snipe
        # $50 at 0.40 → 125 contracts. Fee should be ~125x single-contract.
        result_size_50 = net_profit_news_snipe(0.40, 0.40, 50.0, "polymarket")
        result_size_1 = net_profit_news_snipe(0.40, 0.40, 1.0, "polymarket")
        # Pure fee scenario (no price change). Loss must scale roughly linearly with size.
        # If size_50 is only ~5x size_1 instead of ~50x, the fee bug is back.
        ratio = result_size_50 / result_size_1
        assert 30 < ratio < 80, f"Fee should scale ~50x with size, got {ratio}x"

    def test_correlated_polymarket_fee_scales_with_contracts(self):
        from fees import net_profit_correlated
        # Pure fee scenario: no convergence, both legs flat.
        result_50 = net_profit_correlated(0.40, 0.40, 0.60, 0.60, 50.0, "polymarket", "polymarket")
        result_1 = net_profit_correlated(0.40, 0.40, 0.60, 0.60, 1.0, "polymarket", "polymarket")
        ratio = result_50 / result_1
        # Both losses, ratio of 50.0 / 1.0 trade should be near 50
        assert 30 < ratio < 80, f"Correlated fees should scale ~50x with size, got {ratio}x"

    def test_time_decay_polymarket_fee_scales_with_contracts(self):
        from fees import net_profit_time_decay
        # Buy 0.90, settle 1.0, with size $50 → 55 contracts
        result_50 = net_profit_time_decay(0.90, 1.0, 50.0, "polymarket")
        result_1 = net_profit_time_decay(0.90, 1.0, 1.0, "polymarket")
        # Both should be positive (winning bet at 0.90 settling at 1.0).
        # Net should scale roughly linearly with size.
        ratio = result_50 / result_1
        assert 40 < ratio < 60, f"Time decay net should scale ~50x with size, got {ratio}x"
