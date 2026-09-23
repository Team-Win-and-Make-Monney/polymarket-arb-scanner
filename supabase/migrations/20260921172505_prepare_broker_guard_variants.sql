-- Compatibility prerequisite for the immutable 172506 pin migration.
-- The deployed broker uses reject_mutation/reject_truncate; repository 0005
-- uses block_append_only. Never replace deployed function bodies or triggers.
BEGIN;
SET LOCAL lock_timeout = '5s';
DO $migration$
BEGIN
  IF to_regprocedure('public.broker_reject_mutation()') IS NULL THEN
    EXECUTE $definition$
      CREATE FUNCTION public.broker_reject_mutation() RETURNS trigger
      LANGUAGE plpgsql SET search_path = pg_catalog AS $body$
      BEGIN
        RAISE EXCEPTION '% is append-only', tg_table_name;
      END;
      $body$;
    $definition$;
  END IF;
  IF to_regprocedure('public.broker_reject_truncate()') IS NULL THEN
    EXECUTE $definition$
      CREATE FUNCTION public.broker_reject_truncate() RETURNS trigger
      LANGUAGE plpgsql SET search_path = pg_catalog AS $body$
      BEGIN
        RAISE EXCEPTION '% is append-only (truncate blocked)', tg_table_name;
      END;
      $body$;
    $definition$;
  END IF;
  IF to_regprocedure('public.broker_block_append_only()') IS NOT NULL THEN
    ALTER FUNCTION public.broker_block_append_only() SET search_path = pg_catalog;
  END IF;
END;
$migration$;
COMMIT;
