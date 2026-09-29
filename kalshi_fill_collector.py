"""Read-only Kalshi fill collector for venue reconciliation.

Collects this account's executed fills for one interval from the Kalshi Trade
API and states, explicitly, whether that collection is complete. The result
feeds ``ledger_sync.reconcile_fills``; nothing here reads the local SQLite
ledger, and nothing here can place, cancel or modify an order: the transport
only allows GET on the three read paths below.

Source documentation (Kalshi Trade API OpenAPI 3.31.0, retrieved 2026-09-29):
  - https://docs.kalshi.com/getting_started/historical_data
    ``GET /historical/cutoff`` ``trades_created_ts`` splits fills between
    ``/portfolio/fills`` (at or after the cutoff) and ``/historical/fills``
    (before it). A complete history combines both tiers. Cutoffs advance, so
    a cutoff that moves during collection makes the collection unverifiable.
  - https://docs.kalshi.com/api-reference/portfolio/get-fills
  - https://docs.kalshi.com/api-reference/historical/get-historical-fills
    Responses require ``fills`` and ``cursor``; each fill requires
    ``fill_id`` (``trade_id`` is a legacy alias), ``order_id``, ``count_fp``
    (fixed-point contracts, 2 decimals, fractional allowed) and ``fee_cost``
    (fixed-point dollars). ``created_time`` (date-time) and ``ts`` (legacy
    Unix seconds) give the execution time. ``subaccount_number`` is present
    for direct users. ``min_ts``/``max_ts`` filter "after"/"before" a Unix
    second; inclusivity is not documented, so the collector over-fetches one
    second on each side and filters the exact ``[start, end)`` itself.
    ``limit`` is 1..1000. Omitting ``subaccount`` means all subaccounts, but a
    key restricted to one subaccount returns only that subaccount's fills.

Coverage is asserted only when all of these hold:
  - the running API key matches the operator-declared scope (fingerprint);
  - the interval ended at least ``finality_lag_seconds`` ago;
  - the cutoff was read before and after collection and did not move;
  - every tier the interval needs was paginated to an empty cursor, with no
    failed, malformed, repeated-cursor or page-limit-exhausted page;
  - no fill falls outside the declared subaccount scope and no fill id was
    returned twice with different content.
Successful partial pagination never counts as coverage, and neither does the
local ledger. The declared scope decides what the coverage means: a key
declared for one subaccount covers that subaccount only, never the account.
"""

from __future__ import annotations

import hashlib
import logging
import math
import re
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation

from ledger_sync import _dec, _parse_utc

logger = logging.getLogger(__name__)

VENUE = "kalshi"
COLLECTOR_NAME = "kalshi_fills"
COLLECTOR_VERSION = "1"

LIVE_FILLS_PATH = "/portfolio/fills"
HISTORICAL_FILLS_PATH = "/historical/fills"
CUTOFF_PATH = "/historical/cutoff"
READ_ONLY_PATHS = frozenset({LIVE_FILLS_PATH, HISTORICAL_FILLS_PATH, CUTOFF_PATH})

MAX_PAGE_LIMIT = 1000
_QTY_STEP = Decimal("0.01")
_FINGERPRINT_RE = re.compile(r"^sha256:[0-9a-f]{16}$")
_LABEL_RE = re.compile(r"^[A-Za-z0-9._:-]{1,64}$")
_RETRYABLE_STATUSES = frozenset({429, 500, 502, 503, 504})


class ReadOnlyViolation(RuntimeError):
    """A request outside the collector's read-only allowlist was attempted."""


# ---------------------------------------------------------------------------
# Declared scope (operator-verified; never inferred)
# ---------------------------------------------------------------------------


def key_fingerprint(api_key_id: str) -> str:
    """Non-secret, stable identifier for an API key id (never the key itself)."""
    return "sha256:" + hashlib.sha256(str(api_key_id).encode("utf-8")).hexdigest()[:16]


@dataclass(frozen=True)
class KalshiScope:
    """Which Kalshi credential, account label and subaccounts a collection covers.

    Declared by the operator after checking the key's restriction in Kalshi's
    account settings. ``subaccount=None`` means the key is unrestricted and
    the query omits ``subaccount`` (all subaccounts). ``ledger_services`` are
    the services whose mirrored ledgers must be complete and fresh before the
    ledger side of a check counts.
    """

    account_ref: str
    key_fingerprint: str
    subaccount: int | None
    ledger_services: tuple[str, ...]
    verified_by: str
    verified_on: str

    @property
    def coverage_scope(self) -> str:
        return "all_subaccounts" if self.subaccount is None else f"subaccount:{self.subaccount}"


def parse_kalshi_scope(raw: dict) -> KalshiScope:
    """Validate the operator's declared scope. Raises ValueError when anything is missing.

    Expected shape (non-secret; the fingerprint is ``key_fingerprint(key_id)``)::

        {"account_ref": "kalshi-main", "key_fingerprint": "sha256:0123456789abcdef",
         "subaccount": "all" | 0..63, "ledger_services": ["arb-scanner", "kalshi-mm-pilot"],
         "verified_by": "jonathon", "verified_on": "2026-09-29"}
    """
    if not isinstance(raw, dict):
        raise ValueError("Kalshi scope must be a JSON object")
    account_ref = raw.get("account_ref")
    if not isinstance(account_ref, str) or not _LABEL_RE.match(account_ref):
        raise ValueError("account_ref must be a label of 1-64 characters [A-Za-z0-9._:-]")
    fingerprint = raw.get("key_fingerprint")
    if not isinstance(fingerprint, str) or not _FINGERPRINT_RE.match(fingerprint):
        raise ValueError("key_fingerprint must look like sha256:<16 lowercase hex>")
    sub = raw.get("subaccount")
    if sub == "all":
        subaccount = None
    elif isinstance(sub, int) and not isinstance(sub, bool) and 0 <= sub <= 63:
        subaccount = sub
    else:
        raise ValueError('subaccount must be "all" or an integer 0-63')
    services = raw.get("ledger_services")
    if (not isinstance(services, list) or not services
            or not all(isinstance(s, str) and s.strip() for s in services)
            or len(set(services)) != len(services)):
        raise ValueError("ledger_services must be a non-empty list of distinct service names")
    verified_by = raw.get("verified_by")
    if not isinstance(verified_by, str) or not verified_by.strip():
        raise ValueError("verified_by must name who verified the key scope")
    verified_on = raw.get("verified_on")
    try:
        date.fromisoformat(str(verified_on))
    except ValueError:
        raise ValueError("verified_on must be a YYYY-MM-DD date") from None
    return KalshiScope(account_ref, fingerprint, subaccount, tuple(services),
                       verified_by.strip(), str(verified_on))


# ---------------------------------------------------------------------------
# Read-only transport
# ---------------------------------------------------------------------------


def read_only_transport(client):
    """GET-only adapter over an authenticated ``kalshi_api.KalshiClient``.

    Returns ``get_json(path, params) -> (status_code | None, body | None, error | None)``.
    Reuses the client's signing; refuses any path outside READ_ONLY_PATHS.
    """
    def get_json(path: str, params: dict):
        if path not in READ_ONLY_PATHS:
            raise ReadOnlyViolation(f"refusing non-allowlisted Kalshi path {path!r}")
        try:
            resp = client._request("GET", path, params=params)
        except Exception as exc:  # rate limit / connection errors after the client's own retries
            return None, None, type(exc).__name__
        if resp is None:
            return None, None, "no_response"
        try:
            body = resp.json()
        except ValueError:
            body = None
        return resp.status_code, body, None
    return get_json


# ---------------------------------------------------------------------------
# Record normalization
# ---------------------------------------------------------------------------


# Unix seconds datetime can represent in UTC: [1970-01-01, 9999-12-31T23:59:59].
_MIN_TS = 0
_MAX_TS = 253402300800


def _finite_number(name: str, value, *, minimum: float, allow_equal: bool = True) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError(f"{name} must be a finite number")
    if value < minimum or (not allow_equal and value == minimum):
        raise ValueError(f"{name} must be {'>=' if allow_equal else '>'} {minimum}")
    return value


def _positive_int(name: str, value) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"{name} must be a positive integer")
    return value


def parse_count_fp(value) -> Decimal | None:
    """Finite, positive fixed-point contract count with at most 2 decimals, else None."""
    if not isinstance(value, str):
        return None
    qty = _dec(value)
    if qty is None or qty <= 0:
        return None
    try:
        if qty != qty.quantize(_QTY_STEP):
            return None
    except InvalidOperation:
        return None
    return qty.quantize(_QTY_STEP)


def normalize_fill(raw: dict, account_ref: str, tier: str) -> tuple[dict, set[str]]:
    """One Kalshi fill as a reconcile_fills venue record, plus any problems found.

    Unknown or invalid values stay None so reconcile_fills reports them as
    incomplete evidence; nothing is defaulted to zero.
    """
    problems: set[str] = set()
    fill_id = raw.get("fill_id")
    trade_id = raw.get("trade_id")
    if isinstance(fill_id, str) and isinstance(trade_id, str) and fill_id and trade_id and fill_id != trade_id:
        problems.add("venue_record_id_conflict")
    fid = fill_id if isinstance(fill_id, str) and fill_id else (
        trade_id if isinstance(trade_id, str) and trade_id else None)
    if fid is None:
        problems.add("venue_record_missing_fill_id")

    created = _parse_utc(raw.get("created_time")) if raw.get("created_time") is not None else None
    ts = raw.get("ts")
    ts_ok = isinstance(ts, int) and not isinstance(ts, bool)
    if ts_ok and not _MIN_TS <= ts < _MAX_TS:
        # Out of datetime range: an explicit problem, never a crash.
        problems.add("venue_record_time_invalid")
        ts_ok = False
    filled_at = None
    if raw.get("created_time") is not None and created is None:
        problems.add("venue_record_time_invalid")
    elif created is not None and ts_ok and abs(created.timestamp() - ts) >= 1:
        problems.add("venue_record_time_conflict")
    elif created is not None:
        filled_at = created
    elif ts_ok:
        filled_at = datetime.fromtimestamp(ts, timezone.utc)

    qty = parse_count_fp(raw.get("count_fp"))
    fee = _dec(raw.get("fee_cost")) if isinstance(raw.get("fee_cost"), str) else None
    sub = raw.get("subaccount_number")
    order_id = raw.get("order_id")
    rec = {
        "fill_id": fid,
        "order_id": order_id if isinstance(order_id, str) and order_id else None,
        "ticker": raw.get("ticker") or raw.get("market_ticker"),
        "qty": str(qty) if qty is not None else None,
        "filled_at": filled_at.isoformat() if filled_at is not None else None,
        "fee_usd": str(fee) if fee is not None else None,
        "subaccount_number": sub if isinstance(sub, int) and not isinstance(sub, bool) else None,
        "account_ref": account_ref,
        "tier": tier,
    }
    return rec, problems


def _canonical(rec: dict) -> tuple:
    return tuple(rec[k] for k in ("order_id", "ticker", "qty", "filled_at", "fee_usd", "subaccount_number"))


# ---------------------------------------------------------------------------
# Collector
# ---------------------------------------------------------------------------


@dataclass
class KalshiCollection:
    """What the venue said for one interval, and whether that is complete."""

    fills: list[dict]
    coverage: dict
    gaps: list[str]
    evidence: dict = field(default_factory=dict)


class KalshiFillCollector:
    """Collects fills for [start, end) from the live and historical tiers.

    get_json: ``read_only_transport(client)`` or a test double.
    api_key_id: the running credential's key id (only its fingerprint is kept).
    """

    def __init__(self, get_json, api_key_id: str | None, scope: KalshiScope, *,
                 page_limit: int = MAX_PAGE_LIMIT, max_pages: int = 200, max_attempts: int = 3,
                 backoff_seconds: float = 1.0, cutoff_attempts: int = 2,
                 finality_lag_seconds: int = 900, sleep=time.sleep, clock=time.time):
        if _positive_int("page_limit", page_limit) > MAX_PAGE_LIMIT:
            raise ValueError(f"page_limit must be 1..{MAX_PAGE_LIMIT}")
        self._get_json = get_json
        self._fingerprint = key_fingerprint(api_key_id) if api_key_id else None
        self._scope = scope
        self._page_limit = page_limit
        self._max_pages = _positive_int("max_pages", max_pages)
        self._max_attempts = _positive_int("max_attempts", max_attempts)
        self._backoff = _finite_number("backoff_seconds", backoff_seconds, minimum=0)
        self._cutoff_attempts = _positive_int("cutoff_attempts", cutoff_attempts)
        # A negative lag would let an unfinished interval pass as final.
        self._finality_lag = _finite_number("finality_lag_seconds", finality_lag_seconds, minimum=0)
        self._sleep = sleep
        self._clock = clock

    @property
    def source_label(self) -> str:
        return f"kalshi:fills:{self._scope.key_fingerprint}:{self._scope.coverage_scope}"

    # -- HTTP -----------------------------------------------------------------

    def _get(self, path: str, params: dict, requests_log: list) -> tuple[dict | None, str | None]:
        """One logical GET with bounded retry on 429/5xx/transport errors."""
        last = None
        for attempt in range(self._max_attempts):
            status, body, err = self._get_json(path, params)
            requests_log.append({"path": path, "status": status, "error": err})
            if status == 200:
                return body, None
            if status is not None and status not in _RETRYABLE_STATUSES:
                return None, "venue_request_rejected"
            last = "venue_request_failed"
            if attempt + 1 < self._max_attempts:
                self._sleep(self._backoff * (2 ** attempt))
        return None, last

    def _cutoff(self, requests_log: list) -> tuple[datetime | None, str | None]:
        body, err = self._get(CUTOFF_PATH, {}, requests_log)
        if err:
            return None, "venue_cutoff_unavailable"
        cutoff = _parse_utc(body.get("trades_created_ts")) if isinstance(body, dict) else None
        if cutoff is None:
            return None, "venue_cutoff_unavailable"
        return cutoff, None

    def _paginate(self, path: str, min_ts: int, max_ts: int, requests_log: list) -> dict:
        base = {"limit": self._page_limit, "min_ts": min_ts, "max_ts": max_ts}
        if self._scope.subaccount is not None:
            base["subaccount"] = self._scope.subaccount
        out = {"path": path, "pages": 0, "records": [], "empty_pages_with_cursor": 0, "error": None}
        cursor = ""
        seen: set[str] = set()
        while True:
            if out["pages"] >= self._max_pages:
                out["error"] = "venue_page_limit_exhausted"
                return out
            params = dict(base)
            if cursor:
                params["cursor"] = cursor
            body, err = self._get(path, params, requests_log)
            if err:
                out["error"] = err
                return out
            if (not isinstance(body, dict) or not isinstance(body.get("fills"), list)
                    or not isinstance(body.get("cursor"), str)
                    or not all(isinstance(f, dict) for f in body["fills"])):
                out["error"] = "venue_page_malformed"
                return out
            out["pages"] += 1
            out["records"].extend(body["fills"])
            nxt = body["cursor"]
            if not nxt:
                return out
            if not body["fills"]:
                # An empty page with a cursor is not the end; keep walking.
                out["empty_pages_with_cursor"] += 1
            if nxt == cursor or nxt in seen:
                out["error"] = "venue_cursor_repeated"
                return out
            seen.add(nxt)
            cursor = nxt

    # -- collection -------------------------------------------------------------

    def collect(self, interval_start, interval_end) -> KalshiCollection:
        gaps: set[str] = set()
        requests_log: list = []
        start = _parse_utc(interval_start)
        end = _parse_utc(interval_end)
        scope = self._scope
        evidence = {
            "collector": COLLECTOR_NAME,
            "collector_version": COLLECTOR_VERSION,
            "coverage_scope": scope.coverage_scope,
            "key_fingerprint": scope.key_fingerprint,
            "scope_verified_by": scope.verified_by,
            "scope_verified_on": scope.verified_on,
            "collected_at": datetime.fromtimestamp(self._clock(), timezone.utc).isoformat(),
        }
        if start is None or end is None or end <= start:
            gaps.add("invalid_interval")
            return self._result([], start, end, gaps, evidence, requests_log, [])
        if self._fingerprint != scope.key_fingerprint:
            gaps.add("venue_credential_scope_mismatch")
        if self._clock() < end.timestamp() + self._finality_lag:
            gaps.add("venue_interval_not_final")

        min_ts = math.floor(start.timestamp()) - 1
        max_ts = math.ceil(end.timestamp()) + 1
        tiers: list[dict] = []
        cutoffs: list[str] = []
        settled = False
        for _ in range(self._cutoff_attempts):
            before, err = self._cutoff(requests_log)
            if err:
                gaps.add(err)
                break
            paths = []
            if start < before:
                paths.append(HISTORICAL_FILLS_PATH)
            if end > before:
                paths.append(LIVE_FILLS_PATH)
            tiers = [self._paginate(p, min_ts, max_ts, requests_log) for p in paths]
            after, err = self._cutoff(requests_log)
            if err:
                gaps.add(err)
                break
            cutoffs.append(before.isoformat())
            if after == before:
                settled = True
                break
            cutoffs.append(after.isoformat())
        else:
            gaps.add("venue_cutoff_moved")
        evidence["cutoffs_trades_created_ts"] = cutoffs
        evidence["cutoff_stable"] = settled
        for tier in tiers:
            if tier["error"]:
                gaps.add(tier["error"])
        return self._result(tiers, start, end, gaps, evidence, requests_log, cutoffs)

    def _result(self, tiers, start, end, gaps, evidence, requests_log, cutoffs) -> KalshiCollection:
        scope = self._scope
        fills: list[dict] = []
        by_id: dict[str, tuple] = {}
        duplicates = conflicts = 0
        for tier in tiers:
            tier_name = "historical" if tier["path"] == HISTORICAL_FILLS_PATH else "live"
            for raw in tier["records"]:
                rec, problems = normalize_fill(raw, scope.account_ref, tier_name)
                filled_at = _parse_utc(rec["filled_at"])
                # Over-fetched neighbours outside [start, end) do not count, but a
                # record whose time is unknown might be inside, so its problems do.
                if filled_at is None or (start is not None and end is not None and start <= filled_at < end):
                    gaps.update(problems)
                sub = rec["subaccount_number"]
                if scope.subaccount is not None and sub is not None and sub != scope.subaccount:
                    gaps.add("venue_record_outside_scope")
                fid = rec["fill_id"]
                if fid is not None:
                    canon = _canonical(rec)
                    if fid in by_id:
                        if by_id[fid] == canon:
                            duplicates += 1
                        else:
                            conflicts += 1
                            gaps.add("venue_conflicting_duplicate_fill")
                        continue
                    by_id[fid] = canon
                fills.append(rec)
        evidence["tiers"] = [
            {"path": t["path"], "pages": t["pages"], "records": len(t["records"]),
             "empty_pages_with_cursor": t["empty_pages_with_cursor"], "error": t["error"]}
            for t in tiers]
        evidence["duplicate_fills_dropped"] = duplicates
        evidence["conflicting_duplicate_fills"] = conflicts
        evidence["requests"] = len(requests_log)
        evidence["failed_requests"] = sum(1 for r in requests_log if r["status"] != 200)
        coverage = {
            "account_ref": scope.account_ref,
            "interval_start": start.isoformat() if start else None,
            "interval_end": end.isoformat() if end else None,
            "complete": not gaps,
            "source": self.source_label,
            "coverage_scope": scope.coverage_scope,
        }
        return KalshiCollection(fills=fills, coverage=coverage, gaps=sorted(gaps), evidence=evidence)
