"""Tests for the NegRisk NO-side arbitrage scan + execution path (Plan 01)."""

import sys
import os
from unittest.mock import patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import config
from scans import negrisk


def _market(yes_mid, no_mid, yes_token, no_token):
    """Build a fake Polymarket NegRisk outcome-market dict."""
    return {
        "id": f"m-{no_token}",
        "question": f"Outcome {no_token}",
        "groupItemTitle": f"O{no_token}",
        "outcomePrices": [str(yes_mid), str(no_mid)],  # index 0 = YES, 1 = NO
        "clobTokenIds": [yes_token, no_token],
        "endDateIso": "2099-01-01T00:00:00Z",
        "volume": "1000",
    }


class TestScanNegriskNoSide:
    def setup_method(self):
        # Resolve the LIVE config module: other test files replace sys.modules["config"],
        # so a module-level `import config` reference can go stale and the scan (which reads
        # the flag off sys.modules["config"] at call time) would see a different object.
        import config as cfg
        self._cfg = cfg
        self._orig = cfg.NEGRISK_NO_SIDE_ENABLED
        cfg.NEGRISK_NO_SIDE_ENABLED = True

    def teardown_method(self):
        self._cfg.NEGRISK_NO_SIDE_ENABLED = self._orig

    def _run(self, event, no_by_token, min_profit=0.01):
        def fake_clob(market, price_cache=None):
            no_tok = market["clobTokenIds"][1]
            return market, {
                "no_ask": no_by_token[no_tok], "no_ask_size": 100,
                "yes_ask": None, "yes_ask_size": 0,
            }

        def fake_parse(market):
            # Patched explicitly: other test files stub sys.modules["polymarket_api"]
            # with a MagicMock, which can leave scans.negrisk.parse_outcome_prices a mock.
            raw = market.get("outcomePrices")
            return [float(p) for p in raw] if raw else None

        with patch.object(negrisk, "get_negrisk_events", return_value=[event]), \
             patch.object(negrisk, "parse_outcome_prices", side_effect=fake_parse), \
             patch.object(negrisk, "_within_resolution_window", return_value=True), \
             patch.object(negrisk, "_fetch_clob_for_market", side_effect=fake_clob):
            return negrisk.scan_negrisk_no_side([event], min_profit=min_profit)

    def test_detects_no_side_arb(self):
        # 3 outcomes, NO 0.40/0.55/0.70 -> sum 1.65 < (N-1)=2, gross 0.35
        markets = [
            _market(0.60, 0.40, "y1", "n1"),
            _market(0.45, 0.55, "y2", "n2"),
            _market(0.30, 0.70, "y3", "n3"),
        ]
        event = {"id": "evt1", "title": "Who wins?", "markets": markets}
        opps = self._run(event, {"n1": 0.40, "n2": 0.55, "n3": 0.70})

        assert len(opps) == 1
        opp = opps[0]
        assert opp["type"] == "NegRiskNO(3)"
        assert opp["_layer"] == 1
        assert opp["_token_ids"] == ["n1", "n2", "n3"]   # NO tokens
        assert len(opp["_no_prices"]) == 3
        assert opp["net_profit"] > 0

    def test_disabled_returns_empty(self):
        self._cfg.NEGRISK_NO_SIDE_ENABLED = False
        opps = negrisk.scan_negrisk_no_side(
            [{"id": "e", "title": "x", "markets": []}], min_profit=0.01)
        assert opps == []

    def test_no_arb_when_sum_above_floor(self):
        # NO 0.80/0.80/0.80 -> sum 2.40 > (N-1)=2: no edge
        markets = [
            _market(0.20, 0.80, "y1", "n1"),
            _market(0.20, 0.80, "y2", "n2"),
            _market(0.20, 0.80, "y3", "n3"),
        ]
        event = {"id": "evt2", "title": "No edge", "markets": markets}
        opps = self._run(event, {"n1": 0.80, "n2": 0.80, "n3": 0.80})
        assert opps == []

    @pytest.mark.parametrize("bad_ask", [-0.01, 0.0, 1.0, 1.01])
    def test_invalid_executable_ask_fails_closed(self, bad_ask):
        markets = [
            _market(0.60, 0.40, "y1", "n1"),
            _market(0.45, 0.55, "y2", "n2"),
            _market(0.30, 0.70, "y3", "n3"),
        ]
        event = {"id": "evt-invalid", "title": "Invalid book", "markets": markets}

        assert self._run(event, {"n1": bad_ask, "n2": 0.55, "n3": 0.70}) == []


class TestBuildLegsNegriskNo:
    def test_build_legs_uses_no_tokens(self):
        from executor import ArbitrageExecutor
        # The NegRiskNO _build_legs branch only reads the opp dict, not self state.
        exec_obj = object.__new__(ArbitrageExecutor)
        opp = {
            "type": "NegRiskNO(3)",
            "_token_ids": ["n1", "n2", "n3"],
            "_no_prices": [0.40, 0.55, 0.70],
        }
        legs = exec_obj._build_legs(opp, size=10.0)
        assert len(legs) == 3
        assert [leg["_token_id"] for leg in legs] == ["n1", "n2", "n3"]
        assert all(leg["platform"] == "polymarket" and leg["side"] == "BUY" for leg in legs)
        assert [leg["price"] for leg in legs] == [0.40, 0.55, 0.70]

    def test_build_legs_rejects_token_price_mismatch(self):
        from executor import ArbitrageExecutor
        exec_obj = object.__new__(ArbitrageExecutor)
        opp = {
            "type": "NegRiskNO(3)",
            "_token_ids": ["n1", "n2", "n3"],
            "_no_prices": [0.40, 0.55],   # length mismatch
        }
        assert exec_obj._build_legs(opp, size=10.0) == []
