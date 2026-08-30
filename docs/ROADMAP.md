# Roadmap

> **Owner:** Jonathon Tamm · **Review cadence:** monthly.
> Sequencing view. The "done" bar lives in `PRD.md`; per-strategy status in `docs/strategy-framework-v2.md`.

## Now (completion; broad expansion paused)
- Land and independently review `codex/project-completion-20260825` after PR #134's merged recovery baseline.
- Preserve complete executable-book, balance, inventory, audit-ledger, and control-plane fail-closed guarantees.
- Keep the clean Python 3.12 environment and full-suite result reproducible on the proposed commit.
- Keep Railway dry-run only while its cost and research value are explicitly evaluated.
- Close superseded PRs #130/#132 and reconcile Linear/GitHub against current code; stale backlog text is not an implementation requirement.

## Next (only after completion merges)
- Run a deduplicated dry-run window on a narrow universe, recording executable depth, dollar profit, slippage, latency, feed uptime, cycle duration, and revalidation failure rate.
- Decide whether Railway should remain as a narrow Kalshi research worker or be stopped.
- Revisit slow scan stages only from fresh timing evidence; optional strategies must not blind feed-health monitoring.

## Later / parked
- Broad venue expansion, SX Bet signing, and new live routers.
- Auto-rebalance and other custody-changing automation.
- Automatic tuning changes in production.
- These items require a new portfolio decision and their own safety/eligibility review.

## Completion acceptance milestone
1. Full tests and correctness lint pass from a clean Python 3.12 environment.
2. No candidate reaches execution without fresh, valid, executable prices for every leg.
3. Dry-run evidence reports depth, dollar edge, latency, slippage assumptions, feed uptime, and rejection reasons without duplicates.
4. Any live Kalshi D0 decision is a separate operator action with the five-field envelope and all authority gates.
