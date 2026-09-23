-- DISPOSABLE DATABASE ONLY. Run as postgres with anon/authenticated/service_role
-- already present. The included migrations commit database-wide default ACLs.
\set ON_ERROR_STOP on

-- Reproduce the prior Supabase public-schema defaults and PostgreSQL's global
-- PUBLIC function default. These setup statements are unsafe on an existing DB.
ALTER DEFAULT PRIVILEGES FOR ROLE postgres GRANT EXECUTE ON FUNCTIONS TO PUBLIC;
ALTER DEFAULT PRIVILEGES FOR ROLE postgres IN SCHEMA public
  GRANT ALL ON TABLES TO anon, authenticated, service_role;
ALTER DEFAULT PRIVILEGES FOR ROLE postgres IN SCHEMA public
  GRANT ALL ON SEQUENCES TO anon, authenticated, service_role;
ALTER DEFAULT PRIVILEGES FOR ROLE postgres IN SCHEMA public
  GRANT EXECUTE ON FUNCTIONS TO anon, authenticated, service_role;
CREATE SCHEMA default_acl_private_probe;
CREATE FUNCTION default_acl_private_probe.existing_fn() RETURNS integer LANGUAGE sql AS 'SELECT 1';
DO $$ BEGIN
  IF NOT has_function_privilege('service_role', 'default_acl_private_probe.existing_fn()', 'EXECUTE') THEN
    RAISE EXCEPTION 'Baseline backend EXECUTE missing';
  END IF;
END $$;

\ir ../supabase/migrations/20260921173502_explicit_new_table_api_grants.sql
\ir ../supabase/migrations/20260921181000_private_api_defaults_with_backend_access.sql

BEGIN;
CREATE TABLE public.default_acl_probe(id integer);
CREATE SEQUENCE public.default_acl_probe_seq;
CREATE FUNCTION public.default_acl_probe_fn() RETURNS integer LANGUAGE sql AS 'SELECT 1';
CREATE FUNCTION default_acl_private_probe.future_fn() RETURNS integer LANGUAGE sql AS 'SELECT 1';
DO $$
DECLARE
  client_role text;
  privilege_name text;
  function_name text;
BEGIN
  FOREACH client_role IN ARRAY ARRAY['anon', 'authenticated'] LOOP
    FOREACH privilege_name IN ARRAY ARRAY['SELECT', 'INSERT', 'UPDATE', 'DELETE', 'TRUNCATE', 'REFERENCES', 'TRIGGER'] LOOP
      IF has_table_privilege(client_role, 'public.default_acl_probe', privilege_name) THEN
        RAISE EXCEPTION 'Unexpected table % for %', privilege_name, client_role;
      END IF;
    END LOOP;
    FOREACH privilege_name IN ARRAY ARRAY['USAGE', 'SELECT', 'UPDATE'] LOOP
      IF has_sequence_privilege(client_role, 'public.default_acl_probe_seq', privilege_name) THEN
        RAISE EXCEPTION 'Unexpected sequence % for %', privilege_name, client_role;
      END IF;
    END LOOP;
    FOREACH function_name IN ARRAY ARRAY['public.default_acl_probe_fn()', 'default_acl_private_probe.future_fn()'] LOOP
      IF has_function_privilege(client_role, function_name, 'EXECUTE') THEN
        RAISE EXCEPTION 'Unexpected EXECUTE on % for %', function_name, client_role;
      END IF;
    END LOOP;
  END LOOP;
  FOREACH privilege_name IN ARRAY ARRAY['SELECT', 'INSERT', 'UPDATE', 'DELETE', 'TRUNCATE', 'REFERENCES', 'TRIGGER'] LOOP
    IF NOT has_table_privilege('service_role', 'public.default_acl_probe', privilege_name) THEN
      RAISE EXCEPTION 'Missing backend table %', privilege_name;
    END IF;
  END LOOP;
  FOREACH privilege_name IN ARRAY ARRAY['USAGE', 'SELECT', 'UPDATE'] LOOP
    IF NOT has_sequence_privilege('service_role', 'public.default_acl_probe_seq', privilege_name) THEN
      RAISE EXCEPTION 'Missing backend sequence %', privilege_name;
    END IF;
  END LOOP;
  FOREACH function_name IN ARRAY ARRAY['public.default_acl_probe_fn()', 'default_acl_private_probe.future_fn()', 'default_acl_private_probe.existing_fn()'] LOOP
    IF NOT has_function_privilege('service_role', function_name, 'EXECUTE') THEN
      RAISE EXCEPTION 'Missing backend EXECUTE on %', function_name;
    END IF;
  END LOOP;
END $$;
ROLLBACK;
DROP SCHEMA default_acl_private_probe CASCADE;
\echo 'PASS: API defaults denied; backend function EXECUTE preserved across schemas'
