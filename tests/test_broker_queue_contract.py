"""Backend-agnostic contract suite for the policy-broker queue interface.

Runs against the SQLite IntentQueue always, and against a LIVE Supabase
SupabaseIntentQueue when SUPABASE_URL + a service-role key are in the
environment (else that backend is skipped — CI stays green offline).

Every test namespaces its idempotency keys / halt scopes / lease name with a
unique per-test id, so the suite is safe against the append-only, shared
Supabase tables (rows accumulate by design; uniqueness keeps tests correct).
"""

import os
import sys
import time
import uuid

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from broker.queue import STATUS_EXECUTED, STATUS_PENDING, STATUS_REJECTED, Intent, IntentError

_HAS_SUPABASE = bool(
    os.getenv("SUPABASE_URL")
    and (os.getenv("SUPABASE_SERVICE_ROLE_KEY") or os.getenv("SUPABASE_KEY"))
)


def _sqlite_factory(lease_name):
    from broker.queue import IntentQueue
    return IntentQueue(":memory:", lease_name=lease_name)


def _supabase_factory(lease_name):
    from broker.supabase_queue import SupabaseIntentQueue
    return SupabaseIntentQueue(lease_name=lease_name)


_BACKENDS = [pytest.param(_sqlite_factory, id="sqlite")]
if _HAS_SUPABASE:
    _BACKENDS.append(pytest.param(_supabase_factory, id="supabase"))
else:
    _BACKENDS.append(pytest.param(
        _supabase_factory, id="supabase",
        marks=pytest.mark.skip(reason="SUPABASE_URL / service key not set"),
    ))


@pytest.fixture(params=_BACKENDS)
def queue(request):
    # Unique lease name per test → no cross-test collision on the shared table.
    return request.param(f"lease-{uuid.uuid4().hex}")


@pytest.fixture
def pfx():
    """Unique namespace for this test's keys/scopes (append-only safe)."""
    return uuid.uuid4().hex[:12]


def flip(key):
    return Intent("flip_lane",
                  {"lane": "kalshi-lip", "venue": "kalshi", "action": "enable"}, key)


# ---------------------------------------------------------------------------
# Submit + idempotency
# ---------------------------------------------------------------------------

class TestSubmitContract:
    def test_submit_creates(self, queue, pfx):
        intent_id, created = queue.submit(flip(f"{pfx}-a"))
        assert isinstance(intent_id, int) and intent_id >= 1
        assert created is True

    def test_duplicate_key_is_noop(self, queue, pfx):
        id1, c1 = queue.submit(flip(f"{pfx}-dup"))
        id2, c2 = queue.submit(flip(f"{pfx}-dup"))
        assert (c1, c2) == (True, False)
        assert id1 == id2

    def test_duplicate_key_order_independent(self, queue, pfx):
        id1, _ = queue.submit(Intent("flip_lane",
            {"lane": "L", "venue": "kalshi", "action": "enable"}, f"{pfx}-ord"))
        # same content, keys in different order → still a dup (no-op)
        id2, created = queue.submit(Intent("flip_lane",
            {"action": "enable", "venue": "kalshi", "lane": "L"}, f"{pfx}-ord"))
        assert created is False
        assert id1 == id2

    def test_key_reuse_different_content_rejected(self, queue, pfx):
        queue.submit(flip(f"{pfx}-reuse"))
        with pytest.raises(IntentError, match="reused"):
            queue.submit(Intent("move_capital",
                {"amount_usd": 500, "from_venue": "kalshi", "to_venue": "polymarket"},
                f"{pfx}-reuse"))

    def test_payload_frozen_after_construction(self, queue, pfx):
        # The intent deep-freezes its payload at construction: the caller's dict
        # is detached AND the payload is read-only, so neither a caller edit nor
        # an in-place edit of intent.payload can smuggle content.
        raw = {"lane": "kalshi-lip", "venue": "kalshi", "action": "enable"}
        intent = Intent("flip_lane", raw, f"{pfx}-mut")
        raw["lane"] = "SMUGGLED"  # caller mutates their own dict — must not reach us
        with pytest.raises(TypeError):
            intent.payload["venue"] = object()  # frozen payload rejects mutation
        intent_id, created = queue.submit(intent)
        assert created is True
        # A pristine resubmit dedupes against the stored snapshot, proving the
        # caller's mutation never reached the queue.
        id2, created2 = queue.submit(flip(f"{pfx}-mut"))
        assert (id2, created2) == (intent_id, False)

    def test_nested_payload_is_deep_frozen(self, queue, pfx):
        intent = Intent("move_capital",
                        {"amount_usd": 100, "from_venue": "kalshi", "to_venue": "polymarket",
                         "meta": {"note": "x", "tags": ["a"]}}, f"{pfx}-nest")
        with pytest.raises(TypeError):
            intent.payload["meta"]["note"] = "SMUGGLED"  # nested dict is read-only too


# ---------------------------------------------------------------------------
# Event-sourced status
# ---------------------------------------------------------------------------

class TestStatusContract:
    def test_defaults_to_pending(self, queue, pfx):
        intent_id, _ = queue.submit(flip(f"{pfx}-s"))
        assert queue.current_status(intent_id) == STATUS_PENDING

    def test_terminal_status_is_write_once(self, queue, pfx):
        intent_id, _ = queue.submit(flip(f"{pfx}-s2"))
        queue.append_event(intent_id, STATUS_REJECTED, "caps")
        with pytest.raises(IntentError, match="write-once"):
            queue.append_event(intent_id, STATUS_EXECUTED, "ok")
        assert queue.current_status(intent_id) == STATUS_REJECTED
        assert queue.last_reason(intent_id) == "caps"

    def test_invalid_status_rejected(self, queue, pfx):
        intent_id, _ = queue.submit(flip(f"{pfx}-s3"))
        with pytest.raises(IntentError, match="invalid status"):
            queue.append_event(intent_id, "MAYBE")


# ---------------------------------------------------------------------------
# Halt ledger
# ---------------------------------------------------------------------------

class TestHaltContract:
    def test_no_halt_by_default(self, queue, pfx):
        assert queue.halt_active(f"scope-{pfx}") is False

    def test_record_then_active(self, queue, pfx):
        scope = f"scope-{pfx}"
        queue.record_halt(scope, "recon break")
        assert queue.halt_active(scope) is True

    def test_clear_requires_operator(self, queue, pfx):
        scope = f"scope-{pfx}"
        queue.record_halt(scope, "break")
        with pytest.raises(IntentError, match="operator"):
            queue.clear_halt(scope, "")
        assert queue.halt_active(scope) is True

    def test_operator_clear_deactivates(self, queue, pfx):
        scope = f"scope-{pfx}"
        queue.record_halt(scope, "break")
        queue.clear_halt(scope, "jonathon")
        assert queue.halt_active(scope) is False


# ---------------------------------------------------------------------------
# Single-writer lease
# ---------------------------------------------------------------------------

class TestLeaseContract:
    def test_acquire_and_holder(self, queue):
        assert queue.acquire_lease("sessionA", 60) is True
        assert queue.lease_holder() == "sessionA"

    def test_second_holder_blocked_while_held(self, queue):
        queue.acquire_lease("sessionA", 60)
        assert queue.acquire_lease("sessionB", 60) is False
        assert queue.lease_holder() == "sessionA"

    def test_idempotent_reacquire_by_holder(self, queue):
        queue.acquire_lease("sessionA", 60)
        assert queue.acquire_lease("sessionA", 60) is True

    def test_renew_only_by_holder(self, queue):
        queue.acquire_lease("sessionA", 60)
        assert queue.renew_lease("sessionA", 60) is True
        assert queue.renew_lease("sessionB", 60) is False

    def test_release_only_by_holder_then_reacquire(self, queue):
        queue.acquire_lease("sessionA", 60)
        assert queue.release_lease("sessionB") is False
        assert queue.release_lease("sessionA") is True
        assert queue.lease_holder() is None
        assert queue.acquire_lease("sessionB", 60) is True

    def test_expired_lease_is_reclaimable(self, queue):
        # Shortest valid TTL, then wait past it (SQLite's lease clock has
        # whole-second resolution, so the wait must cross a second boundary).
        assert queue.acquire_lease("sessionA", 1) is True
        time.sleep(2.2)
        assert queue.acquire_lease("sessionB", 60) is True
        assert queue.lease_holder() == "sessionB"

    def test_non_finite_ttl_rejected(self, queue):
        # NaN expiry would make every lease comparison fail-open.
        with pytest.raises(IntentError, match="finite"):
            queue.acquire_lease("sessionA", float("nan"))
        with pytest.raises(IntentError, match="finite"):
            queue.renew_lease("sessionA", float("inf"))

    def test_nonpositive_ttl_rejected(self, queue):
        # A zero/negative TTL would grant a lease already expired — silently
        # defeating the single-writer discipline.
        with pytest.raises(IntentError, match="> 0"):
            queue.acquire_lease("sessionA", 0)
        with pytest.raises(IntentError, match="> 0"):
            queue.acquire_lease("sessionA", -1)
        with pytest.raises(IntentError, match="> 0"):
            queue.renew_lease("sessionA", -60)
