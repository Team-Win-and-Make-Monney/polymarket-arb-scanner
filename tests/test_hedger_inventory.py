"""Tests for MM inventory-hedging (Strategy #12).

Covers:
- PartialFillHedger.hedge_inventory dispatches to the right platform helper
- DB audit row written with opportunity_id=-1 marker
- MarketMaker.on_fill triggers hedge_inventory when over threshold and flag enabled
- MarketMaker.on_fill is a no-op when flag disabled, even if over threshold
- Hedge exception is swallowed and logged (does not crash the MM loop)
"""

import sys, os
import pytest
from unittest.mock import MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from db import TradeDB


@pytest.fixture(autouse=True)
def mock_external_modules():
    mock_modules = {}
    for mod_name in [
        "polymarket_api", "kalshi_api",
        "betfair_api", "smarkets_api", "sxbet_api", "matchbook_api",
        "gemini_api", "ibkr_api",
    ]:
        if mod_name not in sys.modules:
            mock_modules[mod_name] = MagicMock()
            sys.modules[mod_name] = mock_modules[mod_name]
    yield
    for mod_name in mock_modules:
        del sys.modules[mod_name]


def _import_hedger():
    if "hedger" in sys.modules:
        del sys.modules["hedger"]
    from hedger import PartialFillHedger
    return PartialFillHedger


def _import_market_maker():
    if "market_maker" in sys.modules:
        del sys.modules["market_maker"]
    from market_maker import MarketMaker, InventoryTracker
    return MarketMaker, InventoryTracker


@pytest.fixture
def db():
    trade_db = TradeDB(":memory:")
    yield trade_db
    trade_db.close()


# ---------------------------------------------------------------------------
# hedge_inventory dispatch + DB audit row
# ---------------------------------------------------------------------------

class TestHedgeInventoryDispatch:
    def test_smarkets_dispatch(self, db):
        PartialFillHedger = _import_hedger()
        client = MagicMock()
        client.authenticated = True
        client.place_order.return_value = {"id": "h_1"}
        hedger = PartialFillHedger(smarkets_client=client, db=db)
        ok = hedger.hedge_inventory(
            market_key="sm_market_1",
            platform="smarkets",
            side="BACK",
            fill_price=0.40,
            size=5.0,
            market_id="sm_market_1",
            contract_id="sm_contract_1",
        )
        assert ok is True
        client.place_order.assert_called_once()

    def test_polymarket_dispatch(self, db):
        PartialFillHedger = _import_hedger()
        pm_trader = MagicMock()
        pm_trader.place_order.return_value = {"success": True, "order_id": "pm_h1"}
        pm_trader.get_order_status.return_value = {"status": "matched"}

        # Patch fetch_order_book / get_best_bid_ask in the polymarket_api stub
        sys.modules["polymarket_api"].fetch_order_book = MagicMock(
            return_value={"bids": [{"price": 0.39, "size": 100}]}
        )
        sys.modules["polymarket_api"].get_best_bid_ask = MagicMock(
            return_value={"bid": 0.39, "ask": 0.42}
        )

        hedger = PartialFillHedger(pm_trader=pm_trader, db=db)
        ok = hedger.hedge_inventory(
            market_key="pm_token_abc",
            platform="polymarket",
            side="bid",
            fill_price=0.40,
            size=5.0,
            token_id="pm_token_abc",
        )
        assert ok is True
        pm_trader.place_order.assert_called_once()

    def test_unknown_platform_returns_false(self, db):
        PartialFillHedger = _import_hedger()
        hedger = PartialFillHedger(db=db)
        ok = hedger.hedge_inventory(
            market_key="x", platform="unknown_platform",
            side="bid", fill_price=0.5, size=5.0,
        )
        assert ok is False

    def test_writes_audit_row_with_opportunity_id_minus_one(self, db):
        PartialFillHedger = _import_hedger()
        # Even when the platform helper rejects the trade, we still want the
        # audit row recorded for ops visibility into how often MM hedges fire.
        client = MagicMock()
        client.authenticated = False
        hedger = PartialFillHedger(smarkets_client=client, db=db)
        hedger.hedge_inventory(
            market_key="sm_x", platform="smarkets",
            side="BACK", fill_price=0.40, size=5.0,
            market_id="sm_x", contract_id="sm_c",
        )
        rows = db.get_pending_partial_fills()
        mm_rows = [r for r in rows if r.get("opportunity_id") == -1]
        assert len(mm_rows) == 1
        assert mm_rows[0]["platform"] == "smarkets"
        assert mm_rows[0]["fill_price"] == 0.40


# ---------------------------------------------------------------------------
# MarketMaker.on_fill wiring
# ---------------------------------------------------------------------------

class TestMarketMakerAutoHedge:
    def _setup(self, auto_hedge_enabled: bool, max_inventory: float = 10.0):
        MarketMaker, InventoryTracker = _import_market_maker()
        inv = InventoryTracker(max_per_market=max_inventory, max_total=100.0)
        hedger = MagicMock()
        hedger.hedge_inventory.return_value = True
        mm = MarketMaker(
            inventory=inv,
            max_inventory=max_inventory,
            hedger=hedger,
            auto_hedge_enabled=auto_hedge_enabled,
        )
        # Pre-load market metadata so on_fill has somewhere to update
        mm.add_market(
            market_key="mkt1",
            platform="smarkets",
            mid_price=0.50,
        )
        # Place a fake resting bid in the QuoteManager so on_fill can record it
        order_id = mm.quote_manager.place_quote(
            "smarkets", "mkt1", "bid", 0.49, 5.0,
        )
        return mm, hedger, order_id

    def test_fires_when_over_threshold_and_enabled(self):
        mm, hedger, order_id = self._setup(auto_hedge_enabled=True, max_inventory=10.0)
        # Fill 9.0 = 90% of max_inventory > 80% threshold → triggers hedge
        mm.on_fill(
            order_id=order_id,
            market_key="mkt1",
            platform="smarkets",
            side="bid",
            price=0.49,
            size=9.0,
            market_id="sm_market_1",
            contract_id="sm_contract_1",
        )
        hedger.hedge_inventory.assert_called_once()
        kwargs = hedger.hedge_inventory.call_args.kwargs
        assert kwargs["market_key"] == "mkt1"
        assert kwargs["platform"] == "smarkets"
        assert kwargs["side"] == "bid"
        assert kwargs["size"] == 9.0
        assert kwargs["market_id"] == "sm_market_1"

    def test_skipped_when_disabled(self):
        mm, hedger, order_id = self._setup(auto_hedge_enabled=False, max_inventory=10.0)
        mm.on_fill(
            order_id=order_id, market_key="mkt1", platform="smarkets",
            side="bid", price=0.49, size=9.0,
        )
        hedger.hedge_inventory.assert_not_called()

    def test_skipped_when_under_threshold(self):
        mm, hedger, order_id = self._setup(auto_hedge_enabled=True, max_inventory=10.0)
        # 5.0 = 50% of max — well under 80% threshold
        mm.on_fill(
            order_id=order_id, market_key="mkt1", platform="smarkets",
            side="bid", price=0.49, size=5.0,
        )
        hedger.hedge_inventory.assert_not_called()

    def test_hedge_exception_swallowed(self):
        mm, hedger, order_id = self._setup(auto_hedge_enabled=True, max_inventory=10.0)
        hedger.hedge_inventory.side_effect = RuntimeError("upstream API down")
        # Must not raise — MM loop must keep running
        mm.on_fill(
            order_id=order_id, market_key="mkt1", platform="smarkets",
            side="bid", price=0.49, size=9.0,
        )
        hedger.hedge_inventory.assert_called_once()
