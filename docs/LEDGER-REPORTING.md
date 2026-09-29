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

1. **Capture.** `LEDGER_CAPTURE_ENABLED=true` makes `TradeDB` install SQLite triggers. They append every insert, update and delete on `trades` and `positions` to `ledger_outbox`, inside the writer's own transaction. They are local only, with no network I/O. The install is one SQLite transaction. The first install, or any install that finds the triggers incomplete or the epoch missing, records a new capture epoch and the maximum row ids at that moment. It also records a new `db_instance_id` and adds the old one to `superseded_db_instance_ids`, the file's full ancestry, so each epoch is a separate mirror generation. The triggers are never removed automatically.
2. **Export.** `LEDGER_SYNC_ENABLED=true` makes `continuous.py` start `ledger_sync.LedgerSyncWorker`, which runs `LedgerExporter.sync_once` every `LEDGER_SYNC_INTERVAL_SECONDS` (default 60).
   - It runs on its own daemon thread, independent of the scan loop, one sync at a time. It uses its own SQLite connection and is never on the event loop or in an order path.
   - It covers every `--mode`, including `--mode mm-pilot`. The `kalshi-mm-pilot` service runs `python scanner.py --continuous --mode mm-pilot --dry-run` (Railway start command, read 2026-09-29), and the pilot writes the same `TradeDB` as continuous mode.
   - At shutdown it stops after the MM pilot, then exports once more so the last rows are not left only in the local file. Shutdown waits at most 15 seconds for that export; a slower one finishes on the worker's own thread, which closes its SQLite connection only after the export ends, and the feed shutdown continues meanwhile.
   - A failure only logs a warning.
3. **First sync of an epoch.** The exporter first publishes the generation's status row as incomplete, listing every earlier id of the file in `supersedes_db_instance_ids`; if that write fails, nothing is pushed. It then pages a full snapshot, which can resume after a restart, and follows the outbox from where the snapshot began.
   - From that status write on, the views drop every row of every superseded generation. That includes rows deleted locally while nothing was captured, which an old generation could never have tombstoned. Because the ancestry is complete, two resets before a sync still retire the last exported generation. The new generation reports `mirror_complete = false` until its snapshot and outbox are fully exported.
   - A delayed write from the old generation can only reach the old generation's keys and status row, which stay excluded. A generation once superseded stays superseded.
4. **Versions.** Each record carries `source_version`, the highest outbox sequence number visible when the row was read in the same read transaction. The remote version guard ignores a lower version. A `ledger_key` belongs to one epoch, and the guard refuses a write that carries another epoch. That makes replays, retries and partial batches idempotent.
   - The status row has its own guard: it ignores a status whose `last_attempt_at` is older than the stored one, and refuses a change of instance or epoch.
5. **Deletes** become tombstones (`deleted = true`). The remote guard keeps the stored venue, account, order id and time on a tombstone even if the upsert sends nulls, so a delete still invalidates venue checks. A replay of the same version leaves the stored row unchanged, including its provenance and `synced_at`.
6. **Watermark.** The local watermark advances, and consumed outbox rows are pruned, only after every remote write succeeds. The watermark and the pruning commit together in one transaction. The status row gets `last_success_at` only on success.

**Keys.** `ledger_key` is `arbgrid:<service>:<db_instance_id>:<table>:<id>`. `db_instance_id` is a random id stored in the DB file and replaced with each new capture epoch, so a recreated volume, a second service or a re-snapshot never collides with earlier rows. Venue and account are attributes, not part of the key: a deleted row no longer has them, and history has no account at all.

## What a report may claim

- **One service's DB file** (`ledger_reporting.sources.mirror_complete`): the latest export succeeded, the snapshot is complete, no captured change is pending, and the mirror's row counts equal the local counts. This says nothing about an account.
- **Zero fills for an account and day.** This needs `ledger_reporting.venue_reconciliation_days.fills_verified = true` for that venue, account and America/Detroit day. That means:
  - the check covered the venue completely and verified the mirrors of every mapped service;
  - no mirrored row it depended on was written at or after its mirror read began; and
  - every ledger source it verified is still current: the latest instance of its service, fully exported, and successful within `max_source_age_seconds` of now.

  Otherwise the value is null or unverified. A missing row in either view means no data, not zero.
- **As-of claims.** When the check is still unchanged but a source has stopped or been replaced, `fills_matched_as_of` keeps the mirror read time and `mirror_current` is false. A report may say "matched as of <time>", not "verified".
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
- **Ledger side.** It reads the Supabase mirror, never a local `trades.db`. Instances a later capture generation supersedes are ignored, both their status rows and their ledger rows, as in the views. It first verifies every mapped service's `ledger_sync_status`:
  - present: `ledger_mirror_source_missing`
  - one DB instance throughout the interval: `ledger_mirror_source_changed`
  - capture began before the interval: `ledger_capture_not_covering_interval`
  - latest export succeeded: `ledger_mirror_sync_failing`
  - snapshot complete and nothing pending: `ledger_mirror_incomplete`
  - last success after the day plus the finality lag: `ledger_mirror_stale`
  - last success within `LEDGER_RECON_MAX_SOURCE_AGE_SECONDS` (default 3600) of now, even for an old day: `ledger_mirror_source_not_recent`
  - mirror row counts equal the local counts: `ledger_mirror_count_mismatch` (the `venue_reconciliation_days` view re-checks the live trade count too, so a later hard delete in the mirror ends `mirror_current`)
- **Interval scoping.** Both sides count only the day. The venue side uses each fill's own time. The ledger side uses `recorded_at`, which trails the venue fill by up to 300 seconds (`DEFAULT_RECORDING_LAG`) and can lead it by up to 120 seconds of clock skew (`DEFAULT_CLOCK_SKEW`). A ledger row recorded in `[boundary − 120 s, boundary + 300 s)` of either day boundary can't be placed in or out of the day, so its order goes to `boundary_ambiguous_orders` and the check is incomplete (`ledger_boundary_ambiguous`). An undated or unattributed row near a boundary is also ambiguous. An order that filled across local midnight is compared only on the part inside the day.
- **Reads.** Every mirror page is read with an exact count. A short or capped read is `ledger_mirror_read_truncated`. Rows for the account written by a service outside the mapping make the check incomplete (`ledger_unmapped_source`).
- **Read fence.** Any of these makes the check `ledger_mirror_changed_during_read`:
  - the exact total changes between pages;
  - any source status field (instance, epoch, watermark, pending, counts, attempt and success times, error) differs between the reads before and after the rows;
  - an in-scope row has `synced_at` at or after the read start. In scope means this venue (or a tombstone without one), and recorded in `[day start − 120 s, day end + 300 s)`, undated, or one of the compared order ids.

  The read start is the job's clock minus a 120-second skew allowance.
- **Empty or stale mirrors.** An empty, stale or truncated mirror can never yield a reconciled zero.
- **Rows.** Each run writes one row, keyed by `run_id` so a retry is idempotent. It records:
  - the gaps, `coverage_scope` and `ledger_mirror_verified`;
  - what the result depends on: `ledger_read_started_at`, `ledger_order_ids`, `ledger_sources` and `max_source_age_seconds`;
  - an `evidence` object: pages, cutoffs, retries, duplicates and the sources checked.

  Re-runs add rows, and the latest check per day wins. A collector exception still writes an `incomplete` row (`venue_collector_error`), so an older match never stands unchallenged.
- **Staleness.** `venue_reconciliation_days` marks a check stale when a row it depended on is written at or after `ledger_read_started_at`. That includes changes between the read and the row being stored, corrections to compared orders logged outside the window, and deletes. The day is then no longer verified until the check is re-run.
- **Bounded settings.** `LEDGER_RECON_FINALITY_SECONDS` must be finite and ≥ 300 (the recording lag), and `LEDGER_RECON_MAX_SOURCE_AGE_SECONDS` finite and > 0; otherwise the script exits 2. The collector also refuses non-finite or negative backoff and non-positive retry, page and cutoff counts. A fill `ts` outside the datetime range is `venue_record_time_invalid`, not a crash.
- **Writer change.** The MM pilot now records `fill_qty` (contracts) on each fill row, so its orders can be compared. Older pilot rows have no quantity and reconcile as incomplete.
- **Live inventory rebalances** (`inventory_balancer`) record a `filled` row with `fill_qty`, and move inventory, only when the venue confirms the fill:
  - Kalshi: status `executed` with a valid `fill_count_fp`/`fill_count`.
  - Polymarket: status `matched` with a valid `takingAmount` (shares received on a BUY).
  - The quantity must be finite, positive and at most the order size, and it is kept exact, fractions included.
  - A canceled, unmatched, resting, delayed or ambiguous response writes no fill row and moves no inventory. The requested size is never assumed filled. A real fill on such an order then shows as `missing_in_ledger` rather than being guessed.

Run it as its own process with the existing credentials. It prints JSON and writes only with `--write`:

```sh
python scripts/reconcile_venue_fills.py --print-key-fingerprint   # value for LEDGER_KALSHI_SCOPE
python scripts/reconcile_venue_fills.py --venue kalshi --day 2026-09-28 [--write]
```

**Known limits:**
- Only order-level quantities are compared; the local ledger has no venue fill ids.
- An order that fills across a day boundary reconciles per day only when each fill has its own timestamped ledger row. A ledger row recorded inside the boundary ambiguity window leaves the day incomplete (`ledger_boundary_ambiguous`).
- Fees are summed from the venue side only.
- Settlement and position reconciliation do not exist yet.

## Reporter access

- **The `ledger_reporter` role** is `NOLOGIN`. It has `USAGE` on the `ledger_reporting` schema and `SELECT` on its views only, with no base-table, write or function access.
- **The base tables** have RLS enabled with no policies, and `anon` and `authenticated` get no grants.
- **The views** run with the owner's rights, so reporters need no table privileges.
- **The schema is not exposed** through the API. Don't add `ledger_reporting` to PostgREST's exposed schemas.

## Verification (local)

```sh
pytest tests/test_ledger_sync.py tests/test_venue_reconciliation.py -v
pytest tests/test_mm_pilot.py -k ledger_mirror -v
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
