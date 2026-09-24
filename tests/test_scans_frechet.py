"""Tests for scans/frechet.py — Fréchet implication bound arbitrage."""

import pytest
from unittest.mock import MagicMock, patch
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import importlib

def _frechet():
    return sys.modules.get("scans.frechet") or importlib.import_module("scans.frechet")


class TestScansFrechet:
    def test_scan_frechet_polymarket_violation_found(self):
        m_sub = {
            "condition_id": "0xsub",
            "title": "Will BTC be above $100k by Dec 31?",
            "end_date_iso": "2026-12-31T00:00:00Z",
            "outcomePrices": ["0.70", "0.30"],
            "clobTokenIds": ["tok_sub_yes", "tok_sub_no"],
        }
        m_sup = {
            "condition_id": "0xsup",
            "title": "Will BTC be above $90k by Dec 31?",
            "end_date_iso": "2026-12-31T00:00:00Z",
            "outcomePrices": ["0.45", "0.55"],
            "clobTokenIds": ["tok_sup_yes", "tok_sup_no"],
        }
        # Violation: P(A=100k) = 0.70 > P(B=90k) = 0.45
        candidates = _frechet().scan_frechet([m_sub, m_sup], platform="polymarket", min_profit=0.01)
        assert len(candidates) == 1
        cand = candidates[0]
        assert cand["type"] == "FrechetArb"
        assert cand["_layer"] == 1
        assert cand["net_profit"] > 0
        assert cand["_p_a"] == pytest.approx(0.70)
        assert cand["_p_b"] == pytest.approx(0.45)
        assert cand["_buy_yes_token"] == "tok_sup_yes"
        assert cand["_buy_no_token"] == "tok_sub_no"
        assert cand["_token_ids"] == ["tok_sup_yes", "tok_sub_no"]

    def test_scan_frechet_coherent_no_candidate(self):
        m_sub = {
            "condition_id": "0xsub",
            "title": "Will BTC be above $100k by Dec 31?",
            "end_date_iso": "2026-12-31T00:00:00Z",
            "outcomePrices": ["0.40", "0.60"],
            "clobTokenIds": ["tok_sub_yes", "tok_sub_no"],
        }
        m_sup = {
            "condition_id": "0xsup",
            "title": "Will BTC be above $90k by Dec 31?",
            "end_date_iso": "2026-12-31T00:00:00Z",
            "outcomePrices": ["0.65", "0.35"],
            "clobTokenIds": ["tok_sup_yes", "tok_sup_no"],
        }
        # Coherent: P(A=100k) = 0.40 <= P(B=90k) = 0.65
        candidates = _frechet().scan_frechet([m_sub, m_sup], platform="polymarket")
        assert len(candidates) == 0

    def test_scan_frechet_min_violation_filter(self):
        m_sub = {
            "condition_id": "0xsub",
            "title": "Will BTC be above $100k by Dec 31?",
            "end_date_iso": "2026-12-31T00:00:00Z",
            "outcomePrices": ["0.51", "0.49"],
            "clobTokenIds": ["tok_sub_yes", "tok_sub_no"],
        }
        m_sup = {
            "condition_id": "0xsup",
            "title": "Will BTC be above $90k by Dec 31?",
            "end_date_iso": "2026-12-31T00:00:00Z",
            "outcomePrices": ["0.50", "0.50"],
            "clobTokenIds": ["tok_sup_yes", "tok_sup_no"],
        }
        # Spread is 0.01 < min_violation=0.02
        candidates = _frechet().scan_frechet([m_sub, m_sup], min_violation=0.02)
        assert len(candidates) == 0

    def test_scan_frechet_kalshi(self):
        m_sub = {
            "ticker": "KXBTC-26DEC31-T100000",
            "title": "Will BTC be above $100,000 on Dec 31, 2026?",
            "yes_bid": 0.70,
            "platform": "kalshi",
        }
        m_sup = {
            "ticker": "KXBTC-26DEC31-T90000",
            "title": "Will BTC be above $90,000 on Dec 31, 2026?",
            "yes_ask": 0.50,
            "platform": "kalshi",
        }
        candidates = _frechet().scan_frechet([m_sub, m_sup], platform="kalshi")
        assert len(candidates) == 1
        cand = candidates[0]
        assert cand["_platform"] == "kalshi"
        assert cand["_buy_yes_ticker"] == "KXBTC-26DEC31-T90000"
        assert cand["_buy_no_ticker"] == "KXBTC-26DEC31-T100000"

    def test_scan_frechet_funnel_tracking(self):
        m_sub = {
            "condition_id": "0xsub",
            "title": "Will BTC be above $100k by Dec 31?",
            "end_date_iso": "2026-12-31T00:00:00Z",
            "outcomePrices": ["0.70", "0.30"],
            "clobTokenIds": ["tok_sub_yes", "tok_sub_no"],
        }
        m_sup = {
            "condition_id": "0xsup",
            "title": "Will BTC be above $90k by Dec 31?",
            "end_date_iso": "2026-12-31T00:00:00Z",
            "outcomePrices": ["0.45", "0.55"],
            "clobTokenIds": ["tok_sup_yes", "tok_sup_no"],
        }
        funnel = MagicMock()
        candidates = _frechet().scan_frechet([m_sub, m_sup], platform="polymarket", funnel=funnel)
        assert len(candidates) == 1
        funnel.record_screened.assert_called_once_with(1)
        funnel.record_mid_candidates.assert_called_once_with(1)

    def test_refine_frechet_with_clob_profitable(self):
        m_sub = {
            "condition_id": "0xsub",
            "title": "Will BTC be above $100k by Dec 31?",
            "end_date_iso": "2026-12-31T00:00:00Z",
        }
        m_sup = {
            "condition_id": "0xsup",
            "title": "Will BTC be above $90k by Dec 31?",
            "end_date_iso": "2026-12-31T00:00:00Z",
        }
        cand = {
            "type": "FrechetArb",
            "_platform": "polymarket",
            "_sub_market": m_sub,
            "_sup_market": m_sup,
            "prices": "initial",
        }

        # Mock CLOB fetch:
        # Superset B: yes_ask = 0.45, size = 50.0
        # Subset A: no_ask = 0.30 (implied P(A) = 0.70), size = 40.0
        # Total cost = 0.45 + 0.30 = 0.75 -> Gross = 0.25
        def mock_fetch(market, cache):
            if market["condition_id"] == "0xsup":
                return market, {"yes_ask": 0.45, "yes_ask_size": 50.0}
            else:
                return market, {"no_ask": 0.30, "no_ask_size": 40.0}

        funnel = MagicMock()
        with patch("scans.frechet._fetch_clob_for_market", side_effect=mock_fetch):
            refined = _frechet()._refine_frechet_with_clob([cand], min_profit=0.01, funnel=funnel)

        assert len(refined) == 1
        res = refined[0]
        assert res["_clob_refined"] is True
        assert res["_clob_depth"] == 40.0  # min(50.0, 40.0)
        assert res["_p_a"] == pytest.approx(0.70)
        assert res["_p_b"] == pytest.approx(0.45)
        funnel.record_clob_evaluated.assert_called_once_with(1)
        funnel.record_surfaced.assert_called_once_with(1)

    def test_refine_frechet_unknown_depth_is_zero_not_none(self):
        """Cached books can carry no sizes; the surfaced depth must stay numeric (fails closed)."""
        m_sub = {"condition_id": "0xsub", "title": "BTC above $100k?"}
        m_sup = {"condition_id": "0xsup", "title": "BTC above $90k?"}
        cand = {"type": "FrechetArb", "_platform": "polymarket", "_sub_market": m_sub, "_sup_market": m_sup}

        def mock_fetch(market, cache):
            if market["condition_id"] == "0xsup":
                return market, {"yes_ask": 0.45, "yes_ask_size": 0}
            return market, {"no_ask": 0.30, "no_ask_size": 0}

        with patch("scans.frechet._fetch_clob_for_market", side_effect=mock_fetch):
            refined = _frechet()._refine_frechet_with_clob([cand], min_profit=0.01)

        assert len(refined) == 1
        assert refined[0]["_clob_depth"] == 0.0

    def test_refine_frechet_with_clob_drops_unprofitable(self):
        m_sub = {"condition_id": "0xsub"}
        m_sup = {"condition_id": "0xsup"}
        cand = {
            "type": "FrechetArb",
            "_platform": "polymarket",
            "_sub_market": m_sub,
            "_sup_market": m_sup,
        }
        # In the CLOB, prices have moved and total cost > 1.0 (no_ask=0.55, yes_ask=0.50 -> cost 1.05)
        def mock_fetch(market, cache):
            if market["condition_id"] == "0xsup":
                return market, {"yes_ask": 0.50, "yes_ask_size": 10.0}
            else:
                return market, {"no_ask": 0.55, "no_ask_size": 10.0}

        funnel = MagicMock()
        with patch("scans.frechet._fetch_clob_for_market", side_effect=mock_fetch):
            refined = _frechet()._refine_frechet_with_clob([cand], min_profit=0.01, funnel=funnel)

        assert len(refined) == 0
        funnel.record_clob_dropped.assert_called_once_with(1)
