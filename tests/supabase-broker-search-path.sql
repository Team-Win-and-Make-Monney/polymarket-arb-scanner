-- DISPOSABLE DATABASE ONLY. Minimal trigger bodies match the pre-pin schema
-- readback from September 21. This does not replay the full provider history.
\set ON_ERROR_STOP on
CREATE FUNCTION public.broker_reject_mutation() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  RAISE EXCEPTION '% is append-only', tg_table_name;
END;
$$;
CREATE FUNCTION public.broker_reject_truncate() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  RAISE EXCEPTION '% is append-only (truncate blocked)', tg_table_name;
END;
$$;
\ir ../supabase/migrations/20260921172506_pin_broker_function_search_paths.sql
BEGIN;
CREATE TABLE public.broker_pin_probe(id integer);
INSERT INTO public.broker_pin_probe VALUES (1);
CREATE TRIGGER reject_mutation BEFORE UPDATE OR DELETE ON public.broker_pin_probe
  FOR EACH ROW EXECUTE FUNCTION public.broker_reject_mutation();
CREATE TRIGGER reject_truncate BEFORE TRUNCATE ON public.broker_pin_probe
  EXECUTE FUNCTION public.broker_reject_truncate();
DO $$
DECLARE statement text;
BEGIN
  IF (SELECT count(*) FROM pg_proc WHERE oid IN
      ('public.broker_reject_mutation()'::regprocedure, 'public.broker_reject_truncate()'::regprocedure)
      AND proconfig = ARRAY['search_path=pg_catalog'] AND NOT prosecdef) <> 2 THEN
    RAISE EXCEPTION 'Unexpected function configuration';
  END IF;
  FOREACH statement IN ARRAY ARRAY[
    'UPDATE public.broker_pin_probe SET id = 2',
    'DELETE FROM public.broker_pin_probe',
    'TRUNCATE public.broker_pin_probe'
  ] LOOP
    BEGIN
      EXECUTE statement;
      RAISE EXCEPTION 'Unexpected write: %', statement;
    EXCEPTION WHEN raise_exception THEN
      IF SQLERRM NOT LIKE 'broker_pin_probe is append-only%' THEN RAISE; END IF;
    END;
  END LOOP;
  IF (SELECT count(*) FROM public.broker_pin_probe WHERE id = 1) <> 1 THEN
    RAISE EXCEPTION 'Probe row changed';
  END IF;
END $$;
ROLLBACK;
DROP FUNCTION public.broker_reject_mutation();
DROP FUNCTION public.broker_reject_truncate();
\echo 'PASS: pinned search paths retain update/delete/truncate protection'
