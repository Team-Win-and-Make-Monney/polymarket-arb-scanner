-- Called after the actual repository migration chain by supabase-clean-replay.sh.
\set ON_ERROR_STOP on
BEGIN;
INSERT INTO public.broker_intents(idempotency_key, intent_type, payload)
  VALUES ('guard-replay-probe', 'flip_lane', '{}');
INSERT INTO public.broker_intent_events(intent_id, status)
  SELECT id, 'PENDING' FROM public.broker_intents WHERE idempotency_key = 'guard-replay-probe';
INSERT INTO public.broker_intent_attempts(intent_id, holder)
  SELECT id, 'fixture' FROM public.broker_intents WHERE idempotency_key = 'guard-replay-probe';
INSERT INTO public.broker_halts(scope, action) VALUES ('fixture', 'halt');
DO $$
DECLARE
  table_name text;
  statement text;
  signatures regprocedure[] := ARRAY[
    'public.broker_block_append_only()'::regprocedure,
    'public.broker_reject_mutation()'::regprocedure,
    'public.broker_reject_truncate()'::regprocedure
  ];
BEGIN
  IF (SELECT count(*) FROM pg_proc WHERE oid = ANY(signatures)
      AND proconfig = ARRAY['search_path=pg_catalog'] AND NOT prosecdef) <> 3 THEN
    RAISE EXCEPTION 'A broker guard is not pinned';
  END IF;
  FOREACH table_name IN ARRAY ARRAY['broker_intents', 'broker_intent_events', 'broker_intent_attempts', 'broker_halts'] LOOP
    IF (SELECT count(*) FROM pg_trigger WHERE tgrelid = format('public.%I', table_name)::regclass
        AND tgfoid = 'public.broker_block_append_only()'::regprocedure AND NOT tgisinternal) <> 2 THEN
      RAISE EXCEPTION 'Unexpected actual guard bindings on %', table_name;
    END IF;
    FOREACH statement IN ARRAY ARRAY[
      format('UPDATE public.%I SET id = id', table_name),
      format('DELETE FROM public.%I', table_name),
      format('TRUNCATE public.%I CASCADE', table_name)
    ] LOOP
      BEGIN
        EXECUTE statement;
        RAISE EXCEPTION 'Unexpected write: %', statement;
      EXCEPTION WHEN raise_exception THEN
        IF SQLERRM NOT LIKE '% is append-only' THEN RAISE; END IF;
      END;
    END LOOP;
  END LOOP;
END $$;
ROLLBACK;
\echo 'PASS: complete repository replay; all four actual append-only tables protected'
