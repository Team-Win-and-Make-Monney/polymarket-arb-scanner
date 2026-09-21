# API default privileges and backend compatibility

The September 21 remediation removes implicit `anon` and `authenticated`
access to future application objects created by `postgres`. It preserves
trusted backend access. The function policy applies globally; public table
and sequence defaults are scoped to `public`. Provider-specific schema
grants and objects created by other roles remain outside this repair.

PostgreSQL originally grants function EXECUTE to PUBLIC globally, including
`service_role`. Replacing PUBLIC with an explicit global `service_role` grant
preserves that backend capability, including functions in nonpublic schemas.
This is intentional. Limiting that replacement to `public` would remove a
preexisting capability and requires a separate consumer inventory and policy
decision. Schema USAGE is still required to call a function; these migrations
do not grant schema access or add schemas to PostgREST.

Global and per-schema default privileges are additive. The final
public-schema REVOKE in `20260921181000` removes redundant schema-level
EXECUTE entries; it does not and is not intended to override the global
backend grant. See the [PostgreSQL default privilege documentation](https://www.postgresql.org/docs/17/sql-alterdefaultprivileges.html).
The applied migration files retain their original bytes.

## Reproducible local verification

Run only in a fresh disposable PostgreSQL 17 database, as `postgres`, with
NOLOGIN roles `anon`, `authenticated` and `service_role` already created:

```sh
psql -X -v ON_ERROR_STOP=1 -f tests/supabase-default-grants.sql
```

The fixture reconstructs the earlier defaults, verifies backend EXECUTE on
a preexisting nonpublic function, applies both actual grant migrations, and
creates new public and nonpublic functions. Every tested API privilege is
denied and every tested backend privilege remains. Individual table and
sequence privileges are checked separately, avoiding the OR semantics of
comma-separated privilege lists. Existing-object grants remain unchanged.

This test covers the SQL access contract, not application delivery, all
provider-owned schema defaults, a physical restore, or trading execution.
Do not replay these setup statements against a linked Supabase database.
The migration history in this repository is not a replacement for fetching
and reconciling a deployed project's actual history before any future push.
