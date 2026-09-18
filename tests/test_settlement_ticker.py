"""Regression tests for the Kalshi settlement-by-ticker fix.

Before the fix, positions stored the human-readable title in market_identifier
and check_settlements looked markets up by that title — which the Kalshi API
rejects — so positions never settled and realized_pnl stayed NULL. These tests
prove the ticker is stored and used.
"""

import os
import sys
import tempfile
import unittest
from unittest.mock import MagicMock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from db import TradeDB
import continuous


class TestPositionTickerStorage(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.db = TradeDB(self.tmp.name)

    def tearDown(self):
        self.db.close()
        os.unlink(self.tmp.name)

    def test_market_ticker_column_exists_and_persists(self):
        opp_id = self.db.log_opportunity("KalshiMulti(4)", "Chelsea: Spreads", "Y=0.40", 0.95, 0.05, 0.05, 1, "exec")
        pid = self.db.create_position(
            opportunity_id=opp_id,
            market_identifier="Chelsea: Spreads",
            platform="kalshi",
            expected_pnl=0.05,
            market_ticker="KXEPLSPREAD-26MAY19CFCTOT-CFC1",
        )
        pos = self.db.get_open_positions()[0]
        self.assertEqual(pos["id"], pid)
        self.assertEqual(pos["market_identifier"], "Chelsea: Spreads")
        self.assertEqual(pos["market_ticker"], "KXEPLSPREAD-26MAY19CFCTOT-CFC1")

    def test_market_ticker_defaults_none_for_legacy_rows(self):
        opp_id = self.db.log_opportunity("Binary", "Some Market", "Y=0.50", 0.99, 0.01, 0.01, 1, "exec")
        self.db.create_position(
            opportunity_id=opp_id, market_identifier="Some Market",
            platform="kalshi", expected_pnl=0.01,
        )
        pos = self.db.get_open_positions()[0]
        self.assertIsNone(pos["market_ticker"])


class TestCheckSettlementsUsesTicker(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.db = TradeDB(self.tmp.name)

    def tearDown(self):
        self.db.close()
        os.unlink(self.tmp.name)

    def _make_kalshi_client(self):
        client = MagicMock()
        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = {"market": {"result": "yes"}}
        client._request.return_value = resp
        return client

    def test_settlement_queries_by_ticker_and_settles(self):
        opp_id = self.db.log_opportunity("KalshiMulti(4)", "Chelsea: Spreads", "Y=0.40", 0.95, 0.05, 0.05, 1, "exec")
        # A filled trade so realized P&L can be computed.
        tid = self.db.log_trade(opp_id, "kalshi", "yes", 0.40, 1.0, "filled", fill_price=0.40)
        pid = self.db.create_position(
            opportunity_id=opp_id, market_identifier="Chelsea: Spreads",
            platform="kalshi", expected_pnl=0.05,
            market_ticker="KXEPLSPREAD-26MAY19CFCTOT-CFC1",
        )
        client = self._make_kalshi_client()

        continuous.check_settlements(self.db, client, None)

        # The API must have been called with the TICKER, not the title.
        called_path = client._request.call_args[0][1]
        self.assertIn("KXEPLSPREAD-26MAY19CFCTOT-CFC1", called_path)
        self.assertNotIn("Chelsea", called_path)
        # And the position must now be settled.
        self.assertEqual(self.db.get_open_positions_count(), 0)

    def test_legacy_row_without_ticker_falls_back_to_identifier(self):
        opp_id = self.db.log_opportunity("Binary", "KXLEGACY-1", "Y=0.50", 0.99, 0.01, 0.01, 1, "exec")
        self.db.log_trade(opp_id, "kalshi", "yes", 0.50, 1.0, "filled", fill_price=0.50)
        self.db.create_position(
            opportunity_id=opp_id, market_identifier="KXLEGACY-1",
            platform="kalshi", expected_pnl=0.01,
        )
        client = self._make_kalshi_client()

        continuous.check_settlements(self.db, client, None)

        called_path = client._request.call_args[0][1]
        self.assertIn("KXLEGACY-1", called_path)
        self.assertEqual(self.db.get_open_positions_count(), 0)


class TestPortfolioSettlementsReconciliation(unittest.TestCase):
    """Account-scoped /portfolio/settlements is the authoritative settlement
    source — one call covers all open Kalshi positions; per-market lookup is
    only the fallback for positions absent from the feed."""

    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.db = TradeDB(self.tmp.name)

    def tearDown(self):
        self.db.close()
        os.unlink(self.tmp.name)

    def _position(self, ticker, title="Some Market", side="yes", price=0.40):
        opp_id = self.db.log_opportunity("KalshiBinary", title, f"Y={price}", 0.95, 0.05, 0.05, 1, "exec")
        self.db.log_trade(opp_id, "kalshi", side, price, 1.0, "filled", fill_price=price)
        return self.db.create_position(
            opportunity_id=opp_id, market_identifier=title,
            platform="kalshi", expected_pnl=0.05, market_ticker=ticker,
        )

    def test_settles_from_portfolio_feed_without_market_lookup(self):
        self._position("KXTEST-1")
        client = MagicMock()
        client.get_settlements.return_value = [
            {"ticker": "KXTEST-1", "market_result": "yes", "revenue": 100},
        ]

        continuous.check_settlements(self.db, client, None)

        self.assertEqual(self.db.get_open_positions_count(), 0)
        client._request.assert_not_called()

    def test_position_missing_from_feed_falls_back_to_market_lookup(self):
        self._position("KXTEST-2")
        client = MagicMock()
        client.get_settlements.return_value = []  # not in the feed yet
        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = {"market": {"result": "no"}}
        client._request.return_value = resp

        continuous.check_settlements(self.db, client, None)

        called_path = client._request.call_args[0][1]
        self.assertIn("KXTEST-2", called_path)
        self.assertEqual(self.db.get_open_positions_count(), 0)

    def test_unsettled_market_stays_open(self):
        self._position("KXTEST-3")
        client = MagicMock()
        client.get_settlements.return_value = []
        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = {"market": {"result": ""}}  # not resolved yet
        client._request.return_value = resp

        continuous.check_settlements(self.db, client, None)

        self.assertEqual(self.db.get_open_positions_count(), 1)

    def test_settlements_fetch_error_falls_back(self):
        self._position("KXTEST-4")
        client = MagicMock()
        client.get_settlements.side_effect = RuntimeError("api down")
        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = {"market": {"result": "yes"}}
        client._request.return_value = resp

        continuous.check_settlements(self.db, client, None)

        self.assertEqual(self.db.get_open_positions_count(), 0)

    def test_winning_side_from_feed_drives_pnl(self):
        # Directional YES position, market resolved NO → realized loss, not
        # the legacy arb assumption of 1.0 - cost. Kalshi trades.size is the
        # requested DOLLAR amount; the executor places
        # max(1, int(size/price)) contracts: int(1.00/0.40) = 2 contracts at
        # 0.40 → cost 0.80. Losing settlement = -0.80.
        self._position("KXTEST-5", side="yes", price=0.40)
        client = MagicMock()
        client.get_settlements.return_value = [
            {"ticker": "KXTEST-5", "market_result": "no"},
        ]

        continuous.check_settlements(self.db, client, None)

        row = self.db.conn.execute(
            "SELECT realized_pnl FROM positions WHERE market_ticker='KXTEST-5'"
        ).fetchone()
        self.assertAlmostEqual(row[0], -0.80, places=4)


if __name__ == "__main__":
    unittest.main()
