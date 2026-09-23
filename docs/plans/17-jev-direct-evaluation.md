# Jev direct API and evidence-first evaluation

Approved scope (2026-09-20): use TypeSafe direct access, repair evaluation, and
prioritize semantic news interpretation and contract equivalence. This branch
implements that scope without enabling trading or changing deployed secrets.

## Work

- Replace the OpenRouter transport with the official TypeSafe endpoint and pinned
  model. Accept a server-side environment key or an explicitly configured private
  file; never log keys or provider response bodies. Validate typed answers.
- Make missing settlement rules an explicit review outcome. Keep dates, arithmetic,
  action selection, and authority in code. Benchmark semantics on labeled fixtures.
- Preserve exact contract rules, timestamps, both executable asks, model and request
  provenance in research records. Crypto predictions remain experimental and paper-only.
- Report paper returns only from complete quotes and explicit cost assumptions;
  distinguish contracts from dollars, reject invalid records, and deduplicate markets.
- Add an opt-in, bounded direct-API benchmark with immutable input hashes, error and
  coverage reporting, latency, usage, and baseline comparison. It cannot submit orders.

## Verification and graduation

Run regression tests, critical Ruff checks, and the full Python 3.12 suite. Live API
validation sends synthetic public examples only and writes local research artifacts.
The initial fixture benchmark is a smoke test, not independent proof of accuracy or
profitability. Freeze prompts and gather prospective unseen examples next; report
class-level errors, false equivalence, coverage, cost and review burden. Forecast
evaluation requires independent settled markets, event-grouped chronological splits,
market/statistical baselines, uncertainty, and realistic fills/costs. The existing
300-settled-market gate is a minimum, not proof or automatic capital authority.

Production activation, repository merge (which auto-deploys), and any capital use
are separate decisions after review. Broad commercial product work is not included.
