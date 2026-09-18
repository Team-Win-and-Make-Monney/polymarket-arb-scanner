"""Lease-renewing single-writer runtime for the out-of-band policy broker."""

from __future__ import annotations

import argparse
import logging
import math
import os
import signal
import socket
import threading
import time
import uuid
from pathlib import Path

from notifier import WebhookNotifier

from .adapters import JsonAuthoritySource, load_command_executors
from .broker import PolicyBroker
from .policy import load_policy
from .queue import IntentQueue
from .queue import STATUS_IN_DOUBT, STATUS_PENDING
from .supabase_queue import SupabaseIntentQueue

logger = logging.getLogger(__name__)


class BrokerWorker:
    """Own the queue lease, reconcile, and drain pending intents deterministically."""

    def __init__(
        self,
        broker: PolicyBroker,
        queue,
        *,
        holder: str,
        lease_ttl_seconds: float = 60.0,
        batch_size: int = 25,
        monotonic=time.monotonic,
    ):
        lease_ttl_seconds = float(lease_ttl_seconds)
        if not math.isfinite(lease_ttl_seconds) or lease_ttl_seconds <= 3:
            raise ValueError("lease_ttl_seconds must be > 3")
        if isinstance(batch_size, bool) or not isinstance(batch_size, int) or batch_size <= 0:
            raise ValueError("batch_size must be a positive integer")
        self.broker = broker
        self.queue = queue
        self.holder = holder
        if not isinstance(holder, str) or not holder.strip():
            raise ValueError("holder must be a non-empty string")
        self.lease_ttl_seconds = lease_ttl_seconds
        self.batch_size = batch_size
        self.monotonic = monotonic
        self._next_renewal = 0.0

    def acquire(self) -> bool:
        acquired = self.queue.acquire_lease(self.holder, self.lease_ttl_seconds)
        if acquired:
            self._next_renewal = self.monotonic() + self.lease_ttl_seconds / 3.0
        return acquired

    def _renew_if_due(self) -> bool:
        if self.monotonic() < self._next_renewal:
            return True
        if not self.queue.renew_lease(self.holder, self.lease_ttl_seconds):
            self.broker.escalate(
                "[broker] CRITICAL: worker lost the single-writer lease; "
                "stopping before any further intent"
            )
            return False
        self._next_renewal = self.monotonic() + self.lease_ttl_seconds / 3.0
        return True

    def run_once(self) -> int:
        """Run one preflight + bounded batch. Caller must hold the lease."""
        if not self._renew_if_due():
            return -1
        reconciliation = self.broker.reconcile_preflight()
        if not reconciliation.ok:
            self.broker.escalate(
                f"[broker] reconciliation preflight failed: {reconciliation.reason}"
            )
            return 0
        processed = 0
        for intent_id, _intent in self.queue.pending_intents(self.batch_size):
            if not self._renew_if_due():
                return -1
            if not self.queue.claim_intent_attempt(intent_id, self.holder):
                reason = (
                    "a prior automatic attempt already claimed this intent; "
                    "refusing to retry an ambiguous consequential action"
                )
                self.broker.escalate(f"[broker] intent {intent_id} IN_DOUBT: {reason}")
                try:
                    self.queue.append_event(intent_id, STATUS_IN_DOUBT, reason)
                except Exception as exc:
                    self.broker.escalate(
                        f"[broker] CRITICAL: could not persist IN_DOUBT for intent "
                        f"{intent_id}: {exc}; stopping worker"
                    )
                    return -1
                continue
            decision = self.broker.process_stored(intent_id)
            if (
                decision.status != STATUS_PENDING
                and self.queue.current_status(intent_id) == STATUS_PENDING
            ):
                self.broker.escalate(
                    f"[broker] CRITICAL: intent {intent_id} reached "
                    f"{decision.status} but the durable ledger is still PENDING; "
                    "stopping before any further intent"
                )
                return -1
            processed += 1
        return processed

    def close(self) -> None:
        try:
            if not self.queue.release_lease(self.holder):
                self.broker.escalate(
                    "[broker] worker lease release was not acknowledged; "
                    "the TTL must expire before another worker proceeds"
                )
        except Exception as exc:
            self.broker.escalate(
                f"[broker] worker lease release failed: {exc}; "
                "the TTL must expire before another worker proceeds"
            )


def _escalator(url: str = ""):
    notifier = WebhookNotifier(url, min_profit=0.0) if url else None

    def escalate(message: str) -> None:
        logger.critical("%s", message)
        if notifier is not None:
            try:
                notifier.notify_text(message)
            except Exception as exc:
                logger.error("Broker escalation delivery failed: %s", exc)

    return escalate


def build_worker(repo_root: Path | None = None) -> BrokerWorker:
    repo_root = (repo_root or Path(__file__).resolve().parents[1]).resolve()
    backend = os.getenv("BROKER_QUEUE_BACKEND", "supabase").strip().lower()
    if backend == "supabase":
        webhook_url = os.getenv("BROKER_WEBHOOK_URL") or os.getenv("WEBHOOK_URL", "")
        if not webhook_url:
            raise RuntimeError(
                "BROKER_WEBHOOK_URL is required for the production Supabase backend"
            )
        queue = SupabaseIntentQueue()
    elif backend == "sqlite":
        webhook_url = os.getenv("BROKER_WEBHOOK_URL") or os.getenv("WEBHOOK_URL", "")
        queue_path = os.getenv("BROKER_SQLITE_PATH")
        if not queue_path:
            raise RuntimeError("BROKER_SQLITE_PATH is required for the sqlite backend")
        queue = IntentQueue(queue_path)
    else:
        raise RuntimeError("BROKER_QUEUE_BACKEND must be supabase|sqlite")

    source_path = os.getenv("BROKER_AUTHORITY_SNAPSHOT_PATH")
    executor_path = os.getenv("BROKER_EXECUTOR_CONFIG_PATH")
    if not source_path or not executor_path:
        raise RuntimeError(
            "BROKER_AUTHORITY_SNAPSHOT_PATH and BROKER_EXECUTOR_CONFIG_PATH are required"
        )
    sources = JsonAuthoritySource(source_path, repo_root).live_sources()
    executors = load_command_executors(executor_path, repo_root)
    broker = PolicyBroker(load_policy(), queue, sources, executors, _escalator(webhook_url))
    holder = os.getenv(
        "BROKER_LEASE_HOLDER",
        f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:8]}",
    )
    return BrokerWorker(
        broker,
        queue,
        holder=holder,
        lease_ttl_seconds=float(os.getenv("BROKER_LEASE_TTL_SECONDS", "60")),
        batch_size=int(os.getenv("BROKER_BATCH_SIZE", "25")),
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the out-of-band policy broker")
    parser.add_argument("--once", action="store_true", help="process one bounded batch and exit")
    args = parser.parse_args()
    logging.basicConfig(
        level=getattr(logging, os.getenv("LOG_LEVEL", "INFO").upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    worker = build_worker()
    if not worker.acquire():
        logger.error("Another policy-broker worker holds the lease")
        return 2

    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_args: stop.set())
    try:
        poll = float(os.getenv("BROKER_POLL_SECONDS", "5"))
        if not math.isfinite(poll) or poll <= 0:
            raise RuntimeError("BROKER_POLL_SECONDS must be finite and > 0")
        while not stop.is_set():
            result = worker.run_once()
            if result < 0:
                return 3
            if args.once:
                return 0
            stop.wait(poll)
    finally:
        worker.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
