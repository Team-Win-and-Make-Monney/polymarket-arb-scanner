-- Paper-trading opportunity mirror for the Financial Markets command center.
-- trades.db (SQLite on the arbgrid deploy volume) stays the source of truth;
-- supabase_sync.OpportunitySync mirrors detections here so the paper record is
-- durable off-box and queryable by cloud agents/routines that have no access
-- to the deploy environment's credentials. Idempotent: safe to re-run.

create table if not exists public.paper_opportunities (
  id           uuid primary key default gen_random_uuid(),
  engine       text not null default 'arbgrid',
  source_id    bigint not null,               -- opportunities.id in the engine's SQLite
  detected_at  timestamptz not null,
  opp_type     text not null,
  market       text,
  prices       text,
  total_cost   numeric(14, 4),
  net_profit   numeric(14, 4),
  net_roi      numeric(10, 6),
  depth        numeric(14, 2),
  action       text,                          -- dry_run / skipped:<reason> / executed
  synced_at    timestamptz not null default now(),
  unique (engine, source_id)
);

create index if not exists paper_opportunities_type_date_idx
  on public.paper_opportunities (opp_type, detected_at);

create index if not exists paper_opportunities_action_idx
  on public.paper_opportunities (action);

-- Deny-by-default RLS, same posture as 0002: written only by service-role jobs.
alter table public.paper_opportunities enable row level security;
