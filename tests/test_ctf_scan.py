"""Unit tests for scans/ctf.py CTF primitives scan."""

from __future__ import annotations

import os
import sys
from unittest.mock import patch

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import scans.ctf as ctf_mod


class TestCTFScan:
    """Test suite for two-stage CTFMerge and CTFMint scan."""

    @pytest.fixture(autouse=True)
    def _bind_real_polymarket_helpers(self, monkeypatch) -> None:
        """Ensure scans.ctf uses production polymarket_api helpers even if ambient sys.modules is mocked."""
        import polymarket_api
        monkeypatch.setattr(ctf_mod, "get_binary_markets", polymarket_api.get_binary_markets)
        monkeypatch.setattr(ctf_mod, "parse_outcome_prices", polymarket_api.parse_outcome_prices)

    def _sample_markets(self) -> list[dict]:
        from datetime import datetime, timezone, timedelta
        future_date = (datetime.now(timezone.utc) + timedelta(days=2)).isoformat()
        return [
            {
                "id": "1",
                "conditionId": "0x1111111111111111111111111111111111111111111111111111111111111111",
                "question": "Will candidate A win the election?",
                "outcomes": '["Yes", "No"]',
                "outcomePrices": '["0.42", "0.46"]',
                "clobTokenIds": '["1001", "1002"]',
                "endDateIso": future_date,
                "volume": "50000",
            },
            {
                "id": "2",
                "conditionId": "0x2222222222222222222222222222222222222222222222222222222222222222",
                "question": "Will candidate B win the election?",
                "outcomes": '["Yes", "No"]',
                "outcomePrices": '["0.56", "0.54"]',
                "clobTokenIds": '["2001", "2002"]',
                "endDateIso": future_date,
                "volume": "75000",
            },
            {
                "id": "3",
                "conditionId": "0x3333333333333333333333333333333333333333333333333333333333333333",
                "question": "Expired market from 2023",
                "outcomes": '["Yes", "No"]',
                "outcomePrices": '["0.40", "0.40"]',
                "clobTokenIds": '["3001", "3002"]',
                "endDateIso": "2023-01-01T00:00:00Z",
                "volume": "1000",
            },
        ]

    @patch("scans.ctf._fetch_clob_for_market")
    def test_scan_ctf_detects_merge_and_mint(self, mock_fetch_clob) -> None:
        """Verify scan_ctf identifies both CTFMerge and CTFMint opportunities."""
        # Market 1: Merge candidate (0.42 + 0.46 = 0.88)
        # Market 2: Mint candidate (0.56 + 0.54 = 1.10)
        def mock_clob_impl(m, cache=None):
            cid = m.get("conditionId")
            if cid.endswith("1111"):
                return m, {
                    "yes_ask": 0.43,
                    "yes_ask_size": 200,
                    "no_ask": 0.47,
                    "no_ask_size": 250,
                    "yes_bid": 0.41,
                    "yes_bid_size": 150,
                    "no_bid": 0.45,
                    "no_bid_size": 180,
                }
            elif cid.endswith("2222"):
                return m, {
                    "yes_ask": 0.58,
                    "yes_ask_size": 100,
                    "no_ask": 0.56,
                    "no_ask_size": 120,
                    "yes_bid": 0.55,
                    "yes_bid_size": 300,
                    "no_bid": 0.53,
                    "no_bid_size": 350,
                }
            return m, None

        mock_fetch_clob.side_effect = mock_clob_impl

        markets = self._sample_markets()
        opps = ctf_mod.scan_ctf(markets, min_profit=0.005)

        # Should find 2 opportunities (Market 1 Merge and Market 2 Mint; Market 3 expired)
        types = [o["type"] for o in opps]
        assert "CTFMerge" in types
        assert "CTFMint" in types

        merge_opp = next(o for o in opps if o["type"] == "CTFMerge")
        assert merge_opp["_clob_depth"] == 200
        assert merge_opp["_action"] == "merge"
        assert merge_opp["net_profit"] > 0.005

        mint_opp = next(o for o in opps if o["type"] == "CTFMint")
        assert mint_opp["_clob_depth"] == 300
        assert mint_opp["_action"] == "mint"
        assert mint_opp["net_profit"] > 0.005

    @patch("scans.ctf._fetch_clob_for_market")
    def test_refine_drops_unprofitable_clob(self, mock_fetch_clob) -> None:
        """Verify refinement drops candidates when CLOB prices do not yield profit."""
        # Market 1 had mid prices 0.42 + 0.46, but CLOB asks are 0.55 + 0.50 (1.05)
        def mock_clob_impl(m, cache=None):
            return m, {
                "yes_ask": 0.55,
                "yes_ask_size": 200,
                "no_ask": 0.50,
                "no_ask_size": 250,
                "yes_bid": 0.40,
                "yes_bid_size": 100,
                "no_bid": 0.40,
                "no_bid_size": 100,
            }

        mock_fetch_clob.side_effect = mock_clob_impl

        markets = [self._sample_markets()[0]]
        opps = ctf_mod.scan_ctf(markets, min_profit=0.005)
        assert len(opps) == 0

    @patch("scans.ctf._fetch_clob_for_market")
    def test_flag_overrides(self, mock_fetch_clob) -> None:
        """Verify enable_merge and enable_mint explicitly filter opportunity types."""
        mock_fetch_clob.side_effect = lambda m, c=None: (
            m,
            {
                "yes_ask": 0.43, "yes_ask_size": 100,
                "no_ask": 0.47, "no_ask_size": 100,
                "yes_bid": 0.55, "yes_bid_size": 100,
                "no_bid": 0.53, "no_bid_size": 100,
            },
        )

        markets = self._sample_markets()

        # Only merge
        merge_only = ctf_mod.scan_ctf(markets, min_profit=0.005, enable_merge=True, enable_mint=False)
        assert all(o["type"] == "CTFMerge" for o in merge_only)

        # Only mint
        mint_only = ctf_mod.scan_ctf(markets, min_profit=0.005, enable_merge=False, enable_mint=True)
        assert all(o["type"] == "CTFMint" for o in mint_only)
