"""Tests for the trade-ledger reporting mirror (db change capture + ledger_sync).

The fake Supabase client mirrors the draft migration's semantics: upsert on
ledger_key/source_key merges only the columns sent, and a record whose
source_version is lower than the stored one (same capture epoch) is ignored.
"""

import os
import sys
from decimal import Decimal

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import config  # noqa: E402
import db as db_module  # noqa: E402
import ledger_sync  # noqa: E402
from db import TradeDB  # noqa: E402
from ledger_sync import LedgerCaptureMissing, LedgerExporter, derive_run_mode, reconcile_fills  # noqa: E402


class FakeRemote:
    """In-memory stand-in for the Supabase tables the exporter writes."""

    KEYS = {"ledger_trades": "ledger_key", "ledger_positions": "ledger_key", "ledger_sync_status": "source_key"}

    def __init__(self):
        self.tables = {name: {} for name in self.KEYS}
        self.calls = []
        self.fail_on = None  # (table, nth call to that table) or callable

    def table(self, name):
        return _FakeQuery(self, name)

    def apply(self, name, rows, on_conflict):
        assert on_conflict == self.KEYS[name]
        self.calls.append((name, len(rows)))
        if self.fail_on and self.fail_on(name, rows):
            raise RuntimeError(f"injected failure on {name}")
        store = self.tables[name]
        for row in rows:
            key = row[on_conflict]
            old = store.get(key)
            if (old is not None and name != "ledger_sync_status"
                    and old.get("capture_epoch") == row.get("capture_epoch")
                    and row["source_version"] < old["source_version"]):
                continue  # version guard
            merged = dict(old or {})
            merged.update(row)
            store[key] = merged

    def rows(self, name, include_deleted=False):
        return sorted((r for r in self.tables[name].values() if include_deleted or not r.get("deleted")),
                      key=lambda r: r["source_id"])


class _FakeQuery:
    def __init__(self, remote, name):
        self.remote, self.name = remote, name

    def upsert(self, rows, on_conflict=""):
        self.rows, self.on_conflict = rows, on_conflict
        return self

    def execute(self):
        self.remote.apply(self.name, self.rows, self.on_conflict)


@pytest.fixture
def ledger(tmp_path, monkeypatch):
    monkeypatch.delenv("LEDGER_ACCOUNT_REFS", raising=False)
    monkeypatch.delenv("LEDGER_CAPTURE_ENABLED", raising=False)
    path = str(tmp_path / "trades.db")
    tdb = TradeDB(path, ledger_capture=True)
    remote = FakeRemote()
    exporter = LedgerExporter(remote, path, service="arb-scanner", batch_size=500)
    yield tdb, remote, exporter, path
    exporter.close()
    tdb.close()


def _trade(tdb, status="pending", platform="kalshi", **kw):
    return tdb.log_trade(opportunity_id=1, platform=platform, side="BUY", price=0.4, size=4.0,
                         status=status, **kw)


class TestChangeCapture:
    def test_triggers_record_insert_update_delete(self, ledger):
        tdb, _, _, _ = ledger
        tid = _trade(tdb, run_mode="live")
        tdb.update_trade_status(tid, "filled", fill_price=0.41, fill_qty=10)
        pid = tdb.create_position(1, "Mkt", "kalshi", expected_pnl=0.2, run_mode="live")
        tdb.settle_position(pid, realized_pnl=0.18)
        tdb.conn.execute("DELETE FROM trades WHERE id = ?", (tid,))
        tdb.conn.commit()
        ops = [(r["source_table"], r["op"]) for r in tdb.conn.execute("SELECT * FROM ledger_outbox ORDER BY seq")]
        assert ops == [("trades", "insert"), ("trades", "update"), ("trades", "update"),
                       ("positions", "insert"), ("positions", "update"), ("trades", "delete")]

    def test_capture_off_by_default_and_never_removed(self, tmp_path, monkeypatch):
        monkeypatch.delenv("LEDGER_CAPTURE_ENABLED", raising=False)
        path = str(tmp_path / "t.db")
        assert not TradeDB(path).ledger_capture_installed()
        TradeDB(path, ledger_capture=True)
        assert TradeDB(path, ledger_capture=False).ledger_capture_installed()

    def test_env_flag_installs_capture(self, tmp_path, monkeypatch):
        monkeypatch.setenv("LEDGER_CAPTURE_ENABLED", "true")
        assert TradeDB(str(tmp_path / "t.db")).ledger_capture_installed()

    def test_reinstall_keeps_epoch(self, tmp_path):
        path = str(tmp_path / "t.db")
        first = TradeDB(path, ledger_capture=True).get_ledger_meta()
        second = TradeDB(path, ledger_capture=True).get_ledger_meta()
        assert first["capture_epoch"] == second["capture_epoch"]
        assert first["db_instance_id"] == second["db_instance_id"]

    def test_boundary_records_pre_capture_rows(self, tmp_path):
        path = str(tmp_path / "t.db")
        old = TradeDB(path, ledger_capture=False)
        _trade(old)
        _trade(old)
        meta = TradeDB(path, ledger_capture=True).get_ledger_meta()
        assert meta["capture_boundary_trades_id"] == "2"

    def test_invalid_run_mode_rejected(self, ledger):
        tdb, *_ = ledger
        with pytest.raises(ValueError):
            _trade(tdb, run_mode="dry")

    def test_account_refs_stamped_for_new_rows_only(self, tmp_path, monkeypatch):
        path = str(tmp_path / "t.db")
        tdb = TradeDB(path)
        before = _trade(tdb)
        monkeypatch.setenv("LEDGER_ACCOUNT_REFS", '{"kalshi": "kalshi-main", "polymarket": "bad label!"}')
        tdb2 = TradeDB(path)
        after = _trade(tdb2)
        poly = _trade(tdb2, platform="polymarket")
        refs = {r["id"]: r["account_ref"] for r in tdb2.conn.execute("SELECT id, account_ref FROM trades")}
        assert refs == {before: None, after: "kalshi-main", poly: None}


class TestProvenance:
    def test_writer_stamp(self):
        assert derive_run_mode({"run_mode": "live", "status": "filled"}) == ("live", "writer_stamped")
        assert derive_run_mode({"run_mode": "paper"}) == ("paper", "writer_stamped")

    def test_row_markers(self):
        assert derive_run_mode({"status": "dry_run"}) == ("paper", "row_status")
        assert derive_run_mode({"status": "filled", "order_id": "dry_mmpilot_X_quote_bid_3"}) == \
            ("paper", "row_order_id")

    def test_unmarked_history_is_unknown(self):
        assert derive_run_mode({"status": "filled", "order_id": "abc"}) == ("unknown", "none")
        assert derive_run_mode({"status": "settled"}) == ("unknown", "none")

    def test_live_stamp_on_dry_row_is_conflict(self):
        assert derive_run_mode({"run_mode": "live", "order_id": "dry_x"}) == ("unknown", "conflict")

    def test_history_not_labelled_from_current_config(self, tmp_path, monkeypatch):
        """Unmarked pre-capture rows stay unknown whatever DRY_RUN says today."""
        path = str(tmp_path / "t.db")
        old = TradeDB(path)
        _trade(old, status="filled", order_id="real-1")
        old.create_position(1, "Mkt", "kalshi", expected_pnl=1.0)
        TradeDB(path, ledger_capture=True)
        remote = FakeRemote()
        for dry_run in (True, False):
            monkeypatch.setattr(config, "DRY_RUN", dry_run)
            monkeypatch.setenv("DRY_RUN", str(dry_run).lower())
            LedgerExporter(remote, path, service="svc").sync_once()
            trade = remote.rows("ledger_trades")[0]
            pos = remote.rows("ledger_positions")[0]
            assert (trade["run_mode"], trade["mode_evidence"], trade["pre_capture"]) == ("unknown", "none", True)
            assert pos["run_mode"] == "unknown" and pos["account_ref"] is None


class TestExporter:
    def test_requires_capture(self, tmp_path):
        path = str(tmp_path / "t.db")
        TradeDB(path)
        with pytest.raises(LedgerCaptureMissing):
            LedgerExporter(FakeRemote(), path, service="svc").sync_once()

    def test_requires_service_identity(self, ledger):
        _, _, _, path = ledger
        with pytest.raises(ValueError):
            LedgerExporter(FakeRemote(), path, service=None)

    def test_snapshot_then_outbox(self, ledger):
        tdb, remote, exporter, _ = ledger
        t1 = _trade(tdb, run_mode="live")
        res = exporter.sync_once()
        assert res.snapshot_complete and res.pending_changes == 0
        assert [r["source_id"] for r in remote.rows("ledger_trades")] == [t1]
        t2 = _trade(tdb, status="dry_run", run_mode="paper")
        exporter.sync_once()
        assert [r["source_id"] for r in remote.rows("ledger_trades")] == [t1, t2]
        assert tdb.conn.execute("SELECT COUNT(*) FROM ledger_outbox").fetchone()[0] == 0  # pruned

    def test_duplicate_replay_is_idempotent(self, ledger):
        tdb, remote, exporter, path = ledger
        tid = _trade(tdb, run_mode="live")
        exporter.sync_once()
        before = {k: dict(v) for k, v in remote.tables["ledger_trades"].items()}
        # Replay: forget local sync state entirely and export everything again.
        exporter._conn.execute("DELETE FROM ledger_sync_state")
        exporter.sync_once()
        LedgerExporter(remote, path, service="arb-scanner").sync_once()
        after = remote.tables["ledger_trades"]
        assert list(after) == list(before) and len(after) == 1
        for key in after:
            assert {k: v for k, v in after[key].items() if k != "exported_at"} == \
                {k: v for k, v in before[key].items() if k != "exported_at"}
        assert after[next(iter(after))]["source_id"] == tid

    def test_correction_after_watermark_is_exported(self, ledger):
        """Fill and settlement updates on already-exported rows (ids below any
        id watermark) must reach the mirror."""
        tdb, remote, exporter, _ = ledger
        tid = _trade(tdb, run_mode="live")
        pid = tdb.create_position(1, "Mkt", "kalshi", expected_pnl=0.5, market_ticker="KX-1", run_mode="live")
        _trade(tdb, run_mode="live")  # a newer id, so an id watermark would sit past tid
        exporter.sync_once()
        tdb.update_trade_status(tid, "filled", fill_price=0.42, slippage=0.02, fill_qty=9.5)
        tdb.settle_position(pid, realized_pnl=0.47)
        exporter.sync_once()
        trade = remote.tables["ledger_trades"][ledger_sync.ledger_key(
            "arb-scanner", tdb.get_ledger_meta()["db_instance_id"], "trades", tid)]
        assert (trade["status"], trade["fill_price"], trade["fill_qty"], trade["slippage"]) == \
            ("filled", 0.42, 9.5, 0.02)
        pos = remote.rows("ledger_positions")[0]
        assert pos["status"] == "settled" and pos["realized_pnl"] == 0.47 and pos["settled_at"]
        assert pos["pnl_basis"] == "engine_computed_unverified" and pos["fee_status"] == "not_recorded"

    def test_delete_becomes_tombstone(self, ledger):
        tdb, remote, exporter, _ = ledger
        opp = tdb.log_opportunity("SpreadKalshi", "M", "", 1, 0.1, 0.1, 5, "executed")
        tid = tdb.log_trade(opp, "kalshi", "BUY", 0.4, 4, "filled", run_mode="live")
        exporter.sync_once()
        tdb.purge_opportunities_by_type("SpreadKalshi")
        exporter.sync_once()
        assert remote.rows("ledger_trades") == []
        tomb = remote.rows("ledger_trades", include_deleted=True)[0]
        assert tomb["source_id"] == tid and tomb["deleted"] is True
        assert tomb["status"] == "filled"  # last known state kept alongside the tombstone

    def test_insert_then_delete_between_syncs(self, ledger):
        tdb, remote, exporter, _ = ledger
        exporter.sync_once()
        tid = _trade(tdb, run_mode="live")
        tdb.conn.execute("DELETE FROM trades WHERE id = ?", (tid,))
        tdb.conn.commit()
        exporter.sync_once()
        assert remote.rows("ledger_trades", include_deleted=True)[0]["deleted"] is True

    def test_failed_upsert_advances_nothing_then_recovers(self, ledger):
        tdb, remote, exporter, _ = ledger
        exporter.sync_once()
        tid = _trade(tdb, run_mode="live")
        remote.fail_on = lambda name, rows: name == "ledger_trades"
        with pytest.raises(RuntimeError):
            exporter.sync_once()
        assert remote.rows("ledger_trades") == []
        assert tdb.conn.execute("SELECT COUNT(*) FROM ledger_outbox").fetchone()[0] == 1
        status = next(iter(remote.tables["ledger_sync_status"].values()))
        assert status["last_error"].startswith("RuntimeError") and status["pending_changes"] == 1
        first_success = status["last_success_at"]
        remote.fail_on = None
        exporter.sync_once()
        assert [r["source_id"] for r in remote.rows("ledger_trades")] == [tid]
        status = next(iter(remote.tables["ledger_sync_status"].values()))
        assert status["last_error"] is None and status["last_success_at"] >= first_success

    def test_partial_batch_failure_replays_safely(self, ledger):
        """Trades land, positions fail: the retry resends both without duplicates."""
        tdb, remote, exporter, _ = ledger
        exporter.sync_once()
        _trade(tdb, run_mode="live")
        tdb.create_position(1, "Mkt", "kalshi", expected_pnl=0.1, run_mode="live")
        remote.fail_on = lambda name, rows: name == "ledger_positions"
        with pytest.raises(RuntimeError):
            exporter.sync_once()
        assert len(remote.rows("ledger_trades")) == 1 and remote.rows("ledger_positions") == []
        remote.fail_on = None
        exporter.sync_once()
        assert len(remote.rows("ledger_trades")) == 1 and len(remote.rows("ledger_positions")) == 1

    def test_status_failure_after_rows_is_not_success(self, ledger):
        tdb, remote, exporter, _ = ledger
        exporter.sync_once()
        _trade(tdb, run_mode="live")
        remote.fail_on = lambda name, rows: name == "ledger_sync_status" and rows[0]["last_error"] is None
        with pytest.raises(RuntimeError):
            exporter.sync_once()
        remote.fail_on = None
        exporter.sync_once()
        assert len(remote.rows("ledger_trades")) == 1

    def test_older_version_never_overwrites_newer(self, ledger):
        tdb, remote, exporter, _ = ledger
        tid = _trade(tdb, run_mode="live")
        exporter.sync_once()
        stale = dict(remote.rows("ledger_trades")[0])
        tdb.update_trade_status(tid, "filled", fill_price=0.5)
        exporter.sync_once()
        stale.update({"status": "pending", "source_version": stale["source_version"] - 1})
        remote.apply("ledger_trades", [stale], "ledger_key")
        assert remote.rows("ledger_trades")[0]["status"] == "filled"

    def test_backlog_larger_than_batch_reports_pending(self, tmp_path):
        path = str(tmp_path / "t.db")
        tdb = TradeDB(path, ledger_capture=True)
        remote = FakeRemote()
        exporter = LedgerExporter(remote, path, service="svc", batch_size=2, max_batches=1)
        exporter.sync_once()  # snapshot of empty tables
        exporter.sync_once()
        for _ in range(5):
            _trade(tdb, run_mode="live")
        res = exporter.sync_once()
        assert res.pending_changes == 3
        status = next(iter(remote.tables["ledger_sync_status"].values()))
        assert status["pending_changes"] == 3 and status["local_trades_count"] == 5
        for _ in range(3):
            exporter.sync_once()
        assert len(remote.rows("ledger_trades")) == 5
        assert next(iter(remote.tables["ledger_sync_status"].values()))["pending_changes"] == 0

    def test_snapshot_pages_resume_and_report_incomplete(self, tmp_path):
        path = str(tmp_path / "t.db")
        old = TradeDB(path)
        for _ in range(5):
            _trade(old, status="filled")
        TradeDB(path, ledger_capture=True)
        remote = FakeRemote()
        exporter = LedgerExporter(remote, path, service="svc", batch_size=2, max_batches=1)
        res = exporter.sync_once()
        assert not res.snapshot_complete
        assert next(iter(remote.tables["ledger_sync_status"].values()))["snapshot_complete"] is False
        # A new exporter (restart) resumes from the saved cursor.
        exporter = LedgerExporter(remote, path, service="svc", batch_size=2, max_batches=10)
        assert exporter.sync_once().snapshot_complete
        assert [r["source_id"] for r in remote.rows("ledger_trades")] == [1, 2, 3, 4, 5]

    def test_change_during_snapshot_is_not_lost(self, tmp_path):
        path = str(tmp_path / "t.db")
        tdb = TradeDB(path)
        for _ in range(3):
            _trade(tdb)
        tdb = TradeDB(path, ledger_capture=True)
        remote = FakeRemote()
        exporter = LedgerExporter(remote, path, service="svc", batch_size=2, max_batches=1)
        exporter.sync_once()  # exports ids 1-2
        tdb.update_trade_status(1, "filled", fill_price=0.4)  # already-snapshotted row changes
        for _ in range(5):
            exporter.sync_once()
        assert remote.rows("ledger_trades")[0]["status"] == "filled"

    def test_mixed_modes_stay_separate(self, ledger):
        tdb, remote, exporter, _ = ledger
        _trade(tdb, status="dry_run", run_mode="paper")
        _trade(tdb, status="filled", order_id="dry_mmpilot_KX_quote_bid_1")  # unstamped sim fill
        _trade(tdb, status="filled", order_id="ord-9", run_mode="live")
        _trade(tdb, status="filled", order_id="ord-10")  # unstamped real-looking fill
        exporter.sync_once()
        modes = [(r["run_mode"], r["mode_evidence"]) for r in remote.rows("ledger_trades")]
        assert modes == [("paper", "writer_stamped"), ("paper", "row_order_id"),
                         ("live", "writer_stamped"), ("unknown", "none")]

    def test_new_capture_epoch_forces_resnapshot(self, ledger):
        tdb, remote, exporter, path = ledger
        _trade(tdb, run_mode="live")
        exporter.sync_once()
        for name in ("insert", "update", "delete"):
            tdb.conn.execute(f"DROP TRIGGER ledger_capture_trades_{name}")
        tdb.conn.commit()
        with pytest.raises(LedgerCaptureMissing):
            exporter.sync_once()
        tid = _trade(tdb, run_mode="live")  # uncaptured write
        TradeDB(path, ledger_capture=True)  # reinstall: new epoch
        exporter.sync_once()
        assert tid in [r["source_id"] for r in remote.rows("ledger_trades")]

    def test_exporter_failure_never_blocks_trade_writes(self, ledger):
        tdb, remote, exporter, _ = ledger
        remote.fail_on = lambda name, rows: True
        with pytest.raises(RuntimeError):
            exporter.sync_once()
        assert _trade(tdb, run_mode="live")  # the order path's local write still works


class TestReconcileFills:
    LEDGER = [
        {"venue": "kalshi", "run_mode": "live", "status": "filled", "fill_price": 0.4, "fill_qty": 10,
         "order_id": "A", "account_ref": "k1", "recorded_at": "2026-09-28T10:00:00+00:00"},
        {"venue": "kalshi", "run_mode": "live", "status": "filled", "fill_price": 0.5, "fill_qty": 3,
         "order_id": "B", "account_ref": "k1", "recorded_at": "2026-09-28T11:00:00+00:00"},
        {"venue": "kalshi", "run_mode": "paper", "status": "filled", "fill_price": 0.5,
         "order_id": "dry_1", "account_ref": "k1", "recorded_at": "2026-09-28T11:00:00+00:00"},
    ]
    KW = dict(venue="kalshi", account_ref="k1",
              interval_start="2026-09-28T00:00:00", interval_end="2026-09-29T00:00:00")

    def test_matched(self):
        venue = [{"order_id": "A", "qty": "10", "fee_usd": "0.07"}, {"order_id": "B", "qty": 3, "fee_usd": "0.02"}]
        out = reconcile_fills(venue, self.LEDGER, **self.KW)
        assert out["status"] == "matched" and out["matched_order_count"] == 2
        assert Decimal(out["venue_fees_usd"]) == Decimal("0.09")

    def test_missing_both_sides_and_qty(self):
        venue = [{"order_id": "A", "qty": 9}, {"order_id": "C", "qty": 1}]
        out = reconcile_fills(venue, self.LEDGER, **self.KW)
        assert out["status"] == "mismatched"
        assert out["missing_in_ledger"] == ["C"] and out["missing_in_venue"] == ["B"]
        assert out["qty_mismatch"] == ["A"] and out["venue_fees_usd"] is None

    def test_paper_rows_never_count(self):
        out = reconcile_fills([], self.LEDGER[2:], **self.KW)
        assert out["ledger_order_count"] == 0 and out["status"] == "matched"

    def test_paper_rows_never_count_as_run_modes(self):
        assert "dry" not in db_module.RUN_MODES


class TestRemoteSchemaContract:
    """Every field the exporter sends must be a column in the draft migration."""

    DRAFT = os.path.join(os.path.dirname(__file__), "..", "supabase", "drafts", "0007_trade_ledger_reporting.sql")

    def _columns(self, table):
        import re
        sql = open(self.DRAFT).read()
        body = re.search(rf"create table if not exists public\.{table} \((.*?)\n\);", sql, re.S).group(1)
        cols = set()
        for line in body.splitlines():
            word = line.strip().split(" ")[0]
            if word and word not in ("unique", "--") and re.match(r"^[a-z_]+$", word):
                cols.add(word)
        return cols

    def test_exported_fields_exist_remotely(self, ledger):
        tdb, remote, exporter, _ = ledger
        tid = _trade(tdb, run_mode="live")
        tdb.create_position(1, "Mkt", "kalshi", expected_pnl=0.1, run_mode="live")
        exporter.sync_once()
        tdb.conn.execute("DELETE FROM trades WHERE id = ?", (tid,))
        tdb.conn.commit()
        exporter.sync_once()
        for name in ("ledger_trades", "ledger_positions", "ledger_sync_status"):
            sent = set().union(*(set(r) for r in remote.tables[name].values()))
            assert sent <= self._columns(name), (name, sent - self._columns(name))
