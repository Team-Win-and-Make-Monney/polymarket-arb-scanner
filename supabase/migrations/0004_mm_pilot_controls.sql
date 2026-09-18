-- Plan 10 (Kalshi reward-MM pilot): seed the mm_pilot_enabled kill switch in
-- bot_controls, default OFF. The pilot's ControlsPoller reads this row every
-- MM_CONTROLS_POLL_SECONDS; a stale or missing value fails closed (quotes
-- pulled). Mirrors 0001's lip_bot_enabled seed. Idempotent.
--
-- NOTE: the spec named this file 0003_mm_pilot_controls.sql, but
-- 0003_pnl_schema.sql already existed on master, so this is 0004.

insert into bot_controls (key, value) values
  ('mm_pilot_enabled', false)
on conflict (key) do nothing;
