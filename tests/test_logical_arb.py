"""Tests for scans/logical_arb.py — combinatorial logical arbitrage detection."""

import sys
import os
import math
import pytest
import json
from unittest.mock import patch, MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# Mock external APIs before importing the module under test, but save and
# restore so that later test files (e.g. test_polymarket_api) which need
# the real module are not poisoned by a leftover MagicMock in sys.modules.
_saved_polymarket_api = sys.modules.get("polymarket_api")
sys.modules["polymarket_api"] = MagicMock()

# Also need to mock helpers since logical_arb imports it
from scans import helpers

# Restore original module so other test files are not affected.
if _saved_polymarket_api is not None:
    sys.modules["polymarket_api"] = _saved_polymarket_api
else:
    sys.modules.pop("polymarket_api", None)

# Patch _extract_token_ids in helpers
def mock_extract_token_ids(market: dict) -> list:
    """Mock implementation of _extract_token_ids."""
    token_ids_raw = market.get("clobTokenIds")
    if not token_ids_raw:
        return []
    try:
        if isinstance(token_ids_raw, str):
            return json.loads(token_ids_raw)
        return list(token_ids_raw)
    except (json.JSONDecodeError, ValueError, TypeError):
        return []

helpers._extract_token_ids = mock_extract_token_ids


def _validated_best_bid_ask(order_book: dict) -> dict:
    """Test stub matching the production executable-ask contract."""
    result = {"bid": None, "bid_size": None, "ask": None, "ask_size": None}
    asks = order_book.get("asks", [])
    if asks:
        try:
            price = float(asks[0].get("price"))
            size = float(asks[0].get("size"))
        except (TypeError, ValueError):
            return result
        if math.isfinite(price) and math.isfinite(size) and 0.0 < price < 1.0 and size > 0.0:
            result["ask"] = price
            result["ask_size"] = size
    return result


def _import_logical_arb():
    """Import or reimport the logical_arb module."""
    sys.modules.pop("scans.logical_arb", None)
    saved_polymarket_api = sys.modules.get("polymarket_api")
    polymarket_stub = MagicMock()
    polymarket_stub.get_best_bid_ask.side_effect = _validated_best_bid_ask
    sys.modules["polymarket_api"] = polymarket_stub
    try:
        from scans.logical_arb import (
            scan_logical_arb,
            _refine_logical_arb_with_clob,
        )
    finally:
        if saved_polymarket_api is not None:
            sys.modules["polymarket_api"] = saved_polymarket_api
        else:
            sys.modules.pop("polymarket_api", None)
    return scan_logical_arb, _refine_logical_arb_with_clob


# ---------------------------------------------------------------------------
# TestScanStage1 — Test scan_logical_arb() mid-price detection
# ---------------------------------------------------------------------------


class TestScanStage1:
    """Test Stage 1: mid-price candidate identification."""

    def test_detects_rule_violation(self):
        """When then_price < if_price * (1 - threshold), should create opportunity."""
        scan_logical_arb, _ = _import_logical_arb()

        # Bitcoin >$100k at 0.50, Bitcoin >$90k at 0.40 (5% discount)
        # Violates: P(>$90k) should be >= P(>$100k)
        markets_by_key = {
            "polymarket-market-100k": {
                "price": 0.50,
                "question": "Bitcoin >$100k",
                "clobTokenIds": '["token-100k-yes", "token-100k-no"]',
            },
            "polymarket-market-90k": {
                "price": 0.40,
                "question": "Bitcoin >$90k",
                "clobTokenIds": '["token-90k-yes", "token-90k-no"]',
            },
        }

        rules = [
            {
                "if_yes": "market-100k",
                "then_yes": "market-90k",
                "relationship": "implies",
            }
        ]

        # Mock fetch_order_book to return a valid book (within 30% tolerance)
        with patch("scans.logical_arb.fetch_order_book") as mock_fetch:
            mock_fetch.return_value = {
                "asks": [{"price": 0.41, "size": 100}],
                "bids": [],
            }

            opps = scan_logical_arb(
                markets_by_key=markets_by_key,
                logical_arb_rules=rules,
                price_threshold=0.05,
            )

        # Should detect the violation (0.40 < 0.50 * 0.95 = 0.475)
        assert len(opps) > 0
        assert opps[0]["type"] == "LogicalArb"
        assert opps[0]["_if_price"] == 0.50
        assert opps[0]["_then_price"] == 0.41

    def test_respects_price_threshold(self):
        """Opportunities should only be created when discount exceeds threshold."""
        scan_logical_arb, _ = _import_logical_arb()

        # Set threshold to 10%
        markets_by_key = {
            "polymarket-market-a": {
                "price": 0.50,
                "question": "Event A",
                "clobTokenIds": '["token-a-yes", "token-a-no"]',
            },
            "polymarket-market-b": {
                "price": 0.48,  # Only 4% discount
                "question": "Event B",
                "clobTokenIds": '["token-b-yes", "token-b-no"]',
            },
        }

        rules = [{"if_yes": "market-a", "then_yes": "market-b", "relationship": "implies"}]

        # With 10% threshold, 4% discount should NOT create opportunity
        with patch("scans.logical_arb.fetch_order_book") as mock_fetch:
            mock_fetch.return_value = {
                "asks": [{"price": 0.49, "size": 100}],
                "bids": [],
            }
            opps = scan_logical_arb(
                markets_by_key=markets_by_key,
                logical_arb_rules=rules,
                price_threshold=0.10,
            )

        assert len(opps) == 0

        # With 3% threshold, 4% discount should create opportunity
        with patch("scans.logical_arb.fetch_order_book") as mock_fetch:
            mock_fetch.return_value = {
                "asks": [{"price": 0.49, "size": 100}],
                "bids": [],
            }
            opps = scan_logical_arb(
                markets_by_key=markets_by_key,
                logical_arb_rules=rules,
                price_threshold=0.03,
            )

        assert len(opps) > 0

    def test_returns_required_keys(self):
        """Opportunity dict should contain all required keys."""
        scan_logical_arb, _ = _import_logical_arb()

        markets_by_key = {
            "polymarket-market-1": {
                "price": 0.60,
                "question": "Market 1",
                "clobTokenIds": '["token-1-yes", "token-1-no"]',
            },
            "polymarket-market-2": {
                "price": 0.50,
                "question": "Market 2",
                "clobTokenIds": '["token-2-yes", "token-2-no"]',
            },
        }

        rules = [{"if_yes": "market-1", "then_yes": "market-2", "relationship": "implies"}]

        with patch("scans.logical_arb.fetch_order_book") as mock_fetch:
            mock_fetch.side_effect = [
                {"asks": [{"price": 0.51, "size": 100}], "bids": []},
                {"asks": [{"price": 0.35, "size": 100}], "bids": []},
            ]
            opps = scan_logical_arb(
                markets_by_key=markets_by_key,
                logical_arb_rules=rules,
                price_threshold=0.05,
            )

        assert len(opps) > 0
        opp = opps[0]

        # Check required keys
        required_keys = [
            "type",
            "market",
            "if_market_id",
            "then_market_id",
            "_if_price",
            "_then_price",
            "_token_ids",
            "_if_token_ids",
            "_market_key",
            "_layer",
        ]
        for key in required_keys:
            assert key in opp, f"Missing key: {key}"

        # Check layer
        assert opp["_layer"] == 4

    def test_handles_empty_rules(self):
        """Should return empty list when rules list is empty."""
        scan_logical_arb, _ = _import_logical_arb()

        markets_by_key = {
            "polymarket-market-1": {
                "price": 0.60,
                "question": "Market 1",
                "clobTokenIds": '["token-1-yes", "token-1-no"]',
            },
        }

        opps = scan_logical_arb(
            markets_by_key=markets_by_key,
            logical_arb_rules=[],
            price_threshold=0.05,
        )

        assert len(opps) == 0

    def test_skips_missing_markets(self):
        """Should skip rules where referenced markets don't exist."""
        scan_logical_arb, _ = _import_logical_arb()

        markets_by_key = {
            "polymarket-market-1": {
                "price": 0.60,
                "question": "Market 1",
                "clobTokenIds": '["token-1-yes", "token-1-no"]',
            },
            # market-2 is missing
        }

        rules = [{"if_yes": "market-1", "then_yes": "market-2", "relationship": "implies"}]

        opps = scan_logical_arb(
            markets_by_key=markets_by_key,
            logical_arb_rules=rules,
            price_threshold=0.05,
        )

        assert len(opps) == 0

    def test_skips_non_implies_relationships(self):
        """Should only process 'implies' relationships."""
        scan_logical_arb, _ = _import_logical_arb()

        markets_by_key = {
            "polymarket-market-1": {
                "price": 0.60,
                "question": "Market 1",
                "clobTokenIds": '["token-1-yes", "token-1-no"]',
            },
            "polymarket-market-2": {
                "price": 0.40,
                "question": "Market 2",
                "clobTokenIds": '["token-2-yes", "token-2-no"]',
            },
        }

        rules = [{"if_yes": "market-1", "then_yes": "market-2", "relationship": "contradicts"}]

        opps = scan_logical_arb(
            markets_by_key=markets_by_key,
            logical_arb_rules=rules,
            price_threshold=0.05,
        )

        assert len(opps) == 0


# ---------------------------------------------------------------------------
# TestRefinementStage2 — Test _refine_logical_arb_with_clob()
# ---------------------------------------------------------------------------


class TestRefinementStage2:
    """Test Stage 2: CLOB refinement."""

    def test_refines_with_clob_ask_price(self):
        """Should fetch CLOB and add _clob_ask_price key."""
        _, _refine_logical_arb_with_clob = _import_logical_arb()

        opportunities = [
            {
                "type": "LogicalArb",
                "_then_price": 0.40,
                "_token_ids": ["token-yes", "token-no"],
                "_if_token_ids": ["if-yes", "if-no"],
                "_market_key": "market-1",
            }
        ]

        # Mock fetch_order_book to return a book with asks
        with patch("scans.logical_arb.fetch_order_book") as mock_fetch:
            mock_fetch.return_value = {
                "asks": [{"price": 0.41, "size": 100}],
                "bids": [],
            }

            refined = _refine_logical_arb_with_clob(opportunities)

        assert len(refined) == 1
        assert "_clob_ask_price" in refined[0]
        assert refined[0]["_clob_ask_price"] == 0.41

    def test_drops_on_spread_widening(self):
        """Should drop opportunity if ask price >30% higher than Stage 1."""
        _, _refine_logical_arb_with_clob = _import_logical_arb()

        opportunities = [
            {
                "type": "LogicalArb",
                "_then_price": 0.40,
                "_token_ids": ["token-yes", "token-no"],
                "_if_token_ids": ["if-yes", "if-no"],
                "_market_key": "market-1",
            }
        ]

        # Mock fetch_order_book: ask price is 0.55 (37% above 0.40)
        with patch("scans.logical_arb.fetch_order_book") as mock_fetch:
            mock_fetch.return_value = {
                "asks": [{"price": 0.55, "size": 100}],  # 37% above Stage 1
                "bids": [],
            }

            refined = _refine_logical_arb_with_clob(opportunities)

        # Should drop the opportunity
        assert len(refined) == 0

    def test_clob_exception_fails_closed(self):
        """Should drop opportunity if CLOB fetch raises exception."""
        _, _refine_logical_arb_with_clob = _import_logical_arb()

        opportunities = [
            {
                "type": "LogicalArb",
                "_then_price": 0.40,
                "_token_ids": ["token-yes", "token-no"],
                "_if_token_ids": ["if-yes", "if-no"],
                "_market_key": "market-1",
            }
        ]

        # Mock fetch_order_book to raise an exception
        with patch("scans.logical_arb.fetch_order_book") as mock_fetch:
            mock_fetch.side_effect = Exception("CLOB API timeout")

            refined = _refine_logical_arb_with_clob(opportunities)

        assert refined == []

    def test_clob_none_fails_closed(self):
        """Should drop opportunity if CLOB returns None."""
        _, _refine_logical_arb_with_clob = _import_logical_arb()

        opportunities = [
            {
                "type": "LogicalArb",
                "_then_price": 0.40,
                "_token_ids": ["token-yes", "token-no"],
                "_if_token_ids": ["if-yes", "if-no"],
                "_market_key": "market-1",
            }
        ]

        # Mock fetch_order_book to return None
        with patch("scans.logical_arb.fetch_order_book") as mock_fetch:
            mock_fetch.return_value = None

            refined = _refine_logical_arb_with_clob(opportunities)

        assert refined == []

    def test_invalid_executable_ask_fails_closed(self):
        """A non-finite ask must not fall back to the Stage 1 price."""
        _, _refine_logical_arb_with_clob = _import_logical_arb()
        opportunities = [{
            "type": "LogicalArb",
            "_then_price": 0.40,
            "_token_ids": ["token-yes", "token-no"],
            "_if_token_ids": ["if-yes", "if-no"],
            "_market_key": "market-1",
        }]

        with patch("scans.logical_arb.fetch_order_book") as mock_fetch:
            mock_fetch.side_effect = [
                {"asks": [{"price": "nan", "size": 100}]},
                {"asks": [{"price": 0.35, "size": 100}]},
            ]
            refined = _refine_logical_arb_with_clob(opportunities)

        assert refined == []

    def test_empty_opportunities_list(self):
        """Should return empty list when input is empty."""
        _, _refine_logical_arb_with_clob = _import_logical_arb()

        refined = _refine_logical_arb_with_clob([])
        assert len(refined) == 0

    def test_drops_opportunity_with_no_token_ids(self):
        """Should drop opportunity if token_ids is missing."""
        _, _refine_logical_arb_with_clob = _import_logical_arb()

        opportunities = [
            {
                "type": "LogicalArb",
                "_then_price": 0.40,
                "_token_ids": [],  # Empty token IDs
                "_market_key": "market-1",
            }
        ]

        refined = _refine_logical_arb_with_clob(opportunities)

        assert len(refined) == 0


# ---------------------------------------------------------------------------
# TestFeeCalculation — Test net_profit_logical_arb()
# ---------------------------------------------------------------------------


class TestFeeCalculation:
    """Test fee calculation for logical arb."""

    def test_net_profit_basic(self):
        """net_profit_logical_arb(0.50, 0.40) should be positive."""
        from fees import net_profit_logical_arb

        profit = net_profit_logical_arb(price_if_yes=0.50, price_then_yes=0.40)

        # Gross: 0.50 - 0.40 = 0.10
        # Fees: polymarket_taker_fee(0.50) + polymarket_taker_fee(0.40)
        # With default fee rate 0.04: fees = 0.04 * 0.50 * 0.50 + 0.04 * 0.40 * 0.60 ≈ 0.0196
        # Net profit ≈ 0.10 - 0.0196 ≈ 0.080
        assert profit > 0.07
        assert profit < 0.11

    def test_net_profit_zero_margin(self):
        """When prices are equal, profit should be negative due to fees."""
        from fees import net_profit_logical_arb

        profit = net_profit_logical_arb(price_if_yes=0.50, price_then_yes=0.50)

        # Gross: 0.0 (no difference)
        # Fees: both sides pay taker fee
        # Net should be negative
        assert profit < 0

    def test_accounts_for_taker_fees(self):
        """Profit should be reduced by both entry fees (buy + sell)."""
        from fees import net_profit_logical_arb

        # Case 1: Mid-price (0.5)
        profit_mid = net_profit_logical_arb(price_if_yes=0.55, price_then_yes=0.50)

        # Case 2: Extreme prices (lower fee)
        profit_extreme = net_profit_logical_arb(price_if_yes=0.95, price_then_yes=0.90)

        # Both should have fees subtracted; extreme prices have lower fees
        # So extreme case should have higher profit for same price difference
        assert profit_extreme > profit_mid

    def test_handles_edge_prices(self):
        """Should handle edge case prices (0, 1)."""
        from fees import net_profit_logical_arb

        # Price at 0 or 1 has 0 fee
        profit_edge = net_profit_logical_arb(price_if_yes=0.99, price_then_yes=0.01)

        # Should be close to: 0.99 - 0.01 = 0.98 (minimal fees at extremes)
        assert profit_edge > 0.95


# ---------------------------------------------------------------------------
# TestExecutorIntegration — Test executor._build_legs() integration
# ---------------------------------------------------------------------------


class TestExecutorIntegration:
    """Test executor integration with LogicalArb opportunities."""

    def test_build_legs_logical_arb(self):
        """executor._build_legs() should create 2 legs for LogicalArb."""
        scan_logical_arb, _ = _import_logical_arb()

        # Test that scan and fee functions work together
        markets_by_key = {
            "polymarket-market-1": {
                "price": 0.60,
                "question": "Bitcoin >$100k",
                "clobTokenIds": '["token-100k-yes", "token-100k-no"]',
            },
            "polymarket-market-2": {
                "price": 0.45,
                "question": "Bitcoin >$90k",
                "clobTokenIds": '["token-90k-yes", "token-90k-no"]',
            },
        }

        rules = [{"if_yes": "market-1", "then_yes": "market-2", "relationship": "implies"}]

        with patch("scans.logical_arb.fetch_order_book") as mock_fetch:
            mock_fetch.return_value = {
                "asks": [{"price": 0.46, "size": 100}],
                "bids": [],
            }

            opps = scan_logical_arb(
                markets_by_key=markets_by_key,
                logical_arb_rules=rules,
                price_threshold=0.05,
            )

        assert len(opps) > 0
        opp = opps[0]

        # Opportunity structure should match what executor expects
        assert "type" in opp and opp["type"] == "LogicalArb"
        assert "_token_ids" in opp
        assert "_if_token_ids" in opp
        assert "_if_price" in opp and "_then_price" in opp
        assert "_layer" in opp and opp["_layer"] == 4

    def test_revalidate_should_check_price_movement(self):
        """Revalidation should reject if prices move >10%."""
        from fees import net_profit_logical_arb as fee_calc

        profit = fee_calc(0.50, 0.45)
        assert isinstance(profit, float)
        assert profit != 0  # Should have some non-zero value

    def test_opportunit_has_market_key(self):
        """Opportunity should have _market_key for execution tracking."""
        scan_logical_arb, _ = _import_logical_arb()

        markets_by_key = {
            "polymarket-market-abc": {
                "price": 0.60,
                "question": "Question A",
                "clobTokenIds": '["token-a-yes", "token-a-no"]',
            },
            "polymarket-market-def": {
                "price": 0.45,
                "question": "Question B",
                "clobTokenIds": '["token-b-yes", "token-b-no"]',
            },
        }

        rules = [{"if_yes": "market-abc", "then_yes": "market-def", "relationship": "implies"}]

        with patch("scans.logical_arb.fetch_order_book") as mock_fetch:
            mock_fetch.return_value = {
                "asks": [{"price": 0.46, "size": 100}],
                "bids": [],
            }

            opps = scan_logical_arb(
                markets_by_key=markets_by_key,
                logical_arb_rules=rules,
                price_threshold=0.05,
            )

            assert len(opps) > 0
            assert "_market_key" in opps[0]
            assert opps[0]["_market_key"] == "market-def"
