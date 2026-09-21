"""Unit tests for scans/ctf.py CTF primitives scan."""

from __future__ import annotations

import os
import sys
from unittest.mock import patch

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scans.ctf import scan_ctf


class TestCTFScan:
    """Test suite for two-stage CTFMerge and CTFMint scan."""

    @pytest.fixture(autouse=True)
    def _ensure_polymarket_funcs(self):
        """Ensure scans.ctf has valid binary parsing functions even if stubbed by prior tests."""
        import scans.ctf as ctf_mod
        orig_gbm = getattr(ctf_mod, "get_binary_markets", None)
        orig_pop = getattr(ctf_mod, "parse_outcome_prices", None)

        def _real_pop(market):
            raw = market.get("outcomePrices")
            if not raw:
                return None
            try:
                import json
                prices = json.loads(raw) if isinstance(raw, str) else raw
                return [float(p) for p in prices]
            except Exception:
                return None

        def _real_gbm(markets):
            binary = []
            for m in markets:
                if m.get("negRisk"):
                    continue
                outcomes = m.get("outcomes")
                if isinstance(outcomes, str):
                    try:
                        import json
                        outcomes = json.loads(outcomes)
                    except Exception:
                        continue
                if outcomes and len(outcomes) == 2:
                    prices = _real_pop(m)
                    if prices and len(prices) == 2:
                        binary.append(m)
            return binary

        ctf_mod.get_binary_markets = _real_gbm
        ctf_mod.parse_outcome_prices = _real_pop
        yield
        if orig_gbm is not None:
            ctf_mod.get_binary_markets = orig_gbm
        if orig_pop is not None:
            ctf_mod.parse_outcome_prices = orig_pop

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
        opps = scan_ctf(markets, min_profit=0.005)

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
        opps = scan_ctf(markets, min_profit=0.005)
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
        merge_only = scan_ctf(markets, min_profit=0.005, enable_merge=True, enable_mint=False)
        assert all(o["type"] == "CTFMerge" for o in merge_only)

        # Only mint
        mint_only = scan_ctf(markets, min_profit=0.005, enable_merge=False, enable_mint=True)
        assert all(o["type"] == "CTFMint" for o in mint_only)
