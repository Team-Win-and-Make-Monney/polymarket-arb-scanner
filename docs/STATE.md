# Current Project State

Last verified: 2026-08-24.

## Portfolio decision

Broad multi-venue expansion is parked. The retained mission is a narrow, observable dry-run research system plus a separately gated Kalshi D0 operating path. Railway is still running a dry-run worker; stopping or changing that service is an external account action and is not implied by this repository state.

## Canonical code line

- Remote default branch inspected: `origin/master` at `edf66fa93accdabdbcd9b1070a00284415b82312`.
- Recovery branch: `codex/project-recovery-20260824`.
- Recovery worktree: `/Users/jonathontamm/Dev/pm-arb-recovery-20260824`.
- Original dirty checkouts and worktrees are preserved. None have been reset, pruned, or deleted.
- The uncommitted multi-venue controller work in `/Users/jonathontamm/Dev/polymarket-arb-scanner` is deliberately parked and excluded from this recovery branch. Its tracked and untracked files remain intact for a future, separately scoped review.
- Configured remote is the verified organization repository `https://github.com/Team-Win-and-Make-Monney/polymarket-arb-scanner.git`. Its protected `master` matched the recovery base at `edf66fa93accdabdbcd9b1070a00284415b82312` before the URL was changed.

## Operating boundaries

- `DRY_RUN=true` remains the default.
- Only Kalshi D0 has a verified live launcher: `scripts/launch-kalshi-d0-live.sh`.
- `DRY_RUN=false` requires `artifacts/live-envelope.json` with `venue`, `pair`, `max_notional_usd`, `max_daily_loss_usd`, and `kill_switch` from one operator message.
- Account/credential readiness, current jurisdiction and product eligibility, limits, emergency stop, and action-time approval remain mandatory.
- Adapter presence, user-reported access, or a passing test does not establish live authority.

## Recovery scope

The recovery branch incorporates PR #132's Kalshi submission-policy hardening and repairs the current high-severity correctness findings: fail-open book revalidation, reversed expected-fee weights, one-legged logical-arb routing, time-decay side/gain errors, unsafe Supabase endpoints, non-finite executor timeouts, incorrect analytics, duplicate treasury execution, reward-estimate scaling, invalid NegRisk asks, and degraded gas/price authorization.

The Docker entrypoint is narrowed from broad continuous mode to `--mode kalshi`. This removes the duplicated multi-venue work behind the observed 291-second Railway cycle from the unattended path; broad mode remains available only when explicitly selected.

## Remaining external decisions

- Review and merge the recovery branch after CI evidence is available.
- Decide whether to stop the active Railway dry-run service; stopping it changes external account state.
- Revalidate and close/update stale Linear backlog items against the merged commit; issue mutations are external writes.
- Start any new paper window only after deployment selection and measurement parameters are approved.
