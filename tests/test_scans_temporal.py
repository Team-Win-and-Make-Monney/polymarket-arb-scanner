"""Unit tests for scans/temporal.py cross-date temporal arbitrage."""

import os
import sys
from unittest.mock import MagicMock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scans.temporal import _refine_temporal_with_clob, scan_temporal_arb


class TestScansTemporal:
    """Test two-stage scan_temporal_arb and _refine_temporal_with_clob."""

    def _sample_markets(self) -> list[dict]:
        return [
            {
                "ticker": "KXBTC-26MAR31-T100000",
                "title": "Will Bitcoin reach $100,000 by March 31, 2026?",
                "yes_bid": 0.60,
                "yes_price": 0.60,
                "yes_ask": 0.62,
                "no_ask": 0.42,
            },
            {
                "ticker": "KXBTC-26JUN30-T100000",
                "title": "Will Bitcoin reach $100,000 by June 30, 2026?",
                "yes_bid": 0.48,
                "yes_price": 0.50,
                "yes_ask": 0.50,
                "no_ask": 0.52,
            },
        ]

    def test_scan_temporal_arb_detects_violation(self) -> None:
        markets = self._sample_markets()
        cands = scan_temporal_arb(markets, min_profit=0.01, min_violation=0.02)
        assert len(cands) == 1
        cand = cands[0]
        assert cand["type"] == "TemporalArb"
        assert cand["_early_ticker"] == "KXBTC-26MAR31-T100000"
        assert cand["_late_ticker"] == "KXBTC-26JUN30-T100000"
        assert cand["_p_early"] == 0.60
        assert cand["_p_late"] == 0.50
        assert cand["net_profit"] > 0.01
        assert cand["_asset"] == "BTC"
        assert cand["_strike"] == 100000.0

    def test_scan_temporal_arb_filters_small_violation(self) -> None:
        markets = [
            {
                "ticker": "KXBTC-26MAR31-T100000",
                "title": "Will Bitcoin reach $100k by March 31?",
                "yes_bid": 0.51,
                "yes_price": 0.51,
            },
            {
                "ticker": "KXBTC-26JUN30-T100000",
                "title": "Will Bitcoin reach $100k by June 30?",
                "yes_bid": 0.50,
                "yes_price": 0.50,
            },
        ]
        # Spread is 0.01 < min_violation 0.02
        cands = scan_temporal_arb(markets, min_profit=0.001, min_violation=0.02)
        assert len(cands) == 0

    def test_scan_temporal_arb_filters_unprofitable_after_fees(self) -> None:
        markets = [
            {
                "ticker": "KXBTC-26MAR31-T100000",
                "title": "Will Bitcoin reach $100k by March 31?",
                "yes_bid": 0.53,
                "yes_price": 0.53,
            },
            {
                "ticker": "KXBTC-26JUN30-T100000",
                "title": "Will Bitcoin reach $100k by June 30?",
                "yes_bid": 0.50,
                "yes_price": 0.50,
            },
        ]
        # Spread is 0.03, but after Kalshi fees (~0.035) net profit is negative
        cands = scan_temporal_arb(markets, min_profit=0.01, min_violation=0.02)
        assert len(cands) == 0

    def test_scan_temporal_funnel_tracking(self) -> None:
        mock_funnel = MagicMock()
        markets = self._sample_markets()
        cands = scan_temporal_arb(markets, min_profit=0.01, min_violation=0.02, funnel=mock_funnel)
        assert len(cands) == 1
        mock_funnel.record_screened.assert_called_once_with(1)
        mock_funnel.record_mid_candidates.assert_called_once_with(1)

    def test_refine_temporal_with_clob_orderbook(self) -> None:
        cand = {
            "type": "TemporalArb",
            "_early_ticker": "KXBTC-26MAR31-T100000",
            "_late_ticker": "KXBTC-26JUN30-T100000",
            "_sup_market": {
                "orderbook": {
                    "orderbook_fp": {
                        "yes_dollars": [["0.45", "100"]],
                        "no_dollars": [["0.51", "100"]],  # NO bid 0.51 -> YES ask = 0.49
                    }
                }
            },
            "_sub_market": {
                "orderbook": {
                    "orderbook_fp": {
                        "yes_dollars": [["0.65", "120"]],  # YES bid 0.65 -> NO ask = 0.35
                        "no_dollars": [["0.30", "150"]],
                    }
                }
            },
            "net_profit": 0.08,
            "_p_early": 0.65,
            "_p_late": 0.49,
        }

        refined = _refine_temporal_with_clob([cand], min_profit=0.01)
        assert len(refined) == 1
        ref = refined[0]
        assert ref["_kalshi_late_yes"] == 0.49
        assert ref["_kalshi_early_no"] == 0.35
        assert ref["_clob_depth"] == 100  # min(100, 120)
        assert ref["net_profit"] > 0.05

    def test_refine_temporal_drops_when_clob_spread_erodes(self) -> None:
        cand = {
            "type": "TemporalArb",
            "_early_ticker": "KXBTC-26MAR31-T100000",
            "_late_ticker": "KXBTC-26JUN30-T100000",
            "_sup_market": {
                "orderbook": {
                    "yes": [{"price": 0.58, "quantity": 50}],  # Late ask moved up to 0.58
                }
            },
            "_sub_market": {
                "orderbook": {
                    "no": [{"price": 0.45, "quantity": 50}],   # Early NO ask moved up to 0.45 (total cost 1.03)
                }
            },
            "net_profit": 0.08,
        }

        mock_funnel = MagicMock()
        refined = _refine_temporal_with_clob([cand], min_profit=0.01, funnel=mock_funnel)
        assert len(refined) == 0
        mock_funnel.record_clob_dropped.assert_called_once_with(1)
