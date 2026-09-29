# Trade-ledger reporting mirror

Status: code merged behind flags that default to off. The Supabase schema is a
**draft** at `supabase/drafts/0007_trade_ledger_reporting.sql` and has not been
applied anywhere. Decision (Jonathon, 2026-09-29): reports read trades synced
to Supabase through a restricted read-only view/role. The operational ledger
stays the source record, and venue records check completeness.

## What is mirrored, and what is not

| Local source (trades.db) | Mirror | Notes |
| --- | --- | --- |
| `trades` rows, including later status/fill/slippage/order-id updates and deletes | `ledger_trades` | `recorded_at` is the engine log time, not the venue fill time |
| `positions` rows, including settlements and deletes | `ledger_positions` | `realized_pnl` is engine-computed at settlement (`pnl_basis = engine_computed_unverified`) |
| Fees | not recorded locally | `fee_usd` is null, `fee_status = not_recorded`. Venue fees come only from reconciliation |
| `partial_fills`, `transfers`, `opportunities` | not mirrored | Opportunities already mirror via `OpportunitySync` |
| Exporter state | `ledger_sync_status` | One row per service and DB file |
| Venue checks | `ledger_venue_reconciliations` | Written by a future job with read-only venue credentials |

## Provenance rules

- **`run_mode` sources.** `run_mode` is `paper` or `live` only when one of these supports it:
  - The writer stamped it at insert. `executor`, `mm_pilot`, `inventory_balancer` and `scripts/canary_trade.py` now pass it.
  - The row carries a dry-run marker: status `dry_run` or `paper_near_miss`, or an order id starting `dry_` or `dryfill_`.
- **Everything else is `unknown`.** That includes every historical row without a marker. Nothing reads today's `DRY_RUN` or account configuration to label history. A `live` stamp on a row with a dry-run marker becomes `unknown` with `conflict` evidence.
- **Simulated pilot fills look like real fills.** In D0 dry-run, the MM pilot logs simulated fills as `status = 'filled'`, so status alone never proves a real fill. Their `dry_...` order ids mark them as paper.
- **`account_ref` is a non-secret label.** It comes from `LEDGER_ACCOUNT_REFS` (a JSON map from platform to label) and is stamped only on rows written after it is set. Historical rows keep a null account, meaning unknown.
- **`pre_capture` marks older rows.** It is set for rows that existed before change capture was installed; their mode and account are usually unknown.
- **Reporting views keep uncertain rows out of PnL.** `realized_pnl_daily` excludes rows with unknown mode or account. Those rows are counted in `unattributed_settlements_daily`.
- **Backfilling history needs outside evidence.** Mirroring history with a live mode needs a boundary established independently: which service, account and mode wrote which rows. It is never inferred.

## How changes are captured and exported

1. **Capture.** `LEDGER_CAPTURE_ENABLED=true` makes `TradeDB` install SQLite triggers. They append every insert, update and delete on `trades` and `positions` to `ledger_outbox`, inside the writer's own transaction. They are local only, with no network I/O. The first install records a capture epoch and the maximum row ids at that moment. The triggers are never removed automatically.
2. **Export.** `LEDGER_SYNC_ENABLED=true` starts `ledger_sync.LedgerExporter` in `continuous.py`, every `LEDGER_SYNC_EVERY_N_SCANS` scans.
   - It runs on a worker thread through `run_in_executor`, with an in-flight guard. It uses its own SQLite connection and is never on the event loop or in an order path.
   - A failure only logs a warning.
3. **First sync of an epoch.** The exporter pages a full snapshot first, and it can resume after a restart. It then follows the outbox from where the snapshot began.
4. **Versions.** Each record carries `source_version`, the highest outbox sequence number visible when the row was read in the same read transaction. The remote version guard ignores a lower version within the same epoch. That makes replays, retries and partial batches idempotent.
5. **Deletes** become tombstones (`deleted = true`) that keep the row's last known state.
6. **Watermark.** The local watermark advances, and consumed outbox rows are pruned, only after every remote write succeeds. The status row gets `last_success_at` only on success.

**Keys.** `ledger_key` is `arbgrid:<service>:<db_instance_id>:<table>:<id>`. `db_instance_id` is a random id stored in the DB file, so a recreated volume or a second service never collides. Venue and account are attributes, not part of the key: a deleted row no longer has them, and history has no account at all.

## What a report may claim

- **One service's DB file** (`ledger_reporting.sources.mirror_complete`): the latest export succeeded, the snapshot is complete, no captured change is pending, and the mirror's row counts equal the local counts. This says nothing about an account.
- **Zero fills or zero PnL for an account and period.** This needs a `matched` venue reconciliation covering that account and interval, plus complete mirrors of every service that trades that account. Otherwise the value is null or unverified. An absent row in `realized_pnl_daily` means no data, not zero.
- **Realized PnL** stays `pnl_verified = false` until a settlement-level venue reconciliation exists. That doesn't exist yet.

## Venue reconciliation (`ledger_sync.reconcile_fills`)

Fails closed. A result is `matched` only when all of the following hold:

- **Coverage:** the venue source asserts complete coverage (`venue_coverage.complete is True`) for the same, known `account_ref`, over exactly the requested interval.
- **Venue records:** every in-interval record has an order id, a finite quantity and a timezone-aware fill time.
- **Ledger rows:** every relevant ledger fill is live, attributed to the account, and carries an order id and a finite quantity.
- **Agreement:** nothing is missing on either side and all quantities agree.

The other outcomes:

- **`incomplete`:** any missing or invalid evidence (listed in `incomplete_reasons`). This includes NaN or infinite values, naive or unparsable timestamps, and ledger fills of unknown mode or account in the interval.
- **`mismatched`:** valid evidence that disagrees.
- **Quiet period:** a verified interval with no activity on either side is `matched` with zero counts.

Timestamps are compared as UTC instants, never as strings, and only venue fills inside `[start, end)` count.

The draft table enforces the same rules with CHECK constraints:

- `matched` requires verified coverage, an account, a source, an interval, no reasons and nothing missing.
- `incomplete` requires at least one reason.
- Fees must be finite.

`fills_reconciled_for_day` is true only when a verified `matched` check covers the whole UTC day and no later check touching that day disagrees or is incomplete. The job that fetches venue records is deferred.

## Reporter access

- **The `ledger_reporter` role** is `NOLOGIN`. It has `USAGE` on the `ledger_reporting` schema and `SELECT` on its views only, with no base-table, write or function access.
- **The base tables** have RLS enabled with no policies, and `anon` and `authenticated` get no grants.
- **The views** run with the owner's rights, so reporters need no table privileges.
- **The schema is not exposed** through the API. Don't add `ledger_reporting` to PostgREST's exposed schemas.

## Verification (local)

```sh
pytest tests/test_ledger_sync.py -v
# Disposable Postgres, as postgres, with NOLOGIN anon/authenticated and
# service_role (BYPASSRLS, as on Supabase) created:
bash tests/supabase-ledger-reporting.sh
```

## Rollout plan (each step needs operator approval; none has been done)

1. **Review and merge** the code. Flags stay off, so nothing changes at runtime. Auto-deploy restarts services with the new nullable columns, and writers start stamping `run_mode` on new rows.
2. **Verify the target Supabase project.** Confirm ownership and schema (see `supabase/API-DEFAULTS-2026-09-21.md` about remote history). Then apply `0007` as a real migration, after reconciling it with the deployed migration history.
3. **Create a login role for routines** that is a member of `ledger_reporter`, with `default_transaction_read_only = on` and a secret held outside the repo. Connect through the pooler, not PostgREST.
4. **Set `LEDGER_ACCOUNT_REFS`** (non-secret labels) and `LEDGER_CAPTURE_ENABLED=true` on each service. Capture starts; the outbox grows until sync is on.
5. **Set `LEDGER_SYNC_ENABLED=true`.** The exporter uses the existing `SUPABASE_URL`/`SUPABASE_SERVICE_KEY` backend credential; a least-privilege writer role is a follow-up. The service name comes from `RAILWAY_SERVICE_NAME`, or `LEDGER_SERVICE_NAME` if that's missing. The first sync snapshots history with its honest (mostly unknown) provenance.
6. **Build a reconciliation job** with read-only venue credentials. It writes `ledger_venue_reconciliations` using `ledger_sync.reconcile_fills`.
7. **Point the routines** at the `ledger_reporting` views.

## Rollback

- **Stop exporting:** set `LEDGER_SYNC_ENABLED=false`. This affects nothing else.
- **Stop capture:** set `LEDGER_CAPTURE_ENABLED=false` and restart. Existing triggers stay, so the outbox keeps growing. Drop them with `DROP TRIGGER ledger_capture_<table>_<op>` (six triggers) during maintenance. A later re-enable starts a new epoch and a full re-snapshot.
- **Local schema:** the added columns and tables are nullable or additive and are read by nothing else. Leave them in place.
- **Remote:** revoke `ledger_reporter` membership from the login role, then drop the `ledger_reporting` schema and the `ledger_*` tables if needed. Nothing else depends on them.
