"""Integration and unit tests for Jev Crypto continuous mode orchestration.

Asserts:
- Jev crypto scan helper and scan function are properly bound in continuous.py.
- Priority queue weighting prioritizes JevCrypto opportunities (weight 2.2).
- Continuous scan runner correctly gates on mode and configuration.
- Real-time WebSocket profit recalculation handles JevCrypto YES/NO token updates.
- Dashboard state properly records and exposes jev_crypto_opps.
"""

from __future__ import annotations

import os
import sys
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# Mock external SDK-dependent modules before importing continuous
_modules_to_mock = ["kalshi_api", "polymarket_api", "display", "recovery"]
_saved_modules = {name: sys.modules[name] for name in _modules_to_mock if name in sys.modules}
for _mod_name in _modules_to_mock:
    sys.modules[_mod_name] = MagicMock()

import config as config_mod
import continuous
from dashboard import _DashboardState

# Restore saved modules for any sibling test files
for _mod_name in _modules_to_mock:
    if _mod_name in _saved_modules:
        sys.modules[_mod_name] = _saved_modules[_mod_name]
    elif _mod_name in sys.modules:
        del sys.modules[_mod_name]


_POLY_CRYPTO_MARKET = {
    "condition_id": "cond-crypto-btc-1",
    "question": "Will Bitcoin reach $100k by end of month?",
    "clobTokenIds": ["token-btc-yes", "token-btc-no"],
}


class TestContinuousImportsJevScan:
    """Ensure Jev crypto scan components are imported and exposed in continuous.py."""

    def test_scan_jev_crypto_imported(self):
        assert hasattr(continuous, "scan_jev_crypto")

    def test_helper_defined(self):
        assert hasattr(continuous, "_scan_jev_crypto_continuous")
        assert callable(continuous._scan_jev_crypto_continuous)


class TestJevPriorityWeight:
    """Verify execution priority scoring assigns high priority to JevCrypto."""

    def test_priority_weight_exists(self):
        assert "JevCrypto" in continuous._PRIORITY_WEIGHTS
        assert continuous._PRIORITY_WEIGHTS["JevCrypto"] == 2.2

    def test_execution_priority_scales_by_jev_weight(self):
        opp = {
            "type": "JevCrypto",
            "net_roi": 0.10,
            "total_cost": 50.0,
            "duration_days": 1.0,
        }
        score = continuous._execution_priority(opp)
        base_eff = continuous.capital_efficiency_score(opp)
        assert pytest.approx(score, rel=1e-4) == 2.2 * base_eff


class TestJevCryptoWiring:
    """Verify continuous mode runner behaves properly under different flag/mode settings."""

    def test_invoked_when_flag_on_in_all_mode(self, monkeypatch):
        monkeypatch.setattr(config_mod, "JEV_CRYPTO_ENABLED", True)
        with patch.object(continuous, "scan_jev_crypto", autospec=True) as m:
            m.return_value = [{"type": "JevCrypto", "market": "BTC 100k"}]
            out = continuous._scan_jev_crypto_continuous(
                [_POLY_CRYPTO_MARKET],
                mode="all",
                min_profit=0.02,
            )
        m.assert_called_once()
        call_kwargs = m.call_args.kwargs
        call_args = m.call_args.args
        assert "polymarket-cond-crypto-btc-1" in call_args[0]
        assert call_kwargs.get("min_profit") == 0.02
        assert out == [{"type": "JevCrypto", "market": "BTC 100k"}]

    def test_invoked_in_explicit_jev_crypto_mode_regardless_of_flag(self, monkeypatch):
        monkeypatch.setattr(config_mod, "JEV_CRYPTO_ENABLED", False)
        with patch.object(continuous, "scan_jev_crypto", autospec=True) as m:
            m.return_value = [{"type": "JevCrypto"}]
            out = continuous._scan_jev_crypto_continuous(
                [_POLY_CRYPTO_MARKET],
                mode="jev-crypto",
                min_profit=0.01,
            )
        m.assert_called_once()
        assert out == [{"type": "JevCrypto"}]

    def test_skipped_when_flag_off_in_all_mode(self, monkeypatch):
        monkeypatch.setattr(config_mod, "JEV_CRYPTO_ENABLED", False)
        with patch.object(continuous, "scan_jev_crypto", autospec=True) as m:
            out = continuous._scan_jev_crypto_continuous(
                [_POLY_CRYPTO_MARKET],
                mode="all",
                min_profit=0.02,
            )
        m.assert_not_called()
        assert out == []

    def test_skipped_when_mode_excludes(self, monkeypatch):
        monkeypatch.setattr(config_mod, "JEV_CRYPTO_ENABLED", True)
        with patch.object(continuous, "scan_jev_crypto", autospec=True) as m:
            out = continuous._scan_jev_crypto_continuous(
                [_POLY_CRYPTO_MARKET],
                mode="binary",
                min_profit=0.02,
            )
        m.assert_not_called()
        assert out == []

    def test_empty_when_no_markets_provided(self, monkeypatch):
        monkeypatch.setattr(config_mod, "JEV_CRYPTO_ENABLED", True)
        with patch.object(continuous, "scan_jev_crypto", autospec=True) as m:
            assert continuous._scan_jev_crypto_continuous([], "all", 0.01) == []
            assert continuous._scan_jev_crypto_continuous(None, "all", 0.01) == []
        m.assert_not_called()


class TestRecalcProfitJevCrypto:
    """Test WebSocket price updates recalculating net profit for tracked JevCrypto opps."""

    def test_recalc_profit_buy_yes(self):
        from fees import net_profit_jev_crypto
        opp = {
            "type": "JevCrypto",
            "_action": "buy_yes",
            "_token_ids": ["tok_yes_123", "tok_no_123"],
            "_model_prob": 0.85,
        }
        new_yes_price = 0.60
        recalc = continuous._recalc_profit(
            opp=opp,
            platform="polymarket",
            ticker="tok_yes_123",
            new_price=new_yes_price,
            price_cache={},
        )
        expected = net_profit_jev_crypto(
            price=new_yes_price,
            model_prob=0.85,
            size=50.0,
        )["net_profit"]
        assert recalc is not None
        assert pytest.approx(recalc, rel=1e-4) == expected

    def test_recalc_profit_buy_no(self):
        from fees import net_profit_jev_crypto
        opp = {
            "type": "JevCrypto",
            "_action": "buy_no",
            "_token_ids": ["tok_yes_456", "tok_no_456"],
            "_model_prob": 0.25,  # YES prob is 0.25 -> NO prob is 0.75
        }
        new_no_price = 0.50
        recalc = continuous._recalc_profit(
            opp=opp,
            platform="polymarket",
            ticker="tok_no_456",
            new_price=new_no_price,
            price_cache={},
        )
        expected = net_profit_jev_crypto(
            price=new_no_price,
            model_prob=0.75,
            size=50.0,
        )["net_profit"]
        assert recalc is not None
        assert pytest.approx(recalc, rel=1e-4) == expected

    def test_recalc_profit_unrelated_ticker_returns_none(self):
        opp = {
            "type": "JevCrypto",
            "_action": "buy_yes",
            "_token_ids": ["tok_yes_123", "tok_no_123"],
            "_model_prob": 0.85,
        }
        recalc = continuous._recalc_profit(
            opp=opp,
            platform="polymarket",
            ticker="some_other_token",
            new_price=0.60,
            price_cache={},
        )
        assert recalc is None

    def test_recalc_profit_missing_metadata_returns_none(self):
        opp = {
            "type": "JevCrypto",
            "_action": "buy_yes",
            # missing _token_ids and _model_prob
        }
        recalc = continuous._recalc_profit(
            opp=opp,
            platform="polymarket",
            ticker="tok_yes_123",
            new_price=0.60,
            price_cache={},
        )
        assert recalc is None


class TestDashboardJevCrypto:
    """Verify dashboard state tracking for Jev Crypto opportunities."""

    def test_dashboard_state_includes_jev_crypto(self):
        state = _DashboardState()
        assert hasattr(state, "jev_crypto_opps")
        assert state.jev_crypto_opps == 0

        state.jev_crypto_opps = 5
        d = state.to_dict()
        assert d.get("jev_crypto_opps") == 5
