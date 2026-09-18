"""End-to-end tests for broker/broker.py + broker/secrets.py.

Covers: execute path, duplicate no-op (never retry), rejection without
execution, IN_DOUBT semantics, hard-stops, 2FA wall, recon-break halt
propagation, and secret rotation that never logs the value.
"""

import os
import sys
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))

from broker.broker import (
    BrokerDecision,
    ExecutionResult,
    IntentExecutors,
    PolicyBroker,
    TwoFactorWallError,
)
from broker.queue import (
    HALT_SCOPE_CAPITAL,
    STATUS_EXECUTED,
    STATUS_HARD_STOP,
    STATUS_IN_DOUBT,
    STATUS_REJECTED,
    Intent,
    IntentQueue,
)
from broker.secrets import SecretRotationError, rotate_secret_via_stdin
from broker_helpers import healthy_sources, make_policy


def flip_enable(key="f1", lane="kalshi-lip", venue="kalshi"):
    return Intent("flip_lane", {"lane": lane, "venue": venue, "action": "enable"}, key)


def move(key="m1", amount=100.0, **extra):
    payload = {"amount_usd": amount, "from_venue": "kalshi", "to_venue": "polymarket",
               "market": "KXTEST"}
    payload.update(extra)
    return Intent("move_capital", payload, key)


def ok_executors():
    return IntentExecutors(
        flip_lane=MagicMock(return_value=ExecutionResult(True, "flipped")),
        move_capital=MagicMock(return_value=ExecutionResult(True, "moved")),
        rotate_secret=MagicMock(return_value=ExecutionResult(True, "rotated")),
    )


def make_broker(policy=None, sources=None, executors=None, escalations=None):
    queue = IntentQueue(":memory:")
    executors = executors or ok_executors()
    escalate = escalations.append if escalations is not None else None
    broker = PolicyBroker(
        policy or make_policy(), queue, sources or healthy_sources(),
        executors, escalate=escalate,
    )
    return broker, queue, executors


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------

class TestExecutePath:
    def test_valid_flip_executes(self):
        broker, queue, executors = make_broker()
        decision = broker.process(flip_enable())
        assert decision.status == STATUS_EXECUTED
        assert queue.current_status(decision.intent_id) == STATUS_EXECUTED
        executors.flip_lane.assert_called_once()

    def test_flip_enable_carries_micro_entry_directive(self):
        broker, _, _ = make_broker()
        decision = broker.process(flip_enable())
        assert decision.micro_entry["max_first_order_usd"] == 10.0
        assert decision.micro_entry["first_n_fills"] == 5

    def test_valid_move_executes(self):
        broker, _, executors = make_broker()
        assert broker.process(move()).status == STATUS_EXECUTED
        executors.move_capital.assert_called_once()

    def test_status_persistence_failure_after_execute_escalates(self):
        # The side effect already happened; if append_event fails, surface it as
        # CRITICAL rather than leaving the ledger silently PENDING (CR CLI #3).
        escalations = []
        broker, queue, executors = make_broker(escalations=escalations)
        real_append = queue.append_event
        calls = {"n": 0}

        def flaky_append(intent_id, status, reason=""):
            calls["n"] += 1
            if status == STATUS_EXECUTED:
                raise RuntimeError("supabase write failed")
            return real_append(intent_id, status, reason)

        queue.append_event = flaky_append
        decision = broker.process(flip_enable("persist-fail"))
        assert decision.status == STATUS_EXECUTED  # the action DID execute
        executors.flip_lane.assert_called_once()
        assert any("CRITICAL" in e and "persistence failed" in e for e in escalations)


# ---------------------------------------------------------------------------
# Idempotency — repeat = no-op, never retry
# ---------------------------------------------------------------------------

class TestIdempotency:
    def test_duplicate_returns_prior_outcome_without_reexecuting(self):
        broker, _, executors = make_broker()
        first = broker.process(flip_enable("dup-key"))
        second = broker.process(flip_enable("dup-key"))
        assert first.status == STATUS_EXECUTED
        assert second.duplicate is True
        assert second.status == STATUS_EXECUTED
        executors.flip_lane.assert_called_once()

    def test_key_reuse_different_content_returns_decision_not_raise(self):
        escalations = []
        broker, queue, executors = make_broker(escalations=escalations)
        queue.submit(flip_enable("shared-key"))
        smuggled = Intent("move_capital",
                          {"amount_usd": 999, "from_venue": "kalshi",
                           "to_venue": "polymarket"}, "shared-key")
        decision = broker.process(smuggled)
        assert decision.status == STATUS_REJECTED
        assert "reused" in decision.reason
        assert len(escalations) == 1
        executors.move_capital.assert_not_called()

    def test_in_doubt_intent_is_never_retried(self):
        executors = ok_executors()
        executors.move_capital = MagicMock(side_effect=RuntimeError("socket dropped"))
        broker, _, _ = make_broker(executors=executors)
        first = broker.process(move("in-doubt-key"))
        second = broker.process(move("in-doubt-key"))
        assert first.status == STATUS_IN_DOUBT
        assert second.duplicate is True
        assert second.status == STATUS_IN_DOUBT
        executors.move_capital.assert_called_once()


# ---------------------------------------------------------------------------
# Rejection — fail-closed, executor never runs
# ---------------------------------------------------------------------------

class TestRejection:
    def test_disallowed_venue_rejected_without_execution(self):
        escalations = []
        broker, _, executors = make_broker(escalations=escalations)
        decision = broker.process(flip_enable(venue="hyperliquid"))
        assert decision.status == STATUS_REJECTED
        assert "allowlist" in decision.reason
        executors.flip_lane.assert_not_called()
        assert len(escalations) == 1

    def test_recon_break_rejects_move_and_halts_all_capital(self):
        broker, queue, executors = make_broker(
            sources=healthy_sources(
                venue_balances=lambda: {"kalshi": 1.0, "polymarket": 2000.0}),
        )
        decision = broker.process(move("recon-break"))
        assert decision.status == STATUS_REJECTED
        # The broker (not the validator) records the ALL-capital halt.
        assert queue.halt_active(HALT_SCOPE_CAPITAL) is True
        executors.move_capital.assert_not_called()

    def test_recon_break_with_failed_halt_record_escalates_loudly(self):
        # If record_halt itself fails during a confirmed break, the failure must
        # be escalated CRITICAL — never swallowed leaving the capital lane
        # silently unfrozen (Codex R2 finding #4).
        escalations = []
        broker, queue, executors = make_broker(
            escalations=escalations,
            sources=healthy_sources(
                venue_balances=lambda: {"kalshi": 1.0, "polymarket": 2000.0}),
        )
        queue.record_halt = MagicMock(side_effect=RuntimeError("supabase down"))
        decision = broker.process(move("break-halt-fail"))
        assert decision.status == STATUS_REJECTED
        assert any("CRITICAL" in e and "capital halt" in e for e in escalations)
        executors.move_capital.assert_not_called()

    def test_failed_halt_write_freezes_capital_in_process(self):
        # After a confirmed break whose durable halt write FAILS, the in-process
        # guard must block a LATER move even once reconciliation looks healthy
        # again — the DB shows no halt, so only the in-process freeze protects
        # it (Codex R3 findings #1/#3).
        state = {"break": True}

        def venue_balances():
            return {"kalshi": 1.0 if state["break"] else 3000.0, "polymarket": 2000.0}

        broker, queue, executors = make_broker(
            sources=healthy_sources(venue_balances=venue_balances))
        queue.record_halt = MagicMock(side_effect=RuntimeError("db down"))
        first = broker.process(move("m1"))
        assert first.status == STATUS_REJECTED
        assert broker._capital_frozen is True
        state["break"] = False  # reconciliation is now HEALTHY
        second = broker.process(move("m2"))
        assert second.status == STATUS_REJECTED
        assert "frozen in-process" in second.reason
        executors.move_capital.assert_not_called()

    def test_in_doubt_move_with_failed_halt_still_records_in_doubt(self):
        # If the executor raises AND record_halt raises, _in_doubt must still
        # append the IN_DOUBT event, escalate, and freeze — never propagate the
        # halt exception and skip recording (Codex R3 finding #2).
        escalations = []
        executors = ok_executors()
        executors.move_capital = MagicMock(side_effect=RuntimeError("wire dropped"))
        broker, queue, _ = make_broker(executors=executors, escalations=escalations)
        queue.record_halt = MagicMock(side_effect=RuntimeError("db down"))
        decision = broker.process(move("m-doubt-halt-fail"))
        assert decision.status == STATUS_IN_DOUBT
        assert broker._capital_frozen is True
        assert any("CRITICAL" in e and "capital halt" in e for e in escalations)

    def test_capital_halt_blocks_subsequent_healthy_moves(self):
        broker, queue, executors = make_broker()
        queue.record_halt(HALT_SCOPE_CAPITAL, "prior break")
        decision = broker.process(move("post-halt"))
        assert decision.status == STATUS_REJECTED
        assert "halted" in decision.reason
        executors.move_capital.assert_not_called()


# ---------------------------------------------------------------------------
# IN_DOUBT semantics
# ---------------------------------------------------------------------------

class TestInDoubt:
    def test_executor_exception_marks_in_doubt_and_escalates(self):
        escalations = []
        executors = ok_executors()
        executors.flip_lane = MagicMock(side_effect=RuntimeError("timeout mid-flip"))
        broker, _, _ = make_broker(executors=executors, escalations=escalations)
        decision = broker.process(flip_enable())
        assert decision.status == STATUS_IN_DOUBT
        assert "never retry" in decision.reason
        assert len(escalations) == 1

    def test_unverified_result_marks_in_doubt(self):
        executors = ok_executors()
        executors.rotate_secret = MagicMock(
            return_value=ExecutionResult(False, "setter exited 1"))
        broker, _, _ = make_broker(executors=executors)
        intent = Intent("rotate_secret",
                        {"venue": "kalshi", "secret_name": "KALSHI_API_KEY"}, "r1")
        assert broker.process(intent).status == STATUS_IN_DOUBT

    def test_in_doubt_capital_move_halts_all_capital_moves(self):
        executors = ok_executors()
        executors.move_capital = MagicMock(side_effect=RuntimeError("wire dropped"))
        broker, queue, _ = make_broker(executors=executors)
        broker.process(move("m-doubt"))
        assert queue.halt_active(HALT_SCOPE_CAPITAL) is True
        # a subsequent healthy move is blocked until an operator clears
        executors.move_capital.reset_mock()
        assert broker.process(move("m-next")).status == STATUS_REJECTED
        executors.move_capital.assert_not_called()


# ---------------------------------------------------------------------------
# Hard-stops
# ---------------------------------------------------------------------------

class TestHardStops:
    def test_restart_after_kill_halt_hard_stops(self):
        escalations = []
        policy = make_policy(kill_state={"global": False,
                                         "lanes": {"kalshi-lip": True}})
        broker, _, executors = make_broker(policy=policy, escalations=escalations)
        decision = broker.process(flip_enable(lane="kalshi-lip"))
        assert decision.status == STATUS_HARD_STOP
        assert "operator approval" in decision.reason
        executors.flip_lane.assert_not_called()
        assert len(escalations) == 1

    def test_tranche_advance_hard_stops(self):
        broker, _, executors = make_broker()
        decision = broker.process(move("t2", tranche_advance=True))
        assert decision.status == STATUS_HARD_STOP
        executors.move_capital.assert_not_called()

    def test_unknown_flip_action_rejected_not_executed(self):
        broker, _, executors = make_broker()
        intent = Intent("flip_lane",
                        {"lane": "kalshi-lip", "venue": "kalshi", "action": "frobnicate"},
                        "bad-action")
        decision = broker.process(intent)
        assert decision.status == STATUS_REJECTED
        assert "enable|disable" in decision.reason
        executors.flip_lane.assert_not_called()

    def test_move_missing_required_fields_rejected_before_execute(self):
        # move_capital without from_venue/to_venue/market must not reach the
        # executor (Codex finding #6).
        broker, _, executors = make_broker()
        intent = Intent("move_capital", {"amount_usd": 100.0, "venue": "kalshi"}, "bad-move")
        decision = broker.process(intent)
        assert decision.status == STATUS_REJECTED
        assert "requires a non-empty" in decision.reason
        executors.move_capital.assert_not_called()

    def test_rotate_secret_missing_name_rejected_before_execute(self):
        broker, _, executors = make_broker()
        intent = Intent("rotate_secret", {"venue": "kalshi"}, "bad-rotate")
        decision = broker.process(intent)
        assert decision.status == STATUS_REJECTED
        assert "secret_name" in decision.reason
        executors.rotate_secret.assert_not_called()

    def test_two_factor_wall_hard_stops_never_bypassed(self):
        escalations = []
        executors = ok_executors()
        executors.rotate_secret = MagicMock(
            side_effect=TwoFactorWallError("Kalshi asked for TOTP"))
        broker, _, _ = make_broker(executors=executors, escalations=escalations)
        intent = Intent("rotate_secret",
                        {"venue": "kalshi", "secret_name": "K"}, "2fa")
        decision = broker.process(intent)
        assert decision.status == STATUS_HARD_STOP
        assert "never bypassed" in decision.reason
        assert len(escalations) == 1


# ---------------------------------------------------------------------------
# Secret rotation — value never in logs / errors
# ---------------------------------------------------------------------------

class TestSecretRotation:
    SECRET = b"sk-live-EXTREMELY-SECRET-VALUE\n"

    def _proc(self, returncode=0, stdout=b""):
        proc = MagicMock()
        proc.returncode = returncode
        proc.stdout = stdout
        return proc

    def test_success_pipes_value_via_stdin(self):
        with patch("broker.secrets.subprocess.run") as run:
            run.side_effect = [self._proc(0, self.SECRET), self._proc(0)]
            assert rotate_secret_via_stdin(["get"], ["set"]) is True
            set_call = run.call_args_list[1]
            # getter's terminating newline is stripped — it isn't part of the secret
            assert set_call.kwargs["input"] == self.SECRET.rstrip(b"\r\n")

    def test_getter_failure_raises_without_value(self):
        with patch("broker.secrets.subprocess.run") as run:
            run.side_effect = [self._proc(1, b"")]
            with pytest.raises(SecretRotationError) as excinfo:
                rotate_secret_via_stdin(["get"], ["set"])
            assert "EXTREMELY-SECRET" not in str(excinfo.value)

    def test_empty_secret_raises(self):
        with patch("broker.secrets.subprocess.run") as run:
            run.side_effect = [self._proc(0, b"  \n")]
            with pytest.raises(SecretRotationError, match="empty"):
                rotate_secret_via_stdin(["get"], ["set"])

    def test_setter_failure_raises_never_silently_succeeds(self):
        # Spec: a rotation must RAISE on setter failure so the caller marks it
        # IN_DOUBT and escalates — never return a falsey no-op a caller could
        # misread (Codex finding #10). The value never leaks into the error.
        with patch("broker.secrets.subprocess.run") as run:
            run.side_effect = [self._proc(0, self.SECRET), self._proc(1)]
            with pytest.raises(SecretRotationError) as excinfo:
                rotate_secret_via_stdin(["get"], ["set"])
            assert "exited 1" in str(excinfo.value)
            assert "EXTREMELY-SECRET" not in str(excinfo.value)

    def test_getter_timeout_raises_without_value(self):
        with patch("broker.secrets.subprocess.run") as run:
            run.side_effect = __import__("subprocess").TimeoutExpired(
                cmd="get", timeout=1, output=self.SECRET)
            with pytest.raises(SecretRotationError) as excinfo:
                rotate_secret_via_stdin(["get"], ["set"])
            assert "EXTREMELY-SECRET" not in str(excinfo.value)

    def test_getter_not_found_raises_secret_error(self):
        with patch("broker.secrets.subprocess.run") as run:
            run.side_effect = FileNotFoundError("no such command: infisical")
            with pytest.raises(SecretRotationError, match="could not run"):
                rotate_secret_via_stdin(["infisical"], ["gh"])

    def test_value_never_logged(self, caplog):
        with caplog.at_level("DEBUG"):
            with patch("broker.secrets.subprocess.run") as run:
                run.side_effect = [self._proc(0, self.SECRET), self._proc(0)]
                rotate_secret_via_stdin(["infisical"], ["gh"])
        assert "EXTREMELY-SECRET" not in caplog.text
