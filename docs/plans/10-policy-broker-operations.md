# 10 — Policy Broker Operations

**Status:** BUILD + PR only. This runtime is not deployed and grants no live authority by itself.

## Process boundary

Deploy the broker as a separate Railway worker using `Dockerfile.broker` and
`railway.broker.toml`. Do not change the scanner service entrypoint. Mount the
authority snapshot and executor config read-only from paths outside this repo;
the worker rejects in-repo files, symlinks, relative executables, and executables
stored in this checkout.

Required environment:

- `BROKER_QUEUE_BACKEND=supabase` (production) or `sqlite` (local validation)
- `SUPABASE_URL` and `SUPABASE_SERVICE_ROLE_KEY` for the production queue
- `BROKER_POLICY_PATH` — out-of-repo policy JSON
- `BROKER_AUTHORITY_SNAPSHOT_PATH` — independent observer snapshot, read-only
- `BROKER_EXECUTOR_CONFIG_PATH` — out-of-repo command map, read-only
- `BROKER_WEBHOOK_URL` — operator escalation destination
- `BROKER_LEASE_TTL_SECONDS=60`, `BROKER_POLL_SECONDS=5`, `BROKER_BATCH_SIZE=25`

The executor config contains one entry for each intent type:

```json
{
  "flip_lane": {"argv": ["/opt/policy-broker/bin/flip-lane"], "timeout_seconds": 30},
  "move_capital": {"argv": ["/opt/policy-broker/bin/move-capital"], "timeout_seconds": 60},
  "rotate_secret": {"argv": ["/opt/policy-broker/bin/rotate-secret"], "timeout_seconds": 60}
}
```

Commands receive strict intent JSON on stdin and must return
`{"verified": true|false, "detail": "..."}`. Exit code `42` means a 2FA/KYC
wall and becomes a hard stop. The broker never invokes a shell and never retries
an `IN_DOUBT` result.

## Reconciliation and lease behavior

Before every batch the worker independently compares ledger and venue balances.
A confirmed break records the durable all-capital halt. Unreadable evidence
fails closed and escalates. The worker renews its single-writer lease at one-third
of the TTL and stops before the next intent if renewal is not acknowledged.
Before processing, it atomically appends a durable one-attempt claim. A pending
intent that was already claimed is marked `IN_DOUBT` and escalated, never
automatically retried. If a terminal outcome cannot be persisted after an
action, the worker stops immediately; the next worker converts that claimed,
still-pending row to `IN_DOUBT` without repeating the action.

## Safe verification

```bash
BROKER_QUEUE_BACKEND=sqlite \
BROKER_SQLITE_PATH=/tmp/policy-broker.db \
BROKER_POLICY_PATH=/absolute/outside/repo/policy.json \
BROKER_AUTHORITY_SNAPSHOT_PATH=/absolute/outside/repo/authority.json \
BROKER_EXECUTOR_CONFIG_PATH=/absolute/outside/repo/executors.json \
python -m broker.worker --once
```

This command is only safe when the three out-of-repo executor commands are
non-consequential test doubles. Applying migration `0005_policy_broker.sql`,
deploying the worker, granting service-role credentials, or replacing test
executors with account-side commands remains operator-gated.
