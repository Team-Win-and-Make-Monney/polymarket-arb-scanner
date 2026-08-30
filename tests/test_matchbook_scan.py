"""Tests for Matchbook scan functions — back-all and back-lay arbitrage detection."""

import pytest
from unittest.mock import MagicMock, patch

import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


@pytest.fixture(autouse=True)
def mock_external_modules():
    """Mock external API modules that may not be installed."""
    mock_modules = {}
    for mod_name in [
        "polymarket_api", "kalshi_api",
        "betfair_api", "smarkets_api", "sxbet_api", "matchbook_api",
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
    if "scans.matchbook" in sys.modules:
        del sys.modules["scans.matchbook"]


def _make_runner(runner_id, back_odds=None, lay_odds=None,
                 back_amount=100, lay_amount=100, name="Runner"):
    """Create a Matchbook runner dict with price data."""
    prices = []
    if back_odds is not None:
        prices.append({
            "side": "back",
            "odds": back_odds,
            "available-amount": back_amount,
        })
    if lay_odds is not None:
        prices.append({
            "side": "lay",
            "odds": lay_odds,
            "available-amount": lay_amount,
        })
    return {
        "id": runner_id,
        "name": name,
        "prices": prices,
    }


def _make_market(market_id, event_name="Test Event", market_name="Test Market",
                 event_id="evt_1"):
    """Create a Matchbook market dict with _event parent attached."""
    return {
        "id": market_id,
        "name": market_name,
        "_event": {
            "id": event_id,
            "name": event_name,
        },
    }


# ============================================================
# scan_matchbook_backall tests
# ============================================================


class TestScanMatchbookBackAll:
    def _import_scan(self):
        """Import scan module with mocked fee functions."""
        if "scans.matchbook" in sys.modules:
            del sys.modules["scans.matchbook"]

        # Mock the fee functions that don't exist yet
        fees_mod = sys.modules.get("fees")
        if fees_mod is None:
            fees_mod = MagicMock()
            sys.modules["fees"] = fees_mod

        # net_profit_matchbook_backall: 0% commission, pure spread
        def mock_backall(implied_probs):
            total = sum(implied_probs)
            gross = 1.0 - total
            if gross <= 0:
                return {"gross_spread": gross, "fees": 0, "net_profit": gross}
            return {"gross_spread": gross, "fees": 0, "net_profit": gross}

        fees_mod.net_profit_matchbook_backall = mock_backall
        fees_mod.net_profit_matchbook_backlay = MagicMock()

        from scans.matchbook import scan_matchbook_backall
        return scan_matchbook_backall

    def test_finds_profitable_backall(self):
        """Under-round book: sum of implied probs < 1.0 should yield opportunity."""
        scan_fn = self._import_scan()

        client = MagicMock()
        client.authenticated = True

        # Two runners at 2.50 decimal odds each -> implied probs = 0.40 each -> sum = 0.80
        market = _make_market("mkt_1")
        client.fetch_all_markets.return_value = [market]
        client.list_runners.return_value = [
            _make_runner("r1", back_odds=2.50, name="Yes"),
            _make_runner("r2", back_odds=2.50, name="No"),
        ]

        result = scan_fn(client, min_profit=0.001)
        assert len(result) >= 1
        assert result[0]["type"] == "MatchbookBackAll"
        assert result[0]["net_profit"] > 0
        assert "_mb_market_id" in result[0]
        assert "_mb_runner_ids" in result[0]

    def test_depth_uses_selected_best_back_quotes(self):
        scan_fn = self._import_scan()
        client = MagicMock()
        client.authenticated = True
        client.fetch_all_markets.return_value = [_make_market("mkt_depth")]
        client.list_runners.return_value = [
            {
                "id": "r1",
                "prices": [
                    {"side": "back", "odds": 2.0, "available-amount": 1},
                    {"side": "back", "odds": 2.5, "available-amount": 80},
                ],
            },
            {
                "id": "r2",
                "prices": [
                    {"side": "back", "odds": 2.1, "available-amount": 2},
                    {"side": "back", "odds": 2.5, "available-amount": 60},
                ],
            },
        ]

        result = scan_fn(client, min_profit=0.001)
        assert result[0]["_clob_depth"] == 60

    def test_no_arb_when_overround(self):
        """Overround book: sum of implied probs > 1.0 should yield no opportunity."""
        scan_fn = self._import_scan()

        client = MagicMock()
        client.authenticated = True

        # Two runners at 1.80 decimal odds each -> implied probs = 0.556 each -> sum = 1.11
        market = _make_market("mkt_2")
        client.fetch_all_markets.return_value = [market]
        client.list_runners.return_value = [
            _make_runner("r1", back_odds=1.80, name="Yes"),
            _make_runner("r2", back_odds=1.80, name="No"),
        ]

        result = scan_fn(client, min_profit=0.001)
        assert len(result) == 0

    def test_handles_empty_markets(self):
        """Empty market list should return no opportunities."""
        scan_fn = self._import_scan()

        client = MagicMock()
        client.authenticated = True
        client.fetch_all_markets.return_value = []

        result = scan_fn(client, min_profit=0.001)
        assert result == []

    def test_skips_runners_without_back_prices(self):
        """Runners without back prices should cause the market to be skipped."""
        scan_fn = self._import_scan()

        client = MagicMock()
        client.authenticated = True

        market = _make_market("mkt_3")
        client.fetch_all_markets.return_value = [market]
        # One runner has back price, the other has only lay price
        client.list_runners.return_value = [
            _make_runner("r1", back_odds=2.50, name="Yes"),
            _make_runner("r2", lay_odds=2.00, name="No"),  # no back price
        ]

        result = scan_fn(client, min_profit=0.001)
        assert len(result) == 0

    def test_returns_empty_when_not_authenticated(self):
        """Unauthenticated client should return empty list."""
        scan_fn = self._import_scan()

        client = MagicMock()
        client.authenticated = False

        result = scan_fn(client, min_profit=0.001)
        assert result == []

    def test_returns_empty_for_none_client(self):
        """None client should return empty list."""
        scan_fn = self._import_scan()
        result = scan_fn(None, min_profit=0.001)
        assert result == []


# ============================================================
# scan_matchbook_backlay tests
# ============================================================


class TestScanMatchbookBackLay:
    def _import_scan(self):
        """Import scan module with mocked fee functions."""
        if "scans.matchbook" in sys.modules:
            del sys.modules["scans.matchbook"]

        fees_mod = sys.modules.get("fees")
        if fees_mod is None:
            fees_mod = MagicMock()
            sys.modules["fees"] = fees_mod

        # net_profit_matchbook_backlay: 0% commission, pure spread
        def mock_backlay(back_price, lay_price):
            if lay_price <= back_price:
                return {"gross_spread": 0, "fees": 0, "net_profit": 0}
            gross = lay_price - back_price
            return {"gross_spread": gross, "fees": 0, "net_profit": gross}

        fees_mod.net_profit_matchbook_backlay = mock_backlay
        fees_mod.net_profit_matchbook_backall = MagicMock()

        from scans.matchbook import scan_matchbook_backlay
        return scan_matchbook_backlay

    def test_finds_crossed_book(self):
        """Crossed book: back odds > lay odds should yield opportunity."""
        scan_fn = self._import_scan()

        client = MagicMock()
        client.authenticated = True

        market = _make_market("mkt_1")
        client.fetch_all_markets.return_value = [market]
        # Back at 3.00 (prob 0.333), Lay at 2.50 (prob 0.400)
        # back_odds (3.00) > lay_odds (2.50) -> crossed
        # back_prob (0.333) < lay_prob (0.400) -> profit
        client.list_runners.return_value = [
            _make_runner("r1", back_odds=3.00, lay_odds=2.50, name="Yes"),
        ]

        result = scan_fn(client, min_profit=0.001)
        assert len(result) >= 1
        assert result[0]["type"] == "MatchbookBackLay"
        assert result[0]["net_profit"] > 0
        assert "_mb_market_id" in result[0]
        assert "_mb_runner_id" in result[0]

    def test_no_arb_when_not_crossed(self):
        """Normal book: back odds < lay odds should yield no opportunity."""
        scan_fn = self._import_scan()

        client = MagicMock()
        client.authenticated = True

        market = _make_market("mkt_2")
        client.fetch_all_markets.return_value = [market]
        # Back at 2.00, Lay at 2.50 -> not crossed (back < lay)
        client.list_runners.return_value = [
            _make_runner("r1", back_odds=2.00, lay_odds=2.50, name="Yes"),
        ]

        result = scan_fn(client, min_profit=0.001)
        assert len(result) == 0

    def test_handles_empty_markets(self):
        """Empty market list should return no opportunities."""
        scan_fn = self._import_scan()

        client = MagicMock()
        client.authenticated = True
        client.fetch_all_markets.return_value = []

        result = scan_fn(client, min_profit=0.001)
        assert result == []

    def test_skips_runner_missing_side(self):
        """Runner with only back (no lay) should be skipped."""
        scan_fn = self._import_scan()

        client = MagicMock()
        client.authenticated = True

        market = _make_market("mkt_3")
        client.fetch_all_markets.return_value = [market]
        client.list_runners.return_value = [
            _make_runner("r1", back_odds=2.50, name="Yes"),  # no lay
        ]

        result = scan_fn(client, min_profit=0.001)
        assert len(result) == 0

    def test_returns_empty_when_not_authenticated(self):
        """Unauthenticated client should return empty list."""
        scan_fn = self._import_scan()

        client = MagicMock()
        client.authenticated = False

        result = scan_fn(client, min_profit=0.001)
        assert result == []

    def test_returns_empty_for_none_client(self):
        """None client should return empty list."""
        scan_fn = self._import_scan()
        result = scan_fn(None, min_profit=0.001)
        assert result == []
