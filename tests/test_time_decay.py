"""Tests for scans/time_decay.py — time decay convergence detection."""

import sys
import os
import pytest
from unittest.mock import patch, MagicMock
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# scans.time_decay does not import signal_aggregator at module level — it
# only receives it as a function parameter — so no module mocking is needed.
# Previously this file injected `sys.modules["signal_aggregator"] = MagicMock()`,
# which polluted sys.modules for any later test that did
# `from signal_aggregator import SignalAggregator` (e.g. test_new_strategies).

from scans.time_decay import (
    scan_time_decay,
    _check_time_to_expiry,
    _validate_consensus,
    _refine_time_decay_with_prices,
)


@pytest.fixture(autouse=True)
def cleanup_modules():
    """Remove scans.time_decay from sys.modules to prevent test pollution."""
    yield
    sys.modules.pop("scans.time_decay", None)


@pytest.fixture(autouse=True)
def mock_time():
    """Mock time.time() to control expiry calculations."""
    with patch("scans.time_decay.time.time") as mock_t:
        # 2026-04-04 12:00:00 UTC
        mock_t.return_value = 1712282400
        yield mock_t


# ---------------------------------------------------------------------------
# TestExpiryTiming — Test _check_time_to_expiry()
# ---------------------------------------------------------------------------

class TestExpiryTiming:
    """Test sweet spot detection for time to expiry."""

    def test_accepts_48h_to_expiry(self):
        """Market with exactly 48h to expiry should return 48.0."""
        now = 1712282400
        hours_left = 48
        resolution_ts = int(now + (hours_left * 3600))

        result = _check_time_to_expiry(resolution_ts, min_hours=48)
        assert result is not None
        assert result == pytest.approx(48.0, abs=0.1)

    def test_accepts_12h_to_expiry(self):
        """Market with 12h to expiry (in sweet spot) should return 12.0."""
        now = 1712282400
        hours_left = 12
        resolution_ts = int(now + (hours_left * 3600))

        result = _check_time_to_expiry(resolution_ts, min_hours=48)
        assert result is not None
        assert result == pytest.approx(12.0, abs=0.1)

    def test_rejects_too_early_72h(self):
        """Market with 72h to expiry (too early) should return None."""
        now = 1712282400
        hours_left = 72
        resolution_ts = int(now + (hours_left * 3600))

        result = _check_time_to_expiry(resolution_ts, min_hours=48)
        assert result is None

    def test_rejects_too_late_30min(self):
        """Market with 0.5h to expiry (too late) should return None."""
        now = 1712282400
        minutes_left = 30
        resolution_ts = int(now + (minutes_left * 60))

        result = _check_time_to_expiry(resolution_ts, min_hours=48)
        assert result is None

    def test_rejects_expired(self):
        """Market that already expired (negative hours) should return None."""
        now = 1712282400
        resolution_ts = int(now - 3600)  # 1 hour ago

        result = _check_time_to_expiry(resolution_ts, min_hours=48)
        assert result is None

    def test_rejects_none_timestamp(self):
        """None timestamp should return None."""
        result = _check_time_to_expiry(None, min_hours=48)
        assert result is None

    def test_accepts_boundary_1h_plus(self):
        """Market with 1.1h to expiry (just over 1h boundary) should return ~1.1."""
        now = 1712282400
        hours_left = 1.1
        resolution_ts = int(now + (hours_left * 3600))

        result = _check_time_to_expiry(resolution_ts, min_hours=48)
        assert result is not None
        assert result > 1.0
        assert result < 2.0


# ---------------------------------------------------------------------------
# TestConsensusThreshold — Test _validate_consensus()
# ---------------------------------------------------------------------------

class TestConsensusThreshold:
    """Test consensus probability threshold validation."""

    def test_accepts_high_consensus(self):
        """Consensus of 0.95 should pass 0.90 threshold."""
        result = _validate_consensus(0.95, min_threshold=0.90)
        assert result is True

    def test_accepts_exactly_threshold(self):
        """Consensus of exactly 0.90 should pass 0.90 threshold."""
        result = _validate_consensus(0.90, min_threshold=0.90)
        assert result is True

    def test_rejects_low_consensus(self):
        """Consensus of 0.85 should reject 0.90 threshold."""
        result = _validate_consensus(0.85, min_threshold=0.90)
        assert result is False

    def test_accepts_zero_as_certain_no_consensus(self):
        """A zero YES probability is a 100% NO consensus."""
        result = _validate_consensus(0.0, min_threshold=0.90)
        assert result is True

    def test_rejects_none_consensus(self):
        """None consensus should return False."""
        result = _validate_consensus(None, min_threshold=0.90)
        assert result is False

    def test_rejects_non_numeric_consensus(self):
        """Non-numeric consensus should return False."""
        result = _validate_consensus("0.95", min_threshold=0.90)
        assert result is False

    def test_rejects_boolean_consensus(self):
        """Booleans are not valid probabilities despite subclassing int."""
        assert _validate_consensus(True, min_threshold=0.90) is False

    def test_accepts_very_high_consensus(self):
        """Consensus of 0.99 should easily pass threshold."""
        result = _validate_consensus(0.99, min_threshold=0.90)
        assert result is True


# ---------------------------------------------------------------------------
# TestScanStage1 — Test scan_time_decay() filtering
# ---------------------------------------------------------------------------

class TestScanStage1:
    """Test Stage 1 scanning logic."""

    def test_finds_high_consensus_near_expiry(self):
        """Market with 0.95 consensus, 24h expiry, price 0.90 should be included."""
        now = 1712282400
        resolution_ts = int(now + (24 * 3600))

        markets_by_key = {
            "market_1": {
                "id": "market_1",
                "question": "Will Bitcoin reach $100k?",
                "resolutionSource": {"timestamp": resolution_ts},
                "price": 0.90,
            }
        }

        mock_aggregator = MagicMock()
        mock_aggregator.get_consensus.return_value = {"probability": 0.95, "sources": ["test"]}

        opps = scan_time_decay(
            markets_by_key,
            mock_aggregator,
            min_hours_to_expiry=48,
            min_consensus=0.90,
            buy_below_price=0.95,
        )

        assert len(opps) == 1
        assert opps[0]["type"] == "TimeDecay"
        assert opps[0]["market_key"] == "market_1"
        assert opps[0]["_consensus_prob"] == 0.95

    def test_skips_low_consensus(self):
        """Market with 0.85 consensus (below 0.90 threshold) should be skipped."""
        now = 1712282400
        resolution_ts = int(now + (24 * 3600))

        markets_by_key = {
            "market_2": {
                "id": "market_2",
                "question": "Will Trump win 2028?",
                "resolutionSource": {"timestamp": resolution_ts},
                "price": 0.90,
            }
        }

        mock_aggregator = MagicMock()
        mock_aggregator.get_consensus.return_value = {"probability": 0.85, "sources": ["test"]}

        opps = scan_time_decay(
            markets_by_key,
            mock_aggregator,
            min_hours_to_expiry=48,
            min_consensus=0.90,
        )

        assert len(opps) == 0

    def test_skips_too_early(self):
        """Market with 72h to expiry (outside sweet spot) should be skipped."""
        now = 1712282400
        resolution_ts = int(now + (72 * 3600))

        markets_by_key = {
            "market_3": {
                "id": "market_3",
                "question": "Fed rate cut?",
                "resolutionSource": {"timestamp": resolution_ts},
                "price": 0.90,
            }
        }

        mock_aggregator = MagicMock()
        mock_aggregator.get_consensus.return_value = {"probability": 0.95, "sources": ["test"]}

        opps = scan_time_decay(
            markets_by_key,
            mock_aggregator,
            min_hours_to_expiry=48,
            min_consensus=0.90,
        )

        assert len(opps) == 0

    def test_skips_price_above_threshold(self):
        """Market with price 0.96 >= 0.95 buy threshold should be skipped."""
        now = 1712282400
        resolution_ts = int(now + (24 * 3600))

        markets_by_key = {
            "market_4": {
                "id": "market_4",
                "question": "Economic contraction?",
                "resolutionSource": {"timestamp": resolution_ts},
                "price": 0.96,  # Above buy_below_price threshold
            }
        }

        mock_aggregator = MagicMock()
        mock_aggregator.get_consensus.return_value = {"probability": 0.95, "sources": ["test"]}

        opps = scan_time_decay(
            markets_by_key,
            mock_aggregator,
            min_hours_to_expiry=48,
            min_consensus=0.90,
            buy_below_price=0.95,
        )

        assert len(opps) == 0

    def test_returns_guaranteed_gain(self):
        """Opportunity should include target minus executable outcome price."""
        now = 1712282400
        resolution_ts = int(now + (24 * 3600))

        markets_by_key = {
            "market_5": {
                "id": "market_5",
                "question": "Price surge?",
                "resolutionSource": {"timestamp": resolution_ts},
                "price": 0.90,
            }
        }

        mock_aggregator = MagicMock()
        mock_aggregator.get_consensus.return_value = {"probability": 0.95, "sources": ["test"]}

        opps = scan_time_decay(
            markets_by_key,
            mock_aggregator,
            min_hours_to_expiry=48,
            min_consensus=0.90,
            buy_below_price=0.95,
        )

        assert len(opps) == 1
        assert "_guaranteed_gain" in opps[0]
        assert opps[0]["_guaranteed_gain"] == pytest.approx(0.05)

    def test_consensus_side_yes_when_high(self):
        """Consensus >0.50 should set _consensus_side = YES."""
        now = 1712282400
        resolution_ts = int(now + (24 * 3600))

        markets_by_key = {
            "market_6": {
                "id": "market_6",
                "question": "Inflation spike?",
                "resolutionSource": {"timestamp": resolution_ts},
                "price": 0.90,
            }
        }

        mock_aggregator = MagicMock()
        mock_aggregator.get_consensus.return_value = {"probability": 0.92, "sources": ["test"]}

        opps = scan_time_decay(
            markets_by_key,
            mock_aggregator,
            min_hours_to_expiry=48,
            min_consensus=0.90,
        )

        assert len(opps) == 1
        assert opps[0]["_consensus_side"] == "YES"

    def test_consensus_side_no_when_low(self):
        """Consensus <0.50 should set _consensus_side = NO."""
        now = 1712282400
        resolution_ts = int(now + (24 * 3600))

        markets_by_key = {
            "market_7": {
                "id": "market_7",
                "question": "Market crash?",
                "resolutionSource": {"timestamp": resolution_ts},
                "price": 0.12,  # Market price offers NO at 0.88
            }
        }

        mock_aggregator = MagicMock()
        mock_aggregator.get_consensus.return_value = {"probability": 0.08, "sources": ["test"]}  # 92% consensus on NO

        opps = scan_time_decay(
            markets_by_key,
            mock_aggregator,
            min_hours_to_expiry=48,
            min_consensus=0.90,
        )

        assert len(opps) == 1
        assert opps[0]["_consensus_side"] == "NO"
        assert opps[0]["_current_price"] == pytest.approx(0.88)
        assert opps[0]["_guaranteed_gain"] == pytest.approx(0.04)


# ---------------------------------------------------------------------------
# TestRefinement — Test Stage 2 validation
# ---------------------------------------------------------------------------

class TestRefinement:
    """Test Stage 2 refinement logic."""

    def test_rejects_price_rise(self):
        """Price risen to 0.96 > buy_below_price should be dropped."""
        opportunities = [
            {
                "type": "TimeDecay",
                "market_key": "market_1",
                "_hours_to_expiry": 24.0,
                "_consensus_side": "YES",
                "_consensus_prob": 0.95,
                "_target_price": 0.95,
                "_guaranteed_gain": 0.0,
                "_current_price": 0.90,
            }
        ]

        current_prices = {"market_1": 0.96}

        refined = _refine_time_decay_with_prices(opportunities, current_prices)

        assert len(refined) == 0

    def test_keeps_same_price(self):
        """Price remaining at 0.94 < 0.95 should be kept."""
        opportunities = [
            {
                "type": "TimeDecay",
                "market_key": "market_2",
                "_hours_to_expiry": 24.0,
                "_consensus_side": "YES",
                "_target_price": 0.95,
                "_current_price": 0.90,
            }
        ]

        current_prices = {"market_2": 0.94}

        refined = _refine_time_decay_with_prices(opportunities, current_prices)

        assert len(refined) == 1

    def test_rejects_expired_too_late(self):
        """Market with <1h remaining should be dropped."""
        opportunities = [
            {
                "type": "TimeDecay",
                "market_key": "market_3",
                "_hours_to_expiry": 0.5,  # Less than 1 hour
                "_target_price": 0.95,
                "_current_price": 0.90,
            }
        ]

        current_prices = {"market_3": 0.92}

        refined = _refine_time_decay_with_prices(opportunities, current_prices)

        assert len(refined) == 0

    def test_keeps_still_profitable(self):
        """Market with 2h left and price 0.93 should be kept."""
        opportunities = [
            {
                "type": "TimeDecay",
                "market_key": "market_4",
                "_hours_to_expiry": 2.0,
                "_target_price": 0.95,
                "_current_price": 0.90,
            }
        ]

        current_prices = {"market_4": 0.93}

        refined = _refine_time_decay_with_prices(opportunities, current_prices)

        assert len(refined) == 1


# ---------------------------------------------------------------------------
# TestHoldToResolution — Test hold-to-resolution logic
# ---------------------------------------------------------------------------

class TestHoldToResolution:
    """Test hold-to-resolution behavior."""

    def test_profit_realized_at_resolution_correct(self):
        """Buy 0.90, resolution YES → profit = 1.0 - 0.90 - fees."""
        from fees import net_profit_time_decay

        entry_price = 0.90
        exit_price = 1.0
        size = 1.0

        net_profit = net_profit_time_decay(entry_price, exit_price, size, "polymarket")

        # Should be positive: gross (0.10) minus entry taker fee
        assert net_profit > 0.0
        # Conservatively, profit should be between 5-10% for this scenario
        assert net_profit > 0.04
        assert net_profit < 0.10

    def test_loss_if_consensus_wrong(self):
        """Buy 0.90, resolution NO → loss = 0 - 0.90 - fees."""
        from fees import net_profit_time_decay

        entry_price = 0.90
        exit_price = 0.0
        size = 1.0

        net_profit = net_profit_time_decay(entry_price, exit_price, size, "polymarket")

        # Should be negative: loss of 0.90 plus fees
        assert net_profit < 0.0
        assert net_profit < -0.90


# ---------------------------------------------------------------------------
# TestOpportunitiesSerialization — Test opportunity dict structure
# ---------------------------------------------------------------------------

class TestOpportunitiesSerialization:
    """Test that opportunities have correct structure."""

    def test_has_required_fields(self):
        """Opportunity dict should have all required fields."""
        now = 1712282400
        resolution_ts = int(now + (24 * 3600))

        markets_by_key = {
            "market_1": {
                "question": "Test?",
                "resolutionSource": {"timestamp": resolution_ts},
                "price": 0.90,
            }
        }

        mock_aggregator = MagicMock()
        mock_aggregator.get_consensus.return_value = {"probability": 0.95, "sources": ["test"]}

        opps = scan_time_decay(
            markets_by_key,
            mock_aggregator,
            min_hours_to_expiry=48,
            min_consensus=0.90,
        )

        assert len(opps) == 1
        opp = opps[0]

        required_fields = [
            "type",
            "market",
            "market_key",
            "_hours_to_expiry",
            "_consensus_side",
            "_consensus_prob",
            "_target_price",
            "_guaranteed_gain",
            "_current_price",
        ]

        for field in required_fields:
            assert field in opp, f"Missing field: {field}"

    def test_consensus_side_matches_probability(self):
        """_consensus_side should match consensus probability direction."""
        now = 1712282400
        resolution_ts = int(now + (24 * 3600))

        # Test YES side (consensus > 0.50)
        markets_by_key = {
            "market_yes": {
                "question": "Test YES?",
                "resolutionSource": {"timestamp": resolution_ts},
                "price": 0.90,
            }
        }

        mock_aggregator = MagicMock()
        mock_aggregator.get_consensus.return_value = {"probability": 0.92, "sources": ["test"]}

        opps = scan_time_decay(markets_by_key, mock_aggregator)
        assert len(opps) == 1
        assert opps[0]["_consensus_side"] == "YES"
        assert opps[0]["_consensus_prob"] == 0.92

        # Test NO side (consensus < 0.50)
        markets_by_key_no = {
            "market_no": {
                "question": "Test NO?",
                "resolutionSource": {"timestamp": resolution_ts},
                "price": 0.10,
            }
        }

        mock_aggregator.get_consensus.return_value = {"probability": 0.08, "sources": ["test"]}

        opps_no = scan_time_decay(
            markets_by_key_no,
            mock_aggregator,
            min_consensus=0.05,  # Lower threshold for NO
        )

        assert len(opps_no) == 1
        assert opps_no[0]["_consensus_side"] == "NO"
        assert opps_no[0]["_consensus_prob"] == 0.08


# ---------------------------------------------------------------------------
# TestEdgeCases — Test edge cases and boundary conditions
# ---------------------------------------------------------------------------

class TestEdgeCases:
    """Test edge cases and boundary conditions."""

    def test_empty_markets(self):
        """Empty markets dict should return empty opportunities list."""
        markets_by_key = {}
        mock_aggregator = MagicMock()

        opps = scan_time_decay(markets_by_key, mock_aggregator)

        assert len(opps) == 0

    def test_market_no_resolution_source(self):
        """Market missing resolutionSource should be skipped."""
        markets_by_key = {
            "market_1": {
                "question": "Test?",
                "price": 0.90,
                # No resolutionSource
            }
        }

        mock_aggregator = MagicMock()
        mock_aggregator.get_consensus.return_value = {"probability": 0.95, "sources": ["test"]}

        opps = scan_time_decay(markets_by_key, mock_aggregator)

        assert len(opps) == 0

    def test_market_nonnumeric_resolution_timestamp(self):
        markets_by_key = {
            "market_1": {
                "question": "Test?",
                "resolutionSource": {"timestamp": "unknown"},
                "price": 0.90,
            }
        }
        mock_aggregator = MagicMock()
        mock_aggregator.get_consensus.return_value = {"probability": 0.95, "sources": ["test"]}

        assert scan_time_decay(markets_by_key, mock_aggregator) == []

    def test_market_boolean_price_is_skipped(self):
        now = 1712282400
        markets_by_key = {
            "market_1": {
                "question": "Test?",
                "resolutionSource": {"timestamp": int(now + 24 * 3600)},
                "price": False,
            }
        }
        mock_aggregator = MagicMock()
        mock_aggregator.get_consensus.return_value = {"probability": 0.95, "sources": ["test"]}

        assert scan_time_decay(markets_by_key, mock_aggregator) == []

    def test_market_none_consensus(self):
        """Market with None consensus should be skipped."""
        now = 1712282400
        resolution_ts = int(now + (24 * 3600))

        markets_by_key = {
            "market_1": {
                "question": "Test?",
                "resolutionSource": {"timestamp": resolution_ts},
                "price": 0.90,
            }
        }

        mock_aggregator = MagicMock()
        mock_aggregator.get_consensus.return_value = None

        opps = scan_time_decay(markets_by_key, mock_aggregator)

        assert len(opps) == 0

    def test_market_no_price(self):
        """Market missing price field should be skipped."""
        now = 1712282400
        resolution_ts = int(now + (24 * 3600))

        markets_by_key = {
            "market_1": {
                "question": "Test?",
                "resolutionSource": {"timestamp": resolution_ts},
                # No price
            }
        }

        mock_aggregator = MagicMock()
        mock_aggregator.get_consensus.return_value = {"probability": 0.95, "sources": ["test"]}

        opps = scan_time_decay(markets_by_key, mock_aggregator)

        assert len(opps) == 0

    def test_multiple_markets_mixed_results(self):
        """Multiple markets with mixed pass/fail should filter correctly."""
        now = 1712282400

        markets_by_key = {
            "market_pass_1": {
                "question": "Pass 1?",
                "resolutionSource": {"timestamp": int(now + (24 * 3600))},
                "price": 0.90,
            },
            "market_fail_consensus": {
                "question": "Fail?",
                "resolutionSource": {"timestamp": int(now + (24 * 3600))},
                "price": 0.90,
            },
            "market_fail_price": {
                "question": "Fail price?",
                "resolutionSource": {"timestamp": int(now + (24 * 3600))},
                "price": 0.96,
            },
        }

        mock_aggregator = MagicMock()
        mock_aggregator.get_consensus.side_effect = lambda mk: {
            "market_pass_1": 0.95,
            "market_fail_consensus": 0.85,
            "market_fail_price": 0.95,
        }.get(mk)

        opps = scan_time_decay(
            markets_by_key,
            mock_aggregator,
            min_hours_to_expiry=48,
            min_consensus=0.90,
            buy_below_price=0.95,
        )

        assert len(opps) == 1
        assert opps[0]["market_key"] == "market_pass_1"
