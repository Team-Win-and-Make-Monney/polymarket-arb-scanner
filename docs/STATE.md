# Current Project State

Last verified: 2026-08-29.

## Portfolio decision

Broad multi-venue expansion is parked. The retained mission is a narrow, observable dry-run research system plus a separately gated Kalshi D0 operating path. Railway still runs the scanner and an older egress-proxy service; stopping either service is an external account action and is not implied by this repository state.

## Canonical code line

- Remote default branch inspected: `origin/master` at `9ea7fe8ab2d49d289ee1d11401e045924d3078d1` (PR #134 plus the repository-policy update in PR #136).
- Completion branch and draft PR: `codex/project-completion-20260825` / PR #135.
- Final-review worktree: `/Users/jonathontamm/Dev/polymarket-arb-scanner/.worktrees/pr135-final-review`.
- Dirty and active checkouts remain preserved; local checkout lifecycle is outside this PR's deployment contents.
- The uncommitted multi-venue controller work in `/Users/jonathontamm/Dev/polymarket-arb-scanner` is deliberately parked and excluded from this recovery branch. Its tracked and untracked files remain intact for a future, separately scoped review.
- Configured remote is the verified organization repository `https://github.com/Team-Win-and-Make-Monney/polymarket-arb-scanner.git`.

## Operating boundaries

- `DRY_RUN=true` remains the default.
- Only Kalshi D0 has a verified live launcher: `scripts/launch-kalshi-d0-live.sh`.
- `DRY_RUN=false` requires `artifacts/live-envelope.json` with `venue`, `pair`, `max_notional_usd`, `max_daily_loss_usd`, and `kill_switch` from one operator message.
- Configuration permits `DRY_RUN=false` only for the Kalshi MM pilot; generic executor submission is disabled. The envelope clamps per-market gross, per-market inventory, aggregate inventory, daily loss, and canary loss.
- Dashboard state-changing requests require authenticated JSON and reject explicit cross-site origins. The MM pilot consumes the same pause state before authorization and quote placement.
- Account/credential readiness, current jurisdiction and product eligibility, limits, emergency stop, and action-time approval remain mandatory.
- Adapter presence, user-reported access, or a passing test does not establish live authority.

## Merged recovery state

PR #134 incorporates the useful Kalshi submission-policy hardening from PR #132 and repairs the primary high-severity correctness findings: fail-open book revalidation, reversed expected-fee weights, one-legged logical-arb routing, time-decay side/gain errors, unsafe Supabase endpoints, non-finite executor timeouts, incorrect analytics, duplicate treasury execution, reward-estimate scaling, invalid NegRisk asks, and degraded gas/price authorization.

The Docker entrypoint is narrowed from broad continuous mode to `--mode kalshi`. This removes the duplicated multi-venue work behind the observed 291-second Railway cycle from the unattended path; broad mode remains available only when explicitly selected.

Railway readback on 2026-08-27 reports scanner deployment `18443059-5c2d-4e98-ab16-4d19b7592249` and legacy egress-proxy deployment `c24e057a-817f-472d-a7c1-a352658fa6e7`, both with status `SUCCESS`. The scanner remains at merged commit `13072f2`; no deployment was changed during completion work.

## Completion branch scope

The completion branch reconciles the remainder of PR #130's 125-finding report and closes residual concurrency, hedge-inventory, broker-ledger, SSRF, path-validation, executable-price, fee-verification, resource-lifecycle, and test-quality gaps. It also adds an atomic Supabase broker-event migration. See `docs/AUDIT-RECONCILIATION-2026-08-26.md`.

A fresh full-repository Codex Security scan (`f1944337-acfd-4909-b211-b17f259530dd`) validated 14 findings at the pre-remediation PR head: 9 high, 4 medium, and 1 low. The current branch remediates those findings by fail-closing generic live execution and unknown balances, persisting fresh Kalshi revalidation prices, enforcing matched contract counts, aggregate quote notional, always-on daily loss, dashboard pause/CSRF controls, indeterminate-order handling, non-finite configuration rejection, authenticated SX proxying, immutable workflow action pins, and a non-root primary container. Committed-diff scan `4188e60c-4f3f-47e5-8d13-e6500db3c703` then found one high-severity UTC-rollover defect in the new daily-loss baseline; the follow-up fixes establish the baseline before the first new-day fill and explicitly remove the SX service token before upstream proxying. Local Python 3.12 verification now passes 3,666 tests with 39 skipped; bytecode compilation, targeted Ruff correctness checks, 24 fee cases, workflow YAML parsing, dependency consistency, both container builds, and a current installed-environment vulnerability audit also pass. One earlier full-suite run under load reproduced a pre-existing timing assertion at 0.276 seconds versus a 0.270-second threshold; its isolated rerun passed in 0.26 seconds, and two subsequent complete runs passed. Hosted CI and CodeRabbit remain the independent review boundary. These branch checks are not evidence that the branch has been merged or deployed.

## Remaining external decisions

- Review and merge the completion branch after CI and independent-review evidence is available. Because merge triggers Railway deployment, merge requires a controlled deployment decision.
- Configure a strong `SXBET_PROXY_TOKEN` on any retained SX proxy deployment before deploying this branch; the hardened proxy intentionally fails startup without it.
- Decide whether to stop the active Railway dry-run service; stopping it changes external account state.
- Decide whether the still-running `polymarket-egress-proxy` has any retained purpose.
- PRs #130 and #132 are closed as superseded; their still-valid work is preserved by PRs #134 and #135.
- Linear cleanup is complete: all 29 previously noncompleted issues were revalidated and moved to Done or Canceled; the project remains Paused.
- Start any new paper window only after deployment selection and measurement parameters are approved.
