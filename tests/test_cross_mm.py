"""Tests for Strategy #11: cross-platform market making.

Covers:
- scan_cross_mm produces a CrossPlatformMM opp dict with both legs and the
  expected fields when the cross-platform spread clears the threshold
- scan_cross_mm filters out non-whitelisted platforms
- scan_cross_mm filters out spreads below the minimum
- CrossPlatformMaker.add_pair / refresh_quotes_paired tracks state and posts legs
- CrossPlatformMaker.on_fill cancels the sibling leg and triggers hedge_inventory
"""

import sys, os
import pytest
from unittest.mock import MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


@pytest.fixture(autouse=True)
def _isolate_modules():
    # Only pop modules we own — popping `fees` or `market_maker` here would
    # break later tests that imported names from those modules at module
    # load time, since their bound references would point at the old copy
    # while our fresh import lives under the same dotted name.
    for mod in ("scans.cross_mm",):
        sys.modules.pop(mod, None)
    yield


# ---------------------------------------------------------------------------
# scan_cross_mm
# ---------------------------------------------------------------------------

class TestScanCrossMM:
    def _import(self):
        from scans.cross_mm import scan_cross_mm
        return scan_cross_mm

    def test_emits_opp_when_spread_above_threshold(self, monkeypatch):
        import fees as fees_mod
        monkeypatch.setattr(
            fees_mod, "net_profit_cross_generic",
            lambda *a, **kw: {"net_profit": 0.01, "fees": 0.005, "gross_spread": 0.05},
        )
        scan_cross_mm = self._import()
        pairs = [{
            "platform_a": "polymarket",
            "platform_b": "kalshi",
            "market_key": "presidential_2028",
            "market_a": {"yes_bid": 0.45, "yes_ask": 0.47, "question": "Will X win?"},
            "market_b": {"yes_bid": 0.52, "yes_ask": 0.55, "question": "Will X win?"},
        }]
        opps = scan_cross_mm(pairs, min_spread=0.04, quote_size=5.0,
                             platforms_whitelist=("polymarket", "kalshi"))
        assert len(opps) == 1
        opp = opps[0]
        assert opp["type"] == "CrossPlatformMM"
        assert opp["_layer"] == 3
        assert "_leg_a" in opp and "_leg_b" in opp
        assert opp["_leg_a"]["platform"] in ("polymarket", "kalshi")
        assert opp["_leg_b"]["platform"] in ("polymarket", "kalshi")
        assert opp["_leg_a"]["platform"] != opp["_leg_b"]["platform"]
        assert opp["_leg_a"]["size"] == 5.0
        assert opp["_leg_b"]["size"] == 5.0
        assert opp["net_profit"] > 0

    def test_filters_non_whitelisted_platforms(self):
        scan_cross_mm = self._import()
        pairs = [{
            "platform_a": "polymarket", "platform_b": "ibkr",  # ibkr blocked
            "market_key": "x",
            "market_a": {"yes_bid": 0.45, "yes_ask": 0.47},
            "market_b": {"yes_bid": 0.52, "yes_ask": 0.55},
        }]
        opps = scan_cross_mm(pairs, platforms_whitelist=("polymarket", "kalshi"))
        assert opps == []

    def test_filters_low_spread(self, monkeypatch):
        import fees as fees_mod
        monkeypatch.setattr(
            fees_mod, "net_profit_cross_generic",
            lambda *a, **kw: {"net_profit": 0.001, "fees": 0.005, "gross_spread": 0.005},
        )
        scan_cross_mm = self._import()
        pairs = [{
            "platform_a": "polymarket", "platform_b": "kalshi",
            "market_key": "x",
            "market_a": {"yes_bid": 0.50, "yes_ask": 0.51},
            "market_b": {"yes_bid": 0.51, "yes_ask": 0.52},
        }]
        opps = scan_cross_mm(pairs, min_spread=0.04,
                             platforms_whitelist=("polymarket", "kalshi"))
        assert opps == []

    def test_skips_pairs_missing_prices(self):
        scan_cross_mm = self._import()
        pairs = [{
            "platform_a": "polymarket", "platform_b": "kalshi",
            "market_key": "x",
            "market_a": {},  # no bid/ask
            "market_b": {"yes_bid": 0.50, "yes_ask": 0.51},
        }]
        assert scan_cross_mm(pairs) == []

    def test_empty_input(self):
        scan_cross_mm = self._import()
        assert scan_cross_mm([]) == []


# ---------------------------------------------------------------------------
# CrossPlatformMaker
# ---------------------------------------------------------------------------

class TestCrossPlatformMaker:
    def _import(self):
        from market_maker import CrossPlatformMaker, InventoryTracker
        return CrossPlatformMaker, InventoryTracker

    def test_add_pair_and_refresh_places_two_legs(self):
        CrossPlatformMaker, InventoryTracker = self._import()
        mm = CrossPlatformMaker(quote_size=5.0, max_inventory=100.0)
        mm.add_pair(
            market_key="m1",
            platform_a="polymarket", platform_b="kalshi",
            mid_a=0.45, mid_b=0.55,
        )
        new_quotes = mm.refresh_quotes_paired()
        # In dry-run mode (default) place_quote returns synthetic order IDs;
        # we expect both bid+ask placed (one per platform side).
        assert len(new_quotes) == 2
        platforms = {q["platform"] for q in new_quotes}
        assert "polymarket" in platforms and "kalshi" in platforms

    def test_remove_pair_cancels_legs(self):
        CrossPlatformMaker, _ = self._import()
        mm = CrossPlatformMaker(quote_size=5.0, max_inventory=100.0)
        mm.add_pair(market_key="m1", platform_a="polymarket", platform_b="kalshi",
                    mid_a=0.45, mid_b=0.55)
        mm.refresh_quotes_paired()
        mm.remove_pair("m1")
        assert mm.quote_manager.get_active_orders("m1") == []

    def test_on_fill_cancels_sibling_and_triggers_hedge(self):
        CrossPlatformMaker, _ = self._import()
        hedger = MagicMock()
        hedger.hedge_inventory.return_value = True
        mm = CrossPlatformMaker(
            quote_size=5.0, max_inventory=100.0,
            hedger=hedger, auto_hedge_enabled=True,
        )
        mm.add_pair(market_key="m1", platform_a="polymarket", platform_b="kalshi",
                    mid_a=0.45, mid_b=0.55)
        mm.refresh_quotes_paired()

        # Pretend the polymarket bid filled; pull its order_id from active orders
        active = mm.quote_manager.get_active_orders("m1")
        pm_orders = [o for o in active if o["platform"] == "polymarket"]
        assert pm_orders, "expected at least one polymarket leg"
        order_id = pm_orders[0]["order_id"]

        mm.on_fill(
            order_id=order_id, market_key="m1", platform="polymarket",
            side="bid", price=0.45, size=5.0,
        )

        # Sibling leg(s) should be canceled — at most one residual order remains
        # and it must NOT be the filled one.
        residual = mm.quote_manager.get_active_orders("m1")
        assert order_id not in {o["order_id"] for o in residual}
        # Hedge invoked exactly once on the filled side
        hedger.hedge_inventory.assert_called_once()

    def test_on_fill_no_op_when_hedge_disabled(self):
        CrossPlatformMaker, _ = self._import()
        hedger = MagicMock()
        mm = CrossPlatformMaker(
            quote_size=5.0, max_inventory=100.0,
            hedger=hedger, auto_hedge_enabled=False,
        )
        mm.add_pair(market_key="m1", platform_a="polymarket", platform_b="kalshi",
                    mid_a=0.45, mid_b=0.55)
        mm.refresh_quotes_paired()
        order_id = mm.quote_manager.get_active_orders("m1")[0]["order_id"]
        mm.on_fill(order_id=order_id, market_key="m1", platform="polymarket",
                   side="bid", price=0.45, size=5.0)
        hedger.hedge_inventory.assert_not_called()

    def test_live_fill_uses_stored_trader_and_retains_failed_cancel(self):
        CrossPlatformMaker, _ = self._import()
        mm = CrossPlatformMaker(dry_run=False)
        mm.add_pair(market_key="m1", platform_a="polymarket",
                    platform_b="kalshi", mid_a=0.45, mid_b=0.55)
        trader = MagicMock()
        trader.cancel_order.return_value = False
        mm._traders["kalshi"] = trader
        mm.quote_manager._active_orders["live_sibling"] = {
            "platform": "kalshi", "market_key": "m1", "side": "ask",
            "price": 0.55, "size": 5.0, "status": "resting",
            "placed_at": 1.0,
        }

        mm.on_fill(order_id="filled_elsewhere", market_key="m1",
                   platform="polymarket", side="bid", price=0.45, size=5.0)
        assert mm.quote_manager.get_active_orders("m1")[0]["order_id"] == "live_sibling"

        trader.cancel_order.return_value = True
        mm.on_fill(order_id="filled_elsewhere", market_key="m1",
                   platform="polymarket", side="bid", price=0.45, size=5.0)
        assert mm.quote_manager.get_active_orders("m1") == []

    def test_generate_opportunities(self):
        CrossPlatformMaker, _ = self._import()
        mm = CrossPlatformMaker(quote_size=5.0, max_inventory=100.0)
        mm.add_pair(market_key="m1", platform_a="polymarket", platform_b="kalshi",
                    mid_a=0.45, mid_b=0.55)
        opps = mm.generate_opportunities()
        assert len(opps) == 1
        assert opps[0]["type"] == "CrossPlatformMM"
        assert opps[0]["_leg_a"]["platform"] != opps[0]["_leg_b"]["platform"]
