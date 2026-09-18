"""Append-only intent queue + halt ledger for the policy broker.

SQLite-backed (WAL + thread lock, same pattern as db.TradeDB). Append-only is
enforced in the database itself: RAISE(ABORT) triggers block every UPDATE and
DELETE, so status changes are appended events and history can never be
rewritten. Idempotency-key dedupe makes a repeated submit a no-op.
"""

import json
import logging
import math
import sqlite3
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from types import MappingProxyType

logger = logging.getLogger(__name__)


def _deep_freeze(value):
    """Recursively make a JSON value read-only so a held Intent's payload cannot
    be mutated after construction — every downstream reader sees exactly the
    content that was validated, serialized, and deduped."""
    if isinstance(value, dict):
        return MappingProxyType({k: _deep_freeze(v) for k, v in value.items()})
    if isinstance(value, list):
        return tuple(_deep_freeze(v) for v in value)
    return value

# ---------------------------------------------------------------------------
# Intent
# ---------------------------------------------------------------------------

VALID_INTENT_TYPES = frozenset(["flip_lane", "move_capital", "rotate_secret"])

STATUS_PENDING = "PENDING"
STATUS_EXECUTED = "EXECUTED"
STATUS_REJECTED = "REJECTED"
STATUS_IN_DOUBT = "IN_DOUBT"
STATUS_HARD_STOP = "HARD_STOP"

VALID_STATUSES = frozenset([
    STATUS_PENDING, STATUS_EXECUTED, STATUS_REJECTED,
    STATUS_IN_DOUBT, STATUS_HARD_STOP,
])

# Once an intent reaches any of these, its outcome is WRITE-ONCE — no further
# event may be appended. This preserves the idempotency guarantee: a duplicate
# submit always reads back the original terminal outcome, never a later override.
TERMINAL_STATUSES = frozenset([
    STATUS_EXECUTED, STATUS_REJECTED, STATUS_IN_DOUBT, STATUS_HARD_STOP,
])


class IntentError(ValueError):
    """Malformed intent — rejected before it ever reaches the queue."""


@dataclass(frozen=True)
class Intent:
    intent_type: str
    payload: dict = field(default_factory=dict)
    idempotency_key: str = ""
    # Canonical JSON snapshot of the payload, frozen at construction. Submit
    # paths serialize from THIS, so mutating a dict reference after
    # construction can neither smuggle new content under the validated key
    # nor surface a raw serialization error past the IntentError boundary.
    payload_json: str = field(init=False, default="", compare=False, repr=False)

    def __post_init__(self):
        if self.intent_type not in VALID_INTENT_TYPES:
            raise IntentError(
                f"unknown intent_type {self.intent_type!r} "
                f"(valid: {sorted(VALID_INTENT_TYPES)})"
            )
        if not isinstance(self.payload, dict):
            raise IntentError("payload must be a dict")
        if not self.idempotency_key or not isinstance(self.idempotency_key, str):
            raise IntentError("idempotency_key is required on every intent")
        # Fail closed at construction: the payload must round-trip as strict
        # JSON (no NaN/Infinity, no unserializable objects) or dedupe/content
        # comparisons downstream would be unreliable.
        try:
            canonical = json.dumps(self.payload, sort_keys=True, allow_nan=False)
        except (TypeError, ValueError) as exc:
            raise IntentError(
                f"payload must be JSON-serializable with finite numbers: {exc}"
            ) from exc
        # Detach from the caller's dict AND deep-freeze it: a held Intent's
        # payload (including nested dicts/lists) can no longer be mutated, so a
        # validator reading intent.payload always sees the same content that was
        # serialized and deduped via payload_json.
        object.__setattr__(self, "payload", _deep_freeze(json.loads(canonical)))
        object.__setattr__(self, "payload_json", canonical)


# ---------------------------------------------------------------------------
# Queue
# ---------------------------------------------------------------------------

HALT_SCOPE_CAPITAL = "capital_moves"
HALT_SCOPE_ALL = "all"

DEFAULT_LEASE_NAME = "policy_broker_loop"

_APPEND_ONLY_TABLES = ("intents", "intent_events", "intent_attempts", "halts")


def _require_finite_ttl(ttl_seconds: float) -> float:
    """Shared lease-TTL guard for every queue backend (single copy — the two
    backends must not drift on this safety check).

    A NaN TTL would make every expiry comparison False (fail-open); a zero or
    negative TTL grants a lease that is expired the instant it is issued,
    silently defeating the single-writer discipline.
    """
    try:
        ttl = float(ttl_seconds)
    except (TypeError, ValueError) as exc:
        raise IntentError(f"lease ttl_seconds must be a number: {ttl_seconds!r}") from exc
    if not math.isfinite(ttl):
        raise IntentError(f"lease ttl_seconds must be finite: {ttl_seconds!r}")
    if ttl <= 0:
        raise IntentError(f"lease ttl_seconds must be > 0: {ttl_seconds!r}")
    return ttl


class IntentQueue:
    """Thread-safe append-only intent queue + halt ledger."""

    def __init__(self, db_path: str = ":memory:", lease_name: str = DEFAULT_LEASE_NAME):
        self._lock = threading.Lock()
        self._lease_name = lease_name
        self.conn = sqlite3.connect(db_path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.execute("PRAGMA journal_mode=WAL")
        self._create_tables()

    def _create_tables(self):
        self.conn.executescript("""
            CREATE TABLE IF NOT EXISTS intents (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                idempotency_key TEXT NOT NULL UNIQUE,
                intent_type TEXT NOT NULL,
                payload TEXT NOT NULL,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS intent_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                intent_id INTEGER NOT NULL REFERENCES intents(id),
                status TEXT NOT NULL,
                reason TEXT,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS intent_attempts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                intent_id INTEGER NOT NULL UNIQUE REFERENCES intents(id),
                holder TEXT NOT NULL,
                claimed_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS halts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                scope TEXT NOT NULL,
                action TEXT NOT NULL CHECK (action IN ('halt', 'clear')),
                reason TEXT,
                operator TEXT,
                created_at TEXT NOT NULL
            );

            -- Lease is deliberately MUTABLE (heartbeat renewal) — not append-only.
            CREATE TABLE IF NOT EXISTS leases (
                name TEXT PRIMARY KEY,
                holder TEXT NOT NULL,
                expires_at REAL NOT NULL
            );
        """)
        # Append-only enforcement lives in the DB, not in Python discipline.
        for table in _APPEND_ONLY_TABLES:
            for op in ("UPDATE", "DELETE"):
                self.conn.execute(
                    f"CREATE TRIGGER IF NOT EXISTS {table}_no_{op.lower()} "
                    f"BEFORE {op} ON {table} "
                    f"BEGIN SELECT RAISE(ABORT, '{table} is append-only'); END"
                )
        self.conn.commit()

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat()

    # -- intents ------------------------------------------------------------

    def submit(self, intent: Intent) -> tuple[int, bool]:
        """Append an intent. Returns (intent_id, created).

        A repeated idempotency key is a no-op: the existing row id is returned
        with created=False and nothing is written. The insert is a single
        atomic upsert, so two connections racing on the same key can never
        surface an IntegrityError — exactly one wins, the other dedupes.
        """
        payload_json = intent.payload_json
        with self._lock:
            cur = self.conn.execute(
                "INSERT INTO intents (idempotency_key, intent_type, payload, created_at) "
                "VALUES (?, ?, ?, ?) ON CONFLICT(idempotency_key) DO NOTHING",
                (
                    intent.idempotency_key,
                    intent.intent_type,
                    payload_json,
                    self._now(),
                ),
            )
            self.conn.commit()
            created = cur.rowcount == 1
            row = self.conn.execute(
                "SELECT id, intent_type, payload FROM intents WHERE idempotency_key = ?",
                (intent.idempotency_key,),
            ).fetchone()
        if row is None:  # unreachable: rows are append-only, never deleted
            raise RuntimeError(
                f"intent {intent.idempotency_key!r} vanished after upsert"
            )
        if not created:
            # A key reused for DIFFERENT content is not a retry — it is a
            # bug or an attempt to smuggle a new action under an old key.
            if (row["intent_type"] != intent.intent_type
                    or row["payload"] != payload_json):
                raise IntentError(
                    f"idempotency_key {intent.idempotency_key!r} reused "
                    "for a different intent — rejected"
                )
            logger.info(
                "Duplicate intent %s (id=%d) — no-op",
                intent.idempotency_key, row["id"],
            )
        return row["id"], created

    def append_event(self, intent_id: int, status: str, reason: str = "") -> None:
        if status not in VALID_STATUSES:
            raise IntentError(f"invalid status {status!r}")
        with self._lock:
            latest = self.conn.execute(
                "SELECT status FROM intent_events WHERE intent_id = ? "
                "ORDER BY id DESC LIMIT 1",
                (intent_id,),
            ).fetchone()
            current = latest["status"] if latest else STATUS_PENDING
            # Terminal outcomes are write-once: appending EXECUTED after REJECTED
            # (etc.) would let a duplicate read a different outcome than the one
            # that was decided. Reject any event past a terminal status.
            if current in TERMINAL_STATUSES:
                raise IntentError(
                    f"intent {intent_id} is already in terminal status {current!r} "
                    "— outcomes are write-once"
                )
            self.conn.execute(
                "INSERT INTO intent_events (intent_id, status, reason, created_at) "
                "VALUES (?, ?, ?, ?)",
                (intent_id, status, reason, self._now()),
            )
            self.conn.commit()

    def current_status(self, intent_id: int) -> str:
        """Derived status: the most recent appended event, else PENDING."""
        with self._lock:
            row = self.conn.execute(
                "SELECT status FROM intent_events WHERE intent_id = ? "
                "ORDER BY id DESC LIMIT 1",
                (intent_id,),
            ).fetchone()
        return row["status"] if row else STATUS_PENDING

    def last_reason(self, intent_id: int) -> str:
        with self._lock:
            row = self.conn.execute(
                "SELECT reason FROM intent_events WHERE intent_id = ? "
                "ORDER BY id DESC LIMIT 1",
                (intent_id,),
            ).fetchone()
        return (row["reason"] or "") if row else ""

    def get_intent(self, intent_id: int) -> Intent:
        """Return the immutable intent stored for ``intent_id``.

        Workers must execute the database row itself, never an object supplied
        by a caller alongside an id. Reconstructing here preserves that trust
        boundary and re-runs Intent's strict JSON validation.
        """
        with self._lock:
            row = self.conn.execute(
                "SELECT idempotency_key, intent_type, payload FROM intents WHERE id = ?",
                (intent_id,),
            ).fetchone()
        if row is None:
            raise IntentError(f"unknown intent id {intent_id}")
        try:
            payload = json.loads(row["payload"])
        except (TypeError, json.JSONDecodeError) as exc:
            raise IntentError(f"stored intent {intent_id} has invalid JSON") from exc
        return Intent(row["intent_type"], payload, row["idempotency_key"])

    def pending_intents(self, limit: int = 25) -> list[tuple[int, Intent]]:
        """Return oldest non-terminal intents for the single leased worker."""
        if isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0:
            raise IntentError("pending intent limit must be a positive integer")
        with self._lock:
            rows = self.conn.execute(
                "SELECT i.id, i.idempotency_key, i.intent_type, i.payload "
                "FROM intents i "
                "LEFT JOIN intent_events e ON e.id = ("
                "  SELECT e2.id FROM intent_events e2 "
                "  WHERE e2.intent_id = i.id ORDER BY e2.id DESC LIMIT 1"
                ") "
                "WHERE COALESCE(e.status, ?) = ? "
                "ORDER BY i.id ASC LIMIT ?",
                (STATUS_PENDING, STATUS_PENDING, limit),
            ).fetchall()
        pending: list[tuple[int, Intent]] = []
        for row in rows:
            try:
                payload = json.loads(row["payload"])
            except (TypeError, json.JSONDecodeError) as exc:
                raise IntentError(f"stored intent {row['id']} has invalid JSON") from exc
            pending.append((
                row["id"],
                Intent(row["intent_type"], payload, row["idempotency_key"]),
            ))
        return pending

    def claim_intent_attempt(self, intent_id: int, holder: str) -> bool:
        """Atomically claim the intent's only automatic execution attempt."""
        if not isinstance(holder, str) or not holder.strip():
            raise IntentError("attempt holder must be a non-empty string")
        with self._lock:
            cur = self.conn.execute(
                "INSERT INTO intent_attempts (intent_id, holder, claimed_at) "
                "VALUES (?, ?, ?) ON CONFLICT(intent_id) DO NOTHING",
                (int(intent_id), holder.strip(), self._now()),
            )
            self.conn.commit()
        return cur.rowcount == 1

    # -- halts ---------------------------------------------------------------

    def record_halt(self, scope: str, reason: str) -> None:
        with self._lock:
            self.conn.execute(
                "INSERT INTO halts (scope, action, reason, created_at) "
                "VALUES (?, 'halt', ?, ?)",
                (scope, reason, self._now()),
            )
            self.conn.commit()
        logger.critical("BROKER HALT scope=%s reason=%s", scope, reason)

    def clear_halt(self, scope: str, operator: str) -> None:
        """Only a named human operator clears a halt (hard-stop discipline)."""
        if not operator or not operator.strip():
            raise IntentError("clearing a halt requires a named operator")
        with self._lock:
            self.conn.execute(
                "INSERT INTO halts (scope, action, operator, created_at) "
                "VALUES (?, 'clear', ?, ?)",
                (scope, operator.strip(), self._now()),
            )
            self.conn.commit()
        logger.warning("Halt cleared scope=%s by operator=%s", scope, operator)

    def halt_active(self, scope: str) -> bool:
        """True if the most recent event for this scope (or 'all') is a halt."""
        with self._lock:
            for check_scope in {scope, HALT_SCOPE_ALL}:
                row = self.conn.execute(
                    "SELECT action FROM halts WHERE scope = ? "
                    "ORDER BY id DESC LIMIT 1",
                    (check_scope,),
                ).fetchone()
                if row is not None and row["action"] == "halt":
                    return True
        return False

    # -- single-writer lease -------------------------------------------------
    # Mirrors the Supabase lease RPCs. SQLite computes the reference "now" so a
    # caller can't drift the clock; the lease table is mutable by design.

    def _sql_now(self) -> float:
        return self.conn.execute("SELECT strftime('%s', 'now') + 0.0").fetchone()[0]

    def acquire_lease(self, holder: str, ttl_seconds: float) -> bool:
        """Acquire (or idempotently renew) the loop lease. False if held.

        Atomic in a single statement: a fresh INSERT wins the free lease; on
        conflict the DO UPDATE fires only if the lease is expired or already
        ours, so two processes can't both win. Success is read back from the
        row rather than assumed.
        """
        ttl_seconds = _require_finite_ttl(ttl_seconds)
        with self._lock:
            now = self._sql_now()
            self.conn.execute(
                "INSERT INTO leases (name, holder, expires_at) VALUES (?, ?, ?) "
                "ON CONFLICT(name) DO UPDATE SET holder = excluded.holder, "
                "expires_at = excluded.expires_at "
                "WHERE leases.expires_at < ? OR leases.holder = excluded.holder",
                (self._lease_name, holder, now + ttl_seconds, now),
            )
            self.conn.commit()
            row = self.conn.execute(
                "SELECT holder FROM leases WHERE name = ?", (self._lease_name,),
            ).fetchone()
            return row is not None and row["holder"] == holder

    def renew_lease(self, holder: str, ttl_seconds: float) -> bool:
        ttl_seconds = _require_finite_ttl(ttl_seconds)
        with self._lock:
            now = self._sql_now()
            cur = self.conn.execute(
                "UPDATE leases SET expires_at = ? "
                "WHERE name = ? AND holder = ? AND expires_at >= ?",
                (now + ttl_seconds, self._lease_name, holder, now),
            )
            self.conn.commit()
            return cur.rowcount > 0

    def release_lease(self, holder: str) -> bool:
        with self._lock:
            cur = self.conn.execute(
                "DELETE FROM leases WHERE name = ? AND holder = ?",
                (self._lease_name, holder),
            )
            self.conn.commit()
            return cur.rowcount > 0

    def lease_holder(self) -> str | None:
        with self._lock:
            row = self.conn.execute(
                "SELECT holder FROM leases WHERE name = ?", (self._lease_name,),
            ).fetchone()
        return row["holder"] if row else None
