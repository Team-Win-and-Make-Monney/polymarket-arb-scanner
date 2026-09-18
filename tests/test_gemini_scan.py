"""Tests for Gemini scan functions — binary and multi-outcome arbitrage detection."""

import pytest
from unittest.mock import MagicMock, patch

import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


@pytest.fixture(autouse=True)
def mock_external_modules():
    """Mock external API modules that may not be installed."""
    mock_modules = {}
    for mod_name in [
        "polymarket_api", "kalshi_api",
        "betfair_api", "smarkets_api", "sxbet_api", "matchbook_api",
        "ib_insync",
        "ws_feeds", "db", "risk_manager", "executor",
    ]:
        if mod_name not in sys.modules:
            mock_modules[mod_name] = MagicMock()
            sys.modules[mod_name] = mock_modules[mod_name]
    yield
    for mod_name in mock_modules:
        if mod_name in sys.modules:
            del sys.modules[mod_name]
    # Only remove the scan module under test — NOT scans.helpers or scans.__init__
    # which may be referenced by other test modules (e.g. test_helpers).
    if "scans.gemini" in sys.modules:
        del sys.modules["scans.gemini"]


def _make_binary_event(event_id, title, yes_price, no_price):
    return {
        "id": event_id,
        "title": title,
        "type": "binary",
        "category": "test",
        "contracts": [
            {"id": "c1", "label": "Yes", "price": yes_price,
             "instrumentSymbol": f"{event_id}-YES", "outcome": "yes"},
            {"id": "c2", "label": "No", "price": no_price,
             "instrumentSymbol": f"{event_id}-NO", "outcome": "no"},
        ],
        "status": "active",
    }


def _make_categorical_event(event_id, title, prices):
    contracts = []
    for i, p in enumerate(prices):
        contracts.append({
            "id": f"c{i}",
            "label": f"Option {i+1}",
            "price": p,
            "instrumentSymbol": f"{event_id}-O{i+1}",
            "outcome": f"option_{i+1}",
        })
    return {
        "id": event_id,
        "title": title,
        "type": "categorical",
        "category": "test",
        "contracts": contracts,
        "status": "active",
    }


# ============================================================
# scan_gemini_binary tests
# ============================================================


class TestScanGeminiBinary:
    def _import_scan(self):
        if "scans.gemini" in sys.modules:
            del sys.modules["scans.gemini"]
        from scans.gemini import scan_gemini_binary
        return scan_gemini_binary

    def test_finds_profitable_under_round(self):
        scan_fn = self._import_scan()
        client = MagicMock()
        client.authenticated = True
        # YES=0.40, NO=0.40 -> total=0.80, profit=0.20
        client.fetch_all_markets.return_value = [
            _make_binary_event("E1", "Test Market", 0.40, 0.40),
        ]
        client.get_market_price.return_value = (0.40, 0.40)
        client.get_order_book.return_value = {"asks": [{"price": 0.40, "amount": 100}]}

        result = scan_fn(client, min_profit=0.001)
        assert len(result) >= 1
        assert result[0]["type"] == "GeminiBinary"
        assert result[0]["net_profit"] > 0
        assert "_gm_event_id" in result[0]
        assert "_gm_yes_symbol" in result[0]
        assert "_gm_no_symbol" in result[0]

    def test_no_arb_when_sum_exceeds_one(self):
        scan_fn = self._import_scan()
        client = MagicMock()
        client.authenticated = True
        # YES=0.55, NO=0.50 -> total=1.05
        client.fetch_all_markets.return_value = [
            _make_binary_event("E1", "Test", 0.55, 0.50),
        ]
        client.get_market_price.return_value = (0.55, 0.50)

        result = scan_fn(client, min_profit=0.001)
        assert len(result) == 0

    def test_handles_empty_markets(self):
        scan_fn = self._import_scan()
        client = MagicMock()
        client.authenticated = True
        client.fetch_all_markets.return_value = []

        result = scan_fn(client, min_profit=0.001)
        assert result == []

    def test_returns_empty_when_not_authenticated(self):
        scan_fn = self._import_scan()
        client = MagicMock()
        client.authenticated = False

        result = scan_fn(client, min_profit=0.001)
        assert result == []

    def test_returns_empty_for_none_client(self):
        scan_fn = self._import_scan()
        result = scan_fn(None, min_profit=0.001)
        assert result == []


# ============================================================
# scan_gemini_multi tests
# ============================================================


class TestScanGeminiMulti:
    def _import_scan(self):
        if "scans.gemini" in sys.modules:
            del sys.modules["scans.gemini"]
        from scans.gemini import scan_gemini_multi
        return scan_gemini_multi

    def test_finds_profitable_categorical(self):
        scan_fn = self._import_scan()
        client = MagicMock()
        client.authenticated = True
        # 4 outcomes at 0.22 each -> total=0.88 (above the implausible-sum
        # floor), profit=0.12
        client.fetch_all_markets.return_value = [
            _make_categorical_event("E1", "Who wins?", [0.22, 0.22, 0.22, 0.22]),
        ]
        client.get_order_book.return_value = {"asks": [{"price": 0.22, "amount": 50}]}

        result = scan_fn(client, min_profit=0.001)
        assert len(result) >= 1
        assert result[0]["type"] == "GeminiMulti"
        assert result[0]["net_profit"] > 0
        assert "_gm_event_id" in result[0]
        assert "_gm_symbols" in result[0]
        assert "_gm_prices" in result[0]

    def test_no_arb_when_sum_exceeds_one(self):
        scan_fn = self._import_scan()
        client = MagicMock()
        client.authenticated = True
        # 3 outcomes: 0.40 + 0.40 + 0.30 = 1.10
        client.fetch_all_markets.return_value = [
            _make_categorical_event("E1", "Test", [0.40, 0.40, 0.30]),
        ]

        result = scan_fn(client, min_profit=0.001)
        assert len(result) == 0

    def test_skips_events_with_fewer_than_3_contracts(self):
        scan_fn = self._import_scan()
        client = MagicMock()
        client.authenticated = True
        # Binary event misclassified as categorical — should be skipped
        client.fetch_all_markets.return_value = [{
            "id": "E1", "title": "Test", "type": "categorical",
            "contracts": [{"price": 0.4, "instrumentSymbol": "S1"},
                          {"price": 0.4, "instrumentSymbol": "S2"}],
        }]

        result = scan_fn(client, min_profit=0.001)
        assert len(result) == 0

    def test_returns_empty_when_not_authenticated(self):
        scan_fn = self._import_scan()
        client = MagicMock()
        client.authenticated = False

        result = scan_fn(client, min_profit=0.001)
        assert result == []


class TestGeminiMultiImplausibleSumGate:
    """GEMINI_MULTI_MIN_SUM: outcome sets summing far below $1 are not
    exhaustive MECE sets (point-spread alternative lines) — buying them all
    does not guarantee a $1 payout. 2026-07-24 paper-window FP class."""

    def _import_scan(self):
        if "scans.gemini" in sys.modules:
            del sys.modules["scans.gemini"]
        from scans.gemini import scan_gemini_multi
        return scan_gemini_multi

    def test_non_exhaustive_point_spread_filtered(self):
        scan_fn = self._import_scan()
        client = MagicMock()
        client.authenticated = True
        # Alternative spread lines summing to 0.52 — the prod phantom that
        # showed as a ~90% ROI riskless arb.
        client.fetch_all_markets.return_value = [
            _make_categorical_event("E1", "Toronto vs D.C.: Point Spread",
                                    [0.20, 0.17, 0.15]),
        ]
        result = scan_fn(client, min_profit=0.001)
        assert result == []

    def test_sum_just_above_floor_passes(self):
        scan_fn = self._import_scan()
        client = MagicMock()
        client.authenticated = True
        client.fetch_all_markets.return_value = [
            _make_categorical_event("E1", "Election winner",
                                    [0.30, 0.30, 0.27]),
        ]
        client.get_order_book.return_value = {"asks": [{"price": 0.30, "amount": 50}]}
        result = scan_fn(client, min_profit=0.001)
        assert len(result) == 1
        assert result[0]["type"] == "GeminiMulti"

    def test_floor_respects_config_override(self, monkeypatch):
        import config
        monkeypatch.setattr(config, "GEMINI_MULTI_MIN_SUM", 0.40)
        scan_fn = self._import_scan()
        client = MagicMock()
        client.authenticated = True
        client.fetch_all_markets.return_value = [
            _make_categorical_event("E1", "Toronto vs D.C.: Point Spread",
                                    [0.20, 0.17, 0.15]),
        ]
        client.get_order_book.return_value = {"asks": [{"price": 0.20, "amount": 50}]}
        result = scan_fn(client, min_profit=0.001)
        assert len(result) == 1
