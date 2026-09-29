-- DRAFT, NOT APPLIED. Kept outside supabase/migrations so no replay or
-- integration applies it. Rollout steps: docs/LEDGER-REPORTING.md.
--
-- Trade-ledger reporting mirror. trades.db (SQLite on each service's volume)
-- stays the source record; ledger_sync.LedgerExporter mirrors the current
-- state of trades/positions rows here. Reporting routines read only the
-- ledger_reporting views through the NOLOGIN ledger_reporter role: no base
-- table access, no writes, no API exposure, no service role.
--
-- What the mirror can and cannot say:
--   * run_mode is 'paper'/'live' only from the writer's stamp or a dry-run row
--     marker; everything else is 'unknown'. History is never labelled from
--     current configuration.
--   * Fees are not recorded by the local ledger (fee_status='not_recorded').
--   * realized_pnl is engine-computed at settlement and unverified.
--   * A complete mirror covers one service's local DB, not an account. Venue
--     completeness comes only from ledger_venue_reconciliations.
-- Idempotent: safe to re-run.

BEGIN;

-- ---------------------------------------------------------------------------
-- Base tables (written by the exporter's backend credential only)
-- ---------------------------------------------------------------------------

create table if not exists public.ledger_trades (
  ledger_key        text primary key,        -- system:service:db_instance:table:id
  source_system     text not null,
  service           text not null,
  db_instance_id    text not null,
  source_table      text not null check (source_table = 'trades'),
  source_id         bigint not null,
  source_version    bigint not null,          -- local outbox seq visible when read
  capture_epoch     text not null,
  deleted           boolean not null default false,
  pre_capture       boolean,                  -- row existed before change capture began
  source_changed_at timestamptz,              -- local outbox time of the latest change
  exported_at       timestamptz,
  synced_at         timestamptz not null default now(),
  venue             text,
  account_ref       text,                     -- operator label; null = unknown
  run_mode          text check (run_mode in ('paper', 'live', 'unknown')),
  mode_evidence     text check (mode_evidence in
                      ('writer_stamped', 'row_status', 'row_order_id', 'conflict', 'none')),
  opportunity_id    bigint,
  recorded_at       timestamptz,              -- engine log time, not venue fill time
  side              text,
  outcome           text,
  order_price       numeric,
  size              numeric,
  status            text,
  fill_price        numeric,
  fill_qty          numeric,
  slippage          numeric,
  order_id          text,
  client_order_id   text,
  fee_usd           numeric,
  fee_status        text,
  unique (source_system, service, db_instance_id, source_table, source_id)
);

create table if not exists public.ledger_positions (
  ledger_key        text primary key,
  source_system     text not null,
  service           text not null,
  db_instance_id    text not null,
  source_table      text not null check (source_table = 'positions'),
  source_id         bigint not null,
  source_version    bigint not null,
  capture_epoch     text not null,
  deleted           boolean not null default false,
  pre_capture       boolean,
  source_changed_at timestamptz,
  exported_at       timestamptz,
  synced_at         timestamptz not null default now(),
  venue             text,
  account_ref       text,
  run_mode          text check (run_mode in ('paper', 'live', 'unknown')),
  mode_evidence     text check (mode_evidence in
                      ('writer_stamped', 'row_status', 'row_order_id', 'conflict', 'none')),
  opportunity_id    bigint,
  market_identifier text,
  market_ticker     text,
  entry_at          timestamptz,
  settled_at        timestamptz,              -- engine settlement time
  status            text,
  expected_pnl      numeric,
  realized_pnl      numeric,                  -- engine-computed; see pnl_basis
  pnl_basis         text,
  fee_usd           numeric,
  fee_status        text,
  unique (source_system, service, db_instance_id, source_table, source_id)
);

create index if not exists ledger_trades_venue_recorded_idx
  on public.ledger_trades (venue, recorded_at);
create index if not exists ledger_positions_venue_settled_idx
  on public.ledger_positions (venue, settled_at);

create table if not exists public.ledger_sync_status (
  source_key                    text primary key,  -- system:service:db_instance
  source_system                 text not null,
  service                       text not null,
  db_instance_id                text,
  capture_epoch                 text,
  capture_since                 timestamptz,
  capture_boundary_trades_id    bigint,
  capture_boundary_positions_id bigint,
  snapshot_complete             boolean not null default false,
  watermark_seq                 bigint,
  pending_changes               bigint,
  local_trades_count            bigint,
  local_max_trades_id           bigint,
  local_positions_count         bigint,
  local_max_positions_id        bigint,
  last_attempt_at               timestamptz,
  last_success_at               timestamptz,
  last_error                    text,
  exporter_version              text,
  synced_at                     timestamptz not null default now()
);

-- One row per venue check, written by scripts/reconcile_venue_fills.py
-- (venue_reconciliation.run_reconciliation) with read-only venue access.
-- Missing or invalid evidence is recorded as 'incomplete' with reasons; a row
-- can only be 'matched' with verified venue coverage, a verified ledger
-- mirror and nothing outstanding. Re-runs add rows; the latest check for a
-- reporting day wins (see ledger_reporting.venue_reconciliation_days).
create table if not exists public.ledger_venue_reconciliations (
  id                  uuid primary key default gen_random_uuid(),
  run_id              text not null unique,    -- idempotency key for one collector run
  venue               text not null,
  account_ref         text,                    -- null only on an incomplete check
  interval_start      timestamptz,
  interval_end        timestamptz,
  venue_source        text,                    -- statement id / API export reference
  status              text not null check (status in ('matched', 'mismatched', 'incomplete')),
  coverage_verified   boolean not null default false,
  incomplete_reasons  jsonb not null default '[]'::jsonb,
  venue_order_count   integer not null,
  ledger_order_count  integer not null,
  matched_order_count integer not null,
  venue_records_out_of_interval integer not null default 0,
  missing_in_ledger   jsonb not null default '[]'::jsonb,
  missing_in_venue    jsonb not null default '[]'::jsonb,
  qty_mismatch        jsonb not null default '[]'::jsonb,
  venue_fees_usd      numeric check (venue_fees_usd is null
                        or venue_fees_usd not in ('NaN'::numeric, 'Infinity'::numeric, '-Infinity'::numeric)),
  fees_complete       boolean not null default false,
  coverage_scope      text check (coverage_scope is null
                        or coverage_scope ~ '^(all_subaccounts|subaccount:[0-9]{1,2})$'),
  reporting_tz        text,
  reporting_day       date,                    -- local calendar day the interval covers
  collector           text,
  collector_version   text,
  collected_at        timestamptz,
  ledger_mirror_verified boolean not null default false,
  evidence            jsonb not null default '{}'::jsonb,  -- pages, cutoffs, sources checked
  checked_at          timestamptz not null default now(),
  constraint ledger_recon_interval_valid
    check (interval_end is null or interval_start is null or interval_end > interval_start),
  constraint ledger_recon_incomplete_has_reason
    check ((status = 'incomplete') = (incomplete_reasons <> '[]'::jsonb)),
  constraint ledger_recon_day_matches_interval
    check (reporting_day is null or (
      reporting_tz is not null
      and interval_start = (reporting_day::timestamp at time zone reporting_tz)
      and interval_end = ((reporting_day + 1)::timestamp at time zone reporting_tz))),
  constraint ledger_recon_matched_is_verified
    check (status <> 'matched' or (
      coverage_verified and ledger_mirror_verified and coverage_scope is not null
      and account_ref is not null and venue_source is not null
      and interval_start is not null and interval_end is not null
      and incomplete_reasons = '[]'::jsonb
      and missing_in_ledger = '[]'::jsonb and missing_in_venue = '[]'::jsonb
      and qty_mismatch = '[]'::jsonb
      and matched_order_count = venue_order_count
      and ledger_order_count <= matched_order_count))
);

create index if not exists ledger_venue_recon_day_idx
  on public.ledger_venue_reconciliations (venue, account_ref, reporting_day, checked_at);

-- ---------------------------------------------------------------------------
-- Version guard: an older export never overwrites a newer one
-- ---------------------------------------------------------------------------

create or replace function public.ledger_version_guard()
returns trigger
language plpgsql
set search_path = ''
as $$
begin
  if tg_op = 'UPDATE'
     and new.capture_epoch = old.capture_epoch
     and new.source_version < old.source_version then
    return null;  -- keep the stored, newer row
  end if;
  new.synced_at := now();
  return new;
end;
$$;

create or replace function public.ledger_touch_synced_at()
returns trigger
language plpgsql
set search_path = ''
as $$
begin
  new.synced_at := now();
  return new;
end;
$$;

revoke all on function public.ledger_version_guard() from public, anon, authenticated;
revoke all on function public.ledger_touch_synced_at() from public, anon, authenticated;

drop trigger if exists ledger_trades_version_guard on public.ledger_trades;
create trigger ledger_trades_version_guard
  before insert or update on public.ledger_trades
  for each row execute function public.ledger_version_guard();

drop trigger if exists ledger_positions_version_guard on public.ledger_positions;
create trigger ledger_positions_version_guard
  before insert or update on public.ledger_positions
  for each row execute function public.ledger_version_guard();

drop trigger if exists ledger_sync_status_touch on public.ledger_sync_status;
create trigger ledger_sync_status_touch
  before insert or update on public.ledger_sync_status
  for each row execute function public.ledger_touch_synced_at();

-- Deny-by-default: RLS on, no policies, no client-role grants.
alter table public.ledger_trades enable row level security;
alter table public.ledger_positions enable row level security;
alter table public.ledger_sync_status enable row level security;
alter table public.ledger_venue_reconciliations enable row level security;
revoke all on public.ledger_trades, public.ledger_positions, public.ledger_sync_status,
  public.ledger_venue_reconciliations from public, anon, authenticated;

-- ---------------------------------------------------------------------------
-- Reporting schema: the only surface reporters can read
-- ---------------------------------------------------------------------------

create schema if not exists ledger_reporting;
revoke all on schema ledger_reporting from public;

do $$
begin
  if not exists (select from pg_roles where rolname = 'ledger_reporter') then
    create role ledger_reporter nologin;
  end if;
end $$;

-- Per-source mirror state. mirror_complete means: the latest attempt
-- succeeded, the snapshot finished and nothing captured is still pending,
-- and the mirror's live row counts equal the local counts reported. It says
-- nothing about the venue account; see venue_reconciliations.
create or replace view ledger_reporting.sources as
select
  s.source_key, s.service, s.db_instance_id, s.capture_epoch, s.capture_since,
  s.capture_boundary_trades_id, s.capture_boundary_positions_id,
  s.snapshot_complete, s.pending_changes, s.last_attempt_at, s.last_success_at,
  s.last_error, s.local_trades_count, s.local_positions_count,
  t.mirror_trades_count, p.mirror_positions_count,
  extract(epoch from (now() - s.last_success_at))::bigint as seconds_since_success,
  (s.last_error is null
   and s.last_success_at is not null
   and s.last_success_at >= s.last_attempt_at
   and s.snapshot_complete
   and s.pending_changes = 0
   and t.mirror_trades_count = s.local_trades_count
   and p.mirror_positions_count = s.local_positions_count) as mirror_complete
from public.ledger_sync_status s
left join lateral (
  select count(*) as mirror_trades_count from public.ledger_trades lt
  where lt.service = s.service and lt.db_instance_id = s.db_instance_id and not lt.deleted
) t on true
left join lateral (
  select count(*) as mirror_positions_count from public.ledger_positions lp
  where lp.service = s.service and lp.db_instance_id = s.db_instance_id and not lp.deleted
) p on true;

create or replace view ledger_reporting.trades as
select
  ledger_key, service, db_instance_id, source_id, venue, account_ref,
  run_mode, mode_evidence, (mode_evidence = 'writer_stamped') as mode_writer_stamped,
  pre_capture, opportunity_id, recorded_at, side, outcome, order_price, size,
  status, fill_price, fill_qty, slippage, order_id, client_order_id,
  fee_usd, fee_status, source_changed_at, synced_at
from public.ledger_trades
where not deleted;

create or replace view ledger_reporting.positions as
select
  ledger_key, service, db_instance_id, source_id, venue, account_ref,
  run_mode, mode_evidence, (mode_evidence = 'writer_stamped') as mode_writer_stamped,
  pre_capture, opportunity_id, market_identifier, market_ticker, entry_at,
  settled_at, status, expected_pnl, realized_pnl, pnl_basis, fee_usd, fee_status,
  source_changed_at, synced_at
from public.ledger_positions
where not deleted;

create or replace view ledger_reporting.venue_reconciliations as
select run_id, venue, account_ref, interval_start, interval_end, reporting_tz, reporting_day,
       venue_source, coverage_scope, status, coverage_verified, ledger_mirror_verified,
       incomplete_reasons, venue_order_count, ledger_order_count, matched_order_count,
       venue_records_out_of_interval, missing_in_ledger, missing_in_venue, qty_mismatch,
       venue_fees_usd, fees_complete, collector, collector_version, collected_at,
       checked_at, evidence
from public.ledger_venue_reconciliations;

-- Latest check per venue, account and local reporting day. A day with no
-- check has NO row: absence is unknown, never zero.
--   fills_verified: the latest check is 'matched' (verified venue coverage and
--     verified ledger mirror) and no mirrored ledger row for that venue and
--     account changed after the check within the window the check read
--     (2 days before to 1 day after the day). A later ledger correction makes
--     the check stale until it is re-run.
--   account_wide: the venue coverage was for all subaccounts. A check made
--     with a subaccount-restricted key covers that subaccount only.
-- Fills only: fees, settlements and positions are not reconciled here.
create or replace view ledger_reporting.venue_reconciliation_days as
with latest as (
  select distinct on (r.venue, r.account_ref, r.reporting_day) r.*
  from public.ledger_venue_reconciliations r
  where r.reporting_day is not null and r.account_ref is not null
  order by r.venue, r.account_ref, r.reporting_day, r.checked_at desc, r.id desc
)
select
  l.venue, l.account_ref, l.reporting_day, l.reporting_tz, l.interval_start, l.interval_end,
  l.run_id, l.status, l.coverage_scope, (l.coverage_scope = 'all_subaccounts') as account_wide,
  l.coverage_verified, l.ledger_mirror_verified, l.incomplete_reasons,
  l.venue_order_count, l.ledger_order_count, l.matched_order_count,
  l.venue_fees_usd, l.fees_complete, l.collected_at, l.checked_at,
  stale.changed as ledger_changed_since_check,
  (l.status = 'matched' and not stale.changed) as fills_verified,
  false as pnl_verified
from latest l
cross join lateral (
  select exists (
    select 1 from public.ledger_trades t
    where t.venue = l.venue
      and (t.account_ref = l.account_ref or t.account_ref is null)
      and t.synced_at > l.checked_at
      and (t.recorded_at is null
           or (t.recorded_at >= l.interval_start - interval '2 days'
               and t.recorded_at < l.interval_end + interval '1 day'))
  ) as changed
) stale;

-- Engine-computed realized PnL of live, settled positions per America/Detroit
-- day, venue and account label. A day with no settled live position has NO
-- row: absence is not zero. realized_pnl_sum is null when any settled position
-- in the group lacks realized_pnl. Rows with unknown mode or account are
-- excluded here and counted in unattributed_settlements_daily instead.
-- pnl_verified is always false: settlements, fees and positions are not
-- reconciled with the venue; fills_reconciled_for_day covers fills only.
create or replace view ledger_reporting.realized_pnl_daily as
with settled as (
  select p.service, p.db_instance_id, p.venue, p.account_ref, p.realized_pnl,
         (p.settled_at at time zone 'America/Detroit')::date as settle_day_local
  from public.ledger_positions p
  where not p.deleted
    and p.run_mode = 'live'
    and p.account_ref is not null
    and p.status = 'settled'
    and p.settled_at is not null
)
select
  s.settle_day_local,
  'America/Detroit'::text as reporting_tz,
  s.venue,
  s.account_ref,
  count(*) as settled_positions,
  count(*) filter (where s.realized_pnl is null) as positions_missing_pnl,
  case when count(*) filter (where s.realized_pnl is null) = 0
       then sum(s.realized_pnl) end as realized_pnl_sum,
  'engine_computed_unverified'::text as pnl_basis,
  'not_recorded'::text as fee_status,
  false as pnl_verified,
  bool_and(coalesce(src.mirror_complete, false)) as mirror_complete_now,
  coalesce(bool_or(d.fills_verified), false) as fills_reconciled_for_day,
  coalesce(bool_or(d.fills_verified and d.account_wide), false) as account_wide_fills_reconciled
from settled s
left join ledger_reporting.sources src
  on src.service = s.service and src.db_instance_id = s.db_instance_id
left join ledger_reporting.venue_reconciliation_days d
  on d.venue = s.venue and d.account_ref = s.account_ref
 and d.reporting_day = s.settle_day_local and d.reporting_tz = 'America/Detroit'
group by s.settle_day_local, s.venue, s.account_ref;

create or replace view ledger_reporting.unattributed_settlements_daily as
select
  (settled_at at time zone 'America/Detroit')::date as settle_day_local,
  'America/Detroit'::text as reporting_tz,
  venue,
  run_mode,
  (account_ref is null) as account_unknown,
  count(*) as settled_positions
from public.ledger_positions
where not deleted
  and status = 'settled'
  and settled_at is not null
  and (run_mode is distinct from 'live' or account_ref is null)
group by 1, 2, 3, 4, 5;

revoke all on all tables in schema ledger_reporting from public, anon, authenticated;
grant usage on schema ledger_reporting to ledger_reporter;
grant select on all tables in schema ledger_reporting to ledger_reporter;

COMMIT;
