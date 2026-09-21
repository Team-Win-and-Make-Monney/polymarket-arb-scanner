#!/usr/bin/env bash
# Run with PGHOST/PGPORT/PGUSER/PGDATABASE pointing to a DISPOSABLE database.
set -euo pipefail
cd "$(dirname "$0")/.."
psql -X -v ON_ERROR_STOP=1 <<'SQL'
DO $$ BEGIN
  IF EXISTS (SELECT FROM pg_class WHERE relnamespace = 'public'::regnamespace)
     OR EXISTS (SELECT FROM pg_proc WHERE pronamespace = 'public'::regnamespace) THEN
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
