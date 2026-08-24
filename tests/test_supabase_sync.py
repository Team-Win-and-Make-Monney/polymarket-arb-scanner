"""Unit tests for the Supabase rewards sync (supabase_sync.py)."""

import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from supabase_sync import (  # noqa: E402
    REWARDS_TABLE,
    RewardSync,
    reward_metric_to_event,
)


def _metric(**overrides):
    row = {
        'id': 1,
        'platform': 'kalshi',
        'market_key': 'KXBTC-26',
        'order_id': 'ord-1',
        'event': 'placed',
        'size': 100.0,
        'spread': 0.02,
        'resting_seconds': 0,
        'timestamp': 1_700_000_000,
    }
    row.update(overrides)
    return row


class TestMapping:
    def test_kalshi_maps_to_lip_engine(self):
        event = reward_metric_to_event(_metric())
        assert event['engine'] == 'kalshi-lip'
        assert event['lane'] == 'prediction-markets'
        assert event['tax_bucket'] == 'ordinary'
        assert event['source_key'] == 'reward_metric:1'

    def test_event_date_from_timestamp(self):
        event = reward_metric_to_event(_metric(timestamp=1_700_000_000))
        assert event['event_date'] == '2023-11-14'

    def test_notes_capture_activity(self):
        event = reward_metric_to_event(_metric(event='cancelled', resting_seconds=42))
        assert 'cancelled' in event['notes']
        assert 'resting_s=42' in event['notes']

    def test_none_timestamp_uses_epoch(self):
        event = reward_metric_to_event(_metric(timestamp=None))
        assert event['event_date'] == '1970-01-01'


class _FakeUpsertChain:
    def __init__(self, recorder):
        self._recorder = recorder

    def upsert(self, rows, on_conflict=None):
        self._recorder['rows'] = rows
        self._recorder['on_conflict'] = on_conflict
        return self

    def execute(self):
        self._recorder['executed'] = True
        return {'data': self._recorder['rows']}


class _FakeClient:
    def __init__(self):
        self.recorder = {}

    def table(self, name):
        self.recorder['table'] = name
        return _FakeUpsertChain(self.recorder)


class TestUpsert:
    def test_upsert_sends_records_with_conflict_target(self):
        client = _FakeClient()
        sync = RewardSync(client)
        n = sync.upsert_events([reward_metric_to_event(_metric())])
        assert n == 1
        assert client.recorder['table'] == REWARDS_TABLE
        assert client.recorder['on_conflict'] == 'engine,source_key'
        assert client.recorder['executed'] is True

    def test_empty_events_is_noop(self):
        client = _FakeClient()
        sync = RewardSync(client)
        assert sync.upsert_events([]) == 0
        assert 'executed' not in client.recorder

    def test_missing_required_field_raises(self):
        client = _FakeClient()
        sync = RewardSync(client)
        bad = reward_metric_to_event(_metric())
        bad['engine'] = ''
        with pytest.raises(ValueError):
            sync.upsert_events([bad])

    def test_upsert_propagates_client_error(self):
        client = MagicMock()
        client.table.return_value.upsert.return_value.execute.side_effect = RuntimeError('boom')
        sync = RewardSync(client)
        with pytest.raises(RuntimeError):
            sync.upsert_events([reward_metric_to_event(_metric())])


class TestSyncFromDb:
    def test_sync_reads_db_and_upserts(self, tmp_path):
        from db import TradeDB
        db = TradeDB(db_path=str(tmp_path / 'trades.db'))
        try:
            db.log_reward_metric('kalshi', 'KXBTC-26', 'ord-1', 'placed', 100.0, 0.02, 0)
            db.log_reward_metric('kalshi', 'KXETH-26', 'ord-2', 'placed', 50.0, 0.03, 0)

            client = _FakeClient()
            sync = RewardSync(client, db=db)
            n = sync.sync_reward_metrics()
            assert n == 2
            assert len(client.recorder['rows']) == 2
            assert all(r['engine'] == 'kalshi-lip' for r in client.recorder['rows'])
        finally:
            db.close()

    def test_sync_without_db_raises(self):
        sync = RewardSync(_FakeClient())
        with pytest.raises(RuntimeError):
            sync.sync_reward_metrics()


# ---------------------------------------------------------------------------
# OpportunitySync — paper-record mirror (2026-07-21 observability build-out)
# ---------------------------------------------------------------------------


class TestOpportunitySync:
    @pytest.fixture
    def opps_db(self, tmp_path):
        from db import TradeDB
        db = TradeDB(str(tmp_path / "opps.db"))
        db.log_opportunity("KalshiMulti(4)", "Fed Combo", "0.6,0.29", 0.91, 0.04, 0.033, 151.0, "dry_run")
        db.log_opportunity("CrossPlatform", "X vs Y", "0.5,0.4", 0.90, 0.06, 0.066, 80.0, "skipped:risk")
        yield db
        db.conn.close()

    def test_syncs_new_rows_and_returns_high_water_mark(self, opps_db):
        from supabase_sync import OpportunitySync
        client = MagicMock()
        db = opps_db
        sync = OpportunitySync(client, db=db)

        last_id = sync.sync_opportunities(after_id=0)

        client.table.assert_called_once_with("paper_opportunities")
        rows = client.table.return_value.upsert.call_args[0][0]
        assert len(rows) == 2
        assert {r["opp_type"] for r in rows} == {"KalshiMulti(4)", "CrossPlatform"}
        assert all(r["engine"] == "arbgrid" for r in rows)
        assert all("source_id" in r and "detected_at" in r for r in rows)
        assert client.table.return_value.upsert.call_args[1]["on_conflict"] == "engine,source_id"
        assert last_id == max(r["source_id"] for r in rows)

    def test_incremental_sync_skips_already_synced(self, opps_db):
        from supabase_sync import OpportunitySync
        client = MagicMock()
        db = opps_db
        sync = OpportunitySync(client, db=db)
        hwm = sync.sync_opportunities(after_id=0)
        client.reset_mock()

        # No new rows: no upsert call, high-water mark unchanged.
        assert sync.sync_opportunities(after_id=hwm) == hwm
        client.table.assert_not_called()

        db.log_opportunity("KalshiBinary", "New", "0.9", 0.95, 0.02, 0.02, 40.0, "dry_run")
        new_hwm = sync.sync_opportunities(after_id=hwm)
        rows = client.table.return_value.upsert.call_args[0][0]
        assert len(rows) == 1
        assert new_hwm > hwm

    def test_client_error_propagates_and_hwm_not_advanced(self, opps_db):
        from supabase_sync import OpportunitySync
        client = MagicMock()
        client.table.return_value.upsert.return_value.execute.side_effect = RuntimeError("supabase down")
        db = opps_db
        sync = OpportunitySync(client, db=db)
        with pytest.raises(RuntimeError):
            sync.sync_opportunities(after_id=0)

    def test_without_db_raises(self):
        from supabase_sync import OpportunitySync
        with pytest.raises(RuntimeError):
            OpportunitySync(MagicMock(), db=None).sync_opportunities(after_id=0)


class TestRemoteHighWaterMark:
    def test_reads_max_source_id_from_supabase(self):
        from supabase_sync import OpportunitySync
        client = MagicMock()
        chain = client.table.return_value.select.return_value.eq.return_value.order.return_value.limit.return_value
        chain.execute.return_value = MagicMock(data=[{"source_id": 445490}])
        assert OpportunitySync(client).get_remote_high_water_mark() == 445490

    def test_empty_table_returns_zero(self):
        from supabase_sync import OpportunitySync
        client = MagicMock()
        chain = client.table.return_value.select.return_value.eq.return_value.order.return_value.limit.return_value
        chain.execute.return_value = MagicMock(data=[])
        assert OpportunitySync(client).get_remote_high_water_mark() == 0

    def test_outage_falls_back_to_local_max_not_full_backfill(self, tmp_path):
        from supabase_sync import OpportunitySync
        from db import TradeDB
        db = TradeDB(str(tmp_path / "o.db"))
        try:
            db.log_opportunity("T", "m", "p", 1.0, 0.01, 0.01, 1.0, "dry_run")
            local_max_row = db.conn.execute("SELECT MAX(id) FROM opportunities").fetchone()
            client = MagicMock()
            client.table.return_value.select.side_effect = RuntimeError("supabase down")
            assert OpportunitySync(client, db=db).get_remote_high_water_mark() == local_max_row[0]
        finally:
            db.conn.close()


# ---------------------------------------------------------------------------
# PostgREST fallback client (supabase-py is not a runtime dependency)
# ---------------------------------------------------------------------------


class TestRestClientShim:
    def _client(self):
        from supabase_sync import PostgrestClient
        session = MagicMock()
        session.post.return_value = MagicMock(status_code=201, text="")
        resp = MagicMock(status_code=200)
        resp.json.return_value = [{"source_id": 42}]
        session.get.return_value = resp
        return PostgrestClient("https://proj.supabase.co", "svc-key", session=session), session

    def test_upsert_posts_with_merge_duplicates(self):
        client, session = self._client()
        client.table("paper_opportunities").upsert(
            [{"engine": "arbgrid", "source_id": 1}], on_conflict="engine,source_id"
        ).execute()
        url = session.post.call_args[0][0]
        assert url == "https://proj.supabase.co/rest/v1/paper_opportunities"
        kwargs = session.post.call_args[1]
        assert kwargs["params"]["on_conflict"] == "engine,source_id"
        assert "resolution=merge-duplicates" in kwargs["headers"]["Prefer"]
        assert kwargs["headers"]["apikey"] == "svc-key"
        assert kwargs["json"] == [{"engine": "arbgrid", "source_id": 1}]

    def test_upsert_error_status_raises(self):
        client, session = self._client()
        session.post.return_value = MagicMock(status_code=409, text="conflict")
        with pytest.raises(RuntimeError):
            client.table("t").upsert([{"a": 1}], on_conflict="a").execute()

    def test_select_chain_builds_query_and_returns_data(self):
        client, session = self._client()
        result = (
            client.table("paper_opportunities")
            .select("source_id")
            .eq("engine", "arbgrid")
            .order("source_id", desc=True)
            .limit(1)
            .execute()
        )
        assert result.data == [{"source_id": 42}]
        params = session.get.call_args[1]["params"]
        assert params["select"] == "source_id"
        assert params["engine"] == "eq.arbgrid"
        assert params["order"] == "source_id.desc"
        assert params["limit"] == 1

    def test_build_client_falls_back_to_shim_without_sdk(self, monkeypatch):
        import supabase_sync
        monkeypatch.setenv("SUPABASE_URL", "https://proj.supabase.co")
        monkeypatch.setenv("SUPABASE_SERVICE_KEY", "svc-key")
        # Simulate supabase-py missing regardless of local environment.
        import builtins
        real_import = builtins.__import__
        def _no_sdk(name, *a, **k):
            if name == "supabase":
                raise ImportError("No module named 'supabase'")
            return real_import(name, *a, **k)
        monkeypatch.setattr(builtins, "__import__", _no_sdk)
        client = supabase_sync.build_client_from_env()
        assert isinstance(client, supabase_sync.PostgrestClient)

    @pytest.mark.parametrize("url", ["http://proj.supabase.co", "https://127.0.0.1"])
    def test_build_client_rejects_unsafe_url(self, monkeypatch, url):
        import supabase_sync

        monkeypatch.setenv("SUPABASE_URL", url)
        monkeypatch.setenv("SUPABASE_SERVICE_KEY", "svc-key")
        monkeypatch.delenv("ALLOW_PRIVATE_INTERNAL_URLS", raising=False)
        with pytest.raises(ValueError, match="SUPABASE_URL"):
            supabase_sync.build_client_from_env()


class TestWindowSeededHighWaterMark:
    """An empty remote must not trigger a months-long historical backfill —
    seed the mark just before the first in-window opportunity instead."""

    def _db(self, tmp_path):
        from db import TradeDB
        db = TradeDB(str(tmp_path / "w.db"))
        with db._lock:
            for i, ts in enumerate(
                ["2026-03-01T00:00:00+00:00", "2026-07-20T00:00:00+00:00",
                 "2026-07-21T22:00:00+00:00", "2026-07-22T01:00:00+00:00"], 1):
                db.conn.execute(
                    "INSERT INTO opportunities (id, timestamp, type, market, prices,"
                    " total_cost, net_profit, net_roi, depth, action)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (i, ts, "T", "m", "p", 1.0, 0.01, 0.01, 1.0, "dry_run"))
            db.conn.commit()
        return db

    def test_empty_remote_seeds_from_window_start(self, tmp_path):
        from supabase_sync import OpportunitySync
        db = self._db(tmp_path)
        try:
            client = MagicMock()
            chain = client.table.return_value.select.return_value.eq.return_value.order.return_value.limit.return_value
            chain.execute.return_value = MagicMock(data=[])
            sync = OpportunitySync(client, db=db)
            # Window opened 2026-07-21T21:08Z -> rows 3 and 4 are in-window;
            # the mark must sit just before row 3, skipping 1-2 entirely.
            from datetime import datetime, timezone
            ws = datetime(2026, 7, 21, 21, 8, tzinfo=timezone.utc).timestamp()
            assert sync.get_remote_high_water_mark(window_start_ts=ws) == 2
        finally:
            db.conn.close()

    def test_remote_below_window_seed_is_floored_to_seed(self, tmp_path):
        # A partially backfilled remote (pre-window rows only) must not pull
        # the resume point back into history: the window seed is a floor.
        from supabase_sync import OpportunitySync
        db = self._db(tmp_path)
        try:
            client = MagicMock()
            chain = client.table.return_value.select.return_value.eq.return_value.order.return_value.limit.return_value
            chain.execute.return_value = MagicMock(data=[{"source_id": 1}])
            sync = OpportunitySync(client, db=db)
            from datetime import datetime, timezone
            ws = datetime(2026, 7, 21, 21, 8, tzinfo=timezone.utc).timestamp()
            assert sync.get_remote_high_water_mark(window_start_ts=ws) == 2
        finally:
            db.conn.close()

    def test_nonempty_remote_ignores_window_seed(self, tmp_path):
        from supabase_sync import OpportunitySync
        db = self._db(tmp_path)
        try:
            client = MagicMock()
            chain = client.table.return_value.select.return_value.eq.return_value.order.return_value.limit.return_value
            chain.execute.return_value = MagicMock(data=[{"source_id": 3}])
            sync = OpportunitySync(client, db=db)
            assert sync.get_remote_high_water_mark(window_start_ts=1.0) == 3
        finally:
            db.conn.close()

    def test_window_with_no_inwindow_rows_seeds_at_local_max(self, tmp_path):
        # Window configured but nothing detected in-window yet: seed at the
        # local max so an empty remote never replays pre-window history.
        from supabase_sync import OpportunitySync
        db = self._db(tmp_path)
        try:
            client = MagicMock()
            chain = client.table.return_value.select.return_value.eq.return_value.order.return_value.limit.return_value
            chain.execute.return_value = MagicMock(data=[])
            sync = OpportunitySync(client, db=db)
            from datetime import datetime, timezone
            ws = datetime(2027, 1, 1, tzinfo=timezone.utc).timestamp()  # future window
            assert sync.get_remote_high_water_mark(window_start_ts=ws) == 4  # local max id
        finally:
            db.conn.close()

    def test_empty_remote_without_window_keeps_zero(self, tmp_path):
        from supabase_sync import OpportunitySync
        db = self._db(tmp_path)
        try:
            client = MagicMock()
            chain = client.table.return_value.select.return_value.eq.return_value.order.return_value.limit.return_value
            chain.execute.return_value = MagicMock(data=[])
            assert OpportunitySync(client, db=db).get_remote_high_water_mark() == 0
        finally:
            db.conn.close()
