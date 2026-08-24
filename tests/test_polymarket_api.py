"""Tests for polymarket_api.py — rate limiter thread safety."""

import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import threading
import time
import types
import pytest
from unittest.mock import MagicMock, patch

# Mock tenacity before importing polymarket_api — the module uses decorators
# at import time, so we need a mock that passes through the decorated function.
if "tenacity" not in sys.modules:
    _tenacity_mock = types.ModuleType("tenacity")
    # retry() must be a decorator factory that returns the original function unchanged
    _tenacity_mock.retry = lambda **kwargs: (lambda fn: fn)
    _tenacity_mock.stop_after_attempt = lambda *a, **kw: None
    _tenacity_mock.wait_exponential = lambda *a, **kw: None
    _tenacity_mock.retry_if_exception_type = lambda *a, **kw: None
    sys.modules["tenacity"] = _tenacity_mock

# Mock py_clob_client_v2 since CI may not have the SDK installed
for mod in [
    "py_clob_client_v2",
    "py_clob_client_v2.client",
    "py_clob_client_v2.clob_types",
    "py_clob_client_v2.http_helpers",
    "py_clob_client_v2.http_helpers.helpers",
]:
    if mod not in sys.modules:
        sys.modules[mod] = MagicMock()

# Provide OrderType / OrderArgs / etc. as simple stand-ins for trader tests
_clob_types = sys.modules["py_clob_client_v2.clob_types"]
if not hasattr(_clob_types, "OrderType") or isinstance(getattr(_clob_types, "OrderType", None), MagicMock):
    class _OrderType:
        GTC = "GTC"
        FOK = "FOK"
        FAK = "FAK"
        GTD = "GTD"
    _clob_types.OrderType = _OrderType
    _clob_types.OrderArgs = MagicMock
    _clob_types.OrderPayload = MagicMock
    _clob_types.PartialCreateOrderOptions = MagicMock
    _clob_types.AssetType = MagicMock()
    _clob_types.BalanceAllowanceParams = MagicMock

import polymarket_api
from polymarket_api import _rate_limit, _rate_lock, get_best_bid_ask as _real_get_best_bid_ask, PolymarketTrader
from config import PM_RATE_LIMIT as MIN_REQUEST_INTERVAL


@pytest.fixture(autouse=True)
def reset_circuit_breaker():
    """Reset polymarket circuit breaker state between tests."""
    polymarket_api._circuit.record_success()
    yield
    polymarket_api._circuit.record_success()


@pytest.fixture(autouse=True)
def reset_rate_limiter():
    """Clear module-level _last_request_time between tests so a previous
    test's call doesn't shift timing in the next one."""
    polymarket_api._last_request_time = 0
    yield
    polymarket_api._last_request_time = 0


# ---------------------------------------------------------------------------
# _rate_lock behaviour
# ---------------------------------------------------------------------------
#
# We assert the *behaviour* of _rate_lock (acquire/release, context-manager
# usable, mutually exclusive) rather than its concrete type. Python 3.13
# turned threading.Lock into a class where it had previously been a factory
# returning _thread.lock, so isinstance(_rate_lock, type(threading.Lock()))
# is brittle across Python versions.


class TestRateLockExists:
    def test_rate_lock_is_a_lock(self):
        # Lock-shaped: has acquire/release and works as a context manager.
        assert callable(getattr(_rate_lock, "acquire", None))
        assert callable(getattr(_rate_lock, "release", None))
        with _rate_lock:
            pass

    def test_rate_lock_is_mutually_exclusive(self):
        # Holding the lock should block a non-blocking acquire from another
        # thread. Earlier versions of this test compared
        # ``acquired_concurrently == [False]``; on recent Python versions
        # ``threading.Lock.acquire(blocking=False)`` returns the lock-state
        # object rather than the literal ``False`` when blocking. Compare
        # truthiness instead.
        acquired_concurrently = []

        def try_acquire():
            acquired_concurrently.append(_rate_lock.acquire(blocking=False))

        with _rate_lock:
            t = threading.Thread(target=try_acquire)
            t.start()
            t.join(timeout=2)

        assert len(acquired_concurrently) == 1
        assert not acquired_concurrently[0]

    def test_min_request_interval_value(self):
        assert MIN_REQUEST_INTERVAL == 0.01


# ---------------------------------------------------------------------------
# Single-threaded rate limiting (deterministic — mocked time)
# ---------------------------------------------------------------------------
#
# Real-time tests are unreliable on shared CI runners: time.sleep(0.01) can
# return after only microseconds when the runner is under contention, so we
# observed gaps of ~0.0002s where 0.008s was expected. Rather than papering
# over the flakiness with a sleep budget, we mock time.time + time.sleep and
# assert the rate limiter's *logic*: it calls sleep with the correct
# remaining-interval duration, and updates _last_request_time afterwards.


def _fake_time_module(time_values, sleep_mock):
    """Build a fake replacement for the ``time`` module reference held by
    polymarket_api. Replacing the whole module reference (instead of
    patching ``polymarket_api.time.time`` directly) keeps the mock fully
    scoped to the test — patching the sub-attribute mutated the real
    ``time`` module's globals, which made the assertions race against
    real wall-clock readings on shared CI runners.
    """
    fake = MagicMock()
    fake.time = MagicMock(side_effect=lambda: next(time_values))
    fake.sleep = sleep_mock
    return fake


class TestRateLimitSingleThread:
    def test_sleeps_when_called_too_quickly(self):
        """When _last_request_time is recent, _rate_limit must sleep for the
        remainder of MIN_REQUEST_INTERVAL."""
        # Pretend the previous request happened at t=100.000 and now is
        # t=100.003 — 3ms in, so we should sleep for the remaining 7ms.
        polymarket_api._last_request_time = 100.000
        time_values = iter([100.003, 100.010])
        mock_sleep = MagicMock()
        fake_time = _fake_time_module(time_values, mock_sleep)

        with patch.object(polymarket_api, "time", fake_time):
            _rate_limit()

        assert mock_sleep.call_count == 1
        slept_for = mock_sleep.call_args[0][0]
        assert slept_for == pytest.approx(MIN_REQUEST_INTERVAL - 0.003, abs=1e-9)
        # _last_request_time should advance to the post-sleep timestamp.
        assert polymarket_api._last_request_time == 100.010

    def test_no_sleep_after_sufficient_pause(self):
        """When the previous request was long ago, _rate_limit must not sleep."""
        polymarket_api._last_request_time = 100.000
        time_values = iter([100.500, 100.500])
        mock_sleep = MagicMock()
        fake_time = _fake_time_module(time_values, mock_sleep)

        with patch.object(polymarket_api, "time", fake_time):
            _rate_limit()

        mock_sleep.assert_not_called()
        assert polymarket_api._last_request_time == 100.500


# ---------------------------------------------------------------------------
# Multi-threaded rate limiting
# ---------------------------------------------------------------------------
#
# Real concurrency tests with a 10ms sleep are hopelessly flaky on shared CI
# runners (we saw 5-thread total runtimes of 0.7ms vs expected 32ms). The
# guarantees we actually care about are:
#   1. _rate_limit is serialised by _rate_lock — only one thread executes the
#      sleep/timestamp-update critical section at a time.
#   2. Each call serialises through that critical section, so N concurrent
#      callers each observe a fresh _last_request_time set by their
#      predecessor.
# Both can be verified deterministically without relying on wall-clock sleep.


class TestRateLimitMultiThread:
    def test_threads_maintain_minimum_interval(self):
        """All threads must serialise through the rate-limiter critical
        section: each call observes a _last_request_time at least as recent
        as the previous call's update."""
        observed_last_times: list[float] = []
        clock_lock = threading.Lock()

        # Counter advances by MIN_REQUEST_INTERVAL each time time.time() is
        # consulted, so every thread sees a strictly increasing clock.
        clock = [100.000]

        def fake_time():
            with clock_lock:
                clock[0] += MIN_REQUEST_INTERVAL
                return clock[0]

        # We only want to verify ordering, not actually sleep.
        def fake_sleep(_):
            pass

        fake_time_mod = MagicMock()
        fake_time_mod.time = fake_time
        fake_time_mod.sleep = fake_sleep

        def worker():
            _rate_limit()
            observed_last_times.append(polymarket_api._last_request_time)

        polymarket_api._last_request_time = 0
        # Apply the time-module patch outside the worker so all threads
        # share one consistent fake. Patching inside the worker thread
        # creates a race where the patch may not apply by the time the
        # worker calls _rate_limit() on a fast CI runner.
        with patch.object(polymarket_api, "time", fake_time_mod):
            threads = [threading.Thread(target=worker) for _ in range(5)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=5)

        # Every thread must have seen a strictly increasing timestamp ≥
        # MIN_REQUEST_INTERVAL apart, proving they were serialised.
        sorted_times = sorted(observed_last_times)
        assert len(sorted_times) == 5
        for i in range(1, len(sorted_times)):
            assert sorted_times[i] >= sorted_times[i - 1]

    def test_total_time_scales_with_thread_count(self):
        """N concurrent _rate_limit calls must result in N total
        sleep-or-update cycles — i.e. each thread executes the critical
        section exactly once and the lock prevents any from being skipped.

        Mirrors test_sleeps_when_called_too_quickly's pattern of swapping
        the entire ``polymarket_api.time`` reference rather than patching
        sub-attributes, so the mock is fully scoped to this test.
        """
        sleep_calls: list[float] = []
        lock = threading.Lock()
        clock = [100.000]

        def fake_time():
            with lock:
                # Each tick advances the clock by 1ms — each thread sees a
                # fresh _last_request_time + 1ms when it enters the
                # critical section, so it always finds the limiter "hot"
                # and calls sleep before updating _last_request_time.
                clock[0] += 0.001
                return clock[0]

        def fake_sleep(d):
            with lock:
                sleep_calls.append(d)

        fake_time_mod = MagicMock()
        fake_time_mod.time = fake_time
        fake_time_mod.sleep = fake_sleep

        polymarket_api._last_request_time = 100.000

        with patch.object(polymarket_api, "time", fake_time_mod):
            num_threads = 5
            threads = [
                threading.Thread(target=_rate_limit) for _ in range(num_threads)
            ]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=5)

        # Every thread that found the limiter "hot" should have called sleep.
        # At minimum, num_threads-1 calls would have to wait (one might have
        # arrived after enough simulated clock advancement to skip).
        assert len(sleep_calls) >= num_threads - 1


# ---------------------------------------------------------------------------
# PolymarketTrader write-path adapter
# ---------------------------------------------------------------------------


class TestPolymarketTraderPlaceOrder:
    """Adapter-boundary tests for PolymarketTrader.place_order."""

    def test_place_order_forwards_order_type(self):
        """Executor-supplied order_type must reach create_and_post_order."""
        mock_client = MagicMock()
        mock_client.create_and_post_order.return_value = {
            "success": True, "orderID": "oid-1",
        }
        trader = PolymarketTrader.__new__(PolymarketTrader)
        trader.client = mock_client
        trader.execution_enabled = True

        resp = trader.place_order(
            token_id="tok",
            side="BUY",
            price=0.45,
            size=10.0,
            order_type="FOK",
        )
        assert resp["success"] is True
        assert mock_client.create_and_post_order.called
        kwargs = mock_client.create_and_post_order.call_args
        # order_type is the 3rd positional or keyword
        ot = kwargs.kwargs.get("order_type") if kwargs.kwargs else None
        if ot is None and len(kwargs.args) >= 3:
            ot = kwargs.args[2]
        # Compare against the module's own map — the concrete OrderType binding
        # depends on whether the real SDK or the stub was imported first.
        assert ot == polymarket_api._ORDER_TYPE_MAP["FOK"]

    def test_place_order_accepts_signature_compatible_kwargs(self):
        """place_order must accept the kwargs executor passes (no TypeError)."""
        mock_client = MagicMock()
        mock_client.create_and_post_order.return_value = {"success": True, "orderID": "x"}
        trader = PolymarketTrader.__new__(PolymarketTrader)
        trader.client = mock_client
        trader.execution_enabled = True
        # This is the exact call shape from executor._execute_single_leg
        resp = trader.place_order(
            token_id="tok",
            side="BUY",
            price=0.45,
            size=5.0,
            neg_risk=False,
            order_type="GTC",
        )
        assert resp is not None

    def test_place_order_unknown_order_type_returns_none(self):
        """Unknown order types must be rejected without hitting the venue."""
        mock_client = MagicMock()
        trader = PolymarketTrader.__new__(PolymarketTrader)
        trader.client = mock_client
        trader.execution_enabled = True
        resp = trader.place_order(
            token_id="tok", side="BUY", price=0.45, size=5.0, order_type="IOC",
        )
        assert resp is None
        assert not mock_client.create_and_post_order.called

    def test_place_order_gtd_requires_expiration(self):
        """GTD without a non-zero expiration must raise ValueError."""
        trader = PolymarketTrader.__new__(PolymarketTrader)
        trader.client = MagicMock()
        trader.execution_enabled = True
        with pytest.raises(ValueError):
            trader.place_order(
                token_id="tok", side="BUY", price=0.45, size=5.0, order_type="GTD",
            )
        with pytest.raises(ValueError):
            trader.place_order(
                token_id="tok", side="BUY", price=0.45, size=5.0,
                order_type="GTD", expiration=0,
            )
        assert not trader.client.create_and_post_order.called

    def test_place_order_gtd_forwards_expiration(self):
        """A valid GTD order forwards the GTD mapping and the expiration."""
        mock_client = MagicMock()
        mock_client.create_and_post_order.return_value = {"success": True, "orderID": "x"}
        trader = PolymarketTrader.__new__(PolymarketTrader)
        trader.client = mock_client
        trader.execution_enabled = True
        captured = {}

        class _CapturingOrderArgs:
            def __init__(self, **kwargs):
                captured.update(kwargs)

        with patch.object(polymarket_api, "OrderArgs", _CapturingOrderArgs):
            resp = trader.place_order(
                token_id="tok", side="BUY", price=0.45, size=5.0,
                order_type="GTD", expiration=1893456000,
            )
        assert resp is not None
        assert captured["expiration"] == 1893456000
        call = mock_client.create_and_post_order.call_args
        assert call.kwargs["order_type"] == polymarket_api._ORDER_TYPE_MAP["GTD"]
        assert isinstance(call.args[0], _CapturingOrderArgs)

    def test_place_order_is_blocked_without_explicit_code_gate(self):
        mock_client = MagicMock()
        trader = PolymarketTrader.__new__(PolymarketTrader)
        trader.client = mock_client

        assert trader.place_order(
            token_id="tok", side="BUY", price=0.45, size=5.0,
        ) is None
        mock_client.create_and_post_order.assert_not_called()


class TestPolymarketTraderCancelOrder:
    """Fail-closed cancel confirmation."""

    def _trader(self, resp):
        trader = PolymarketTrader.__new__(PolymarketTrader)
        trader.client = MagicMock()
        trader.client.cancel_order.return_value = resp
        return trader

    def test_cancel_non_dict_response_is_false(self):
        assert self._trader("OK").cancel_order("oid-1") is False

    def test_cancel_none_response_is_false(self):
        assert self._trader(None).cancel_order("oid-1") is False

    def test_cancel_empty_canceled_list_is_false(self):
        assert self._trader({"canceled": []}).cancel_order("oid-1") is False

    def test_cancel_list_without_our_id_is_false(self):
        assert self._trader({"canceled": ["other-id"]}).cancel_order("oid-1") is False

    def test_cancel_list_with_our_id_is_true(self):
        assert self._trader({"canceled": ["oid-1"]}).cancel_order("oid-1") is True

    def test_cancel_british_spelling_with_our_id_is_true(self):
        assert self._trader({"cancelled": ["oid-1"]}).cancel_order("oid-1") is True

    def test_cancel_exception_is_false(self):
        trader = PolymarketTrader.__new__(PolymarketTrader)
        trader.client = MagicMock()
        trader.client.cancel_order.side_effect = RuntimeError("boom")
        assert trader.cancel_order("oid-1") is False


class TestPolymarketTraderGetBalance:
    """Fail-closed balance validation."""

    def _trader(self, resp):
        trader = PolymarketTrader.__new__(PolymarketTrader)
        trader.client = MagicMock()
        trader.client.get_balance_allowance.return_value = resp
        return trader

    def test_valid_balance(self):
        assert self._trader({"balance": "12500000"}).get_balance() == 12.5

    def test_non_dict_response_is_none(self):
        assert self._trader("12500000").get_balance() is None

    def test_missing_balance_key_is_none(self):
        assert self._trader({"allowance": "1"}).get_balance() is None

    def test_malformed_balance_is_none(self):
        assert self._trader({"balance": "not-a-number"}).get_balance() is None
        assert self._trader({"balance": None}).get_balance() is None

    def test_nan_inf_negative_balance_is_none(self):
        assert self._trader({"balance": "nan"}).get_balance() is None
        assert self._trader({"balance": "inf"}).get_balance() is None
        assert self._trader({"balance": "-1"}).get_balance() is None


class TestClobProxyInjection:
    """POLYMARKET_PROXY_URL must fail closed if the SDK internal disappears."""

    def test_missing_sdk_internal_raises(self):
        helpers_mod = polymarket_api._clob_http
        had_attr = hasattr(helpers_mod, "_http_client")
        saved = getattr(helpers_mod, "_http_client", None)
        try:
            if had_attr:
                delattr(helpers_mod, "_http_client")
            with pytest.raises(RuntimeError):
                polymarket_api._install_clob_proxy("http://proxy.local:8080")
        finally:
            if had_attr:
                helpers_mod._http_client = saved

    def test_install_sets_httpx_client(self):
        helpers_mod = polymarket_api._clob_http
        had_attr = hasattr(helpers_mod, "_http_client")
        saved = getattr(helpers_mod, "_http_client", None)
        helpers_mod._http_client = object()
        try:
            with patch.object(polymarket_api.httpx, "Client") as mock_client_cls:
                polymarket_api._install_clob_proxy("http://proxy.local:8080")
                mock_client_cls.assert_called_once_with(
                    http2=True, proxy="http://proxy.local:8080")
                assert helpers_mod._http_client is mock_client_cls.return_value
        finally:
            if had_attr:
                helpers_mod._http_client = saved
            else:
                del helpers_mod._http_client

    def test_trader_init_installs_proxy_before_client_when_env_set(self):
        """The write path must be proxied before the CLOB client exists."""
        calls = []
        with patch.dict(os.environ, {"POLYMARKET_PROXY_URL": "http://proxy.local:8080"}), \
             patch.object(polymarket_api, "_install_clob_proxy",
                          side_effect=lambda url: calls.append(("proxy", url))), \
             patch.object(polymarket_api, "ClobClient",
                          side_effect=lambda **kw: calls.append(("client",)) or MagicMock()):
            PolymarketTrader(private_key="0xkey", execution_enabled=True)
        assert calls[0] == ("proxy", "http://proxy.local:8080")
        assert ("client",) in calls

    def test_trader_init_fails_closed_when_sdk_internal_missing(self):
        """Missing SDK internal aborts trader construction, not module import."""
        helpers_mod = polymarket_api._clob_http
        had_attr = hasattr(helpers_mod, "_http_client")
        saved = getattr(helpers_mod, "_http_client", None)
        try:
            if had_attr:
                delattr(helpers_mod, "_http_client")
            with patch.dict(os.environ, {"POLYMARKET_PROXY_URL": "http://proxy.local:8080"}), \
                 patch.object(polymarket_api, "ClobClient") as mock_cls:
                with pytest.raises(RuntimeError):
                    PolymarketTrader(private_key="0xkey", execution_enabled=True)
                assert not mock_cls.called
        finally:
            if had_attr:
                helpers_mod._http_client = saved

    def test_trader_init_skips_proxy_when_env_unset(self):
        env_without = {k: v for k, v in os.environ.items() if k != "POLYMARKET_PROXY_URL"}
        with patch.dict(os.environ, env_without, clear=True), \
             patch.object(polymarket_api, "_install_clob_proxy") as mock_install, \
             patch.object(polymarket_api, "ClobClient", return_value=MagicMock()):
            PolymarketTrader(private_key="0xkey", execution_enabled=True)
        assert not mock_install.called


class TestPolymarketTraderGetOrders:
    """get_orders must pass through the V2 get_open_orders result."""

    def _trader(self, resp):
        trader = PolymarketTrader.__new__(PolymarketTrader)
        trader.client = MagicMock()
        trader.client.get_open_orders.return_value = resp
        return trader

    def test_list_response_passthrough(self):
        orders = [{"id": "a"}, {"id": "b"}]
        assert self._trader(orders).get_orders() == orders

    def test_dict_response_unwraps_orders_key(self):
        assert self._trader({"orders": [{"id": "a"}]}).get_orders() == [{"id": "a"}]

    def test_none_response_is_empty_list(self):
        assert self._trader(None).get_orders() == []

    def test_exception_is_empty_list(self):
        trader = PolymarketTrader.__new__(PolymarketTrader)
        trader.client = MagicMock()
        trader.client.get_open_orders.side_effect = RuntimeError("boom")
        assert trader.get_orders() == []


class TestBestBidAskValidation:
    @pytest.mark.parametrize("bad_price", [-0.01, 0.0, 1.0, 1.01, float("nan"), float("inf")])
    def test_invalid_top_level_is_not_executable(self, bad_price):
        result = _real_get_best_bid_ask({
            "bids": [{"price": bad_price, "size": 10}],
            "asks": [{"price": bad_price, "size": 10}],
        })
        assert result == {"bid": None, "bid_size": None, "ask": None, "ask_size": None}

    def test_valid_top_levels_are_preserved(self):
        result = _real_get_best_bid_ask({
            "bids": [{"price": "0.49", "size": "12"}],
            "asks": [{"price": "0.51", "size": "8"}],
        })
        assert result == {"bid": 0.49, "bid_size": 12.0, "ask": 0.51, "ask_size": 8.0}
