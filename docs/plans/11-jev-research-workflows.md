# JEV research workflows — implementation contract

Date: 2026-09-20. Scope: the eight financial-research use cases in the portfolio
review. These are local, advisory research workflows over supplied snapshots.

## Behavior

`python -m research_jev` exposes filing/news classification, novelty screening,
source-to-market relevance, settlement-language comparison, a watchlist research
queue, incentive changes, transcript analysis, and paper-only feature recording
with an offline forecast evaluator. Every workflow validates identity, source
availability, byte budgets, and required context before invoking the shared
`client.evaluate(state, questions)` interface. Rubrics and complete questions are
versioned and hashed. Sources are retained with exact text and hashes.

The package has no imports from scanner, executor, broker, provider clients,
notification modules, or production configuration. Default mode is off. Shadow
results preserve incumbent ordering and candidates. Advisory mode changes only
the local research output. Missing or malformed judgments preserve candidates;
no positive judgment approves market equivalence, account eligibility, or a
trade. No forecast is inferred from JEV's textual judgment probabilities.

## Reuse and boundaries

The older `feat/jev-signal-relevance-gate` worktree separates same-event and
same-direction judgments; the discovery experiment is a remove-only screen.
Preserve those semantics in snapshot adapters and test the incumbent baseline.
Do not transplant their uncommitted changes or change the active OpenRouter
client, production signal aggregation, discovery caches, settlement veto, or
feature flags. PR #142 already owns Firecrawl resolution-source ingestion; this
work consumes snapshots and does not duplicate ingestion.

## Verification

- Semantic workflow behavior with injected responses; no provider calls.
- Exact decimal/date/entity mismatch checks, missing evidence and uncertain
  judgments, off/shadow behavior, and outage candidate preservation.
- Source publication/availability/capture time checks before inference.
- Paper feature records exclude outcomes; evaluation rejects future sources,
  post-outcome forecasts, overlapping event groups, and invalid probabilities.
- Python 3.12, correctness lint, relevant tests, full repository pytest gate,
  and actual CLI runs on clearly synthetic fixtures.

## Activation evidence still required

Real, approved timestamped source snapshots; prediction-blind labels grouped by
event and time; baseline comparisons and missed-event checks; task-specific
threshold calibration; measured usage and end-to-end latency. Hypothetical
examples and mocked tests establish behavior, not research or trading value.
Production ingestion, scheduling, notifications, settings, trading execution,
and capital remain outside this implementation.
