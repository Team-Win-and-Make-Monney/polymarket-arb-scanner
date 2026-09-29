-- Assertions for supabase/drafts/0007_trade_ledger_reporting.sql.
-- Run by tests/supabase-ledger-reporting.sh in a DISPOSABLE database after the
-- repository migrations and the draft. Every failed check raises.

\set ON_ERROR_STOP 1

-- Exporter-style upsert (what PostgREST merge-duplicates issues), as the
-- backend role the exporter uses today.
create or replace function pg_temp.upsert_trade(
  k text, ver bigint, epoch text, st text, mode text, acct text, del boolean default false)
returns void language sql as $$
  insert into public.ledger_trades (ledger_key, source_system, service, db_instance_id, source_table,
    source_id, source_version, capture_epoch, deleted, venue, account_ref, run_mode, mode_evidence,
    recorded_at, status, fill_price, order_id)
  values (k, 'arbgrid', 'svc', 'db1', 'trades', split_part(k, ':', 5)::bigint, ver, epoch, del, 'kalshi',
    acct, mode, case when mode = 'unknown' then 'none' else 'writer_stamped' end,
    '2026-09-28T10:00:00+00:00', st, 0.4, 'ord-' || k)
  on conflict (ledger_key) do update set
    source_version = excluded.source_version, capture_epoch = excluded.capture_epoch,
    deleted = excluded.deleted, status = excluded.status, run_mode = excluded.run_mode,
    account_ref = excluded.account_ref;
$$;

set role service_role;
select pg_temp.upsert_trade('arbgrid:svc:db1:trades:1', 5, 'e1', 'pending', 'live', 'k1');
select pg_temp.upsert_trade('arbgrid:svc:db1:trades:1', 5, 'e1', 'pending', 'live', 'k1');  -- replay
select pg_temp.upsert_trade('arbgrid:svc:db1:trades:1', 9, 'e1', 'filled', 'live', 'k1');   -- newer
select pg_temp.upsert_trade('arbgrid:svc:db1:trades:1', 7, 'e1', 'pending', 'live', 'k1');  -- stale retry
select pg_temp.upsert_trade('arbgrid:svc:db1:trades:2', 3, 'e1', 'filled', 'unknown', null);
reset role;

do $$ begin
  if (select count(*) from public.ledger_trades) <> 2 then
    raise exception 'replay created duplicates';
  end if;
  if (select status from public.ledger_trades where source_id = 1) <> 'filled'
     or (select source_version from public.ledger_trades where source_id = 1) <> 9 then
    raise exception 'older version overwrote a newer one';
  end if;
end $$;

-- A new capture epoch (re-snapshot) replaces rows even at a lower seq.
set role service_role;
select pg_temp.upsert_trade('arbgrid:svc:db1:trades:1', 2, 'e2', 'filled', 'live', 'k1');
select pg_temp.upsert_trade('arbgrid:svc:db1:trades:2', 2, 'e2', 'filled', 'unknown', null, true);  -- tombstone
reset role;
do $$ begin
  if (select capture_epoch from public.ledger_trades where source_id = 1) <> 'e2' then
    raise exception 'new epoch did not replace row';
  end if;
end $$;

-- Positions and sync status for PnL/coverage views.
set role service_role;
insert into public.ledger_positions (ledger_key, source_system, service, db_instance_id, source_table,
  source_id, source_version, capture_epoch, venue, account_ref, run_mode, mode_evidence, status,
  settled_at, realized_pnl)
values
  ('arbgrid:svc:db1:positions:1', 'arbgrid', 'svc', 'db1', 'positions', 1, 3, 'e2', 'kalshi', 'k1',
   'live', 'writer_stamped', 'settled', '2026-09-28T12:00:00+00:00', 1.25),
  ('arbgrid:svc:db1:positions:2', 'arbgrid', 'svc', 'db1', 'positions', 2, 3, 'e2', 'kalshi', 'k1',
   'live', 'writer_stamped', 'settled', '2026-09-28T13:00:00+00:00', -0.25),
  ('arbgrid:svc:db1:positions:3', 'arbgrid', 'svc', 'db1', 'positions', 3, 3, 'e2', 'kalshi', 'k1',
   'live', 'writer_stamped', 'settled', '2026-09-27T13:00:00+00:00', null),
  ('arbgrid:svc:db1:positions:4', 'arbgrid', 'svc', 'db1', 'positions', 4, 3, 'e2', 'kalshi', null,
   'unknown', 'none', 'settled', '2026-09-28T14:00:00+00:00', 9.0),
  ('arbgrid:svc:db1:positions:5', 'arbgrid', 'svc', 'db1', 'positions', 5, 3, 'e2', 'kalshi', 'k1',
   'paper', 'writer_stamped', 'settled', '2026-09-28T14:00:00+00:00', 50.0);
insert into public.ledger_sync_status (source_key, source_system, service, db_instance_id, capture_epoch,
  snapshot_complete, pending_changes, local_trades_count, local_positions_count,
  last_attempt_at, last_success_at, last_error)
values ('arbgrid:svc:db1', 'arbgrid', 'svc', 'db1', 'e2', true, 0, 1, 5, now(), now(), null);

-- Reconciliation rows as the collector writes them (Detroit days).
create or replace function pg_temp.recon(
  rid text, st text, d date, reasons jsonb default '[]', scope text default 'all_subaccounts',
  mirror_ok boolean default true, cov boolean default true, acct text default 'k1',
  at timestamptz default clock_timestamp(), read_at timestamptz default null,
  order_ids jsonb default '[]',
  sources jsonb default '[{"service": "svc", "source_key": "arbgrid:svc:db1"}]',
  max_age numeric default 3600,
  -- One matched gate each; the defaults satisfy every gate.
  counts int[] default '{1,1,1}', miss_ledger jsonb default '[]', miss_venue jsonb default '[]',
  qty_mm jsonb default '[]', ambiguous jsonb default '[]')
returns void language sql as $$
  insert into public.ledger_venue_reconciliations (run_id, venue, account_ref, interval_start, interval_end,
    reporting_tz, reporting_day, venue_source, coverage_scope, status, coverage_verified,
    ledger_mirror_verified, incomplete_reasons, venue_order_count, ledger_order_count, matched_order_count,
    missing_in_ledger, missing_in_venue, qty_mismatch, boundary_ambiguous_orders,
    collector, collector_version, collected_at, checked_at, ledger_read_started_at, ledger_order_ids,
    ledger_sources, max_source_age_seconds)
  values (rid, 'kalshi', acct, d::timestamp at time zone 'America/Detroit',
    (d + 1)::timestamp at time zone 'America/Detroit', 'America/Detroit', d,
    'kalshi:fills:sha256:0123456789abcdef:' || scope, scope, st, cov, mirror_ok, reasons,
    counts[1], counts[2], counts[3], miss_ledger, miss_venue, qty_mm, ambiguous,
    'kalshi_fills', '1', at, at, coalesce(read_at, at), order_ids, sources, max_age);
$$;

-- Runs stmt and requires it to fail on exactly the named check constraint,
-- so each case proves the one gate it changes.
create or replace function pg_temp.expect_check(label text, stmt text,
  want text default 'ledger_recon_matched_is_verified')
returns void language plpgsql as $$
declare got text;
begin
  begin
    execute stmt;
  exception when check_violation then
    get stacked diagnostics got = constraint_name;
    if got is distinct from want then
      raise exception '% failed on % instead of %', label, got, want;
    end if;
    return;
  end;
  raise exception '% was accepted', label;
end $$;
select pg_temp.recon('run-1', 'matched', '2026-09-28');
reset role;

-- Reconciliation rows cannot claim a match from incomplete evidence. The
-- baseline passes every gate; each case changes one gate and must fail on
-- the matched constraint itself.
select pg_temp.recon('iso-baseline', 'matched', '2026-08-01');
delete from public.ledger_venue_reconciliations where run_id = 'iso-baseline';
select pg_temp.expect_check('bad-1 unverified venue coverage',
  $q$select pg_temp.recon('bad-1', 'matched', '2026-09-27', cov => false)$q$);
select pg_temp.expect_check('bad-2 unverified ledger mirror',
  $q$select pg_temp.recon('bad-2', 'matched', '2026-09-27', mirror_ok => false)$q$);
select pg_temp.expect_check('bad-3 no account',
  $q$select pg_temp.recon('bad-3', 'matched', '2026-09-27', acct => null)$q$);
select pg_temp.expect_check('bad-5 missing_in_venue',
  $q$select pg_temp.recon('bad-5', 'matched', '2026-09-27', miss_venue => '["B"]')$q$);
select pg_temp.expect_check('bad-5b missing_in_ledger',
  $q$select pg_temp.recon('bad-5b', 'matched', '2026-09-27', miss_ledger => '["A"]')$q$);
select pg_temp.expect_check('bad-5c qty_mismatch',
  $q$select pg_temp.recon('bad-5c', 'matched', '2026-09-27',
                          qty_mm => '[{"order_id": "A", "venue": "2", "ledger": "1"}]')$q$);
select pg_temp.expect_check('bad-5d boundary_ambiguous_orders',
  $q$select pg_temp.recon('bad-5d', 'matched', '2026-09-27', ambiguous => '["A"]')$q$);
select pg_temp.expect_check('bad-5e matched_order_count <> venue_order_count',
  $q$select pg_temp.recon('bad-5e', 'matched', '2026-09-27', counts => '{2,1,1}')$q$);
select pg_temp.expect_check('bad-5f ledger_order_count > matched_order_count',
  $q$select pg_temp.recon('bad-5f', 'matched', '2026-09-27', counts => '{1,2,1}')$q$);
select pg_temp.expect_check('bad-11 no ledger sources',
  $q$select pg_temp.recon('bad-11', 'matched', '2026-09-27', sources => '[]')$q$);
do $$
begin
  begin
    perform pg_temp.recon('bad-4', 'matched', '2026-09-27', scope => 'whole-account');
    raise exception 'an unknown coverage scope was accepted';
  exception when check_violation then null;
  end;
  begin
    perform pg_temp.recon('bad-6', 'incomplete', '2026-09-27');
    raise exception 'incomplete without a reason was accepted';
  exception when check_violation then null;
  end;
  begin
    insert into public.ledger_venue_reconciliations (run_id, venue, account_ref, interval_start, interval_end,
      venue_source, status, incomplete_reasons, venue_order_count, ledger_order_count, matched_order_count,
      venue_fees_usd)
    values ('bad-7', 'kalshi', 'k1', '2026-09-27T04:00:00+00', '2026-09-28T04:00:00+00', 's', 'incomplete',
      '["venue_record_qty_unknown"]', 0, 0, 0, 'NaN');
    raise exception 'non-finite fees were accepted';
  exception when check_violation then null;
  end;
  begin
    -- UTC midnight bounds are not the Detroit day.
    insert into public.ledger_venue_reconciliations (run_id, venue, account_ref, interval_start, interval_end,
      reporting_tz, reporting_day, venue_source, status, incomplete_reasons, venue_order_count,
      ledger_order_count, matched_order_count)
    values ('bad-8', 'kalshi', 'k1', '2026-09-27T00:00:00+00', '2026-09-28T00:00:00+00', 'America/Detroit',
      '2026-09-27', 's', 'incomplete', '["x"]', 0, 0, 0);
    raise exception 'a reporting day with the wrong UTC bounds was accepted';
  exception when check_violation then null;
  end;
  begin
    -- Correct end, start one hour off (a DST-naive start).
    insert into public.ledger_venue_reconciliations (run_id, venue, account_ref, interval_start, interval_end,
      reporting_tz, reporting_day, venue_source, status, incomplete_reasons, venue_order_count,
      ledger_order_count, matched_order_count)
    values ('bad-9', 'kalshi', 'k1', '2026-03-08T04:00:00+00', '2026-03-09T04:00:00+00', 'America/Detroit',
      '2026-03-08', 's', 'incomplete', '["x"]', 0, 0, 0);
    raise exception 'a reporting day with a DST-naive start was accepted';
  exception when check_violation then null;
  end;
  begin
    insert into public.ledger_venue_reconciliations (run_id, venue, account_ref, interval_start, interval_end,
      venue_source, status, coverage_verified, ledger_mirror_verified, coverage_scope, venue_order_count,
      ledger_order_count, matched_order_count, ledger_sources, max_source_age_seconds)
    values ('bad-10', 'kalshi', 'k1', '2026-09-27T04:00:00+00', '2026-09-28T04:00:00+00', 's', 'matched', true,
      true, 'all_subaccounts', 0, 0, 0, '[{"service": "svc", "source_key": "arbgrid:svc:db1"}]', 3600);
    raise exception 'matched without a mirror read time was accepted';
  exception when check_violation then null;
  end;
  begin
    perform pg_temp.recon('bad-12', 'matched', '2026-09-27', max_age => 0);
    raise exception 'a non-positive source age was accepted';
  exception when check_violation then null;
  end;
  begin
    perform pg_temp.recon('bad-12b', 'matched', '2026-09-27', max_age => 'NaN');
    raise exception 'a NaN source age was accepted';
  exception when check_violation then null;
  end;
  begin
    perform pg_temp.recon('bad-12c', 'matched', '2026-09-27', max_age => 'Infinity');
    raise exception 'an infinite source age was accepted';
  exception when check_violation then null;
  end;
  begin
    perform pg_temp.recon('bad-13', 'matched', '2026-09-27', order_ids => '{"o": 1}');
    raise exception 'non-array order ids were accepted';
  exception when check_violation then null;
  end;
  begin
    perform pg_temp.recon('run-1', 'incomplete', '2026-09-27', '["x"]');
    raise exception 'a duplicate run_id was accepted';
  exception when unique_violation then null;
  end;
end $$;
-- DST days: 23 h in March, 25 h in November, both accepted with exact bounds.
select pg_temp.recon('dst-spring', 'incomplete', '2026-03-08', '["venue_interval_not_final"]');
select pg_temp.recon('dst-fall', 'incomplete', '2026-11-01', '["venue_interval_not_final"]');
do $$ begin
  if (select interval_end - interval_start from public.ledger_venue_reconciliations
      where run_id = 'dst-spring') <> interval '23 hours'
     or (select interval_end - interval_start from public.ledger_venue_reconciliations
         where run_id = 'dst-fall') <> interval '25 hours' then
    raise exception 'DST reporting days have the wrong length';
  end if;
end $$;
-- An incomplete check with no account is recordable (and matches nothing).
insert into public.ledger_venue_reconciliations (run_id, venue, account_ref, interval_start, interval_end,
  venue_source, status, incomplete_reasons, venue_order_count, ledger_order_count, matched_order_count)
values ('no-acct', 'kalshi', null, null, null, null, 'incomplete', '["account_unknown", "invalid_interval"]',
  0, 0, 0);

-- Reporter: views only, read only.
set role ledger_reporter;
do $$
declare r record;
begin
  if (select count(*) from ledger_reporting.trades) <> 1 then
    raise exception 'trades view must hide tombstones';
  end if;
  if not (select mirror_complete from ledger_reporting.sources where service = 'svc') then
    raise exception 'complete mirror not reported complete';
  end if;
  select * into r from ledger_reporting.realized_pnl_daily where settle_day_local = '2026-09-28';
  if r.realized_pnl_sum <> 1.00 or r.settled_positions <> 2 or r.pnl_verified
     or not r.fills_reconciled_for_day or not r.account_wide_fills_reconciled
     or r.pnl_basis <> 'engine_computed_unverified' or r.reporting_tz <> 'America/Detroit' then
    raise exception 'daily pnl row wrong: %', r;
  end if;
  select * into r from ledger_reporting.realized_pnl_daily where settle_day_local = '2026-09-27';
  if r.realized_pnl_sum is not null or r.positions_missing_pnl <> 1 or r.fills_reconciled_for_day then
    raise exception 'missing pnl must not sum to a number: %', r;
  end if;
  if exists (select 1 from ledger_reporting.realized_pnl_daily where settle_day_local = '2026-09-26') then
    raise exception 'a day without settlements must have no row';
  end if;
  if (select sum(settled_positions) from ledger_reporting.unattributed_settlements_daily) <> 2 then
    raise exception 'unknown-mode, unknown-account and paper settlements must be counted separately';
  end if;
  select * into r from ledger_reporting.venue_reconciliation_days where reporting_day = '2026-09-28';
  if not r.fills_verified or r.pnl_verified or not r.account_wide or r.run_id <> 'run-1' then
    raise exception 'reconciliation day row wrong: %', r;
  end if;
  if exists (select 1 from ledger_reporting.venue_reconciliation_days where reporting_day = '2026-09-25') then
    raise exception 'a day without a check must have no row (unknown, not zero)';
  end if;
end $$;

do $$ begin
  begin
    perform 1 from public.ledger_trades;
    raise exception 'reporter read a base table';
  exception when insufficient_privilege then null;
  end;
  begin
    perform 1 from public.ledger_sync_status;
    raise exception 'reporter read sync status base table';
  exception when insufficient_privilege then null;
  end;
  begin
    insert into public.ledger_venue_reconciliations (venue, account_ref, interval_start, interval_end,
      venue_source, status, venue_order_count, ledger_order_count, matched_order_count)
    values ('kalshi', 'k1', now(), now() + interval '1 day', 'forged', 'matched', 0, 0, 0);
    raise exception 'reporter wrote a reconciliation';
  exception when insufficient_privilege then null;
  end;
  begin
    delete from ledger_reporting.trades;
    raise exception 'reporter deleted through a view';
  exception when insufficient_privilege then null;
  end;
  begin
    perform public.ledger_version_guard();
    raise exception 'reporter could call the guard function';
  exception when insufficient_privilege or feature_not_supported or wrong_object_type
    or undefined_function then null;
  end;
end $$;
reset role;

-- Client API roles see nothing.
do $$
declare role_name text;
begin
  foreach role_name in array array['anon', 'authenticated'] loop
    execute format('set local role %I', role_name);
    begin
      perform 1 from ledger_reporting.trades;
      raise exception '% read the reporting schema', role_name;
    exception when insufficient_privilege then null;
    end;
    begin
      perform 1 from public.ledger_positions;
      raise exception '% read a ledger base table', role_name;
    exception when insufficient_privilege then null;
    end;
    reset role;
  end loop;
end $$;

-- An incomplete mirror is reported incomplete.
update public.ledger_sync_status set pending_changes = 3;
do $$ begin
  if (select mirror_complete from ledger_reporting.sources) then
    raise exception 'pending changes must make the mirror incomplete';
  end if;
end $$;
update public.ledger_sync_status set pending_changes = 0, last_attempt_at = now(), last_error = 'RuntimeError: x';
do $$ begin
  if (select mirror_complete from ledger_reporting.sources) then
    raise exception 'a failed latest attempt must make the mirror incomplete';
  end if;
end $$;
update public.ledger_sync_status set last_error = null, last_success_at = last_attempt_at, local_positions_count = 6;
do $$ begin
  if (select mirror_complete from ledger_reporting.sources) then
    raise exception 'a row-count mismatch must make the mirror incomplete';
  end if;
end $$;


-- A later incomplete check for the day replaces the match as the day's status.
do $$ begin
  if not (select fills_reconciled_for_day from ledger_reporting.realized_pnl_daily
          where settle_day_local = '2026-09-28') then
    raise exception 'setup: day should start reconciled';
  end if;
end $$;
select pg_temp.recon('run-2', 'incomplete', '2026-09-28', '["venue_cursor_repeated"]');
do $$ begin
  if (select fills_reconciled_for_day from ledger_reporting.realized_pnl_daily
      where settle_day_local = '2026-09-28') then
    raise exception 'a later incomplete check must withdraw the reconciled flag';
  end if;
  if (select count(*) from ledger_reporting.venue_reconciliations where status = 'incomplete') <> 4 then
    raise exception 'incomplete checks must be visible to reporters';
  end if;
end $$;

-- A fresh matched check restores it; a later ledger correction makes it stale.
select pg_temp.recon('run-3', 'matched', '2026-09-28');
do $$ begin
  if not (select fills_verified from ledger_reporting.venue_reconciliation_days
          where reporting_day = '2026-09-28') then
    raise exception 'a newer matched check must verify the day';
  end if;
end $$;
set role service_role;
select pg_temp.upsert_trade('arbgrid:svc:db1:trades:1', 3, 'e2', 'filled', 'live', 'k1');  -- correction
reset role;
do $$ begin
  if (select fills_verified or not ledger_changed_since_check from ledger_reporting.venue_reconciliation_days
      where reporting_day = '2026-09-28') then
    raise exception 'a ledger change after the check must make it stale';
  end if;
  if (select fills_reconciled_for_day from ledger_reporting.realized_pnl_daily
      where settle_day_local = '2026-09-28') then
    raise exception 'a stale check must not reconcile the day';
  end if;
end $$;

-- A subaccount-restricted check never counts as account-wide.
select pg_temp.recon('run-4', 'matched', '2026-09-28', scope => 'subaccount:0');
do $$ begin
  if not (select fills_reconciled_for_day and not account_wide_fills_reconciled
          from ledger_reporting.realized_pnl_daily where settle_day_local = '2026-09-28') then
    raise exception 'a subaccount check must reconcile only its own scope';
  end if;
end $$;

-- ---------------------------------------------------------------------------
-- Review fixes: read fence, targeted order ids, tombstones, source recency.
-- Day 2026-09-15 (k1); no other fixture row is in its read window.
-- ---------------------------------------------------------------------------
create temp table t_marks (name text primary key, at timestamptz);
grant all on t_marks to service_role;

-- What the exporter reports after a sync: the source's local live trade
-- count, which equals the mirror's while nothing is lost in between.
create or replace function pg_temp.report_local_trades()
returns void language sql as $$
  update public.ledger_sync_status s set local_trades_count = (
    select count(*) from public.ledger_trades t
    where t.service = s.service and t.db_instance_id = s.db_instance_id and not t.deleted)
  where s.source_key = 'arbgrid:svc:db1';
$$;

-- PostgREST-style tombstone that sends no provenance (explicit nulls): the
-- stored row keeps its venue, order id and time.
create or replace function pg_temp.upsert_bare_tombstone(k text, ver bigint)
returns void language sql as $$
  insert into public.ledger_trades (ledger_key, source_system, service, db_instance_id, source_table,
    source_id, source_version, capture_epoch, deleted)
  values (k, 'arbgrid', 'svc', 'db1', 'trades', split_part(k, ':', 5)::bigint, ver, 'e2', true)
  on conflict (ledger_key) do update set
    source_version = excluded.source_version, deleted = excluded.deleted, venue = excluded.venue,
    account_ref = excluded.account_ref, order_id = excluded.order_id, recorded_at = excluded.recorded_at;
$$;

set role service_role;
insert into public.ledger_trades (ledger_key, source_system, service, db_instance_id, source_table, source_id,
  source_version, capture_epoch, venue, account_ref, run_mode, mode_evidence, recorded_at, status, fill_qty,
  order_id)
values
  ('arbgrid:svc:db1:trades:20', 'arbgrid', 'svc', 'db1', 'trades', 20, 10, 'e2', 'kalshi', 'k1', 'live',
   'writer_stamped', '2026-09-15T12:00:00+00', 'filled', 1, 'ord-in-window'),
  ('arbgrid:svc:db1:trades:21', 'arbgrid', 'svc', 'db1', 'trades', 21, 10, 'e2', 'kalshi', 'k1', 'live',
   'writer_stamped', '2026-09-01T12:00:00+00', 'filled', 2, 'ord-old-targeted'),
  ('arbgrid:svc:db1:trades:22', 'arbgrid', 'svc', 'db1', 'trades', 22, 10, 'e2', 'kalshi', 'k1', 'live',
   'writer_stamped', '2026-09-01T12:00:00+00', 'filled', 3, 'ord-old-unrelated');
reset role;
select pg_temp.report_local_trades();
select pg_sleep(0.01);

-- 1. A replay of the same version is not a change.
insert into t_marks values ('before_replay', (select synced_at from public.ledger_trades
                                               where source_id = 20));
set role service_role;
insert into public.ledger_trades (ledger_key, source_system, service, db_instance_id, source_table, source_id,
  source_version, capture_epoch, venue, account_ref, run_mode, mode_evidence, recorded_at, status, fill_qty,
  order_id)
values ('arbgrid:svc:db1:trades:20', 'arbgrid', 'svc', 'db1', 'trades', 20, 10, 'e2', 'kalshi', 'k1', 'live',
   'writer_stamped', '2026-09-15T12:00:00+00', 'filled', 1, 'ord-in-window')
on conflict (ledger_key) do update set source_version = excluded.source_version, fill_qty = excluded.fill_qty;
reset role;
do $$ begin
  if (select synced_at from public.ledger_trades where source_id = 20)
     <> (select at from t_marks where name = 'before_replay') then
    raise exception 'a same-version replay must not move synced_at';
  end if;
end $$;

-- 2. A check whose mirror read started before a correction that landed
--    before the check row was persisted is stale from the start.
insert into t_marks values ('read_1', clock_timestamp());
select pg_sleep(0.01);
set role service_role;
update public.ledger_trades set fill_qty = 1.5, source_version = 11 where source_id = 20;
reset role;
select pg_sleep(0.01);
select pg_temp.recon('fence-1', 'matched', '2026-09-15', read_at => (select at from t_marks where name = 'read_1'),
                     order_ids => '["ord-in-window", "ord-old-targeted"]');
do $$ begin
  if (select fills_verified or fills_matched_as_of is not null or not ledger_changed_since_check
      from ledger_reporting.venue_reconciliation_days where reporting_day = '2026-09-15') then
    raise exception 'a change between the mirror read and persistence must make the check stale';
  end if;
end $$;

-- A clean re-check verifies the day, as of its read time.
insert into t_marks values ('read_2', clock_timestamp());
select pg_temp.recon('fence-2', 'matched', '2026-09-15', read_at => (select at from t_marks where name = 'read_2'),
                     order_ids => '["ord-in-window", "ord-old-targeted"]');
do $$ declare r record; begin
  select * into r from ledger_reporting.venue_reconciliation_days where reporting_day = '2026-09-15';
  if not r.fills_verified or not r.mirror_current
     or r.fills_matched_as_of <> (select at from t_marks where name = 'read_2') then
    raise exception 'a clean re-check must verify the day as of its read: %', r;
  end if;
end $$;

-- 3. An out-of-window row the check did not use does not withdraw it...
set role service_role;
update public.ledger_trades set fill_qty = 4, source_version = 12 where source_id = 22;
reset role;
do $$ begin
  if not (select fills_verified from ledger_reporting.venue_reconciliation_days
          where reporting_day = '2026-09-15') then
    raise exception 'an unrelated old order must not withdraw the check';
  end if;
end $$;
-- ...but a correction to an out-of-window order the check compared does.
set role service_role;
update public.ledger_trades set fill_qty = 2.5, source_version = 12 where source_id = 21;
reset role;
do $$ begin
  if (select fills_verified or not ledger_changed_since_check from ledger_reporting.venue_reconciliation_days
      where reporting_day = '2026-09-15') then
    raise exception 'a correction to a compared out-of-window order must make the check stale';
  end if;
end $$;

-- 3b. The next day's trading on other orders does not withdraw the day.
insert into t_marks values ('read_2b', clock_timestamp());
select pg_temp.recon('fence-2b', 'matched', '2026-09-15', read_at => (select at from t_marks where name = 'read_2b'),
                     order_ids => '["ord-in-window", "ord-old-targeted"]');
set role service_role;
insert into public.ledger_trades (ledger_key, source_system, service, db_instance_id, source_table, source_id,
  source_version, capture_epoch, venue, account_ref, run_mode, mode_evidence, recorded_at, status, fill_qty,
  order_id)
values ('arbgrid:svc:db1:trades:23', 'arbgrid', 'svc', 'db1', 'trades', 23, 20, 'e2', 'kalshi', 'k1', 'live',
   'writer_stamped', '2026-09-16T06:00:00+00', 'filled', 1, 'ord-next-day');
reset role;
select pg_temp.report_local_trades();
do $$ begin
  if not (select fills_verified from ledger_reporting.venue_reconciliation_days
          where reporting_day = '2026-09-15') then
    raise exception 'the next day''s trading on other orders must not withdraw the check';
  end if;
end $$;

-- 4. A bare tombstone keeps provenance and withdraws a check that used the row.
insert into t_marks values ('read_3', clock_timestamp());
select pg_temp.recon('fence-3', 'matched', '2026-09-15', read_at => (select at from t_marks where name = 'read_3'),
                     order_ids => '["ord-in-window", "ord-old-targeted"]');
set role service_role;
select pg_temp.upsert_bare_tombstone('arbgrid:svc:db1:trades:21', 13);
reset role;
select pg_temp.report_local_trades();
do $$ declare r record; begin
  select venue, order_id, account_ref, recorded_at, deleted into r
  from public.ledger_trades where source_id = 21;
  if not r.deleted or r.venue is distinct from 'kalshi' or r.order_id is distinct from 'ord-old-targeted'
     or r.account_ref is distinct from 'k1' or r.recorded_at is null then
    raise exception 'a tombstone must keep the row provenance: %', r;
  end if;
  if (select fills_verified from ledger_reporting.venue_reconciliation_days where reporting_day = '2026-09-15') then
    raise exception 'deleting a compared row must make the check stale';
  end if;
end $$;
-- A retried delete (same version, no provenance sent) leaves the stored
-- tombstone exactly as it was.
insert into t_marks select 'tombstone_synced', synced_at from public.ledger_trades where source_id = 21;
select pg_sleep(0.01);
set role service_role;
select pg_temp.upsert_bare_tombstone('arbgrid:svc:db1:trades:21', 13);
reset role;
do $$ declare r record; begin
  select venue, order_id, account_ref, recorded_at, deleted, synced_at into r
  from public.ledger_trades where source_id = 21;
  if not r.deleted or r.venue is distinct from 'kalshi' or r.order_id is distinct from 'ord-old-targeted'
     or r.account_ref is distinct from 'k1' or r.recorded_at is null
     or r.synced_at <> (select at from t_marks where name = 'tombstone_synced') then
    raise exception 'a replayed tombstone must keep provenance and synced_at: %', r;
  end if;
end $$;

-- 5. A source that stopped syncing leaves only an as-of claim.
insert into t_marks values ('read_4', clock_timestamp());
select pg_temp.recon('fence-4', 'matched', '2026-09-15', read_at => (select at from t_marks where name = 'read_4'),
                     order_ids => '["ord-in-window", "ord-old-targeted"]');
update public.ledger_sync_status set last_attempt_at = now() - interval '2 hours',
  last_success_at = now() - interval '2 hours';
do $$ declare r record; begin
  select * into r from ledger_reporting.venue_reconciliation_days where reporting_day = '2026-09-15';
  if r.fills_verified or r.mirror_current or r.fills_matched_as_of is null then
    raise exception 'a silent source must leave only an as-of claim: %', r;
  end if;
end $$;
update public.ledger_sync_status set last_attempt_at = now(), last_success_at = now();
do $$ begin
  if not (select fills_verified from ledger_reporting.venue_reconciliation_days
          where reporting_day = '2026-09-15') then
    raise exception 'a recovered source must restore the day';
  end if;
end $$;
-- A newer instance of the service (redeploy without a volume) is not current.
insert into public.ledger_sync_status (source_key, source_system, service, db_instance_id, capture_epoch,
  snapshot_complete, pending_changes, local_trades_count, local_positions_count,
  last_attempt_at, last_success_at, last_error)
values ('arbgrid:svc:db9', 'arbgrid', 'svc', 'db9', 'e9', true, 0, 0, 0,
  now() + interval '1 second', now() + interval '1 second', null);
do $$ begin
  if (select fills_verified or mirror_current from ledger_reporting.venue_reconciliation_days
      where reporting_day = '2026-09-15') then
    raise exception 'a replaced source instance must not stay current';
  end if;
end $$;
delete from public.ledger_sync_status where source_key = 'arbgrid:svc:db9';

-- 6. A tombstone for a row never exported with provenance (insert then
--    delete between syncs) is unattributable, so it withdraws the check.
set role service_role;
select pg_temp.upsert_bare_tombstone('arbgrid:svc:db1:trades:30', 14);
reset role;
do $$ begin
  if (select fills_verified from ledger_reporting.venue_reconciliation_days where reporting_day = '2026-09-15') then
    raise exception 'an unattributable tombstone must make the check stale';
  end if;
end $$;

-- ---------------------------------------------------------------------------
-- mirror_current needs the mirror's live trade count to equal the count the
-- source reported: a hard delete in the mirror, or a source row that never
-- arrived, is not a current mirror even though no synced_at moved.
-- ---------------------------------------------------------------------------
select pg_temp.report_local_trades();
insert into t_marks values ('read_5', clock_timestamp());
select pg_temp.recon('count-1', 'matched', '2026-09-15', read_at => (select at from t_marks where name = 'read_5'),
                     order_ids => '["ord-in-window"]');
do $$ begin
  if not (select fills_verified and mirror_current from ledger_reporting.venue_reconciliation_days
          where reporting_day = '2026-09-15') then
    raise exception 'setup: a clean check with equal counts must verify the day';
  end if;
end $$;
-- Hard-delete a compared trade row outside the exporter (no tombstone).
delete from public.ledger_trades where source_id = 20;
do $$ declare r record; begin
  select * into r from ledger_reporting.venue_reconciliation_days where reporting_day = '2026-09-15';
  if r.fills_verified or r.mirror_current or r.ledger_changed_since_check then
    raise exception 'a hard-deleted mirror row must end mirror_current without a synced_at change: %', r;
  end if;
  if (select mirror_complete from ledger_reporting.sources where source_key = 'arbgrid:svc:db1') then
    raise exception 'a hard-deleted mirror row must make the source incomplete';
  end if;
end $$;
-- The source reporting a row the mirror never received is the same gap.
select pg_temp.report_local_trades();
do $$ begin
  if not (select mirror_current from ledger_reporting.venue_reconciliation_days
          where reporting_day = '2026-09-15') then
    raise exception 'equal counts again must restore mirror_current';
  end if;
end $$;
update public.ledger_sync_status set local_trades_count = local_trades_count + 1
where source_key = 'arbgrid:svc:db1';
do $$ begin
  if (select fills_verified or mirror_current from ledger_reporting.venue_reconciliation_days
      where reporting_day = '2026-09-15') then
    raise exception 'a source row missing from the mirror must end mirror_current';
  end if;
end $$;
update public.ledger_sync_status set local_trades_count = null where source_key = 'arbgrid:svc:db1';
do $$ begin
  if (select mirror_current from ledger_reporting.venue_reconciliation_days where reporting_day = '2026-09-15') then
    raise exception 'an unreported local count must not count as current';
  end if;
end $$;
select pg_temp.report_local_trades();

-- A row recorded just past the day's end is in the recording window, so its
-- later change withdraws the check; one recorded well after does not.
insert into t_marks values ('read_6', clock_timestamp());
select pg_temp.recon('window-1', 'matched', '2026-09-15', read_at => (select at from t_marks where name = 'read_6'),
                     order_ids => '["ord-in-window"]');
set role service_role;
insert into public.ledger_trades (ledger_key, source_system, service, db_instance_id, source_table, source_id,
  source_version, capture_epoch, venue, account_ref, run_mode, mode_evidence, recorded_at, status, fill_qty,
  order_id)
values ('arbgrid:svc:db1:trades:24', 'arbgrid', 'svc', 'db1', 'trades', 24, 30, 'e2', 'kalshi', 'k1', 'live',
   'writer_stamped', '2026-09-16T04:10:00+00', 'filled', 1, 'ord-well-after');
reset role;
select pg_temp.report_local_trades();
do $$ begin
  if not (select fills_verified from ledger_reporting.venue_reconciliation_days
          where reporting_day = '2026-09-15') then
    raise exception 'a row recorded ten minutes after the day must not withdraw it';
  end if;
end $$;
set role service_role;
insert into public.ledger_trades (ledger_key, source_system, service, db_instance_id, source_table, source_id,
  source_version, capture_epoch, venue, account_ref, run_mode, mode_evidence, recorded_at, status, fill_qty,
  order_id)
values ('arbgrid:svc:db1:trades:25', 'arbgrid', 'svc', 'db1', 'trades', 25, 31, 'e2', 'kalshi', 'k1', 'live',
   'writer_stamped', '2026-09-16T04:02:00+00', 'filled', 1, 'ord-just-after');
reset role;
select pg_temp.report_local_trades();
do $$ begin
  if (select fills_verified or not ledger_changed_since_check from ledger_reporting.venue_reconciliation_days
      where reporting_day = '2026-09-15') then
    raise exception 'a row recorded inside the recording window after the day must withdraw it';
  end if;
end $$;

select 'ledger reporting assertions passed' as result;
