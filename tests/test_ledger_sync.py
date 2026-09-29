"""Tests for the trade-ledger reporting mirror (db change capture + ledger_sync).

The fake Supabase client mirrors the draft migration's semantics: upsert on
ledger_key/source_key merges only the columns sent, a record whose
source_version is lower than the stored one is ignored, a record carrying
another capture epoch than its stored key is refused, and a status older than
the stored one is ignored. ``current`` applies the views' superseded filter.
"""

import json
import os
import sqlite3
import sys
import threading
import time
from decimal import Decimal

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import config  # noqa: E402
import db as db_module  # noqa: E402
import ledger_sync  # noqa: E402

TradeDB = db_module.TradeDB
LedgerCaptureMissing = ledger_sync.LedgerCaptureMissing
LedgerExporter = ledger_sync.LedgerExporter
derive_run_mode = ledger_sync.derive_run_mode
reconcile_fills = ledger_sync.reconcile_fills


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
            if old is not None and name != "ledger_sync_status":
                if row.get("capture_epoch") != old.get("capture_epoch"):
                    raise RuntimeError(f"{key} belongs to epoch {old.get('capture_epoch')}")
                if row["source_version"] < old["source_version"]:
                    continue  # version guard
            if old is not None and name == "ledger_sync_status":
                if (row.get("db_instance_id"), row.get("capture_epoch")) != (
                        old.get("db_instance_id"), old.get("capture_epoch")):
                    raise RuntimeError(f"{key} belongs to another generation")
                if row["last_attempt_at"] < old["last_attempt_at"]:
                    continue  # status guard: keep the newer status
                kept = list(old.get("supersedes_db_instance_ids") or [])
                row = dict(row, supersedes_db_instance_ids=kept + [
                    i for i in row.get("supersedes_db_instance_ids") or [] if i not in kept])
            merged = dict(old or {})
            merged.update(row)
            store[key] = merged

    def superseded(self):
        return {(r["service"], inst) for r in self.tables["ledger_sync_status"].values()
                for inst in r.get("supersedes_db_instance_ids") or []}

    def current(self, name):
        """Live rows as the reporting views select them: no superseded generation."""
        gone = self.superseded()
        return [r for r in self.rows(name) if (r["service"], r["db_instance_id"]) not in gone]

    def mirror_complete(self, source_key):
        """ledger_reporting.sources.mirror_complete for one source."""
        s = self.tables["ledger_sync_status"][source_key]
        counts = {t: sum(1 for r in self.rows(f"ledger_{t}")
                         if (r["service"], r["db_instance_id"]) == (s["service"], s["db_instance_id"]))
                  for t in ("trades", "positions")}
        return ((s["service"], s["db_instance_id"]) not in self.superseded()
                and s.get("last_error") is None and s.get("last_success_at") is not None
                and s["last_success_at"] >= s["last_attempt_at"] and s["snapshot_complete"]
                and s["pending_changes"] == 0 and counts["trades"] == s["local_trades_count"]
                and counts["positions"] == s["local_positions_count"])

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

    def test_missing_epoch_with_triggers_present_starts_a_new_epoch(self, tmp_path):
        path = str(tmp_path / "t.db")
        first = TradeDB(path, ledger_capture=True)
        _trade(first)
        first.conn.execute("DELETE FROM ledger_meta WHERE key IN ('capture_epoch', 'capture_boundary_trades_id')")
        first.conn.commit()
        meta = TradeDB(path, ledger_capture=True).get_ledger_meta()
        assert meta["capture_epoch"]
        assert meta["capture_boundary_trades_id"] == "1"

    def test_failed_install_leaves_no_triggers_or_epoch(self, tmp_path, monkeypatch):
        tdb = TradeDB(str(tmp_path / "t.db"), ledger_capture=False)

        def boom(key, value):
            raise sqlite3.OperationalError("disk I/O error")

        monkeypatch.setattr(tdb, "_set_meta_if_absent", boom)
        with pytest.raises(sqlite3.OperationalError):
            tdb.enable_ledger_capture()
        assert not tdb.conn.in_transaction
        assert not tdb.ledger_capture_installed()
        assert tdb.conn.execute(
            "SELECT count(*) FROM sqlite_master WHERE name = 'ledger_outbox' OR name LIKE 'ledger_capture_%'"
        ).fetchone()[0] == 0
        assert "capture_epoch" not in tdb.get_ledger_meta()

    def test_boundary_records_pre_capture_rows(self, tmp_path):
        path = str(tmp_path / "t.db")
        old = TradeDB(path, ledger_capture=False)
        _trade(old)
        _trade(old)
        meta = TradeDB(path, ledger_capture=True).get_ledger_meta()
        assert meta["capture_boundary_trades_id"] == "2"

    def test_fill_qty_recorded_at_insert_when_known(self, ledger):
        tdb, *_ = ledger
        known = _trade(tdb, status="filled", run_mode="live", fill_qty=2.5)
        unknown = _trade(tdb, status="filled", run_mode="live")
        qty = {r["id"]: r["fill_qty"] for r in tdb.conn.execute("SELECT id, fill_qty FROM trades")}
        assert qty == {known: 2.5, unknown: None}

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

    def test_watermark_and_outbox_cleanup_commit_together(self, ledger):
        tdb, remote, exporter, _ = ledger
        exporter.sync_once()
        before = exporter._state()["watermark_seq"]
        _trade(tdb, run_mode="live")
        tdb.conn.execute("CREATE TRIGGER fail_cleanup BEFORE DELETE ON ledger_outbox "
                         "BEGIN SELECT RAISE(ABORT, 'cleanup failed'); END")
        tdb.conn.commit()
        with pytest.raises(sqlite3.Error):
            exporter.sync_once()
        # The cleanup failed, so the watermark did not move either: the batch
        # is still pending locally and reported as such.
        assert exporter._state()["watermark_seq"] == before
        assert tdb.conn.execute("SELECT COUNT(*) FROM ledger_outbox").fetchone()[0] == 1
        status = next(iter(remote.tables["ledger_sync_status"].values()))
        assert status["watermark_seq"] == int(before) and status["pending_changes"] == 1
        assert "cleanup failed" in status["last_error"]
        tdb.conn.execute("DROP TRIGGER fail_cleanup")
        tdb.conn.commit()
        res = exporter.sync_once()
        assert res.pending_changes == 0 and int(exporter._state()["watermark_seq"]) > int(before)
        assert tdb.conn.execute("SELECT COUNT(*) FROM ledger_outbox").fetchone()[0] == 0

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


def _lose_capture(tdb, how):
    """Break capture the two ways a reinstall recovers from."""
    if how == "triggers":
        for name in ("insert", "update", "delete"):
            tdb.conn.execute(f"DROP TRIGGER ledger_capture_trades_{name}")
            tdb.conn.execute(f"DROP TRIGGER ledger_capture_positions_{name}")
    else:
        tdb.conn.execute("DELETE FROM ledger_meta WHERE key = 'capture_epoch'")
    tdb.conn.commit()


def _source_key(meta):
    return f"arbgrid:arb-scanner:{meta['db_instance_id']}"


class TestCaptureGenerations:
    """A new capture epoch is a new mirror generation that supersedes the old one."""

    def _regenerate(self, tdb, path, how, uncaptured=None):
        old = tdb.get_ledger_meta()
        _lose_capture(tdb, how)
        if uncaptured:
            uncaptured()
        TradeDB(path, ledger_capture=True).close()
        return old, tdb.get_ledger_meta()

    @pytest.mark.parametrize("how", ["triggers", "epoch"])
    def test_new_epoch_rotates_the_instance_id(self, tmp_path, how):
        path = str(tmp_path / "t.db")
        tdb = TradeDB(path, ledger_capture=True)
        first = tdb.get_ledger_meta()
        assert json.loads(first["superseded_db_instance_ids"]) == []
        TradeDB(path, ledger_capture=True).close()  # plain reinstall: same generation
        assert tdb.get_ledger_meta()["db_instance_id"] == first["db_instance_id"]
        old, new = self._regenerate(tdb, path, how)
        assert new["capture_epoch"] != old["capture_epoch"]
        assert new["db_instance_id"] != old["db_instance_id"]
        assert json.loads(new["superseded_db_instance_ids"]) == [old["db_instance_id"]]
        tdb.close()

    @pytest.mark.parametrize("how", ["triggers", "epoch"])
    def test_rows_deleted_before_the_resnapshot_are_retired(self, ledger, how):
        tdb, remote, exporter, path = ledger
        ids = [_trade(tdb, run_mode="live") for _ in range(3)]
        pos = [tdb.create_position(1, f"M{i}", "kalshi", expected_pnl=0.1, run_mode="live") for i in range(2)]
        exporter.sync_once()

        def delete_while_uncaptured():
            tdb.conn.execute("DELETE FROM trades WHERE id = ?", (ids[1],))
            tdb.conn.execute("DELETE FROM positions WHERE id = ?", (pos[0],))
            tdb.conn.commit()

        old, new = self._regenerate(tdb, path, how, delete_while_uncaptured)
        assert exporter.sync_once().snapshot_complete
        assert [r["source_id"] for r in remote.current("ledger_trades")] == [ids[0], ids[2]]
        assert [r["source_id"] for r in remote.current("ledger_positions")] == [pos[1]]
        # The old generation still holds the deleted rows, but nothing selects it.
        assert ids[1] in [r["source_id"] for r in remote.rows("ledger_trades")
                          if r["db_instance_id"] == old["db_instance_id"]]
        status = remote.tables["ledger_sync_status"][_source_key(new)]
        assert status["supersedes_db_instance_ids"] == [old["db_instance_id"]]
        assert remote.mirror_complete(_source_key(new))
        assert not remote.mirror_complete(_source_key(old))

    def test_equal_count_with_changed_membership_shows_only_the_new_set(self, ledger):
        tdb, remote, exporter, path = ledger
        ids = [_trade(tdb, run_mode="live") for _ in range(3)]
        exporter.sync_once()
        added = []

        def swap_while_uncaptured():
            tdb.conn.execute("DELETE FROM trades WHERE id = ?", (ids[2],))
            tdb.conn.commit()
            added.append(_trade(tdb, run_mode="live"))

        old, new = self._regenerate(tdb, path, "triggers", swap_while_uncaptured)
        exporter.sync_once()
        assert [r["source_id"] for r in remote.current("ledger_trades")] == [ids[0], ids[1], added[0]]
        assert {r["db_instance_id"] for r in remote.current("ledger_trades")} == {new["db_instance_id"]}
        assert remote.tables["ledger_sync_status"][_source_key(new)]["local_trades_count"] == 3
        assert remote.mirror_complete(_source_key(new))

    def test_status_is_published_before_any_row_of_the_new_generation(self, ledger):
        tdb, remote, exporter, path = ledger
        _trade(tdb, run_mode="live")
        exporter.sync_once()
        old, new = self._regenerate(tdb, path, "triggers")
        seen = []
        remote.fail_on = lambda name, rows: seen.append((name, [dict(r) for r in rows])) and False
        exporter.sync_once()
        name, rows = seen[0]
        assert name == "ledger_sync_status"
        assert rows[0]["source_key"] == _source_key(new)
        assert rows[0]["supersedes_db_instance_ids"] == [old["db_instance_id"]]
        assert rows[0]["snapshot_complete"] is False and "last_success_at" not in rows[0]
        assert "ledger_trades" in [n for n, _ in seen[1:]]

    def test_failed_status_publication_pushes_no_rows(self, ledger):
        tdb, remote, exporter, path = ledger
        _trade(tdb, run_mode="live")
        exporter.sync_once()
        old, new = self._regenerate(tdb, path, "triggers")
        remote.fail_on = lambda name, rows: name == "ledger_sync_status"
        with pytest.raises(RuntimeError):
            exporter.sync_once()
        assert not [r for r in remote.rows("ledger_trades") if r["db_instance_id"] == new["db_instance_id"]]
        # Until the new generation is published, the old one stays as it was.
        assert remote.mirror_complete(_source_key(old))
        remote.fail_on = None
        exporter.sync_once()
        assert remote.mirror_complete(_source_key(new)) and not remote.mirror_complete(_source_key(old))

    def test_new_generation_stays_incomplete_until_its_snapshot_is_exported(self, tmp_path):
        path = str(tmp_path / "t.db")
        tdb = TradeDB(path, ledger_capture=True)
        ids = [_trade(tdb, run_mode="live") for _ in range(4)]
        remote = FakeRemote()
        exporter = LedgerExporter(remote, path, service="arb-scanner", batch_size=1, max_batches=1)
        while not exporter.sync_once().snapshot_complete:
            pass
        old, new = self._regenerate(tdb, path, "triggers")
        res = exporter.sync_once()
        assert not res.snapshot_complete
        assert not remote.mirror_complete(_source_key(new))
        current = remote.current("ledger_trades")
        assert len(current) < len(ids)  # partial, and never mixed with the old generation
        assert {r["db_instance_id"] for r in current} <= {new["db_instance_id"]}
        for _ in range(10):
            if exporter.sync_once().snapshot_complete:
                break
        assert remote.mirror_complete(_source_key(new))
        assert [r["source_id"] for r in remote.current("ledger_trades")] == ids
        exporter.close()
        tdb.close()

    def test_delayed_previous_generation_writes_change_nothing_current(self, ledger):
        tdb, remote, exporter, path = ledger
        tid = _trade(tdb, run_mode="live")
        pid = tdb.create_position(1, "M", "kalshi", expected_pnl=0.1, run_mode="live")
        exporter.sync_once()
        old_trades = [dict(r) for r in remote.rows("ledger_trades")]
        old_positions = [dict(r) for r in remote.rows("ledger_positions")]
        old_status = dict(remote.tables["ledger_sync_status"][_source_key(tdb.get_ledger_meta())])

        def delete_while_uncaptured():
            tdb.conn.execute("DELETE FROM trades WHERE id = ?", (tid,))
            tdb.conn.execute("DELETE FROM positions WHERE id = ?", (pid,))
            tdb.conn.commit()

        old, new = self._regenerate(tdb, path, "triggers", delete_while_uncaptured)
        exporter.sync_once()
        new_status = dict(remote.tables["ledger_sync_status"][_source_key(new)])
        # Old-generation writes that arrive late, even ones stamped later.
        remote.apply("ledger_trades", [dict(r, source_version=r["source_version"] + 100, status="filled")
                                       for r in old_trades], "ledger_key")
        remote.apply("ledger_positions", [dict(r, source_version=r["source_version"] + 100)
                                          for r in old_positions], "ledger_key")
        remote.apply("ledger_sync_status", [dict(old_status, last_attempt_at="9999-01-01T00:00:00+00:00",
                                                 last_success_at="9999-01-01T00:00:00+00:00",
                                                 supersedes_db_instance_ids=[])], "source_key")
        assert remote.current("ledger_trades") == [] and remote.current("ledger_positions") == []
        assert not remote.mirror_complete(_source_key(old))
        assert remote.mirror_complete(_source_key(new))
        # A delayed status of the new generation itself cannot roll it back.
        remote.apply("ledger_sync_status", [dict(new_status, last_attempt_at="2000-01-01T00:00:00+00:00",
                                                 snapshot_complete=False)], "source_key")
        assert remote.mirror_complete(_source_key(new))

    def test_two_resets_before_a_sync_keep_the_whole_ancestry(self, tmp_path):
        path = str(tmp_path / "t.db")
        tdb = TradeDB(path, ledger_capture=True)
        g0 = tdb.get_ledger_meta()["db_instance_id"]
        _, g1 = self._regenerate(tdb, path, "triggers")
        _, g2 = self._regenerate(tdb, path, "epoch")
        assert json.loads(g2["superseded_db_instance_ids"]) == [g0, g1["db_instance_id"]]
        tdb.close()

    def test_two_resets_before_a_sync_retire_the_last_exported_generation(self, ledger):
        # G0 is exported. Capture resets to G1 while the exporter is offline,
        # then again to G2 before G1's status ever reaches the mirror.
        tdb, remote, exporter, path = ledger
        ids = [_trade(tdb, run_mode="live") for _ in range(3)]
        pid = tdb.create_position(1, "M", "kalshi", expected_pnl=0.1, run_mode="live")
        exporter.sync_once()
        g0 = tdb.get_ledger_meta()
        old_status = dict(remote.tables["ledger_sync_status"][_source_key(g0)])
        added = []

        def delete_trade():
            tdb.conn.execute("DELETE FROM trades WHERE id = ?", (ids[2],))
            tdb.conn.commit()

        def replace_position_and_trade():
            tdb.conn.execute("DELETE FROM positions WHERE id = ?", (pid,))
            tdb.conn.commit()
            added.append(_trade(tdb, run_mode="live"))

        _, g1 = self._regenerate(tdb, path, "triggers", delete_trade)
        _, g2 = self._regenerate(tdb, path, "epoch", replace_position_and_trade)
        assert exporter.sync_once().snapshot_complete
        status = remote.tables["ledger_sync_status"][_source_key(g2)]
        assert status["supersedes_db_instance_ids"] == [g0["db_instance_id"], g1["db_instance_id"]]
        assert _source_key(g1) not in remote.tables["ledger_sync_status"]  # G1 never published
        # Same trade count as G0, different rows; G0's rows are all retired.
        assert [r["source_id"] for r in remote.current("ledger_trades")] == [ids[0], ids[1], added[0]]
        assert remote.current("ledger_positions") == []
        assert remote.mirror_complete(_source_key(g2)) and not remote.mirror_complete(_source_key(g0))
        # A delayed G0 status, stamped later, changes nothing.
        remote.apply("ledger_sync_status", [dict(old_status, last_attempt_at="9999-01-01T00:00:00+00:00",
                                                 last_success_at="9999-01-01T00:00:00+00:00")], "source_key")
        assert not remote.mirror_complete(_source_key(g0)) and remote.mirror_complete(_source_key(g2))
        assert [r["source_id"] for r in remote.current("ledger_trades")] == [ids[0], ids[1], added[0]]


class TestReconcileFills:
    START, END = "2026-09-28T00:00:00+00:00", "2026-09-29T00:00:00+00:00"
    COVERAGE = {"account_ref": "k1", "interval_start": START, "interval_end": END,
                "complete": True, "source": "kalshi-fills-export-2026-09-28"}
    LEDGER = [
        {"venue": "kalshi", "run_mode": "live", "status": "filled", "fill_price": 0.4, "fill_qty": 10,
         "order_id": "A", "account_ref": "k1", "recorded_at": "2026-09-28T10:00:00+00:00"},
        {"venue": "kalshi", "run_mode": "live", "status": "filled", "fill_price": 0.5, "fill_qty": 3,
         "order_id": "B", "account_ref": "k1", "recorded_at": "2026-09-28T11:00:00+00:00"},
        {"venue": "kalshi", "run_mode": "paper", "status": "filled", "fill_price": 0.5,
         "order_id": "dry_1", "account_ref": "k1", "recorded_at": "2026-09-28T11:00:00+00:00"},
    ]
    VENUE = [
        {"order_id": "A", "qty": "10", "fee_usd": "0.07", "filled_at": "2026-09-28T10:00:01Z"},
        {"order_id": "B", "qty": 3, "fee_usd": "0.02", "filled_at": "2026-09-28T11:00:01Z"},
    ]

    def _run(self, venue=None, ledger=None, **kw):
        args = dict(venue="kalshi", account_ref="k1", interval_start=self.START,
                    interval_end=self.END, venue_coverage=self.COVERAGE)
        args.update(kw)
        return reconcile_fills(self.VENUE if venue is None else venue,
                               self.LEDGER if ledger is None else ledger, **args)

    def test_caller_evidence_gaps_make_an_agreeing_check_incomplete(self):
        result = self._run(evidence_gaps=["ledger_mirror_stale", "", None])
        assert result["status"] == "incomplete"
        assert result["incomplete_reasons"] == ["ledger_mirror_stale"]
        assert self._run()["status"] == "matched"

    def test_matched_with_verified_coverage(self):
        out = self._run()
        assert out["status"] == "matched" and out["incomplete_reasons"] == []
        assert out["matched_order_count"] == 2 and out["coverage_verified"] is True
        assert Decimal(out["venue_fees_usd"]) == Decimal("0.09") and out["fees_complete"] is True

    def test_verified_zero_activity_interval_is_matched(self):
        out = self._run(venue=[], ledger=[])
        assert out["status"] == "matched"
        assert (out["venue_order_count"], out["ledger_order_count"]) == (0, 0)

    def test_empty_inputs_without_coverage_are_incomplete(self):
        out = self._run(venue=[], ledger=[], venue_coverage=None)
        assert out["status"] == "incomplete" and "venue_coverage_missing" in out["incomplete_reasons"]

    def test_absent_account_is_incomplete(self):
        out = self._run(account_ref=None)
        assert out["status"] == "incomplete" and "account_unknown" in out["incomplete_reasons"]

    def test_coverage_must_be_complete_same_account_exact_interval(self):
        for change in ({"complete": False}, {"complete": "true"}, {"account_ref": "k2"},
                       {"interval_end": "2026-09-28T23:00:00+00:00"}, {"source": ""}):
            out = self._run(venue_coverage={**self.COVERAGE, **change})
            assert out["status"] == "incomplete", change
            assert "venue_coverage_unverified" in out["incomplete_reasons"]

    def test_equivalent_timezone_offsets_match(self):
        coverage = {**self.COVERAGE, "interval_start": "2026-09-27T20:00:00-04:00",
                    "interval_end": "2026-09-28T20:00:00-04:00"}
        venue = [dict(self.VENUE[0], filled_at="2026-09-28T06:00:01-04:00"), self.VENUE[1]]
        out = self._run(venue=venue, venue_coverage=coverage,
                        interval_start="2026-09-28T02:00:00+02:00", interval_end="2026-09-29T00:00:00Z")
        assert out["status"] == "matched", out
        assert out["interval_start"] == "2026-09-28T00:00:00+00:00"

    def test_naive_or_invalid_interval_is_incomplete(self):
        for start, end in (("2026-09-28T00:00:00", self.END), (self.START, "not a time"), (self.END, self.START)):
            out = self._run(interval_start=start, interval_end=end)
            assert out["status"] == "incomplete" and "invalid_interval" in out["incomplete_reasons"]

    def test_only_intended_interval_counts(self):
        """Bounds are UTC instants, not strings: each record below sorts the
        wrong way as text."""
        looks_inside = {"order_id": "C", "qty": 1, "fee_usd": 0, "filled_at": "2026-09-28T23:30:00-01:00"}
        looks_outside = {"order_id": "E", "qty": 1, "fee_usd": 0, "filled_at": "2026-09-29T00:30:00+01:00"}
        out = self._run(venue=self.VENUE + [looks_inside])
        assert out["status"] == "matched" and out["venue_records_out_of_interval"] == 1
        out = self._run(venue=self.VENUE + [looks_outside])
        assert out["status"] == "mismatched" and out["missing_in_ledger"] == ["E"]
        assert out["venue_records_out_of_interval"] == 0

    @staticmethod
    def _live(oid, qty, at, **kw):
        return {"venue": "kalshi", "run_mode": "live", "status": "filled", "fill_price": 0.5,
                "fill_qty": qty, "order_id": oid, "account_ref": "k1", "recorded_at": at, **kw}

    def test_order_filled_across_midnight_compares_only_the_days_part(self):
        # Order M fills 0.5 on each side of midnight; only today's half is in
        # the venue's day, so only today's ledger row may be summed.
        venue = [{"order_id": "M", "qty": "0.50", "fee_usd": 0, "filled_at": "2026-09-28T23:40:00Z"}]
        ledger = [self._live("M", 0.5, "2026-09-28T23:40:03+00:00"),
                  self._live("M", 0.5, "2026-09-29T00:20:00+00:00"),
                  self._live("M", 2.0, "2026-09-27T09:00:00+00:00")]
        out = self._run(venue=venue, ledger=ledger)
        assert out["status"] == "matched", out
        assert out["boundary_ambiguous_orders"] == [] and out["qty_mismatch"] == []

    def test_fill_recorded_just_after_the_day_ends_is_ambiguous_not_missing(self):
        venue = [{"order_id": "L", "qty": "1.00", "fee_usd": 0, "filled_at": "2026-09-28T23:59:30Z"}]
        ledger = [self._live("L", 1, "2026-09-29T00:00:20+00:00")]
        out = self._run(venue=venue, ledger=ledger)
        assert out["status"] == "incomplete", out
        assert out["incomplete_reasons"] == ["ledger_boundary_ambiguous"]
        assert out["boundary_ambiguous_orders"] == ["L"] and out["missing_in_ledger"] == ["L"]

    def test_previous_days_fill_recorded_after_midnight_is_ambiguous_not_extra(self):
        ledger = [self._live("P", 1, "2026-09-28T00:01:00+00:00")]
        out = self._run(venue=[], ledger=ledger)
        assert out["status"] == "incomplete"
        assert out["incomplete_reasons"] == ["ledger_boundary_ambiguous"]
        assert out["boundary_ambiguous_orders"] == ["P"]

    def test_ambiguity_window_edges(self):
        # [b - skew, b + lag) around each boundary; outside it recorded_at decides.
        cases = {"2026-09-27T23:57:59+00:00": "matched",      # start - 121s: yesterday
                 "2026-09-27T23:58:00+00:00": "incomplete",   # start - skew
                 "2026-09-28T00:04:59+00:00": "incomplete",   # start + lag - 1s
                 "2026-09-29T00:05:00+00:00": "matched"}      # end + lag: tomorrow
        for at, expected in cases.items():
            out = self._run(ledger=self.LEDGER + [self._live("Z", 1, at)])
            assert out["status"] == expected, (at, out)
        ledger = self.LEDGER + [self._live("Z", 1, "2026-09-28T00:05:00+00:00")]
        out = self._run(ledger=ledger)          # start + lag: today, and the venue lacks it
        assert out["status"] == "mismatched" and out["missing_in_venue"] == ["Z"]

    def test_unattributable_rows_near_a_boundary_are_incomplete(self):
        for kw in ({"run_mode": None}, {"account_ref": None}, {"order_id": ""}):
            row = self._live("U", 1, "2026-09-29T00:02:00+00:00", **kw)
            out = self._run(ledger=self.LEDGER + [row])
            assert out["status"] == "incomplete", kw

    def test_recording_window_settings_must_be_non_negative(self):
        from datetime import timedelta
        for kw in ({"recording_lag": timedelta(seconds=-1)}, {"clock_skew": timedelta(seconds=-1)},
                   {"recording_lag": 300}):
            with pytest.raises(ValueError):
                self._run(**kw)
        zero = self._run(ledger=self.LEDGER + [self._live("Z", 1, "2026-09-29T00:00:00+00:00")],
                         recording_lag=timedelta(0), clock_skew=timedelta(0))
        assert zero["status"] == "matched"

    def test_missing_venue_qty_is_incomplete_not_matched(self):
        venue = [dict(self.VENUE[0], qty=None), self.VENUE[1]]
        out = self._run(venue=venue)
        assert out["status"] == "incomplete" and "venue_record_qty_unknown" in out["incomplete_reasons"]

    def test_missing_ledger_qty_is_incomplete_not_matched(self):
        ledger = [dict(self.LEDGER[0], fill_qty=None)] + self.LEDGER[1:]
        out = self._run(ledger=ledger)
        assert out["status"] == "incomplete" and "ledger_record_qty_unknown" in out["incomplete_reasons"]

    def test_non_finite_values_are_unknown(self):
        for bad in ("NaN", "Infinity", "-inf", float("nan"), float("inf"), "sNaN", True):
            assert ledger_sync._dec(bad) is None, bad
            venue = [dict(self.VENUE[0], qty=bad), self.VENUE[1]]
            assert self._run(venue=venue)["status"] == "incomplete", bad
        fee_nan = [dict(self.VENUE[0], fee_usd="NaN"), self.VENUE[1]]
        out = self._run(venue=fee_nan)
        assert out["status"] == "matched" and out["venue_fees_usd"] is None and out["fees_complete"] is False

    def test_venue_row_without_order_id_is_incomplete(self):
        out = self._run(venue=self.VENUE + [{"order_id": "", "qty": 1, "filled_at": "2026-09-28T12:00:00Z"}])
        assert out["status"] == "incomplete" and "venue_record_missing_order_id" in out["incomplete_reasons"]

    def test_venue_row_without_time_or_other_account_is_incomplete(self):
        out = self._run(venue=self.VENUE + [{"order_id": "Z", "qty": 1}])
        assert "venue_record_time_unknown" in out["incomplete_reasons"]
        out = self._run(venue=self.VENUE + [{"order_id": "Z", "qty": 1, "account_ref": "k2",
                                             "filled_at": "2026-09-28T12:00:00Z"}])
        assert "venue_record_account_mismatch" in out["incomplete_reasons"]

    def test_unattributed_ledger_fill_is_incomplete(self):
        unknown = {"venue": "kalshi", "run_mode": "unknown", "status": "filled", "fill_price": 0.3,
                   "order_id": "Q", "account_ref": None, "recorded_at": "2026-09-28T09:00:00+00:00"}
        out = self._run(ledger=self.LEDGER + [unknown])
        assert out["status"] == "incomplete" and "ledger_fill_unattributed" in out["incomplete_reasons"]

    def test_ledger_fill_without_order_id_is_incomplete(self):
        out = self._run(ledger=self.LEDGER + [dict(self.LEDGER[0], order_id=None)])
        assert "ledger_record_missing_order_id" in out["incomplete_reasons"]

    def test_valid_evidence_that_disagrees_is_mismatched(self):
        venue = [dict(self.VENUE[0], qty=9), {"order_id": "C", "qty": 1, "fee_usd": 0,
                                               "filled_at": "2026-09-28T12:00:00Z"}]
        out = self._run(venue=venue)
        assert out["status"] == "mismatched" and out["incomplete_reasons"] == []
        assert out["missing_in_ledger"] == ["C"] and out["missing_in_venue"] == ["B"]
        assert out["qty_mismatch"] == ["A"]

    def test_paper_rows_never_count(self):
        out = self._run(venue=[], ledger=self.LEDGER[2:])
        assert out["ledger_order_count"] == 0 and out["status"] == "matched"

    def test_paper_rows_never_count_as_run_modes(self):
        assert "dry" not in db_module.RUN_MODES


class TestRemoteSchemaContract:
    """Every field the exporter sends must be a column in the draft migration."""

    DRAFT = os.path.join(os.path.dirname(__file__), "..", "supabase", "drafts", "0007_trade_ledger_reporting.sql")

    def _columns(self, table):
        import re
        with open(self.DRAFT) as fh:
            sql = fh.read()
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

    def test_reconciliation_fields_exist_remotely(self):
        cols = self._columns("ledger_venue_reconciliations")
        rec = TestReconcileFills()
        for out in (rec._run(), rec._run(account_ref=None), rec._run(venue=[dict(rec.VENUE[0], qty=9)])):
            assert set(out) <= cols, set(out) - cols


class _ScriptedExporter:
    """sync_once stand-in: records the thread and concurrency of each call."""

    def __init__(self, outcomes=None, delay=0.0):
        self.outcomes = list(outcomes or [])
        self.delay = delay
        self.calls = 0
        self.threads: list[str] = []
        self.active = 0
        self.max_active = 0
        self.closed = False
        self.ran = threading.Event()
        self._lock = threading.Lock()

    def sync_once(self):
        with self._lock:
            self.active += 1
            self.max_active = max(self.max_active, self.active)
        try:
            self.calls += 1
            self.threads.append(threading.current_thread().name)
            time.sleep(self.delay)
            outcome = self.outcomes.pop(0) if self.outcomes else "ok"
            if isinstance(outcome, Exception):
                raise outcome
            return outcome
        finally:
            with self._lock:
                self.active -= 1
            self.ran.set()

    def close(self):
        self.closed_while_active = self.active > 0
        self.closed = True


class TestLedgerSyncWorker:
    @pytest.mark.parametrize("interval", [0, -1, float("nan"), float("inf"), True, "60"])
    def test_invalid_interval_is_refused(self, interval):
        with pytest.raises(ValueError):
            ledger_sync.LedgerSyncWorker(_ScriptedExporter(), interval)

    def test_runs_on_its_own_thread_repeatedly_and_survives_failures(self):
        exporter = _ScriptedExporter(outcomes=[RuntimeError("503"), "ok", "ok"])
        worker = ledger_sync.LedgerSyncWorker(exporter, 0.01)
        worker.start()
        deadline = time.monotonic() + 5
        while exporter.calls < 3 and time.monotonic() < deadline:
            time.sleep(0.01)
        worker.stop(final_sync=False)
        assert exporter.calls >= 3
        assert set(exporter.threads) == {"ledger-sync"}
        assert worker.failures == 1 and worker.last_error is None
        assert exporter.closed and not worker.alive

    def test_never_runs_two_syncs_at_once(self):
        exporter = _ScriptedExporter(delay=0.05)
        worker = ledger_sync.LedgerSyncWorker(exporter, 0.001)
        worker.start()
        extra = [threading.Thread(target=worker.run_once) for _ in range(4)]
        for t in extra:
            t.start()
        for t in extra:
            t.join()
        worker.stop(final_sync=False)
        assert exporter.max_active == 1

    def test_stop_exports_the_final_tail_then_closes(self):
        exporter = _ScriptedExporter()
        worker = ledger_sync.LedgerSyncWorker(exporter, 3600)
        worker.start()
        assert exporter.ran.wait(5)
        worker.stop()
        assert exporter.calls == 2 and exporter.closed

    def test_slow_export_bounds_stop_and_closes_only_after_it_finishes(self):
        exporter = _ScriptedExporter(delay=0.3)
        worker = ledger_sync.LedgerSyncWorker(exporter, 3600)
        worker.start()
        began = time.monotonic()
        assert worker.stop(timeout=0.05) is False
        assert time.monotonic() - began < 0.25
        assert not exporter.closed
        # The in-flight export and the final tail finish on the worker thread,
        # which then closes the exporter itself, never under an export.
        assert worker.closed.wait(5)
        assert exporter.calls == 2 and exporter.closed and not exporter.closed_while_active
        assert set(exporter.threads) == {"ledger-sync"}

    def test_stop_without_start_still_exports_then_closes(self):
        exporter = _ScriptedExporter()
        worker = ledger_sync.LedgerSyncWorker(exporter, 3600)
        assert worker.stop() is True
        assert exporter.calls == 1 and exporter.closed
        worker.start()                      # a stopped worker never restarts
        assert not worker.alive and exporter.calls == 1

    def test_async_stop_does_not_hold_up_the_rest_of_shutdown(self):
        import asyncio

        exporter = _ScriptedExporter(delay=0.5)
        worker = ledger_sync.LedgerSyncWorker(exporter, 3600)
        worker.start()
        steps = []

        async def shutdown():
            steps.append(("ledger", await ledger_sync.stop_ledger_sync_worker(worker, timeout=0.05)))
            steps.append(("feeds", time.monotonic()))   # stands in for feed_manager.stop()

        began = time.monotonic()
        asyncio.run(shutdown())
        assert steps[0] == ("ledger", False) and steps[1][0] == "feeds"
        assert steps[1][1] - began < 0.4
        assert worker.closed.wait(5) and not exporter.closed_while_active

    def test_async_stop_never_raises(self):
        import asyncio

        class Broken:
            def stop(self, timeout):
                raise RuntimeError("boom")
        assert asyncio.run(ledger_sync.stop_ledger_sync_worker(Broken(), timeout=0.01)) is False

    def test_start_requires_capture_and_a_client(self, tmp_path):
        path = str(tmp_path / "t.db")
        with pytest.raises(RuntimeError, match="LEDGER_CAPTURE_ENABLED"):
            ledger_sync.start_ledger_sync_worker(path, capture_enabled=False, interval_seconds=60,
                                                 service="svc", client_factory=FakeRemote)
        with pytest.raises(RuntimeError, match="Supabase client unavailable"):
            ledger_sync.start_ledger_sync_worker(path, capture_enabled=True, interval_seconds=60,
                                                 service="svc", client_factory=lambda: None)

    def test_started_worker_exports_rows_off_the_caller_thread(self, ledger):
        tdb, _, _, path = ledger
        tid = _trade(tdb, status="filled", run_mode="live", fill_qty=2.0, order_id="o-1")
        remote = FakeRemote()
        worker = ledger_sync.start_ledger_sync_worker(path, capture_enabled=True, interval_seconds=3600,
                                                      service="arb-scanner", client_factory=lambda: remote)
        try:
            deadline = time.monotonic() + 5
            while worker.runs < 1 and time.monotonic() < deadline:
                time.sleep(0.01)
            assert worker.last_error is None
            rows = remote.rows("ledger_trades")
            assert [(r["source_id"], r["fill_qty"], r["run_mode"]) for r in rows] == [(tid, 2.0, "live")]
            assert remote.tables["ledger_sync_status"]
        finally:
            worker.stop(final_sync=False)

    def test_continuous_mode_runs_the_worker_for_every_mode(self):
        # The exporter is started once at init (any --mode, including
        # mm-pilot) and stopped after the pilot at shutdown, not tied to scans.
        with open(os.path.join(os.path.dirname(__file__), "..", "continuous.py")) as fh:
            src = fh.read()
        init = src.index("start_ledger_sync_worker(")
        pilot_start = src.index("if (config.MM_KALSHI_PILOT_ENABLED")
        assert init < pilot_start
        assert "scan_count % max(1, config.LEDGER_SYNC" not in src
        cleanup = src.index("await stop_ledger_sync_worker(_ledger_sync_worker, timeout=")
        assert src.index('logger.info("Stopping Kalshi MM pilot...")') < cleanup
        assert cleanup < src.index('logger.info("Stopping WebSocket feeds...")')
