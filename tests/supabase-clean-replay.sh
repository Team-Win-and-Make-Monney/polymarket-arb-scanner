#!/usr/bin/env bash
# Run with PGHOST/PGPORT/PGUSER/PGDATABASE pointing to a DISPOSABLE database.
set -euo pipefail
cd "$(dirname "$0")/.."
psql -X -v ON_ERROR_STOP=1 <<'SQL'
DO $$ BEGIN
  -- Namespace dependencies include types, collations, operators, text-search
  -- objects and extensions as well as tables/functions. Reject any dependent
  -- object rather than keeping an incomplete list of per-catalog checks.
  IF EXISTS (SELECT FROM pg_depend
             WHERE refclassid = 'pg_namespace'::regclass
               AND refobjid = 'public'::regnamespace) THEN
    RAISE EXCEPTION 'Clean replay requires an empty disposable public schema';
  END IF;
  IF current_user <> 'postgres' THEN
    RAISE EXCEPTION 'Clean replay requires the postgres migration role';
  END IF;
END $$;
SQL
for migration in supabase/migrations/*.sql; do
  psql -X -v ON_ERROR_STOP=1 -f "$migration"
done
psql -X -v ON_ERROR_STOP=1 -f tests/supabase-clean-replay-assertions.sql
