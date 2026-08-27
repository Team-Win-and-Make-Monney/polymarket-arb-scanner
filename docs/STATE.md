# Current Project State

Last verified: 2026-08-27.

## Portfolio decision

Broad multi-venue expansion is parked. The retained mission is a narrow, observable dry-run research system plus a separately gated Kalshi D0 operating path. Railway still runs the scanner and an older egress-proxy service; stopping either service is an external account action and is not implied by this repository state.

## Canonical code line

- Remote default branch inspected: `origin/master` at `13072f2885bbd4351907b6dbe1d037bf0665c57a` (merged PR #134).
- Completion branch and draft PR: `codex/project-completion-20260825` / PR #135.
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

Railway readback on 2026-08-27 reports scanner deployment `18443059-5c2d-4e98-ab16-4d19b7592249` and legacy egress-proxy deployment `c24e057a-817f-472d-a7c1-a352658fa6e7`, both with status `SUCCESS`. The scanner remains at merged commit `13072f2`; no deployment was changed during completion work.

## Completion branch scope

The completion branch reconciles the remainder of PR #130's 125-finding report and a fresh CodeRabbit review. It closes residual concurrency, hedge-inventory, broker-ledger, SSRF, path-validation, executable-price, fee-verification, resource-lifecycle, and test-quality gaps. It also adds an atomic Supabase broker-event migration. See `docs/AUDIT-RECONCILIATION-2026-08-26.md`.

The current completion diff passes 3,646 tests with 39 skipped in both exact-head CI and a clean local Python 3.12 virtual environment installed from `requirements.txt` plus `requirements-dev.txt`. The standalone eight-platform fee verifier, bytecode compilation, targeted Ruff correctness checks, and `git diff --check` also pass. CodeRabbit's authenticated functional-remediation review completed with zero findings; its maintenance review identified one stale audit-status sentence, which was corrected by explicitly separating the historical finding from its verified current disposition. Codex Security scan `dff40a30-2741-4409-b269-94733848489c` reported the low-severity inverted-side mapping defect, follow-up scan `4068112a-cdf5-42ec-a71f-2c92e30f8b6d` found zero reportable issues in its published remediation, and maintenance-range scan `52bea6b9-1925-40af-b6db-1e5522c12478` found zero reportable issues at head `5183c0c7`. These are branch checks, not evidence that the branch has been merged or deployed.

## Remaining external decisions

- Review and merge the completion branch after CI and independent-review evidence is available.
- Decide whether to stop the active Railway dry-run service; stopping it changes external account state.
- Decide whether the still-running `polymarket-egress-proxy` has any retained purpose.
- PRs #130 and #132 are closed as superseded; their still-valid work is preserved by PRs #134 and #135.
- Linear cleanup is complete: all 29 previously noncompleted issues were revalidated and moved to Done or Canceled; the project remains Paused.
- Start any new paper window only after deployment selection and measurement parameters are approved.
