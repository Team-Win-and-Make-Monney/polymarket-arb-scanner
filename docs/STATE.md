# Current Project State

Last verified: 2026-08-26.

## Portfolio decision

Broad multi-venue expansion is parked. The retained mission is a narrow, observable dry-run research system plus a separately gated Kalshi D0 operating path. Railway still runs the scanner and an older egress-proxy service; stopping either service is an external account action and is not implied by this repository state.

## Canonical code line

- Remote default branch inspected: `origin/master` at `13072f2885bbd4351907b6dbe1d037bf0665c57a` (merged PR #134).
- Completion branch: `codex/project-completion-20260825`.
- Completion worktree: `/Users/jonathontamm/Dev/pm-arb-completion-20260825`.
- Original dirty checkouts and worktrees are preserved. None have been reset, pruned, or deleted.
- The uncommitted multi-venue controller work in `/Users/jonathontamm/Dev/polymarket-arb-scanner` is deliberately parked and excluded from this recovery branch. Its tracked and untracked files remain intact for a future, separately scoped review.
- Configured remote is the verified organization repository `https://github.com/Team-Win-and-Make-Monney/polymarket-arb-scanner.git`.

## Operating boundaries

- `DRY_RUN=true` remains the default.
- Only Kalshi D0 has a verified live launcher: `scripts/launch-kalshi-d0-live.sh`.
- `DRY_RUN=false` requires `artifacts/live-envelope.json` with `venue`, `pair`, `max_notional_usd`, `max_daily_loss_usd`, and `kill_switch` from one operator message.
- Account/credential readiness, current jurisdiction and product eligibility, limits, emergency stop, and action-time approval remain mandatory.
- Adapter presence, user-reported access, or a passing test does not establish live authority.

## Merged recovery state

PR #134 incorporates the useful Kalshi submission-policy hardening from PR #132 and repairs the primary high-severity correctness findings: fail-open book revalidation, reversed expected-fee weights, one-legged logical-arb routing, time-decay side/gain errors, unsafe Supabase endpoints, non-finite executor timeouts, incorrect analytics, duplicate treasury execution, reward-estimate scaling, invalid NegRisk asks, and degraded gas/price authorization.

The Docker entrypoint is narrowed from broad continuous mode to `--mode kalshi`. This removes the duplicated multi-venue work behind the observed 291-second Railway cycle from the unattended path; broad mode remains available only when explicitly selected.

Railway currently reports deployment `18443059-5c2d-4e98-ab16-4d19b7592249` at exact commit `13072f2`, status `SUCCESS`, one running scanner replica, Dockerfile build, `/healthz`, and a ready `/data` volume. A live request to `/healthz` returned `{"status": "ok"}`. The deployment was not changed during completion work.

## Completion branch scope

The completion branch reconciles the remainder of PR #130's 125-finding report and a fresh CodeRabbit review. It closes residual concurrency, hedge-inventory, broker-ledger, SSRF, path-validation, executable-price, fee-verification, resource-lifecycle, and test-quality gaps. It also adds an atomic Supabase broker-event migration. See `docs/AUDIT-RECONCILIATION-2026-08-26.md`.

The current completion diff passes 3,643 tests with 39 skipped, the standalone eight-platform fee verifier, bytecode compilation, targeted Ruff correctness checks, and `git diff --check`. These are local branch checks, not evidence that the branch has been merged or deployed.

## Remaining external decisions

- Review and merge the completion branch after CI and independent-review evidence is available.
- Decide whether to stop the active Railway dry-run service; stopping it changes external account state.
- Decide whether the still-running `polymarket-egress-proxy` has any retained purpose.
- Close superseded PRs #130 and #132 after the completion PR preserves their still-valid work.
- Revalidate and close/update stale Linear and GitHub backlog items against the merged commit.
- Start any new paper window only after deployment selection and measurement parameters are approved.
