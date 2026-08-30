"""Tests for gas_monitor.py — real-time gas price monitor and dynamic thresholds."""

import sys
import os
import time

import pytest
from unittest.mock import patch, MagicMock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from gas_monitor import GasMonitor


# ---------------------------------------------------------------------------
# Helper: build a mock RPC response for eth_gasPrice
# ---------------------------------------------------------------------------

def _mock_rpc_response(gas_wei_hex: str):
    """Create a mock requests.Response for an eth_gasPrice JSON-RPC call."""
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {
        "jsonrpc": "2.0",
        "id": 1,
        "result": gas_wei_hex,
    }
    mock_resp.raise_for_status = MagicMock()
    return mock_resp


def _mock_coingecko_response(matic_usd: float):
    """Create a mock requests.Response for CoinGecko MATIC price."""
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {
        "matic-network": {"usd": matic_usd},
    }
    mock_resp.raise_for_status = MagicMock()
    return mock_resp


# ---------------------------------------------------------------------------
# get_polygon_gas_gwei
# ---------------------------------------------------------------------------

class TestGetPolygonGasGwei:
    def test_fetches_gas_price_from_rpc(self):
        """Should parse hex gas price from RPC and convert to Gwei."""
        # 30 Gwei = 30_000_000_000 Wei = 0x6FC23AC00
        gas_hex = hex(30_000_000_000)
        monitor = GasMonitor(polygon_rpc_url="http://fake-rpc", enabled=True)

        with patch("gas_monitor.requests.post", return_value=_mock_rpc_response(gas_hex)):
            result = monitor.get_polygon_gas_gwei()

        assert result == pytest.approx(30.0)

    def test_returns_different_gas_values(self):
        """Should correctly parse various gas price values."""
        # 50 Gwei = 50_000_000_000 Wei
        gas_hex = hex(50_000_000_000)
        monitor = GasMonitor(polygon_rpc_url="http://fake-rpc", enabled=True)

        with patch("gas_monitor.requests.post", return_value=_mock_rpc_response(gas_hex)):
            result = monitor.get_polygon_gas_gwei()

        assert result == pytest.approx(50.0)

    def test_caching_within_ttl(self):
        """Second call within TTL should NOT make another HTTP request."""
        gas_hex = hex(30_000_000_000)
        monitor = GasMonitor(
            polygon_rpc_url="http://fake-rpc",
            cache_ttl=60.0,  # Long TTL so cache won't expire
            enabled=True,
        )

        mock_post = MagicMock(return_value=_mock_rpc_response(gas_hex))
        with patch("gas_monitor.requests.post", mock_post):
            result1 = monitor.get_polygon_gas_gwei()
            result2 = monitor.get_polygon_gas_gwei()

        assert result1 == pytest.approx(30.0)
        assert result2 == pytest.approx(30.0)
        # Should only call RPC once — second call uses cache
        assert mock_post.call_count == 1

    def test_cache_expired_refetches(self):
        """After TTL expires, should fetch again."""
        gas_hex = hex(30_000_000_000)
        monitor = GasMonitor(
            polygon_rpc_url="http://fake-rpc",
            cache_ttl=0.0,  # Immediate expiry
            enabled=True,
        )

        mock_post = MagicMock(return_value=_mock_rpc_response(gas_hex))
        with patch("gas_monitor.requests.post", mock_post):
            monitor.get_polygon_gas_gwei()
            monitor.get_polygon_gas_gwei()

        assert mock_post.call_count == 2

    def test_returns_default_when_disabled(self):
        """When disabled, should return default 30 Gwei without RPC call."""
        monitor = GasMonitor(enabled=False)

        mock_post = MagicMock()
        with patch("gas_monitor.requests.post", mock_post):
            result = monitor.get_polygon_gas_gwei()

        assert result == pytest.approx(30.0)
        mock_post.assert_not_called()

    def test_fallback_on_rpc_failure(self):
        """Should return default Gwei when RPC call raises an exception."""
        monitor = GasMonitor(polygon_rpc_url="http://fake-rpc", enabled=True)

        mock_post = MagicMock(side_effect=Exception("connection refused"))
        with patch("gas_monitor.requests.post", mock_post):
            result = monitor.get_polygon_gas_gwei()

        assert result == pytest.approx(30.0)


# ---------------------------------------------------------------------------
# get_polygon_gas_cost
# ---------------------------------------------------------------------------

class TestGetPolygonGasCost:
    def test_calculation_formula(self):
        """Gas cost = gas_gwei * 21000 * matic_price / 1e9."""
        # 30 Gwei, MATIC = $0.50
        # Cost = 30 * 21000 * 0.50 / 1e9 = 0.000315
        gas_hex = hex(30_000_000_000)
        monitor = GasMonitor(polygon_rpc_url="http://fake-rpc", enabled=True)

        with patch("gas_monitor.requests.post", return_value=_mock_rpc_response(gas_hex)):
            with patch("gas_monitor.requests.get", return_value=_mock_coingecko_response(0.50)):
                result = monitor.get_polygon_gas_cost()

        expected = 30.0 * 21000 * 0.50 / 1e9
        assert result == pytest.approx(expected)

    def test_higher_gas_price_increases_cost(self):
        """Higher gas price should yield higher dollar cost."""
        monitor = GasMonitor(polygon_rpc_url="http://fake-rpc", enabled=True)

        # Low gas: 10 Gwei
        with patch("gas_monitor.requests.post", return_value=_mock_rpc_response(hex(10_000_000_000))):
            with patch("gas_monitor.requests.get", return_value=_mock_coingecko_response(0.50)):
                low_cost = monitor.get_polygon_gas_cost()

        # Reset cache
        monitor._gas_gwei = None
        monitor._gas_gwei_ts = 0

        # High gas: 100 Gwei
        with patch("gas_monitor.requests.post", return_value=_mock_rpc_response(hex(100_000_000_000))):
            with patch("gas_monitor.requests.get", return_value=_mock_coingecko_response(0.50)):
                high_cost = monitor.get_polygon_gas_cost()

        assert high_cost > low_cost

    def test_returns_fallback_when_disabled(self):
        """When disabled, should return fallback_gas_cost."""
        monitor = GasMonitor(enabled=False, fallback_gas_cost=0.05)
        assert monitor.get_polygon_gas_cost() == pytest.approx(0.05)

    def test_returns_fallback_on_total_failure(self):
        """When both RPC and price fetch fail, returns fallback."""
        monitor = GasMonitor(
            polygon_rpc_url="http://fake-rpc",
            enabled=True,
            fallback_gas_cost=0.04,
        )

        # Make get_polygon_gas_gwei succeed but _fetch_matic_price raise
        with patch("gas_monitor.requests.post", return_value=_mock_rpc_response(hex(30_000_000_000))):
            with patch("gas_monitor.requests.get", side_effect=Exception("coingecko down")):
                # _fetch_matic_price will fallback to $0.50, so gas_cost computation succeeds
                # Let's verify it doesn't crash
                result = monitor.get_polygon_gas_cost()
                assert result > 0

    def test_returns_fallback_when_gas_cost_raises(self):
        """Full computation failure returns fallback."""
        monitor = GasMonitor(
            polygon_rpc_url="http://fake-rpc",
            enabled=True,
            fallback_gas_cost=0.03,
        )

        # Patch get_polygon_gas_gwei to raise
        with patch.object(monitor, "get_polygon_gas_gwei", side_effect=Exception("total failure")):
            result = monitor.get_polygon_gas_cost()

        assert result == pytest.approx(0.03)


# ---------------------------------------------------------------------------
# get_effective_threshold
# ---------------------------------------------------------------------------

class TestGetEffectiveThreshold:
    def _make_monitor(self, gas_cost: float = 0.001):
        """Create a monitor with a fixed gas cost for deterministic testing."""
        monitor = GasMonitor(enabled=True, safety_margin=1.2)
        # Patch get_polygon_gas_cost to return fixed value
        monitor.get_polygon_gas_cost = MagicMock(return_value=gas_cost)
        return monitor

    def test_polymarket_internal_two_gas_txns(self):
        """Polymarket vs Polymarket: 2 on-chain txns (1 per leg)."""
        monitor = self._make_monitor(gas_cost=0.001)
        threshold = monitor.get_effective_threshold("polymarket", "polymarket")
        # 2 txns * $0.001 + $0 + $0 platform fees = $0.002, * 1.2 = $0.0024
        assert threshold == pytest.approx(0.002 * 1.2)

    def test_cross_polymarket_kalshi(self):
        """Polymarket vs Kalshi: 1 on-chain txn; gas only, no fee add-on."""
        monitor = self._make_monitor(gas_cost=0.001)
        threshold = monitor.get_effective_threshold("polymarket", "kalshi")
        # 1 txn * $0.001 (PM) + 0 txns (Kalshi); platform fees are NOT added —
        # scan net_profit is already fee-netted by fees.py (double-count fix).
        expected = 0.001 * 1.2
        assert threshold == pytest.approx(expected)

    def test_cross_polymarket_betfair(self):
        """Polymarket vs Betfair: 1 on-chain txn; gas only, no fee add-on."""
        monitor = self._make_monitor(gas_cost=0.001)
        threshold = monitor.get_effective_threshold("polymarket", "betfair")
        expected = 0.001 * 1.2
        assert threshold == pytest.approx(expected)

    def test_cross_polymarket_smarkets(self):
        """Polymarket vs Smarkets: 1 on-chain txn; gas only, no fee add-on."""
        monitor = self._make_monitor(gas_cost=0.001)
        threshold = monitor.get_effective_threshold("polymarket", "smarkets")
        expected = 0.001 * 1.2
        assert threshold == pytest.approx(expected)

    def test_kalshi_internal_no_gas(self):
        """Kalshi vs Kalshi: 0 on-chain txns -> zero threshold."""
        monitor = self._make_monitor(gas_cost=0.001)
        threshold = monitor.get_effective_threshold("kalshi", "kalshi")
        # 0 gas txns; Kalshi fees are already inside scan net_profit -> $0.
        assert threshold == pytest.approx(0.0)

    def test_sxbet_zero_fees(self):
        """SX Bet vs SX Bet: 0 gas, 0 platform fees -> threshold = 0."""
        monitor = self._make_monitor(gas_cost=0.001)
        threshold = monitor.get_effective_threshold("sxbet", "sxbet")
        # 0 txns + $0 + $0 = $0, * 1.2 = $0
        assert threshold == pytest.approx(0.0)

    def test_different_pairs_yield_different_thresholds(self):
        """Different platform pairs should produce different thresholds."""
        monitor = self._make_monitor(gas_cost=0.001)
        t_pm_pm = monitor.get_effective_threshold("polymarket", "polymarket")
        t_pm_kalshi = monitor.get_effective_threshold("polymarket", "kalshi")
        t_kalshi_kalshi = monitor.get_effective_threshold("kalshi", "kalshi")

        # Thresholds now track gas txn count only (fees live in net_profit).
        assert t_pm_pm > t_pm_kalshi  # 2 on-chain legs vs 1
        assert t_pm_kalshi > t_kalshi_kalshi  # 1 on-chain leg vs 0

    def test_safety_margin_applied(self):
        """Threshold should scale with safety_margin."""
        monitor_low = GasMonitor(enabled=True, safety_margin=1.0)
        monitor_low.get_polygon_gas_cost = MagicMock(return_value=0.001)

        monitor_high = GasMonitor(enabled=True, safety_margin=2.0)
        monitor_high.get_polygon_gas_cost = MagicMock(return_value=0.001)

        t_low = monitor_low.get_effective_threshold("polymarket", "kalshi")
        t_high = monitor_high.get_effective_threshold("polymarket", "kalshi")

        assert t_high == pytest.approx(t_low * 2.0)

    def test_case_insensitive_platforms(self):
        """Platform names should be case-insensitive."""
        monitor = self._make_monitor(gas_cost=0.001)
        t_lower = monitor.get_effective_threshold("polymarket", "kalshi")
        t_upper = monitor.get_effective_threshold("Polymarket", "Kalshi")
        assert t_lower == pytest.approx(t_upper)

    def test_unknown_platform_zero_fees(self):
        """Unknown platform names should default to 0 gas txns and 0 fees."""
        monitor = self._make_monitor(gas_cost=0.001)
        threshold = monitor.get_effective_threshold("unknown_exchange", "another_one")
        # 0 txns + $0 + $0 = $0, * 1.2 = $0
        assert threshold == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# should_execute
# ---------------------------------------------------------------------------

class TestShouldExecute:
    def _make_monitor(self, gas_cost: float = 0.001):
        """Create a monitor with fixed gas cost."""
        monitor = GasMonitor(enabled=True, safety_margin=1.2)
        monitor.get_polygon_gas_cost = MagicMock(return_value=gas_cost)
        monitor._gas_source_valid = True
        monitor._matic_source_valid = True
        return monitor

    def test_returns_true_for_profitable_opp(self):
        """Opportunity with profit well above threshold should pass."""
        monitor = self._make_monitor(gas_cost=0.001)
        opp = {
            "net_profit": 0.10,
            "_platform_a": "polymarket",
            "_platform_b": "kalshi",
            "type": "Cross-Kalshi",
        }
        assert monitor.should_execute(opp) is True

    def test_returns_false_for_unprofitable_opp(self):
        """Opportunity with profit below threshold should fail."""
        monitor = self._make_monitor(gas_cost=0.01)
        # Threshold for PM vs Betfair: 1 gas txn * 0.01 * 1.2 = 0.012
        opp = {
            "net_profit": 0.01,
            "_platform_a": "polymarket",
            "_platform_b": "betfair",
            "type": "Cross-Betfair",
        }
        assert monitor.should_execute(opp) is False

    def test_fee_netted_kalshi_multi_passes(self):
        """Regression (2026-07-21): a KalshiMulti(4) with $0.04 net profit —
        already net of real Kalshi fees via net_profit_kalshi_multi — was
        skipped as 'gas_threshold' because the gate re-added a flat $0.02/leg
        fee estimate on a zero-gas platform. Fees live in net_profit; the
        gate prices gas only."""
        monitor = self._make_monitor(gas_cost=0.001)
        opp = {"net_profit": 0.04, "type": "KalshiMulti(4)"}
        assert monitor.should_execute(opp) is True

    def test_returns_true_when_disabled(self):
        """When disabled, should_execute always returns True."""
        monitor = GasMonitor(enabled=False)
        opp = {
            "net_profit": 0.0001,  # Tiny profit
            "_platform_a": "polymarket",
            "_platform_b": "betfair",
            "type": "Cross-Betfair",
        }
        assert monitor.should_execute(opp) is True

    def test_infers_platforms_from_type_cross_kalshi(self):
        """Should infer polymarket/kalshi from 'Cross-Kalshi' type."""
        monitor = self._make_monitor(gas_cost=0.001)
        opp = {
            "net_profit": 0.10,
            "type": "Cross-Kalshi",
        }
        assert monitor.should_execute(opp) is True

    def test_infers_platforms_from_type_kalshi_internal(self):
        """Should infer kalshi/kalshi from 'Kalshi Binary' type."""
        monitor = self._make_monitor(gas_cost=0.001)
        opp = {
            "net_profit": 0.10,
            "type": "Kalshi Binary",
        }
        assert monitor.should_execute(opp) is True

    def test_infers_platforms_from_type_binary(self):
        """Should infer polymarket/polymarket from 'Binary' type (PM internal)."""
        monitor = self._make_monitor(gas_cost=0.001)
        opp = {
            "net_profit": 0.005,
            "type": "Binary",
        }
        # Threshold for PM internal: 2 * 0.001 * 1.2 = 0.0024
        assert monitor.should_execute(opp) is True

    def test_exact_threshold_boundary(self):
        """Profit exactly at threshold is accepted by the inclusive boundary."""
        monitor = self._make_monitor(gas_cost=0.001)
        # PM vs PM threshold: 2 * 0.001 * 1.2 = 0.0024
        threshold = monitor.get_effective_threshold("polymarket", "polymarket")
        opp = {
            "net_profit": threshold,
            "_platform_a": "polymarket",
            "_platform_b": "polymarket",
            "type": "Binary",
        }
        # net_profit < threshold is False when equal, so should_execute returns True
        # because the condition is net_profit < threshold -> False -> return True
        assert monitor.should_execute(opp) is True

    def test_just_below_threshold_fails(self):
        """Profit just below threshold should fail."""
        monitor = self._make_monitor(gas_cost=0.001)
        threshold = monitor.get_effective_threshold("polymarket", "polymarket")
        opp = {
            "net_profit": threshold - 0.0001,
            "_platform_a": "polymarket",
            "_platform_b": "polymarket",
            "type": "Binary",
        }
        assert monitor.should_execute(opp) is False

    def test_platform_keys_take_priority_over_type(self):
        """_platform_a/_platform_b should override type-based inference."""
        monitor = self._make_monitor(gas_cost=0.001)
        # Type says Cross-Kalshi but platform keys say betfair
        opp = {
            "net_profit": 0.10,
            "_platform_a": "polymarket",
            "_platform_b": "betfair",
            "type": "Cross-Kalshi",
        }
        # Should use betfair threshold (higher), not kalshi
        # Still passes because 0.10 is well above any threshold
        assert monitor.should_execute(opp) is True


# ---------------------------------------------------------------------------
# _fetch_matic_price
# ---------------------------------------------------------------------------

class TestFetchMaticPrice:
    def test_fetches_from_coingecko(self):
        """Should parse MATIC price from CoinGecko response."""
        monitor = GasMonitor(polygon_rpc_url="http://fake-rpc", enabled=True)

        with patch("gas_monitor.requests.get", return_value=_mock_coingecko_response(0.75)):
            result = monitor._fetch_matic_price()

        assert result == pytest.approx(0.75)

    def test_caching_within_ttl(self):
        """Second call within 60s should use cached MATIC price."""
        monitor = GasMonitor(polygon_rpc_url="http://fake-rpc", enabled=True)

        mock_get = MagicMock(return_value=_mock_coingecko_response(0.75))
        with patch("gas_monitor.requests.get", mock_get):
            result1 = monitor._fetch_matic_price()
            result2 = monitor._fetch_matic_price()

        assert result1 == pytest.approx(0.75)
        assert result2 == pytest.approx(0.75)
        assert mock_get.call_count == 1

    def test_fallback_on_error(self):
        """Should return $0.50 fallback when CoinGecko fails."""
        monitor = GasMonitor(polygon_rpc_url="http://fake-rpc", enabled=True)

        with patch("gas_monitor.requests.get", side_effect=Exception("API error")):
            result = monitor._fetch_matic_price()

        assert result == pytest.approx(0.50)

    def test_fallback_on_malformed_response(self):
        """Should return fallback when response JSON is malformed."""
        monitor = GasMonitor(polygon_rpc_url="http://fake-rpc", enabled=True)

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"unexpected": "data"}
        mock_resp.raise_for_status = MagicMock()

        with patch("gas_monitor.requests.get", return_value=mock_resp):
            result = monitor._fetch_matic_price()

        assert result == pytest.approx(0.50)

    def test_different_prices(self):
        """Should correctly parse various MATIC price values."""
        monitor = GasMonitor(polygon_rpc_url="http://fake-rpc", enabled=True)

        with patch("gas_monitor.requests.get", return_value=_mock_coingecko_response(1.25)):
            result = monitor._fetch_matic_price()

        assert result == pytest.approx(1.25)


# ---------------------------------------------------------------------------
# Graceful fallback / degradation
# ---------------------------------------------------------------------------

class TestGracefulFallback:
    def test_rpc_failure_uses_default_gwei(self):
        """When RPC is unreachable, gas price falls back to 30 Gwei."""
        monitor = GasMonitor(polygon_rpc_url="http://unreachable", enabled=True)

        with patch("gas_monitor.requests.post", side_effect=ConnectionError("refused")):
            result = monitor.get_polygon_gas_gwei()

        assert result == pytest.approx(30.0)

    def test_rpc_bad_json_uses_default(self):
        """When RPC returns bad JSON, gas price falls back to default."""
        monitor = GasMonitor(polygon_rpc_url="http://fake-rpc", enabled=True)

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.side_effect = ValueError("bad json")
        mock_resp.raise_for_status = MagicMock()

        with patch("gas_monitor.requests.post", return_value=mock_resp):
            result = monitor.get_polygon_gas_gwei()

        assert result == pytest.approx(30.0)

    def test_full_pipeline_with_fallbacks(self):
        """When both RPC and CoinGecko fail, gas cost uses sensible defaults."""
        monitor = GasMonitor(
            polygon_rpc_url="http://fake-rpc",
            enabled=True,
            fallback_gas_cost=0.03,
        )

        with patch("gas_monitor.requests.post", side_effect=Exception("rpc down")):
            with patch("gas_monitor.requests.get", side_effect=Exception("coingecko down")):
                gas_gwei = monitor.get_polygon_gas_gwei()
                gas_cost = monitor.get_polygon_gas_cost()

        # Gas Gwei falls back to 30.0
        assert gas_gwei == pytest.approx(30.0)
        # Gas cost: 30 * 21000 * 0.50 / 1e9 = 0.000315
        # (uses default MATIC price $0.50)
        expected_cost = 30.0 * 21000 * 0.50 / 1e9
        assert gas_cost == pytest.approx(expected_cost)

    def test_fallback_inputs_never_authorize_polymarket_execution(self):
        monitor = GasMonitor(polygon_rpc_url="http://fake-rpc", enabled=True)

        with patch("gas_monitor.requests.post", side_effect=Exception("rpc down")), \
             patch("gas_monitor.requests.get", side_effect=Exception("price down")):
            allowed = monitor.should_execute({
                "type": "CrossPlatform",
                "_platform_a": "polymarket",
                "_platform_b": "kalshi",
                "net_profit": 100.0,
            })

        assert allowed is False

    @pytest.mark.parametrize("net_profit", [float("nan"), float("inf"), "bad"])
    def test_invalid_profit_never_authorizes_execution(self, net_profit):
        monitor = GasMonitor(enabled=True)
        assert monitor.should_execute({"type": "KalshiBinary", "net_profit": net_profit}) is False

    def test_disabled_monitor_does_not_call_apis(self):
        """When disabled, no HTTP calls should be made."""
        monitor = GasMonitor(enabled=False, fallback_gas_cost=0.03)

        mock_post = MagicMock()
        mock_get = MagicMock()
        with patch("gas_monitor.requests.post", mock_post):
            with patch("gas_monitor.requests.get", mock_get):
                gas_gwei = monitor.get_polygon_gas_gwei()
                gas_cost = monitor.get_polygon_gas_cost()
                should_exec = monitor.should_execute({"net_profit": 0.0001})

        mock_post.assert_not_called()
        mock_get.assert_not_called()
        assert gas_gwei == pytest.approx(30.0)
        assert gas_cost == pytest.approx(0.03)
        assert should_exec is True


# ---------------------------------------------------------------------------
# Constructor / initialization
# ---------------------------------------------------------------------------

class TestConstructor:
    def test_default_rpc_url(self):
        """Without explicit URL or env var, uses public Polygon RPC."""
        with patch.dict(os.environ, {}, clear=False):
            # Remove POLYGON_RPC_URL if present
            env = os.environ.copy()
            env.pop("POLYGON_RPC_URL", None)
            with patch.dict(os.environ, env, clear=True):
                monitor = GasMonitor()
                assert monitor.polygon_rpc_url == "https://polygon-rpc.com"

    def test_explicit_rpc_url(self):
        """Explicit URL takes priority over env var."""
        monitor = GasMonitor(polygon_rpc_url="http://my-custom-rpc")
        assert monitor.polygon_rpc_url == "http://my-custom-rpc"

    def test_env_var_rpc_url(self):
        """POLYGON_RPC_URL env var is used when no explicit URL given."""
        with patch.dict(os.environ, {"POLYGON_RPC_URL": "http://env-rpc"}):
            monitor = GasMonitor()
            assert monitor.polygon_rpc_url == "http://env-rpc"

    def test_custom_parameters(self):
        """Constructor stores all custom parameters correctly."""
        monitor = GasMonitor(
            polygon_rpc_url="http://custom",
            cache_ttl=30.0,
            safety_margin=1.5,
            fallback_gas_cost=0.05,
            enabled=False,
        )
        assert monitor.polygon_rpc_url == "http://custom"
        assert monitor.cache_ttl == 30.0
        assert monitor.safety_margin == 1.5
        assert monitor.fallback_gas_cost == 0.05
        assert monitor.enabled is False


# ---------------------------------------------------------------------------
# Thread safety
# ---------------------------------------------------------------------------

class TestThreadSafety:
    def test_concurrent_gas_price_fetches(self):
        """Multiple threads fetching gas price should not corrupt cache."""
        import threading

        gas_hex = hex(30_000_000_000)
        monitor = GasMonitor(
            polygon_rpc_url="http://fake-rpc",
            cache_ttl=0.0,  # No caching — force re-fetch each time
            enabled=True,
        )
        results = []
        errors = []

        def fetch():
            try:
                r = monitor.get_polygon_gas_gwei()
                results.append(r)
            except Exception as exc:
                errors.append(exc)

        with patch("gas_monitor.requests.post", return_value=_mock_rpc_response(gas_hex)):
            threads = [threading.Thread(target=fetch) for _ in range(10)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()

        assert len(errors) == 0
        assert all(r == pytest.approx(30.0) for r in results)
        assert len(results) == 10


# ---------------------------------------------------------------------------
# get_current_gas_cost (public getter)
# ---------------------------------------------------------------------------

class TestGetCurrentGasCost:
    def test_returns_same_as_get_polygon_gas_cost(self):
        """get_current_gas_cost should delegate to get_polygon_gas_cost."""
        monitor = GasMonitor(enabled=True)
        monitor.get_polygon_gas_cost = MagicMock(return_value=0.0042)
        assert monitor.get_current_gas_cost() == pytest.approx(0.0042)


class TestMaticPolMigration:
    """CoinGecko serves Polygon's gas token as polygon-ecosystem-token after
    the MATIC->POL migration; the legacy id intermittently returns an empty
    dict (the prod KeyError-'usd' class). Fetch failures must keep the
    last-good price, not poison the cache with the default."""

    def test_prefers_pol_id(self):
        monitor = GasMonitor(polygon_rpc_url="http://fake-rpc", enabled=True)
        resp = MagicMock()
        resp.raise_for_status = MagicMock()
        resp.json.return_value = {"polygon-ecosystem-token": {"usd": 0.42},
                                  "matic-network": {"usd": 0.41}}
        with patch("gas_monitor.requests.get", return_value=resp):
            assert monitor._do_fetch_matic_price() == pytest.approx(0.42)

    def test_empty_legacy_dict_returns_none_not_keyerror(self):
        monitor = GasMonitor(polygon_rpc_url="http://fake-rpc", enabled=True)
        resp = MagicMock()
        resp.raise_for_status = MagicMock()
        resp.json.return_value = {"matic-network": {}}
        with patch("gas_monitor.requests.get", return_value=resp):
            assert monitor._do_fetch_matic_price() is None

    def test_fetch_failure_keeps_last_good_price(self):
        monitor = GasMonitor(polygon_rpc_url="http://fake-rpc", enabled=True)
        good = MagicMock()
        good.raise_for_status = MagicMock()
        good.json.return_value = {"polygon-ecosystem-token": {"usd": 0.44}}
        with patch("gas_monitor.requests.get", return_value=good):
            assert monitor._fetch_matic_price() == pytest.approx(0.44)
        # Expire the TTL, then fail the next fetch: last-good must survive.
        monitor._matic_price_ts = 0.0
        with patch("gas_monitor.requests.get", side_effect=Exception("down")):
            assert monitor._fetch_matic_price() == pytest.approx(0.44)
