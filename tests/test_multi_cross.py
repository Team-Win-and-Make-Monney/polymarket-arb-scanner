"""Tests for scans/multi_cross.py — multi-outcome cross-platform arbitrage."""

import pytest
from unittest.mock import MagicMock, patch
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


# ---------------------------------------------------------------------------
# Mock setup — thefuzz may not be installed in CI
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def mock_external_modules():
    """Mock modules that may not be installed in test environment."""
    mocked = {}
    for mod_name in ["polymarket_api", "kalshi_api"]:
        if mod_name not in sys.modules:
            mocked[mod_name] = MagicMock()
            sys.modules[mod_name] = mocked[mod_name]
    # Ensure scans.multi_cross gets fresh import
    for key in list(sys.modules):
        if key == "scans.multi_cross":
            del sys.modules[key]
    yield
    for mod_name in mocked:
        if mod_name in sys.modules:
            del sys.modules[mod_name]


# ---------------------------------------------------------------------------
# Event matching tests
# ---------------------------------------------------------------------------

class TestMatchEventsByTitle:
    def test_exact_match(self):
        from scans.multi_cross import _match_events_by_title

        pm_events = [{"title": "Who will win the 2024 Presidential Election?", "markets": []}]
        kalshi_events = {"PRES-2024": [{"ticker": "PRES-2024-DEM"}]}
        kalshi_titles = {"PRES-2024": "Who will win the 2024 Presidential Election?"}

        matches = _match_events_by_title(pm_events, kalshi_events, kalshi_titles)
        assert len(matches) == 1
        assert matches[0][1] == "PRES-2024"

    def test_no_match_below_threshold(self):
        from scans.multi_cross import _match_events_by_title

        pm_events = [{"title": "Price of gold in 2025", "markets": []}]
        kalshi_events = {"WEATHER-2024": [{"ticker": "WEATHER-NYC"}]}
        kalshi_titles = {"WEATHER-2024": "NYC weather forecast December"}

        matches = _match_events_by_title(pm_events, kalshi_events, kalshi_titles)
        assert len(matches) == 0

    def test_empty_inputs(self):
        from scans.multi_cross import _match_events_by_title

        assert _match_events_by_title([], {}, {}) == []
        assert _match_events_by_title([{"title": "test"}], {}, {}) == []

    def test_matcher_preserves_entity_overlap_gate(self):
        from scans.multi_cross import _match_events_by_title

        pm_events = [{"title": "Who will win the Michigan governor election in 2026?", "markets": []}]
        kalshi_events = {
            "MI-GOV": [{"ticker": "MI-GOV-DEM"}],
            "GOLD": [{"ticker": "GOLD-PRICE"}],
            "WEATHER": [{"ticker": "WEATHER-NYC"}],
        }
        kalshi_titles = {
            "MI-GOV": "Who will win the Michigan governor election in 2026?",
            "GOLD": "Price of gold above 3000 in 2026?",
            "WEATHER": "New York City snowfall in December?",
        }

        matches = _match_events_by_title(pm_events, kalshi_events, kalshi_titles)

        assert [match[1] for match in matches] == ["MI-GOV"]

    def test_tied_scores_preserve_first_kalshi_event(self):
        from scans.multi_cross import _match_events_by_title

        pm_events = [{"title": "Michigan governor election in 2026", "markets": []}]
        kalshi_events = {
            "FIRST": [{"ticker": "FIRST"}],
            "SECOND": [{"ticker": "SECOND"}],
        }
        kalshi_titles = {
            "FIRST": "Michigan governor election in 2026",
            "SECOND": "Michigan governor election in 2026",
        }

        matches = _match_events_by_title(pm_events, kalshi_events, kalshi_titles)

        assert [match[1] for match in matches] == ["FIRST"]


# ---------------------------------------------------------------------------
# Outcome matching tests
# ---------------------------------------------------------------------------

class TestMatchOutcomes:
    def test_matches_outcomes_by_title(self):
        from scans.multi_cross import _match_outcomes

        pm_markets = [
            {"groupItemTitle": "Biden", "outcomePrices": '[0.40, 0.60]'},
            {"groupItemTitle": "Trump", "outcomePrices": '[0.55, 0.45]'},
        ]
        kalshi_markets = [
            {"title": "Biden", "yes_price": 0.38},
            {"title": "Trump", "yes_price": 0.52},
        ]

        # Mock parse_outcome_prices to return the first element
        with patch("scans.multi_cross.parse_outcome_prices") as mock_parse:
            mock_parse.side_effect = lambda m: [
                float(m["outcomePrices"].strip("[]").split(",")[0])
            ]
            outcomes = _match_outcomes(pm_markets, kalshi_markets)

        assert len(outcomes) == 2
        for o in outcomes:
            assert "best_price" in o
            assert "best_platform" in o
            assert o["best_platform"] in ("polymarket", "kalshi")

    def test_picks_cheaper_platform(self):
        from scans.multi_cross import _match_outcomes

        pm_markets = [
            {"groupItemTitle": "Option A", "outcomePrices": '[0.50, 0.50]'},
        ]
        kalshi_markets = [
            {"title": "Option A", "yes_price": 0.40},
        ]

        with patch("scans.multi_cross.parse_outcome_prices") as mock_parse:
            mock_parse.return_value = [0.50]
            outcomes = _match_outcomes(pm_markets, kalshi_markets)

        assert len(outcomes) == 1
        assert outcomes[0]["best_platform"] == "kalshi"
        assert outcomes[0]["best_price"] == 0.40

    @staticmethod
    def _match(pm_markets, kalshi_markets):
        from scans.multi_cross import _match_outcomes

        with patch("scans.multi_cross.parse_outcome_prices",
                   side_effect=lambda m: [m["_yes"]]):
            return {o["label"]: o for o in _match_outcomes(pm_markets, kalshi_markets)}

    def test_placeholder_leg_never_takes_kalshi_price(self):
        """Live 2026-09-25: the "Song T" placeholder fuzzy-matched Kalshi's
        "stupid song" (score 71) and priced the slot at 0.01."""
        outcomes = self._match(
            [{"groupItemTitle": '"Song T"', "active": False, "_yes": 0.5}],
            [{"yes_sub_title": "stupid song", "yes_price": 0.01}],
        )

        leg = outcomes['"Song T"']
        assert leg["kalshi_market"] is None
        assert leg["best_platform"] == "polymarket"
        assert leg["best_price"] == 0.5

    def test_neg_risk_other_leg_never_takes_kalshi_price(self):
        """Live 2026-09-25: Serie A's Other leg matched Kalshi "Inter" (score 60)."""
        outcomes = self._match(
            [{"groupItemTitle": "Other", "negRiskOther": True, "active": True, "_yes": 0.30}],
            [{"yes_sub_title": "Inter", "yes_price": 0.10}],
        )

        leg = outcomes["Other"]
        assert leg["kalshi_market"] is None
        assert leg["best_platform"] == "polymarket"
        assert leg["best_price"] == 0.30

    def test_catch_all_legs_are_not_paired_across_venues(self):
        """Each venue's catch-all covers what that venue did not list."""
        outcomes = self._match(
            [
                {"groupItemTitle": "Alice", "_yes": 0.40},
                {"groupItemTitle": "Other", "_yes": 0.30},
                {"groupItemTitle": "Other Party", "_yes": 0.20},
            ],
            [
                {"yes_sub_title": "Alice", "yes_price": 0.35},
                {"yes_sub_title": "Other", "yes_price": 0.05},
            ],
        )

        assert outcomes["Alice"]["best_platform"] == "kalshi"
        assert outcomes["Other"]["kalshi_market"] is None
        assert outcomes["Other"]["best_price"] == 0.30
        # A named PM leg cannot absorb the Kalshi catch-all either (score 62).
        assert outcomes["Other Party"]["kalshi_market"] is None
        assert outcomes["Other Party"]["best_price"] == 0.20

    def test_is_unnamed_pm_leg_identifies_unnamed_and_placeholder_legs(self):
        from scans.multi_cross import _is_unnamed_pm_leg

        assert _is_unnamed_pm_leg({"active": False}) is True
        assert _is_unnamed_pm_leg({"negRiskOther": True}) is True
        assert _is_unnamed_pm_leg({"groupItemTitle": "Other"}) is True
        assert _is_unnamed_pm_leg({"groupItemTitle": "Someone else"}) is True
        # Fallback to question when groupItemTitle is absent or None
        assert _is_unnamed_pm_leg({"question": "Other"}) is True
        assert _is_unnamed_pm_leg({"groupItemTitle": None, "question": "Other"}) is True
        assert _is_unnamed_pm_leg({"groupItemTitle": "", "question": "Someone else"}) is True
        # Named active legs
        assert _is_unnamed_pm_leg({"groupItemTitle": "Alice", "active": True}) is False
        assert _is_unnamed_pm_leg({"question": "Alice", "active": True}) is False


# ---------------------------------------------------------------------------
# Main scan function
# ---------------------------------------------------------------------------

class TestScanMultiCross:
    def test_returns_empty_with_no_events(self):
        from scans.multi_cross import scan_multi_cross

        with patch("scans.multi_cross.get_negrisk_events", return_value=[]):
            result = scan_multi_cross([], kalshi_client=MagicMock())
        assert result == []

    def test_returns_empty_without_kalshi(self):
        from scans.multi_cross import scan_multi_cross

        result = scan_multi_cross([{"title": "test"}], kalshi_client=None)
        assert result == []

    def test_opportunity_has_required_keys(self):
        from scans.multi_cross import scan_multi_cross

        fake_event = {
            "title": "Test Election",
            "id": "ev-1",
            "markets": [
                {"groupItemTitle": "A", "outcomePrices": '[0.30, 0.70]',
                 "clobTokenIds": '["tok_a_yes","tok_a_no"]',
                 "endDate": "2030-01-01T00:00:00Z"},
                {"groupItemTitle": "B", "outcomePrices": '[0.25, 0.75]',
                 "clobTokenIds": '["tok_b_yes","tok_b_no"]',
                 "endDate": "2030-01-01T00:00:00Z"},
                {"groupItemTitle": "C", "outcomePrices": '[0.20, 0.80]',
                 "clobTokenIds": '["tok_c_yes","tok_c_no"]',
                 "endDate": "2030-01-01T00:00:00Z"},
            ],
        }
        kalshi_markets = [
            {"title": "A", "ticker": "K-A", "yes_price": 0.28},
            {"title": "B", "ticker": "K-B", "yes_price": 0.22},
            {"title": "C", "ticker": "K-C", "yes_price": 0.18},
        ]
        kalshi_data = (
            kalshi_markets,
            {"EV-1": kalshi_markets},
            {"EV-1": "Test Election"},
        )

        with patch("scans.multi_cross.get_negrisk_events", return_value=[fake_event]), \
             patch("scans.multi_cross.parse_outcome_prices", side_effect=lambda m: [
                 float(m["outcomePrices"].strip("[]").split(",")[0].strip())
             ]), \
             patch("scans.multi_cross._within_resolution_window", return_value=True), \
             patch("scans.multi_cross._refine_multi_cross_with_clob", side_effect=lambda opps, *a, **kw: opps), \
             patch("scans.multi_cross.filter_dust", side_effect=lambda opps: opps), \
             patch("scans.multi_cross.net_profit_multi_cross", return_value={
                 "gross_spread": 0.12, "fees": 0.02, "net_profit": 0.10,
             }), \
             patch("scans.multi_cross._match_events_by_title", return_value=[
                 (fake_event, "EV-1", kalshi_markets),
             ]), \
             patch("scans.multi_cross._match_outcomes", return_value=[
                 {"label": "A", "pm_price": 0.28, "kalshi_price": 0.35,
                  "pm_market": fake_event["markets"][0], "kalshi_market": kalshi_markets[0],
                  "best_price": 0.28, "best_platform": "polymarket"},
                 {"label": "B", "pm_price": 0.30, "kalshi_price": 0.22,
                  "pm_market": fake_event["markets"][1], "kalshi_market": kalshi_markets[1],
                  "best_price": 0.22, "best_platform": "kalshi"},
                 {"label": "C", "pm_price": 0.25, "kalshi_price": 0.18,
                  "pm_market": fake_event["markets"][2], "kalshi_market": kalshi_markets[2],
                  "best_price": 0.18, "best_platform": "kalshi"},
             ]):
            result = scan_multi_cross(
                [fake_event], kalshi_client=MagicMock(),
                min_profit=0.01, kalshi_data=kalshi_data,
            )

        assert len(result) >= 1
        opp = result[0]
        assert opp["type"].startswith("MultiCross")
        assert "net_profit" in opp
        assert "_outcome_legs" in opp
        assert opp["net_profit"] > 0

    def test_non_mutually_exclusive_kalshi_events_excluded_from_matching(self):
        """The complete-set gate must drop non-exclusive Kalshi events before
        title matching — non-exclusive ladders are not complete sets."""
        from scans.multi_cross import scan_multi_cross

        fake_event = {"title": "Test Election", "negRisk": True,
                      "markets": [{"question": "A"}, {"question": "B"}]}
        kalshi_markets = [{"title": "A", "ticker": "K-A"}, {"title": "B", "ticker": "K-B"}]

        def run(me_flag):
            events_list = [{"event_ticker": "EV-1", "mutually_exclusive": me_flag}]
            kalshi_data = (events_list, {"EV-1": kalshi_markets}, {"EV-1": "Test Election"})
            with patch("scans.multi_cross.get_negrisk_events", return_value=[fake_event]), \
                 patch("scans.multi_cross._match_events_by_title", return_value=[]) as matcher:
                scan_multi_cross([fake_event], kalshi_client=MagicMock(),
                                 min_profit=0.01, kalshi_data=kalshi_data)
            return matcher.call_args[0][1]  # the kalshi_multi dict passed in

        assert run(False) == {}                      # gated out
        assert "EV-1" in run(True)                   # passes the gate

    def test_augmented_event_unnamed_slots_not_filled_from_kalshi(self):
        """An augmented event's complete set is named + placeholder + Other legs.
        Filling the placeholder and Other slots with Kalshi outcomes that share
        their labels priced this basket at 0.71; they must stay Polymarket legs."""
        from scans.multi_cross import scan_multi_cross

        pm_event = {
            "title": "Who will win the election?", "id": "ev-aug", "negRiskAugmented": True,
            "markets": [
                {"groupItemTitle": "Alice", "active": True, "_yes": 0.36},
                {"groupItemTitle": "Bob", "active": True, "_yes": 0.35},
                {"groupItemTitle": "Person C", "active": False, "_yes": 0.5},
                {"groupItemTitle": "Other", "active": False, "negRiskOther": True, "_yes": 0.5},
            ],
        }
        kalshi_markets = [
            {"yes_sub_title": "Alice", "ticker": "K-A", "yes_price": 0.38},
            {"yes_sub_title": "Bob", "ticker": "K-B", "yes_price": 0.33},
            {"yes_sub_title": "Person C", "ticker": "K-C", "yes_price": 0.01},
            {"yes_sub_title": "Other", "ticker": "K-O", "yes_price": 0.01},
        ]
        kalshi_data = (
            [{"event_ticker": "EV-1", "mutually_exclusive": True}],
            {"EV-1": kalshi_markets},
            {"EV-1": "Who will win the election?"},
        )

        with patch("scans.multi_cross.get_negrisk_events", return_value=[pm_event]), \
             patch("scans.multi_cross.parse_outcome_prices", side_effect=lambda m: [m["_yes"]]), \
             patch("scans.multi_cross._within_resolution_window", return_value=True), \
             patch("scans.multi_cross._days_to_resolution", return_value=30), \
             patch("scans.multi_cross._extract_token_ids", return_value=["tok"]), \
             patch("scans.multi_cross._refine_multi_cross_with_clob", side_effect=lambda opps, *a, **kw: opps), \
             patch("scans.multi_cross.filter_dust", side_effect=lambda opps: opps), \
             patch("scans.multi_cross._match_events_by_title", return_value=[
                 (pm_event, "EV-1", kalshi_markets),
             ]):
            result = scan_multi_cross(
                [pm_event], kalshi_client=None, min_profit=0.0, kalshi_data=kalshi_data,
            )

        assert result == []

    def test_scan_multi_cross_drops_baskets_with_unnamed_pm_legs(self):
        """Even if total cost is low and spread is profitable, a basket containing
        an unnamed, placeholder, or catch-all Polymarket leg must be dropped."""
        from scans.multi_cross import scan_multi_cross

        pm_event = {
            "title": "Who will win the tournament?", "id": "ev-tourney",
            "markets": [
                {"groupItemTitle": "Team Alpha", "active": True, "_yes": 0.20},
                {"groupItemTitle": "Team Beta", "active": True, "_yes": 0.20},
                {"groupItemTitle": "Other", "active": True, "negRiskOther": True, "_yes": 0.10},
            ],
        }
        kalshi_markets = [
            {"yes_sub_title": "Team Alpha", "ticker": "K-A", "yes_price": 0.25},
            {"yes_sub_title": "Team Beta", "ticker": "K-B", "yes_price": 0.25},
            {"yes_sub_title": "Other", "ticker": "K-O", "yes_price": 0.15},
        ]
        kalshi_data = (
            [{"event_ticker": "EV-1", "mutually_exclusive": True}],
            {"EV-1": kalshi_markets},
            {"EV-1": "Who will win the tournament?"},
        )

        with patch("scans.multi_cross.get_negrisk_events", return_value=[pm_event]), \
             patch("scans.multi_cross.parse_outcome_prices", side_effect=lambda m: [m["_yes"]]), \
             patch("scans.multi_cross._within_resolution_window", return_value=True), \
             patch("scans.multi_cross._days_to_resolution", return_value=30), \
             patch("scans.multi_cross._extract_token_ids", return_value=["tok"]), \
             patch("scans.multi_cross._refine_multi_cross_with_clob", side_effect=lambda opps, *a, **kw: opps), \
             patch("scans.multi_cross.filter_dust", side_effect=lambda opps: opps), \
             patch("scans.multi_cross._match_events_by_title", return_value=[
                 (pm_event, "EV-1", kalshi_markets),
             ]):
            result = scan_multi_cross(
                [pm_event], kalshi_client=None, min_profit=0.01, kalshi_data=kalshi_data,
            )

        assert result == []


# ---------------------------------------------------------------------------
# Executor integration — MultiCross _build_legs and _revalidate
# ---------------------------------------------------------------------------

class TestMultiCrossExecutor:
    @pytest.fixture
    def executor(self):
        """Create an executor with mocked dependencies."""
        for mod_name in ["betfair_api", "smarkets_api", "sxbet_api",
                         "matchbook_api", "gemini_api", "ibkr_api"]:
            if mod_name not in sys.modules:
                sys.modules[mod_name] = MagicMock()
        if "executor" in sys.modules:
            del sys.modules["executor"]
        from executor import ArbitrageExecutor
        from db import TradeDB
        from risk_manager import RiskManager

        db = TradeDB(":memory:")
        risk = RiskManager({
            "max_trade_size": 5.0, "daily_loss_limit": 25.0,
            "max_open_positions": 25, "min_liquidity": 25.0,
            "min_liquidity_high_roi": 10.0, "min_net_roi": 0,
            "allow_better_reentry": True, "reentry_improvement_threshold": 0.20,
        })
        exc = ArbitrageExecutor(
            pm_trader=MagicMock(), kalshi_client=MagicMock(),
            db=db, risk_manager=risk, dry_run=True, max_trade_size=5.0,
        )
        yield exc
        db.close()

    def test_build_legs_multi_cross_polymarket(self, executor):
        """MultiCross legs with Polymarket outcomes should produce BUY legs."""
        opp = {
            "type": "MultiCross(3)",
            "net_profit": 0.10,
            "_outcome_legs": [
                {"platform": "polymarket", "outcome": "A", "price": 0.30,
                 "side": "yes", "_token_id": "tok_a"},
                {"platform": "polymarket", "outcome": "B", "price": 0.25,
                 "side": "yes", "_token_id": "tok_b"},
                {"platform": "polymarket", "outcome": "C", "price": 0.20,
                 "side": "yes", "_token_id": "tok_c"},
            ],
        }
        with patch("executor.ENABLED_EXECUTION_PLATFORMS", frozenset({"polymarket"})):
            legs = executor._build_legs(opp, 5.0)
        assert len(legs) == 3
        for leg in legs:
            assert leg["platform"] == "polymarket"
            assert leg["side"] == "BUY"

    def test_build_legs_multi_cross_kalshi(self, executor):
        """MultiCross legs with Kalshi outcomes should produce buy legs."""
        opp = {
            "type": "MultiCross(2)",
            "net_profit": 0.05,
            "_outcome_legs": [
                {"platform": "kalshi", "outcome": "A", "price": 0.40,
                 "side": "yes", "_kalshi_ticker": "K-A"},
                {"platform": "kalshi", "outcome": "B", "price": 0.35,
                 "side": "yes", "_kalshi_ticker": "K-B"},
            ],
        }
        legs = executor._build_legs(opp, 5.0)
        assert len(legs) == 2
        for leg in legs:
            assert leg["platform"] == "kalshi"
            assert leg["side"] == "yes"
            assert leg["action"] == "buy"

    def test_build_legs_multi_cross_mixed(self, executor):
        """MultiCross legs across platforms produce mixed platform legs."""
        opp = {
            "type": "MultiCross(3)",
            "net_profit": 0.08,
            "_outcome_legs": [
                {"platform": "polymarket", "outcome": "A", "price": 0.28,
                 "side": "yes", "_token_id": "tok_a"},
                {"platform": "kalshi", "outcome": "B", "price": 0.22,
                 "side": "yes", "_kalshi_ticker": "K-B"},
                {"platform": "polymarket", "outcome": "C", "price": 0.18,
                 "side": "yes", "_token_id": "tok_c"},
            ],
        }
        with patch(
            "executor.ENABLED_EXECUTION_PLATFORMS",
            frozenset({"polymarket", "kalshi"}),
        ):
            legs = executor._build_legs(opp, 5.0)
        assert len(legs) == 3
        platforms = [leg["platform"] for leg in legs]
        assert "polymarket" in platforms
        assert "kalshi" in platforms

    def test_build_legs_empty_when_no_outcome_legs(self, executor):
        """MultiCross with no _outcome_legs returns empty."""
        opp = {"type": "MultiCross(3)", "net_profit": 0.10}
        legs = executor._build_legs(opp, 5.0)
        assert legs == []

    def test_revalidate_multi_cross_passes(self, executor):
        """Revalidation passes when profit stays above threshold."""
        opp = {
            "type": "MultiCross(3)",
            "net_profit": 0.10,
            "total_cost": "$0.75",
            "_outcome_legs": [
                {"platform": "polymarket", "price": 0.25, "_token_id": "tok_a"},
                {"platform": "kalshi", "price": 0.25, "_kalshi_ticker": "K-B"},
                {"platform": "polymarket", "price": 0.25, "_token_id": "tok_c"},
            ],
        }
        # The MultiCross revalidation path now includes a per-leg Kalshi
        # depth gate (kill-switch for the FOK partial-fill trap added in
        # commit 3017193). This test isolates the profit-threshold logic,
        # so disable the depth gate by returning no orderbook for the
        # Kalshi leg — the gate falls through when book is falsy.
        executor.kalshi_client.fetch_order_book.return_value = None
        with patch("executor.net_profit_multi_cross", return_value={
            "gross_spread": 0.10, "fees": 0.005, "net_profit": 0.095,
        }):
            result = executor._revalidate(opp)
        assert result is True

    def test_revalidate_multi_cross_fails_on_degraded_profit(self, executor):
        """Revalidation fails when profit drops below threshold."""
        opp = {
            "type": "MultiCross(3)",
            "net_profit": 0.10,
            "total_cost": "$0.75",
            "_outcome_legs": [
                {"platform": "polymarket", "price": 0.30, "_token_id": "tok_a"},
                {"platform": "kalshi", "price": 0.30, "_kalshi_ticker": "K-B"},
                {"platform": "polymarket", "price": 0.30, "_token_id": "tok_c"},
            ],
        }
        with patch("executor.net_profit_multi_cross", return_value={
            "gross_spread": 0.01, "fees": 0.01, "net_profit": 0.001,
        }):
            result = executor._revalidate(opp)
        assert result is False


# ---------------------------------------------------------------------------
# Kalshi fallback fetch (audit #77): when kalshi_data is not pre-fetched the
# scan must fall back to scans.kalshi._fetch_kalshi_data (which returns the
# expected (events, by_event, titles) 3-tuple). The old call to
# scans.helpers._parallel_fetch_kalshi(kalshi_client) raised TypeError —
# missing its required `tickers` arg — and returned a dict, not a 3-tuple.
# ---------------------------------------------------------------------------

class TestKalshiFallbackFetch:
    def test_falls_back_to_fetch_kalshi_data(self):
        from scans.multi_cross import scan_multi_cross

        fake_event = {"title": "Test Election", "id": "ev-1", "markets": [{}, {}]}
        with patch("scans.multi_cross.get_negrisk_events", return_value=[fake_event]), \
             patch("scans.kalshi._fetch_kalshi_data",
                   return_value=([], {}, {})) as mock_fetch:
            result = scan_multi_cross(
                [fake_event], kalshi_client=MagicMock(), kalshi_data=None,
            )
        mock_fetch.assert_called_once()
        assert result == []

    def test_fallback_result_unpacks_into_three_parts(self):
        """The fallback fetch must feed events/by_event/titles downstream."""
        from scans.multi_cross import scan_multi_cross

        fake_event = {"title": "Test Election", "id": "ev-1", "markets": [{}, {}]}
        kalshi_events = [{"event_ticker": "EV-1", "mutually_exclusive": True}]
        kalshi_by_event = {"EV-1": [{"title": "A"}, {"title": "B"}]}
        kalshi_titles = {"EV-1": "Totally Different Title"}

        with patch("scans.multi_cross.get_negrisk_events", return_value=[fake_event]), \
             patch("scans.kalshi._fetch_kalshi_data",
                   return_value=(kalshi_events, kalshi_by_event, kalshi_titles)):
            # No titles match, so no opportunities — but no TypeError either.
            result = scan_multi_cross(
                [fake_event], kalshi_client=MagicMock(), kalshi_data=None,
            )
        assert result == []
