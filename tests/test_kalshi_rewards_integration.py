"""Integration tests for Kalshi LIP + VIP reward tracking.

Exercises the two reward engines together against Kalshi-API-shaped data and
locks the continuous-mode VIP poll-throttle contract, all with mocked clients
(no network, no auth).
"""

import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from kalshi_lip import KalshiLipScorer  # noqa: E402
from kalshi_vip import KalshiVipTracker  # noqa: E402


class TestVipEndToEnd:
    def test_summarize_since_with_api_shaped_fills(self):
        # Kalshi /portfolio/fills-shaped records: integer-cent prices + count.
        client = MagicMock()
        client.get_fills.return_value = [
            {'ticker': 'KXBTC-26', 'side': 'yes', 'action': 'buy', 'count': 50, 'yes_price': 50, 'no_price': 50},
            {'ticker': 'KXBTC-26', 'side': 'no', 'action': 'sell', 'count': 30, 'yes_price': 4, 'no_price': 96},
            {'ticker': 'KXETH-26', 'side': 'yes', 'action': 'buy', 'count': 25, 'yes_price': 1, 'no_price': 99},
        ]
        tracker = KalshiVipTracker(client)
        summary = tracker.summarize_since(min_ts=1_700_000_000)

        # 50 (price 0.50) + 30 (price 0.04) eligible; 25 (price 0.01) ineligible.
        assert summary['eligible_contracts'] == 80
        assert summary['ineligible_contracts'] == 25
        assert summary['reward_cap_usd'] == pytest.approx(80 * 0.005)
        client.get_fills.assert_called_once_with(min_ts=1_700_000_000)

    def test_empty_fills_yields_zero(self):
        client = MagicMock()
        client.get_fills.return_value = []
        tracker = KalshiVipTracker(client)
        summary = tracker.summarize_since()
        assert summary == {
            'eligible_contracts': 0,
            'ineligible_contracts': 0,
            'total_fills': 0,
            'reward_cap_usd': 0.0,
        }


class TestVipPollThrottle:
    """Replicates the continuous.py VIP poll-throttle contract."""

    def _poll_if_due(self, tracker, now_ts, interval):
        if now_ts - tracker.last_poll_ts >= interval:
            tracker.last_poll_ts = now_ts
            tracker.summarize_since()
            return True
        return False

    def test_tracker_exposes_public_last_poll_ts(self):
        tracker = KalshiVipTracker(MagicMock())
        assert tracker.last_poll_ts == 0.0

    def test_throttle_skips_within_interval_and_fires_after(self):
        client = MagicMock()
        client.get_fills.return_value = []
        tracker = KalshiVipTracker(client)
        interval = 1800
        base = 1_700_000_000.0

        # First poll: last_poll is 0, so the window has long elapsed -> fire.
        assert self._poll_if_due(tracker, now_ts=base, interval=interval) is True
        # 1500s later, still within the 1800s window -> skip.
        assert self._poll_if_due(tracker, now_ts=base + 1500, interval=interval) is False
        # Past the window -> fire again.
        assert self._poll_if_due(tracker, now_ts=base + 1900, interval=interval) is True
        assert client.get_fills.call_count == 2


class TestLipScoringOverPeriod:
    def test_accumulates_snapshots_and_estimates_reward(self):
        # Best bid 0.50, we rest 200 contracts at best for 60 one-second snapshots.
        scorer = KalshiLipScorer('KXBTC-26', target_size=200, discount_factor=0.75)
        orders = [{'side': 'bid', 'price': 0.50, 'size': 200}]
        for _ in range(60):
            scorer.record_snapshot(orders, best_bid=0.50, best_ask=None)

        assert scorer.snapshot_count == 60
        # 200 size * 1.0 multiplier * 60 snapshots.
        assert scorer.accumulated_score == pytest.approx(200 * 60)
        # We hold 25% of total market score in a $40 pool -> $10.
        reward = scorer.estimate_reward(reward_pool=40.0, total_market_score=scorer.accumulated_score * 4)
        assert reward == pytest.approx(10.0)

    def test_orders_off_best_score_less(self):
        scorer_best = KalshiLipScorer('M', target_size=100, discount_factor=0.5)
        scorer_off = KalshiLipScorer('M', target_size=100, discount_factor=0.5)
        scorer_best.record_snapshot([{'side': 'bid', 'price': 0.50, 'size': 100}], 0.50, None)
        scorer_off.record_snapshot([{'side': 'bid', 'price': 0.47, 'size': 100}], 0.50, None)
        assert scorer_off.accumulated_score < scorer_best.accumulated_score
        # 3 ticks at df=0.5 -> 0.5**3 = 0.125 multiplier.
        assert scorer_off.accumulated_score == pytest.approx(100 * 0.125)
