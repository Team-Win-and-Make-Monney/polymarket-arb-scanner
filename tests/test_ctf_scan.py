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

    def test_is_ctf_market_eligible(self) -> None:
        """Verify _is_ctf_market_eligible allows unexpired future markets when max_days=0 and rejects expired."""
        from datetime import datetime, timezone, timedelta
        now = datetime.now(timezone.utc)
        in_30_days = (now + timedelta(days=30)).isoformat()
        in_3_days = (now + timedelta(days=3)).isoformat()
        in_past = (now - timedelta(days=1)).isoformat()

        # 1. 30 days in future: eligible when max_days=0
        m_far = {"endDateIso": in_30_days, "closed": False, "active": True}
        assert ctf_mod._is_ctf_market_eligible(m_far, max_days=0) is True

        # 2. 30 days in future: NOT eligible when max_days=7
        assert ctf_mod._is_ctf_market_eligible(m_far, max_days=7) is False

        # 3. 3 days in future: eligible when max_days=7
        m_near = {"endDateIso": in_3_days, "closed": False, "active": True}
        assert ctf_mod._is_ctf_market_eligible(m_near, max_days=7) is True

        # 4. Past date: rejected even when max_days=0
        m_expired = {"endDateIso": in_past, "closed": False, "active": True}
        assert ctf_mod._is_ctf_market_eligible(m_expired, max_days=0) is False

        # 5. Closed market: rejected
        m_closed = {"endDateIso": in_3_days, "closed": True, "active": True}
        assert ctf_mod._is_ctf_market_eligible(m_closed, max_days=0) is False

        # 6. Inactive market: rejected
        m_inactive = {"endDateIso": in_3_days, "closed": False, "active": False}
        assert ctf_mod._is_ctf_market_eligible(m_inactive, max_days=0) is False

        # 7. Missing or invalid date: rejected
        assert ctf_mod._is_ctf_market_eligible({"closed": False, "active": True}, max_days=0) is False
        assert ctf_mod._is_ctf_market_eligible({"endDateIso": "not-a-date", "closed": False}, max_days=0) is False

    def test_scan_ctf_ws_price_cache_detects_merge(self) -> None:
        """Verify scan_ctf detects CTFMerge from live WS price_cache even when mid-prices sum to 1.0."""
        import time
        from datetime import datetime, timezone, timedelta
        future_date = (datetime.now(timezone.utc) + timedelta(days=20)).isoformat()

        # Mid-prices sum to 1.0 (no mid-price arb)
        market = {
            "id": "10",
            "conditionId": "0xcccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc",
            "question": "Will Congress pass the budget bill?",
            "outcomes": '["Yes", "No"]',
            "outcomePrices": '["0.50", "0.50"]',
            "clobTokenIds": '["tok_yes_1", "tok_no_1"]',
            "endDateIso": future_date,
            "volume": "100000",
        }

        # WebSocket price cache has executable ask prices: 0.44 + 0.45 = 0.89 (< 1.00)
        now = time.time()
        price_cache = {
            ("polymarket", "tok_yes_1"): {
                "best_ask": 0.44,
                "best_ask_size": 150,
                "best_bid": 0.42,
                "best_bid_size": 100,
                "_ts": now,
            },
            ("polymarket", "tok_no_1"): {
                "best_ask": 0.45,
                "best_ask_size": 220,
                "best_bid": 0.43,
                "best_bid_size": 110,
                "_ts": now,
            },
        }

        opps = ctf_mod.scan_ctf([market], min_profit=0.005, price_cache=price_cache)
        assert len(opps) == 1
        opp = opps[0]
        assert opp["type"] == "CTFMerge"
        assert opp["_price_source"] == "ws_cache"
        assert opp["_clob_depth"] == 150  # min(150, 220)
        assert opp["_clob_refined"] is True
        assert opp["net_profit"] > 0.005
        assert "Y=0.440 N=0.450" in opp["prices"]

    def test_scan_ctf_ws_price_cache_detects_mint(self) -> None:
        """Verify scan_ctf detects CTFMint from live WS price_cache even when mid-prices sum to 1.0."""
        import time
        from datetime import datetime, timezone, timedelta
        future_date = (datetime.now(timezone.utc) + timedelta(days=20)).isoformat()

        # Mid-prices sum to 1.0 (no mid-price arb)
        market = {
            "id": "11",
            "conditionId": "0xdddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd",
            "question": "Will NASA launch Artemis II this year?",
            "outcomes": '["Yes", "No"]',
            "outcomePrices": '["0.50", "0.50"]',
            "clobTokenIds": '["tok_yes_2", "tok_no_2"]',
            "endDateIso": future_date,
            "volume": "100000",
        }

        # WebSocket price cache has executable bid prices: 0.54 + 0.55 = 1.09 (> 1.00)
        now = time.time()
        price_cache = {
            ("polymarket", "tok_yes_2"): {
                "best_ask": 0.56,
                "best_ask_size": 100,
                "best_bid": 0.54,
                "best_bid_size": 320,
                "_ts": now,
            },
            ("polymarket", "tok_no_2"): {
                "best_ask": 0.57,
                "best_ask_size": 100,
                "best_bid": 0.55,
                "best_bid_size": 280,
                "_ts": now,
            },
        }

        opps = ctf_mod.scan_ctf([market], min_profit=0.005, price_cache=price_cache)
        assert len(opps) == 1
        opp = opps[0]
        assert opp["type"] == "CTFMint"
        assert opp["_price_source"] == "ws_cache"
        assert opp["_clob_depth"] == 280  # min(320, 280)
        assert opp["_clob_refined"] is True
        assert opp["net_profit"] > 0.005
        assert "Y=0.540 N=0.550" in opp["prices"]

    def test_scan_ctf_stale_ws_cache_falls_back_to_mid(self) -> None:
        """Verify stale WS cache (> 30s) is ignored and falls back to mid-prices."""
        import time
        from datetime import datetime, timezone, timedelta
        future_date = (datetime.now(timezone.utc) + timedelta(days=2)).isoformat()

        # Mid-prices sum to 1.0 (no arb)
        market = {
            "id": "12",
            "conditionId": "0xeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee",
            "question": "Will company X hit earnings?",
            "outcomes": '["Yes", "No"]',
            "outcomePrices": '["0.50", "0.50"]',
            "clobTokenIds": '["tok_yes_3", "tok_no_3"]',
            "endDateIso": future_date,
            "volume": "100000",
        }

        # Cache entry is 45s old (stale)
        stale_ts = time.time() - 45.0
        price_cache = {
            ("polymarket", "tok_yes_3"): {
                "best_ask": 0.44, "best_ask_size": 150, "best_bid": 0.42, "best_bid_size": 100, "_ts": stale_ts,
            },
            ("polymarket", "tok_no_3"): {
                "best_ask": 0.45, "best_ask_size": 220, "best_bid": 0.43, "best_bid_size": 110, "_ts": stale_ts,
            },
        }

        opps = ctf_mod.scan_ctf([market], min_profit=0.005, price_cache=price_cache)
        # Should be empty because mid-prices (0.50 + 0.50 = 1.0) do not yield arb and cache was stale
        assert len(opps) == 0

    @patch("scans.ctf._fetch_clob_for_market")
    def test_scan_ctf_incomplete_ws_quotes_falls_back_to_mid_prices(self, mock_fetch_clob) -> None:
        """Verify incomplete WS quotes fall back to REST mid-prices rather than dropping candidate."""
        mock_fetch_clob.side_effect = lambda m, c=None: (
            m,
            {
                "yes_ask": 0.43, "yes_ask_size": 100,
                "no_ask": 0.47, "no_ask_size": 100,
                "yes_bid": 0.41, "yes_bid_size": 100,
                "no_bid": 0.45, "no_bid_size": 100,
            },
        )
        import time
        from datetime import datetime, timezone, timedelta
        future_date = (datetime.now(timezone.utc) + timedelta(days=2)).isoformat()

        # Market has profitable mid-prices: 0.42 + 0.46 = 0.88 (< 1.0)
        market = {
            "id": "13",
            "conditionId": "0xffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff",
            "question": "Will candidate C win?",
            "outcomes": '["Yes", "No"]',
            "outcomePrices": '["0.42", "0.46"]',
            "clobTokenIds": '["tok_yes_4", "tok_no_4"]',
            "endDateIso": future_date,
            "volume": "100000",
        }

        # Cache has YES ask, but NO ask is missing (incomplete for merge)
        now = time.time()
        price_cache = {
            ("polymarket", "tok_yes_4"): {
                "best_ask": 0.44, "best_ask_size": 100, "_ts": now,
            },
            ("polymarket", "tok_no_4"): {
                # missing best_ask
                "best_bid": 0.40, "best_bid_size": 100, "_ts": now,
            },
        }

        opps = ctf_mod.scan_ctf([market], min_profit=0.005, price_cache=price_cache, enable_merge=True, enable_mint=False)
        assert len(opps) == 1
        assert opps[0]["type"] == "CTFMerge"
        assert opps[0]["net_profit"] > 0.005
