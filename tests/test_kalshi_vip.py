"""Unit tests for Kalshi VIP volume tracking (kalshi_vip.py)."""

import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from kalshi_vip import (  # noqa: E402
    VIP_PER_CONTRACT_CAP,
    KalshiVipTracker,
    count_eligible_contracts,
    estimate_vip_reward,
    fill_price_dollars,
    is_eligible_fill,
)


def _fill(yes_price=None, no_price=None, count=10):
    f = {'count': count}
    if yes_price is not None:
        f['yes_price'] = yes_price
    if no_price is not None:
        f['no_price'] = no_price
    return f


class TestFillPrice:
    def test_yes_price_cents_to_dollars(self):
        assert fill_price_dollars(_fill(yes_price=50)) == pytest.approx(0.50)

    def test_no_price_converted_to_yes(self):
        assert fill_price_dollars(_fill(no_price=95)) == pytest.approx(0.05)

    def test_missing_price_returns_none(self):
        assert fill_price_dollars({'count': 5}) is None

    def test_non_numeric_price_skipped(self):
        assert fill_price_dollars({'yes_price': 'bad'}) is None


class TestEligibility:
    def test_mid_price_eligible(self):
        assert is_eligible_fill(_fill(yes_price=50)) is True

    def test_three_cents_eligible_boundary(self):
        assert is_eligible_fill(_fill(yes_price=3)) is True

    def test_ninety_seven_cents_eligible_boundary(self):
        assert is_eligible_fill(_fill(yes_price=97)) is True

    def test_one_cent_ineligible(self):
        assert is_eligible_fill(_fill(yes_price=1)) is False

    def test_ninety_nine_cents_ineligible(self):
        assert is_eligible_fill(_fill(yes_price=99)) is False


class TestCounting:
    def test_counts_only_eligible(self):
        fills = [
            _fill(yes_price=50, count=10),   # eligible
            _fill(yes_price=2, count=20),     # ineligible (too cheap)
            _fill(yes_price=98, count=30),    # ineligible (too rich)
            _fill(yes_price=40, count=5),     # eligible
        ]
        assert count_eligible_contracts(fills) == 15

    def test_bad_count_treated_as_zero(self):
        fills = [_fill(yes_price=50, count='oops')]
        assert count_eligible_contracts(fills) == 0

    def test_negative_count_clamped_to_zero(self):
        # A negative count must never drag the total below zero.
        fills = [_fill(yes_price=50, count=-30), _fill(yes_price=50, count=10)]
        assert count_eligible_contracts(fills) == 10


class TestRewardEstimate:
    def test_capped_at_half_cent_per_contract(self):
        # Huge pool, tiny total: pro-rata would exceed cap, so cap binds.
        reward = estimate_vip_reward(your_contracts=100, total_contracts=100, reward_pool=1000.0)
        assert reward == pytest.approx(100 * VIP_PER_CONTRACT_CAP)

    def test_pro_rata_binds_when_below_cap(self):
        # 10% of a $1 pool = $0.10; cap on 100 contracts = $0.50, so pro-rata binds.
        reward = estimate_vip_reward(your_contracts=100, total_contracts=1000, reward_pool=1.0)
        assert reward == pytest.approx(0.10)

    def test_zero_inputs(self):
        assert estimate_vip_reward(0, 100, 50.0) == 0.0
        assert estimate_vip_reward(100, 0, 50.0) == 0.0
        assert estimate_vip_reward(100, 100, 0.0) == 0.0


class TestKalshiVipTracker:
    def test_summarize_fills_splits_eligible(self):
        tracker = KalshiVipTracker()
        fills = [
            _fill(yes_price=50, count=10),
            _fill(yes_price=1, count=20),
        ]
        summary = tracker.summarize_fills(fills)
        assert summary['eligible_contracts'] == 10
        assert summary['ineligible_contracts'] == 20
        assert summary['total_fills'] == 2
        assert summary['reward_cap_usd'] == pytest.approx(10 * VIP_PER_CONTRACT_CAP)

    def test_summarize_since_pulls_from_client(self):
        client = MagicMock()
        client.get_fills.return_value = [_fill(yes_price=50, count=40)]
        tracker = KalshiVipTracker(kalshi_client=client)
        summary = tracker.summarize_since(min_ts=123)
        client.get_fills.assert_called_once_with(min_ts=123)
        assert summary['eligible_contracts'] == 40

    def test_summarize_since_without_client_raises(self):
        tracker = KalshiVipTracker()
        with pytest.raises(RuntimeError):
            tracker.summarize_since()
