"""Tests for scans/imbalance.py — order book imbalance detection."""

import sys
import os
import pytest
from unittest.mock import patch, MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# Mock external APIs before importing the module under test, but save and
# restore so that later tests (e.g. test_polymarket_api) which need the real
# module are not poisoned by a leftover MagicMock in sys.modules.
_saved_polymarket_api = sys.modules.get("polymarket_api")
sys.modules["polymarket_api"] = MagicMock()

from scans.imbalance import (
    _calculate_imbalance_ratio,
    _refine_imbalance_with_clob,
    scan_imbalance,
)

# Restore original module so other test files are not affected.
if _saved_polymarket_api is not None:
    sys.modules["polymarket_api"] = _saved_polymarket_api
else:
    sys.modules.pop("polymarket_api", None)


@pytest.fixture(autouse=True)
def cleanup_modules():
    """Remove scans.imbalance from sys.modules to prevent test pollution."""
    yield
    sys.modules.pop("scans.imbalance", None)


# ---------------------------------------------------------------------------
# TestImbalanceRatio — Test _calculate_imbalance_ratio()
# ---------------------------------------------------------------------------

class TestImbalanceRatio:
    """Test the imbalance ratio calculation formula."""

    def test_ratio_positive_bid_dominance(self):
        """3:1 bid/ask ratio (100 bid vs 50 ask) should give (100-50)/(100+50) = 0.333."""
        order_book = {
            "bids": [
                {"price": 0.50, "size": 100},
                {"price": 0.49, "size": 0},
                {"price": 0.48, "size": 0},
                {"price": 0.47, "size": 0},
                {"price": 0.46, "size": 0},
            ],
            "asks": [
                {"price": 0.51, "size": 50},
                {"price": 0.52, "size": 0},
                {"price": 0.53, "size": 0},
                {"price": 0.54, "size": 0},
                {"price": 0.55, "size": 0},
            ]
        }
        ratio = _calculate_imbalance_ratio(order_book, top_levels=5)
        assert ratio == pytest.approx((100 - 50) / (100 + 50))
        assert ratio > 0  # Bid dominance

    def test_ratio_negative_ask_dominance(self):
        """1:3 bid/ask ratio (50 bid vs 100 ask) should give (50-100)/(50+100) = -0.333."""
        order_book = {
            "bids": [
                {"price": 0.50, "size": 50},
                {"price": 0.49, "size": 0},
                {"price": 0.48, "size": 0},
                {"price": 0.47, "size": 0},
                {"price": 0.46, "size": 0},
            ],
            "asks": [
                {"price": 0.51, "size": 100},
                {"price": 0.52, "size": 0},
                {"price": 0.53, "size": 0},
                {"price": 0.54, "size": 0},
                {"price": 0.55, "size": 0},
            ]
        }
        ratio = _calculate_imbalance_ratio(order_book, top_levels=5)
        assert ratio == pytest.approx((50 - 100) / (50 + 100))
        assert ratio < 0  # Ask dominance

    def test_ratio_zero_balanced(self):
        """Equal bid/ask volumes (75 each) should give ratio = 0.0."""
        order_book = {
            "bids": [
                {"price": 0.50, "size": 75},
                {"price": 0.49, "size": 0},
                {"price": 0.48, "size": 0},
                {"price": 0.47, "size": 0},
                {"price": 0.46, "size": 0},
            ],
            "asks": [
                {"price": 0.51, "size": 75},
                {"price": 0.52, "size": 0},
                {"price": 0.53, "size": 0},
                {"price": 0.54, "size": 0},
                {"price": 0.55, "size": 0},
            ]
        }
        ratio = _calculate_imbalance_ratio(order_book, top_levels=5)
        assert ratio == pytest.approx(0.0)

    def test_ratio_empty_orderbook(self):
        """Empty order book (no bids/asks) should return 0.0."""
        order_book = {"bids": [], "asks": []}
        ratio = _calculate_imbalance_ratio(order_book, top_levels=5)
        assert ratio == 0.0

    def test_ratio_respects_top_levels(self):
        """Only first N levels should be summed (top_levels parameter)."""
        order_book = {
            "bids": [
                {"price": 0.50, "size": 100},
                {"price": 0.49, "size": 100},
                {"price": 0.48, "size": 100},
                {"price": 0.47, "size": 10},  # Lower at level 4
                {"price": 0.46, "size": 10},  # Lower at level 5
            ],
            "asks": [
                {"price": 0.51, "size": 50},
                {"price": 0.52, "size": 50},
                {"price": 0.53, "size": 50},
                {"price": 0.54, "size": 100},  # Higher at level 4
                {"price": 0.55, "size": 100},  # Higher at level 5
            ]
        }
        # With top_levels=3: (100+100+100)=300 bids, (50+50+50)=150 asks
        ratio_top3 = _calculate_imbalance_ratio(order_book, top_levels=3)
        assert ratio_top3 == pytest.approx((300 - 150) / (300 + 150))

        # With top_levels=5: (100+100+100+10+10)=320 bids, (50+50+50+100+100)=350 asks
        ratio_top5 = _calculate_imbalance_ratio(order_book, top_levels=5)
        assert ratio_top5 == pytest.approx((320 - 350) / (320 + 350))

        # top_levels=3 should show stronger bid dominance than top_levels=5
        assert ratio_top3 > ratio_top5

    def test_ratio_only_bids(self):
        """Order book with only bids should return 1.0."""
        order_book = {
            "bids": [
                {"price": 0.50, "size": 100},
                {"price": 0.49, "size": 100},
            ],
            "asks": []
        }
        ratio = _calculate_imbalance_ratio(order_book, top_levels=5)
        assert ratio == pytest.approx(1.0)

    def test_ratio_only_asks(self):
        """Order book with only asks should return -1.0."""
        order_book = {
            "bids": [],
            "asks": [
                {"price": 0.51, "size": 100},
                {"price": 0.52, "size": 100},
            ]
        }
        ratio = _calculate_imbalance_ratio(order_book, top_levels=5)
        assert ratio == pytest.approx(-1.0)

    def test_ratio_missing_size_field(self):
        """Order book entries with missing size should be treated as 0."""
        order_book = {
            "bids": [
                {"price": 0.50},  # Missing size
                {"price": 0.49, "size": 100},
            ],
            "asks": [
                {"price": 0.51, "size": 50},
            ]
        }
        # bid_vol = 0 + 100 = 100, ask_vol = 50
        ratio = _calculate_imbalance_ratio(order_book, top_levels=5)
        assert ratio == pytest.approx((100 - 50) / (100 + 50))


# ---------------------------------------------------------------------------
# TestRefinement — Test _refine_imbalance_with_clob()
# ---------------------------------------------------------------------------

class TestRefinement:
    """Test Stage 2 refinement logic."""

    def test_accepts_stable_imbalance(self):
        """If imbalance ratio drops <30%, opportunity should be kept."""
        opportunities = [
            {
                "type": "Imbalance",
                "market": "Test Market",
                "_imbalance_ratio": 0.4,
                "_direction": "YES",
                "_token_ids": ["token123"],
                "_market_key": "market1",
            }
        ]

        # Mock fetch_order_book to return a book with slightly lower ratio (but not collapsed)
        # Original: 0.4, Collapse threshold: 0.7 * |0.4| = 0.28
        # New ratio should be >= 0.28 to survive
        # 200 bids / 100 asks = (200-100)/(200+100) = 0.333, which is > 0.28
        def mock_fetch_order_book(token_id):
            return {
                "bids": [{"price": 0.50, "size": 200}],
                "asks": [{"price": 0.51, "size": 100}]
            }

        with patch("polymarket_api.fetch_order_book", side_effect=mock_fetch_order_book):
            refined = _refine_imbalance_with_clob(opportunities)

        assert len(refined) == 1
        assert refined[0]["_clob_validated"] is True

    def test_rejects_collapsed_imbalance(self):
        """If imbalance ratio drops >30%, opportunity should be rejected."""
        opportunities = [
            {
                "type": "Imbalance",
                "market": "Test Market",
                "_imbalance_ratio": 0.4,  # Original ratio
                "_direction": "YES",
                "_token_ids": ["token123"],
                "_market_key": "market1",
            }
        ]

        # Mock fetch_order_book to return a book with much lower ratio (collapse >30%)
        # Collapse threshold = 0.7 * |0.4| = 0.28
        # Current ratio = 0.1 < 0.28 → rejected
        def mock_fetch_order_book(token_id):
            return {
                "bids": [{"price": 0.50, "size": 110}],
                "asks": [{"price": 0.51, "size": 100}]  # Will give ~0.048 ratio
            }

        with patch("polymarket_api.fetch_order_book", side_effect=mock_fetch_order_book):
            refined = _refine_imbalance_with_clob(opportunities)

        # Should be rejected because current ratio is too low
        assert len(refined) == 0

    def test_returns_refined_list(self):
        """5 opportunities, 3 survive refinement."""
        opportunities = [
            {
                "type": "Imbalance",
                "market": f"Market {i}",
                "_imbalance_ratio": 0.8,  # Higher ratio so collapse_threshold = 0.7 * 0.8 = 0.56
                "_direction": "YES",
                "_token_ids": [f"token{i}"],
                "_market_key": f"market{i}",
            }
            for i in range(5)
        ]

        # Mock: alternating stable and collapsed
        counter = {"count": 0}

        def mock_fetch_order_book(token_id):
            counter["count"] += 1
            # Odd calls: return stable (ratio ~0.6 > threshold 0.56), even: return collapsed (ratio ~0.3 < threshold 0.56)
            if counter["count"] % 2 == 1:
                return {
                    "bids": [{"price": 0.50, "size": 800}],
                    "asks": [{"price": 0.51, "size": 200}]
                    # Ratio: (800-200)/(800+200) = 0.6
                }
            else:
                # Collapsed ratio (absolute value < 0.56)
                return {
                    "bids": [{"price": 0.50, "size": 220}],
                    "asks": [{"price": 0.51, "size": 280}]
                    # Ratio: (220-280)/(220+280) = -0.12
                }

        with patch("polymarket_api.fetch_order_book", side_effect=mock_fetch_order_book):
            refined = _refine_imbalance_with_clob(opportunities)

        # Should keep the odd-indexed ones (indices 0, 2, 4) = 3 opportunities
        assert len(refined) == 3

    def test_handles_clob_unavailable(self):
        """If CLOB fetch fails, drop the unverified opportunity."""
        opportunities = [
            {
                "type": "Imbalance",
                "market": "Test Market",
                "_imbalance_ratio": 0.4,
                "_direction": "YES",
                "_token_ids": ["token123"],
                "_market_key": "market1",
            }
        ]

        def mock_fetch_order_book(token_id):
            return None  # Simulate CLOB unavailable

        with patch("polymarket_api.fetch_order_book", side_effect=mock_fetch_order_book):
            refined = _refine_imbalance_with_clob(opportunities)

        # No executable CLOB means the opportunity cannot be validated.
        assert refined == []

    def test_drops_without_token_ids(self):
        """Opportunities without _token_ids should be dropped."""
        opportunities = [
            {
                "type": "Imbalance",
                "market": "Test Market",
                "_imbalance_ratio": 0.4,
                "_direction": "YES",
                "_token_ids": [],  # Empty!
                "_market_key": "market1",
            }
        ]

        refined = _refine_imbalance_with_clob(opportunities)
        assert len(refined) == 0


# ---------------------------------------------------------------------------
# TestScanStage1 — Test scan_imbalance()
# ---------------------------------------------------------------------------

class TestScanStage1:
    """Test Stage 1 scanning logic."""

    def test_finds_bid_dominance(self):
        """Market with 3:1 bid/ask ratio should create YES opportunity."""
        markets_by_key = {
            "market1": {
                "conditionId": "market1",
                "question": "Will ETH reach $5000?",
                "clobTokenIds": ["token_yes", "token_no"],
            }
        }

        def mock_fetch_order_book(token_id):
            if token_id == "token_yes":
                return {
                    "bids": [
                        {"price": 0.50, "size": 100},
                        {"price": 0.49, "size": 100},
                        {"price": 0.48, "size": 100},
                        {"price": 0.47, "size": 100},
                        {"price": 0.46, "size": 100},
                    ],
                    "asks": [
                        {"price": 0.51, "size": 33},
                        {"price": 0.52, "size": 33},
                        {"price": 0.53, "size": 34},
                        {"price": 0.54, "size": 0},
                        {"price": 0.55, "size": 0},
                    ]
                }
            return None

        with patch("polymarket_api.fetch_order_book", side_effect=mock_fetch_order_book):
            with patch("scans.helpers._extract_token_ids", return_value=["token_yes", "token_no"]):
                opps = scan_imbalance(markets_by_key, min_ratio=3.0)

        assert len(opps) > 0
        assert opps[0]["type"] == "Imbalance"
        assert opps[0]["_direction"] == "YES"
        assert opps[0]["_imbalance_ratio"] > 0

    def test_finds_ask_dominance(self):
        """Market with 1:3 bid/ask ratio should create NO opportunity."""
        markets_by_key = {
            "market1": {
                "conditionId": "market1",
                "question": "Will ETH reach $5000?",
                "clobTokenIds": ["token_yes", "token_no"],
            }
        }

        def mock_fetch_order_book(token_id):
            if token_id == "token_yes":
                return {
                    "bids": [
                        {"price": 0.50, "size": 33},
                        {"price": 0.49, "size": 33},
                        {"price": 0.48, "size": 34},
                        {"price": 0.47, "size": 0},
                        {"price": 0.46, "size": 0},
                    ],
                    "asks": [
                        {"price": 0.51, "size": 100},
                        {"price": 0.52, "size": 100},
                        {"price": 0.53, "size": 100},
                        {"price": 0.54, "size": 0},
                        {"price": 0.55, "size": 0},
                    ]
                }
            return None

        with patch("polymarket_api.fetch_order_book", side_effect=mock_fetch_order_book):
            with patch("scans.helpers._extract_token_ids", return_value=["token_yes", "token_no"]):
                opps = scan_imbalance(markets_by_key, min_ratio=3.0)

        assert len(opps) > 0
        assert opps[0]["type"] == "Imbalance"
        assert opps[0]["_direction"] == "NO"
        assert opps[0]["_imbalance_ratio"] < 0

    def test_respects_min_ratio_threshold(self):
        """Opportunities with imbalance < threshold should be skipped."""
        markets_by_key = {
            "market1": {
                "conditionId": "market1",
                "question": "Will ETH reach $5000?",
                "clobTokenIds": ["token_yes", "token_no"],
            }
        }

        def mock_fetch_order_book(token_id):
            if token_id == "token_yes":
                # This gives ~0.33 ratio (3:1), but min_ratio=4.0 expects >= 0.6
                return {
                    "bids": [{"price": 0.50, "size": 100}],
                    "asks": [{"price": 0.51, "size": 50}]
                }
            return None

        with patch("polymarket_api.fetch_order_book", side_effect=mock_fetch_order_book):
            with patch("scans.helpers._extract_token_ids", return_value=["token_yes", "token_no"]):
                # min_ratio=4.0 requires threshold >= (4-1)/(4+1) = 0.6
                opps = scan_imbalance(markets_by_key, min_ratio=4.0)

        # Ratio is 0.33 < 0.6 threshold, so should be filtered
        assert len(opps) == 0

    def test_skips_markets_without_token_ids(self):
        """Markets without clobTokenIds should be skipped."""
        markets_by_key = {
            "market1": {
                "conditionId": "market1",
                "question": "Will ETH reach $5000?",
                # No clobTokenIds!
            }
        }

        opps = scan_imbalance(markets_by_key, min_ratio=3.0)
        assert len(opps) == 0

    def test_skips_markets_with_insufficient_token_ids(self):
        """Markets with < 2 token IDs should be skipped."""
        markets_by_key = {
            "market1": {
                "conditionId": "market1",
                "question": "Will ETH reach $5000?",
                "clobTokenIds": ["token_yes"],  # Only 1 token ID
            }
        }

        opps = scan_imbalance(markets_by_key, min_ratio=3.0)
        assert len(opps) == 0

    def test_returns_opportunity_with_correct_keys(self):
        """Returned opportunities should have all required keys."""
        markets_by_key = {
            "market1": {
                "conditionId": "market1",
                "question": "Will ETH reach $5000?",
                "clobTokenIds": ["token_yes", "token_no"],
            }
        }

        def mock_fetch_order_book(token_id):
            if token_id == "token_yes":
                return {
                    "bids": [{"price": 0.50, "size": 100}],
                    "asks": [{"price": 0.51, "size": 50}]
                }
            return None

        with patch("polymarket_api.fetch_order_book", side_effect=mock_fetch_order_book):
            with patch("scans.helpers._extract_token_ids", return_value=["token_yes", "token_no"]):
                opps = scan_imbalance(markets_by_key, min_ratio=1.0)

        # After stage 1, before refinement
        assert len(opps) > 0
        opp = opps[0]

        # Check required keys
        assert "type" in opp
        assert opp["type"] == "Imbalance"
        assert "market" in opp
        assert "_imbalance_ratio" in opp
        assert "_direction" in opp
        assert "_token_ids" in opp
        assert "_market_key" in opp
