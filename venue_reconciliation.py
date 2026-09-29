"""Venue fill reconciliation job: venue records vs the mirrored trade ledger.

One run checks one venue account for one reporting day and writes one row to
``ledger_venue_reconciliations``. It compares:

  - what the venue says (a read-only collector such as
    ``kalshi_fill_collector.KalshiFillCollector``), including the collector's
    own statement of whether its collection is complete; and
  - the Supabase ledger mirror (``ledger_trades``), but only after checking
    that every ledger source the operator mapped to the account is present,
    fully exported, fresh past the interval and recently successful, the same
    DB instance for the whole interval, and has mirror row counts equal to its
    local counts.

The mirror read is fenced: source status is read before and after the rows,
every page must report the same exact total, and no in-scope row may have
been written since the read started. Any movement makes the check
incomplete. The read start (backdated by a clock-skew allowance) is stored as
``ledger_read_started_at`` with the order ids and sources the check relied
on, so the reporting view withdraws the result when any of them changes
later, or when a source stops syncing.

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
import math
import time
import uuid
from datetime import date, datetime, timedelta, timezone
from datetime import time as dt_time
from zoneinfo import ZoneInfo

from ledger_sync import (DEFAULT_CLOCK_SKEW, DEFAULT_RECORDING_LAG, STATUS_TABLE, _parse_utc,
                         reconcile_fills)
from url_guard import assert_public_url

logger = logging.getLogger(__name__)

REPORTING_TZ = "America/Detroit"
RECONCILIATIONS_TABLE = "ledger_venue_reconciliations"
# Ledger rows are engine log times, which can precede the venue fill (a resting
# order) or trail it slightly; read a wider window, then targeted order ids.
LEDGER_WINDOW_BEFORE = timedelta(days=2)
LEDGER_WINDOW_AFTER = timedelta(days=1)
_ORDER_ID_CHUNK = 100
# Backdate the stored read start so a mirror write stamped by a database clock
# slightly behind this host is still seen as "after the read".
MIRROR_CLOCK_SKEW = timedelta(seconds=120)
# A source whose last successful export is older than this is not current
# evidence, even for a long-finished day: corrections may be unexported.
DEFAULT_MAX_SOURCE_AGE_SECONDS = 3600
# Status fields that must not move between the fence reads.
_FENCE_FIELDS = ("source_key", "db_instance_id", "capture_epoch", "supersedes_db_instance_ids",
                 "watermark_seq", "pending_changes",
                 "snapshot_complete", "local_trades_count", "local_positions_count",
                 "last_attempt_at", "last_success_at", "last_error")


def _check_seconds(name: str, value, *, minimum: float, allow_equal: bool) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError(f"{name} must be a finite number")
    if value < minimum or (not allow_equal and value == minimum):
        raise ValueError(f"{name} must be {'>=' if allow_equal else '>'} {minimum}")
    return value


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
                         interval_end: datetime, *, now: datetime, finality_lag_seconds: float,
                         max_source_age_seconds: float = DEFAULT_MAX_SOURCE_AGE_SECONDS
                         ) -> tuple[set[str], list[dict]]:
    """Gaps (incomplete reasons) and per-service evidence for the mapped ledger sources.

    mirror_counts maps source_key -> {"trades": n, "positions": n}, or None when
    the count could not be read. A source must have succeeded after the day
    plus the finality lag AND within max_source_age_seconds of now. An
    instance another status row supersedes (an earlier capture generation of
    the same DB file) is never a candidate; see superseded_instances.
    """
    _check_seconds("finality_lag_seconds", finality_lag_seconds, minimum=0, allow_equal=True)
    _check_seconds("max_source_age_seconds", max_source_age_seconds, minimum=0, allow_equal=False)
    gaps: set[str] = set()
    evidence: list[dict] = []
    fresh_after = interval_end + timedelta(seconds=finality_lag_seconds)
    recent_after = now - timedelta(seconds=max_source_age_seconds)
    superseded = superseded_instances(status_rows)
    for service in services:
        rows = [r for r in status_rows if r.get("service") == service
                and (service, r.get("db_instance_id")) not in superseded]
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
        if last_success is None or last_success < recent_after:
            problems.append("ledger_mirror_source_not_recent")
        counts = mirror_counts.get(current.get("source_key"))
        if counts is None:
            problems.append("ledger_mirror_count_unavailable")
        elif (counts.get("trades") != current.get("local_trades_count")
              or counts.get("positions") != current.get("local_positions_count")):
            problems.append("ledger_mirror_count_mismatch")
        item["last_success_at"] = current.get("last_success_at")
        gaps.update(problems)
    return gaps, evidence


def superseded_instances(status_rows: list[dict]) -> set[tuple[str, str]]:
    """(service, db_instance_id) pairs replaced by a later capture generation.

    Their mirrored rows may include rows deleted or changed while nothing was
    captured, so they are excluded everywhere, as in the reporting views.
    """
    return {(r.get("service"), inst) for r in status_rows
            for inst in (r.get("supersedes_db_instance_ids") or [])}


# ---------------------------------------------------------------------------
# PostgREST access (service backend credential; never the reporter role)
# ---------------------------------------------------------------------------


class MirrorReadError(RuntimeError):
    """The ledger mirror could not be read completely."""


class MirrorChangedError(MirrorReadError):
    """The ledger mirror changed while it was being read."""


def _quote(value) -> str:
    return '"' + str(value).replace("\\", "\\\\").replace('"', '\\"') + '"'


def _in_list(values) -> str:
    return f"in.({','.join(_quote(v) for v in values)})"


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
            # Every page reports the exact total; a change between pages means
            # offsets shifted and the pages may mix two states.
            page, count = self._get(table, params, count=True)
            if total is None:
                total = count
            elif count != total:
                raise MirrorChangedError(f"{table}: row count moved from {total} to {count} during paging")
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
            "and": f"(recorded_at.gte.{_quote(since.isoformat())},recorded_at.lt.{_quote(until.isoformat())})",
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

    def changed_since(self, venue: str, since: datetime, interval_start: datetime, interval_end: datetime,
                      order_ids) -> int:
        """Exact count of in-scope ledger_trades rows written at or after ``since``.

        In scope: this venue (or a tombstone that never carried one), and
        recorded inside the interval, undated, or one of the order ids the
        result depends on. The same scope as the reporting view's staleness.
        """
        venue_clause = f"or(venue.eq.{_quote(venue)},and(deleted.is.true,venue.is.null))"
        scopes = [f"or(and(recorded_at.gte.{_quote(interval_start.isoformat())},"
                  f"recorded_at.lt.{_quote(interval_end.isoformat())}),recorded_at.is.null)"]
        ids = sorted(set(order_ids))
        scopes += [f"order_id.{_in_list(ids[i:i + _ORDER_ID_CHUNK])}"
                   for i in range(0, len(ids), _ORDER_ID_CHUNK)]
        total = 0
        for scope in scopes:
            _, n = self._get("ledger_trades", {
                "synced_at": f"gte.{since.isoformat()}",
                "and": f"({venue_clause},{scope})",
                "select": "ledger_key", "limit": 1}, count=True)
            total += n
        return total

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


def _fence(status_rows: list[dict]) -> list[tuple]:
    return sorted(tuple(str(r.get(k)) for k in _FENCE_FIELDS) for r in status_rows)


def _read_ledger(mirror, scope, venue: str, start: datetime, end: datetime, venue_order_ids,
                 read_started: datetime) -> tuple[list, set, dict]:
    """Fenced read of the mapped sources' status, counts and ledger rows."""
    gaps: set[str] = set()
    info: dict = {}
    services = list(scope.ledger_services)
    status = mirror.status_rows(services)
    counts = {}
    for row in status:
        try:
            counts[row.get("source_key")] = mirror.live_counts(row.get("service"), row.get("db_instance_id"))
        except Exception as exc:
            logger.warning("Ledger mirror count failed for %s: %s", row.get("source_key"), exc)
            counts[row.get("source_key")] = None
    info["status_rows"] = status
    info["counts"] = counts
    window_since, window_until = start - LEDGER_WINDOW_BEFORE, end + LEDGER_WINDOW_AFTER
    superseded = superseded_instances(status)

    def current(found: list[dict]) -> list[dict]:
        return [r for r in found if (r.get("service"), r.get("db_instance_id")) not in superseded]

    rows = current(mirror.ledger_trades(venue, window_since, window_until))
    have = {str(r.get("order_id")) for r in rows if r.get("order_id")}
    missing = [oid for oid in venue_order_ids if oid not in have]
    if missing:
        rows.extend(current(mirror.ledger_trades_for_orders(venue, missing)))
    merged = {r.get("ledger_key"): r for r in rows}
    rows = list(merged.values())
    # The result depends on the venue's orders and on ledger rows recorded in
    # the interval, near enough to a boundary to be ambiguous, or undated;
    # rows for other orders further out do not change it, so later activity on
    # other days does not withdraw it.
    relevant = set(venue_order_ids)
    near_start, near_end = start - DEFAULT_CLOCK_SKEW, end + DEFAULT_RECORDING_LAG
    for r in rows:
        at = _parse_utc(r.get("recorded_at"))
        if r.get("order_id") and (at is None or near_start <= at < near_end):
            relevant.add(str(r["order_id"]))
    order_ids = sorted(relevant)
    info["order_ids"] = order_ids

    # Fence: the sources must not have moved, and nothing the check relies on
    # may have been written since the read started.
    if _fence(mirror.status_rows(services)) != _fence(status):
        gaps.add("ledger_mirror_changed_during_read")
    if mirror.changed_since(venue, read_started, near_start, near_end, order_ids):
        gaps.add("ledger_mirror_changed_during_read")

    unmapped = sorted({r.get("service") for r in rows
                       if r.get("account_ref") == scope.account_ref and not r.get("deleted")
                       and r.get("service") not in scope.ledger_services})
    if unmapped:
        gaps.add("ledger_unmapped_source")
    info["unmapped_services"] = unmapped
    return rows, gaps, info


def _failed_collection(scope, start: datetime, end: datetime, exc: Exception):
    """An incomplete collection for a collector that raised: a row is still
    written, so an older matched check never stands unchallenged."""
    from kalshi_fill_collector import KalshiCollection

    return KalshiCollection(
        fills=[], gaps=["venue_collector_error"],
        coverage={"account_ref": scope.account_ref, "interval_start": start.isoformat(),
                  "interval_end": end.isoformat(), "complete": False, "source": None,
                  "coverage_scope": scope.coverage_scope},
        evidence={"collector_error": type(exc).__name__})


def run_reconciliation(collector, scope, mirror, day: date, *, venue: str, clock=time.time,
                       finality_lag_seconds: float = 900,
                       max_source_age_seconds: float = DEFAULT_MAX_SOURCE_AGE_SECONDS,
                       run_id: str | None = None, tz_name: str = REPORTING_TZ) -> dict:
    """Collect, verify and compare one reporting day. Returns the row to persist.

    Never raises for missing or bad evidence: every failure, including a
    collector or mirror exception, becomes an incomplete reason, so the
    persisted row says exactly what could not be verified. Invalid settings
    raise ValueError before anything is read.
    """
    _check_seconds("finality_lag_seconds", finality_lag_seconds, minimum=0, allow_equal=True)
    _check_seconds("max_source_age_seconds", max_source_age_seconds, minimum=0, allow_equal=False)
    if finality_lag_seconds < DEFAULT_RECORDING_LAG.total_seconds():
        # Rows for fills just before the day ends may not be recorded yet.
        raise ValueError(f"finality_lag_seconds must be at least the "
                         f"{DEFAULT_RECORDING_LAG.total_seconds():.0f}s recording lag")
    start, end = reporting_day_bounds(day, tz_name)
    try:
        collection = collector.collect(start, end)
    except Exception as exc:
        logger.warning("Venue collector failed: %s", exc)
        collection = _failed_collection(scope, start, end, exc)
    venue_order_ids = sorted({f["order_id"] for f in collection.fills if f.get("order_id")})
    now = datetime.fromtimestamp(clock(), timezone.utc)
    read_started = now - MIRROR_CLOCK_SKEW
    mirror_gaps: set[str] = set()
    sources_evidence: list[dict] = []
    ledger_rows: list[dict] = []
    unmapped: list[str] = []
    order_ids: list[str] = list(venue_order_ids)
    try:
        ledger_rows, read_gaps, info = _read_ledger(mirror, scope, venue, start, end, venue_order_ids,
                                                    read_started)
        mirror_gaps |= read_gaps
        unmapped = info["unmapped_services"]
        order_ids = info["order_ids"]
        source_gaps, sources_evidence = check_mirror_sources(
            info["status_rows"], info["counts"], scope.ledger_services, start, end,
            now=now, finality_lag_seconds=finality_lag_seconds,
            max_source_age_seconds=max_source_age_seconds)
        mirror_gaps |= source_gaps
    except MirrorChangedError as exc:
        logger.warning("Ledger mirror changed during read: %s", exc)
        mirror_gaps.add("ledger_mirror_changed_during_read")
        ledger_rows = []
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
        "mirror_clock_skew_seconds": MIRROR_CLOCK_SKEW.total_seconds(),
        "recording_lag_seconds": DEFAULT_RECORDING_LAG.total_seconds(),
        "recording_clock_skew_seconds": DEFAULT_CLOCK_SKEW.total_seconds(),
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
        # What the result depends on, so the reporting view can withdraw it
        # when any of it changes after the read, or a source stops syncing.
        "ledger_read_started_at": read_started.isoformat(),
        "ledger_order_ids": order_ids,
        "ledger_sources": [{"service": e["service"], "source_key": e["source_key"]}
                           for e in sources_evidence if e.get("source_key")],
        "max_source_age_seconds": float(max_source_age_seconds),
        "evidence": evidence,
    }
