"""Regression tests for lock re-entry deadlocks found in audit #77."""

from __future__ import annotations

from datetime import datetime, timedelta
import threading
from typing import Callable, TypeVar

import capital_optimizer
import latency_monitor


T = TypeVar("T")


def _assert_completes(call: Callable[[], T], timeout: float = 0.5) -> T:
    """Run a call without allowing a lock regression to hang pytest."""
    outcome: dict[str, object] = {}

    def target() -> None:
        try:
            outcome["value"] = call()
        except BaseException as exc:  # pragma: no cover - re-raised below
            outcome["error"] = exc

    thread = threading.Thread(target=target, daemon=True)
    thread.start()
    thread.join(timeout)

    assert not thread.is_alive(), "call deadlocked while re-entering its instance lock"
    if "error" in outcome:
        raise outcome["error"]  # type: ignore[misc]
    return outcome["value"]  # type: ignore[return-value]


class TestDeadlockRegressions:
    def test_tax_harvest_candidates_does_not_deadlock(self, monkeypatch) -> None:
        monkeypatch.setattr(capital_optimizer, "TAX_AWARE_ENABLED", True)
        optimizer = capital_optimizer.TaxOptimizer()
        optimizer.record_entry(
            "old-loss",
            "kalshi",
            cost=100.0,
            quantity=100.0,
            entry_time=datetime.now() - timedelta(days=400),
        )

        candidates = _assert_completes(
            lambda: optimizer.get_harvest_candidates({"old-loss": 50.0}, threshold=0.1)
        )

        assert candidates == [
            ("old-loss", -50.0, 50.0 * capital_optimizer.TAX_LONG_TERM_RATE)
        ]

    def test_region_router_external_route_check_does_not_deadlock(self, monkeypatch) -> None:
        monkeypatch.setattr(latency_monitor, "GEOGRAPHIC_LATENCY_ENABLED", True)
        router = latency_monitor.RegionRouter(local_region="us-east")
        router.record_region_latency("kalshi", "us-east", 100.0)
        router.record_region_latency("kalshi", "us-west", 50.0)

        should_route = _assert_completes(
            lambda: router.should_route_externally("kalshi")
        )

        assert should_route is True
