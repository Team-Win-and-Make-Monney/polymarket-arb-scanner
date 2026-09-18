"""Tests for broker/queue.py — append-only intent queue + halt ledger.

DoD item 1: append-only, idempotency-key dedupe (repeat = no-op).
"""

import os
import sqlite3
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from broker.queue import (
    HALT_SCOPE_ALL,
    HALT_SCOPE_CAPITAL,
    STATUS_EXECUTED,
    STATUS_PENDING,
    STATUS_REJECTED,
    Intent,
    IntentError,
    IntentQueue,
)


@pytest.fixture
def queue():
    return IntentQueue(":memory:")


def flip(key="k1"):
    return Intent("flip_lane", {"lane": "kalshi-lip", "venue": "kalshi",
                                "action": "enable"}, key)


# ---------------------------------------------------------------------------
# Intent shape validation
# ---------------------------------------------------------------------------

class TestIntent:
    def test_valid(self):
        assert flip().intent_type == "flip_lane"

    def test_unknown_type_rejected(self):
        with pytest.raises(IntentError, match="intent_type"):
            Intent("widen_caps", {}, "k")

    def test_missing_idempotency_key_rejected(self):
        with pytest.raises(IntentError, match="idempotency_key"):
            Intent("flip_lane", {}, "")

    def test_non_dict_payload_rejected(self):
        with pytest.raises(IntentError, match="payload"):
            Intent("flip_lane", "not-a-dict", "k")

    def test_nan_in_payload_rejected(self):
        with pytest.raises(IntentError, match="finite"):
            Intent("move_capital", {"amount_usd": float("nan")}, "k")

    def test_unserializable_payload_rejected(self):
        with pytest.raises(IntentError, match="JSON-serializable"):
            Intent("flip_lane", {"lanes": {"a", "b"}}, "k")


# ---------------------------------------------------------------------------
# Submit + idempotency dedupe
# ---------------------------------------------------------------------------

class TestSubmit:
    def test_submit_returns_id_and_created(self, queue):
        intent_id, created = queue.submit(flip())
        assert intent_id >= 1
        assert created is True

    def test_duplicate_key_is_noop(self, queue):
        id1, created1 = queue.submit(flip("same-key"))
        id2, created2 = queue.submit(flip("same-key"))
        assert (created1, created2) == (True, False)
        assert id1 == id2
        count = queue.conn.execute("SELECT COUNT(*) c FROM intents").fetchone()["c"]
        assert count == 1

    def test_distinct_keys_create_distinct_rows(self, queue):
        id1, _ = queue.submit(flip("a"))
        id2, _ = queue.submit(flip("b"))
        assert id1 != id2

    def test_key_reuse_with_different_intent_rejected(self, queue):
        queue.submit(flip("same-key"))
        smuggled = Intent("move_capital",
                          {"amount_usd": 500.0, "from_venue": "kalshi",
                           "to_venue": "polymarket"}, "same-key")
        with pytest.raises(IntentError, match="reused"):
            queue.submit(smuggled)

    def test_key_reuse_with_different_payload_rejected(self, queue):
        queue.submit(flip("same-key"))
        altered = Intent("flip_lane", {"lane": "OTHER", "venue": "kalshi",
                                       "action": "enable"}, "same-key")
        with pytest.raises(IntentError, match="reused"):
            queue.submit(altered)

    def test_event_for_unknown_intent_rejected(self, queue):
        # PRAGMA foreign_keys=ON — events must reference a real intent.
        with pytest.raises(sqlite3.IntegrityError):
            queue.append_event(9999, STATUS_REJECTED, "orphan")


# ---------------------------------------------------------------------------
# Event-sourced status
# ---------------------------------------------------------------------------

class TestStatus:
    def test_defaults_to_pending(self, queue):
        intent_id, _ = queue.submit(flip())
        assert queue.current_status(intent_id) == STATUS_PENDING

    def test_terminal_status_is_write_once(self, queue):
        intent_id, _ = queue.submit(flip())
        queue.append_event(intent_id, STATUS_REJECTED, "caps")
        # A second terminal event is refused — the outcome is write-once, so a
        # duplicate always reads back the original decision.
        with pytest.raises(IntentError, match="write-once"):
            queue.append_event(intent_id, STATUS_EXECUTED, "operator override rerun")
        assert queue.current_status(intent_id) == STATUS_REJECTED
        assert queue.last_reason(intent_id) == "caps"

    def test_invalid_status_rejected(self, queue):
        intent_id, _ = queue.submit(flip())
        with pytest.raises(IntentError, match="invalid status"):
            queue.append_event(intent_id, "MAYBE")


# ---------------------------------------------------------------------------
# Append-only enforcement — in the DB itself, not Python discipline
# ---------------------------------------------------------------------------

class TestAppendOnly:
    def test_update_intents_blocked(self, queue):
        queue.submit(flip())
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            queue.conn.execute("UPDATE intents SET payload = '{}'")

    def test_delete_intents_blocked(self, queue):
        queue.submit(flip())
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            queue.conn.execute("DELETE FROM intents")

    def test_update_events_blocked(self, queue):
        intent_id, _ = queue.submit(flip())
        queue.append_event(intent_id, STATUS_REJECTED, "x")
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            queue.conn.execute("UPDATE intent_events SET status = 'EXECUTED'")

    def test_delete_halts_blocked(self, queue):
        queue.record_halt(HALT_SCOPE_CAPITAL, "recon break")
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            queue.conn.execute("DELETE FROM halts")


# ---------------------------------------------------------------------------
# Halt ledger
# ---------------------------------------------------------------------------

class TestHalts:
    def test_no_halt_by_default(self, queue):
        assert queue.halt_active(HALT_SCOPE_CAPITAL) is False

    def test_record_then_active(self, queue):
        queue.record_halt(HALT_SCOPE_CAPITAL, "ledger vs venue break")
        assert queue.halt_active(HALT_SCOPE_CAPITAL) is True

    def test_clear_requires_operator(self, queue):
        queue.record_halt(HALT_SCOPE_CAPITAL, "break")
        with pytest.raises(IntentError, match="operator"):
            queue.clear_halt(HALT_SCOPE_CAPITAL, "")
        assert queue.halt_active(HALT_SCOPE_CAPITAL) is True

    def test_operator_clear_deactivates(self, queue):
        queue.record_halt(HALT_SCOPE_CAPITAL, "break")
        queue.clear_halt(HALT_SCOPE_CAPITAL, "jonathon")
        assert queue.halt_active(HALT_SCOPE_CAPITAL) is False

    def test_all_scope_halts_every_scope(self, queue):
        queue.record_halt(HALT_SCOPE_ALL, "global stop")
        assert queue.halt_active(HALT_SCOPE_CAPITAL) is True
        assert queue.halt_active("lane:kalshi-lip") is True

    def test_rehalt_after_clear(self, queue):
        queue.record_halt(HALT_SCOPE_CAPITAL, "break 1")
        queue.clear_halt(HALT_SCOPE_CAPITAL, "jonathon")
        queue.record_halt(HALT_SCOPE_CAPITAL, "break 2")
        assert queue.halt_active(HALT_SCOPE_CAPITAL) is True


# ---------------------------------------------------------------------------
# Submit atomicity across separate connections (two competing processes)
# ---------------------------------------------------------------------------

class TestSubmitCrossConnection:
    def test_second_connection_dedupes_instead_of_erroring(self):
        # Two IntentQueue instances share one DB file. The atomic upsert means
        # the loser of a same-key race gets (existing_id, created=False) — it
        # must never see a raw sqlite3.IntegrityError.
        tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        tmp.close()
        try:
            a = IntentQueue(tmp.name)
            b = IntentQueue(tmp.name)
            id1, c1 = a.submit(flip("race-key"))
            id2, c2 = b.submit(flip("race-key"))
            assert (c1, c2) == (True, False)
            assert id1 == id2
            # Different content under the same key is still rejected.
            altered = Intent("flip_lane", {"lane": "OTHER", "venue": "kalshi",
                                           "action": "enable"}, "race-key")
            with pytest.raises(IntentError, match="reused"):
                b.submit(altered)
            a.conn.close()
            b.conn.close()
        finally:
            os.unlink(tmp.name)


# ---------------------------------------------------------------------------
# Lease atomicity across separate connections (two competing processes)
# ---------------------------------------------------------------------------

class TestLeaseCrossConnection:
    def test_only_one_connection_wins(self):
        tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        tmp.close()
        try:
            a = IntentQueue(tmp.name, lease_name="loop")
            b = IntentQueue(tmp.name, lease_name="loop")
            # A wins the free lease; B (separate connection) must not also win.
            assert a.acquire_lease("A", 60) is True
            assert b.acquire_lease("B", 60) is False
            assert b.lease_holder() == "A"
            # After A releases, B can take it.
            assert a.release_lease("A") is True
            assert b.acquire_lease("B", 60) is True
            a.conn.close()
            b.conn.close()
        finally:
            os.unlink(tmp.name)
