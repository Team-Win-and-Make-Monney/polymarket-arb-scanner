"""Tests for SX Bet fee calculations and scan modules."""

import pytest
import sys
import os
from unittest.mock import patch, MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from fees import (
    net_profit_sxbet_backall,
    net_profit_sxbet_backlay,
    net_profit_cross_sxbet,
)


class TestSXBetFees:
    def test_backall_profitable(self):
        """Under-round book with 0% commission: full spread is profit."""
        probs = [0.30, 0.30, 0.30]  # Total = 0.90
        result = net_profit_sxbet_backall(probs)
        assert result["gross_spread"] == pytest.approx(0.10)
        assert result["fees"] == 0
        assert result["net_profit"] == pytest.approx(0.10)

    def test_backall_unprofitable(self):
        """Over-round book: sum > 1.0."""
        probs = [0.40, 0.40, 0.30]
        result = net_profit_sxbet_backall(probs)
        assert result["net_profit"] <= 0

    def test_backall_exact_round(self):
        """Exactly 1.0 = no profit."""
        probs = [0.50, 0.30, 0.20]
        result = net_profit_sxbet_backall(probs)
        assert result["net_profit"] <= 0

    def test_backall_no_fees(self):
        """SX Bet always has 0 fees."""
        probs = [0.10, 0.20, 0.30]  # Total = 0.60
        result = net_profit_sxbet_backall(probs)
        assert result["fees"] == 0
        assert result["net_profit"] == pytest.approx(0.40)

    def test_backlay_profitable(self):
        """Crossed book: back_prob < lay_prob. 0% fees."""
        result = net_profit_sxbet_backlay(0.40, 0.50)
        assert result["gross_spread"] == pytest.approx(0.10)
        assert result["fees"] == 0
        assert result["net_profit"] == pytest.approx(0.10)

    def test_backlay_not_crossed(self):
        """Normal book: lay_prob <= back_prob."""
        result = net_profit_sxbet_backlay(0.50, 0.40)
        assert result["net_profit"] == 0

    def test_backlay_equal_prices(self):
        """Equal prices: no profit."""
        result = net_profit_sxbet_backlay(0.50, 0.50)
        assert result["net_profit"] == 0

    def test_cross_sxbet_profitable(self):
        """Cross-platform Poly vs SX Bet with positive spread."""
        result = net_profit_cross_sxbet(0.30, 0.30, "yes", "no")
        assert result["gross_spread"] == pytest.approx(0.40)
        assert result["net_profit"] > 0

    def test_cross_sxbet_unprofitable(self):
        """Cross-platform with total >= 1.0."""
        result = net_profit_cross_sxbet(0.55, 0.50, "yes", "no")
        assert result["net_profit"] <= 0


class TestSXBetBackAllScan:
    def test_finds_underround_book(self):
        from scans.sxbet import scan_sxbet_backall

        mock_client = MagicMock()
        mock_client.authenticated = True
        mock_client.fetch_all_markets.return_value = [
            {
                "marketHash": "0xabc123",
                "title": "Winner",
                "_sport": {"label": "Politics"},
                "outcomes": [
                    {"outcomeId": "o1", "price": 0.25},
                    {"outcomeId": "o2", "price": 0.25},
                ],
            }
        ]
        # Scan now uses batch orderbook fetching (commit 8f98555).
        mock_client.get_orderbooks_batch.return_value = {
            "0xabc123": {
                "bids": [{"price": 0.25, "size": 100}],
                "asks": [{"price": 0.30, "size": 100}],
            }
        }

        opps = scan_sxbet_backall(mock_client, min_profit=0.001)
        assert len(opps) == 1
        assert opps[0]["type"] == "SXBetBackAll"
        assert opps[0]["net_profit"] > 0
        assert opps[0]["fees"] == "$0.0000"  # 0% commission

    def test_skips_overround_book(self):
        from scans.sxbet import scan_sxbet_backall

        mock_client = MagicMock()
        mock_client.authenticated = True
        mock_client.fetch_all_markets.return_value = [
            {
                "marketHash": "0xabc123",
                "title": "Winner",
                "_sport": {"label": "Politics"},
                "outcomes": [
                    {"outcomeId": "o1", "price": 0.60},
                    {"outcomeId": "o2", "price": 0.60},
                ],
            }
        ]
        mock_client.get_orderbooks_batch.return_value = {
            "0xabc123": {
                "bids": [{"price": 0.60, "size": 100}],
                "asks": [],
            }
        }

        opps = scan_sxbet_backall(mock_client, min_profit=0.001)
        assert len(opps) == 0

    def test_not_authenticated(self):
        from scans.sxbet import scan_sxbet_backall

        mock_client = MagicMock()
        mock_client.authenticated = False

        opps = scan_sxbet_backall(mock_client, min_profit=0.001)
        assert len(opps) == 0

    def test_none_client(self):
        from scans.sxbet import scan_sxbet_backall

        opps = scan_sxbet_backall(None, min_profit=0.001)
        assert len(opps) == 0

    def test_skips_single_outcome(self):
        from scans.sxbet import scan_sxbet_backall

        mock_client = MagicMock()
        mock_client.authenticated = True
        mock_client.fetch_all_markets.return_value = [
            {
                "marketHash": "0xabc123",
                "title": "Single",
                "outcomes": [
                    {"outcomeId": "o1", "price": 0.25},
                ],
            }
        ]

        opps = scan_sxbet_backall(mock_client, min_profit=0.001)
        assert len(opps) == 0


class TestSXBetBackLayScan:
    def test_finds_crossed_book(self):
        from scans.sxbet import scan_sxbet_backlay

        mock_client = MagicMock()
        mock_client.authenticated = True
        mock_client.fetch_all_markets.return_value = [
            {
                "marketHash": "0xabc123",
                "title": "Winner",
                "_sport": {"label": "Politics"},
            }
        ]
        # Crossed: bid (0.50) > ask (0.30). Scan now uses batch fetch.
        mock_client.get_orderbooks_batch.return_value = {
            "0xabc123": {
                "bids": [{"price": 0.50, "size": 50}],
                "asks": [{"price": 0.30, "size": 50}],
            }
        }

        opps = scan_sxbet_backlay(mock_client, min_profit=0.001)
        assert len(opps) == 1
        assert opps[0]["type"] == "SXBetBackLay"
        assert opps[0]["net_profit"] > 0
        # back=0.30 (ask), lay=0.50 (bid), spread = 0.20
        assert opps[0]["net_profit"] == pytest.approx(0.20)

    def test_skips_normal_book(self):
        from scans.sxbet import scan_sxbet_backlay

        mock_client = MagicMock()
        mock_client.authenticated = True
        mock_client.fetch_all_markets.return_value = [
            {"marketHash": "0xabc123", "title": "Winner"}
        ]
        # Normal: executable outcome prices sum above one, so lay <= back.
        mock_client.get_orderbooks_batch.return_value = {
            "0xabc123": {
                "bids": [{"price": 0.60, "size": 50}],
                "asks": [{"price": 0.50, "size": 50}],
            }
        }

        opps = scan_sxbet_backlay(mock_client, min_profit=0.001)
        assert len(opps) == 0

    def test_skips_empty_orderbook(self):
        from scans.sxbet import scan_sxbet_backlay

        mock_client = MagicMock()
        mock_client.authenticated = True
        mock_client.fetch_all_markets.return_value = [
            {"marketHash": "0xabc123", "title": "Winner"}
        ]
        mock_client.get_orderbooks_batch.return_value = {
            "0xabc123": {"bids": [], "asks": []}
        }

        opps = scan_sxbet_backlay(mock_client, min_profit=0.001)
        assert len(opps) == 0

    def test_not_authenticated(self):
        from scans.sxbet import scan_sxbet_backlay

        mock_client = MagicMock()
        mock_client.authenticated = False

        opps = scan_sxbet_backlay(mock_client, min_profit=0.001)
        assert len(opps) == 0


class TestSXBetCombinedScan:
    def test_reuses_one_snapshot_for_both_strategies(self):
        from scans.sxbet import scan_sxbet

        mock_client = MagicMock()
        mock_client.authenticated = True
        mock_client.fetch_all_markets.return_value = [
            {"marketHash": "0xabc123", "teamOneName": "A", "teamTwoName": "B"}
        ]
        mock_client.get_orderbooks_batch.return_value = {
            "0xabc123": {
                "bids": [{"price": 0.50, "size": 50}],
                "asks": [{"price": 0.30, "size": 50}],
            }
        }

        backall, backlay = scan_sxbet(mock_client, min_profit=0.001)

        assert len(backall) == 1
        assert len(backlay) == 0
        mock_client.fetch_all_markets.assert_called_once_with()
        mock_client.get_orderbooks_batch.assert_called_once_with(["0xabc123"], batch_size=200)
