"""Tests for the read-only Kalshi fill collector and the venue reconciliation job.

Offline only: Kalshi responses come from spec-shaped fixtures
(tests/fixtures/kalshi_fills, Kalshi Trade API OpenAPI 3.31.0) or small
in-test pages; the ledger mirror is an in-memory fake. Nothing here reaches a
network, a trading path or the local SQLite ledger of a service.
"""

import copy
import inspect
import json
import os
import re
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import kalshi_fill_collector  # noqa: E402
import venue_reconciliation  # noqa: E402

KalshiFillCollector = kalshi_fill_collector.KalshiFillCollector
ReadOnlyViolation = kalshi_fill_collector.ReadOnlyViolation
key_fingerprint = kalshi_fill_collector.key_fingerprint
normalize_fill = kalshi_fill_collector.normalize_fill
parse_count_fp = kalshi_fill_collector.parse_count_fp
parse_kalshi_scope = kalshi_fill_collector.parse_kalshi_scope
read_only_transport = kalshi_fill_collector.read_only_transport
LIVE = kalshi_fill_collector.LIVE_FILLS_PATH
HIST = kalshi_fill_collector.HISTORICAL_FILLS_PATH
CUTOFF = kalshi_fill_collector.CUTOFF_PATH
MirrorReadError = venue_reconciliation.MirrorReadError
MirrorChangedError = venue_reconciliation.MirrorChangedError
PostgrestLedgerMirror = venue_reconciliation.PostgrestLedgerMirror
check_mirror_sources = venue_reconciliation.check_mirror_sources
reporting_day_bounds = venue_reconciliation.reporting_day_bounds
run_reconciliation = venue_reconciliation.run_reconciliation

FIXTURES = Path(__file__).parent / "fixtures" / "kalshi_fills"
KEY_ID = "test-key-id-not-secret"
DAY = date(2026, 9, 28)                      # Detroit day: 04:00Z .. next 04:00Z
DAY_START = datetime(2026, 9, 28, 4, tzinfo=timezone.utc)
DAY_END = datetime(2026, 9, 29, 4, tzinfo=timezone.utc)
AFTER_DAY = DAY_END.timestamp() + 3600      # one hour after the day: final
SCOPE_RAW = {
    "account_ref": "kalshi-main",
    "key_fingerprint": key_fingerprint(KEY_ID),
    "subaccount": "all",
    "ledger_services": ["arb-scanner", "kalshi-mm-pilot"],
    "verified_by": "operator",
    "verified_on": "2026-09-29",
}


def _fixture(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text())


def _scope(**overrides):
    raw = dict(SCOPE_RAW)
    raw.update(overrides)
    return parse_kalshi_scope(raw)


def _fill(fid, order_id, count_fp, created, **extra):
    rec = {"fill_id": fid, "trade_id": fid, "exchange_index": 0, "order_id": order_id,
           "ticker": "KXT-A", "market_ticker": "KXT-A", "outcome_side": "yes", "book_side": "bid",
           "count_fp": count_fp, "yes_price_dollars": "0.5000", "no_price_dollars": "0.5000",
           "is_taker": False, "fee_cost": "0.0100", "created_time": created, "subaccount_number": 0}
    rec.update(extra)
    return rec


class FakeVenue:
    """Scripted Kalshi read API. pages[path][cursor] is a body or a list of
    (status, body) attempts; cutoffs is consumed one per cutoff read."""

    def __init__(self, pages=None, cutoffs=None):
        self.pages = pages or {}
        self.cutoffs = list(cutoffs or ["2026-09-20T00:00:00Z"])
        self.calls: list[tuple[str, dict]] = []
        self._attempts: dict = {}

    def get_json(self, path, params):
        self.calls.append((path, dict(params)))
        if path == CUTOFF:
            value = self.cutoffs.pop(0) if len(self.cutoffs) > 1 else self.cutoffs[0]
            if isinstance(value, tuple):
                return value[0], value[1], None
            return 200, {"trades_created_ts": value, "market_settled_ts": value, "orders_updated_ts": value}, None
        script = self.pages.get(path, {}).get(params.get("cursor", ""), {"fills": [], "cursor": ""})
        if isinstance(script, list):
            key = (path, params.get("cursor", ""))
            i = self._attempts.get(key, 0)
            self._attempts[key] = i + 1
            status, body = script[min(i, len(script) - 1)]
            return status, body, None if status is not None else "ConnectionError"
        return 200, copy.deepcopy(script), None

    def paths(self):
        return [p for p, _ in self.calls]


def _collector(venue, scope=None, api_key_id=KEY_ID, clock=None, sleeps=None, **kw):
    sleeps = sleeps if sleeps is not None else []
    return KalshiFillCollector(venue.get_json, api_key_id, scope or _scope(), sleep=sleeps.append,
                               clock=clock or (lambda: AFTER_DAY), **kw)


def _fixture_venue(cutoffs=None):
    return FakeVenue(pages={
        HIST: {"": _fixture("historical_page_1.json")},
        LIVE: {"": _fixture("live_page_1.json"), "c-live-2": _fixture("live_page_2.json")},
    }, cutoffs=cutoffs or [_fixture("cutoff.json")["trades_created_ts"]])


class FakeMirror:
    """In-memory ledger mirror with PostgrestLedgerMirror's read interface."""

    def __init__(self, status=None, counts=None, trades=None, fail=None, after_rows=None):
        self.status = status if status is not None else []
        self.counts = counts or {}
        self.trades = trades or []
        self.fail = fail
        self.after_rows = after_rows  # callable(mirror): a concurrent write after the rows are read
        self.order_lookups: list[list[str]] = []
        self.status_reads = 0
        self.changed_queries: list[dict] = []

    def status_rows(self, services):
        if self.fail:
            raise self.fail
        self.status_reads += 1
        return [dict(r) for r in self.status if r["service"] in services]

    def changed_since(self, venue, since, window_since, window_until, order_ids):
        # window_* are the reporting day widened by the recording window.
        ids = set(order_ids)
        self.changed_queries.append({"since": since, "order_ids": sorted(ids),
                                     "window": (window_since, window_until)})
        n = 0
        for r in self.trades:
            synced = r.get("synced_at")
            if synced is None or datetime.fromisoformat(synced) < since:
                continue
            if not (r.get("venue") == venue or (r.get("deleted") and r.get("venue") is None)):
                continue
            at = r.get("recorded_at")
            if at is None or window_since <= datetime.fromisoformat(at) < window_until or r.get("order_id") in ids:
                n += 1
        return n

    def live_counts(self, service, db_instance_id):
        return self.counts[f"arbgrid:{service}:{db_instance_id}"]

    def _visible(self, venue):
        return [dict(r) for r in self.trades if r.get("venue") == venue]

    def ledger_trades(self, venue, since, until):
        out = []
        for r in self._visible(venue):
            at = r.get("recorded_at")
            if at is None or since <= datetime.fromisoformat(at) < until:
                out.append(r)
        if self.after_rows:
            self.after_rows(self)
        return out

    def ledger_trades_for_orders(self, venue, order_ids):
        self.order_lookups.append(list(order_ids))
        return [r for r in self._visible(venue) if r.get("order_id") in set(order_ids)]


def _status(service, db="db1", success=None, **overrides):
    success = success or (DAY_END + timedelta(minutes=30)).isoformat()
    row = {"source_key": f"arbgrid:{service}:{db}", "service": service, "db_instance_id": db,
           "capture_since": "2026-09-01T00:00:00+00:00", "snapshot_complete": True, "pending_changes": 0,
           "last_attempt_at": success, "last_success_at": success, "last_error": None,
           "local_trades_count": 3, "local_positions_count": 1}
    row.update(overrides)
    return row


def _fresh_mirror(trades=None, after_rows=None, **overrides):
    status = [_status("arb-scanner", **overrides), _status("kalshi-mm-pilot", db="db2")]
    counts = {"arbgrid:arb-scanner:db1": {"trades": 3, "positions": 1},
              "arbgrid:kalshi-mm-pilot:db2": {"trades": 3, "positions": 1}}
    return FakeMirror(status=status, counts=counts, trades=trades or [], after_rows=after_rows)


def _ledger(order_id, qty, recorded="2026-09-28T12:00:00+00:00", service="kalshi-mm-pilot", n=1, **extra):
    row = {"ledger_key": f"arbgrid:{service}:db:trades:{n}", "service": service, "venue": "kalshi",
           "account_ref": "kalshi-main", "run_mode": "live", "deleted": False, "status": "filled",
           "fill_price": 0.5, "fill_qty": qty, "order_id": order_id, "recorded_at": recorded}
    row.update(extra)
    return row


# ---------------------------------------------------------------------------
# Reporting days (America/Detroit)
# ---------------------------------------------------------------------------


class TestReportingDayBounds:
    def test_ordinary_day_is_24_hours_at_edt_offset(self):
        assert reporting_day_bounds(DAY) == (DAY_START, DAY_END)

    def test_spring_forward_day_is_23_hours(self):
        start, end = reporting_day_bounds(date(2026, 3, 8))
        assert start == datetime(2026, 3, 8, 5, tzinfo=timezone.utc)   # EST midnight
        assert end == datetime(2026, 3, 9, 4, tzinfo=timezone.utc)     # EDT midnight
        assert end - start == timedelta(hours=23)

    def test_fall_back_day_is_25_hours(self):
        start, end = reporting_day_bounds(date(2026, 11, 1))
        assert start == datetime(2026, 11, 1, 4, tzinfo=timezone.utc)
        assert end == datetime(2026, 11, 2, 5, tzinfo=timezone.utc)
        assert end - start == timedelta(hours=25)

    def test_consecutive_days_tile_without_gaps(self):
        for d in (date(2026, 3, 7), date(2026, 3, 8), date(2026, 10, 31), date(2026, 11, 1)):
            assert reporting_day_bounds(d)[1] == reporting_day_bounds(d + timedelta(days=1))[0]


# ---------------------------------------------------------------------------
# Declared scope and read-only transport
# ---------------------------------------------------------------------------


class TestScope:
    def test_valid_scope_all_subaccounts(self):
        scope = _scope()
        assert scope.subaccount is None and scope.coverage_scope == "all_subaccounts"
        assert scope.ledger_services == ("arb-scanner", "kalshi-mm-pilot")

    def test_restricted_scope_names_its_subaccount(self):
        assert _scope(subaccount=3).coverage_scope == "subaccount:3"

    @pytest.mark.parametrize("field, value", [
        ("account_ref", ""), ("account_ref", "has space"), ("key_fingerprint", KEY_ID),
        ("key_fingerprint", "sha256:XYZ"), ("subaccount", None), ("subaccount", True), ("subaccount", 64),
        ("subaccount", "0"), ("ledger_services", []), ("ledger_services", ["a", "a"]),
        ("ledger_services", "arb-scanner"), ("verified_by", ""), ("verified_on", "yesterday"),
    ])
    def test_invalid_scope_rejected(self, field, value):
        with pytest.raises(ValueError):
            _scope(**{field: value})

    def test_missing_scope_rejected(self):
        with pytest.raises(ValueError):
            parse_kalshi_scope(None)

    def test_fingerprint_does_not_contain_the_key_id(self):
        fp = key_fingerprint(KEY_ID)
        assert re.fullmatch(r"sha256:[0-9a-f]{16}", fp) and KEY_ID not in fp


class TestReadOnlyTransport:
    @pytest.mark.parametrize("path", ["/portfolio/orders", "/portfolio/events/orders",
                                      "/portfolio/orders/abc", "/portfolio/balance", "/portfolio/fills/x"])
    def test_non_allowlisted_path_is_refused(self, path):
        client = MagicMock()
        with pytest.raises(ReadOnlyViolation):
            read_only_transport(client)(path, {})
        client._request.assert_not_called()

    def test_allowlisted_path_is_a_signed_get(self):
        client = MagicMock()
        client._request.return_value.status_code = 200
        client._request.return_value.json.return_value = {"fills": [], "cursor": ""}
        status, body, err = read_only_transport(client)(LIVE, {"limit": 1})
        client._request.assert_called_once_with("GET", LIVE, params={"limit": 1})
        assert (status, body, err) == (200, {"fills": [], "cursor": ""}, None)

    def test_transport_errors_are_reported_not_raised(self):
        client = MagicMock()
        client._request.side_effect = ConnectionError("boom")
        assert read_only_transport(client)(LIVE, {}) == (None, None, "ConnectionError")
        client._request.side_effect = None
        client._request.return_value = None
        assert read_only_transport(client)(LIVE, {}) == (None, None, "no_response")

    def test_collector_module_has_no_order_mutation_code(self):
        source = inspect.getsource(kalshi_fill_collector)
        for forbidden in ("place_order", "cancel_order", "amend", '"POST"', '"DELETE"', '"PUT"'):
            assert forbidden not in source
        assert kalshi_fill_collector.READ_ONLY_PATHS == {LIVE, HIST, CUTOFF}


# ---------------------------------------------------------------------------
# Record normalization (fixed point, time, identity)
# ---------------------------------------------------------------------------


class TestNormalization:
    @pytest.mark.parametrize("raw, expected", [
        ("10.00", "10.00"), ("2.5", "2.50"), ("0.01", "0.01"), ("3", "3.00"),
        ("2.505", None), ("0", None), ("-1.00", None), ("NaN", None), ("Infinity", None), ("-inf", None),
        ("sNaN", None), ("", None), (2.5, None), (3, None), (None, None), (True, None),
    ])
    def test_count_fp(self, raw, expected):
        got = parse_count_fp(raw)
        assert (str(got) if got is not None else None) == expected

    def test_fee_must_be_a_finite_fixed_point_string(self):
        rec, _ = normalize_fill(_fill("f", "o", "1.00", "2026-09-28T05:00:00Z", fee_cost="NaN"), "a", "live")
        assert rec["fee_usd"] is None
        rec, _ = normalize_fill(_fill("f", "o", "1.00", "2026-09-28T05:00:00Z", fee_cost=0.01), "a", "live")
        assert rec["fee_usd"] is None
        rec, _ = normalize_fill(_fill("f", "o", "1.00", "2026-09-28T05:00:00Z", fee_cost="0.0175"), "a", "live")
        assert rec["fee_usd"] == "0.0175"

    def test_equivalent_offsets_normalize_to_the_same_instant(self):
        a, _ = normalize_fill(_fill("f", "o", "1.00", "2026-09-28T12:00:00-04:00"), "a", "live")
        b, _ = normalize_fill(_fill("f", "o", "1.00", "2026-09-28T16:00:00Z"), "a", "live")
        assert a["filled_at"] == b["filled_at"] == "2026-09-28T16:00:00+00:00"

    def test_legacy_ts_used_only_when_created_time_absent(self):
        raw = _fill("f", "o", "1.00", None, ts=1790568000)
        del raw["created_time"]
        rec, problems = normalize_fill(raw, "a", "live")
        assert rec["filled_at"] == DAY_START.isoformat() and not problems

    def test_conflicting_time_fields_are_unknown(self):
        rec, problems = normalize_fill(_fill("f", "o", "1.00", "2026-09-28T05:00:00Z", ts=1790568000), "a", "live")
        assert rec["filled_at"] is None and "venue_record_time_conflict" in problems

    def test_naive_or_invalid_created_time_is_unknown(self):
        for bad in ("2026-09-28T05:00:00", "not-a-time"):
            rec, problems = normalize_fill(_fill("f", "o", "1.00", bad), "a", "live")
            assert rec["filled_at"] is None and "venue_record_time_invalid" in problems

    @pytest.mark.parametrize("ts", [10 ** 20, -(10 ** 20), 2 ** 63, 253402300800, -1])
    def test_out_of_range_ts_is_an_explicit_problem_not_a_crash(self, ts):
        raw = _fill("f1", "o1", "1.00", None, ts=ts)
        del raw["created_time"]
        rec, problems = normalize_fill(raw, "a", "live")
        assert rec["filled_at"] is None and "venue_record_time_invalid" in problems

    @pytest.mark.parametrize("created", ["0001-01-01T00:00:00+01:00", "9999-12-31T23:59:59-05:00"])
    def test_created_time_without_a_utc_instant_is_invalid(self, created):
        rec, problems = normalize_fill(_fill("f1", "o1", "1.00", created), "a", "live")
        assert rec["filled_at"] is None and "venue_record_time_invalid" in problems

    def test_fill_id_and_legacy_trade_id_must_agree(self):
        _, problems = normalize_fill(_fill("f1", "o", "1.00", "2026-09-28T05:00:00Z", trade_id="f2"), "a", "live")
        assert "venue_record_id_conflict" in problems
        raw = _fill(None, "o", "1.00", "2026-09-28T05:00:00Z", trade_id="legacy")
        rec, problems = normalize_fill(raw, "a", "live")
        assert rec["fill_id"] == "legacy" and not problems
        raw = _fill(None, "o", "1.00", "2026-09-28T05:00:00Z", trade_id=None)
        rec, problems = normalize_fill(raw, "a", "live")
        assert rec["fill_id"] is None and "venue_record_missing_fill_id" in problems


# ---------------------------------------------------------------------------
# Collector: tiers, cutoff, pagination, retries
# ---------------------------------------------------------------------------


class TestCollectorFixtures:
    def test_cutoff_spanning_day_reads_both_tiers_completely(self):
        venue = _fixture_venue()
        col = _collector(venue).collect(DAY_START, DAY_END)
        assert col.gaps == [] and col.coverage["complete"] is True
        assert col.coverage["account_ref"] == "kalshi-main"
        assert col.coverage["coverage_scope"] == "all_subaccounts"
        assert venue.paths() == [CUTOFF, HIST, LIVE, LIVE, CUTOFF]
        ids = [f["fill_id"] for f in col.fills]
        assert ids == ["f-hist-edge", "f-hist-start", "f-overlap", "f-live-b", "f-live-end-in", "f-live-end-out"]
        assert col.evidence["duplicate_fills_dropped"] == 1   # f-overlap in both tiers, same instant
        assert col.evidence["cutoff_stable"] is True

    def test_query_bounds_overfetch_one_second_and_omit_subaccount_for_all(self):
        venue = _fixture_venue()
        _collector(venue).collect(DAY_START, DAY_END)
        params = [p for path, p in venue.calls if path in (LIVE, HIST)]
        for p in params:
            assert p["min_ts"] == int(DAY_START.timestamp()) - 1
            assert p["max_ts"] == int(DAY_END.timestamp()) + 1
            assert p["limit"] == 1000 and "subaccount" not in p
        assert params[2]["cursor"] == "c-live-2"

    def test_exact_boundaries_are_filtered_by_reconciliation(self):
        venue = _fixture_venue()
        record = run_reconciliation(_collector(venue), _scope(), _fresh_mirror(trades=[
            _ledger("ord-a", 2.5, n=1), _ledger("ord-b", 10, n=2), _ledger("ord-c", 0.5, n=3)]),
            DAY, venue="kalshi", clock=lambda: AFTER_DAY)
        # 03:59:59.5Z and 04:00:00Z-next-day fall outside [start, end); 04:00:00Z and
        # 03:59:59.999999Z-next-day fall inside.
        assert record["venue_records_out_of_interval"] == 2
        assert record["status"] == "matched", record
        assert record["venue_order_count"] == 3 and record["venue_fees_usd"] == "0.1800"

    def test_day_after_cutoff_reads_live_only(self):
        venue = FakeVenue(cutoffs=["2026-09-01T00:00:00Z"])
        col = _collector(venue).collect(DAY_START, DAY_END)
        assert venue.paths() == [CUTOFF, LIVE, CUTOFF] and col.coverage["complete"]

    def test_day_before_cutoff_reads_historical_only(self):
        venue = FakeVenue(cutoffs=["2026-09-29T12:00:00Z"])
        col = _collector(venue).collect(DAY_START, DAY_END)
        assert venue.paths() == [CUTOFF, HIST, CUTOFF] and col.coverage["complete"]

    def test_cutoff_moving_once_is_retried(self):
        venue = FakeVenue(cutoffs=["2026-09-28T10:00:00Z", "2026-09-28T11:00:00Z",
                                   "2026-09-28T11:00:00Z", "2026-09-28T11:00:00Z"])
        col = _collector(venue).collect(DAY_START, DAY_END)
        assert col.gaps == [] and col.evidence["cutoff_stable"] is True
        assert venue.paths().count(CUTOFF) == 4

    def test_cutoff_that_keeps_moving_is_incomplete(self):
        venue = FakeVenue(cutoffs=["2026-09-28T10:00:00Z", "2026-09-28T11:00:00Z",
                                   "2026-09-28T12:00:00Z", "2026-09-28T13:00:00Z"])
        col = _collector(venue).collect(DAY_START, DAY_END)
        assert "venue_cutoff_moved" in col.gaps and col.coverage["complete"] is False

    def test_cutoff_unavailable_is_incomplete(self):
        venue = FakeVenue(cutoffs=[(500, None)])
        col = _collector(venue).collect(DAY_START, DAY_END)
        assert "venue_cutoff_unavailable" in col.gaps and not col.fills


class TestCollectorPagination:
    def _one_tier(self, pages, **kw):
        venue = FakeVenue(pages={LIVE: pages}, cutoffs=["2026-09-01T00:00:00Z"])
        return venue, _collector(venue, **kw).collect(DAY_START, DAY_END)

    def test_empty_page_with_cursor_is_not_the_end(self):
        venue, col = self._one_tier({
            "": {"fills": [], "cursor": "c2"},
            "c2": {"fills": [_fill("f1", "o1", "1.00", "2026-09-28T05:00:00Z")], "cursor": ""},
        })
        assert col.gaps == [] and [f["fill_id"] for f in col.fills] == ["f1"]
        assert col.evidence["tiers"][0]["empty_pages_with_cursor"] == 1

    def test_repeated_cursor_is_incomplete(self):
        _, col = self._one_tier({
            "": {"fills": [_fill("f1", "o1", "1.00", "2026-09-28T05:00:00Z")], "cursor": "c2"},
            "c2": {"fills": [_fill("f2", "o1", "1.00", "2026-09-28T06:00:00Z")], "cursor": "c2"},
        })
        assert "venue_cursor_repeated" in col.gaps and col.coverage["complete"] is False

    def test_page_limit_exhaustion_is_incomplete(self):
        venue, col = self._one_tier({
            "": {"fills": [_fill("f1", "o1", "1.00", "2026-09-28T05:00:00Z")], "cursor": "c2"},
            "c2": {"fills": [_fill("f2", "o1", "1.00", "2026-09-28T06:00:00Z")], "cursor": "c3"},
            "c3": {"fills": [], "cursor": ""},
        }, max_pages=2)
        assert "venue_page_limit_exhausted" in col.gaps
        assert [f["fill_id"] for f in col.fills] == ["f1", "f2"]   # kept, but not coverage

    def test_failed_later_page_after_bounded_retries_is_incomplete(self):
        sleeps: list = []
        venue = FakeVenue(pages={LIVE: {
            "": {"fills": [_fill("f1", "o1", "1.00", "2026-09-28T05:00:00Z")], "cursor": "c2"},
            "c2": [(500, None)],
        }}, cutoffs=["2026-09-01T00:00:00Z"])
        col = _collector(venue, sleeps=sleeps).collect(DAY_START, DAY_END)
        assert "venue_request_failed" in col.gaps and col.coverage["complete"] is False
        assert sum(1 for p, q in venue.calls if q.get("cursor") == "c2") == 3
        assert sleeps == [1.0, 2.0]
        assert col.evidence["failed_requests"] == 3

    def test_transient_errors_then_success_is_complete(self):
        sleeps: list = []
        venue = FakeVenue(pages={LIVE: {
            "": [(503, None), (None, None), (200, {"fills": [], "cursor": ""})],
        }}, cutoffs=["2026-09-01T00:00:00Z"])
        col = _collector(venue, sleeps=sleeps).collect(DAY_START, DAY_END)
        assert col.gaps == [] and sleeps == [1.0, 2.0]

    @pytest.mark.parametrize("status", [400, 401, 403, 404])
    def test_client_errors_are_not_retried(self, status):
        venue = FakeVenue(pages={LIVE: {"": [(status, {"code": "x"})]}}, cutoffs=["2026-09-01T00:00:00Z"])
        col = _collector(venue).collect(DAY_START, DAY_END)
        assert "venue_request_rejected" in col.gaps
        assert venue.paths().count(LIVE) == 1

    @pytest.mark.parametrize("body", [
        {"fills": []}, {"cursor": ""}, {"fills": None, "cursor": ""}, {"fills": [], "cursor": None},
        {"fills": ["x"], "cursor": ""}, ["not", "a", "dict"],
    ])
    def test_malformed_page_is_incomplete(self, body):
        _, col = self._one_tier({"": [(200, body)]})
        assert "venue_page_malformed" in col.gaps


class TestCollectorIntegrity:
    def _collect(self, fills, scope=None, **kw):
        venue = FakeVenue(pages={LIVE: {"": {"fills": fills, "cursor": ""}}}, cutoffs=["2026-09-01T00:00:00Z"])
        return venue, _collector(venue, scope=scope, **kw).collect(DAY_START, DAY_END)

    def test_identical_duplicate_is_dropped(self):
        f = _fill("f1", "o1", "1.00", "2026-09-28T05:00:00Z")
        _, col = self._collect([f, copy.deepcopy(f)])
        assert len(col.fills) == 1 and col.gaps == []

    def test_conflicting_duplicate_is_incomplete(self):
        _, col = self._collect([_fill("f1", "o1", "1.00", "2026-09-28T05:00:00Z"),
                                _fill("f1", "o1", "2.00", "2026-09-28T05:00:00Z")])
        assert "venue_conflicting_duplicate_fill" in col.gaps
        assert col.evidence["conflicting_duplicate_fills"] == 1

    def test_restricted_key_queries_its_subaccount_and_rejects_others(self):
        venue, col = self._collect([_fill("f1", "o1", "1.00", "2026-09-28T05:00:00Z", subaccount_number=0),
                                    _fill("f2", "o2", "1.00", "2026-09-28T05:00:00Z", subaccount_number=2)],
                                   scope=_scope(subaccount=0))
        assert all(p.get("subaccount") == 0 for path, p in venue.calls if path == LIVE)
        assert "venue_record_outside_scope" in col.gaps
        assert col.coverage["coverage_scope"] == "subaccount:0"

    def test_restricted_scope_never_claims_all_subaccounts(self):
        _, col = self._collect([], scope=_scope(subaccount=0))
        assert col.coverage["complete"] and col.coverage["coverage_scope"] == "subaccount:0"
        assert "subaccount:0" in col.coverage["source"]

    def test_credential_not_matching_declared_scope_is_incomplete(self):
        _, col = self._collect([], api_key_id="some-other-key")
        assert "venue_credential_scope_mismatch" in col.gaps
        _, col = self._collect([], api_key_id=None)
        assert "venue_credential_scope_mismatch" in col.gaps

    def test_interval_not_yet_final_is_stale_coverage(self):
        _, col = self._collect([], clock=lambda: DAY_END.timestamp() + 60)
        assert "venue_interval_not_final" in col.gaps and col.coverage["complete"] is False

    def test_problems_on_overfetched_neighbours_do_not_count(self):
        _, col = self._collect([_fill("f0", "o0", "NaN", "2026-09-28T03:59:59.9Z")])
        assert col.gaps == []

    def test_out_of_range_ts_makes_the_day_incomplete_not_a_crash(self):
        raw = _fill("f1", "o1", "1.00", None, ts=10 ** 20)
        del raw["created_time"]
        _, col = self._collect([raw])
        assert "venue_record_time_invalid" in col.gaps and col.coverage["complete"] is False

    @pytest.mark.parametrize("kw", [
        {"finality_lag_seconds": -1}, {"finality_lag_seconds": float("nan")},
        {"finality_lag_seconds": float("inf")}, {"finality_lag_seconds": "900"},
        {"backoff_seconds": -0.5}, {"backoff_seconds": float("nan")}, {"backoff_seconds": float("inf")},
        {"max_attempts": 0}, {"max_attempts": True}, {"max_attempts": 1.5},
        {"max_pages": 0}, {"cutoff_attempts": 0}, {"page_limit": 0}, {"page_limit": 1001},
    ])
    def test_invalid_settings_are_refused(self, kw):
        with pytest.raises(ValueError):
            _collector(FakeVenue(), **kw)

    def test_zero_lag_and_zero_backoff_are_allowed(self):
        _collector(FakeVenue(), finality_lag_seconds=0, backoff_seconds=0)

    def test_invalid_interval(self):
        venue = FakeVenue()
        col = _collector(venue).collect(DAY_END, DAY_START)
        assert col.gaps == ["invalid_interval"] and venue.calls == []


# ---------------------------------------------------------------------------
# Ledger mirror verification
# ---------------------------------------------------------------------------


class TestMirrorSources:
    NOW = DAY_END + timedelta(hours=1)

    def _check(self, status, counts=None, services=("arb-scanner",), now=None, **kw):
        counts = counts if counts is not None else {r["source_key"]: {"trades": 3, "positions": 1} for r in status}
        gaps, _ = check_mirror_sources(status, counts, services, DAY_START, DAY_END, now=now or self.NOW,
                                       finality_lag_seconds=kw.pop("finality_lag_seconds", 900), **kw)
        return gaps

    def test_fresh_complete_source_has_no_gaps(self):
        assert self._check([_status("arb-scanner")]) == set()

    def test_missing_mapped_source(self):
        assert self._check([], services=("arb-scanner",)) == {"ledger_mirror_source_missing"}

    def test_stale_source(self):
        assert "ledger_mirror_stale" in self._check([_status("arb-scanner", success=DAY_END.isoformat())])

    def test_pending_or_unfinished_snapshot(self):
        assert "ledger_mirror_incomplete" in self._check([_status("arb-scanner", pending_changes=4)])
        assert "ledger_mirror_incomplete" in self._check([_status("arb-scanner", snapshot_complete=False)])
        assert "ledger_mirror_incomplete" in self._check([_status("arb-scanner", pending_changes=None)])

    def test_failing_latest_attempt(self):
        later = (DAY_END + timedelta(hours=1)).isoformat()
        assert "ledger_mirror_sync_failing" in self._check([_status("arb-scanner", last_attempt_at=later)])
        assert "ledger_mirror_sync_failing" in self._check([_status("arb-scanner", last_error="RuntimeError")])

    def test_count_mismatch_or_unreadable_counts(self):
        row = _status("arb-scanner")
        assert "ledger_mirror_count_mismatch" in self._check([row], {row["source_key"]: {"trades": 2,
                                                                                         "positions": 1}})
        assert "ledger_mirror_count_unavailable" in self._check([row], {row["source_key"]: None})

    def test_capture_started_inside_the_interval(self):
        row = _status("arb-scanner", capture_since=(DAY_START + timedelta(hours=3)).isoformat())
        assert "ledger_capture_not_covering_interval" in self._check([row])

    def test_source_that_stopped_syncing_is_not_recent_even_for_an_old_day(self):
        # Fresh past the day's end, but the exporter has been silent for two
        # hours since: later corrections may be sitting unexported.
        row = _status("arb-scanner")
        later = DAY_END + timedelta(minutes=30) + timedelta(hours=2, seconds=1)
        gaps = self._check([row], now=later)
        assert gaps == {"ledger_mirror_source_not_recent"}
        assert self._check([row], now=later, max_source_age_seconds=3 * 3600) == set()

    @pytest.mark.parametrize("kw", [
        {"finality_lag_seconds": -1}, {"finality_lag_seconds": float("nan")},
        {"finality_lag_seconds": float("inf")}, {"finality_lag_seconds": True},
        {"max_source_age_seconds": 0}, {"max_source_age_seconds": -5},
        {"max_source_age_seconds": float("nan")}, {"max_source_age_seconds": float("inf")},
    ])
    def test_invalid_settings_are_refused(self, kw):
        with pytest.raises(ValueError):
            self._check([_status("arb-scanner")], **kw)

    def test_new_db_instance_during_interval(self):
        old = _status("arb-scanner", db="old", success=(DAY_START + timedelta(hours=1)).isoformat())
        new = _status("arb-scanner", db="new")
        assert "ledger_mirror_source_changed" in self._check([old, new])
        retired = _status("arb-scanner", db="old", success=(DAY_START - timedelta(days=3)).isoformat())
        assert "ledger_mirror_source_changed" not in self._check([retired, new])


# ---------------------------------------------------------------------------
# End-to-end reconciliation runs
# ---------------------------------------------------------------------------


def _run(fills=None, mirror=None, scope=None, clock=lambda: AFTER_DAY, venue=None, day=DAY, **kw):
    venue = venue or FakeVenue(pages={LIVE: {"": {"fills": fills or [], "cursor": ""}}},
                               cutoffs=["2026-09-01T00:00:00Z"])
    scope = scope or _scope()
    return run_reconciliation(_collector(venue, scope=scope, clock=clock), scope,
                              mirror if mirror is not None else _fresh_mirror(), day,
                              venue="kalshi", clock=clock, **kw)


class TestRunReconciliation:
    def test_verified_quiet_day_is_matched_zero(self):
        rec = _run()
        assert rec["status"] == "matched" and rec["incomplete_reasons"] == []
        assert (rec["venue_order_count"], rec["ledger_order_count"]) == (0, 0)
        assert rec["coverage_verified"] and rec["ledger_mirror_verified"]
        assert rec["coverage_scope"] == "all_subaccounts" and rec["reporting_tz"] == "America/Detroit"
        assert rec["reporting_day"] == "2026-09-28"
        assert rec["interval_start"] == DAY_START.isoformat() and rec["interval_end"] == DAY_END.isoformat()

    def test_empty_mirror_never_reconciles_zero(self):
        rec = _run(mirror=FakeMirror())
        assert rec["status"] == "incomplete"
        assert "ledger_mirror_source_missing" in rec["incomplete_reasons"]
        assert not rec["ledger_mirror_verified"]

    def test_stale_mirror_never_reconciles_zero(self):
        rec = _run(mirror=_fresh_mirror(success=(DAY_END - timedelta(hours=1)).isoformat()))
        assert rec["status"] == "incomplete" and "ledger_mirror_stale" in rec["incomplete_reasons"]

    def test_truncated_mirror_read(self):
        rec = _run(mirror=FakeMirror(fail=MirrorReadError("ledger_trades: read 1000 of 1500 rows")))
        assert rec["status"] == "incomplete" and "ledger_mirror_read_truncated" in rec["incomplete_reasons"]

    def test_unreadable_mirror(self):
        rec = _run(mirror=FakeMirror(fail=RuntimeError("503")))
        assert rec["status"] == "incomplete" and "ledger_mirror_unreadable" in rec["incomplete_reasons"]

    def test_partial_venue_pagination_never_matches(self):
        venue = FakeVenue(pages={LIVE: {
            "": {"fills": [_fill("f1", "o1", "1.00", "2026-09-28T05:00:00Z")], "cursor": "c2"},
            "c2": [(502, None)],
        }}, cutoffs=["2026-09-01T00:00:00Z"])
        rec = _run(venue=venue, mirror=_fresh_mirror(trades=[_ledger("o1", 1.0)]))
        assert rec["status"] == "incomplete" and not rec["coverage_verified"]
        assert {"venue_request_failed", "venue_coverage_unverified"} <= set(rec["incomplete_reasons"])

    def test_fractional_fills_sum_per_order_and_match(self):
        fills = [_fill("f1", "o1", "1.25", "2026-09-28T05:00:00Z"),
                 _fill("f2", "o1", "1.25", "2026-09-28T06:00:00Z")]
        rec = _run(fills, mirror=_fresh_mirror(trades=[_ledger("o1", 2.5)]))
        assert rec["status"] == "matched" and rec["matched_order_count"] == 1

    def test_quantity_disagreement_is_mismatched(self):
        rec = _run([_fill("f1", "o1", "2.50", "2026-09-28T05:00:00Z")],
                   mirror=_fresh_mirror(trades=[_ledger("o1", 2.0)]))
        assert rec["status"] == "mismatched" and rec["qty_mismatch"] == ["o1"]

    def test_venue_fill_missing_from_ledger_is_mismatched(self):
        rec = _run([_fill("f1", "o1", "1.00", "2026-09-28T05:00:00Z")])
        assert rec["status"] == "mismatched" and rec["missing_in_ledger"] == ["o1"]

    def test_ledger_correction_then_rerun_matches(self):
        mirror = _fresh_mirror(trades=[_ledger("o1", 1.0)])
        fills = [_fill("f1", "o1", "3.00", "2026-09-28T05:00:00Z")]
        first = _run(fills, mirror=mirror, run_id="r1")
        mirror.trades[0]["fill_qty"] = 3.0   # the exporter mirrored a fill correction
        second = _run(fills, mirror=mirror, run_id="r2")
        assert (first["status"], second["status"]) == ("mismatched", "matched")
        assert first["run_id"] != second["run_id"]

    def test_unknown_account_ledger_fill_is_incomplete(self):
        rec = _run(mirror=_fresh_mirror(trades=[_ledger("o9", 1.0, account_ref=None)]))
        assert rec["status"] == "incomplete" and "ledger_fill_unattributed" in rec["incomplete_reasons"]

    def test_mixed_modes_paper_ignored_unknown_incomplete(self):
        rec = _run(mirror=_fresh_mirror(trades=[_ledger("dry_1", 1.0, run_mode="paper")]))
        assert rec["status"] == "matched"
        rec = _run(mirror=_fresh_mirror(trades=[_ledger("o2", 1.0, run_mode="unknown")]))
        assert rec["status"] == "incomplete" and "ledger_fill_unattributed" in rec["incomplete_reasons"]

    def test_unmapped_service_writing_the_account_is_incomplete(self):
        rec = _run(mirror=_fresh_mirror(trades=[_ledger("o1", 1.0, service="rogue-worker")]),
                   fills=[_fill("f1", "o1", "1.00", "2026-09-28T05:00:00Z")])
        assert rec["status"] == "incomplete" and "ledger_unmapped_source" in rec["incomplete_reasons"]
        assert rec["evidence"]["unmapped_ledger_services"] == ["rogue-worker"]

    def test_order_logged_days_before_its_venue_fill_is_found_and_not_counted(self):
        # A ledger row cannot record a fill that happens days later, so the
        # day's venue fill has no ledger counterpart.
        mirror = _fresh_mirror(trades=[_ledger("o-old", 2.0, recorded="2026-09-20T12:00:00+00:00")])
        rec = _run([_fill("f1", "o-old", "2.00", "2026-09-28T05:00:00Z")], mirror=mirror)
        assert mirror.order_lookups == [["o-old"]]
        assert rec["status"] == "mismatched" and rec["missing_in_ledger"] == ["o-old"]

    def test_restricted_key_match_is_scoped_to_its_subaccount(self):
        rec = _run(scope=_scope(subaccount=0))
        assert rec["status"] == "matched" and rec["coverage_scope"] == "subaccount:0"

    def test_fall_back_day_includes_the_extra_hour(self):
        day = date(2026, 11, 1)
        late = "2026-11-02T04:30:00Z"   # 23:30 EST on Nov 1
        clock = lambda: datetime(2026, 11, 2, 8, tzinfo=timezone.utc).timestamp()  # noqa: E731
        mirror = _fresh_mirror(trades=[_ledger("o1", 1.0, recorded="2026-11-02T04:29:00+00:00")],
                               success=datetime(2026, 11, 2, 7, tzinfo=timezone.utc).isoformat())
        mirror.status[1]["last_success_at"] = mirror.status[1]["last_attempt_at"] = mirror.status[0]["last_success_at"]
        rec = _run([_fill("f1", "o1", "1.00", late)], mirror=mirror, day=day, clock=clock)
        assert rec["status"] == "matched", rec["incomplete_reasons"]
        assert rec["evidence"]["reporting_day_hours"] == 25

    def test_record_names_what_the_result_depends_on(self):
        mirror = _fresh_mirror(trades=[_ledger("o-old", 2.0, recorded="2026-09-20T12:00:00+00:00"),
                                       _ledger("o2", 1.0, n=2)])
        rec = _run([_fill("f1", "o-old", "2.00", "2026-09-28T05:00:00Z"),
                    _fill("f2", "o2", "1.00", "2026-09-28T06:00:00Z")], mirror=mirror)
        assert rec["status"] == "mismatched" and rec["missing_in_ledger"] == ["o-old"]
        assert rec["ledger_order_ids"] == ["o-old", "o2"]
        read_started = datetime.fromtimestamp(AFTER_DAY, timezone.utc) - venue_reconciliation.MIRROR_CLOCK_SKEW
        assert rec["ledger_read_started_at"] == read_started.isoformat()
        assert rec["ledger_sources"] == [{"service": "arb-scanner", "source_key": "arbgrid:arb-scanner:db1"},
                                         {"service": "kalshi-mm-pilot", "source_key": "arbgrid:kalshi-mm-pilot:db2"}]
        assert rec["max_source_age_seconds"] == 3600
        assert mirror.status_reads == 2   # fenced: before and after the rows
        assert mirror.changed_queries[0]["order_ids"] == ["o-old", "o2"]

    def test_source_status_moving_during_the_read_is_incomplete(self):
        def export_ran(m):
            m.status[1]["last_success_at"] = m.status[1]["last_attempt_at"] = (
                DAY_END + timedelta(minutes=45)).isoformat()
        rec = _run(mirror=_fresh_mirror(after_rows=export_ran))
        assert rec["status"] == "incomplete"
        assert rec["incomplete_reasons"] == ["ledger_mirror_changed_during_read"]
        assert not rec["ledger_mirror_verified"]

    def test_row_written_during_the_read_is_incomplete(self):
        # Same count, same status (the exporter's status write has not landed
        # yet), but an in-scope row was rewritten while pages were read.
        def correction(m):
            m.trades[0]["fill_qty"] = 5.0
            m.trades[0]["synced_at"] = datetime.fromtimestamp(AFTER_DAY, timezone.utc).isoformat()
        mirror = _fresh_mirror(trades=[_ledger("o1", 1.0, synced_at="2026-09-28T13:00:00+00:00")],
                               after_rows=correction)
        rec = _run([_fill("f1", "o1", "1.00", "2026-09-28T05:00:00Z")], mirror=mirror)
        assert rec["status"] == "incomplete"
        assert "ledger_mirror_changed_during_read" in rec["incomplete_reasons"]

    def test_out_of_window_order_rewritten_during_the_read_is_incomplete(self):
        def correction(m):
            m.trades[0]["synced_at"] = datetime.fromtimestamp(AFTER_DAY, timezone.utc).isoformat()
        mirror = _fresh_mirror(trades=[_ledger("o-old", 2.0, recorded="2026-09-10T12:00:00+00:00")],
                               after_rows=correction)
        rec = _run([_fill("f1", "o-old", "2.00", "2026-09-28T05:00:00Z")], mirror=mirror)
        assert "ledger_mirror_changed_during_read" in rec["incomplete_reasons"]

    def test_tombstone_without_venue_written_during_the_read_is_incomplete(self):
        def delete(m):
            m.trades.append({"ledger_key": "arbgrid:kalshi-mm-pilot:db:trades:9", "service": "kalshi-mm-pilot",
                             "deleted": True, "venue": None, "recorded_at": None,
                             "synced_at": datetime.fromtimestamp(AFTER_DAY, timezone.utc).isoformat()})
        rec = _run(mirror=_fresh_mirror(after_rows=delete))
        assert "ledger_mirror_changed_during_read" in rec["incomplete_reasons"]

    def test_next_days_activity_on_other_orders_is_not_a_change(self):
        # Trading continues after midnight: a new order logged the next day,
        # inside the read window but not part of this day's result, is written
        # while the check reads. It must not make the day incomplete.
        def todays_trade(m):
            m.trades.append(_ledger("o-today", 1.0, recorded="2026-09-29T04:30:00+00:00", n=7,
                                    synced_at=datetime.fromtimestamp(AFTER_DAY, timezone.utc).isoformat()))
        mirror = _fresh_mirror(trades=[_ledger("o1", 1.0)], after_rows=todays_trade)
        rec = _run([_fill("f1", "o1", "1.00", "2026-09-28T05:00:00Z")], mirror=mirror)
        assert rec["status"] == "matched", rec["incomplete_reasons"]
        assert rec["ledger_order_ids"] == ["o1"]

    def test_order_filled_across_local_midnight_matches_on_the_days_part(self):
        # 23:40 Detroit fill of 0.5 today, the other 0.5 after midnight.
        mirror = _fresh_mirror(trades=[_ledger("o-m", "0.50", recorded="2026-09-29T03:40:05+00:00"),
                                       _ledger("o-m", "0.50", recorded="2026-09-29T04:30:00+00:00", n=2)])
        rec = _run([_fill("f1", "o-m", "0.50", "2026-09-29T03:40:00Z")], mirror=mirror)
        assert rec["status"] == "matched", rec["incomplete_reasons"]
        assert rec["qty_mismatch"] == [] and rec["boundary_ambiguous_orders"] == []

    def test_fill_recorded_after_local_midnight_is_ambiguous(self):
        mirror = _fresh_mirror(trades=[_ledger("o-late", "1.00", recorded="2026-09-29T04:00:30+00:00")])
        rec = _run([_fill("f1", "o-late", "1.00", "2026-09-29T03:59:50Z")], mirror=mirror)
        assert rec["status"] == "incomplete"
        assert rec["incomplete_reasons"] == ["ledger_boundary_ambiguous"]
        assert rec["boundary_ambiguous_orders"] == ["o-late"] and rec["ledger_order_ids"] == ["o-late"]
        assert rec["evidence"]["recording_lag_seconds"] == 300

    def test_row_in_the_recording_window_written_during_the_read_is_a_change(self):
        def late_row(m):
            m.trades.append(_ledger("o-new", 1.0, recorded="2026-09-29T04:02:00+00:00", n=7,
                                    synced_at=datetime.fromtimestamp(AFTER_DAY, timezone.utc).isoformat()))
        mirror = _fresh_mirror(trades=[_ledger("o1", 1.0)], after_rows=late_row)
        rec = _run([_fill("f1", "o1", "1.00", "2026-09-28T05:00:00Z")], mirror=mirror)
        assert rec["incomplete_reasons"] == ["ledger_mirror_changed_during_read"]
        assert mirror.changed_queries[0]["window"] == (DAY_START - timedelta(seconds=120),
                                                       DAY_END + timedelta(seconds=300))

    def test_in_day_ledger_order_is_a_dependency_even_without_a_venue_fill(self):
        rec = _run(mirror=_fresh_mirror(trades=[_ledger("o-ledger-only", 1.0)]))
        assert rec["status"] == "mismatched" and rec["ledger_order_ids"] == ["o-ledger-only"]

    def test_write_before_the_read_started_is_not_a_change(self):
        mirror = _fresh_mirror(trades=[_ledger("o1", 1.0, synced_at="2026-09-29T04:20:00+00:00")])
        rec = _run([_fill("f1", "o1", "1.00", "2026-09-28T05:00:00Z")], mirror=mirror)
        assert rec["status"] == "matched"

    def test_mirror_count_moving_between_pages_is_incomplete(self):
        rec = _run(mirror=FakeMirror(fail=MirrorChangedError("ledger_trades: row count moved")))
        assert rec["status"] == "incomplete"
        assert "ledger_mirror_changed_during_read" in rec["incomplete_reasons"]

    def test_collector_crash_still_writes_an_incomplete_row(self):
        class Boom:
            def collect(self, start, end):
                raise OverflowError("date value out of range")
        scope = _scope()
        rec = run_reconciliation(Boom(), scope, _fresh_mirror(), DAY, venue="kalshi", clock=lambda: AFTER_DAY)
        assert rec["status"] == "incomplete" and not rec["coverage_verified"]
        assert "venue_collector_error" in rec["incomplete_reasons"]
        json.dumps(rec)

    @pytest.mark.parametrize("kw", [
        {"finality_lag_seconds": -900}, {"finality_lag_seconds": float("nan")},
        {"finality_lag_seconds": 299},   # shorter than the recording lag
        {"max_source_age_seconds": 0}, {"max_source_age_seconds": float("inf")},
    ])
    def test_invalid_settings_fail_before_any_read(self, kw):
        venue = FakeVenue()
        mirror = _fresh_mirror()
        with pytest.raises(ValueError):
            _run(venue=venue, mirror=mirror, **kw)
        assert venue.calls == [] and mirror.status_reads == 0

    def test_record_is_json_serializable_and_pnl_is_not_claimed(self):
        rec = _run()
        json.dumps(rec)
        assert "pnl_verified" not in rec and "realized_pnl" not in json.dumps(rec)


class TestRemoteRecordContract:
    DRAFT = Path(__file__).parent.parent / "supabase" / "drafts" / "0007_trade_ledger_reporting.sql"

    def test_every_record_field_is_a_reconciliation_column(self):
        sql = self.DRAFT.read_text()
        body = sql.split("create table if not exists public.ledger_venue_reconciliations (", 1)[1]
        body = body.split("\n);", 1)[0]
        columns = set(re.findall(r"^\s{2}([a-z_]+)\s", body, flags=re.M)) - {"constraint", "check"}
        rec = _run()
        assert set(rec) <= columns, set(rec) - columns


# ---------------------------------------------------------------------------
# PostgREST mirror access
# ---------------------------------------------------------------------------


class _Resp:
    def __init__(self, status=200, body=None, content_range=None):
        self.status_code = status
        self._body = body if body is not None else []
        self.headers = {"Content-Range": content_range} if content_range else {}

    def json(self):
        return self._body


class _Session:
    def __init__(self, responses):
        self.responses = list(responses)
        self.gets: list = []
        self.posts: list = []

    def get(self, url, params=None, headers=None, timeout=None, allow_redirects=True):
        self.gets.append({"url": url, "params": params, "headers": headers, "allow_redirects": allow_redirects})
        return self.responses.pop(0)

    def post(self, url, params=None, headers=None, json=None, timeout=None, allow_redirects=True):
        self.posts.append({"url": url, "params": params, "json": json, "allow_redirects": allow_redirects})
        return self.responses.pop(0)


class TestPostgrestLedgerMirror:
    URL = "https://example.supabase.co"

    def test_pages_until_the_exact_count(self):
        session = _Session([_Resp(body=[{"ledger_key": "a"}, {"ledger_key": "b"}], content_range="0-1/3"),
                            _Resp(body=[{"ledger_key": "c"}], content_range="2-2/3")])
        mirror = PostgrestLedgerMirror(self.URL, "k", session=session, page_size=2)
        rows = mirror.status_rows(["arb-scanner"])
        assert [r["ledger_key"] for r in rows] == ["a", "b", "c"]
        assert all(g["headers"]["Prefer"] == "count=exact" for g in session.gets)
        assert session.gets[1]["params"]["offset"] == 2
        assert all(g["allow_redirects"] is False for g in session.gets)

    def test_server_row_cap_is_detected_as_truncation(self):
        # Asked for 1000, the server capped each page at 2 and then returned nothing.
        session = _Session([_Resp(body=[{"k": 1}, {"k": 2}], content_range="0-1/5"), _Resp(body=[])])
        mirror = PostgrestLedgerMirror(self.URL, "k", session=session)
        with pytest.raises(MirrorReadError):
            mirror.ledger_trades_for_orders("kalshi", ["o1"])

    def test_total_moving_between_pages_is_a_change(self):
        # A same-size update would not move the total, but an insert or delete
        # between pages shifts offsets: the pages may mix two states.
        session = _Session([_Resp(body=[{"k": 1}, {"k": 2}], content_range="0-1/3"),
                            _Resp(body=[{"k": 3}, {"k": 4}], content_range="2-3/4")])
        mirror = PostgrestLedgerMirror(self.URL, "k", session=session, page_size=2)
        with pytest.raises(MirrorChangedError):
            mirror.ledger_trades_for_orders("kalshi", ["o1"])

    def test_missing_count_on_a_later_page_is_an_error(self):
        session = _Session([_Resp(body=[{"k": 1}, {"k": 2}], content_range="0-1/3"), _Resp(body=[{"k": 3}])])
        mirror = PostgrestLedgerMirror(self.URL, "k", session=session, page_size=2)
        with pytest.raises(MirrorReadError):
            mirror.status_rows(["a"])

    def test_changed_since_counts_window_undated_orders_and_venueless_tombstones(self):
        session = _Session([_Resp(body=[], content_range="*/0"), _Resp(body=[], content_range="0-0/2")])
        mirror = PostgrestLedgerMirror(self.URL, "k", session=session)
        since = DAY_END + timedelta(minutes=58)
        n = mirror.changed_since("kalshi", since, DAY_START, DAY_END, ["o2", "o1"])
        assert n == 2
        first, second = (g["params"] for g in session.gets)
        assert first["synced_at"] == second["synced_at"] == f"gte.{since.isoformat()}"
        venue_clause = 'or(venue.eq."kalshi",and(deleted.is.true,venue.is.null))'
        assert first["and"] == (
            f'({venue_clause},or(and(recorded_at.gte."{DAY_START.isoformat()}",'
            f'recorded_at.lt."{DAY_END.isoformat()}"),recorded_at.is.null))')
        assert second["and"] == f'({venue_clause},order_id.in.("o1","o2"))'
        assert all(g["headers"]["Prefer"] == "count=exact" for g in session.gets)

    def test_missing_exact_count_is_an_error(self):
        mirror = PostgrestLedgerMirror(self.URL, "k", session=_Session([_Resp(body=[])]))
        with pytest.raises(MirrorReadError):
            mirror.status_rows(["a"])

    def test_http_error_is_an_error(self):
        mirror = PostgrestLedgerMirror(self.URL, "k", session=_Session([_Resp(status=401)]))
        with pytest.raises(MirrorReadError):
            mirror.status_rows(["a"])

    def test_window_and_in_filters(self):
        session = _Session([_Resp(body=[], content_range="*/0"), _Resp(body=[], content_range="*/0")])
        mirror = PostgrestLedgerMirror(self.URL, "k", session=session)
        mirror.ledger_trades("kalshi", DAY_START, DAY_END)
        assert session.gets[0]["params"]["and"] == (
            f'(recorded_at.gte."{DAY_START.isoformat()}",recorded_at.lt."{DAY_END.isoformat()}")')
        assert session.gets[1]["params"]["recorded_at"] == "is.null"
        assert venue_reconciliation._in_list(['a"b', "c"]) == 'in.("a\\"b","c")'

    def test_counts_use_exact_count_of_live_rows(self):
        session = _Session([_Resp(body=[], content_range="0-0/7"), _Resp(body=[], content_range="0-0/2")])
        mirror = PostgrestLedgerMirror(self.URL, "k", session=session)
        assert mirror.live_counts("svc", "db1") == {"trades": 7, "positions": 2}
        assert session.gets[0]["params"]["deleted"] == "is.false"

    def test_write_is_idempotent_on_run_id(self):
        session = _Session([_Resp(status=201)])
        PostgrestLedgerMirror(self.URL, "k", session=session).write_reconciliation({"run_id": "r1"})
        post = session.posts[0]
        assert post["params"] == {"on_conflict": "run_id"} and post["allow_redirects"] is False
        assert post["url"].endswith("/rest/v1/ledger_venue_reconciliations")

    def test_write_failure_raises(self):
        with pytest.raises(RuntimeError):
            PostgrestLedgerMirror(self.URL, "k", session=_Session([_Resp(status=409)])).write_reconciliation({})

    def test_plain_http_url_is_refused(self):
        with pytest.raises(ValueError):
            PostgrestLedgerMirror("http://example.supabase.co", "k", session=_Session([]))


# ---------------------------------------------------------------------------
# CLI guards (no network)
# ---------------------------------------------------------------------------


class TestCli:
    def _main(self):
        sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))
        import reconcile_venue_fills
        return reconcile_venue_fills.main

    def test_prints_fingerprint_only(self, monkeypatch, capsys):
        monkeypatch.setenv("KALSHI_API_KEY_ID", KEY_ID)
        assert self._main()(["--print-key-fingerprint"]) == 0
        out = capsys.readouterr().out.strip()
        assert out == key_fingerprint(KEY_ID) and KEY_ID not in out

    def test_missing_scope_refuses_to_run(self, monkeypatch, capsys):
        monkeypatch.delenv("LEDGER_KALSHI_SCOPE", raising=False)
        assert self._main()(["--day", "2026-09-28"]) == 2
        assert "LEDGER_KALSHI_SCOPE" in capsys.readouterr().err

    @pytest.mark.parametrize("name,value", [
        ("LEDGER_RECON_FINALITY_SECONDS", "-900"), ("LEDGER_RECON_FINALITY_SECONDS", "nan"),
        ("LEDGER_RECON_FINALITY_SECONDS", "inf"), ("LEDGER_RECON_FINALITY_SECONDS", "soon"),
        ("LEDGER_RECON_FINALITY_SECONDS", "60"),
        ("LEDGER_RECON_MAX_SOURCE_AGE_SECONDS", "0"), ("LEDGER_RECON_MAX_SOURCE_AGE_SECONDS", "-1"),
        ("LEDGER_RECON_MAX_SOURCE_AGE_SECONDS", "Infinity"),
    ])
    def test_invalid_timing_settings_refuse_to_run(self, monkeypatch, capsys, name, value):
        monkeypatch.setenv("LEDGER_KALSHI_SCOPE", json.dumps(SCOPE_RAW))
        monkeypatch.setenv("SUPABASE_URL", "https://example.supabase.co")
        monkeypatch.setenv("SUPABASE_SERVICE_KEY", "not-a-real-key")
        monkeypatch.setenv(name, value)
        assert self._main()(["--day", "2026-09-28"]) == 2
        assert name in capsys.readouterr().err

    def test_missing_supabase_refuses_to_run(self, monkeypatch, capsys):
        monkeypatch.setenv("LEDGER_KALSHI_SCOPE", json.dumps(SCOPE_RAW))
        monkeypatch.delenv("SUPABASE_URL", raising=False)
        assert self._main()(["--day", "2026-09-28"]) == 2
