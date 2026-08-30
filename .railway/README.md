# Railway configuration

This project defines its Railway infrastructure in code.

```txt
.railway/railway.ts
```

Use this file to describe the Railway project you want: services, databases, buckets, custom domains, replicas, groups, and environment variables.

The TypeScript file imports `railway/iac`. Install the pinned SDK into the isolated `.railway` package (Node.js 22 or newer):

```bash
npm ci --prefix .railway
```

Install Railway CLI 5.45.7 or newer separately from Railway's official distribution, then verify it before planning:

```bash
railway --version
```

## Common commands

Create the configuration files:

```bash
railway config init
```

Import an existing Railway project into code:

```bash
railway config pull
```

Preview what Railway would change:

```bash
railway config plan
```

Apply the planned changes:

```bash
railway config apply
```

## Repository safety gate

This is a named `arb-scanner` partial restricted to the audited Polymarket production project and environment. It deliberately excludes `polymarket-egress-proxy`: the live proxy has no recorded source, and Railway CLI 5.45.7 plus SDK 3.11.0 cannot round-trip its TCP proxy networking without planning a deletion.

The checked-in root `railway.toml` remains the authoritative service manifest. `.railway/railway.ts` fails closed while any root `railway.json` or `railway.toml` remains, so `railway config plan` and `railway config apply` cannot create a second configuration source. Migrate the legacy manifest in a separate reviewed change, verify the resulting diff and redacted plan, and remove the legacy manifest in that same migration before using this IaC definition.

Merging this file does not apply Railway changes. Do not run `railway config migrate --apply` or `railway config apply` without the repository-required action-time approval for the exact redacted plan and a fresh dry-run/quiescence readback.

## Notes

- `railway config plan` is read-only, but the repository guard refuses it until the legacy root manifest has been migrated and removed.
- `railway config apply` previews changes and asks before applying unless you pass `--yes`.
- Destructive changes in non-interactive or agent sessions require `railway config apply --confirm-destructive` after reviewing the plan.
- A future protected deployment workflow may pin a plan (`railway config plan --out railway-plan.json`) and apply only that reviewed plan after an explicit environment approval. Do not auto-apply on merge. On GitHub Actions, use https://github.com/railwayapp/config.
- Services already managed by `railway.json` or `railway.toml` must be migrated before `.railway/railway.ts` can manage them.
- Keep one `.railway` file for the whole project. A named `export const partial` (or `PARTIAL` / `const Partial`) is a last resort for separate repos that cannot share that file. Do not add it unless omit=delete across repos is a blocker.
- Use `replicas` for scaling; advanced placement can still specify region names.
- Use `group("Name", [resources])` to keep large projects organized on the Railway canvas.
- Secrets imported from Railway are rendered as `preserve()` so existing values are retained without writing secret values to source. Use `railway config pull --omit-preserved-variables` for a smaller import. `railway config pull --include-variables` decrypts and inlines non-sealed values (including secrets that were never sealed).
- `railway config migrate` finds every `railway.json` / `railway.toml` in the repository and writes them into this one file.
