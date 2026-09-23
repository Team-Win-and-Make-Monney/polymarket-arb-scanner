BEGIN;
SET LOCAL lock_timeout = '5s';
ALTER FUNCTION public.broker_reject_mutation() SET search_path = pg_catalog;
ALTER FUNCTION public.broker_reject_truncate() SET search_path = pg_catalog;
COMMIT;
