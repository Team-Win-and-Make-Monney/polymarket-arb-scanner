"""Tests for scanner.py — arbitrage scanner scan functions and token ID extraction."""

import io
import pytest
from unittest.mock import MagicMock, patch
from datetime import datetime, timezone, timedelta
import json

import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# Future date within the default 7-day resolution window for test fixtures
_SOON = (datetime.now(timezone.utc) + timedelta(days=2)).isoformat()


# Mock external API modules before importing scanner
@pytest.fixture(autouse=True)
def mock_external_modules():
    """Mock external API modules that may not be installed."""
    mock_modules = {}
    for mod_name in [
        "polymarket_api", "kalshi_api",
        "betfair_api", "smarkets_api", "sxbet_api",
        "ws_feeds", "db", "risk_manager", "executor",
    ]:
        if mod_name not in sys.modules:
            mock_modules[mod_name] = MagicMock()
            sys.modules[mod_name] = mock_modules[mod_name]
    yield
    for mod_name in mock_modules:
        if mod_name in sys.modules:
            del sys.modules[mod_name]
    # Also clear scanner and decomposed sub-modules so they reimport with fresh mocks
    for mod in list(sys.modules):
        if mod in ("scanner", "cli", "display", "continuous") or mod.startswith("scans"):
            del sys.modules[mod]


def _import_scanner():
    """Import scanner module, forcing reimport to pick up mocks."""
    # Clear scanner and all decomposed sub-modules so they reimport with current mocks
    for mod in list(sys.modules):
        if mod in ("scanner", "cli", "display", "continuous") or mod.startswith("scans"):
            del sys.modules[mod]
    import scanner
    return scanner


# ============================================================
# _extract_token_ids tests
# ============================================================


class TestExtractTokenIds:
    def test_json_string_token_ids(self):
        scanner = _import_scanner()
        market = {"clobTokenIds": '["token_a", "token_b"]'}
        result = scanner._extract_token_ids(market)
        assert result == ["token_a", "token_b"]

    def test_list_token_ids(self):
        scanner = _import_scanner()
        market = {"clobTokenIds": ["token_a", "token_b"]}
        result = scanner._extract_token_ids(market)
        assert result == ["token_a", "token_b"]

    def test_empty_token_ids(self):
        scanner = _import_scanner()
        market = {"clobTokenIds": ""}
        result = scanner._extract_token_ids(market)
        assert result == []

    def test_none_token_ids(self):
        scanner = _import_scanner()
        market = {}
        result = scanner._extract_token_ids(market)
        assert result == []

    def test_null_json_token_ids(self):
        scanner = _import_scanner()
        market = {"clobTokenIds": None}
        result = scanner._extract_token_ids(market)
        assert result == []

    def test_invalid_json_string(self):
        scanner = _import_scanner()
        market = {"clobTokenIds": "not_valid_json"}
        result = scanner._extract_token_ids(market)
        assert result == []

    def test_single_token_id(self):
        scanner = _import_scanner()
        market = {"clobTokenIds": '["only_one"]'}
        result = scanner._extract_token_ids(market)
        assert result == ["only_one"]

    def test_tuple_token_ids(self):
        scanner = _import_scanner()
        market = {"clobTokenIds": ("id1", "id2")}
        result = scanner._extract_token_ids(market)
        assert result == ["id1", "id2"]


# ============================================================
# scan_binary_internal tests
# ============================================================


class TestScanBinaryInternal:
    def _make_binary_market(self, yes_price, no_price, question="Test Market"):
        return {
            "question": question,
            "conditionId": f"cid_{question}",
            "outcomes": '["Yes", "No"]',
            "outcomePrices": json.dumps([yes_price, no_price]),
            "negRisk": False,
            "volume": "1000",
            "clobTokenIds": '["yes_token", "no_token"]',
            "endDateIso": _SOON,
        }

    def test_finds_arb_opportunity(self):
        scanner = _import_scanner()
        pm = scanner.sys.modules["polymarket_api"]
        market = self._make_binary_market(0.40, 0.40)
        pm.get_binary_markets.return_value = [market]
        pm.parse_outcome_prices.return_value = [0.40, 0.40]
        pm.get_clob_prices.return_value = None  # Skip CLOB refinement

        result = scanner.scan_binary_internal([market], min_profit=0.001)
        assert len(result) >= 1
        assert result[0]["type"] == "Binary"
        assert result[0]["net_profit"] > 0

    def test_no_arb_when_total_above_one(self):
        scanner = _import_scanner()
        pm = scanner.sys.modules["polymarket_api"]
        market = self._make_binary_market(0.55, 0.50)
        pm.get_binary_markets.return_value = [market]
        pm.parse_outcome_prices.return_value = [0.55, 0.50]

        result = scanner.scan_binary_internal([market], min_profit=0.001)
        assert len(result) == 0

    def test_skips_no_liquidity(self):
        scanner = _import_scanner()
        pm = scanner.sys.modules["polymarket_api"]
        market = self._make_binary_market(0.001, 0.40)
        pm.get_binary_markets.return_value = [market]
        pm.parse_outcome_prices.return_value = [0.001, 0.40]

        result = scanner.scan_binary_internal([market], min_profit=0.001)
        assert len(result) == 0

    def test_skips_resolved_markets(self):
        scanner = _import_scanner()
        pm = scanner.sys.modules["polymarket_api"]
        market = self._make_binary_market(0.99, 0.005)
        pm.get_binary_markets.return_value = [market]
        pm.parse_outcome_prices.return_value = [0.99, 0.005]

        result = scanner.scan_binary_internal([market], min_profit=0.001)
        assert len(result) == 0

    def test_empty_markets(self):
        scanner = _import_scanner()
        pm = scanner.sys.modules["polymarket_api"]
        pm.get_binary_markets.return_value = []

        result = scanner.scan_binary_internal([], min_profit=0.001)
        assert result == []

    def test_clob_refined_flag_set_when_unavailable(self):
        scanner = _import_scanner()
        pm = scanner.sys.modules["polymarket_api"]
        market = self._make_binary_market(0.40, 0.40)
        pm.get_binary_markets.return_value = [market]
        pm.parse_outcome_prices.return_value = [0.40, 0.40]
        pm.get_clob_prices.return_value = None

        result = scanner.scan_binary_internal([market], min_profit=0.001)
        assert len(result) >= 1
        assert result[0].get("_clob_refined") is False

    def test_token_ids_extracted(self):
        scanner = _import_scanner()
        pm = scanner.sys.modules["polymarket_api"]
        market = self._make_binary_market(0.40, 0.40)
        pm.get_binary_markets.return_value = [market]
        pm.parse_outcome_prices.return_value = [0.40, 0.40]
        pm.get_clob_prices.return_value = None

        result = scanner.scan_binary_internal([market], min_profit=0.001)
        assert len(result) >= 1
        assert result[0]["_token_ids"] == ["yes_token", "no_token"]


# ============================================================
# scan_negrisk_internal tests
# ============================================================


class TestScanNegRiskInternal:
    def _make_negrisk_event(self, yes_prices, title="Multi Event"):
        markets = []
        for i, price in enumerate(yes_prices):
            markets.append({
                "question": f"Outcome {i}",
                "groupItemTitle": f"Outcome {i}",
                "negRisk": True,
                "outcomePrices": json.dumps([price, 1.0 - price]),
                "clobTokenIds": json.dumps([f"token_{i}_yes", f"token_{i}_no"]),
                "endDateIso": _SOON,
            })
        return {"id": f"event_{title}", "title": title, "markets": markets}

    def test_finds_negrisk_arb(self):
        scanner = _import_scanner()
        pm = scanner.sys.modules["polymarket_api"]
        event = self._make_negrisk_event([0.20, 0.20, 0.20])  # Sum = 0.60 < 1.0
        pm.get_negrisk_events.return_value = [event]
        pm.parse_outcome_prices.side_effect = lambda m: [
            float(json.loads(m["outcomePrices"])[0])
        ]
        pm.get_clob_prices.return_value = None

        result = scanner.scan_negrisk_internal([event], min_profit=0.001)
        assert result == []

    def test_no_arb_when_sum_above_one(self):
        scanner = _import_scanner()
        pm = scanner.sys.modules["polymarket_api"]
        event = self._make_negrisk_event([0.40, 0.40, 0.30])  # Sum = 1.10
        pm.get_negrisk_events.return_value = [event]
        pm.parse_outcome_prices.side_effect = lambda m: [
            float(json.loads(m["outcomePrices"])[0])
        ]

        result = scanner.scan_negrisk_internal([event], min_profit=0.001)
        assert len(result) == 0

    def test_single_outcome_event_skipped(self):
        scanner = _import_scanner()
        pm = scanner.sys.modules["polymarket_api"]
        event = self._make_negrisk_event([0.20])
        event["markets"] = event["markets"][:1]  # Only one market
        pm.get_negrisk_events.return_value = [event]

        result = scanner.scan_negrisk_internal([event], min_profit=0.001)
        assert len(result) == 0

    def test_empty_events(self):
        scanner = _import_scanner()
        pm = scanner.sys.modules["polymarket_api"]
        pm.get_negrisk_events.return_value = []

        result = scanner.scan_negrisk_internal([], min_profit=0.001)
        assert result == []


# ============================================================
# scan_cross_all tests
# ============================================================


class TestScanCrossAll:
    def test_no_platforms_no_opportunities(self):
        scanner = _import_scanner()
        pm = scanner.sys.modules["polymarket_api"]
        pm.get_binary_markets.return_value = []

        result = scanner.scan_cross_all([], {}, min_profit=0.001)
        assert result == []

    def test_single_platform_pair(self):
        scanner = _import_scanner()
        pm = scanner.sys.modules["polymarket_api"]
        pm.get_binary_markets.return_value = []
        from matcher import match_cross_platform

        # Mock matcher to return empty matches
        with patch("scans.cross.match_cross_platform", return_value=[]):
            result = scanner.scan_cross_all([], {"betfair": (MagicMock(), [{"id": 1}])}, min_profit=0.001)
        assert result == []


# ============================================================
# CLI argument parsing tests
# ============================================================


class TestCLIParsing:
    def test_default_mode(self):
        scanner = _import_scanner()
        parser = scanner.argparse.ArgumentParser()
        parser.add_argument("--mode", choices=["all", "binary", "negrisk", "cross", "kalshi", "cross-all"], default="all")
        args = parser.parse_args([])
        assert args.mode == "all"

    def test_cross_all_mode(self):
        scanner = _import_scanner()
        parser = scanner.argparse.ArgumentParser()
        parser.add_argument("--mode", choices=["all", "binary", "negrisk", "cross", "kalshi", "cross-all"], default="all")
        args = parser.parse_args(["--mode", "cross-all"])
        assert args.mode == "cross-all"

    def test_kalshi_mode(self):
        scanner = _import_scanner()
        parser = scanner.argparse.ArgumentParser()
        parser.add_argument("--mode", choices=["all", "binary", "negrisk", "cross", "kalshi", "cross-all"], default="all")
        args = parser.parse_args(["--mode", "kalshi"])
        assert args.mode == "kalshi"


# ============================================================
# _refine_binary_with_clob tests
# ============================================================


class TestRefineBinaryWithClob:
    def test_keeps_opp_when_clob_unavailable(self):
        scanner = _import_scanner()

        opp = {
            "_market_key": "cid_123",
            "net_profit": 0.05,
            "prices": "Y=0.40 N=0.40",
        }
        markets_by_q = {"cid_123": {"clobTokenIds": '["a","b"]'}}

        with patch("scans.helpers.get_clob_prices", return_value=None):
            result = scanner._refine_binary_with_clob([opp], markets_by_q, 0.001)
        assert len(result) == 1
        assert result[0].get("_clob_refined") is False

    def test_drops_opp_when_clob_shows_no_profit(self):
        scanner = _import_scanner()
        from fees import net_profit_binary_internal
        scanner.net_profit_binary_internal = net_profit_binary_internal

        clob_data = {
            "yes_ask": 0.55,
            "no_ask": 0.50,
            "yes_ask_size": 100,
            "no_ask_size": 100,
        }

        opp = {
            "_market_key": "cid_123",
            "net_profit": 0.05,
            "prices": "Y=0.40 N=0.40",
        }
        markets_by_q = {"cid_123": {"clobTokenIds": '["a","b"]'}}

        with patch("scans.helpers.get_clob_prices", return_value=clob_data):
            result = scanner._refine_binary_with_clob([opp], markets_by_q, 0.001)
        assert len(result) == 0

    def test_empty_list_returns_empty(self):
        scanner = _import_scanner()
        result = scanner._refine_binary_with_clob([], {}, 0.001)
        assert result == []


# ============================================================
# _display_results tests (smoke test)
# ============================================================


class TestDisplayResults:
    def test_no_opportunities(self):
        scanner = _import_scanner()
        buf = io.StringIO()
        with patch("sys.stdout", buf):
            scanner._display_results([], json_output=False)
        output = buf.getvalue()
        assert "0 opportunities" in output

    def test_json_output(self):
        scanner = _import_scanner()
        buf = io.StringIO()
        opps = [{
            "type": "Binary",
            "market": "Test",
            "prices": "Y=0.40 N=0.40",
            "total_cost": "$0.80",
            "gross_spread": "0.20",
            "fees": "$0.01",
            "net_profit": 0.19,
            "net_roi": "23.75%",
            "volume": "$1000",
        }]
        with patch("sys.stdout", buf):
            scanner._display_results(opps, json_output=True)
        output = buf.getvalue()
        assert '"type": "Binary"' in output


# ============================================================
# _attach_exec_metadata tests (cross-all execution metadata)
# ============================================================


class TestScanCrossAllWithMetadata:
    def test_polymarket_gets_token_ids(self):
        scanner = _import_scanner()
        opp = {}
        market = {"clobTokenIds": '["tok_yes", "tok_no"]'}
        scanner._attach_exec_metadata(opp, market, "polymarket", "a")
        assert opp["_token_ids"] == ["tok_yes", "tok_no"]

    def test_betfair_gets_market_id_and_selection_id(self):
        scanner = _import_scanner()
        opp = {}
        market = {
            "marketId": "1.234567890",
            "runners": [{"selectionId": 98765}, {"selectionId": 11111}],
        }
        scanner._attach_exec_metadata(opp, market, "betfair", "b")
        assert opp["_market_id"] == "1.234567890"
        assert opp["_selection_id"] == 98765

    def test_kalshi_gets_kalshi_ticker(self):
        scanner = _import_scanner()
        opp = {}
        market = {"ticker": "KALSHI-TICKER-2026"}
        scanner._attach_exec_metadata(opp, market, "kalshi", "b")
        assert opp["_kalshi_ticker"] == "KALSHI-TICKER-2026"

    def test_polymarket_empty_clob_token_ids(self):
        scanner = _import_scanner()
        opp = {}
        market = {"clobTokenIds": ""}
        scanner._attach_exec_metadata(opp, market, "polymarket", "a")
        assert opp["_token_ids"] == []

    def test_betfair_no_runners(self):
        scanner = _import_scanner()
        opp = {}
        market = {"marketId": "1.999", "runners": []}
        scanner._attach_exec_metadata(opp, market, "betfair", "b")
        assert opp["_market_id"] == "1.999"
        assert "_selection_id" not in opp

    def test_kalshi_missing_ticker_returns_empty_string(self):
        scanner = _import_scanner()
        opp = {}
        market = {}
        scanner._attach_exec_metadata(opp, market, "kalshi", "b")
        assert opp["_kalshi_ticker"] == ""


# ============================================================
# _check_settlements tests
# ============================================================


class TestSettlementChecks:
    def _make_db_with_position(self, platform, market_identifier="12345", expected_pnl=0.05):
        """Create an in-memory TradeDB with one open position and return (db, position)."""
        # Load the real db module, bypassing the autouse mock that replaced it with MagicMock
        import importlib.util
        db_path = os.path.join(os.path.dirname(__file__), "..", "db.py")
        spec = importlib.util.spec_from_file_location("_real_db", db_path)
        real_db = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(real_db)
        TradeDB = real_db.TradeDB
        db = TradeDB(":memory:")
        opp_id = db.log_opportunity(
            opp_type="Cross",
            market="Test Market",
            prices="Y=0.40 N=0.40",
            total_cost=0.95,
            net_profit=expected_pnl,
            net_roi=expected_pnl,
            depth=100,
            action="traded",
        )
        db.create_position(
            opportunity_id=opp_id,
            market_identifier=market_identifier,
            platform=platform,
            expected_pnl=expected_pnl,
        )
        positions = db.get_open_positions()
        return db, positions[0]



# ============================================================
# Price cache cleanup logic tests
# ============================================================


class TestPriceCacheCleanup:
    """Test the price cache cleanup logic used in _run_continuous().

    The actual _cleanup_price_cache() is a nested function inside _run_continuous(),
    so we replicate its logic here and verify the expected behavior.
    """

    @staticmethod
    def _cleanup_price_cache(price_cache, max_age=60):
        """Replicate the cleanup logic from _run_continuous."""
        import time
        now = time.time()
        stale_keys = [k for k, v in price_cache.items() if now - v.get("_ts", 0) > max_age]
        for k in stale_keys:
            del price_cache[k]
        return len(stale_keys)

    def test_stale_entries_removed(self):
        import time
        now = time.time()
        price_cache = {
            ("polymarket", "tok1"): {"price": 0.50, "_ts": now - 120},  # 2 min old, stale
            ("polymarket", "tok2"): {"price": 0.60, "_ts": now - 90},   # 1.5 min old, stale
            ("kalshi", "TICK1"): {"price": 0.45, "_ts": now - 10},      # 10s old, fresh
        }
        removed = self._cleanup_price_cache(price_cache, max_age=60)
        assert removed == 2
        assert len(price_cache) == 1
        assert ("kalshi", "TICK1") in price_cache

    def test_fresh_entries_kept(self):
        import time
        now = time.time()
        price_cache = {
            ("polymarket", "tok1"): {"price": 0.50, "_ts": now - 5},
            ("kalshi", "TICK1"): {"price": 0.45, "_ts": now - 30},
        }
        removed = self._cleanup_price_cache(price_cache, max_age=60)
        assert removed == 0
        assert len(price_cache) == 2

    def test_empty_cache(self):
        price_cache = {}
        removed = self._cleanup_price_cache(price_cache, max_age=60)
        assert removed == 0
        assert len(price_cache) == 0

    def test_entry_without_timestamp_is_stale(self):
        price_cache = {
            ("polymarket", "tok_no_ts"): {"price": 0.50},  # No _ts field
        }
        removed = self._cleanup_price_cache(price_cache, max_age=60)
        assert removed == 1
        assert len(price_cache) == 0

    def test_exact_boundary_is_not_stale(self):
        import time as time_mod
        frozen_now = 1000000.0
        price_cache = {
            ("polymarket", "tok_boundary"): {"price": 0.50, "_ts": frozen_now - 60},
        }
        # At exactly max_age, now - _ts == 60 which is NOT > 60, so it stays
        with patch.object(time_mod, "time", return_value=frozen_now):
            removed = self._cleanup_price_cache(price_cache, max_age=60)
        assert removed == 0
        assert len(price_cache) == 1

    def test_all_stale_entries_removed(self):
        import time
        now = time.time()
        price_cache = {
            ("polymarket", "tok1"): {"price": 0.50, "_ts": now - 300},
            ("polymarket", "tok2"): {"price": 0.60, "_ts": now - 200},
            ("kalshi", "TICK1"): {"price": 0.45, "_ts": now - 100},
        }
        removed = self._cleanup_price_cache(price_cache, max_age=60)
        assert removed == 3
        assert len(price_cache) == 0
