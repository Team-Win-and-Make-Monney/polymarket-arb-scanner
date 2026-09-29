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

RECONCILE_MATCHED = "matched"
RECONCILE_MISMATCHED = "mismatched"
RECONCILE_INCOMPLETE = "incomplete"


def _dec(value) -> Decimal | None:
    """Finite Decimal, or None for missing, unparsable, boolean, NaN or infinite values."""
    if value is None or isinstance(value, bool):
        return None
    try:
        dec = Decimal(str(value).strip())
    except (InvalidOperation, ValueError):
        return None
    return dec if dec.is_finite() else None


def _parse_utc(value) -> datetime | None:
    """A timezone-aware instant in UTC, or None. Naive timestamps are rejected:
    without an offset the instant is unknown."""
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, str) and value.strip():
        text = value.strip()
        if text.endswith(("Z", "z")):
            text = text[:-1] + "+00:00"
        try:
            dt = datetime.fromisoformat(text)
        except ValueError:
            return None
    else:
        return None
    if dt.tzinfo is None or dt.utcoffset() is None:
        return None
    return dt.astimezone(timezone.utc)


def reconcile_fills(venue_fills: list[dict], ledger_trades: list[dict], *, venue: str,
                    account_ref: str | None, interval_start, interval_end,
                    venue_coverage: dict | None) -> dict:
    """Compare one account's venue fill records for [interval_start, interval_end) with the ledger.

    Fails closed: the result is "matched" only when every piece of evidence is
    present and valid and agrees. Any missing or invalid evidence makes it
    "incomplete" (listed in ``incomplete_reasons``); valid evidence that
    disagrees makes it "mismatched". A verified interval with no activity on
    either side is "matched" with zero counts.

    venue_fills: fill records from a venue statement or read-only venue API,
        each with ``order_id``, ``qty``, ``filled_at`` (timezone-aware), and
        optional ``fee_usd`` / ``account_ref``.
    ledger_trades: ledger_trades records (as exported) for this venue.
    venue_coverage: the source's own completeness assertion,
        ``{"account_ref", "interval_start", "interval_end", "complete", "source"}``.
        It must be complete, name the same account, and cover exactly the
        requested interval (compared as UTC instants).

    Interval bounds and all timestamps are compared as UTC instants, never as
    strings. Only venue fills inside the interval count. Ledger fills count
    toward "missing in venue" only when recorded inside the interval; the
    engine log time can trail the venue fill time, so a boundary straddle is
    reported as a mismatch rather than hidden.
    """
    reasons: set[str] = set()
    start = _parse_utc(interval_start)
    end = _parse_utc(interval_end)
    if start is None or end is None or end <= start:
        reasons.add("invalid_interval")
    if not account_ref:
        reasons.add("account_unknown")

    coverage_verified = False
    if not isinstance(venue_coverage, dict):
        reasons.add("venue_coverage_missing")
    else:
        cov_start = _parse_utc(venue_coverage.get("interval_start"))
        cov_end = _parse_utc(venue_coverage.get("interval_end"))
        coverage_verified = (
            venue_coverage.get("complete") is True
            and bool(venue_coverage.get("source"))
            and bool(account_ref)
            and venue_coverage.get("account_ref") == account_ref
            and start is not None and end is not None
            and cov_start == start and cov_end == end
        )
        if not coverage_verified:
            reasons.add("venue_coverage_unverified")

    def in_interval(ts) -> bool:
        return start is not None and end is not None and start <= ts < end

    # Venue side: every in-interval record must be attributable and complete.
    # venue_qty[order] is None when any of the order's quantities is unknown.
    venue_qty: dict[str, Decimal | None] = {}
    out_of_interval = 0
    fees: list[Decimal] = []
    fees_complete = True
    for rec in venue_fills:
        rec_account = rec.get("account_ref")
        if rec_account is not None and rec_account != account_ref:
            reasons.add("venue_record_account_mismatch")
            continue
        filled_at = _parse_utc(rec.get("filled_at"))
        if filled_at is None:
            reasons.add("venue_record_time_unknown")
            continue
        if not in_interval(filled_at):
            out_of_interval += 1
            continue
        oid = str(rec.get("order_id") or "").strip()
        if not oid:
            reasons.add("venue_record_missing_order_id")
            continue
        qty = _dec(rec.get("qty"))
        if qty is None:
            venue_qty[oid] = None
        elif venue_qty.get(oid, Decimal(0)) is not None:
            venue_qty[oid] = venue_qty.get(oid, Decimal(0)) + qty
        fee = _dec(rec.get("fee_usd"))
        if fee is None:
            fees_complete = False
        else:
            fees.append(fee)

    # Ledger side: live fills for this venue and account; anything that could
    # be one but is not attributable makes the result incomplete.
    ledger_qty: dict[str, Decimal | None] = {}
    ledger_in_interval: set[str] = set()
    for rec in ledger_trades:
        if rec.get("deleted") or rec.get("venue") != venue:
            continue
        if rec.get("fill_price") is None and rec.get("status") != "filled":
            continue
        recorded_at = _parse_utc(rec.get("recorded_at"))
        mode = rec.get("run_mode")
        if mode == "paper":
            continue
        if mode != "live" or rec.get("account_ref") is None:
            if recorded_at is None or in_interval(recorded_at):
                reasons.add("ledger_fill_unattributed")
            continue
        if rec.get("account_ref") != account_ref:
            continue
        oid = str(rec.get("order_id") or "").strip()
        if recorded_at is None:
            reasons.add("ledger_record_time_unknown")
        if not oid:
            if recorded_at is None or in_interval(recorded_at):
                reasons.add("ledger_record_missing_order_id")
            continue
        qty = _dec(rec.get("fill_qty"))
        if qty is None:
            ledger_qty[oid] = None
        elif ledger_qty.get(oid, Decimal(0)) is not None:
            ledger_qty[oid] = ledger_qty.get(oid, Decimal(0)) + qty
        if recorded_at is not None and in_interval(recorded_at):
            ledger_in_interval.add(oid)

    venue_orders = set(venue_qty)
    missing_in_ledger = sorted(venue_orders - set(ledger_qty))
    missing_in_venue = sorted(ledger_in_interval - venue_orders)
    qty_mismatch = []
    for oid in sorted(venue_orders & set(ledger_qty)):
        vq, lq = venue_qty[oid], ledger_qty[oid]
        if vq is None:
            reasons.add("venue_record_qty_unknown")
        elif lq is None:
            reasons.add("ledger_record_qty_unknown")
        elif vq != lq:
            qty_mismatch.append(oid)
    for oid in venue_orders - set(ledger_qty):
        if venue_qty[oid] is None:
            reasons.add("venue_record_qty_unknown")

    if reasons:
        status = RECONCILE_INCOMPLETE
    elif missing_in_ledger or missing_in_venue or qty_mismatch:
        status = RECONCILE_MISMATCHED
    else:
        status = RECONCILE_MATCHED
    return {
        "venue": venue,
        "account_ref": account_ref,
        "interval_start": start.isoformat() if start else None,
        "interval_end": end.isoformat() if end else None,
        "venue_source": venue_coverage.get("source") if isinstance(venue_coverage, dict) else None,
        "coverage_verified": coverage_verified,
        "venue_order_count": len(venue_orders),
        "ledger_order_count": len(ledger_in_interval),
        "matched_order_count": len(venue_orders & set(ledger_qty)),
        "venue_records_out_of_interval": out_of_interval,
        "missing_in_ledger": missing_in_ledger,
        "missing_in_venue": missing_in_venue,
        "qty_mismatch": qty_mismatch,
        "venue_fees_usd": str(sum(fees, Decimal(0))) if fees_complete else None,
        "fees_complete": fees_complete,
        "incomplete_reasons": sorted(reasons),
        "status": status,
    }
