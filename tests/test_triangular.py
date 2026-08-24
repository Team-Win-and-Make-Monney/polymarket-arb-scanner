"""Tests for triangular cross-platform arbitrage scan module."""

import importlib
import sys
import os
from unittest.mock import patch, MagicMock

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _import_triangular():
    """Force reimport of scans.triangular to pick up current mocks/state."""
    mod_name = "scans.triangular"
    if mod_name in sys.modules:
        return importlib.reload(sys.modules[mod_name])
    return importlib.import_module(mod_name)


class TestGetMarketPrices:
    """Tests for _get_market_prices()."""

    def test_polymarket_returns_yes_no(self):
        """Polymarket prices extracted via parse_outcome_prices."""
        mod = _import_triangular()
        with patch.object(mod, "parse_outcome_prices", return_value=[0.65, 0.35]) as mock_parse:
            market = {"outcomePrices": "[0.65, 0.35]"}
            yes, no = mod._get_market_prices(market, "polymarket")

            assert yes == 0.65
            assert no == 0.35
            mock_parse.assert_called_once_with(market)

    def test_polymarket_returns_none_when_no_prices(self):
        """Returns (None, None) when parse_outcome_prices returns None."""
        mod = _import_triangular()
        with patch.object(mod, "parse_outcome_prices", return_value=None):
            market = {}
            yes, no = mod._get_market_prices(market, "polymarket")

            assert yes is None
            assert no is None

    def test_polymarket_returns_none_when_single_outcome(self):
        """Returns (None, None) when only one price is available."""
        mod = _import_triangular()
        with patch.object(mod, "parse_outcome_prices", return_value=[0.50]):
            market = {"outcomePrices": "[0.50]"}
            yes, no = mod._get_market_prices(market, "polymarket")

            assert yes is None
            assert no is None

    def test_kalshi_with_client(self):
        """Kalshi prices extracted via client.get_market_price()."""
        mod = _import_triangular()
        mock_client = MagicMock()
        mock_client.get_market_price.return_value = (0.70, 0.30)
        market = {"ticker": "KTEST-01"}

        yes, no = mod._get_market_prices(market, "kalshi", client=mock_client)

        assert yes == 0.70
        assert no == 0.30
        mock_client.get_market_price.assert_called_once_with(market)

    def test_other_platform_with_client(self):
        """Any non-Polymarket platform uses client.get_market_price()."""
        mod = _import_triangular()
        mock_client = MagicMock()
        mock_client.get_market_price.return_value = (0.55, 0.45)
        market = {"id": "bf123"}

        yes, no = mod._get_market_prices(market, "betfair", client=mock_client)

        assert yes == 0.55
        assert no == 0.45

    def test_returns_none_without_client(self):
        """Returns (None, None) when no client is provided for non-Polymarket."""
        mod = _import_triangular()
        market = {"ticker": "KTEST-01"}

        yes, no = mod._get_market_prices(market, "kalshi", client=None)

        assert yes is None
        assert no is None

    def test_returns_none_on_client_exception(self):
        """Returns (None, None) when client raises an exception."""
        mod = _import_triangular()
        mock_client = MagicMock()
        mock_client.get_market_price.side_effect = Exception("API timeout")
        market = {"id": "sm123"}

        yes, no = mod._get_market_prices(market, "smarkets", client=mock_client)

        assert yes is None
        assert no is None


class TestGroupCrossMatches:
    """Tests for _group_cross_matches()."""

    def test_groups_three_platforms(self):
        """Three pairwise matches for the same market yield one group with 3 platforms."""
        mod = _import_triangular()
        matches = [
            {
                "market_a": {"conditionId": "pm1", "question": "Will X happen?"},
                "market_b": {"ticker": "KTEST-01", "title": "Will X happen?"},
                "platform_a": "polymarket",
                "platform_b": "kalshi",
                "similarity": 95,
                "entity_overlap": 5,
                "confidence": "HIGH",
                "title_a": "Will X happen?",
                "title_b": "Will X happen?",
            },
            {
                "market_a": {"conditionId": "pm1", "question": "Will X happen?"},
                "market_b": {"id": "bf1", "name": "Will X happen?"},
                "platform_a": "polymarket",
                "platform_b": "betfair",
                "similarity": 92,
                "entity_overlap": 5,
                "confidence": "HIGH",
                "title_a": "Will X happen?",
                "title_b": "Will X happen?",
            },
            {
                "market_a": {"ticker": "KTEST-01", "title": "Will X happen?"},
                "market_b": {"id": "bf1", "name": "Will X happen?"},
                "platform_a": "kalshi",
                "platform_b": "betfair",
                "similarity": 90,
                "entity_overlap": 5,
                "confidence": "HIGH",
                "title_a": "Will X happen?",
                "title_b": "Will X happen?",
            },
        ]

        groups = mod._group_cross_matches(matches)

        assert len(groups) == 1
        title = list(groups.keys())[0]
        group = groups[title]
        assert "polymarket" in group
        assert "kalshi" in group
        assert "betfair" in group

    def test_excludes_two_platform_groups(self):
        """Groups with only 2 platforms are excluded."""
        mod = _import_triangular()
        matches = [
            {
                "market_a": {"conditionId": "pm2", "question": "Will Y happen?"},
                "market_b": {"ticker": "KTEST-02", "title": "Will Y happen?"},
                "platform_a": "polymarket",
                "platform_b": "kalshi",
                "similarity": 90,
                "entity_overlap": 4,
                "confidence": "HIGH",
                "title_a": "Will Y happen?",
                "title_b": "Will Y happen?",
            },
        ]

        groups = mod._group_cross_matches(matches)

        assert len(groups) == 0

    def test_empty_matches(self):
        """Empty input returns empty dict."""
        mod = _import_triangular()
        groups = mod._group_cross_matches([])
        assert groups == {}

    def test_multiple_separate_markets(self):
        """Multiple distinct markets each on 3 platforms produce separate groups."""
        mod = _import_triangular()
        matches = [
            # Market A across 3 platforms
            {
                "market_a": {"conditionId": "pm_a", "question": "Market Alpha"},
                "market_b": {"ticker": "K_A", "title": "Market Alpha"},
                "platform_a": "polymarket",
                "platform_b": "kalshi",
                "similarity": 95,
                "entity_overlap": 4,
                "confidence": "HIGH",
                "title_a": "Market Alpha",
                "title_b": "Market Alpha",
            },
            {
                "market_a": {"conditionId": "pm_a", "question": "Market Alpha"},
                "market_b": {"id": "bf_a", "name": "Market Alpha"},
                "platform_a": "polymarket",
                "platform_b": "betfair",
                "similarity": 93,
                "entity_overlap": 4,
                "confidence": "HIGH",
                "title_a": "Market Alpha",
                "title_b": "Market Alpha",
            },
            {
                "market_a": {"ticker": "K_A", "title": "Market Alpha"},
                "market_b": {"id": "bf_a", "name": "Market Alpha"},
                "platform_a": "kalshi",
                "platform_b": "betfair",
                "similarity": 91,
                "entity_overlap": 4,
                "confidence": "HIGH",
                "title_a": "Market Alpha",
                "title_b": "Market Alpha",
            },
            # Market B across 3 platforms
            {
                "market_a": {"conditionId": "pm_b", "question": "Market Beta"},
                "market_b": {"ticker": "K_B", "title": "Market Beta"},
                "platform_a": "polymarket",
                "platform_b": "kalshi",
                "similarity": 95,
                "entity_overlap": 4,
                "confidence": "HIGH",
                "title_a": "Market Beta",
                "title_b": "Market Beta",
            },
            {
                "market_a": {"conditionId": "pm_b", "question": "Market Beta"},
                "market_b": {"id": "bf_b", "name": "Market Beta"},
                "platform_a": "polymarket",
                "platform_b": "betfair",
                "similarity": 93,
                "entity_overlap": 4,
                "confidence": "HIGH",
                "title_a": "Market Beta",
                "title_b": "Market Beta",
            },
            {
                "market_a": {"ticker": "K_B", "title": "Market Beta"},
                "market_b": {"id": "bf_b", "name": "Market Beta"},
                "platform_a": "kalshi",
                "platform_b": "betfair",
                "similarity": 91,
                "entity_overlap": 4,
                "confidence": "HIGH",
                "title_a": "Market Beta",
                "title_b": "Market Beta",
            },
        ]

        groups = mod._group_cross_matches(matches)

        assert len(groups) == 2
        for title, group in groups.items():
            assert len(group) >= 3

    def test_transitive_grouping(self):
        """Markets linked transitively through pairwise matches are grouped together."""
        mod = _import_triangular()
        # PM-Kalshi match and Kalshi-Betfair match (no direct PM-Betfair match)
        # should still group all 3 via the shared Kalshi market
        matches = [
            {
                "market_a": {"conditionId": "pm1", "question": "Transitive Test"},
                "market_b": {"ticker": "KT1", "title": "Transitive Test"},
                "platform_a": "polymarket",
                "platform_b": "kalshi",
                "similarity": 95,
                "entity_overlap": 4,
                "confidence": "HIGH",
                "title_a": "Transitive Test",
                "title_b": "Transitive Test",
            },
            {
                "market_a": {"ticker": "KT1", "title": "Transitive Test"},
                "market_b": {"id": "bf1", "name": "Transitive Test"},
                "platform_a": "kalshi",
                "platform_b": "betfair",
                "similarity": 90,
                "entity_overlap": 4,
                "confidence": "HIGH",
                "title_a": "Transitive Test",
                "title_b": "Transitive Test",
            },
        ]

        groups = mod._group_cross_matches(matches)

        assert len(groups) == 1
        group = list(groups.values())[0]
        assert "polymarket" in group
        assert "kalshi" in group
        assert "betfair" in group


class TestScanTriangular:
    """Tests for scan_triangular()."""

    def _setup_three_platform_mocks(self, mod, pm_market, k_market, bf_market):
        """Return a match_side_effect function for 3 platforms."""
        pm_market.setdefault("clobTokenIds", "[\"pm-yes\", \"pm-no\"]")
        def match_side_effect(ma, mb, pa, pb, **kwargs):
            pairs = {
                ("betfair", "kalshi"): (bf_market, k_market),
                ("betfair", "polymarket"): (bf_market, pm_market),
                ("kalshi", "betfair"): (k_market, bf_market),
                ("kalshi", "polymarket"): (k_market, pm_market),
                ("polymarket", "betfair"): (pm_market, bf_market),
                ("polymarket", "kalshi"): (pm_market, k_market),
            }
            key = (pa, pb)
            if key in pairs:
                ma_out, mb_out = pairs[key]
                from matcher import _get_title
                return [{
                    "market_a": ma_out, "market_b": mb_out,
                    "platform_a": pa, "platform_b": pb,
                    "similarity": 95, "entity_overlap": 5, "confidence": "HIGH",
                    "title_a": _get_title(ma_out), "title_b": _get_title(mb_out),
                }]
            return []
        return match_side_effect

    def test_finds_arb_across_three_platforms(self):
        """Finds arb when cheapest YES + cheapest NO < 1.0 across 3 platforms."""
        mod = _import_triangular()

        pm_market = {"conditionId": "pm1", "question": "Test Market", "outcomePrices": "[0.30, 0.60]"}
        k_market = {"ticker": "KT1", "title": "Test Market"}
        bf_market = {"id": "bf1", "name": "Test Market"}

        mock_kalshi = MagicMock()
        mock_kalshi.get_market_price.return_value = (0.40, 0.35)
        mock_betfair = MagicMock()
        mock_betfair.get_market_price.return_value = (0.50, 0.40)

        mock_fee_result = {"gross_spread": 0.35, "fees": 0.05, "net_profit": 0.30}

        with patch.object(mod, "match_cross_platform", side_effect=self._setup_three_platform_mocks(mod, pm_market, k_market, bf_market)), \
             patch.object(mod, "filter_dust", side_effect=lambda x: x), \
             patch.object(mod, "net_profit_triangular", return_value=mock_fee_result), \
             patch.object(mod, "get_clob_prices", return_value={"yes_ask": 0.30, "no_ask": 0.60,
                                                                  "yes_ask_size": 100, "no_ask_size": 100}), \
             patch.object(mod, "parse_outcome_prices", return_value=[0.30, 0.60]):

            opps = mod.scan_triangular(
                {"polymarket": [pm_market], "kalshi": [k_market], "betfair": [bf_market]},
                {"kalshi": mock_kalshi, "betfair": mock_betfair},
                min_profit=0.001,
            )

        assert len(opps) == 1
        assert opps[0]["type"] == "TriangularCross"
        assert opps[0]["net_profit"] == 0.30

    def test_returns_empty_when_no_arbs(self):
        """Returns empty list when no 3-way arbs exist (total cost >= 1.0)."""
        mod = _import_triangular()

        pm_market = {"conditionId": "pm1", "question": "Test Market", "outcomePrices": "[0.55, 0.50]"}
        k_market = {"ticker": "KT1", "title": "Test Market"}
        bf_market = {"id": "bf1", "name": "Test Market"}

        mock_kalshi = MagicMock()
        mock_kalshi.get_market_price.return_value = (0.55, 0.50)
        mock_betfair = MagicMock()
        mock_betfair.get_market_price.return_value = (0.60, 0.55)

        with patch.object(mod, "match_cross_platform", side_effect=self._setup_three_platform_mocks(mod, pm_market, k_market, bf_market)), \
             patch.object(mod, "filter_dust", side_effect=lambda x: x), \
             patch.object(mod, "net_profit_triangular") as mock_fee, \
             patch.object(mod, "parse_outcome_prices", return_value=[0.55, 0.50]):

            opps = mod.scan_triangular(
                {"polymarket": [pm_market], "kalshi": [k_market], "betfair": [bf_market]},
                {"kalshi": mock_kalshi, "betfair": mock_betfair},
                min_profit=0.001,
            )

        assert len(opps) == 0

    def test_handles_empty_platform_markets(self):
        """Returns empty list when platform_markets is empty."""
        mod = _import_triangular()
        opps = mod.scan_triangular({}, {}, min_profit=0.001)
        assert opps == []

    def test_requires_three_platforms(self):
        """Returns empty list when fewer than 3 platforms provided."""
        mod = _import_triangular()
        platform_markets = {
            "polymarket": [{"conditionId": "pm1"}],
            "kalshi": [{"ticker": "K1"}],
        }
        opps = mod.scan_triangular(platform_markets, {}, min_profit=0.001)
        assert opps == []

    def test_skips_when_only_two_platforms_have_data(self):
        """Returns empty when 3 platform keys exist but one is empty."""
        mod = _import_triangular()
        platform_markets = {
            "polymarket": [{"conditionId": "pm1"}],
            "kalshi": [{"ticker": "K1"}],
            "betfair": [],
        }
        opps = mod.scan_triangular(platform_markets, {}, min_profit=0.001)
        assert opps == []

    def test_opportunity_dict_format(self):
        """Opportunity dict has correct keys: type, prices, _platform_a, _platform_b, etc."""
        mod = _import_triangular()

        pm_market = {"conditionId": "pm1", "question": "Format Check Market", "outcomePrices": "[0.25, 0.65]"}
        k_market = {"ticker": "KT1", "title": "Format Check Market"}
        bf_market = {"id": "bf1", "name": "Format Check Market"}

        mock_kalshi = MagicMock()
        mock_kalshi.get_market_price.return_value = (0.35, 0.30)
        mock_betfair = MagicMock()
        mock_betfair.get_market_price.return_value = (0.45, 0.40)

        mock_fee_result = {"gross_spread": 0.40, "fees": 0.03, "net_profit": 0.37}

        with patch.object(mod, "match_cross_platform", side_effect=self._setup_three_platform_mocks(mod, pm_market, k_market, bf_market)), \
             patch.object(mod, "filter_dust", side_effect=lambda x: x), \
             patch.object(mod, "net_profit_triangular", return_value=mock_fee_result), \
             patch.object(mod, "get_clob_prices", return_value={"yes_ask": 0.25, "no_ask": 0.65,
                                                                  "yes_ask_size": 100, "no_ask_size": 100}), \
             patch.object(mod, "parse_outcome_prices", return_value=[0.25, 0.65]):

            opps = mod.scan_triangular(
                {"polymarket": [pm_market], "kalshi": [k_market], "betfair": [bf_market]},
                {"kalshi": mock_kalshi, "betfair": mock_betfair},
                min_profit=0.001,
            )

        assert len(opps) == 1
        opp = opps[0]

        # Required keys
        assert opp["type"] == "TriangularCross"
        assert "market" in opp
        assert "prices" in opp
        assert "total_cost" in opp
        assert "gross_spread" in opp
        assert "fees" in opp
        assert "net_profit" in opp
        assert "net_roi" in opp
        assert "confidence" in opp
        assert "_platform_a" in opp
        assert "_platform_b" in opp
        assert "_platforms_checked" in opp
        assert "_clob_depth" in opp

        # The best YES is polymarket (0.25) and best NO is kalshi (0.30)
        assert opp["_platform_a"] == "polymarket"
        assert opp["_platform_b"] == "kalshi"
        assert len(opp["_platforms_checked"]) == 3

        # Prices string format
        assert "_Y=" in opp["prices"]
        assert "_N=" in opp["prices"]

        # Total cost format
        assert opp["total_cost"].startswith("$")

        # Net ROI format
        assert opp["net_roi"].endswith("%")

    def test_selects_cheapest_yes_and_no_across_platforms(self):
        """Picks the cheapest YES from one platform and cheapest NO from another."""
        mod = _import_triangular()

        # Prices: PM YES=0.20 NO=0.80, K YES=0.60 NO=0.25, BF YES=0.50 NO=0.50
        # Best: PM YES=0.20 + K NO=0.25 = 0.45
        pm_market = {"conditionId": "pm1", "question": "Best Price Test"}
        k_market = {"ticker": "KT1", "title": "Best Price Test"}
        bf_market = {"id": "bf1", "name": "Best Price Test"}

        mock_kalshi = MagicMock()
        mock_kalshi.get_market_price.return_value = (0.60, 0.25)
        mock_betfair = MagicMock()
        mock_betfair.get_market_price.return_value = (0.50, 0.50)

        mock_fee_result = {"gross_spread": 0.55, "fees": 0.04, "net_profit": 0.51}

        with patch.object(mod, "match_cross_platform", side_effect=self._setup_three_platform_mocks(mod, pm_market, k_market, bf_market)), \
             patch.object(mod, "filter_dust", side_effect=lambda x: x), \
             patch.object(mod, "net_profit_triangular", return_value=mock_fee_result) as mock_fee, \
             patch.object(mod, "get_clob_prices", return_value={"yes_ask": 0.20, "no_ask": 0.80,
                                                                  "yes_ask_size": 100, "no_ask_size": 100}), \
             patch.object(mod, "parse_outcome_prices", return_value=[0.20, 0.80]):

            opps = mod.scan_triangular(
                {"polymarket": [pm_market], "kalshi": [k_market], "betfair": [bf_market]},
                {"kalshi": mock_kalshi, "betfair": mock_betfair},
                min_profit=0.001,
            )

        assert len(opps) == 1
        # Best YES = polymarket (0.20), Best NO = kalshi (0.25)
        assert opps[0]["_platform_a"] == "polymarket"
        assert opps[0]["_platform_b"] == "kalshi"
        # Verify fee function was called with correct prices
        mock_fee.assert_called_with(0.20, 0.25, "polymarket", "kalshi")

    def test_skips_below_min_profit(self):
        """Skips opportunities where net_profit < min_profit."""
        mod = _import_triangular()

        pm_market = {"conditionId": "pm1", "question": "Low Profit Test"}
        k_market = {"ticker": "KT1", "title": "Low Profit Test"}
        bf_market = {"id": "bf1", "name": "Low Profit Test"}

        mock_kalshi = MagicMock()
        mock_kalshi.get_market_price.return_value = (0.48, 0.47)
        mock_betfair = MagicMock()
        mock_betfair.get_market_price.return_value = (0.49, 0.48)

        # Net profit of 0.001 is below min_profit of 0.01
        mock_fee_result = {"gross_spread": 0.05, "fees": 0.049, "net_profit": 0.001}

        with patch.object(mod, "match_cross_platform", side_effect=self._setup_three_platform_mocks(mod, pm_market, k_market, bf_market)), \
             patch.object(mod, "filter_dust", side_effect=lambda x: x), \
             patch.object(mod, "net_profit_triangular", return_value=mock_fee_result), \
             patch.object(mod, "parse_outcome_prices", return_value=[0.47, 0.48]):

            opps = mod.scan_triangular(
                {"polymarket": [pm_market], "kalshi": [k_market], "betfair": [bf_market]},
                {"kalshi": mock_kalshi, "betfair": mock_betfair},
                min_profit=0.01,
            )

        assert len(opps) == 0

    def test_no_pairwise_matches(self):
        """Returns empty when no pairwise matches exist across platforms."""
        mod = _import_triangular()

        with patch.object(mod, "SEMANTIC_MATCHING_ENABLED", False), \
             patch.object(mod, "match_cross_platform", return_value=[]):
            opps = mod.scan_triangular(
                {
                    "polymarket": [{"conditionId": "pm1", "question": "Unmatched A"}],
                    "kalshi": [{"ticker": "K1", "title": "Unmatched B"}],
                    "betfair": [{"id": "bf1", "name": "Unmatched C"}],
                },
                {},
                min_profit=0.001,
            )

        assert opps == []

    def test_attaches_execution_metadata(self):
        """Execution metadata is attached for both YES and NO side platforms."""
        mod = _import_triangular()

        pm_market = {
            "conditionId": "pm1",
            "question": "Metadata Test",
            "clobTokenIds": '["tok_yes", "tok_no"]',
        }
        k_market = {"ticker": "KTEST-01", "title": "Metadata Test"}
        bf_market = {"id": "bf1", "name": "Metadata Test"}

        mock_kalshi = MagicMock()
        mock_kalshi.get_market_price.return_value = (0.50, 0.25)
        mock_betfair = MagicMock()
        mock_betfair.get_market_price.return_value = (0.50, 0.50)

        mock_fee_result = {"gross_spread": 0.40, "fees": 0.03, "net_profit": 0.37}

        with patch.object(mod, "match_cross_platform", side_effect=self._setup_three_platform_mocks(mod, pm_market, k_market, bf_market)), \
             patch.object(mod, "filter_dust", side_effect=lambda x: x), \
             patch.object(mod, "net_profit_triangular", return_value=mock_fee_result), \
             patch.object(mod, "get_clob_prices", return_value={"yes_ask": 0.20, "no_ask": 0.80,
                                                                  "yes_ask_size": 100, "no_ask_size": 100}), \
             patch.object(mod, "parse_outcome_prices", return_value=[0.20, 0.80]):

            opps = mod.scan_triangular(
                {"polymarket": [pm_market], "kalshi": [k_market], "betfair": [bf_market]},
                {"kalshi": mock_kalshi, "betfair": mock_betfair},
                min_profit=0.001,
            )

        assert len(opps) == 1
        opp = opps[0]

        # Polymarket is YES side (cheapest YES=0.20)
        assert "_token_ids" in opp
        assert opp["_token_ids"] == ["tok_yes", "tok_no"]

        # Kalshi is NO side (cheapest NO=0.25)
        assert "_kalshi_ticker" in opp
        assert opp["_kalshi_ticker"] == "KTEST-01"


# ---------------------------------------------------------------------------
# Refinement metadata (audit #77): opp dicts must carry _price_a/_price_b and
# _side_a/_side_b so _refine_triangular_with_clob can reprice the PM leg
# against the OTHER leg's real price. Without them, other_price defaulted to
# 0 and the PM leg was always treated as the YES side.
# ---------------------------------------------------------------------------

class TestRefinementMetadata(TestScanTriangular):
    def _scan(self, mod, pm_prices, k_prices, bf_prices, fee_func):
        pm_market = {"conditionId": "pm1", "question": "Meta Market",
                     "outcomePrices": "[0.25, 0.65]",
                     "clobTokenIds": '["tok_yes","tok_no"]'}
        k_market = {"ticker": "KT1", "title": "Meta Market"}
        bf_market = {"id": "bf1", "name": "Meta Market"}

        mock_kalshi = MagicMock()
        mock_kalshi.get_market_price.return_value = k_prices
        mock_betfair = MagicMock()
        mock_betfair.get_market_price.return_value = bf_prices

        with patch.object(mod, "match_cross_platform", side_effect=self._setup_three_platform_mocks(mod, pm_market, k_market, bf_market)), \
             patch.object(mod, "filter_dust", side_effect=lambda x: x), \
             patch.object(mod, "net_profit_triangular", side_effect=fee_func), \
             patch.object(mod, "parse_outcome_prices", return_value=list(pm_prices)), \
             patch.object(mod, "get_clob_prices", return_value={
                 "yes_ask": 0.28, "yes_ask_size": 40,
                 "no_ask": 0.80, "no_ask_size": 40,
                 "yes_bid": 0.26, "yes_bid_size": 40,
                 "no_bid": 0.78, "no_bid_size": 40,
             }):
            return mod.scan_triangular(
                {"polymarket": [pm_market], "kalshi": [k_market], "betfair": [bf_market]},
                {"kalshi": mock_kalshi, "betfair": mock_betfair},
                min_profit=0.001,
            )

    @staticmethod
    def _linear_fee(yes_p, no_p, *_args, **_kwargs):
        net = 1.0 - yes_p - no_p
        return {"gross_spread": net, "fees": 0.0, "net_profit": net}

    def test_opp_carries_price_and_side_keys(self):
        mod = _import_triangular()
        # PM YES 0.25 is cheapest YES; Kalshi NO 0.30 is cheapest NO.
        # After CLOB refinement the PM YES leg is repriced to the live 0.28
        # ask, and the refined price is persisted back onto _price_a.
        opps = self._scan(mod, (0.25, 0.65), (0.35, 0.30), (0.45, 0.40),
                          self._linear_fee)
        assert len(opps) == 1
        opp = opps[0]
        assert opp["_side_a"] == "yes"
        assert opp["_price_a"] == pytest.approx(0.28)  # live PM ask
        assert opp["_side_b"] == "no"
        assert opp["_price_b"] == pytest.approx(0.30)  # Kalshi NO leg

    def test_refinement_uses_other_leg_price_not_zero(self):
        """CLOB refinement must reprice against the real other-leg price.

        PM YES ask = 0.28, Kalshi NO = 0.30 -> net = 1 - 0.28 - 0.30 = 0.42.
        The pre-fix behavior used other_price = 0, yielding 0.72."""
        mod = _import_triangular()
        opps = self._scan(mod, (0.25, 0.65), (0.35, 0.30), (0.45, 0.40),
                          self._linear_fee)
        assert len(opps) == 1
        assert opps[0]["net_profit"] == pytest.approx(0.42)

    def test_refinement_persists_live_prices_and_cost(self):
        """Audit #77 round 2: execution parses the `prices` string, so the
        refined opp must carry the LIVE executable prices and total_cost —
        not the stale Stage-1 mid prices."""
        mod = _import_triangular()
        opps = self._scan(mod, (0.25, 0.65), (0.35, 0.30), (0.45, 0.40),
                          self._linear_fee)
        assert len(opps) == 1
        opp = opps[0]
        # PM YES leg repriced 0.25 -> 0.28 (live ask); Kalshi NO stays 0.30.
        assert opp["prices"] == "polymarket_Y=0.280 kalshi_N=0.300"
        assert opp["total_cost"] == "$0.5800"
        assert opp["gross_spread"] == "0.4200"

    def test_refinement_refreshes_both_polymarket_legs(self):
        """A PM YES+NO pair must not retain the stale Stage-1 NO midpoint."""
        mod = _import_triangular()
        opp = {
            "_platform_a": "polymarket",
            "_platform_b": "polymarket",
            "_side_a": "yes",
            "_side_b": "no",
            "_price_a": 0.20,
            "_price_b": 0.30,
            "_token_ids": ["tok_yes", "tok_no"],
            "prices": "polymarket_Y=0.200 polymarket_N=0.300",
        }
        book = {
            "yes_ask": 0.28,
            "no_ask": 0.67,
            "yes_ask_size": 40,
            "no_ask_size": 30,
        }
        with (
            patch.object(mod, "get_clob_prices", return_value=book),
            patch.object(mod, "net_profit_triangular", side_effect=self._linear_fee),
        ):
            refined = mod._refine_triangular_with_clob([opp], min_profit=0.001)

        assert len(refined) == 1
        assert opp["_price_a"] == pytest.approx(0.28)
        assert opp["_price_b"] == pytest.approx(0.67)
        assert opp["prices"] == "polymarket_Y=0.280 polymarket_N=0.670"
        assert opp["total_cost"] == "$0.9500"


class TestBothLegsPolymarketRefinement:
    """Round-4 review finding: when BOTH legs are Polymarket, refinement
    refreshed only the YES leg and persisted the stale Stage-1 NO midpoint
    as _price_b — a false arb could survive and execute at an unverified
    NO price."""

    def _refine(self, mod, clob, price_a=0.25, price_b=0.60):
        opp = {
            "type": "Triangular",
            "market": "PM-only market",
            "net_profit": 0.10,
            "_platform_a": "polymarket",
            "_platform_b": "polymarket",
            "_side_a": "yes",
            "_side_b": "no",
            "_price_a": price_a,
            "_price_b": price_b,
            "_token_ids": ["tok_yes", "tok_no"],
        }
        with patch.object(mod, "get_clob_prices", return_value=clob), \
             patch.object(mod, "net_profit_triangular",
                          side_effect=lambda y, n, *_a, **_k: {
                              "gross_spread": 1.0 - y - n, "fees": 0.0,
                              "net_profit": 1.0 - y - n}):
            return mod._refine_triangular_with_clob([opp], min_profit=0.001)

    def test_no_leg_is_refreshed_from_live_book(self):
        """Stage-1 NO midpoint 0.60 looked like an arb (1 - 0.28 - 0.60 > 0)
        but the live NO ask is 0.75 — both legs must be repriced."""
        mod = _import_triangular()
        refined = self._refine(mod, {
            "yes_ask": 0.28, "yes_ask_size": 40,
            "no_ask": 0.75, "no_ask_size": 30,
        })
        assert len(refined) == 0  # 1 - 0.28 - 0.75 < min_profit: dropped

    def test_both_pm_prices_persisted_from_live_book(self):
        mod = _import_triangular()
        refined = self._refine(mod, {
            "yes_ask": 0.28, "yes_ask_size": 40,
            "no_ask": 0.55, "no_ask_size": 30,
        })
        assert len(refined) == 1
        opp = refined[0]
        assert opp["_price_a"] == pytest.approx(0.28)
        assert opp["_price_b"] == pytest.approx(0.55)  # live NO ask, not 0.60
        assert opp["prices"] == "polymarket_Y=0.280 polymarket_N=0.550"
        assert opp["net_profit"] == pytest.approx(1.0 - 0.28 - 0.55)

    def test_missing_token_ids_fail_closed(self):
        mod = _import_triangular()
        opp = {"_platform_a": "polymarket", "_platform_b": "kalshi", "_token_ids": []}

        assert mod._refine_triangular_with_clob([opp], min_profit=0.001) == []

    def test_missing_executable_asks_fail_closed(self):
        mod = _import_triangular()
        opp = {
            "_platform_a": "polymarket",
            "_platform_b": "kalshi",
            "_token_ids": ["yes", "no"],
        }
        with patch.object(mod, "get_clob_prices", return_value=None):
            assert mod._refine_triangular_with_clob([opp], min_profit=0.001) == []
