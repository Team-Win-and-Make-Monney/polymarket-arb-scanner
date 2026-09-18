-- Shared rewards / cross-engine P&L schema for the Financial Markets command center.
-- Source of truth stays in arbgrid SQLite; supabase_sync.py mirrors rewards here
-- so multiple engines (arbgrid, rewards-engine) can roll up P&L in one place.
-- Idempotent: safe to re-run.

-- Every reward accrual event (Kalshi LIP/VIP, Coinbase/Kraken staking, USDC lending, ...).
create table if not exists rewards_events (
  id           uuid primary key default gen_random_uuid(),
  engine       text not null,                 -- 'kalshi-lip', 'kalshi-vip', 'coinbase-staking', ...
  lane         text not null,                 -- 'prediction-markets', 'crypto-earn'
  tax_bucket   text not null,                 -- 'ordinary', 'possible_1256', 'gambling'
  event_date   date not null,
  asset        text,
  reward_usd   numeric(14, 4),
  apy_snapshot numeric(8, 4),
  source_key   text not null,                 -- dedupe key (NOT NULL: NULLs don't dedupe under the unique constraint)
  notes        text,
  created_at   timestamptz not null default now(),
  unique (engine, source_key)
);

create index if not exists rewards_events_engine_date_idx
  on rewards_events (engine, event_date);

-- Yield-venue rate snapshots for the USDC routing monitor (Morpho/Aave/...).
create table if not exists rate_snapshots (
  id          uuid primary key default gen_random_uuid(),
  venue       text not null,                  -- 'morpho-base', 'aave-base'
  asset       text not null,                  -- 'USDC'
  apy         numeric(8, 4),
  tvl_usd     numeric(18, 2),
  snapshot_at timestamptz not null default now()
);

create index if not exists rate_snapshots_venue_idx
  on rate_snapshots (venue, snapshot_at);

-- Cache of currently reward-eligible Kalshi LIP markets (weekly scan output).
create table if not exists kalshi_lip_markets (
  ticker          text primary key,
  title           text,
  daily_pool_usd  numeric(10, 2),
  target_size     integer,
  discount_factor numeric(4, 2),
  period_end      date,
  updated_at      timestamptz not null default now()
);

-- Kill switches / control flags for the reward routines.
create table if not exists bot_controls (
  key        text primary key,
  value      boolean not null default true,
  updated_at timestamptz not null default now()
);

insert into bot_controls (key, value) values
  ('lip_bot_enabled', true),
  ('vip_tracker_enabled', true),
  ('staking_monitor_enabled', true)
on conflict (key) do nothing;
