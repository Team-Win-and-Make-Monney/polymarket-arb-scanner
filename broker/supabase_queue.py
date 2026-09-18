"""Supabase (PostgREST) backend for the policy-broker intent queue + lease.

Same interface as broker.queue.IntentQueue, plus a single-writer lease. The
append-only guarantee and idempotency/lease atomicity live in Postgres
(see migrations policy_broker_intent_queue / policy_broker_truncate_guard):
this client is a thin, deterministic wrapper over the REST endpoints.

Credentials come from the environment (never hardcoded, never logged):
  SUPABASE_URL                 e.g. https://<ref>.supabase.co
  SUPABASE_SERVICE_ROLE_KEY    service-role key (bypasses RLS; server-side only)
The broker is a trusted server-side component; the broker tables are RLS-deny
by default so only the service role can touch them.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os

try:
    import requests
except ImportError:  # SQLite-only install — this backend refuses to construct
    requests = None

from .queue import (
    STATUS_PENDING,
    TERMINAL_STATUSES,
    VALID_STATUSES,
    Intent,
    IntentError,
    _require_finite_ttl,
)

logger = logging.getLogger(__name__)

_DEFAULT_LEASE_NAME = "policy_broker_loop"


class SupabaseConfigError(RuntimeError):
    """Supabase URL/key not configured. Callers may skip or fail-closed."""


def _require_env(url: str | None, key: str | None) -> tuple[str, str]:
    url = url or os.getenv("SUPABASE_URL")
    key = (
        key
        or os.getenv("SUPABASE_SERVICE_ROLE_KEY")
        or os.getenv("SUPABASE_KEY")
    )
    if not url or not key:
        raise SupabaseConfigError(
            "SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY must be set for the "
            "Supabase broker backend"
        )
    return url.rstrip("/"), key


class SupabaseIntentQueue:
    """Append-only intent queue + halt ledger + single-writer lease on Supabase.

    Interchangeable with broker.queue.IntentQueue (duck-typed): the broker and
    validator depend only on these methods, never on the backing store.
    """

    def __init__(
        self,
        url: str | None = None,
        key: str | None = None,
        *,
        lease_name: str = _DEFAULT_LEASE_NAME,
        timeout: float = 15.0,
    ):
        if requests is None:
            raise SupabaseConfigError(
                "the 'requests' package is required for the Supabase broker "
                "backend (SQLite-only installs can use broker.queue.IntentQueue)"
            )
        self._url, api_key = _require_env(url, key)
        self._rest = f"{self._url}/rest/v1"
        self._rpc = f"{self._rest}/rpc"
        self._lease_name = lease_name
        self._timeout = timeout
        self._session = requests.Session()
        # api_key/authorization are set once; the key is never logged.
        self._session.headers.update({
            "apikey": api_key,
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        })

    # -- low-level helpers --------------------------------------------------

    def _post_rpc(self, fn: str, payload: dict):
        with self._transport(fn):
            resp = self._session.post(
                f"{self._rpc}/{fn}", json=payload, timeout=self._timeout
            )
        if resp.status_code >= 400:
            self._raise(fn, resp)
        return resp.json()

    def _get(self, table: str, params: dict):
        with self._transport(f"GET {table}"):
            resp = self._session.get(
                f"{self._rest}/{table}", params=params, timeout=self._timeout
            )
        if resp.status_code >= 400:
            self._raise(f"GET {table}", resp)
        return resp.json()

    def _insert(self, table: str, row: dict):
        with self._transport(f"INSERT {table}"):
            resp = self._session.post(
                f"{self._rest}/{table}",
                json=row,
                params={"select": "id"},
                headers={"Prefer": "return=representation"},
                timeout=self._timeout,
            )
        if resp.status_code >= 400:
            self._raise(f"INSERT {table}", resp)
        return resp.json()

    @contextlib.contextmanager
    def _transport(self, op: str):
        # A transport failure surfaces as a controlled RuntimeError (with the op,
        # never the key) so callers fail closed instead of leaking raw requests
        # exceptions out of the queue abstraction.
        try:
            yield
        except requests.RequestException as exc:
            raise RuntimeError(f"Supabase {op} unreachable: {exc}") from exc

    @staticmethod
    def _raise(op: str, resp: requests.Response) -> None:
        # PostgREST surfaces our RAISE(...) and CHECK messages in the body.
        body = resp.text or ""
        if "append-only" in body:
            raise IntentError(f"{op}: append-only violation ({resp.status_code})")
        if "reused for a different intent" in body:
            raise IntentError(f"{op}: idempotency_key reused for a different intent")
        raise RuntimeError(f"Supabase {op} failed ({resp.status_code}): {body}")

    # -- intents ------------------------------------------------------------

    def submit(self, intent: Intent) -> tuple[int, bool]:
        """Append an intent (server-side atomic dedupe). Returns (id, created)."""
        # Serialize from the canonical snapshot frozen at construction, so a
        # payload dict mutated after validation can neither smuggle content
        # nor surface a raw serialization error from the transport layer.
        rows = self._post_rpc("broker_submit_intent", {
            "p_key": intent.idempotency_key,
            "p_type": intent.intent_type,
            "p_payload": json.loads(intent.payload_json),
        })
        row = rows[0] if isinstance(rows, list) and rows else rows
        if not isinstance(row, dict) or "id" not in row or "created" not in row:
            raise RuntimeError(
                f"broker_submit_intent returned an unexpected response: {rows!r}"
            )
        # Strict bool on `created`: a non-bool truthy value (e.g. the string
        # "false") would make bool() True and let a DUPLICATE be treated as
        # newly created — re-executing a consequential action. Fail closed.
        intent_id = int(row["id"])
        created = self._as_strict_bool(row["created"], "broker_submit_intent.created")
        if not created:
            # Defense-in-depth + parity with the SQLite backend: never trust the
            # RPC's dedupe alone. A key reused for DIFFERENT content must be
            # rejected, not silently accepted as a no-op, even if the DB failed
            # to raise on the mismatch.
            self._assert_stored_matches(intent, intent_id)
        return intent_id, created

    def _assert_stored_matches(self, intent: Intent, intent_id: int) -> None:
        rows = self._get("broker_intents", {
            "idempotency_key": f"eq.{intent.idempotency_key}",
            "select": "intent_type,payload",
            "limit": "1",
        })
        if not rows:
            raise RuntimeError(
                f"broker_submit_intent reported a duplicate for "
                f"{intent.idempotency_key!r} but no stored row was found"
            )
        stored = rows[0]
        stored_payload = json.dumps(
            stored.get("payload"), sort_keys=True, allow_nan=False)
        if (stored.get("intent_type") != intent.intent_type
                or stored_payload != intent.payload_json):
            raise IntentError(
                f"idempotency_key {intent.idempotency_key!r} reused "
                "for a different intent — rejected"
            )

    def append_event(self, intent_id: int, status: str, reason: str = "") -> None:
        if status not in VALID_STATUSES:
            raise IntentError(f"invalid status {status!r}")
        # Terminal outcomes are write-once (parity with the SQLite backend); the
        # DB migration enforces this server-side too, this is the client guard.
        if self.current_status(intent_id) in TERMINAL_STATUSES:
            raise IntentError(
                f"intent {intent_id} is already in a terminal status — "
                "outcomes are write-once"
            )
        self._insert("broker_intent_events", {
            "intent_id": intent_id, "status": status, "reason": reason,
        })

    def current_status(self, intent_id: int) -> str:
        rows = self._get("broker_intent_events", {
            "intent_id": f"eq.{intent_id}",
            "select": "status",
            "order": "id.desc",
            "limit": "1",
        })
        return rows[0]["status"] if rows else STATUS_PENDING

    def last_reason(self, intent_id: int) -> str:
        rows = self._get("broker_intent_events", {
            "intent_id": f"eq.{intent_id}",
            "select": "reason",
            "order": "id.desc",
            "limit": "1",
        })
        return (rows[0]["reason"] or "") if rows else ""

    def get_intent(self, intent_id: int) -> Intent:
        rows = self._get("broker_intents", {
            "id": f"eq.{int(intent_id)}",
            "select": "idempotency_key,intent_type,payload",
            "limit": "1",
        })
        if not rows:
            raise IntentError(f"unknown intent id {intent_id}")
        row = rows[0]
        return Intent(row["intent_type"], row["payload"], row["idempotency_key"])

    def pending_intents(self, limit: int = 25) -> list[tuple[int, Intent]]:
        if isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0:
            raise IntentError("pending intent limit must be a positive integer")
        rows = self._post_rpc("broker_pending_intents", {"p_limit": limit})
        if not isinstance(rows, list):
            raise RuntimeError(
                f"broker_pending_intents returned an unexpected response: {rows!r}"
            )
        pending: list[tuple[int, Intent]] = []
        for row in rows:
            if not isinstance(row, dict) or not {
                "id", "idempotency_key", "intent_type", "payload",
            }.issubset(row):
                raise RuntimeError(
                    f"broker_pending_intents returned a malformed row: {row!r}"
                )
            pending.append((
                int(row["id"]),
                Intent(row["intent_type"], row["payload"], row["idempotency_key"]),
            ))
        return pending

    def claim_intent_attempt(self, intent_id: int, holder: str) -> bool:
        """Atomically claim the intent's only automatic execution attempt."""
        if not isinstance(holder, str) or not holder.strip():
            raise IntentError("attempt holder must be a non-empty string")
        return self._as_strict_bool(self._post_rpc("broker_claim_intent_attempt", {
            "p_intent_id": int(intent_id),
            "p_holder": holder.strip(),
        }), "broker_claim_intent_attempt")

    # -- halts ---------------------------------------------------------------

    def record_halt(self, scope: str, reason: str) -> None:
        self._insert("broker_halts", {
            "scope": scope, "action": "halt", "reason": reason,
        })
        logger.critical("BROKER HALT scope=%s reason=%s", scope, reason)

    def clear_halt(self, scope: str, operator: str) -> None:
        if not operator or not operator.strip():
            raise IntentError("clearing a halt requires a named operator")
        self._insert("broker_halts", {
            "scope": scope, "action": "clear", "operator": operator.strip(),
        })
        logger.warning("Halt cleared scope=%s by operator=%s", scope, operator)

    def halt_active(self, scope: str) -> bool:
        for check_scope in {scope, "all"}:
            rows = self._get("broker_halts", {
                "scope": f"eq.{check_scope}",
                "select": "action",
                "order": "id.desc",
                "limit": "1",
            })
            if rows and rows[0]["action"] == "halt":
                return True
        return False

    # -- single-writer lease -------------------------------------------------
    # TTL guard is the shared broker.queue._require_finite_ttl — one copy, so
    # the two backends can never drift on this safety check.

    @staticmethod
    def _as_strict_bool(value, op: str) -> bool:
        # The lease RPCs must return a bare JSON boolean. Anything else (a dict,
        # a list, None, a string) is an ambiguous or changed contract — a plain
        # bool() would coerce a truthy container into a *granted* lease and
        # defeat the single-writer guarantee. Fail closed instead.
        if isinstance(value, bool):
            return value
        raise RuntimeError(f"{op} returned a non-boolean response: {value!r}")

    def acquire_lease(self, holder: str, ttl_seconds: float) -> bool:
        """Acquire (or idempotently renew) the loop lease. False if held."""
        return self._as_strict_bool(self._post_rpc("broker_acquire_lease", {
            "p_name": self._lease_name,
            "p_holder": holder,
            "p_ttl_seconds": _require_finite_ttl(ttl_seconds),
        }), "broker_acquire_lease")

    def renew_lease(self, holder: str, ttl_seconds: float) -> bool:
        return self._as_strict_bool(self._post_rpc("broker_renew_lease", {
            "p_name": self._lease_name,
            "p_holder": holder,
            "p_ttl_seconds": _require_finite_ttl(ttl_seconds),
        }), "broker_renew_lease")

    def release_lease(self, holder: str) -> bool:
        return self._as_strict_bool(self._post_rpc("broker_release_lease", {
            "p_name": self._lease_name,
            "p_holder": holder,
        }), "broker_release_lease")

    def lease_holder(self) -> str | None:
        rows = self._get("broker_leases", {
            "name": f"eq.{self._lease_name}",
            "select": "holder,expires_at",
        })
        return rows[0]["holder"] if rows else None
