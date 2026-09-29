"""Mirror the operational trade ledger (trades.db) into Supabase for reporting.

trades.db stays the source record. This exporter copies the current state of
every ``trades`` and ``positions`` row, including later settlements, fill
corrections and deletes, into ``ledger_trades`` / ``ledger_positions``, and
writes a per-source ``ledger_sync_status`` row that says how complete the
mirror is. Reporting routines read restricted views over those tables (see
``supabase/drafts/0007_trade_ledger_reporting.sql``), never this module.

How changes are found: ``TradeDB.enable_ledger_capture`` installs SQLite
triggers that append every INSERT/UPDATE/DELETE on the two tables to
``ledger_outbox``. A row-id watermark alone would miss settlements and fill
updates on rows already exported. The first sync of a capture epoch pages a
full snapshot first, because changes made before the triggers existed were
never captured.

Versioning: each exported record carries ``source_version``, the highest
outbox ``seq`` visible when the row was read (in the same read transaction),
so the data includes every change up to that seq. The remote tables keep the
highest version (a trigger ignores older ones), so replays and retries are
idempotent and a slow retry can never roll a row back.

Provenance: ``run_mode`` is only ever the writer's own stamp or a row-intrinsic
dry-run marker (status or order id). Everything else is ``unknown``; nothing
here reads DRY_RUN or today's account configuration to label history.

Isolation: the exporter uses its own SQLite connection and is run off the
event loop by continuous.py; a failure never reaches order execution.
Deterministic; no LLM.
"""

from __future__ import annotations

import logging
import os
import sqlite3
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation

from db import LEDGER_CAPTURED_TABLES, RUN_MODES

logger = logging.getLogger(__name__)

SOURCE_SYSTEM = "arbgrid"
EXPORTER_VERSION = "1"
REMOTE_TABLES = {"trades": "ledger_trades", "positions": "ledger_positions"}
STATUS_TABLE = "ledger_sync_status"

# Row-intrinsic paper markers written only by dry-run / paper code paths.
PAPER_STATUSES = frozenset({"dry_run", "paper_near_miss"})
DRY_ORDER_ID_PREFIXES = ("dry_", "dryfill_")

# Fields the local ledger does not record at all; reported explicitly.
FEE_STATUS_NOT_RECORDED = "not_recorded"
PNL_BASIS = "engine_computed_unverified"


class LedgerCaptureMissing(RuntimeError):
    """The local DB has no complete change capture; exporting would be partial."""


# ---------------------------------------------------------------------------
# Identity and provenance
# ---------------------------------------------------------------------------


def service_name_from_env() -> str | None:
    """Service identity for the source key: LEDGER_SERVICE_NAME, else RAILWAY_SERVICE_NAME."""
    name = (os.getenv("LEDGER_SERVICE_NAME") or os.getenv("RAILWAY_SERVICE_NAME") or "").strip()
    return name or None


def ledger_key(service: str, db_instance_id: str, source_table: str, source_id: int) -> str:
    """Stable remote primary key for one local row.

    Scoped by source system, service and the local DB file's instance id, so
    two services (or a recreated volume) never collide. Venue and account are
    row attributes, not key parts: a deleted row no longer has them, and
    history has no account at all, so keying on them would fork identities.
    """
    return f"{SOURCE_SYSTEM}:{service}:{db_instance_id}:{source_table}:{int(source_id)}"


def source_key(service: str, db_instance_id: str) -> str:
    return f"{SOURCE_SYSTEM}:{service}:{db_instance_id}"


def derive_run_mode(row: dict) -> tuple[str, str]:
    """(run_mode, evidence) for one local row.

    - writer_stamped: the writer set run_mode when it inserted the row.
    - row_status / row_order_id: a dry-run marker only paper paths write.
    - conflict: the writer said live but the row carries a paper marker.
    - none: no evidence; the mode is unknown.
    """
    stamped = row.get("run_mode")
    paper_marker = None
    if row.get("status") in PAPER_STATUSES:
        paper_marker = "row_status"
    elif str(row.get("order_id") or "").startswith(DRY_ORDER_ID_PREFIXES):
        paper_marker = "row_order_id"
    if stamped in RUN_MODES:
        if stamped == "live" and paper_marker:
            return "unknown", "conflict"
        return stamped, "writer_stamped"
    if paper_marker:
        return "paper", paper_marker
    return "unknown", "none"


# ---------------------------------------------------------------------------
# Record builders
# ---------------------------------------------------------------------------


def _base_record(ctx: dict, source_table: str, source_id: int, version: int) -> dict:
    return {
        "ledger_key": ledger_key(ctx["service"], ctx["db_instance_id"], source_table, source_id),
        "source_system": SOURCE_SYSTEM,
        "service": ctx["service"],
        "db_instance_id": ctx["db_instance_id"],
        "source_table": source_table,
        "source_id": int(source_id),
        "source_version": int(version),
        "capture_epoch": ctx["capture_epoch"],
        "exported_at": ctx["exported_at"],
    }


def _pre_capture(ctx: dict, source_table: str, source_id: int) -> bool:
    return int(source_id) <= int(ctx["boundaries"].get(source_table, 0))


def trade_record(ctx: dict, row: dict, version: int, changed_at: str | None = None) -> dict:
    run_mode, evidence = derive_run_mode(row)
    rec = _base_record(ctx, "trades", row["id"], version)
    rec.update({
        "deleted": False,
        "pre_capture": _pre_capture(ctx, "trades", row["id"]),
        "source_changed_at": changed_at,
        "venue": row.get("platform"),
        "account_ref": row.get("account_ref"),
        "run_mode": run_mode,
        "mode_evidence": evidence,
        "opportunity_id": row.get("opportunity_id"),
        # Engine log time when the leg was written, not the venue's fill time.
        "recorded_at": row.get("timestamp"),
        "side": row.get("side"),
        "outcome": row.get("outcome"),
        "order_price": row.get("price"),
        "size": row.get("size"),
        "status": row.get("status"),
        "fill_price": row.get("fill_price"),
        "fill_qty": row.get("fill_qty"),
        "slippage": row.get("slippage"),
        "order_id": row.get("order_id"),
        "client_order_id": row.get("client_order_id"),
        "fee_usd": None,
        "fee_status": FEE_STATUS_NOT_RECORDED,
    })
    return rec


def position_record(ctx: dict, row: dict, version: int, changed_at: str | None = None) -> dict:
    run_mode, evidence = derive_run_mode(row)
    rec = _base_record(ctx, "positions", row["id"], version)
    rec.update({
        "deleted": False,
        "pre_capture": _pre_capture(ctx, "positions", row["id"]),
        "source_changed_at": changed_at,
        "venue": row.get("platform"),
        "account_ref": row.get("account_ref"),
        "run_mode": run_mode,
        "mode_evidence": evidence,
        "opportunity_id": row.get("opportunity_id"),
        "market_identifier": row.get("market_identifier"),
        "market_ticker": row.get("market_ticker"),
        "entry_at": row.get("entry_timestamp"),
        "settled_at": row.get("settlement_timestamp"),
        "status": row.get("status"),
        "expected_pnl": row.get("expected_pnl"),
        "realized_pnl": row.get("realized_pnl"),
        "pnl_basis": PNL_BASIS,
        "fee_usd": None,
        "fee_status": FEE_STATUS_NOT_RECORDED,
    })
    return rec


def tombstone_record(ctx: dict, source_table: str, source_id: int, version: int,
                     changed_at: str | None = None) -> dict:
    rec = _base_record(ctx, source_table, source_id, version)
    rec.update({"deleted": True, "source_changed_at": changed_at})
    return rec


_BUILDERS = {"trades": trade_record, "positions": position_record}


# ---------------------------------------------------------------------------
# Exporter
# ---------------------------------------------------------------------------


@dataclass
class SyncResult:
    phase: str = ""
    exported: dict = field(default_factory=dict)
    tombstones: int = 0
    watermark_seq: int = 0
    pending_changes: int = 0
    snapshot_complete: bool = False


class LedgerExporter:
    """Push trades.db ledger changes to Supabase; one instance per local DB file."""

    def __init__(self, client, db_path: str, service: str | None, batch_size: int = 500,
                 max_batches: int = 20, clock=time.time):
        if not service:
            raise ValueError("Ledger sync needs a service identity (LEDGER_SERVICE_NAME or RAILWAY_SERVICE_NAME)")
        self._client = client
        self._service = service
        self._batch_size = max(1, int(batch_size))
        self._max_batches = max(1, int(max_batches))
        self._clock = clock
        # Own connection: never contends for TradeDB's Python lock.
        self._conn = sqlite3.connect(db_path, check_same_thread=False, timeout=5.0,
                                     isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS ledger_sync_state (key TEXT PRIMARY KEY, value TEXT NOT NULL)")

    # -- local state ----------------------------------------------------------

    def _meta(self) -> dict[str, str]:
        try:
            rows = self._conn.execute("SELECT key, value FROM ledger_meta").fetchall()
        except sqlite3.OperationalError:
            return {}
        return {r["key"]: r["value"] for r in rows}

    def _capture_complete(self) -> bool:
        names = {r[0] for r in self._conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'trigger' AND name LIKE 'ledger_capture_%'")}
        return names == {f"ledger_capture_{t}_{op}" for t in LEDGER_CAPTURED_TABLES
                         for op in ("insert", "update", "delete")}

    def _state(self) -> dict[str, str]:
        return {r["key"]: r["value"] for r in self._conn.execute("SELECT key, value FROM ledger_sync_state")}

    def _save_state(self, updates: dict):
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            for key, value in updates.items():
                self._conn.execute(
                    "INSERT OR REPLACE INTO ledger_sync_state (key, value) VALUES (?, ?)", (key, str(value)))
            self._conn.execute("COMMIT")
        except Exception:
            self._conn.execute("ROLLBACK")
            raise

    def _context(self, meta: dict) -> dict:
        return {
            "service": self._service,
            "db_instance_id": meta["db_instance_id"],
            "capture_epoch": meta["capture_epoch"],
            "boundaries": {t: int(meta.get(f"capture_boundary_{t}_id", 0)) for t in LEDGER_CAPTURED_TABLES},
            "exported_at": datetime.fromtimestamp(self._clock(), timezone.utc).isoformat(),
        }

    # -- remote writes --------------------------------------------------------

    def _upsert(self, table: str, records: list[dict]):
        if records:
            conflict = "source_key" if table == STATUS_TABLE else "ledger_key"
            self._client.table(table).upsert(records, on_conflict=conflict).execute()

    def _push(self, ctx: dict, rows_by_table: dict, deleted: list[tuple[str, int, str | None]],
              version: int, changed: dict | None = None) -> tuple[dict, int]:
        """Upsert current rows then tombstones. Raises on any failure."""
        changed = changed or {}
        exported = {}
        for table, rows in rows_by_table.items():
            records = [_BUILDERS[table](ctx, row, version, changed.get((table, row["id"])))
                       for row in rows]
            self._upsert(REMOTE_TABLES[table], records)
            exported[table] = len(records)
        by_table: dict[str, list[dict]] = {}
        for table, source_id, changed_at in deleted:
            by_table.setdefault(table, []).append(tombstone_record(ctx, table, source_id, version, changed_at))
        for table, records in by_table.items():
            self._upsert(REMOTE_TABLES[table], records)
        return exported, len(deleted)

    # -- phases ---------------------------------------------------------------

    def _read_page(self, table: str, after_id: int) -> tuple[list[dict], int]:
        """Rows with id > after_id and the outbox head seq, in one read snapshot."""
        self._conn.execute("BEGIN")
        try:
            head = self._conn.execute("SELECT COALESCE(MAX(seq), 0) FROM ledger_outbox").fetchone()[0]
            rows = [dict(r) for r in self._conn.execute(
                f"SELECT * FROM {table} WHERE id > ? ORDER BY id LIMIT ?", (after_id, self._batch_size))]
        finally:
            self._conn.execute("COMMIT")
        return rows, int(head)

    def _snapshot_step(self, ctx: dict, state: dict, result: SyncResult) -> bool:
        """Export one snapshot page. Returns True once every table is fully paged."""
        for table in LEDGER_CAPTURED_TABLES:
            cursor_key = f"snapshot_cursor_{table}"
            if state.get(f"snapshot_done_{table}") == "1":
                continue
            cursor = int(state.get(cursor_key, 0))
            rows, head = self._read_page(table, cursor)
            if not rows:
                self._save_state({f"snapshot_done_{table}": 1})
                state[f"snapshot_done_{table}"] = "1"
                continue
            exported, _ = self._push(ctx, {table: rows}, [], head)
            result.exported[table] = result.exported.get(table, 0) + exported.get(table, 0)
            new_cursor = rows[-1]["id"]
            self._save_state({cursor_key: new_cursor})
            state[cursor_key] = str(new_cursor)
            return False
        self._save_state({"snapshot_complete": 1})
        state["snapshot_complete"] = "1"
        return True

    def _outbox_step(self, ctx: dict, state: dict, result: SyncResult) -> bool:
        """Export one outbox batch. Returns True when the outbox is drained."""
        watermark = int(state.get("watermark_seq", 0))
        self._conn.execute("BEGIN")
        try:
            events = [dict(r) for r in self._conn.execute(
                "SELECT seq, source_table, source_id, changed_at FROM ledger_outbox "
                "WHERE seq > ? ORDER BY seq LIMIT ?", (watermark, self._batch_size))]
            head = self._conn.execute("SELECT COALESCE(MAX(seq), 0) FROM ledger_outbox").fetchone()[0]
            latest: dict[tuple[str, int], str | None] = {}
            for ev in events:
                if ev["source_table"] in LEDGER_CAPTURED_TABLES:
                    latest[(ev["source_table"], ev["source_id"])] = ev["changed_at"]
            rows_by_table: dict[str, list[dict]] = {}
            deleted = []
            for table in LEDGER_CAPTURED_TABLES:
                ids = sorted(sid for (t, sid) in latest if t == table)
                found: dict[int, dict] = {}
                for i in range(0, len(ids), 500):
                    chunk = ids[i:i + 500]
                    marks = ",".join("?" for _ in chunk)
                    for r in self._conn.execute(f"SELECT * FROM {table} WHERE id IN ({marks})", chunk):
                        found[r["id"]] = dict(r)
                rows_by_table[table] = [found[i] for i in ids if i in found]
                deleted.extend((table, i, latest[(table, i)]) for i in ids if i not in found)
        finally:
            self._conn.execute("COMMIT")
        if not events:
            return True
        exported, tombstones = self._push(ctx, rows_by_table, deleted, int(head), latest)
        for table, n in exported.items():
            result.exported[table] = result.exported.get(table, 0) + n
        result.tombstones += tombstones
        new_watermark = events[-1]["seq"]
        self._save_state({"watermark_seq": new_watermark})
        state["watermark_seq"] = str(new_watermark)
        self._conn.execute("DELETE FROM ledger_outbox WHERE seq <= ?", (new_watermark,))
        return len(events) < self._batch_size

    # -- status ---------------------------------------------------------------

    def _local_counts(self) -> dict:
        out = {}
        for table in LEDGER_CAPTURED_TABLES:
            row = self._conn.execute(f"SELECT COUNT(*), COALESCE(MAX(id), 0) FROM {table}").fetchone()
            out[f"local_{table}_count"] = row[0]
            out[f"local_max_{table}_id"] = row[1]
        return out

    def _status_record(self, meta: dict, state: dict, ok: bool, error: str | None) -> dict:
        now = datetime.fromtimestamp(self._clock(), timezone.utc).isoformat()
        watermark = int(state.get("watermark_seq", 0))
        pending = self._conn.execute(
            "SELECT COUNT(*) FROM ledger_outbox WHERE seq > ?", (watermark,)).fetchone()[0]
        rec = {
            "source_key": source_key(self._service, meta.get("db_instance_id", "unknown")),
            "source_system": SOURCE_SYSTEM,
            "service": self._service,
            "db_instance_id": meta.get("db_instance_id"),
            "capture_epoch": meta.get("capture_epoch"),
            "capture_since": meta.get("capture_since"),
            "capture_boundary_trades_id": int(meta.get("capture_boundary_trades_id", 0)),
            "capture_boundary_positions_id": int(meta.get("capture_boundary_positions_id", 0)),
            "snapshot_complete": state.get("snapshot_complete") == "1",
            "watermark_seq": watermark,
            "pending_changes": int(pending),
            "last_attempt_at": now,
            "last_error": error,
            "exporter_version": EXPORTER_VERSION,
            **self._local_counts(),
        }
        if ok:
            rec["last_success_at"] = now
        return rec

    # -- entry point ----------------------------------------------------------

    def sync_once(self) -> SyncResult:
        """Run up to max_batches export batches. Raises on failure (nothing advances)."""
        meta = self._meta()
        if not meta.get("capture_epoch") or not meta.get("db_instance_id") or not self._capture_complete():
            raise LedgerCaptureMissing(
                "ledger change capture is not installed on this DB (set LEDGER_CAPTURE_ENABLED=true)")
        state = self._state()
        if state.get("synced_epoch") != meta["capture_epoch"]:
            # New capture epoch: anything before it was never captured, so page a
            # full snapshot, then follow the outbox from the snapshot's start.
            head = self._conn.execute("SELECT COALESCE(MAX(seq), 0) FROM ledger_outbox").fetchone()[0]
            self._conn.execute("DELETE FROM ledger_sync_state")
            fresh = {"synced_epoch": meta["capture_epoch"], "watermark_seq": head, "snapshot_complete": 0}
            self._save_state(fresh)
            state = {k: str(v) for k, v in fresh.items()}
        ctx = self._context(meta)
        result = SyncResult()
        error = None
        try:
            for _ in range(self._max_batches):
                if state.get("snapshot_complete") != "1":
                    result.phase = "snapshot"
                    self._snapshot_step(ctx, state, result)
                    continue
                result.phase = "outbox"
                if self._outbox_step(ctx, state, result):
                    break
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"[:500]
            self._report_status(meta, state, ok=False, error=error)
            raise
        self._report_status(meta, state, ok=True, error=None, raise_errors=True)
        result.watermark_seq = int(state.get("watermark_seq", 0))
        result.snapshot_complete = state.get("snapshot_complete") == "1"
        result.pending_changes = self._conn.execute(
            "SELECT COUNT(*) FROM ledger_outbox WHERE seq > ?", (result.watermark_seq,)).fetchone()[0]
        return result

    def _report_status(self, meta, state, ok: bool, error: str | None, raise_errors: bool = False):
        try:
            self._upsert(STATUS_TABLE, [self._status_record(meta, state, ok, error)])
        except Exception as exc:
            if raise_errors:
                raise
            logger.warning("Ledger sync status report failed: %s", exc)

    def close(self):
        self._conn.close()


# ---------------------------------------------------------------------------
# Venue reconciliation (pure; venue records come from read-only venue sources)
# ---------------------------------------------------------------------------


def _dec(value) -> Decimal | None:
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def reconcile_fills(venue_fills: list[dict], ledger_trades: list[dict], *, venue: str,
                    account_ref: str | None, interval_start: str, interval_end: str) -> dict:
    """Compare a venue's fill records for one account/interval with ledger trade rows.

    venue_fills: records from a venue statement or read-only venue API, each
        with ``order_id`` and optional ``qty`` and ``fee_usd``. They must cover
        the whole interval for the account; the caller asserts that.
    ledger_trades: ledger_trades records (as exported) for the same venue.

    Only live, non-deleted ledger rows with a fill (fill_price set, or status
    "filled") and an order id count. Matching is by venue order id. Returns a
    deterministic summary; status is "matched" only with no missing order on
    either side and no quantity mismatch. Venue fees are summed from the venue
    side, since the local ledger records none.
    """
    venue_by_order: dict[str, list[dict]] = {}
    for rec in venue_fills:
        oid = str(rec.get("order_id") or "")
        if oid:
            venue_by_order.setdefault(oid, []).append(rec)
    ledger_by_order: dict[str, list[dict]] = {}
    for rec in ledger_trades:
        if rec.get("deleted") or rec.get("venue") != venue or rec.get("run_mode") != "live":
            continue
        if account_ref is not None and rec.get("account_ref") != account_ref:
            continue
        if rec.get("fill_price") is None and rec.get("status") != "filled":
            continue
        oid = str(rec.get("order_id") or "")
        if oid:
            ledger_by_order.setdefault(oid, []).append(rec)

    missing_in_ledger = sorted(set(venue_by_order) - set(ledger_by_order))
    # Ledger fills are only expected in the venue interval if recorded inside it.
    missing_in_venue = sorted(
        oid for oid, recs in ledger_by_order.items()
        if oid not in venue_by_order
        and any(interval_start <= str(r.get("recorded_at") or "") < interval_end for r in recs))
    qty_mismatch = []
    for oid in sorted(set(venue_by_order) & set(ledger_by_order)):
        vq = [_dec(r.get("qty")) for r in venue_by_order[oid]]
        lq = [_dec(r.get("fill_qty")) for r in ledger_by_order[oid]]
        if None in vq or None in lq:
            continue
        if sum(vq, Decimal(0)) != sum(lq, Decimal(0)):
            qty_mismatch.append(oid)
    fees = [_dec(r.get("fee_usd")) for recs in venue_by_order.values() for r in recs]
    fees_known = bool(fees) and None not in fees
    ok = not missing_in_ledger and not missing_in_venue and not qty_mismatch
    return {
        "venue": venue,
        "account_ref": account_ref,
        "interval_start": interval_start,
        "interval_end": interval_end,
        "venue_order_count": len(venue_by_order),
        "ledger_order_count": len(ledger_by_order),
        "matched_order_count": len(set(venue_by_order) & set(ledger_by_order)),
        "missing_in_ledger": missing_in_ledger,
        "missing_in_venue": missing_in_venue,
        "qty_mismatch": qty_mismatch,
        "venue_fees_usd": str(sum(fees, Decimal(0))) if fees_known else None,
        "status": "matched" if ok else "mismatched",
    }
