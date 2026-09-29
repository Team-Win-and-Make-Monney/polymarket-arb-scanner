#!/usr/bin/env bash
# Run with PGHOST/PGPORT/PGUSER/PGDATABASE pointing to a DISPOSABLE, empty
# database, as postgres, with NOLOGIN roles anon, authenticated and
# service_role already created (service_role with BYPASSRLS, as on Supabase). Applies every repository migration, then the
# UNAPPLIED ledger draft, then its assertions.
set -euo pipefail
cd "$(dirname "$0")/.."
bash tests/supabase-clean-replay.sh
psql -X -v ON_ERROR_STOP=1 -f supabase/drafts/0007_trade_ledger_reporting.sql
# Idempotent: a second application must succeed unchanged.
psql -X -v ON_ERROR_STOP=1 -f supabase/drafts/0007_trade_ledger_reporting.sql
psql -X -v ON_ERROR_STOP=1 -f tests/supabase-ledger-reporting.sql
