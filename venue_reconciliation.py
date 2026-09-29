"""Venue fill reconciliation job: venue records vs the mirrored trade ledger.

One run checks one venue account for one reporting day and writes one row to
``ledger_venue_reconciliations``. It compares:

  - what the venue says (a read-only collector such as
    ``kalshi_fill_collector.KalshiFillCollector``), including the collector's
    own statement of whether its collection is complete; and
  - the Supabase ledger mirror (``ledger_trades``), but only after checking
    that every ledger source the operator mapped to the account is present,
    fully exported, fresh past the interval, the same DB instance for the
    whole interval, and has mirror row counts equal to its local counts.

Any gap on either side makes the check ``incomplete`` with reasons. An empty,
stale, truncated or unmapped mirror therefore never produces a reconciled
zero. A verified quiet day (complete venue coverage, verified mirror, nothing
on either side) is ``matched`` with zero counts.

Reporting days are America/Detroit calendar days converted to UTC, so a day
is 23 or 25 hours long across DST changes.

This job never touches the local SQLite ledger or any order path, and it
reports fills only: realized PnL stays unverified because fees, settlements
and positions are not reconciled here.
"""

from __future__ import annotations

import logging
import time
import uuid
from datetime import date, datetime, timedelta, timezone
from datetime import time as dt_time
from zoneinfo import ZoneInfo

from ledger_sync import STATUS_TABLE, _parse_utc, reconcile_fills
from url_guard import assert_public_url

logger = logging.getLogger(__name__)

REPORTING_TZ = "America/Detroit"
RECONCILIATIONS_TABLE = "ledger_venue_reconciliations"
# Ledger rows are engine log times, which can precede the venue fill (a resting
# order) or trail it slightly; read a wider window, then targeted order ids.
LEDGER_WINDOW_BEFORE = timedelta(days=2)
LEDGER_WINDOW_AFTER = timedelta(days=1)
_ORDER_ID_CHUNK = 100


# ---------------------------------------------------------------------------
# Reporting-day bounds
# ---------------------------------------------------------------------------


def reporting_day_bounds(day: date, tz_name: str = REPORTING_TZ) -> tuple[datetime, datetime]:
    """UTC [start, end) of one local calendar day. Local midnight always exists in
    America/Detroit (its DST changes happen at 02:00), so the day is 23 h long in
    March, 25 h in November and 24 h otherwise."""
    tz = ZoneInfo(tz_name)
    start = datetime.combine(day, dt_time.min, tzinfo=tz)
    end = datetime.combine(day + timedelta(days=1), dt_time.min, tzinfo=tz)
    return start.astimezone(timezone.utc), end.astimezone(timezone.utc)


# ---------------------------------------------------------------------------
# Ledger mirror verification (pure)
# ---------------------------------------------------------------------------


def check_mirror_sources(status_rows: list[dict], mirror_counts: dict, services, interval_start: datetime,
                         interval_end: datetime, *, now: datetime,
                         finality_lag_seconds: int) -> tuple[set[str], list[dict]]:
    """Gaps (incomplete reasons) and per-service evidence for the mapped ledger sources.

    mirror_counts maps source_key -> {"trades": n, "positions": n}, or None when
    the count could not be read.
    """
    gaps: set[str] = set()
    evidence: list[dict] = []
    fresh_after = interval_end + timedelta(seconds=finality_lag_seconds)
    for service in services:
        rows = [r for r in status_rows if r.get("service") == service]
        item = {"service": service, "instances": len(rows), "problems": []}
        evidence.append(item)
        if not rows:
            item["problems"].append("ledger_mirror_source_missing")
            gaps.add("ledger_mirror_source_missing")
            continue
        rows.sort(key=lambda r: _parse_utc(r.get("last_attempt_at")) or datetime.min.replace(tzinfo=timezone.utc))
        current, others = rows[-1], rows[:-1]
        item["source_key"] = current.get("source_key")
        problems = item["problems"]
        for other in others:
            other_attempt = _parse_utc(other.get("last_attempt_at"))
            if other_attempt is None or other_attempt >= interval_start:
                # Another DB instance was active in or after the interval: the
                # service's history may be split, and an unsynced tail lost.
                problems.append("ledger_mirror_source_changed")
                break
        capture_since = _parse_utc(current.get("capture_since"))
        if capture_since is None or capture_since > interval_start:
            problems.append("ledger_capture_not_covering_interval")
        last_attempt = _parse_utc(current.get("last_attempt_at"))
        last_success = _parse_utc(current.get("last_success_at"))
        if (current.get("last_error") or last_attempt is None or last_success is None
                or last_success < last_attempt):
            problems.append("ledger_mirror_sync_failing")
        if not current.get("snapshot_complete") or current.get("pending_changes") != 0:
            problems.append("ledger_mirror_incomplete")
        if last_success is None or last_success < fresh_after or now < fresh_after:
            problems.append("ledger_mirror_stale")
        counts = mirror_counts.get(current.get("source_key"))
        if counts is None:
            problems.append("ledger_mirror_count_unavailable")
        elif (counts.get("trades") != current.get("local_trades_count")
              or counts.get("positions") != current.get("local_positions_count")):
            problems.append("ledger_mirror_count_mismatch")
        item["last_success_at"] = current.get("last_success_at")
        gaps.update(problems)
    return gaps, evidence


# ---------------------------------------------------------------------------
# PostgREST access (service backend credential; never the reporter role)
# ---------------------------------------------------------------------------


class MirrorReadError(RuntimeError):
    """The ledger mirror could not be read completely."""


def _in_list(values) -> str:
    quoted = ",".join('"' + str(v).replace("\\", "\\\\").replace('"', '\\"') + '"' for v in values)
    return f"in.({quoted})"


class PostgrestLedgerMirror:
    """Reads the ledger mirror and writes reconciliation rows over PostgREST.

    Reads are paged in a fixed order with an exact count, so a server-side row
    cap or a short page is detected as truncation instead of passing for a
    complete read.
    """

    def __init__(self, url: str, key: str, session=None, page_size: int = 1000,
                 max_pages: int = 200, timeout: float = 15.0):
        import requests as _requests

        self._base = assert_public_url(url, env_name="SUPABASE_URL", allow_http=False).rstrip("/")
        self._key = key
        self._session = session or _requests.Session()
        self._page_size = page_size
        self._max_pages = max_pages
        self._timeout = timeout

    def _headers(self, extra: dict | None = None) -> dict:
        headers = {"apikey": self._key, "Authorization": f"Bearer {self._key}",
                   "Accept": "application/json"}
        headers.update(extra or {})
        return headers

    def _get(self, table: str, params: dict, count: bool = False) -> tuple[list, int | None]:
        resp = self._session.get(
            f"{self._base}/rest/v1/{table}", params=params,
            headers=self._headers({"Prefer": "count=exact"} if count else None),
            timeout=self._timeout, allow_redirects=False)
        if resp.status_code >= 300:
            raise MirrorReadError(f"select {table} failed ({resp.status_code})")
        total = None
        if count:
            rng = resp.headers.get("Content-Range", "")
            tail = rng.rsplit("/", 1)[-1] if "/" in rng else ""
            if not tail.isdigit():
                raise MirrorReadError(f"select {table} returned no exact count")
            total = int(tail)
        rows = resp.json()
        if not isinstance(rows, list):
            raise MirrorReadError(f"select {table} returned a non-list body")
        return rows, total

    def _read_all(self, table: str, filters: dict, order: str) -> list[dict]:
        rows: list[dict] = []
        total = None
        for _ in range(self._max_pages):
            params = dict(filters, select="*", order=order, limit=self._page_size, offset=len(rows))
            page, count = self._get(table, params, count=total is None)
            if total is None:
                total = count
            if not page:
                break
            rows.extend(page)
            if len(rows) >= total:
                break
        if total is None or len(rows) != total:
            raise MirrorReadError(f"{table}: read {len(rows)} of {total} rows")
        return rows

    def status_rows(self, services) -> list[dict]:
        return self._read_all(STATUS_TABLE, {"service": _in_list(services)}, "source_key.asc")

    def live_counts(self, service: str, db_instance_id: str) -> dict:
        out = {}
        for key, table in (("trades", "ledger_trades"), ("positions", "ledger_positions")):
            _, total = self._get(table, {
                "service": f"eq.{service}", "db_instance_id": f"eq.{db_instance_id}",
                "deleted": "is.false", "select": "ledger_key", "limit": 1}, count=True)
            out[key] = total
        return out

    def ledger_trades(self, venue: str, since: datetime, until: datetime) -> list[dict]:
        windowed = self._read_all("ledger_trades", {
            "venue": f"eq.{venue}",
            "and": f"(recorded_at.gte.{since.isoformat()},recorded_at.lt.{until.isoformat()})",
        }, "ledger_key.asc")
        undated = self._read_all("ledger_trades", {"venue": f"eq.{venue}", "recorded_at": "is.null"},
                                 "ledger_key.asc")
        return windowed + undated

    def ledger_trades_for_orders(self, venue: str, order_ids) -> list[dict]:
        ids = sorted(set(order_ids))
        out: list[dict] = []
        for i in range(0, len(ids), _ORDER_ID_CHUNK):
            out.extend(self._read_all("ledger_trades", {
                "venue": f"eq.{venue}", "order_id": _in_list(ids[i:i + _ORDER_ID_CHUNK])},
                "ledger_key.asc"))
        return out

    def write_reconciliation(self, record: dict) -> None:
        resp = self._session.post(
            f"{self._base}/rest/v1/{RECONCILIATIONS_TABLE}", params={"on_conflict": "run_id"},
            headers=self._headers({"Content-Type": "application/json",
                                   "Prefer": "resolution=merge-duplicates,return=minimal"}),
            json=[record], timeout=self._timeout, allow_redirects=False)
        if resp.status_code >= 300:
            raise RuntimeError(f"reconciliation write failed ({resp.status_code})")


# ---------------------------------------------------------------------------
# One reconciliation run
# ---------------------------------------------------------------------------


def _read_ledger(mirror, scope, venue: str, start: datetime, end: datetime, venue_order_ids) -> tuple[list, set, dict]:
    gaps: set[str] = set()
    info: dict = {}
    status = mirror.status_rows(list(scope.ledger_services))
    counts = {}
    for row in status:
        try:
            counts[row.get("source_key")] = mirror.live_counts(row.get("service"), row.get("db_instance_id"))
        except Exception as exc:
            logger.warning("Ledger mirror count failed for %s: %s", row.get("source_key"), exc)
            counts[row.get("source_key")] = None
    info["status_rows"] = status
    info["counts"] = counts
    rows = mirror.ledger_trades(venue, start - LEDGER_WINDOW_BEFORE, end + LEDGER_WINDOW_AFTER)
    have = {str(r.get("order_id")) for r in rows if r.get("order_id")}
    missing = [oid for oid in venue_order_ids if oid not in have]
    if missing:
        rows.extend(mirror.ledger_trades_for_orders(venue, missing))
    merged = {r.get("ledger_key"): r for r in rows}
    rows = list(merged.values())
    unmapped = sorted({r.get("service") for r in rows
                       if r.get("account_ref") == scope.account_ref and not r.get("deleted")
                       and r.get("service") not in scope.ledger_services})
    if unmapped:
        gaps.add("ledger_unmapped_source")
    info["unmapped_services"] = unmapped
    return rows, gaps, info


def run_reconciliation(collector, scope, mirror, day: date, *, venue: str, clock=time.time,
                       finality_lag_seconds: int = 900, run_id: str | None = None,
                       tz_name: str = REPORTING_TZ) -> dict:
    """Collect, verify and compare one reporting day. Returns the row to persist.

    Never raises for missing evidence: every failure becomes an incomplete
    reason, so the persisted row says exactly what could not be verified.
    """
    start, end = reporting_day_bounds(day, tz_name)
    collection = collector.collect(start, end)
    venue_order_ids = sorted({f["order_id"] for f in collection.fills if f.get("order_id")})
    now = datetime.fromtimestamp(clock(), timezone.utc)
    mirror_gaps: set[str] = set()
    sources_evidence: list[dict] = []
    ledger_rows: list[dict] = []
    unmapped: list[str] = []
    try:
        ledger_rows, read_gaps, info = _read_ledger(mirror, scope, venue, start, end, venue_order_ids)
        mirror_gaps |= read_gaps
        unmapped = info["unmapped_services"]
        source_gaps, sources_evidence = check_mirror_sources(
            info["status_rows"], info["counts"], scope.ledger_services, start, end,
            now=now, finality_lag_seconds=finality_lag_seconds)
        mirror_gaps |= source_gaps
    except MirrorReadError as exc:
        logger.warning("Ledger mirror read incomplete: %s", exc)
        mirror_gaps.add("ledger_mirror_read_truncated")
        ledger_rows = []
    except Exception as exc:
        logger.warning("Ledger mirror unreadable: %s", exc)
        mirror_gaps.add("ledger_mirror_unreadable")
        ledger_rows = []

    result = reconcile_fills(
        collection.fills, ledger_rows, venue=venue, account_ref=scope.account_ref,
        interval_start=start, interval_end=end, venue_coverage=collection.coverage,
        evidence_gaps=list(collection.gaps) + sorted(mirror_gaps))
    evidence = dict(collection.evidence)
    evidence.update({
        "venue_gaps": list(collection.gaps),
        "ledger_gaps": sorted(mirror_gaps),
        "ledger_sources": sources_evidence,
        "ledger_rows_read": len(ledger_rows),
        "unmapped_ledger_services": unmapped,
        "reporting_day_hours": (end - start).total_seconds() / 3600,
    })
    return {
        **result,
        "run_id": run_id or str(uuid.uuid4()),
        "coverage_scope": collection.coverage.get("coverage_scope"),
        "reporting_tz": tz_name,
        "reporting_day": day.isoformat(),
        "collector": evidence.get("collector"),
        "collector_version": evidence.get("collector_version"),
        "collected_at": evidence.get("collected_at"),
        "ledger_mirror_verified": not mirror_gaps,
        "evidence": evidence,
    }
