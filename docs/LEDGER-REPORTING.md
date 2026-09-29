# Trade-ledger reporting mirror

Status: proposed in PR #190; every flag defaults to off. The Supabase schema is a
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
| Venue checks | `ledger_venue_reconciliations` | Written by `scripts/reconcile_venue_fills.py` (Kalshi fills today), one row per run |

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
- **Zero fills for an account and day.** This needs `ledger_reporting.venue_reconciliation_days.fills_verified = true` for that venue, account and America/Detroit day. The check behind it covered the venue completely, verified the mirrors of every mapped service, and no mirrored ledger row changed afterwards. Otherwise the value is null or unverified. A missing row in either view means no data, not zero.
- **Account-wide.** Only when `account_wide = true`, meaning the check covered all subaccounts. A check made with a subaccount-restricted key covers that subaccount only.
- **Realized PnL** stays `pnl_verified = false`. Fills reconciliation does not cover fees, settlements or positions, and nothing reconciles those yet.

## Venue reconciliation

**Comparison rules** (`ledger_sync.reconcile_fills`). It fails closed. A result is `matched` only when all of the following hold:

- **Coverage:** the venue source asserts complete coverage (`venue_coverage.complete is True`) for the same, known `account_ref`, over exactly the requested interval.
- **Venue records:** every in-interval record has an order id, a finite quantity and a timezone-aware fill time.
- **Ledger rows:** every relevant ledger fill is live, attributed to the account, and carries an order id and a finite quantity.
- **Agreement:** nothing is missing on either side and all quantities agree.
- **No caller gaps:** the caller reported no evidence gaps (`evidence_gaps`).

Any missing or invalid evidence is `incomplete`, with reasons. Valid evidence that disagrees is `mismatched`. A verified quiet day is `matched` with zero counts. Timestamps are compared as UTC instants. Only venue fills inside `[start, end)` count.

### Kalshi fill collector (`kalshi_fill_collector.py`)

The collector is read-only. Its transport allows only `GET` on `/portfolio/fills`, `/historical/fills` and `/historical/cutoff`, reusing the existing `KalshiClient` signing. It has no order code.

**Source docs** (Kalshi Trade API OpenAPI 3.31.0, retrieved 2026-09-29):
- <https://docs.kalshi.com/getting_started/historical_data>
- <https://docs.kalshi.com/api-reference/portfolio/get-fills>
- <https://docs.kalshi.com/api-reference/historical/get-historical-fills>
- <https://docs.kalshi.com/api-reference/historical/get-historical-cutoff-timestamps>

**What those docs require, and how the collector handles each point:**
- **Tiers.** `trades_created_ts` from `/historical/cutoff` splits fills: older fills are only in `/historical/fills`. The collector reads whichever tiers the interval needs.
- **Moving cutoff.** It reads the cutoff before and after collecting. If the cutoff moved, it retries once, then marks the collection `venue_cutoff_moved`.
- **Pagination.** Every tier is paginated until the cursor is empty. An empty page with a cursor is not the end. It stops with a gap on any of these:
  - a repeated cursor (`venue_cursor_repeated`)
  - page-limit exhaustion (`venue_page_limit_exhausted`)
  - a malformed page (`venue_page_malformed`)
  - a failed page. Retries are bounded: 3 attempts with backoff on 429, 5xx or transport errors (`venue_request_failed`), and no retry on other 4xx (`venue_request_rejected`).
- **Partial results.** Successful partial pagination never counts as coverage. The old `KalshiClient.get_fills` list helper is not used: it defaults to 5 pages, can return a partial list silently, and stops at an empty page even when a cursor remains.
- **Time bounds.** `min_ts`/`max_ts` are documented only as "after"/"before" a Unix second. The collector over-fetches one second on each side, and reconciliation filters the exact interval.
- **Fill fields:**
  - `fill_id` (with `trade_id` as a legacy alias that must agree)
  - `order_id`
  - `count_fp`: a fixed-point string, finite, positive, at most 2 decimals, fractional allowed
  - `fee_cost`: fixed-point dollars
  - `created_time`, cross-checked against the legacy `ts` when both are present
  - `subaccount_number`
- **Duplicates.** A duplicate `fill_id` with identical content is dropped (tiers can overlap). One with different content makes the collection incomplete (`venue_conflicting_duplicate_fill`).
- **Scope (declared by the operator, never inferred).** `LEDGER_KALSHI_SCOPE` names:
  - the account label
  - the API key's SHA-256 fingerprint (never the key)
  - `subaccount` (`"all"` or 0–63)
  - the ledger services that trade the account
  - who verified it, and when

  The running key must match the fingerprint (`venue_credential_scope_mismatch`). A key restricted to one subaccount only ever returns that subaccount. Its checks record `coverage_scope = subaccount:N` and are never account-wide. Fills from another subaccount make the check incomplete.
- **Finality.** A day is collectable `LEDGER_RECON_FINALITY_SECONDS` (default 900) after it ends. Before that, it is incomplete (`venue_interval_not_final`).

### Reconciliation job (`venue_reconciliation.py`, `scripts/reconcile_venue_fills.py`)

- **Reporting days.** Days are America/Detroit calendar days converted to UTC: 23 hours in March, 25 in November. The draft table enforces that `interval_start`/`interval_end` equal the local day's bounds.
- **Ledger side.** It reads the Supabase mirror, never a local `trades.db`. It first verifies every mapped service's `ledger_sync_status`:
  - present: `ledger_mirror_source_missing`
  - one DB instance throughout the interval: `ledger_mirror_source_changed`
  - capture began before the interval: `ledger_capture_not_covering_interval`
  - latest export succeeded: `ledger_mirror_sync_failing`
  - snapshot complete and nothing pending: `ledger_mirror_incomplete`
  - last success after the day plus the finality lag: `ledger_mirror_stale`
  - mirror row counts equal the local counts: `ledger_mirror_count_mismatch`
- **Reads.** Every mirror read is paged with an exact count. A short or capped read is `ledger_mirror_read_truncated`. Rows for the account written by a service outside the mapping make the check incomplete (`ledger_unmapped_source`).
- **Empty or stale mirrors.** An empty, stale or truncated mirror can never yield a reconciled zero.
- **Rows.** Each run writes one row, keyed by `run_id` so a retry is idempotent. It records the gaps, `coverage_scope`, `ledger_mirror_verified` and an `evidence` object: pages, cutoffs, retries, duplicates and the sources checked. Re-runs add rows, and the latest check per day wins.
- **Staleness.** `venue_reconciliation_days` marks a check stale when a mirrored ledger row for that venue and account changes after it, for example a fill correction. The day is then no longer verified until the check is re-run.
- **Writer change.** The MM pilot now records `fill_qty` (contracts) on each fill row, so its orders can be compared. Older pilot rows have no quantity and reconcile as incomplete.

Run it as its own process with the existing credentials. It prints JSON and writes only with `--write`:

```sh
python scripts/reconcile_venue_fills.py --print-key-fingerprint   # value for LEDGER_KALSHI_SCOPE
python scripts/reconcile_venue_fills.py --venue kalshi --day 2026-09-28 [--write]
```

**Known limits:**
- Only order-level quantities are compared; the local ledger has no venue fill ids.
- An order that fills across a day boundary shows as a quantity mismatch on both days.
- Fees are summed from the venue side only.
- Settlement and position reconciliation do not exist yet.

## Reporter access

- **The `ledger_reporter` role** is `NOLOGIN`. It has `USAGE` on the `ledger_reporting` schema and `SELECT` on its views only, with no base-table, write or function access.
- **The base tables** have RLS enabled with no policies, and `anon` and `authenticated` get no grants.
- **The views** run with the owner's rights, so reporters need no table privileges.
- **The schema is not exposed** through the API. Don't add `ledger_reporting` to PostgREST's exposed schemas.

## Verification (local)

```sh
pytest tests/test_ledger_sync.py -v
pytest tests/test_venue_reconciliation.py -v
# Disposable Postgres, as postgres, with NOLOGIN anon/authenticated and
# service_role (BYPASSRLS, as on Supabase) created:
bash tests/supabase-ledger-reporting.sh
```

## Rollout plan

See [`LEDGER-ROLLOUT-PLAN.md`](LEDGER-ROLLOUT-PLAN.md): exact targets, gates, order, verification and rollback. Every step needs operator approval, and none has been done.

## Rollback

- **Stop exporting:** set `LEDGER_SYNC_ENABLED=false`. This affects nothing else.
- **Stop capture:** set `LEDGER_CAPTURE_ENABLED=false` and restart. Existing triggers stay, so the outbox keeps growing. Drop them with `DROP TRIGGER ledger_capture_<table>_<op>` (six triggers) during maintenance. A later re-enable starts a new epoch and a full re-snapshot.
- **Local schema:** the added columns and tables are nullable or additive and are read by nothing else. Leave them in place.
- **Remote:** revoke `ledger_reporter` membership from the login role, then drop the `ledger_reporting` schema and the `ledger_*` tables if needed. Nothing else depends on them.
