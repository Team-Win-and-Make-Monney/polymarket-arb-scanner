"""Plan 10 tests — fill detection, auto-hedge, kill switch, canary, dry-run.

Spec test-plan cases 1-5 and 13-15 (docs/plans/10-mm-pilot-prep.md section 10).
Fail-before: none of these behaviors exist on origin/master (mm_pilot.py is
new); every test here fails on master by construction.
"""

import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import importlib
import json
import logging
from unittest.mock import MagicMock

import pytest

from market_maker import ToxicFlowDetector, VolatilityTracker
from mm_pilot import (
    ControlsPoller,
    FillEvent,
    KalshiMMPilot,
    SupabaseControlsClient,
    build_controls_client_from_env,
)


def live_config():
    """Resolve the LIVE config module.

    Other test files in this suite pop/replace sys.modules["config"], so a
    module-level `import config` binding can go stale — mm_pilot reads config
    off sys.modules at call time and would see a different object (same
    pattern as tests/test_negrisk_no_side.py).
    """
    return importlib.import_module("config")


TICKER = "KXTEST-26DEC31"


# ---------------------------------------------------------------------------
# Fakes / fixtures
# ---------------------------------------------------------------------------

def make_book(yes_bid=0.49, no_bid=0.49, yes_qty=500.0, no_qty=500.0):
    """Raw Kalshi book (orderbook_fp schema). yes_ask derives as 1 - no_bid."""
    return {"orderbook_fp": {
        "yes_dollars": [[f"{yes_bid:.4f}", f"{yes_qty:.2f}"]],
        "no_dollars": [[f"{no_bid:.4f}", f"{no_qty:.2f}"]],
    }}


class FakeKalshiClient:
    def __init__(self, books=None):
        self.books = books if books is not None else {TICKER: make_book()}
        self.fills_script: list[dict] = []
        self.placed: list[dict] = []
        self.cancelled: list[str] = []
        self.place_order_calls = 0
        self.cancel_order_calls = 0
        self.fail_place = False
        # Cancel-retry / backoff testing (finding #2): the next N calls to
        # cancel_order fail (return False without raising); 0 means succeed.
        self.cancel_fail_count = 0
        # Fill-poll-failure testing (finding #3): raise instead of returning
        # fills_script when True and the caller opted into raise_on_error.
        self.fail_get_fills = False
        # Reconciliation testing (finding #4).
        self.positions_script: list[dict] = []
        self.open_orders_script: list[dict] = []
        self.fail_get_positions = False
        self.fail_get_open_orders = False

    def fetch_order_book(self, ticker):
        return self.books.get(ticker)

    def place_order(self, ticker, side, action, count, price_dollars,
                    time_in_force="fill_or_kill", reducing=False,
                    client_order_id=None):
        self.place_order_calls += 1
        if self.fail_place:
            return None
        oid = f"k_{self.place_order_calls}"
        self.placed.append({
            "order_id": oid, "ticker": ticker, "side": side, "action": action,
            "count": count, "price": price_dollars, "tif": time_in_force,
            "reducing": reducing, "client_order_id": client_order_id,
        })
        return {"order": {"order_id": oid, "client_order_id": client_order_id}}

    def cancel_order(self, order_id):
        self.cancel_order_calls += 1
        if self.cancel_fail_count > 0:
            self.cancel_fail_count -= 1
            return False
        self.cancelled.append(order_id)
        self.open_orders_script = [
            order for order in self.open_orders_script
            if str(order.get("order_id") or order.get("id") or "") != order_id
        ]
        return True

    def get_fills(self, min_ts=None, raise_on_error=False, **kwargs):
        if self.fail_get_fills:
            if raise_on_error:
                raise RuntimeError("fake get_fills failure")
            return []
        return list(self.fills_script)

    def get_positions(self, raise_on_error=False):
        if self.fail_get_positions:
            if raise_on_error:
                raise RuntimeError("fake get_positions failure")
            return []
        return list(self.positions_script)

    def get_open_orders(self, ticker=None, **kwargs):
        if self.fail_get_open_orders:
            raise RuntimeError("fake get_open_orders failure")
        return list(self.open_orders_script)

    def find_order_by_client_order_id(self, client_order_id, ticker=None,
                                      **kwargs):
        for order in self.open_orders_script:
            if order.get("client_order_id") == client_order_id:
                return order
        return None


class RecordingHedger:
    """Stub hedge executor: records calls, scripted success."""

    def __init__(self, result=True):
        self.result = result
        self.calls: list[dict] = []

    def hedge_inventory(self, **kwargs):
        self.calls.append(kwargs)
        return self.result


def kfill(order_id, ticker=TICKER, side="yes", action="buy", count=4,
          yes_price=49, trade_id=None, is_taker=False, created=None):
    fill = {
        "trade_id": trade_id or f"tr_{order_id}_{count}_{yes_price}",
        "order_id": order_id, "ticker": ticker, "side": side,
        "action": action, "count": count, "yes_price": yes_price,
        "is_taker": is_taker,
    }
    if created is not None:
        fill["created_time"] = created
    return fill


@pytest.fixture
def clock():
    return [1_000_000.0]


@pytest.fixture
def pilot_env(monkeypatch):
    """Force the pilot's flag preconditions on (against the LIVE config).

    Yields the live config module so tests can monkeypatch further keys on
    the object mm_pilot actually reads.
    """
    cfg = live_config()
    monkeypatch.setattr(cfg, "MM_KALSHI_PILOT_ENABLED", True)
    monkeypatch.setattr(cfg, "MM_TOXIC_FLOW_ENABLED", True)
    monkeypatch.setattr(cfg, "MM_VOLATILITY_ADJUSTED_ENABLED", True)
    monkeypatch.setattr(cfg, "MM_AUTO_HEDGE_ENABLED", True)
    yield cfg


def build_pilot(clock, client=None, dry_run=False, hedger=None,
                controls_on=True, detector=None, vol=None,
                selection=(TICKER,), reconciled=True):
    def time_fn():
        return clock[0]
    controls = ControlsPoller(time_fn=time_fn)
    controls.set_cached(controls_on)
    decisions: list[dict] = []
    hedger = hedger if hedger is not None else RecordingHedger()
    pilot = KalshiMMPilot(
        kalshi_client=client,
        controls=controls,
        toxic_detector=detector or ToxicFlowDetector(),
        # min_samples=1: most gate/fill/hedge tests aren't exercising G8's
        # volatility warm-up behavior and only ever record one book/WS
        # price tick before quoting — a real (non-injected) tracker here
        # would otherwise fail G8 as "insufficient_samples" on every one of
        # them. TestVolatilityGate and the warm-up test inject their own
        # fakes / real tracker with the production default instead.
        volatility_tracker=vol or VolatilityTracker(min_samples=1),
        hedger_factory=lambda proxy: hedger,
        decision_writer=decisions.append,
        dry_run=dry_run,
        time_fn=time_fn,
        # Route the monotonic clock through the SAME fake clock as wall
        # time so tests that fast-forward `clock[0]` (hedge latency,
        # cancel-retry backoff, etc.) move both together. Real
        # time.monotonic() would ignore the fake clock entirely.
        mono_fn=time_fn,
        # Disable local-file persistence in unit tests (parallels
        # decision_writer=... above disabling decisions.jsonl writes).
        state_path=None,
    )
    pilot.update_selection(list(selection))
    if client is not None:
        for ticker, book in client.books.items():
            pilot.update_book(ticker, book)
    # Most tests exercise gate/fill/hedge logic, not the startup
    # reconciliation feature itself — default to "already reconciled" so
    # authorize_order's finding-#4 gate doesn't block every other test.
    # TestReconciliation below constructs KalshiMMPilot directly to exercise
    # the real unreconciled-by-default state.
    pilot._reconciled = reconciled or dry_run
    pilot._decisions = decisions
    pilot._test_controls = controls
    pilot._test_hedger = hedger
    return pilot


class TestAlertLogging:
    @pytest.mark.parametrize(("severity", "expected_level"), [
        ("INFO", logging.INFO),
        ("WARNING", logging.WARNING),
        ("CRITICAL", logging.CRITICAL),
    ])
    def test_alert_logs_at_declared_severity(self, clock, caplog, severity,
                                             expected_level):
        pilot = build_pilot(clock, dry_run=True)
        with caplog.at_level(logging.INFO, logger="mm_pilot"):
            pilot._alert("test_alert", severity, "test message")

        record = caplog.records[-1]
        assert record.levelno == expected_level
        assert record.getMessage() == "MM pilot test_alert: test message"


# ---------------------------------------------------------------------------
# 1. Fill detection: exactly one FillEvent, deduped across polls
# ---------------------------------------------------------------------------

class TestFillDetection:
    def test_registered_fill_yields_exactly_one_event(self, pilot_env, clock):
        client = FakeKalshiClient()
        pilot = build_pilot(clock, client=client)
        oid = pilot.place_pilot_order(TICKER, "yes", "buy", 4, 0.49,
                                      purpose="quote_bid")
        assert oid is not None
        client.fills_script = [kfill(oid, count=4, created=clock[0])]

        events = pilot.poll_fills()
        assert len(events) == 1
        assert isinstance(events[0], FillEvent)
        assert events[0].order_id == oid
        assert events[0].count == 4
        assert events[0].price == pytest.approx(0.49)

    def test_duplicate_fill_across_overlap_window_deduped(self, pilot_env, clock):
        client = FakeKalshiClient()
        pilot = build_pilot(clock, client=client)
        oid = pilot.place_pilot_order(TICKER, "yes", "buy", 4, 0.49,
                                      purpose="quote_bid")
        fill = kfill(oid, count=4, created=clock[0])
        client.fills_script = [fill]
        first = pilot.poll_fills()
        # Same fill re-served by the 60s overlap window on the next poll
        clock[0] += 2
        second = pilot.poll_fills()
        assert len(first) == 1
        assert len(second) == 0

    def test_fill_without_trade_id_halts_before_accounting(self, pilot_env,
                                                            clock):
        client = FakeKalshiClient()
        pilot = build_pilot(clock, client=client)
        oid = pilot.place_pilot_order(TICKER, "yes", "buy", 4, 0.49,
                                      purpose="quote_bid")
        fill = kfill(oid, count=4, created=clock[0])
        del fill["trade_id"]
        client.fills_script = [fill]

        assert pilot.poll_fills() == []
        assert pilot.halted is True
        assert "fill without trade_id" in pilot.halt_reason
        assert pilot.inventory.net_contracts(TICKER) == 0


# ---------------------------------------------------------------------------
# 2. Unknown-order fill in a pilot market -> halt, all cancels issued
# ---------------------------------------------------------------------------

class TestUnknownOrderFill:
    def test_foreign_fill_halts_pilot_and_cancels_everything(self, pilot_env, clock):
        client = FakeKalshiClient()
        pilot = build_pilot(clock, client=client)
        oid = pilot.place_pilot_order(TICKER, "yes", "buy", 4, 0.49,
                                      purpose="quote_bid")
        client.fills_script = [kfill("someone_elses_order", created=clock[0])]

        pilot.poll_fills()
        assert pilot.halted is True
        assert "unknown order_id" in pilot.halt_reason
        assert oid in client.cancelled          # resting quote cancelled
        assert pilot.resting_orders() == []

    def test_fill_in_non_pilot_market_is_ignored(self, pilot_env, clock):
        client = FakeKalshiClient()
        pilot = build_pilot(clock, client=client)
        client.fills_script = [kfill("arb_executor_order", ticker="OTHER-MKT",
                                     created=clock[0])]
        events = pilot.poll_fills()
        assert events == []
        assert pilot.halted is False


# ---------------------------------------------------------------------------
# 3. Auto-hedge fires past the deadband, correct reduce direction
# ---------------------------------------------------------------------------

class TestAutoHedge:
    def test_long_fill_past_deadband_triggers_sell_yes_hedge(self, pilot_env, clock):
        client = FakeKalshiClient()
        hedger = RecordingHedger(result=True)
        pilot = build_pilot(clock, client=client, hedger=hedger)
        pilot.canary_graduated = True  # isolate hedge logic from canary sizing
        oid = pilot.place_pilot_order(TICKER, "yes", "buy", 20, 0.50,
                                      purpose="quote_bid")
        client.fills_script = [kfill(oid, count=20, yes_price=50,
                                     created=clock[0])]
        pilot.poll_fills()
        # |net| = $10 > deadband $5 -> hedge with excess = $10
        assert len(hedger.calls) == 1
        call = hedger.calls[0]
        assert call["side"] == "yes"
        assert call["platform"] == "kalshi"
        assert call["size"] == pytest.approx(10.0)
        assert call["reduce_action"] == "sell"

    def test_short_fill_past_deadband_hedges_no_side(self, pilot_env, clock):
        client = FakeKalshiClient()
        hedger = RecordingHedger(result=True)
        pilot = build_pilot(clock, client=client, hedger=hedger)
        pilot.canary_graduated = True
        oid = pilot.place_pilot_order(TICKER, "no", "buy", 20, 0.50,
                                      purpose="quote_ask")
        client.fills_script = [kfill(oid, side="no", count=20, yes_price=50,
                                     created=clock[0])]
        pilot.poll_fills()
        assert len(hedger.calls) == 1
        assert hedger.calls[0]["side"] == "no"

    def test_fill_inside_deadband_does_not_hedge(self, pilot_env, clock):
        client = FakeKalshiClient()
        hedger = RecordingHedger(result=True)
        pilot = build_pilot(clock, client=client, hedger=hedger)
        oid = pilot.place_pilot_order(TICKER, "yes", "buy", 4, 0.49,
                                      purpose="quote_bid")
        client.fills_script = [kfill(oid, count=4, created=clock[0])]
        pilot.poll_fills()
        # $1.96 < $5 deadband: rebalance arm, no hedge order
        assert hedger.calls == []
        assert any(d.get("reason") == "hedge_deadband"
                   for d in pilot._decisions)


# ---------------------------------------------------------------------------
# 4. Hedge failure -> quotes pulled, market halted, no re-quote
# ---------------------------------------------------------------------------

class TestHedgeFailClosed:
    def test_failed_hedge_pulls_quotes_and_halts_market(self, pilot_env, clock):
        client = FakeKalshiClient()
        hedger = RecordingHedger(result=False)  # e.g. book with no bids
        pilot = build_pilot(clock, client=client, hedger=hedger)
        pilot.canary_graduated = True  # post-canary: market halt, not pilot halt
        oid = pilot.place_pilot_order(TICKER, "yes", "buy", 20, 0.50,
                                      purpose="quote_bid")
        client.fills_script = [kfill(oid, count=20, yes_price=50,
                                     created=clock[0])]
        pilot.poll_fills()
        # Retry once, then fail closed
        assert len(hedger.calls) == 2
        assert TICKER in pilot.get_status()["markets_halted"]
        assert pilot.resting_orders(TICKER) == []
        # Next refresh places zero quotes in the halted market
        assert pilot.refresh_market(TICKER) == []

    def test_hedge_failure_during_canary_halts_whole_pilot(self, pilot_env,
                                                           clock, monkeypatch):
        monkeypatch.setattr(live_config(), "MM_CANARY_QUOTE_SIZE_USD", 100.0)
        client = FakeKalshiClient()
        hedger = RecordingHedger(result=False)
        pilot = build_pilot(clock, client=client, hedger=hedger)
        oid = pilot.place_pilot_order(TICKER, "yes", "buy", 20, 0.50,
                                      purpose="quote_bid")
        client.fills_script = [kfill(oid, count=20, yes_price=50,
                                     created=clock[0])]
        pilot.poll_fills()
        assert pilot.halted is True

    def test_second_market_halt_in_window_halts_pilot(self, pilot_env, clock):
        client = FakeKalshiClient()
        pilot = build_pilot(clock, client=client)
        pilot.canary_graduated = True
        pilot.halt_market(TICKER, "hedge failure")
        assert pilot.halted is False
        clock[0] += 600  # inside MM_HALT_WINDOW_SECONDS (3600)
        pilot._market_halted.pop(TICKER)  # operator reconciled the market
        pilot.halt_market(TICKER, "hedge failure again")
        assert pilot.halted is True


# ---------------------------------------------------------------------------
# 5. Hedge latency ceiling (frozen clock)
# ---------------------------------------------------------------------------

class TestHedgeLatency:
    def test_latency_over_ceiling_takes_halt_path(self, pilot_env, clock,
                                                  monkeypatch):
        monkeypatch.setattr(live_config(), "MM_CANARY_QUOTE_SIZE_USD", 100.0)
        client = FakeKalshiClient()
        hedger = RecordingHedger(result=True)  # hedge eventually lands...
        pilot = build_pilot(clock, client=client, hedger=hedger)
        oid = pilot.place_pilot_order(TICKER, "yes", "buy", 20, 0.50,
                                      purpose="quote_bid")
        # Fill created 20s ago -> detection + hedge exceed the 10s ceiling
        client.fills_script = [kfill(oid, count=20, yes_price=50,
                                     created=clock[0] - 20)]
        pilot.poll_fills()
        assert pilot.halted is True
        assert "latency" in pilot.halt_reason

    def test_unfilled_hedge_order_past_ceiling_halts(self, pilot_env, clock):
        client = FakeKalshiClient()
        pilot = build_pilot(clock, client=client)
        pilot.inventory.apply_fill(TICKER, "yes", "buy", 10, 0.49)
        hoid = pilot.place_pilot_order(TICKER, "yes", "sell", 10, 0.49,
                                       purpose="hedge", reducing=True)
        assert hoid is not None
        clock[0] += 11  # past MM_HEDGE_MAX_LATENCY_SECONDS with no fill
        pilot.poll_fills()
        assert pilot.halted is True
        assert hoid in client.cancelled  # remainder cancelled


# ---------------------------------------------------------------------------
# 13. Kill switch: flip false / stale cache -> cancel everything
# ---------------------------------------------------------------------------

class TestKillSwitch:
    def test_read_only_rest_client_fetches_control(self):
        response = MagicMock()
        response.json.return_value = [{"value": True}]
        session = MagicMock()
        session.get.return_value = response
        client = SupabaseControlsClient(
            "https://example.supabase.co", "test-key", session=session)

        assert client.fetch_control("mm_pilot_enabled") is True
        response.raise_for_status.assert_called_once()
        _, kwargs = session.get.call_args
        assert kwargs["params"]["key"] == "eq.mm_pilot_enabled"
        assert kwargs["params"]["select"] == "value"

    def test_read_only_rest_client_rejects_string_false(self):
        response = MagicMock()
        response.json.return_value = [{"value": "false"}]
        session = MagicMock()
        session.get.return_value = response
        client = SupabaseControlsClient(
            "https://example.supabase.co", "test-key", session=session)

        assert client.fetch_control("mm_pilot_enabled") is False

    def test_rest_client_builder_requires_control_credentials(
            self, monkeypatch):
        monkeypatch.delenv("SUPABASE_URL", raising=False)
        monkeypatch.delenv("SUPABASE_SERVICE_KEY", raising=False)
        monkeypatch.delenv("SUPABASE_SERVICE_ROLE_KEY", raising=False)
        monkeypatch.delenv("SUPABASE_KEY", raising=False)
        with pytest.raises(RuntimeError, match="SUPABASE_URL"):
            build_controls_client_from_env()

    def test_rest_client_builder_accepts_service_role_key(self, monkeypatch):
        monkeypatch.setenv("SUPABASE_URL", "https://example.supabase.co")
        monkeypatch.delenv("SUPABASE_SERVICE_KEY", raising=False)
        monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "service-role-test")
        monkeypatch.delenv("SUPABASE_KEY", raising=False)

        client = build_controls_client_from_env()

        assert client._key == "service-role-test"

    @pytest.mark.parametrize("url", ["http://example.supabase.co", "https://169.254.169.254"])
    def test_rest_client_builder_rejects_unsafe_url(self, monkeypatch, url):
        monkeypatch.setenv("SUPABASE_URL", url)
        monkeypatch.setenv("SUPABASE_SERVICE_KEY", "service-role-test")

        with pytest.raises(ValueError, match="SUPABASE_URL"):
            build_controls_client_from_env()

    def test_controls_poller_accepts_read_only_rest_client(self, clock):
        client = MagicMock()
        client.fetch_control.return_value = True
        poller = ControlsPoller(supabase_client=client,
                                time_fn=lambda: clock[0])

        poller.poll()

        assert poller.is_enabled() is True
        client.fetch_control.assert_called_once_with("mm_pilot_enabled")

    def test_controls_flip_false_cancels_all_next_cycle(self, pilot_env, clock):
        client = FakeKalshiClient()
        pilot = build_pilot(clock, client=client)
        oid = pilot.place_pilot_order(TICKER, "yes", "buy", 4, 0.49,
                                      purpose="quote_bid")
        assert pilot.resting_orders() != []
        pilot._test_controls.set_cached(False)
        pilot.refresh_market(TICKER)
        assert pilot.halted is True
        assert oid in client.cancelled
        assert pilot.resting_orders() == []

    def test_stale_controls_cache_fails_closed(self, pilot_env, clock):
        client = FakeKalshiClient()
        pilot = build_pilot(clock, client=client)
        pilot.place_pilot_order(TICKER, "yes", "buy", 4, 0.49,
                                purpose="quote_bid")
        # Cache is true but older than MM_CONTROLS_MAX_STALE_SECONDS (300)
        pilot._test_controls.set_cached(True, fetched_at=clock[0] - 301)
        pilot.refresh_market(TICKER)
        assert pilot.halted is True
        assert pilot.resting_orders() == []

    def test_env_flag_off_rejects_orders(self, clock, monkeypatch):
        monkeypatch.setattr(live_config(), "MM_KALSHI_PILOT_ENABLED", False)
        client = FakeKalshiClient()
        pilot = build_pilot(clock, client=client)
        result = pilot.authorize_order(TICKER, "yes", "buy", 4, 0.49)
        assert result.allowed is False
        assert result.reason == "kill_switch_env"

    def test_controls_poller_fail_closed_without_client(self, clock):
        poller = ControlsPoller(supabase_client=None,
                                time_fn=lambda: clock[0])
        poller.poll()  # no client — cache never populates
        assert poller.is_enabled() is False


# ---------------------------------------------------------------------------
# 14. Canary: graduation, max loss, oversized fill
# ---------------------------------------------------------------------------

class TestCanary:
    def test_graduation_after_clean_fills_and_min_hours(self, pilot_env, clock):
        client = FakeKalshiClient()
        pilot = build_pilot(clock, client=client)
        oid = pilot.place_pilot_order(TICKER, "yes", "buy", 40, 0.49,
                                      purpose="quote_bid")
        for i in range(9):
            client.fills_script = [kfill(oid, count=4, created=clock[0],
                                         trade_id=f"c{i}")]
            pilot.poll_fills()
            clock[0] += 10
        assert pilot.canary_graduated is False
        clock[0] += 25 * 3600  # past MM_CANARY_MIN_HOURS
        client.fills_script = [kfill(oid, count=4, created=clock[0],
                                     trade_id="c9")]
        pilot.poll_fills()
        assert pilot.canary_clean_fills == 10
        assert pilot.canary_graduated is True
        assert any(d.get("reason") == "CANARY PASSED"
                   for d in pilot._decisions)

    def test_canary_loss_over_ceiling_halts(self, pilot_env, clock, monkeypatch):
        monkeypatch.setattr(live_config(), "MM_CANARY_QUOTE_SIZE_USD", 100.0)
        # no_bid=0.30 -> yes_ask=0.70, so a resting bid at 0.60 does not
        # cross (finding #2's pre-submit TOCTOU guard would otherwise abort
        # this placement outright — the default book's 0.51 ask is below
        # 0.60 and this test isn't about crossing behavior).
        client = FakeKalshiClient(books={TICKER: make_book(no_bid=0.30)})
        pilot = build_pilot(clock, client=client, hedger=RecordingHedger())
        # Buy 20 @ 0.60, hedge-exit 20 @ 0.05 -> realized -$11.00 < -$10
        oid = pilot.place_pilot_order(TICKER, "yes", "buy", 20, 0.60,
                                      purpose="quote_bid")
        client.fills_script = [kfill(oid, count=20, yes_price=60,
                                     created=clock[0], trade_id="in")]
        pilot.poll_fills()
        hoid = pilot.place_pilot_order(TICKER, "yes", "sell", 20, 0.05,
                                       purpose="hedge", reducing=True)
        client.fills_script = [kfill(hoid, action="sell", count=20,
                                     yes_price=5, created=clock[0],
                                     trade_id="out", is_taker=True)]
        pilot.poll_fills()
        assert pilot.inventory.realized_pnl_total() == pytest.approx(-11.0)
        assert pilot.halted is True
        assert "canary realized P&L" in pilot.halt_reason

    def test_canary_fill_oversized_is_a_deviation_halt(self, pilot_env, clock):
        client = FakeKalshiClient()
        pilot = build_pilot(clock, client=client)
        oid = pilot.place_pilot_order(TICKER, "yes", "buy", 10, 0.50,
                                      purpose="quote_bid")
        # $5.00 notional > MM_CANARY_QUOTE_SIZE_USD ($2)
        client.fills_script = [kfill(oid, count=10, yes_price=50,
                                     created=clock[0])]
        pilot.poll_fills()
        assert pilot.halted is True
        assert "exceeds canary size" in pilot.halt_reason

    def test_taker_fill_on_resting_quote_halts(self, pilot_env, clock):
        client = FakeKalshiClient()
        pilot = build_pilot(clock, client=client)
        oid = pilot.place_pilot_order(TICKER, "yes", "buy", 4, 0.49,
                                      purpose="quote_bid")
        client.fills_script = [kfill(oid, count=4, created=clock[0],
                                     is_taker=True)]
        pilot.poll_fills()
        assert pilot.halted is True
        assert "taker fill" in pilot.halt_reason
        # Codex round-3 finding: the halt must not come at the cost of
        # forgetting the fill. It already happened at the exchange —
        # halting stops FUTURE activity, it must never discard what
        # already occurred. Fail-before: halt_all() + return fired BEFORE
        # any of registry/inventory/log accounting ran, so the order
        # stayed at its pre-fill count and inventory read zero even
        # though 4 real contracts had actually traded.
        assert oid not in pilot._orders  # registry: fully filled, removed
        assert pilot.inventory.net_contracts(TICKER) == 4  # inventory recorded
        assert pilot.inventory.net_usd(TICKER) == pytest.approx(4 * 0.49)

    def test_taker_fill_on_resting_quote_records_partial_shrink(
            self, pilot_env, clock):
        """Same deviation, but a PARTIAL fill — the registry entry must
        shrink (not vanish) to reflect the remaining resting size, exactly
        like the non-deviation fill path already does. The fake client is
        set to fail cancels so halt_all()'s own pull_all() can't ALSO
        remove the order (via a successful cancel) before we can observe
        the shrink — isolating what THIS fix is responsible for from the
        pre-existing, separately-tested cancel-then-pop behavior."""
        client = FakeKalshiClient()
        pilot = build_pilot(clock, client=client)
        oid = pilot.place_pilot_order(TICKER, "yes", "buy", 10, 0.49,
                                      purpose="quote_bid")
        client.cancel_fail_count = 999
        client.fills_script = [kfill(oid, count=4, created=clock[0],
                                     is_taker=True)]
        pilot.poll_fills()
        assert pilot.halted is True
        assert oid in pilot._orders  # 10 - 4 = 6 remaining, not fully filled
        assert pilot._orders[oid]["count"] == 6
        assert pilot.inventory.net_contracts(TICKER) == 4

    def test_taker_fill_on_resting_quote_persists_state_before_halting(
            self, pilot_env, clock, tmp_path):
        """The corrected inventory must be durably persisted before the
        halt, not just held in memory — a crash immediately after halting
        must still be able to reconcile from the right numbers."""
        from mm_pilot import ControlsPoller, KalshiMMPilot, PilotStateStore
        path = tmp_path / "state.json"
        def time_fn():
            return clock[0]
        controls = ControlsPoller(time_fn=time_fn)
        controls.set_cached(True)
        client = FakeKalshiClient()
        pilot = KalshiMMPilot(
            kalshi_client=client, controls=controls,
            volatility_tracker=VolatilityTracker(min_samples=1),
            time_fn=time_fn, mono_fn=time_fn, state_path=str(path),
            dry_run=False,
        )
        pilot._reconciled = True
        pilot.update_selection([TICKER])
        pilot.update_book(TICKER, make_book())
        oid = pilot.place_pilot_order(TICKER, "yes", "buy", 4, 0.49,
                                      purpose="quote_bid")
        client.fills_script = [kfill(oid, count=4, created=clock[0],
                                     is_taker=True)]
        pilot.poll_fills()
        assert pilot.halted is True
        saved = PilotStateStore(str(path)).load()
        assert saved["inventory"]["net"].get(TICKER) == 4


# ---------------------------------------------------------------------------
# 15. Dry-run isolation: zero real client calls across a full session
# ---------------------------------------------------------------------------

class TestDryRunIsolation:
    def test_full_dry_session_never_touches_the_client(self, pilot_env, clock):
        client = FakeKalshiClient(books={TICKER: make_book(yes_bid=0.49,
                                                           no_bid=0.49)})
        pilot = build_pilot(clock, client=client, dry_run=True)
        # Full cycle: quotes placed (dry ids), book crosses generate synthetic
        # fills, hedge fires through the proxy, everything stays local.
        placed = pilot.refresh_market(TICKER)
        assert placed, "dry-run must still produce (synthetic) order ids"
        assert all(oid.startswith("dry_") for oid in placed)
        # Crash the mid through the bid so the dry bid fills
        pilot.update_book(TICKER, make_book(yes_bid=0.10, no_bid=0.88))
        for _ in range(5):
            pilot.poll_fills()
            clock[0] += 2
        pilot.refresh_market(TICKER)
        pilot.stop()
        assert client.place_order_calls == 0
        assert client.cancel_order_calls == 0

    def test_simulated_fill_runs_the_full_pipeline(self, pilot_env, clock):
        client = FakeKalshiClient()
        hedger = RecordingHedger()
        pilot = build_pilot(clock, client=client, dry_run=True, hedger=hedger)
        oid = pilot.place_pilot_order(TICKER, "yes", "buy", 4, 0.49,
                                      purpose="quote_bid")
        assert oid.startswith("dry_")
        pilot.update_book(TICKER, make_book(yes_bid=0.10, no_bid=0.88))
        events = pilot.poll_fills()
        assert len(events) == 1
        # End to end in dry-run: inventory updated + audit rows written
        assert pilot.inventory.net_contracts(TICKER) == 4
        assert any(d.get("gate") == "hedge" for d in pilot._decisions)


# ---------------------------------------------------------------------------
# Choke-point runtime counter (test 10 companion; grep half lives in
# test_mm_pilot_gates.py)
# ---------------------------------------------------------------------------

class TestPlaceOrderCounter:
    def test_all_live_placements_flow_through_the_choke_point(self, pilot_env,
                                                              clock):
        client = FakeKalshiClient()
        pilot = build_pilot(clock, client=client)
        pilot.refresh_market(TICKER)
        pilot.place_pilot_order(TICKER, "yes", "sell", 2, 0.49,
                                purpose="hedge", reducing=True)
        assert client.place_order_calls == pilot.place_order_calls
        assert client.place_order_calls > 0

    def test_hedger_proxy_preserves_client_order_identity(self, pilot_env,
                                                           clock):
        from mm_pilot import _PilotKalshiProxy

        client = FakeKalshiClient()
        pilot = build_pilot(clock, client=client)
        pilot.inventory.apply_fill(TICKER, "yes", "buy", 5, 0.49)
        proxy = _PilotKalshiProxy(pilot)

        response = proxy.place_order(
            TICKER, "yes", "sell", 5, 0.48,
            reducing=True, client_order_id="hedge-client-1",
        )

        assert response == {"order": {"order_id": "k_1"}}
        assert client.placed[-1]["client_order_id"] == "hedge-client-1"
        client.open_orders_script = [{
            "order_id": "k_1",
            "client_order_id": "hedge-client-1",
            "ticker": TICKER,
        }]
        assert proxy.find_order_by_client_order_id(
            "hedge-client-1", ticker=TICKER,
        )["order_id"] == "k_1"


# ---------------------------------------------------------------------------
# Finding #2: cancel confirms on the exchange before the registry pop;
# failed cancels stay in the registry, retry with backoff, and escalate to
# halt_all after MAX_CANCEL_ATTEMPTS.
# Fail-before: on the pre-fix code, _cancel_order popped the order_id from
# the registry unconditionally before even trying to cancel, so a failed
# cancel silently vanished from the registry while the order stayed live on
# the exchange (found invisible to future retries).
# ---------------------------------------------------------------------------

class TestCancelConfirmBeforePop:
    def test_failed_cancel_stays_in_registry_not_popped(self, pilot_env, clock):
        client = FakeKalshiClient()
        client.cancel_fail_count = 1  # first cancel attempt fails
        pilot = build_pilot(clock, client=client)
        oid = pilot.place_pilot_order(TICKER, "yes", "buy", 4, 0.49,
                                      purpose="quote_bid")
        ok = pilot._cancel_order(oid)
        assert ok is False
        assert oid in pilot._orders  # NOT popped — still tracked
        assert pilot._orders[oid]["pending_cancel"] is True
        assert oid not in client.cancelled

    def test_confirmed_cancel_pops_from_registry(self, pilot_env, clock):
        client = FakeKalshiClient()
        pilot = build_pilot(clock, client=client)
        oid = pilot.place_pilot_order(TICKER, "yes", "buy", 4, 0.49,
                                      purpose="quote_bid")
        ok = pilot._cancel_order(oid)
        assert ok is True
        assert oid not in pilot._orders
        assert oid in client.cancelled

    def test_repeated_failures_escalate_to_halt_after_max_attempts(
            self, pilot_env, clock):
        client = FakeKalshiClient()
        client.cancel_fail_count = 999  # never succeeds
        pilot = build_pilot(clock, client=client)
        oid = pilot.place_pilot_order(TICKER, "yes", "buy", 4, 0.49,
                                      purpose="quote_bid")
        for _ in range(3):
            pilot._cancel_order(oid)
        assert pilot.halted is True
        assert "uncancellable" in pilot.halt_reason
        # Still tracked — never silently dropped despite the halt.
        assert oid in pilot._orders

    def test_retry_pending_cancels_respects_backoff_then_succeeds(
            self, pilot_env, clock):
        client = FakeKalshiClient()
        client.cancel_fail_count = 1  # fails once, then succeeds
        pilot = build_pilot(clock, client=client)
        oid = pilot.place_pilot_order(TICKER, "yes", "buy", 4, 0.49,
                                      purpose="quote_bid")
        assert pilot._cancel_order(oid) is False
        # Immediately retrying inside the backoff window does nothing yet.
        pilot._retry_pending_cancels()
        assert client.cancel_order_calls == 1
        assert oid in pilot._orders
        # Advance the fake clock past the 1s backoff (attempt 1 -> 2**0=1s).
        clock[0] += 2
        pilot._retry_pending_cancels()
        assert client.cancel_order_calls == 2
        assert oid not in pilot._orders  # confirmed cancel, finally popped
        assert oid in client.cancelled


# ---------------------------------------------------------------------------
# Finding #3: a fill-poll failure must fail closed — skip the refresh step,
# and after FILL_POLL_FAILURE_LIMIT consecutive failures pull all resting
# quotes and halt quoting until polling recovers.
# Fail-before: get_fills exceptions were swallowed, poll_fills returned []
# indistinguishable from "confirmed no new fills", and refresh_all kept
# quoting through an indefinite blind spell.
# ---------------------------------------------------------------------------

class TestFillPollFailClosed:
    def test_single_failure_does_not_pull_or_halt(self, pilot_env, clock):
        client = FakeKalshiClient()
        client.fail_get_fills = True
        pilot = build_pilot(clock, client=client)
        pilot.place_pilot_order(TICKER, "yes", "buy", 4, 0.49,
                                purpose="quote_bid")
        events = pilot.poll_fills()
        assert events == []
        assert pilot.halted is False
        assert pilot._fill_poll_failures == 1
        assert pilot.resting_orders() != []  # not pulled yet

    def test_limit_consecutive_failures_pulls_all_and_blinds(
            self, pilot_env, clock):
        client = FakeKalshiClient()
        pilot = build_pilot(clock, client=client)
        oid = pilot.place_pilot_order(TICKER, "yes", "buy", 4, 0.49,
                                      purpose="quote_bid")
        client.fail_get_fills = True
        for _ in range(pilot.FILL_POLL_FAILURE_LIMIT):
            pilot.poll_fills()
        assert pilot._fills_blind is True
        assert pilot.halted is False  # blind, not a full halt — self-heals
        assert oid in client.cancelled  # pull_all fired
        placed_before = client.place_order_calls
        assert pilot.refresh_market(TICKER) == []
        assert pilot.refresh_all() == []
        assert client.place_order_calls == placed_before

    def test_recovery_clears_blind_state(self, pilot_env, clock):
        client = FakeKalshiClient()
        pilot = build_pilot(clock, client=client)
        client.fail_get_fills = True
        for _ in range(pilot.FILL_POLL_FAILURE_LIMIT):
            pilot.poll_fills()
        assert pilot._fills_blind is True
        client.fail_get_fills = False
        pilot.poll_fills()
        assert pilot._fills_blind is False
        assert pilot._fill_poll_failures == 0
        assert pilot.refresh_market(TICKER)


# ---------------------------------------------------------------------------
# Finding #4: startup reconciliation against live venue state. Quoting must
# be refused (authorize_order fails closed) until reconcile() succeeds; a
# fresh KalshiMMPilot in live mode starts unreconciled.
# Fail-before: _reconciled didn't exist / wasn't enforced anywhere, and
# _persist_state()/reconcile() were called or needed but never defined —
# a restart reseeded _last_fill_ts to "now" and inventory to zero with no
# attempt to recover real venue state.
# ---------------------------------------------------------------------------

class TestReconciliation:
    def _direct_pilot(self, clock, client, dry_run=False):
        """Construct KalshiMMPilot directly (bypassing build_pilot's
        reconciled=True convenience default) to exercise the true
        unreconciled-by-default live-mode state."""
        from mm_pilot import ControlsPoller, KalshiMMPilot
        def time_fn():
            return clock[0]
        controls = ControlsPoller(time_fn=time_fn)
        controls.set_cached(True)
        return KalshiMMPilot(
            kalshi_client=client,
            controls=controls,
            # See build_pilot's comment: avoid the production-default
            # min_samples=5 module singleton blocking G8 on tests that
            # aren't exercising volatility warm-up behavior.
            volatility_tracker=VolatilityTracker(min_samples=1),
            dry_run=dry_run,
            time_fn=time_fn,
            mono_fn=time_fn,
            state_path=None,  # no disk I/O in unit tests
        )

    def test_dry_run_is_reconciled_trivially(self, clock):
        pilot = self._direct_pilot(clock, client=None, dry_run=True)
        assert pilot._reconciled is True
        assert pilot.reconcile() is True

    def test_live_pilot_starts_unreconciled(self, pilot_env, clock):
        client = FakeKalshiClient()
        pilot = self._direct_pilot(clock, client)
        assert pilot._reconciled is False

    def test_unreconciled_pilot_rejects_every_order(self, pilot_env, clock):
        client = FakeKalshiClient()
        pilot = self._direct_pilot(clock, client)
        result = pilot.authorize_order(TICKER, "yes", "buy", 4, 0.49)
        assert result.allowed is False
        assert result.reason == "not_reconciled"

    def test_unreconciled_pilot_rejects_reducing_orders_too(self, pilot_env,
                                                            clock):
        # Reducing orders bypass inventory caps but must NOT bypass the
        # reconciliation gate — a "reduce" computed off unknown state is
        # not trustworthy either.
        client = FakeKalshiClient()
        pilot = self._direct_pilot(clock, client)
        result = pilot.authorize_order(TICKER, "yes", "sell", 4, 0.49,
                                       reducing=True)
        assert result.allowed is False
        assert result.reason == "not_reconciled"

    def test_evaluate_gates_pulls_with_not_reconciled_reason(self, pilot_env,
                                                             clock):
        client = FakeKalshiClient()
        pilot = self._direct_pilot(clock, client)
        pilot.update_selection([TICKER])
        plan = pilot._evaluate_gates(TICKER)
        assert plan["action"] == "pull"
        assert plan["reason"] == "not_reconciled"

    def test_no_client_fails_closed(self, pilot_env, clock):
        pilot = self._direct_pilot(clock, client=None)
        assert pilot.reconcile() is False
        assert pilot._reconciled is False

    def test_positions_query_failure_fails_closed(self, pilot_env, clock):
        client = FakeKalshiClient()
        client.fail_get_positions = True
        pilot = self._direct_pilot(clock, client)
        assert pilot.reconcile() is False
        assert pilot._reconciled is False

    def test_fills_query_failure_fails_closed(self, pilot_env, clock):
        client = FakeKalshiClient()
        client.fail_get_fills = True
        pilot = self._direct_pilot(clock, client)
        assert pilot.reconcile() is False
        assert pilot._reconciled is False

    def test_open_orders_query_failure_fails_closed(self, pilot_env, clock):
        client = FakeKalshiClient()
        client.fail_get_open_orders = True
        pilot = self._direct_pilot(clock, client)
        assert pilot.reconcile() is False
        assert pilot._reconciled is False

    def test_uncancellable_stale_order_fails_closed(self, pilot_env, clock):
        client = FakeKalshiClient()
        client.open_orders_script = [{"order_id": "stale_1", "ticker": TICKER}]
        client.cancel_fail_count = 999
        pilot = self._direct_pilot(clock, client)
        assert pilot.reconcile() is False
        assert pilot._reconciled is False

    def test_indeterminate_lookup_failure_keeps_pilot_unreconciled(
            self, pilot_env, clock, monkeypatch):
        client = FakeKalshiClient()
        pilot = self._direct_pilot(clock, client)
        pilot._pending_submissions["durable-client-id"] = {
            "ticker": TICKER,
        }

        def fail_lookup(*args, **kwargs):
            raise RuntimeError("order history unavailable")

        monkeypatch.setattr(client, "find_order_by_client_order_id", fail_lookup)

        assert pilot.reconcile() is False
        assert pilot._reconciled is False
        assert "durable-client-id" in pilot._pending_submissions

    def test_success_seeds_inventory_and_cancels_stale_orders(self, pilot_env,
                                                               clock):
        # Field names match Kalshi's documented MarketPosition schema
        # (docs.kalshi.com/api-reference/portfolio/get-positions):
        # position_fp is the signed net-contracts STRING (not "position" /
        # "net_contracts"), and there is no average-price field at all —
        # avg cost is derived from market_exposure_dollars / |position_fp|.
        # A CodeRabbit round-3 finding caught the original test (and the
        # reconcile() code it was validating) guessing at the wrong keys,
        # which would have silently reconciled every real position to zero.
        client = FakeKalshiClient()
        client.positions_script = [
            {"ticker": TICKER, "position_fp": "12",
             "market_exposure_dollars": 5.28},  # 12 contracts @ $0.44 avg
        ]
        client.open_orders_script = [
            {"order_id": "stale_1", "ticker": TICKER},
            {"order_id": "stale_2", "ticker": TICKER},
        ]
        pilot = self._direct_pilot(clock, client)
        ok = pilot.reconcile()
        assert ok is True
        assert pilot._reconciled is True
        assert pilot.inventory.net_contracts(TICKER) == 12
        assert pilot.inventory.avg_cost(TICKER) == pytest.approx(0.44)
        assert set(client.cancelled) == {"stale_1", "stale_2"}
        assert pilot._orders == {}  # nothing adopted — clean slate

    def test_negative_position_fp_means_no_contracts(self, pilot_env, clock):
        """Per the documented schema: negative position_fp = NO contracts
        (mm_pilot's inventory convention: long NO is a negative signed
        count too — signs line up directly, no inversion needed)."""
        client = FakeKalshiClient()
        client.positions_script = [
            {"ticker": TICKER, "position_fp": "-7",
             "market_exposure_dollars": 3.5},
        ]
        pilot = self._direct_pilot(clock, client)
        assert pilot.reconcile() is True
        assert pilot.inventory.net_contracts(TICKER) == -7
        assert pilot.inventory.avg_cost(TICKER) == pytest.approx(0.5)

    def test_unparsable_position_fp_fails_closed(self, pilot_env, clock):
        """Malformed venue positions cannot be treated as zero exposure."""
        client = FakeKalshiClient()
        client.positions_script = [
            {"ticker": TICKER, "position_fp": "not-a-number"},
        ]
        pilot = self._direct_pilot(clock, client)
        assert pilot.reconcile() is False
        assert pilot._reconciled is False
        assert pilot.inventory.net_contracts(TICKER) == 0

    def test_cancels_and_confirms_before_position_and_fill_snapshot(
            self, pilot_env, clock):
        calls: list[str] = []

        class OrderedClient(FakeKalshiClient):
            def get_open_orders(self, ticker=None, **kwargs):
                calls.append("open_orders")
                return super().get_open_orders(ticker=ticker, **kwargs)

            def cancel_order(self, order_id):
                calls.append("cancel")
                return super().cancel_order(order_id)

            def get_positions(self, raise_on_error=False):
                calls.append("positions")
                return super().get_positions(raise_on_error=raise_on_error)

            def get_fills(self, min_ts=None, raise_on_error=False, **kwargs):
                calls.append("fills")
                return super().get_fills(
                    min_ts=min_ts, raise_on_error=raise_on_error, **kwargs)

        client = OrderedClient()
        client.open_orders_script = [{"order_id": "stale_1", "ticker": TICKER}]
        pilot = self._direct_pilot(clock, client)

        assert pilot.reconcile() is True
        assert calls == ["open_orders", "cancel", "open_orders",
                         "positions", "fills"]

    def test_missing_exposure_field_assumes_worst_case_not_zero(self,
                                                                 pilot_env,
                                                                 clock):
        """CodeRabbit round-3: a 0.0 avg-cost fallback would make
        net_usd() = net * avg read as $0 regardless of contract count,
        silently bypassing every USD-denominated cap for a ticker
        reconciliation just discovered real inventory on. A missing/
        unparsable exposure field must assume the worst case ($1.00 —
        the max possible price per contract) so caps trip early instead
        of being bypassed."""
        client = FakeKalshiClient()
        client.positions_script = [
            {"ticker": TICKER, "position_fp": "10"},  # no exposure field
        ]
        pilot = self._direct_pilot(clock, client)
        assert pilot.reconcile() is True
        assert pilot.inventory.net_contracts(TICKER) == 10
        assert pilot.inventory.avg_cost(TICKER) == pytest.approx(1.0)
        assert pilot.inventory.net_usd(TICKER) == pytest.approx(10.0)

    def test_unparsable_exposure_field_also_assumes_worst_case(self,
                                                                pilot_env,
                                                                clock):
        client = FakeKalshiClient()
        client.positions_script = [
            {"ticker": TICKER, "position_fp": "10",
             "market_exposure_dollars": "not-a-number"},
        ]
        pilot = self._direct_pilot(clock, client)
        assert pilot.reconcile() is True
        assert pilot.inventory.avg_cost(TICKER) == pytest.approx(1.0)

    def test_success_marks_fills_seen_and_advances_cursor(self, pilot_env,
                                                           clock):
        client = FakeKalshiClient()
        client.fills_script = [kfill("k_old", count=3, yes_price=40,
                                     trade_id="tr_old", created=clock[0] - 5000)]
        pilot = self._direct_pilot(clock, client)
        assert pilot.reconcile() is True
        assert "tr_old" in pilot._seen_fill_ids
        assert pilot._last_fill_ts == pytest.approx(clock[0] - 5000)

    def test_reconciled_pilot_can_then_place_orders(self, pilot_env, clock):
        client = FakeKalshiClient()
        pilot = self._direct_pilot(clock, client)
        assert pilot.reconcile() is True
        oid = pilot.place_pilot_order(TICKER, "yes", "buy", 4, 0.49,
                                      purpose="quote_bid")
        assert oid is not None

    def test_run_loop_reconciles_before_quoting(self, pilot_env, clock,
                                                monkeypatch):
        """Integration: run_loop must call reconcile() before its first
        refresh/poll cycle, and must not place orders while unreconciled."""
        import threading
        client = FakeKalshiClient()
        pilot = self._direct_pilot(clock, client)
        pilot.update_selection([TICKER])
        stop = threading.Event()
        assert pilot._reconciled is False

        calls = []
        reconcile = pilot.reconcile
        refresh_all = pilot.refresh_all

        def recording_reconcile():
            calls.append("reconcile")
            return reconcile()

        def recording_refresh():
            calls.append("refresh")
            placed = refresh_all()
            stop.set()
            return placed

        def no_op_stop():
            calls.append("stop")

        monkeypatch.setattr(pilot, "reconcile", recording_reconcile)
        monkeypatch.setattr(pilot, "refresh_all", recording_refresh)
        monkeypatch.setattr(pilot, "stop", no_op_stop)

        pilot.run_loop(stop)

        assert pilot._reconciled is True
        assert calls[:2] == ["reconcile", "refresh"]
        assert pilot.resting_orders()  # placed only after reconciliation


# ---------------------------------------------------------------------------
# Finding #5: hedge reduce orders must never be sized past the actual
# current position — hedger.hedge_inventory/_hedge_kalshi accept
# max_contracts and clamp; the pilot passes its own tracked position size.
# Fail-before: count = max(1, int(size / touch)) had no ceiling, so a large
# dollar excess against a stale/moved touch price could compute a count
# exceeding actual holdings, flipping a "reduce" into the opposite side
# while reducing=True bypassed inventory caps entirely.
# ---------------------------------------------------------------------------

class TestHedgeSizeClamp:
    def test_hedge_on_fill_passes_actual_position_as_max_contracts(
            self, pilot_env, clock, monkeypatch):
        # Canary quote-size cap (default $2) would otherwise halt on a $10
        # fill before the hedge decision even runs — bump it out of the way
        # like TestHedgeLatency does, since canary sizing isn't what this
        # test is about.
        monkeypatch.setattr(live_config(), "MM_CANARY_QUOTE_SIZE_USD", 100.0)
        client = FakeKalshiClient()
        hedger = RecordingHedger(result=True)
        pilot = build_pilot(clock, client=client, hedger=hedger)
        oid = pilot.place_pilot_order(TICKER, "yes", "buy", 20, 0.50,
                                      purpose="quote_bid")
        # 20 contracts @ 0.50 = $10 net; well past the deadband -> hedges.
        client.fills_script = [kfill(oid, count=20, yes_price=50)]
        pilot.poll_fills()
        assert hedger.calls  # hedge fired
        call = hedger.calls[-1]
        assert call["max_contracts"] == 20  # abs(net_contracts), not a guess


class TestHedgeKalshiContractClamp:
    """Direct hedger.py unit coverage for the clamp itself (complements the
    mm_pilot integration test above, which only proves wiring)."""

    @staticmethod
    def _book(yes_bid=0.30, no_bid=0.68):
        return {"orderbook_fp": {
            "yes_dollars": [[f"{yes_bid:.4f}", "500.00"]],
            "no_dollars": [[f"{no_bid:.4f}", "500.00"]],
        }}

    def test_count_clamped_to_max_contracts_when_price_moved(self):
        from hedger import PartialFillHedger
        mock_kalshi = __import__("unittest.mock", fromlist=["MagicMock"]).MagicMock()
        mock_kalshi.fetch_order_book.return_value = self._book()
        mock_kalshi.place_order.return_value = {"order_id": "k_1"}
        hedger = PartialFillHedger(kalshi_client=mock_kalshi)
        # size=$20 excess / touch=$0.30 -> naive count=66; actual position
        # is only 5 contracts (price moved a long way since entry).
        result = hedger._hedge_kalshi("TICK", fill_price=0.50, size=20.0,
                                      max_loss=1.0, side="yes", action="sell",
                                      max_contracts=5)
        assert result is True
        call = mock_kalshi.place_order.call_args
        assert call[1]["count"] == 5  # clamped, never the naive 66

    def test_zero_max_contracts_places_nothing(self):
        from hedger import PartialFillHedger
        mock_kalshi = __import__("unittest.mock", fromlist=["MagicMock"]).MagicMock()
        mock_kalshi.fetch_order_book.return_value = self._book()
        hedger = PartialFillHedger(kalshi_client=mock_kalshi)
        result = hedger._hedge_kalshi("TICK", fill_price=0.50, size=20.0,
                                      max_loss=1.0, side="yes", action="sell",
                                      max_contracts=0)
        assert result is False
        mock_kalshi.place_order.assert_not_called()

    def test_none_max_contracts_preserves_unclamped_legacy_behavior(self):
        from hedger import PartialFillHedger
        mock_kalshi = __import__("unittest.mock", fromlist=["MagicMock"]).MagicMock()
        mock_kalshi.fetch_order_book.return_value = self._book()
        mock_kalshi.place_order.return_value = {"order_id": "k_1"}
        hedger = PartialFillHedger(kalshi_client=mock_kalshi)
        result = hedger._hedge_kalshi("TICK", fill_price=0.50, size=20.0,
                                      max_loss=1.0, side="yes", action="sell")
        assert result is True
        call = mock_kalshi.place_order.call_args
        assert call[1]["count"] == 66  # int(20 / 0.30), unclamped


# ---------------------------------------------------------------------------
# Finding #10: hedge-latency and hedge-order aging use the monotonic clock,
# not wall time — a frozen/mocked _time_fn (or a real NTP step) must not
# defeat or false-trigger MM_HEDGE_MAX_LATENCY_SECONDS.
# Fail-before: latency = self._time_fn() - event.created_ts, and
# _check_pending_hedges aged orders off wall-clock `placed_at`.
# ---------------------------------------------------------------------------

class TestMonotonicHedgeLatency:
    def test_frozen_wall_clock_does_not_hide_processing_latency(
            self, pilot_env, clock, monkeypatch):
        """If _time_fn() were still used for the reaction-time component, a
        wall clock that never advances during hedge attempts would always
        read latency=0 no matter how much monotonic time passed.

        ``mono`` is an independent counter the fake hedger advances itself
        (rather than counting _mono_fn() invocations, which is fragile —
        place_pilot_order also reads the monotonic clock for its own
        ``placed_mono`` bookkeeping). This ties the simulated 15s directly
        to "the hedge attempt took 15 real seconds", which is exactly the
        scenario the monotonic-latency fix must catch.
        """
        monkeypatch.setattr(live_config(), "MM_CANARY_QUOTE_SIZE_USD", 100.0)
        client = FakeKalshiClient()
        mono = [clock[0]]  # independent monotonic counter

        class SlowHedger:
            def hedge_inventory(self, **kwargs):
                # The hedge attempt itself burns 15s of monotonic time
                # while the wall clock (clock[0]) never moves at all.
                mono[0] += 15
                return True

        pilot = build_pilot(clock, client=client, hedger=SlowHedger())
        monkeypatch.setattr(pilot, "_mono_fn", lambda: mono[0])
        oid = pilot.place_pilot_order(TICKER, "yes", "buy", 20, 0.50,
                                      purpose="quote_bid")
        # Fill reported as happening right now — zero wall-clock detection
        # lag, isolating the assertion to the monotonic reaction-time term.
        client.fills_script = [kfill(oid, count=20, yes_price=50,
                                     created=clock[0])]
        pilot.poll_fills()
        # 15s of monotonic reaction latency exceeds the 10s ceiling even
        # though the wall clock never moved and the fill was "fresh".
        assert pilot.halted is True
        assert "latency" in pilot.halt_reason

    def test_pending_hedge_ages_on_monotonic_placed_time(self, pilot_env,
                                                          clock):
        client = FakeKalshiClient()
        pilot = build_pilot(clock, client=client)
        pilot.inventory.apply_fill(TICKER, "yes", "buy", 10, 0.49)
        hoid = pilot.place_pilot_order(TICKER, "yes", "sell", 10, 0.49,
                                       purpose="hedge", reducing=True)
        assert pilot._orders[hoid]["placed_mono"] == pytest.approx(clock[0])
        clock[0] += 11  # advances both time_fn and mono_fn (shared fixture)
        pilot.poll_fills()
        assert pilot.halted is True
        assert hoid in client.cancelled


# ---------------------------------------------------------------------------
# Finding #4 support: PilotStateStore / _persist_state on a REAL file.
# The reconciliation tests above use state_path=None throughout (no disk
# I/O) and cover reconcile()'s logic against a mocked venue; these cover
# the persistence mechanism itself, which reconcile() falls back on for
# last_fill_ts / realized P&L when a live query can't supply it.
# Fail-before: _persist_state() was called from four call sites in the
# uncommitted diff but was never defined (AttributeError at runtime on
# every cancel/fill/order-placement in live mode).
# ---------------------------------------------------------------------------

class TestPilotStatePersistence:
    def test_save_then_load_roundtrip(self, tmp_path):
        from mm_pilot import PilotStateStore
        path = str(tmp_path / "state.json")
        store = PilotStateStore(path)
        store.save({"last_fill_ts": 123.0, "orders": {"o1": {"ticker": TICKER}}})
        loaded = store.load()
        assert loaded == {"last_fill_ts": 123.0, "orders": {"o1": {"ticker": TICKER}}}

    def test_load_missing_file_returns_none(self, tmp_path):
        from mm_pilot import PilotStateStore
        store = PilotStateStore(str(tmp_path / "does_not_exist.json"))
        assert store.load() is None

    def test_load_corrupted_file_raises(self, tmp_path):
        from mm_pilot import PilotStateStore
        path = tmp_path / "state.json"
        path.write_text("{not valid json")
        store = PilotStateStore(str(path))
        with pytest.raises(json.JSONDecodeError):
            store.load()

    def test_reconcile_tolerates_corrupted_persisted_file(self, pilot_env,
                                                           clock, tmp_path):
        """A corrupted local cache must not block reconciliation as long as
        the live venue queries still succeed — the file is a fallback, not
        the authority."""
        from mm_pilot import ControlsPoller, KalshiMMPilot
        path = tmp_path / "state.json"
        path.write_text("{not valid json")
        def time_fn():
            return clock[0]
        controls = ControlsPoller(time_fn=time_fn)
        controls.set_cached(True)
        client = FakeKalshiClient()
        pilot = KalshiMMPilot(
            kalshi_client=client, controls=controls,
            volatility_tracker=VolatilityTracker(min_samples=1),
            time_fn=time_fn, mono_fn=time_fn, state_path=str(path),
            dry_run=False,
        )
        assert pilot.reconcile() is True

    def test_place_cancel_and_fill_persist_real_state_to_disk(
            self, pilot_env, clock, tmp_path):
        """End-to-end: a live (non-None state_path) pilot actually writes a
        loadable state file across the placement/cancel/fill lifecycle,
        proving _persist_state's four call sites are wired to a real,
        working implementation (not just silently no-op'd)."""
        from mm_pilot import ControlsPoller, KalshiMMPilot, PilotStateStore
        path = tmp_path / "state.json"
        def time_fn():
            return clock[0]
        controls = ControlsPoller(time_fn=time_fn)
        controls.set_cached(True)
        client = FakeKalshiClient()
        pilot = KalshiMMPilot(
            kalshi_client=client, controls=controls,
            volatility_tracker=VolatilityTracker(min_samples=1),
            time_fn=time_fn, mono_fn=time_fn, state_path=str(path),
            dry_run=False,
        )
        pilot._reconciled = True  # bypass live reconciliation for this test
        oid = pilot.place_pilot_order(TICKER, "yes", "buy", 4, 0.49,
                                      purpose="quote_bid")
        assert path.exists()
        state = PilotStateStore(str(path)).load()
        assert oid in state["orders"]

        pilot._cancel_order(oid)
        state = PilotStateStore(str(path)).load()
        assert oid not in state["orders"]

        fill_oid = pilot.place_pilot_order(
            TICKER, "yes", "buy", 4, 0.49, purpose="quote_bid")
        assert fill_oid is not None
        client.fills_script = [kfill(
            fill_oid, count=4, created=clock[0], trade_id="persisted_fill")]
        events = pilot.poll_fills()
        assert len(events) == 1
        state = PilotStateStore(str(path)).load()
        assert state["inventory"]["net"][TICKER] == 4
        assert state["last_fill_ts"] == pytest.approx(clock[0])
        assert fill_oid not in state["orders"]


# ---------------------------------------------------------------------------
# CodeRabbit round-3 finding: place_pilot_order must not hold self._lock for
# the duration of the live venue round-trip — a different thread (the WS
# feed handler calling on_ws_price) touches disjoint state (self._books)
# and must not be blocked for as long as a slow/stuck placement call takes.
# threading.RLock is reentrant, so a same-thread nested call can't detect
# this regression — the test needs a genuine second thread.
# Fail-before: the entire method (auth through registry write) ran inside
# one `with self._lock:` block, including the network call itself.
# ---------------------------------------------------------------------------

class TestLockNotHeldDuringNetworkCall:
    def test_on_ws_price_proceeds_while_placement_network_call_in_flight(
            self, pilot_env, clock):
        import threading

        entered_network_call = threading.Event()
        release_network_call = threading.Event()

        class SlowClient(FakeKalshiClient):
            def place_order(self, *a, **kw):
                entered_network_call.set()
                # Blocks here until the test explicitly releases it —
                # simulates a slow/stuck venue round-trip.
                release_network_call.wait(timeout=5)
                return super().place_order(*a, **kw)

        slow_client = SlowClient()
        pilot = build_pilot(clock, client=slow_client)

        result: dict = {}

        def placer():
            result["oid"] = pilot.place_pilot_order(
                TICKER, "yes", "buy", 4, 0.49, purpose="quote_bid")

        placing_thread = threading.Thread(target=placer)
        placing_thread.start()
        try:
            assert entered_network_call.wait(timeout=2), (
                "placement never reached the (fake) network call")

            # While that "network call" is in flight on the other thread,
            # on_ws_price must be able to proceed without waiting on
            # self._lock — if the fix regressed, this blocks until the
            # network call above is released (5s), and the timeout below
            # fires first.
            ws_updated = threading.Event()

            def ws_updater():
                pilot.on_ws_price(TICKER, 0.55)
                ws_updated.set()

            ws_thread = threading.Thread(target=ws_updater)
            ws_thread.start()
            try:
                assert ws_updated.wait(timeout=1), (
                    "on_ws_price blocked while place_pilot_order's network "
                    "call was in flight — the lock is still held for the "
                    "duration of the venue round-trip")
            finally:
                ws_thread.join(timeout=5)
        finally:
            release_network_call.set()
            placing_thread.join(timeout=5)

        assert result["oid"] is not None
        assert pilot._book(TICKER)["mid"] == pytest.approx(0.55)


# ---------------------------------------------------------------------------
# CodeRabbit round-4 safety regressions
# ---------------------------------------------------------------------------

class TestInventoryDerivedReduction:
    def test_reducing_flag_without_inventory_is_rejected(self, pilot_env,
                                                          clock):
        pilot = build_pilot(clock, client=FakeKalshiClient())

        result = pilot.authorize_order(
            TICKER, "yes", "sell", 5, 0.49, reducing=True)

        assert result.allowed is False
        assert result.reason == "not_reducing"

    def test_reducing_order_is_capped_at_held_contracts(self, pilot_env,
                                                        clock):
        client = FakeKalshiClient()
        pilot = build_pilot(clock, client=client)
        pilot.inventory.apply_fill(TICKER, "yes", "buy", 5, 0.49)

        oid = pilot.place_pilot_order(
            TICKER, "yes", "sell", 50, 0.49,
            purpose="hedge", reducing=True,
        )

        assert oid is not None
        assert client.placed[-1]["count"] == 5
        assert pilot._orders[oid]["count"] == 5

    def test_derived_reduction_reaches_client_policy_boundary(self, pilot_env,
                                                               clock):
        blocked_ticker = "KXNFLGAME-26AUG18"
        client = FakeKalshiClient(books={blocked_ticker: make_book()})
        pilot = build_pilot(clock, client=client)
        pilot.inventory.apply_fill(blocked_ticker, "yes", "buy", 5, 0.49)

        oid = pilot.place_pilot_order(
            blocked_ticker, "yes", "sell", 5, 0.49,
            purpose="hedge",
        )

        assert oid is not None
        assert client.placed[-1]["reducing"] is True


class TestIndeterminatePlacement:
    @pytest.mark.parametrize("response", [{}, {"order": {}}])
    def test_missing_order_identity_halts_and_requires_reconciliation(
            self, pilot_env, clock, response, monkeypatch):
        client = FakeKalshiClient()
        pilot = build_pilot(clock, client=client)
        monkeypatch.setattr(client, "place_order", lambda **kwargs: response)

        oid = pilot.place_pilot_order(
            TICKER, "yes", "buy", 4, 0.49, purpose="quote_bid")

        assert oid is None
        assert pilot.halted is True
        assert pilot._reconciled is False
        assert "indeterminate order placement" in pilot.halt_reason

    def test_confirmed_rejection_does_not_require_reconciliation(
            self, pilot_env, clock, monkeypatch):
        client = FakeKalshiClient()
        pilot = build_pilot(clock, client=client)
        monkeypatch.setattr(client, "place_order", lambda **kwargs: None)

        oid = pilot.place_pilot_order(
            TICKER, "yes", "buy", 4, 0.49, purpose="quote_bid")

        assert oid is None
        assert pilot.halted is False
        assert pilot._reconciled is True
        assert pilot._pending_submissions == {}

    def test_placement_exception_takes_same_fail_closed_path(
            self, pilot_env, clock, monkeypatch):
        client = FakeKalshiClient()
        pilot = build_pilot(clock, client=client)

        def raise_timeout(**kwargs):
            raise TimeoutError("venue response lost")

        monkeypatch.setattr(client, "place_order", raise_timeout)

        assert pilot.place_pilot_order(
            TICKER, "yes", "buy", 4, 0.49,
            purpose="quote_bid") is None
        assert pilot.halted is True
        assert pilot._reconciled is False

    def test_pending_client_id_is_durable_before_live_post(
            self, pilot_env, clock, tmp_path, monkeypatch):
        from mm_pilot import PilotStateStore

        client = FakeKalshiClient()
        pilot = build_pilot(clock, client=client)
        state_path = tmp_path / "pilot-state.json"
        pilot._state_store = PilotStateStore(str(state_path))
        captured: dict = {}

        def raise_timeout(**kwargs):
            captured.update(kwargs)
            persisted = PilotStateStore(str(state_path)).load()
            assert kwargs["client_order_id"] in persisted["pending_submissions"]
            raise TimeoutError("venue response lost")

        monkeypatch.setattr(client, "place_order", raise_timeout)

        assert pilot.place_pilot_order(
            TICKER, "yes", "buy", 4, 0.49,
            purpose="quote_bid") is None

        client_order_id = captured["client_order_id"]
        persisted = PilotStateStore(str(state_path)).load()
        assert client_order_id in persisted["pending_submissions"]
        assert pilot._pending_submissions[client_order_id]["ticker"] == TICKER
        assert pilot._reconciled is False


class TestCancelReplaceSafety:
    def test_failed_cancel_prevents_replacement_quotes(self, pilot_env, clock):
        client = FakeKalshiClient()
        pilot = build_pilot(clock, client=client)
        old_oid = pilot.place_pilot_order(
            TICKER, "yes", "buy", 4, 0.49, purpose="quote_bid")
        placements_before = client.place_order_calls
        client.cancel_fail_count = 1

        assert pilot.refresh_market(TICKER) == []
        assert client.place_order_calls == placements_before
        assert old_oid in pilot._orders
        assert pilot._orders[old_oid]["pending_cancel"] is True


class TestHaltedFillAccounting:
    def test_known_fill_is_accounted_while_halted(self, pilot_env, clock):
        client = FakeKalshiClient()
        pilot = build_pilot(clock, client=client)
        oid = pilot.place_pilot_order(
            TICKER, "yes", "buy", 4, 0.49, purpose="quote_bid")
        client.cancel_fail_count = 99
        pilot.halt_all("test halt with live order")
        client.fills_script = [kfill(
            oid, count=4, yes_price=49, trade_id="halted_fill",
            created=clock[0],
        )]

        events = pilot.poll_fills()

        assert [event.fill_id for event in events] == ["halted_fill"]
        assert pilot.inventory.net_contracts(TICKER) == 4

    def test_unparseable_known_fill_requires_reconciliation(
            self, pilot_env, clock):
        client = FakeKalshiClient()
        pilot = build_pilot(clock, client=client)
        oid = pilot.place_pilot_order(
            TICKER, "yes", "buy", 4, 0.49, purpose="quote_bid")
        bad_fill = kfill(oid, trade_id="bad_fill", created=clock[0])
        bad_fill.pop("yes_price")
        client.fills_script = [bad_fill]

        assert pilot.poll_fills() == []
        assert pilot.halted is True
        assert pilot._reconciled is False
        assert "bad_fill" not in pilot._seen_fill_ids
        assert pilot.inventory.net_contracts(TICKER) == 0


class TestToxicityFailClosed:
    def test_record_failure_halts_affected_market(self, pilot_env, clock):
        class RaisingToxic(ToxicFlowDetector):
            def record_fill(self, *args, **kwargs):
                raise RuntimeError("toxicity store unavailable")

        client = FakeKalshiClient()
        pilot = build_pilot(clock, client=client, detector=RaisingToxic())
        oid = pilot.place_pilot_order(
            TICKER, "yes", "buy", 4, 0.49, purpose="quote_bid")
        client.fills_script = [kfill(oid, created=clock[0])]

        pilot.poll_fills()

        assert TICKER in pilot._market_halted
        assert "toxicity accounting failed" in pilot._market_halted[TICKER]


class TestShutdownCancellation:
    def test_stop_without_client_confirms_when_no_orders_remain(self, clock):
        pilot = build_pilot(clock, client=None, dry_run=False)
        pilot._alert = MagicMock()

        pilot.stop()

        pilot._alert.assert_not_called()

    def test_stop_retries_until_venue_confirms_no_orders(self, pilot_env,
                                                         clock):
        client = FakeKalshiClient()
        pilot = build_pilot(clock, client=client)
        oid = pilot.place_pilot_order(
            TICKER, "yes", "buy", 4, 0.49, purpose="quote_bid")
        client.open_orders_script = [{"order_id": oid, "ticker": TICKER}]
        client.cancel_fail_count = 2

        pilot.stop()

        assert client.cancel_order_calls >= 3
        assert client.get_open_orders() == []
        assert pilot.resting_orders() == []
