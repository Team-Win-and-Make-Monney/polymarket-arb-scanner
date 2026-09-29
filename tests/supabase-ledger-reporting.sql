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
values ('arbgrid:svc:db1', 'arbgrid', 'svc', 'db1', 'e2', true, 0, 1, 5,
  '2026-09-29T00:00:00+00:00', '2026-09-29T00:00:00+00:00', null);

-- Reconciliation rows as the collector writes them (Detroit days).
create or replace function pg_temp.recon(
  rid text, st text, d date, reasons jsonb default '[]', scope text default 'all_subaccounts',
  mirror_ok boolean default true, cov boolean default true, acct text default 'k1',
  at timestamptz default clock_timestamp())
returns void language sql as $$
  insert into public.ledger_venue_reconciliations (run_id, venue, account_ref, interval_start, interval_end,
    reporting_tz, reporting_day, venue_source, coverage_scope, status, coverage_verified,
    ledger_mirror_verified, incomplete_reasons, venue_order_count, ledger_order_count, matched_order_count,
    collector, collector_version, collected_at, checked_at)
  values (rid, 'kalshi', acct, d::timestamp at time zone 'America/Detroit',
    (d + 1)::timestamp at time zone 'America/Detroit', 'America/Detroit', d,
    'kalshi:fills:sha256:0123456789abcdef:' || scope, scope, st, cov, mirror_ok, reasons, 1, 1, 1,
    'kalshi_fills', '1', at, at);
$$;
select pg_temp.recon('run-1', 'matched', '2026-09-28');
reset role;

-- Reconciliation rows cannot claim a match from incomplete evidence.
do $$
begin
  begin
    perform pg_temp.recon('bad-1', 'matched', '2026-09-27', cov => false);
    raise exception 'matched without verified venue coverage was accepted';
  exception when check_violation then null;
  end;
  begin
    perform pg_temp.recon('bad-2', 'matched', '2026-09-27', mirror_ok => false);
    raise exception 'matched without a verified ledger mirror was accepted';
  exception when check_violation then null;
  end;
  begin
    perform pg_temp.recon('bad-3', 'matched', '2026-09-27', acct => null);
    raise exception 'matched without an account was accepted';
  exception when check_violation then null;
  end;
  begin
    perform pg_temp.recon('bad-4', 'matched', '2026-09-27', scope => 'whole-account');
    raise exception 'an unknown coverage scope was accepted';
  exception when check_violation then null;
  end;
  begin
    insert into public.ledger_venue_reconciliations (run_id, venue, account_ref, interval_start, interval_end,
      venue_source, status, coverage_verified, ledger_mirror_verified, coverage_scope, venue_order_count,
      ledger_order_count, matched_order_count, missing_in_venue)
    values ('bad-5', 'kalshi', 'k1', '2026-09-27T04:00:00+00', '2026-09-28T04:00:00+00', 's', 'matched', true,
      true, 'all_subaccounts', 0, 1, 0, '["B"]');
    raise exception 'matched with a missing order was accepted';
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

select 'ledger reporting assertions passed' as result;
