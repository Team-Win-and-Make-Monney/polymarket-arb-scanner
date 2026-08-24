"""Operational policy-broker worker and out-of-band adapter tests."""

import json
import os
import stat
import sys
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))

from broker.adapters import (
    CommandIntentAdapter,
    JsonAuthoritySource,
    load_command_executors,
)
from broker.broker import ExecutionResult, IntentExecutors, PolicyBroker
from broker.queue import HALT_SCOPE_CAPITAL, STATUS_EXECUTED, Intent, IntentQueue
from broker.worker import BrokerWorker
import broker.worker as worker_module
from broker_helpers import GATE_CONFIG, healthy_sources, make_policy


def _flip(key="worker-flip"):
    return Intent(
        "flip_lane",
        {"lane": "kalshi-lip", "venue": "kalshi", "action": "enable"},
        key,
    )


def _executors():
    return IntentExecutors(
        flip_lane=MagicMock(return_value=ExecutionResult(True, "flipped")),
        move_capital=MagicMock(return_value=ExecutionResult(True, "moved")),
        rotate_secret=MagicMock(return_value=ExecutionResult(True, "rotated")),
    )


def _broker(queue, sources=None, executors=None, escalations=None):
    return PolicyBroker(
        make_policy(),
        queue,
        sources or healthy_sources(),
        executors or _executors(),
        escalate=(escalations if escalations is not None else []).append,
    )


class TestPendingQueue:
    def test_pending_rows_are_trusted_and_terminal_rows_disappear(self):
        queue = IntentQueue(":memory:")
        intent_id, _ = queue.submit(_flip())
        assert queue.pending_intents() == [(intent_id, _flip())]
        stored = queue.get_intent(intent_id)
        assert stored.payload_json == _flip().payload_json
        queue.append_event(intent_id, STATUS_EXECUTED, "done")
        assert queue.pending_intents() == []

    def test_pending_limit_must_be_positive_integer(self):
        queue = IntentQueue(":memory:")
        for value in (0, -1, True, 1.5):
            with pytest.raises(ValueError, match="positive integer"):
                queue.pending_intents(value)


class TestStoredProcessing:
    def test_worker_processes_database_row_once(self):
        queue = IntentQueue(":memory:")
        executors = _executors()
        broker = _broker(queue, executors=executors)
        intent_id, _ = queue.submit(_flip())
        worker = BrokerWorker(broker, queue, holder="worker-a")
        assert worker.acquire() is True
        assert worker.run_once() == 1
        assert queue.current_status(intent_id) == STATUS_EXECUTED
        assert worker.run_once() == 0
        executors.flip_lane.assert_called_once()
        worker.close()

    def test_claimed_pending_intent_becomes_in_doubt_without_execution(self):
        queue = IntentQueue(":memory:")
        executors = _executors()
        broker = _broker(queue, executors=executors)
        intent_id, _ = queue.submit(_flip())
        assert queue.claim_intent_attempt(intent_id, "crashed-worker") is True
        worker = BrokerWorker(broker, queue, holder="replacement-worker")
        assert worker.acquire() is True
        assert worker.run_once() == 0
        assert queue.current_status(intent_id) == "IN_DOUBT"
        executors.flip_lane.assert_not_called()
        worker.close()

    def test_terminal_persistence_failure_stops_worker(self):
        queue = IntentQueue(":memory:")
        intent_id, _ = queue.submit(_flip())
        broker = MagicMock()
        broker.reconcile_preflight.return_value.ok = True
        broker.process_stored.return_value.status = STATUS_EXECUTED
        worker = BrokerWorker(broker, queue, holder="worker-a")
        assert worker.acquire() is True
        assert worker.run_once() == -1
        assert queue.current_status(intent_id) == "PENDING"
        broker.escalate.assert_called_once()

    def test_worker_stops_before_processing_after_lease_loss(self):
        queue = MagicMock()
        queue.acquire_lease.return_value = True
        queue.renew_lease.return_value = False
        broker = MagicMock()
        clock = iter([0.0, 21.0])
        worker = BrokerWorker(
            broker, queue, holder="worker-a", lease_ttl_seconds=60,
            monotonic=lambda: next(clock),
        )
        assert worker.acquire() is True
        assert worker.run_once() == -1
        queue.pending_intents.assert_not_called()
        broker.escalate.assert_called_once()

    def test_worker_rejects_nonfinite_ttl_and_blank_holder(self):
        with pytest.raises(ValueError, match="> 3"):
            BrokerWorker(MagicMock(), MagicMock(), holder="worker", lease_ttl_seconds=float("nan"))
        with pytest.raises(ValueError, match="holder"):
            BrokerWorker(MagicMock(), MagicMock(), holder="  ")

    def test_preflight_break_halts_all_capital(self):
        queue = IntentQueue(":memory:")
        escalations = []
        broker = _broker(
            queue,
            sources=healthy_sources(
                venue_balances=lambda: {"kalshi": 1.0, "polymarket": 2000.0}
            ),
            escalations=escalations,
        )
        worker = BrokerWorker(broker, queue, holder="worker-a")
        assert worker.acquire() is True
        assert worker.run_once() == 0
        assert queue.halt_active(HALT_SCOPE_CAPITAL) is True
        assert any("reconciliation preflight failed" in item for item in escalations)
        worker.close()

    def test_unreadable_reconciliation_stops_before_pending_intents(self):
        queue = MagicMock()
        queue.acquire_lease.return_value = True
        broker = MagicMock()
        broker.reconcile_preflight.return_value.ok = False
        broker.reconcile_preflight.return_value.reason = "unreadable"
        worker = BrokerWorker(broker, queue, holder="worker-a")
        assert worker.acquire() is True
        assert worker.run_once() == 0
        queue.pending_intents.assert_not_called()
        broker.escalate.assert_called_once()


class TestWorkerEntrypoint:
    def test_invalid_poll_still_releases_lease(self, monkeypatch):
        worker = MagicMock()
        worker.acquire.return_value = True
        monkeypatch.setattr(worker_module, "build_worker", lambda: worker)
        monkeypatch.setattr(sys, "argv", ["policy-broker"])
        monkeypatch.setenv("BROKER_POLL_SECONDS", "nan")

        with pytest.raises(RuntimeError, match="finite and > 0"):
            worker_module.main()

        worker.close.assert_called_once()


class TestAuthoritySnapshot:
    def _snapshot(self):
        now = datetime.now(timezone.utc).isoformat()
        return {
            "portfolio_value_usd": 5000.0,
            "realized_pnl_usd": 200.0,
            "ledger_balances": {"kalshi": 3000.0, "polymarket": 2000.0},
            "venue_balances": {"kalshi": 3000.0, "polymarket": 2000.0},
            "gate_config": {"pre_trade": GATE_CONFIG},
            "input_observed_at": {"prices": now, "balances": now},
            "heartbeat_observed_at": {"observer": now},
            "last_flip_at": None,
            "kill_switch_dry_run": True,
            "market_book_depth_usd": {"KXTEST": 5000.0},
        }

    def test_snapshot_is_reread_and_ages_are_computed(self, tmp_path):
        path = tmp_path / "authority.json"
        path.write_text(json.dumps(self._snapshot()), encoding="utf-8")
        sources = JsonAuthoritySource(path, Path(__file__).parents[1]).live_sources()
        assert sources.portfolio_value_usd() == 5000.0
        assert sources.gate_config("pre_trade") == GATE_CONFIG
        assert sources.input_ages_seconds()["prices"] >= 0
        updated = self._snapshot()
        updated["portfolio_value_usd"] = 4999.0
        path.write_text(json.dumps(updated), encoding="utf-8")
        assert sources.portfolio_value_usd() == 4999.0

    def test_in_repo_or_symlinked_snapshot_is_rejected(self, tmp_path):
        repo_root = Path(__file__).parents[1]
        with pytest.raises(RuntimeError, match="outside"):
            JsonAuthoritySource(repo_root / "README.md", repo_root)
        target = tmp_path / "authority.json"
        target.write_text(json.dumps(self._snapshot()), encoding="utf-8")
        link = tmp_path / "authority-link.json"
        link.symlink_to(target)
        with pytest.raises(RuntimeError, match="symlink"):
            JsonAuthoritySource(link, repo_root)

    def test_missing_snapshot_is_reported_as_runtime_error(self, tmp_path):
        with pytest.raises(RuntimeError, match="cannot be resolved"):
            JsonAuthoritySource(
                tmp_path / "missing-authority.json",
                Path(__file__).parents[1],
            )


class TestCommandExecutors:
    def _script(self, tmp_path, body):
        path = tmp_path / "executor"
        path.write_text(f"#!/usr/bin/env python3\n{body}\n", encoding="utf-8")
        path.chmod(path.stat().st_mode | stat.S_IXUSR)
        return path

    def test_verified_contract_executes_without_shell(self, tmp_path):
        script = self._script(
            tmp_path,
            "import json, sys\njson.load(sys.stdin)\nprint(json.dumps({'verified': True, 'detail': 'ok'}))",
        )
        adapter = CommandIntentAdapter([str(script)], 5, Path(__file__).parents[1])
        assert adapter(_flip()) == ExecutionResult(True, "ok")

    def test_malformed_or_nonzero_result_is_unverified(self, tmp_path):
        malformed = self._script(tmp_path, "print('not-json')")
        adapter = CommandIntentAdapter([str(malformed)], 5, Path(__file__).parents[1])
        assert adapter(_flip()).verified is False

    def test_shell_and_in_repo_executables_are_rejected(self):
        repo_root = Path(__file__).parents[1]
        with pytest.raises(RuntimeError, match="shell"):
            CommandIntentAdapter(["/bin/sh", "-c", "true"], 5, repo_root)
        with pytest.raises(RuntimeError, match="outside"):
            CommandIntentAdapter([str(repo_root / "scanner.py")], 5, repo_root)

    @pytest.mark.parametrize("timeout", [float("nan"), float("inf"), "nan"])
    def test_non_finite_timeout_is_rejected(self, tmp_path, timeout):
        executable = self._script(tmp_path, "print('{}')")

        with pytest.raises(RuntimeError, match="finite"):
            CommandIntentAdapter([str(executable)], timeout, Path(__file__).parents[1])

    def test_loader_requires_all_three_adapters(self, tmp_path):
        config = tmp_path / "executors.json"
        config.write_text(json.dumps({}), encoding="utf-8")
        with pytest.raises(RuntimeError, match="flip_lane"):
            load_command_executors(config, Path(__file__).parents[1])


class TestMigrationArtifact:
    def test_migration_contains_server_side_guarantees(self):
        text = (
            Path(__file__).parents[1]
            / "supabase/migrations/0005_policy_broker.sql"
        ).read_text(encoding="utf-8")
        for required in (
            "broker_submit_intent",
            "broker_pending_intents",
            "broker_claim_intent_attempt",
            "broker_acquire_lease",
            "broker_renew_lease",
            "before truncate",
            "terminal_write_once",
            "enable row level security",
        ):
            assert required in text.lower()
