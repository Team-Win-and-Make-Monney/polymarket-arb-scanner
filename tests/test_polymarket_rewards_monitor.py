"""Tests for the read-only Polymarket rewards monitor."""

from __future__ import annotations

import datetime as dt
import os
import sys
import urllib.error
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts import polymarket_rewards_monitor as monitor


# ---------------------------------------------------------------------------
# TestPolymarketRewardsMonitor
# ---------------------------------------------------------------------------

class TestPolymarketRewardsMonitor:
    """Unit tests for read-only Polymarket US rewards monitor."""

    def test_docs_only_candidates_are_manual_gated(self):
        candidates = monitor.build_candidates([])

        assert any(row["program"] == "Liquidity Incentive Program" for row in candidates)
        assert all(row["platform"] == "Polymarket US" for row in candidates)
        assert {row["required_action_type"] for row in candidates} >= {"order_place", "trade"}
        assert all(row["legal_tos_status"] == "not_reviewed" for row in candidates)

    def test_market_to_candidate_extracts_reward_fields(self):
        row = {
            "slug": "will-test-market-resolve-yes",
            "question": "Will test market resolve Yes?",
            "category": "sports",
            "volumeNum": "12000",
            "liquidityNum": "6000",
            "rewardsMinSize": "2500",
            "minimumTradeQty": "0.01",
            "orderPriceMinTickSize": "0.005",
            "bestBid": {"value": "0.49", "currency": "USD"},
            "bestAsk": {"value": "0.51", "currency": "USD"},
        }

        candidate = monitor.market_to_candidate(row)

        assert candidate["market_slug"] == "will-test-market-resolve-yes"
        assert candidate["max_capital_usd"] == 2500
        assert candidate["validation_status"] == "public_api_observed"
        assert candidate["best_bid"] == "0.49"
        assert candidate["best_ask"] == "0.51"

    def test_fetch_markets_returns_blocker_on_gateway_failure(self, monkeypatch):
        def fail(*args, **kwargs):
            raise urllib.error.URLError("access denied")

        monkeypatch.setattr(monitor, "_fetch_json", fail)

        markets, error = monitor.fetch_markets(limit=1)

        assert markets == []
        assert "access denied" in error

    def test_render_digest_records_gateway_error(self):
        now = dt.datetime(2026, 7, 3, tzinfo=dt.timezone.utc)
        digest = monitor.render_digest(monitor.build_candidates([]), now, "URLError: access denied", 3)

        assert "Polymarket Rewards Read-Only Digest" in digest
        assert "Gateway fetch failed" in digest
        assert "No Polymarket credentials" in digest
