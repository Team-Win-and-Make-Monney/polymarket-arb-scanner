-- Make terminal broker outcomes write-once under concurrent writers.

create or replace function public.broker_append_intent_event(
  p_intent_id bigint, p_status text, p_reason text default ''
) returns bigint
language plpgsql security definer set search_path = public as $$
declare
  latest_status text;
  event_id bigint;
begin
  perform pg_advisory_xact_lock(p_intent_id);
  select status into latest_status
  from public.broker_intent_events
  where intent_id = p_intent_id
  order by id desc limit 1;

  if latest_status in ('EXECUTED', 'REJECTED', 'IN_DOUBT', 'HARD_STOP') then
    raise exception 'intent % is already terminal; outcomes are write-once', p_intent_id;
  end if;

  insert into public.broker_intent_events(intent_id, status, reason)
  values (p_intent_id, p_status, p_reason)
  returning id into event_id;
  return event_id;
end;
$$;

revoke all on function public.broker_append_intent_event(bigint, text, text) from public;
grant execute on function public.broker_append_intent_event(bigint, text, text) to service_role;
