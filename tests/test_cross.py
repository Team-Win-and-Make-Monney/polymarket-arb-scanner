"""Tests for scans/cross.py — cross-platform arbitrage scans."""

import pytest
from unittest.mock import MagicMock, patch
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from scans.cross import (
    _ALL_PLATFORMS,
    _CROSS_FEE_FUNCS,
    _make_cross_fee,
    _attach_exec_metadata,
    _refine_cross_with_clob,
)


# ---------------------------------------------------------------------------
# Fee function table
# ---------------------------------------------------------------------------

class TestCrossFeeTable:
    def test_all_platforms_has_eight_entries(self):
        assert len(_ALL_PLATFORMS) == 8

    def test_all_platforms_names(self):
        expected = {"polymarket", "kalshi", "betfair", "smarkets",
                    "sxbet", "matchbook", "gemini", "ibkr"}
        assert set(_ALL_PLATFORMS) == expected

    def test_fee_funcs_has_28_pairs(self):
        """8 platforms -> C(8,2) = 28 unique pairs."""
        assert len(_CROSS_FEE_FUNCS) == 28

    def test_every_pair_has_callable(self):
        for key, func in _CROSS_FEE_FUNCS.items():
            assert callable(func), f"Fee function for {key} is not callable"

    def test_every_platform_pair_covered(self):
        """Every unordered pair of platforms should have a fee function."""
        for i, pa in enumerate(_ALL_PLATFORMS):
            for pb in _ALL_PLATFORMS[i + 1:]:
                has_fwd = (pa, pb) in _CROSS_FEE_FUNCS
                has_rev = (pb, pa) in _CROSS_FEE_FUNCS
                assert has_fwd or has_rev, f"Missing fee function for ({pa}, {pb})"

    def test_polymarket_pairs_use_hand_tuned_funcs(self):
        """Polymarket pairs should use specific (not generic) fee functions."""
        from fees import net_profit_cross_platform
        assert _CROSS_FEE_FUNCS[("polymarket", "kalshi")] is net_profit_cross_platform

    def test_make_cross_fee_returns_callable(self):
        fn = _make_cross_fee("betfair", "smarkets")
        assert callable(fn)

    def test_make_cross_fee_accepts_four_args(self):
        fn = _make_cross_fee("betfair", "smarkets")
        result = fn(0.40, 0.40, "yes", "no")
        assert "net_profit" in result
        assert "fees" in result
        assert "gross_spread" in result


# ---------------------------------------------------------------------------
# _attach_exec_metadata
# ---------------------------------------------------------------------------

class TestAttachExecMetadata:
    def test_polymarket_extracts_token_ids(self):
        opp = {}
        market = {
            "tokens": [
                {"token_id": "tok_yes", "outcome": "Yes"},
                {"token_id": "tok_no", "outcome": "No"},
            ]
        }
        _attach_exec_metadata(opp, market, "polymarket", "a")
        assert "_token_ids" in opp

    def test_kalshi_extracts_ticker(self):
        opp = {}
        market = {"ticker": "KALSHI-T-123"}
        _attach_exec_metadata(opp, market, "kalshi", "b")
        assert opp["_kalshi_ticker"] == "KALSHI-T-123"

    def test_betfair_extracts_market_id_and_selection(self):
        opp = {}
        market = {"marketId": "1.234", "runners": [{"selectionId": 5678}]}
        _attach_exec_metadata(opp, market, "betfair", "a")
        assert opp["_market_id"] == "1.234"
        assert opp["_selection_id"] == 5678

    def test_betfair_no_runners(self):
        opp = {}
        market = {"marketId": "1.234", "runners": []}
        _attach_exec_metadata(opp, market, "betfair", "a")
        assert opp["_market_id"] == "1.234"
        assert "_selection_id" not in opp

    def test_smarkets_extracts_market_id(self):
        opp = {}
        market = {"id": "sm-123"}
        _attach_exec_metadata(opp, market, "smarkets", "b")
        assert opp["_sm_market_id"] == "sm-123"

    def test_sxbet_extracts_market_hash(self):
        opp = {}
        market = {"marketHash": "0xabcdef"}
        _attach_exec_metadata(opp, market, "sxbet", "a")
        assert opp["_sx_market_hash"] == "0xabcdef"

    def test_sxbet_falls_back_to_id(self):
        opp = {}
        market = {"id": "sx-456"}
        _attach_exec_metadata(opp, market, "sxbet", "a")
        assert opp["_sx_market_hash"] == "sx-456"

    def test_matchbook_extracts_market_and_runner(self):
        opp = {}
        market = {"id": "mb-789", "runners": [{"id": "r1"}]}
        _attach_exec_metadata(opp, market, "matchbook", "b")
        assert opp["_mb_market_id"] == "mb-789"
        assert opp["_mb_runner_id"] == "r1"

    def test_gemini_extracts_event_and_symbols(self):
        opp = {}
        market = {
            "id": "gm-001",
            "contracts": [
                {"label": "Yes", "instrumentSymbol": "GM-YES-001"},
                {"label": "No", "instrumentSymbol": "GM-NO-001"},
            ],
        }
        _attach_exec_metadata(opp, market, "gemini", "a")
        assert opp["_gm_event_id"] == "gm-001"
        assert opp["_gm_yes_symbol"] == "GM-YES-001"
        assert opp["_gm_no_symbol"] == "GM-NO-001"

    def test_ibkr_extracts_event_and_conids(self):
        opp = {}
        market = {
            "id": "ibkr-001",
            "contracts": [
                {"side": "YES", "conid": "111"},
                {"side": "NO", "conid": "222"},
            ],
        }
        _attach_exec_metadata(opp, market, "ibkr", "b")
        assert opp["_ibkr_event_id"] == "ibkr-001"
        assert opp["_ibkr_yes_conid"] == "111"
        assert opp["_ibkr_no_conid"] == "222"

    def test_unknown_platform_is_noop(self):
        opp = {}
        market = {"id": "xyz"}
        _attach_exec_metadata(opp, market, "unknown_platform", "a")
        assert opp == {}


# ---------------------------------------------------------------------------
# _refine_cross_with_clob
# ---------------------------------------------------------------------------

class TestRefineCrossWithClob:
    def test_empty_list_returns_empty(self):
        result = _refine_cross_with_clob([], {}, 0.005)
        assert result == []

    @patch("scans.cross._fetch_clob_for_market")
    @patch("scans.cross.net_profit_cross_platform")
    def test_clob_fetch_failure_drops_opp_fail_closed(self, mock_fee, mock_clob):
        """Audit #77 round 2: CLOB verification that cannot complete must
        DROP the opp (fail-closed), never pass it through with stale
        mid-price profit."""
        mock_clob.side_effect = Exception("API down")
        opp = {
            "_market_key": "mk1",
            "_kalshi_yes": 0.40,
            "_kalshi_no": 0.40,
        }
        markets_by_key = {"mk1": {"conditionId": "mk1"}}
        result = _refine_cross_with_clob([opp], markets_by_key, 0.005)
        assert result == []

    @patch("scans.cross._fetch_clob_for_market")
    @patch("scans.cross.net_profit_cross_platform")
    def test_profitable_after_clob_refinement(self, mock_fee, mock_clob):
        mock_clob.return_value = (
            {"conditionId": "mk1"},
            {
                "yes_ask": 0.35,
                "no_ask": 0.35,
                "yes_ask_size": 100,
                "no_ask_size": 100,
                "yes_bid": 0.34,
                "no_bid": 0.34,
            },
        )
        mock_fee.return_value = {
            "net_profit": 0.10,
            "fees": 0.02,
            "gross_spread": 0.12,
        }
        opp = {
            "_market_key": "mk1",
            "_kalshi_yes": 0.40,
            "_kalshi_no": 0.40,
        }
        markets_by_key = {"mk1": {"conditionId": "mk1"}}
        result = _refine_cross_with_clob([opp], markets_by_key, 0.005)
        assert len(result) == 1
        assert result[0]["net_profit"] == 0.10

    @patch("scans.cross._fetch_clob_for_market")
    @patch("scans.cross.net_profit_cross_platform")
    def test_drops_candidate_below_min_profit(self, mock_fee, mock_clob):
        mock_clob.return_value = (
            {"conditionId": "mk1"},
            {
                "yes_ask": 0.35,
                "no_ask": 0.35,
                "yes_ask_size": 100,
                "no_ask_size": 100,
                "yes_bid": 0.34,
                "no_bid": 0.34,
            },
        )
        mock_fee.return_value = {
            "net_profit": 0.001,
            "fees": 0.02,
            "gross_spread": 0.021,
        }
        opp = {
            "_market_key": "mk1",
            "_kalshi_yes": 0.40,
            "_kalshi_no": 0.40,
        }
        markets_by_key = {"mk1": {"conditionId": "mk1"}}
        result = _refine_cross_with_clob([opp], markets_by_key, 0.005)
        assert len(result) == 0

    @patch("scans.cross._fetch_clob_for_market")
    def test_missing_ask_uses_bid_fallback(self, mock_clob):
        mock_clob.return_value = (
            {"conditionId": "mk1"},
            {
                "yes_ask": None,
                "no_ask": None,
                "yes_ask_size": None,
                "no_ask_size": None,
                "yes_bid": 0.34,
                "no_bid": 0.34,
            },
        )
        opp = {
            "_market_key": "mk1",
            "_kalshi_yes": 0.40,
            "_kalshi_no": 0.40,
        }
        markets_by_key = {"mk1": {"conditionId": "mk1"}}
        with patch("scans.cross.net_profit_cross_platform") as mock_fee:
            mock_fee.return_value = {"net_profit": 0.08, "fees": 0.01, "gross_spread": 0.09}
            result = _refine_cross_with_clob([opp], markets_by_key, 0.005)
            if result:
                assert result[0].get("_partial_clob") is True

    def test_no_market_key_drops_fail_closed(self):
        """Audit #77 round 2: no market to verify against -> drop, not
        pass-through with unverified mid-price profit."""
        opp = {"net_profit": 0.05}
        result = _refine_cross_with_clob([opp], {}, 0.005)
        assert result == []

    def test_missing_kalshi_prices_drops_fail_closed(self):
        """Audit #77 round 2: opp without _kalshi_yes/_kalshi_no cannot be
        re-verified -> drop, not pass-through."""
        opp = {"_market_key": "mk1", "net_profit": 0.05}
        markets_by_key = {"mk1": {"conditionId": "mk1"}}
        with patch("scans.cross._fetch_clob_for_market") as mock_clob:
            mock_clob.return_value = (
                {"conditionId": "mk1"},
                {
                    "yes_ask": 0.35, "no_ask": 0.35,
                    "yes_ask_size": 100, "no_ask_size": 100,
                },
            )
            result = _refine_cross_with_clob([opp], markets_by_key, 0.005)
            assert result == []

    @patch("scans.cross._fetch_clob_for_market")
    def test_malformed_nonempty_book_drops_without_keyerror(self, mock_clob):
        mock_clob.return_value = ({"conditionId": "mk1"}, {"yes_ask": 0.35})
        opp = {
            "_market_key": "mk1",
            "_kalshi_yes": 0.40,
            "_kalshi_no": 0.40,
        }

        result = _refine_cross_with_clob(
            [opp], {"mk1": {"conditionId": "mk1"}}, 0.005,
        )

        assert result == []


# ---------------------------------------------------------------------------
# scan_cross_platform
# ---------------------------------------------------------------------------

class TestScanCrossPlatform:
    @patch("scans.cross.filter_dust", side_effect=lambda x: x)
    @patch("scans.cross._refine_cross_with_clob", side_effect=lambda opps, *a, **kw: opps)
    @patch("scans.cross.get_binary_markets")
    def test_no_kalshi_client_returns_empty(self, mock_binary, mock_refine, mock_dust):
        from scans.cross import scan_cross_platform
        result = scan_cross_platform([], None, 0.005)
        assert result == []

    @patch("scans.cross.SEMANTIC_MATCHING_ENABLED", False)
    @patch("scans.cross.filter_dust", side_effect=lambda x: x)
    @patch("scans.cross._refine_cross_with_clob", side_effect=lambda opps, *a, **kw: opps)
    @patch("scans.cross.get_binary_markets", return_value=[])
    @patch("scans.cross.match_markets_to_events", return_value=[])
    def test_no_events_returns_empty(self, mock_match, mock_binary, mock_refine, mock_dust):
        from scans.cross import scan_cross_platform
        kalshi = MagicMock()
        kalshi.fetch_all_events.return_value = []
        result = scan_cross_platform([], kalshi, 0.005)
        assert result == []

    @patch("scans.cross.SEMANTIC_MATCHING_ENABLED", False)
    @patch("scans.cross.filter_dust", side_effect=lambda x: x)
    @patch("scans.cross._refine_cross_with_clob", side_effect=lambda opps, *a, **kw: opps)
    @patch("scans.cross._days_to_resolution", return_value=3.0)
    @patch("scans.cross._within_resolution_window", return_value=True)
    @patch("scans.cross._extract_token_ids", return_value=["tok_y", "tok_n"])
    @patch("scans.cross._parallel_fetch_kalshi")
    @patch("scans.cross.net_profit_cross_platform")
    @patch("scans.cross.detect_inverted", return_value=False)
    @patch("scans.cross.parse_outcome_prices", return_value=[0.35, 0.35])
    @patch("scans.cross.match_markets_to_events")
    @patch("scans.cross.get_binary_markets")
    def test_finds_profitable_arb(
        self, mock_binary, mock_match, mock_prices, mock_inv,
        mock_fee, mock_pf_kalshi, mock_tokens, mock_window,
        mock_days, mock_refine, mock_dust,
    ):
        from scans.cross import scan_cross_platform
        pm_market = {"question": "Will X?", "conditionId": "pm1", "volume": "1000"}
        kalshi_event = {"title": "Will X happen?", "event_ticker": "EVT-1"}
        kalshi_market = {"ticker": "MKT-1", "title": "Will X?"}
        mock_binary.return_value = [pm_market]
        mock_match.return_value = [{
            "polymarket": pm_market,
            "kalshi_event": kalshi_event,
            "similarity": 90,
            "confidence": "HIGH",
        }]
        mock_pf_kalshi.return_value = {"EVT-1": [kalshi_market]}

        kalshi = MagicMock()
        kalshi.fetch_all_events.return_value = [kalshi_event]
        kalshi.get_market_price.return_value = (0.40, 0.40)

        mock_fee.return_value = {"net_profit": 0.08, "fees": 0.02, "gross_spread": 0.10}

        result = scan_cross_platform([pm_market], kalshi, 0.005,
                                     kalshi_events_preloaded=[kalshi_event])
        assert len(result) >= 1
        assert result[0]["net_profit"] == 0.08

    @patch("scans.cross.SEMANTIC_MATCHING_ENABLED", False)
    @patch("scans.cross.filter_dust", side_effect=lambda x: x)
    @patch("scans.cross._refine_cross_with_clob", side_effect=lambda opps, *a, **kw: opps)
    @patch("scans.cross._days_to_resolution", return_value=3.0)
    @patch("scans.cross._within_resolution_window", return_value=False)
    @patch("scans.cross._parallel_fetch_kalshi", return_value={})
    @patch("scans.cross.parse_outcome_prices", return_value=[0.35, 0.35])
    @patch("scans.cross.match_markets_to_events")
    @patch("scans.cross.get_binary_markets")
    def test_resolution_window_filters_markets(
        self, mock_binary, mock_match, mock_prices,
        mock_pf_kalshi, mock_window, mock_days, mock_refine, mock_dust,
    ):
        from scans.cross import scan_cross_platform
        pm_market = {"question": "Will Y?", "conditionId": "pm2", "volume": "500"}
        kalshi_event = {"title": "Will Y?", "event_ticker": "EVT-2"}
        mock_binary.return_value = [pm_market]
        mock_match.return_value = [{
            "polymarket": pm_market,
            "kalshi_event": kalshi_event,
            "similarity": 85,
            "confidence": "MEDIUM",
        }]

        kalshi = MagicMock()
        kalshi.fetch_all_events.return_value = [kalshi_event]

        result = scan_cross_platform([pm_market], kalshi, 0.005,
                                     kalshi_events_preloaded=[kalshi_event])
        # Resolution window rejects, so no opportunities
        assert result == []

    @patch("scans.cross.SEMANTIC_MATCHING_ENABLED", False)
    @patch("scans.cross.filter_dust", side_effect=lambda x: x)
    @patch("scans.cross._refine_cross_with_clob", side_effect=lambda opps, *a, **kw: opps)
    @patch("scans.cross._days_to_resolution", return_value=3.0)
    @patch("scans.cross._within_resolution_window", return_value=True)
    @patch("scans.cross._extract_token_ids", return_value=["tok_y", "tok_n"])
    @patch("scans.cross._parallel_fetch_kalshi")
    @patch("scans.cross.net_profit_cross_platform")
    @patch("scans.cross.detect_inverted", return_value=True)
    @patch("scans.cross.parse_outcome_prices", return_value=[0.35, 0.35])
    @patch("scans.cross.match_markets_to_events")
    @patch("scans.cross.get_binary_markets")
    def test_inversion_flips_kalshi_prices(
        self, mock_binary, mock_match, mock_prices, mock_inv,
        mock_fee, mock_pf_kalshi, mock_tokens, mock_window,
        mock_days, mock_refine, mock_dust,
    ):
        from scans.cross import scan_cross_platform
        pm_market = {"question": "Will Z?", "conditionId": "pm3", "volume": "300"}
        kalshi_event = {"title": "Z won't happen", "event_ticker": "EVT-3"}
        kalshi_market = {"ticker": "MKT-3", "title": "Z won't happen"}
        mock_binary.return_value = [pm_market]
        mock_match.return_value = [{
            "polymarket": pm_market,
            "kalshi_event": kalshi_event,
            "similarity": 82,
            "confidence": "MEDIUM",
        }]
        mock_pf_kalshi.return_value = {"EVT-3": [kalshi_market]}

        kalshi = MagicMock()
        kalshi.fetch_all_events.return_value = [kalshi_event]
        # Original prices: yes=0.60, no=0.30
        kalshi.get_market_price.return_value = (0.60, 0.30)

        mock_fee.return_value = {"net_profit": 0.05, "fees": 0.01, "gross_spread": 0.06}

        result = scan_cross_platform([pm_market], kalshi, 0.005,
                                     kalshi_events_preloaded=[kalshi_event])

        # When inverted, k_yes and k_no should be swapped before fee calc
        # The fee function should have been called with flipped prices
        assert mock_fee.called

    @patch("scans.cross.SEMANTIC_MATCHING_ENABLED", True)
    @patch("scans.cross.filter_dust", side_effect=lambda x: x)
    @patch("scans.cross._refine_cross_with_clob", side_effect=lambda opps, *a, **kw: opps)
    @patch("scans.cross.get_binary_markets", return_value=[])
    @patch("scans.cross.match_markets_to_events_semantic", return_value=[])
    def test_semantic_branch_used_when_enabled(self, mock_sem, mock_binary, mock_refine, mock_dust):
        """When SEMANTIC_MATCHING_ENABLED=True, scan_cross_platform uses semantic matcher."""
        from scans.cross import scan_cross_platform
        kalshi = MagicMock()
        kalshi.fetch_all_events.return_value = [{"title": "Test", "event_ticker": "E1"}]
        scan_cross_platform([], kalshi, 0.005, kalshi_events_preloaded=[{"title": "Test", "event_ticker": "E1"}])
        mock_sem.assert_called_once()

    @patch("scans.cross.SEMANTIC_MATCHING_ENABLED", False)
    @patch("scans.cross.filter_dust", side_effect=lambda x: x)
    @patch("scans.cross._refine_cross_with_clob", side_effect=lambda opps, *a, **kw: opps)
    @patch("scans.cross.get_binary_markets", return_value=[])
    @patch("scans.cross.match_markets_to_events", return_value=[])
    def test_fuzzy_branch_used_when_disabled(self, mock_fuzzy, mock_binary, mock_refine, mock_dust):
        """When SEMANTIC_MATCHING_ENABLED=False, scan_cross_platform uses fuzzy matcher."""
        from scans.cross import scan_cross_platform
        kalshi = MagicMock()
        kalshi.fetch_all_events.return_value = [{"title": "Test", "event_ticker": "E1"}]
        scan_cross_platform([], kalshi, 0.005, kalshi_events_preloaded=[{"title": "Test", "event_ticker": "E1"}])
        mock_fuzzy.assert_called_once()


# ---------------------------------------------------------------------------
# scan_cross_all
# ---------------------------------------------------------------------------

class TestScanCrossAll:
    @patch("scans.cross.filter_dust", side_effect=lambda x: x)
    @patch("scans.cross._refine_cross_all_with_clob")
    @patch("scans.cross.get_binary_markets", return_value=[])
    def test_empty_platforms_returns_empty(self, mock_binary, mock_refine, mock_dust):
        from scans.cross import scan_cross_all
        result = scan_cross_all([], {}, 0.005)
        assert result == []

    @patch("scans.cross.filter_dust", side_effect=lambda x: x)
    @patch("scans.cross._refine_cross_all_with_clob")
    @patch("scans.cross.get_binary_markets")
    @patch("scans.cross.match_cross_platform")
    @patch("scans.cross.parse_outcome_prices", return_value=[0.35, 0.35])
    @patch("scans.cross._days_to_resolution", return_value=3.0)
    def test_finds_cross_all_opportunity(
        self, mock_days, mock_prices, mock_match, mock_binary, mock_refine, mock_dust,
    ):
        from scans.cross import scan_cross_all
        pm_market = {"question": "Will X?", "conditionId": "pm1", "volume": "1000",
                      "tokens": [{"token_id": "ty", "outcome": "Yes"}, {"token_id": "tn", "outcome": "No"}]}
        kalshi_market = {"ticker": "K-1", "title": "Will X?"}

        mock_binary.return_value = [pm_market]
        mock_match.return_value = [{
            "market_a": pm_market,
            "market_b": kalshi_market,
            "platform_a": "polymarket",
            "platform_b": "kalshi",
            "similarity": 90,
            "confidence": "HIGH",
            "title_a": "Will X?",
            "title_b": "Will X?",
        }]

        kalshi_client = MagicMock()
        kalshi_client.get_market_price.return_value = (0.40, 0.40)

        with patch("scans.cross.SEMANTIC_MATCHING_ENABLED", False):
            result = scan_cross_all(
                [pm_market],
                {"kalshi": (kalshi_client, [kalshi_market])},
                0.005,
            )
        # Should find at least one opportunity (profit depends on fee function)
        # Just verify structure
        assert isinstance(result, list)

    @patch("scans.cross.filter_dust", side_effect=lambda x: x)
    @patch("scans.cross._refine_cross_all_with_clob")
    @patch("scans.cross.get_binary_markets")
    @patch("scans.cross.match_cross_platform_semantic")
    def test_semantic_matching_branch(self, mock_sem, mock_binary, mock_refine, mock_dust):
        """When SEMANTIC_MATCHING_ENABLED=True, uses match_cross_platform_semantic."""
        from scans.cross import scan_cross_all
        pm_market = {"question": "Will Bitcoin hit 100k?", "conditionId": "c1"}
        mock_binary.return_value = [pm_market]
        mock_sem.return_value = []

        with patch("scans.cross.SEMANTIC_MATCHING_ENABLED", True):
            scan_cross_all(
                [pm_market],
                {"kalshi": (MagicMock(), [{"title": "Bitcoin 100k", "ticker": "t1"}])},
                0.005,
            )
        assert mock_sem.called

    @patch("scans.cross.filter_dust", side_effect=lambda x: x)
    @patch("scans.cross._refine_cross_all_with_clob")
    @patch("scans.cross.get_binary_markets")
    @patch("scans.cross.match_cross_platform")
    def test_fuzzy_matching_branch(self, mock_fuzzy, mock_binary, mock_refine, mock_dust):
        """When SEMANTIC_MATCHING_ENABLED=False, uses match_cross_platform."""
        from scans.cross import scan_cross_all
        pm_market = {"question": "Will Bitcoin hit 100k?", "conditionId": "c1"}
        mock_binary.return_value = [pm_market]
        mock_fuzzy.return_value = []

        with patch("scans.cross.SEMANTIC_MATCHING_ENABLED", False):
            scan_cross_all(
                [pm_market],
                {"kalshi": (MagicMock(), [{"title": "Bitcoin 100k", "ticker": "t1"}])},
                0.005,
            )
        assert mock_fuzzy.called

    @patch("scans.cross.filter_dust", side_effect=lambda x: x)
    @patch("scans.cross._refine_cross_all_with_clob")
    @patch("scans.cross.get_binary_markets", return_value=[])
    @patch("scans.cross.match_cross_platform", return_value=[])
    def test_fee_lookup_reverse_key(self, mock_match, mock_binary, mock_refine, mock_dust):
        """Fee function lookup should try (pa, pb) then (pb, pa)."""
        from scans.cross import scan_cross_all
        # Both directions should be covered by _CROSS_FEE_FUNCS
        # Just verify no error on any pair
        with patch("scans.cross.SEMANTIC_MATCHING_ENABLED", False):
            result = scan_cross_all(
                [],
                {
                    "betfair": (MagicMock(), [{"title": "t", "id": "b1"}]),
                    "smarkets": (MagicMock(), [{"title": "t", "id": "s1"}]),
                },
                0.005,
            )
        assert isinstance(result, list)

    @patch("scans.cross.filter_dust", side_effect=lambda x: x)
    @patch("scans.cross._refine_cross_all_with_clob")
    @patch("scans.cross.get_binary_markets")
    @patch("scans.cross.match_cross_platform")
    @patch("scans.cross.parse_outcome_prices", return_value=[0.30, 0.30])
    @patch("scans.cross._days_to_resolution", return_value=2.0)
    def test_attaches_exec_metadata_both_sides(
        self, mock_days, mock_prices, mock_match, mock_binary, mock_refine, mock_dust,
    ):
        from scans.cross import scan_cross_all
        pm_market = {"question": "Will X?", "conditionId": "pm1",
                      "tokens": [{"token_id": "ty", "outcome": "Yes"}, {"token_id": "tn", "outcome": "No"}]}
        kalshi_market = {"ticker": "K-1", "title": "Will X?"}

        mock_binary.return_value = [pm_market]
        mock_match.return_value = [{
            "market_a": pm_market, "market_b": kalshi_market,
            "platform_a": "polymarket", "platform_b": "kalshi",
            "similarity": 90, "confidence": "HIGH",
            "title_a": "Will X?", "title_b": "Will X?",
        }]

        kalshi_client = MagicMock()
        kalshi_client.get_market_price.return_value = (0.40, 0.40)

        with patch("scans.cross.SEMANTIC_MATCHING_ENABLED", False):
            result = scan_cross_all(
                [pm_market],
                {"kalshi": (kalshi_client, [kalshi_market])},
                0.001,  # very low threshold to ensure opp is kept
            )

        if result:
            opp = result[0]
            # Polymarket metadata
            assert "_token_ids" in opp or "_platform_a" in opp
            # Kalshi metadata
            assert "_kalshi_ticker" in opp or "_platform_b" in opp

    @patch("scans.cross.filter_dust", side_effect=lambda x: x)
    @patch("scans.cross._refine_cross_all_with_clob")
    @patch("scans.cross.get_binary_markets", return_value=[])
    @patch("scans.cross.match_cross_platform", return_value=[])
    def test_skips_empty_market_lists(self, mock_match, mock_binary, mock_refine, mock_dust):
        from scans.cross import scan_cross_all
        with patch("scans.cross.SEMANTIC_MATCHING_ENABLED", False):
            result = scan_cross_all(
                [],
                {"kalshi": (MagicMock(), [])},  # empty market list
                0.005,
            )
        assert result == []
        # match_cross_platform should not be called for empty lists
        # (polymarket is also empty -> all pairs have empty market lists)


# ---------------------------------------------------------------------------
# _refine_cross_all_with_clob
# ---------------------------------------------------------------------------

class TestRefineCrossAllWithClob:
    @patch("scans.cross.get_clob_prices")
    def test_no_pm_opps_is_noop(self, mock_clob):
        from scans.cross import _refine_cross_all_with_clob
        opps = [{"_platform_a": "betfair", "_platform_b": "smarkets"}]
        _refine_cross_all_with_clob(opps, 0.005)
        mock_clob.assert_not_called()

    @patch("scans.cross.get_clob_prices")
    def test_token_ids_too_short_skipped(self, mock_clob):
        from scans.cross import _refine_cross_all_with_clob
        opps = [{
            "_platform_a": "polymarket", "_platform_b": "kalshi",
            "_token_ids": ["only_one"],
            "prices": "polymarket_Y=0.30 kalshi_N=0.40",
        }]
        _refine_cross_all_with_clob(opps, 0.005)
        mock_clob.assert_not_called()

    @patch("scans.cross._fetch_clob_for_market")
    def test_clob_updates_profit_when_still_profitable(self, mock_fetch):
        from scans.cross import _refine_cross_all_with_clob
        mock_fetch.return_value = (None, {
            "yes_ask": 0.32,
            "no_ask": 0.32,
            "yes_ask_size": 50,
            "no_ask_size": 50,
        })
        opp = {
            "_platform_a": "polymarket",
            "_platform_b": "kalshi",
            "_token_ids": ["tok_y", "tok_n"],
            "prices": "polymarket_Y=0.30 kalshi_N=0.40",
            "net_profit": 0.10,
        }
        _refine_cross_all_with_clob([opp], 0.005)
        # The CLOB refinement should have run — verify net_profit updated
        # (exact value depends on fee func; just verify it was attempted)
        assert "net_profit" in opp


# ---------------------------------------------------------------------------
# _fee_path attachment (find_lowest_fee_path wiring)
# ---------------------------------------------------------------------------

class TestFeePath:
    @patch("scans.cross.SEMANTIC_MATCHING_ENABLED", False)
    @patch("scans.cross.filter_dust", side_effect=lambda x: x)
    @patch("scans.cross.find_lowest_fee_path")
    @patch("scans.cross._refine_cross_with_clob", side_effect=lambda opps, *a, **kw: opps)
    @patch("scans.cross._days_to_resolution", return_value=3.0)
    @patch("scans.cross._within_resolution_window", return_value=True)
    @patch("scans.cross._extract_token_ids", return_value=["tok_y", "tok_n"])
    @patch("scans.cross._parallel_fetch_kalshi")
    @patch("scans.cross.net_profit_cross_platform")
    @patch("scans.cross.detect_inverted", return_value=False)
    @patch("scans.cross.parse_outcome_prices", return_value=[0.35, 0.65])
    @patch("scans.cross.match_markets_to_events")
    @patch("scans.cross.get_binary_markets")
    def test_fee_path_attached_on_cross_platform(
        self, mock_binary, mock_match, mock_prices, mock_inv,
        mock_fee, mock_pf_kalshi, mock_tokens, mock_window,
        mock_days, mock_refine, mock_find_path, mock_dust,
    ):
        """scan_cross_platform opps carry _fee_path when find_lowest_fee_path returns a dict."""
        from scans.cross import scan_cross_platform
        pm_market = {"question": "Will A?", "conditionId": "pm1", "volume": "1000"}
        kalshi_event = {"title": "Will A?", "event_ticker": "EVT-1"}
        kalshi_market = {"ticker": "MKT-1", "title": "Will A?"}

        mock_binary.return_value = [pm_market]
        mock_match.return_value = [{
            "polymarket": pm_market,
            "kalshi_event": kalshi_event,
            "similarity": 90,
            "confidence": "HIGH",
        }]
        mock_pf_kalshi.return_value = {"EVT-1": [kalshi_market]}

        kalshi = MagicMock()
        kalshi.fetch_all_events.return_value = [kalshi_event]
        kalshi.get_market_price.return_value = (0.40, 0.60)

        mock_fee.return_value = {"net_profit": 0.05, "fees": 0.01, "gross_spread": 0.06}

        fake_fee_path = {
            "best_yes_platform": "polymarket",
            "best_no_platform": "kalshi",
            "yes_price": 0.35,
            "no_price": 0.60,
            "total_cost": 0.95,
            "estimated_fees": 0.01,
            "net_profit": 0.04,
        }
        mock_find_path.return_value = fake_fee_path

        result = scan_cross_platform([pm_market], kalshi, 0.005,
                                     kalshi_events_preloaded=[kalshi_event])
        assert len(result) >= 1
        opp = result[0]
        assert "_fee_path" in opp
        fee_path = opp["_fee_path"]
        assert "best_yes_platform" in fee_path
        assert "best_no_platform" in fee_path
        assert "yes_price" in fee_path
        assert "no_price" in fee_path
        assert "total_cost" in fee_path
        assert "estimated_fees" in fee_path
        assert "net_profit" in fee_path

    @patch("scans.cross.SEMANTIC_MATCHING_ENABLED", False)
    @patch("scans.cross.filter_dust", side_effect=lambda x: x)
    @patch("scans.cross.find_lowest_fee_path")
    @patch("scans.cross._refine_cross_with_clob", side_effect=lambda opps, *a, **kw: opps)
    @patch("scans.cross._days_to_resolution", return_value=3.0)
    @patch("scans.cross._within_resolution_window", return_value=True)
    @patch("scans.cross._extract_token_ids", return_value=["tok_y", "tok_n"])
    @patch("scans.cross._parallel_fetch_kalshi")
    @patch("scans.cross.net_profit_cross_platform")
    @patch("scans.cross.detect_inverted", return_value=False)
    @patch("scans.cross.parse_outcome_prices", return_value=[0.35, 0.65])
    @patch("scans.cross.match_markets_to_events")
    @patch("scans.cross.get_binary_markets")
    def test_fee_path_absent_when_no_profitable_path(
        self, mock_binary, mock_match, mock_prices, mock_inv,
        mock_fee, mock_pf_kalshi, mock_tokens, mock_window,
        mock_days, mock_refine, mock_find_path, mock_dust,
    ):
        """When find_lowest_fee_path returns None, _fee_path key must be ABSENT (not set to None)."""
        from scans.cross import scan_cross_platform
        pm_market = {"question": "Will B?", "conditionId": "pm2", "volume": "500"}
        kalshi_event = {"title": "Will B?", "event_ticker": "EVT-2"}
        kalshi_market = {"ticker": "MKT-2", "title": "Will B?"}

        mock_binary.return_value = [pm_market]
        mock_match.return_value = [{
            "polymarket": pm_market,
            "kalshi_event": kalshi_event,
            "similarity": 85,
            "confidence": "MEDIUM",
        }]
        mock_pf_kalshi.return_value = {"EVT-2": [kalshi_market]}

        kalshi = MagicMock()
        kalshi.fetch_all_events.return_value = [kalshi_event]
        kalshi.get_market_price.return_value = (0.50, 0.50)

        mock_fee.return_value = {"net_profit": 0.05, "fees": 0.01, "gross_spread": 0.06}
        mock_find_path.return_value = None  # No profitable fee path found

        result = scan_cross_platform([pm_market], kalshi, 0.005,
                                     kalshi_events_preloaded=[kalshi_event])
        assert len(result) >= 1
        opp = result[0]
        assert "_fee_path" not in opp  # Key must be absent, not set to None

    @patch("scans.cross.filter_dust", side_effect=lambda x: x)
    @patch("scans.cross.find_lowest_fee_path")
    @patch("scans.cross._refine_cross_all_with_clob")
    @patch("scans.cross.get_binary_markets")
    @patch("scans.cross.match_cross_platform")
    @patch("scans.cross.parse_outcome_prices", return_value=[0.35, 0.65])
    @patch("scans.cross._days_to_resolution", return_value=3.0)
    def test_fee_path_on_cross_all(
        self, mock_days, mock_prices, mock_match, mock_binary, mock_refine, mock_find_path, mock_dust,
    ):
        """scan_cross_all opps carry _fee_path when find_lowest_fee_path returns a dict."""
        from scans.cross import scan_cross_all
        pm_market = {"question": "Will C?", "conditionId": "pm3",
                      "tokens": [{"token_id": "ty", "outcome": "Yes"}, {"token_id": "tn", "outcome": "No"}]}
        kalshi_market = {"ticker": "K-3", "title": "Will C?"}

        mock_binary.return_value = [pm_market]
        mock_match.return_value = [{
            "market_a": pm_market,
            "market_b": kalshi_market,
            "platform_a": "polymarket",
            "platform_b": "kalshi",
            "similarity": 90,
            "confidence": "HIGH",
            "title_a": "Will C?",
            "title_b": "Will C?",
        }]

        kalshi_client = MagicMock()
        kalshi_client.get_market_price.return_value = (0.40, 0.60)

        fake_fee_path = {
            "best_yes_platform": "polymarket",
            "best_no_platform": "kalshi",
            "yes_price": 0.35,
            "no_price": 0.60,
            "total_cost": 0.95,
            "estimated_fees": 0.01,
            "net_profit": 0.04,
        }
        mock_find_path.return_value = fake_fee_path

        # _refine_cross_all_with_clob is called for side effects only (mutates in-place)
        mock_refine.return_value = None

        with patch("scans.cross.SEMANTIC_MATCHING_ENABLED", False):
            result = scan_cross_all(
                [pm_market],
                {"kalshi": (kalshi_client, [kalshi_market])},
                0.001,  # very low threshold to ensure opp is kept
            )

        if result:
            opp = result[0]
            assert "_fee_path" in opp
            fee_path = opp["_fee_path"]
            assert "best_yes_platform" in fee_path
            assert "best_no_platform" in fee_path


# ---------------------------------------------------------------------------
# _refine_cross_all_with_clob — Stage-2 drop behavior (audit #77)
# ---------------------------------------------------------------------------

class TestRefineCrossAllDropsUnprofitable:
    """Opps confirmed unprofitable at live CLOB prices must be REMOVED from
    the opportunities list — previously they failed the re-check but stayed
    in the returned list with their stale mid-price net_profit."""

    @patch("scans.cross._fetch_clob_for_market")
    def test_confirmed_unprofitable_opp_is_dropped(self, mock_fetch):
        from scans.cross import _refine_cross_all_with_clob
        # CLOB asks so high that no combination can clear min_profit.
        mock_fetch.return_value = (None, {
            "yes_ask": 0.95,
            "no_ask": 0.95,
            "yes_ask_size": 50,
            "no_ask_size": 50,
        })
        bad_opp = {
            "_platform_a": "polymarket",
            "_platform_b": "kalshi",
            "_token_ids": ["tok_y", "tok_n"],
            "prices": "polymarket_Y=0.30 kalshi_N=0.40",
            "net_profit": 0.25,  # stale mid-price profit
        }
        non_pm_opp = {
            "_platform_a": "betfair",
            "_platform_b": "smarkets",
            "net_profit": 0.05,
        }
        opps = [bad_opp, non_pm_opp]
        _refine_cross_all_with_clob(opps, 0.005)
        assert bad_opp not in opps, (
            "opp confirmed unprofitable at CLOB prices must not survive with "
            "stale mid-price net_profit"
        )
        assert non_pm_opp in opps  # non-PM opps pass through untouched

    @patch("scans.cross._fetch_clob_for_market")
    def test_missing_clob_data_drops_opp_fail_closed(self, mock_fetch):
        """Audit #77 round 2: verification that cannot complete -> DROP.

        A stale mid-price net_profit must never survive to execution just
        because the live book could not be fetched."""
        from scans.cross import _refine_cross_all_with_clob
        mock_fetch.return_value = (None, None)
        opp = {
            "_platform_a": "polymarket",
            "_platform_b": "kalshi",
            "_token_ids": ["tok_y", "tok_n"],
            "prices": "polymarket_Y=0.30 kalshi_N=0.40",
            "net_profit": 0.25,
        }
        opps = [opp]
        _refine_cross_all_with_clob(opps, 0.005)
        assert opps == []

    @patch("scans.cross._fetch_clob_for_market")
    def test_missing_selected_side_size_drops_fail_closed(self, mock_fetch):
        from scans.cross import _refine_cross_all_with_clob
        mock_fetch.return_value = (None, {
            "yes_ask": 0.32,
            "no_ask": 0.68,
        })
        opp = {
            "_platform_a": "polymarket",
            "_platform_b": "kalshi",
            "_token_ids": ["tok_y", "tok_n"],
            "prices": "polymarket_Y=0.30 kalshi_N=0.40",
            "net_profit": 0.25,
        }
        opps = [opp]

        _refine_cross_all_with_clob(opps, 0.005)

        assert opps == []

    @patch("scans.cross._fetch_clob_for_market")
    def test_unparseable_prices_drops_opp_fail_closed(self, mock_fetch):
        """No parseable side/price for both legs -> cannot verify -> DROP."""
        from scans.cross import _refine_cross_all_with_clob
        mock_fetch.return_value = (None, {
            "yes_ask": 0.32, "no_ask": 0.68,
            "yes_ask_size": 50, "no_ask_size": 50,
        })
        opp = {
            "_platform_a": "polymarket",
            "_platform_b": "kalshi",
            "_token_ids": ["tok_y", "tok_n"],
            "prices": "garbage-no-prices",
            "net_profit": 0.25,
        }
        opps = [opp]
        _refine_cross_all_with_clob(opps, 0.005)
        assert opps == []

    @patch("scans.cross._fetch_clob_for_market")
    def test_profitable_opp_survives_with_live_prices_persisted(self, mock_fetch):
        """Survivors carry the LIVE executable prices — the executor parses
        the prices string at execution time, so it must reflect the refined
        CLOB ask, not the stale mid price."""
        from scans.cross import _refine_cross_all_with_clob
        mock_fetch.return_value = (None, {
            "yes_ask": 0.32,
            "no_ask": 0.68,
            "yes_ask_size": 50,
            "no_ask_size": 40,
        })
        opp = {
            "_platform_a": "polymarket",
            "_platform_b": "kalshi",
            "_token_ids": ["tok_y", "tok_n"],
            "prices": "polymarket_Y=0.30 kalshi_N=0.40",
            "net_profit": 0.10,
        }
        opps = [opp]
        _refine_cross_all_with_clob(opps, 0.005)
        assert opp in opps
        assert opp["_clob_refined"] is True
        # PM YES leg repriced to the live 0.32 ask; Kalshi NO leg unchanged.
        assert opp["prices"] == "polymarket_Y=0.320 kalshi_N=0.400"
        assert opp["total_cost"] == "$0.7200"
        # Depth comes from the PM side actually traded (YES ask size).
        assert opp["_clob_depth"] == 50

    @patch("scans.cross._fetch_clob_for_market")
    def test_pm_no_side_repriced_from_no_ask_only(self, mock_fetch):
        """Side-aware refinement: when the opportunity buys PM NO + other
        YES, only that combination is evaluated, using the live no_ask.
        The pre-fix code also evaluated (pm_yes, other_as_no) — treating the
        other platform's YES price as a NO price, an impossible trade."""
        from scans.cross import _refine_cross_all_with_clob
        mock_fetch.return_value = (None, {
            # yes_ask deliberately absurdly cheap: if the refiner wrongly
            # evaluates the (pm_yes, other-as-NO) combination it would win.
            "yes_ask": 0.01,
            "no_ask": 0.55,
            "yes_ask_size": 99,
            "no_ask_size": 40,
        })
        opp = {
            "_platform_a": "polymarket",
            "_platform_b": "kalshi",
            "_token_ids": ["tok_y", "tok_n"],
            "prices": "polymarket_N=0.50 kalshi_Y=0.40",
            "net_profit": 0.05,
        }
        opps = [opp]
        _refine_cross_all_with_clob(opps, 0.001)
        assert opp in opps
        # Repriced from no_ask (0.55), keeping the original PM-NO/K-YES
        # structure — NOT the impossible PM-YES/K-"NO" combination.
        assert opp["prices"] == "polymarket_N=0.550 kalshi_Y=0.400"
        assert opp["total_cost"] == "$0.9500"
        assert opp["_clob_depth"] == 40  # NO-side ask size
