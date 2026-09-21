# Railway infrastructure migration

This configuration preserves the live environment inventory imported on 21 September 2026. Secrets remain in Railway through `preserve()`. The entrypoint refuses unknown project/environment names. Verify the linked project ID and environment ID before every command; names are the authoring guard, not a substitute for account identity.

Prerequisites: Node.js 22 or newer, Railway CLI 5.58.0, and the pinned Railway SDK. The SDK does not install the CLI. Run commands from the repository root:

```sh
npm ci --prefix .railway
railway status
railway config plan --json
```

`railway config init` and `railway config pull` also run from the repository root. Pull into a separate scratch directory when comparing remote state; do not overwrite the environment router. For programmatic subprocess callers, remove an inherited `_` executable hint before invoking Railway: SDK 3.11.0 otherwise may inspect the caller's version instead of Railway's.

No automatic apply runs on merge. Plan each listed environment separately, review the exact changes, and preserve the plan outside `.railway/` with `railway config plan --out <private-plan-file>`. Only an operator-approved exact plan may be applied with `railway config apply --plan <private-plan-file> --yes`. Any destruction requires separate explicit approval and `--confirm-destructive`. Saved plans can contain sensitive values and must not be committed.

## Scope and migration gates

- `polymarket-arb-scanner` / `production`: `environments/arb-production.ts`.

Railway imports describe dashboard state; they can omit settings currently supplied by legacy repository manifests. Effective legacy build, start, health check, and restart settings have been carried into the affected service definitions. The legacy root manifest is removed only in this proposed branch. Before deployment, use the supported migration workflow to clear any explicit remote Config File setting, then regenerate the plan and verify all health/build settings. Do not apply a partial inventory or deploy a branch with a missing legacy manifest before that transition is ready.

Service sources, current provider controls, queue ownership, public TCP endpoints, and secret values are preserved. Source changes are outside this migration. There is no new EDI production worker. Rank staging has its isolated Postgres and fixture-only dispatch; production Redis retains its pinned image and authenticated ACL startup. The arb proxy remains until its external consumer inventory is known.

Template databases have provider-managed volume relationships that the CLI currently omits from the imported graph. Verify their live attachments and a backup before apply; a no-change import alone does not prove volume recovery or full resource ownership.
