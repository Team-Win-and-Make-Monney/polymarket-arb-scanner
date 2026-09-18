-- Enable Row Level Security (deny-by-default) on the rewards schema.
-- These tables are written only by server-side service-role jobs, which bypass
-- RLS; enabling it locks the anon key out of reward P&L. Add policies only if a
-- client/anon read path is ever needed.
-- Applied to project financial-markets-rewards (ref rtvusfddepldnpknqpjt) 2026-06-13.

alter table public.rewards_events enable row level security;
alter table public.rate_snapshots enable row level security;
alter table public.kalshi_lip_markets enable row level security;
alter table public.bot_controls enable row level security;
