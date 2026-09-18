-- Shared cross-engine P&L ledger. Every fill/period across all engines (arbgrid,
-- quant-engine, ...) lands here tagged engine/lane/tax_bucket, so the daily digest
-- and the day-90 review roll up P&L in one place. Idempotent: safe to re-run.
-- Numbered 0003 to sit after the rewards schema migrations (0001/0002), which land
-- with the Kalshi-rewards work; the pnl table is independent of them. All DDL is
-- schema-qualified to public so creation is deterministic regardless of search_path.

-- gen_random_uuid() is core in Postgres 13+ (Supabase); pgcrypto is a defensive
-- no-op for older/non-Supabase targets.
create extension if not exists pgcrypto;

create table if not exists public.pnl (
  id          uuid primary key default gen_random_uuid(),
  engine      text not null,                  -- 'arbgrid', 'quant', ...
  lane        text not null,                  -- 'prediction-markets', 'perp_carry', 'sports', ...
  tax_bucket  text not null
              check (tax_bucket in ('ordinary', 'possible_1256', 'gambling')),
  amount_usd  numeric(14, 4) not null,        -- signed realized P&L
  trade_date  date not null,
  asset       text,
  notes       text,
  created_at  timestamptz not null default now()
);

create index if not exists pnl_engine_date_idx on public.pnl (engine, trade_date);
create index if not exists pnl_lane_date_idx on public.pnl (lane, trade_date);
create index if not exists pnl_tax_bucket_idx on public.pnl (tax_bucket, trade_date);

-- Deny-by-default RLS: written only by server-side service-role jobs (which bypass
-- RLS); this locks the anon key out of P&L. Add policies only if a client read path
-- is ever needed.
alter table public.pnl enable row level security;
