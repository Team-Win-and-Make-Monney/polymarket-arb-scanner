"""Tests for the read-only Kalshi rewards monitor."""

import datetime as dt
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts import kalshi_rewards_monitor as monitor


class TestKalshiRewardsMonitor:
    def test_period_reward_dollars(self):
        """Kalshi period_reward values are rendered in dollars."""
        assert monitor.period_reward_dollars(3000000) == pytest.approx(300.0)
        assert monitor.period_reward_dollars("3333333") == pytest.approx(333.3333)
        assert monitor.period_reward_dollars(None) == 0.0

    def test_group_key_heuristics(self):
        """Grouping keeps one-off markets intact and groups repeated collections."""
        base_counts = {
            "KXBILLS": 26,
            "KXTRUMPPHOTO": 1,
            "KXLIUSACOUPLE": 42,
        }
        assert monitor._group_key("KXBILLS-ROTOR", base_counts) == "KXBILLS"
        assert monitor._group_key("KXTRUMPPHOTO-26JUN14", base_counts) == "KXTRUMPPHOTO-26JUN14"
        assert monitor._group_key("KXLIUSACOUPLE-26AUG31-ABC", base_counts) == "KXLIUSACOUPLE-26AUG31"

    def test_summarize_incentives_scores_small_target_candidate(self):
        """Small-target single markets should surface as manual-review candidates."""
        now = dt.datetime(2026, 6, 13, 12, 0, tzinfo=dt.timezone.utc)
        incentives = [
            {
                "market_ticker": "KXTRUMPPHOTO-26JUN14",
                "period_reward": 10000000,
                "target_size_fp": "300.00",
                "end_date": "2026-06-15T14:00:00Z",
                "incentive_description": "",
            },
            {
                "market_ticker": "KXBILLS-ROTOR",
                "period_reward": 3000000,
                "target_size_fp": "1000.00",
                "end_date": "2026-07-09T03:59:00Z",
                "incentive_description": "",
            },
            {
                "market_ticker": "KXBILLS-DATA",
                "period_reward": 3000000,
                "target_size_fp": "1000.00",
                "end_date": "2026-07-09T03:59:00Z",
                "incentive_description": "",
            },
            {
                "market_ticker": "KXBILLS-SURF",
                "period_reward": 3000000,
                "target_size_fp": "1000.00",
                "end_date": "2026-07-09T03:59:00Z",
                "incentive_description": "",
            },
        ]

        summaries = monitor.summarize_incentives(incentives, now)
        assert summaries[0]["group_key"] == "KXTRUMPPHOTO-26JUN14"
        assert summaries[0]["avg_target"] == pytest.approx(300.0)
        assert summaries[0]["competition_proxy"] == "High bounty"
        assert "resting limit orders" in summaries[0]["required_action"]
        assert summaries[0]["automation_mode"].startswith("Read-only")

    def test_write_csv_includes_requirement_fields(self, tmp_path):
        """Full output should explain what each reward pool requires."""
        now = dt.datetime(2026, 6, 13, 12, 0, tzinfo=dt.timezone.utc)
        incentives = [{
            "market_ticker": "KXEOWEEK-26JUN13-1",
            "period_reward": 3333333,
            "target_size_fp": "300.00",
            "end_date": "2026-06-27T14:00:00Z",
            "incentive_type": "liquidity",
            "incentive_description": "",
        }]
        summaries = monitor.summarize_incentives(incentives, now)
        output = tmp_path / "rewards.csv"

        monitor.write_csv(summaries, output)

        text = output.read_text(encoding="utf-8")
        assert "required_action" in text
        assert "Post qualifying resting limit orders" in text
        assert "live orders require human approval" in text
