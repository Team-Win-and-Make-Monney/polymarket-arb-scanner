# Audit Reconciliation — 2026-08-26

## Scope

This register reconciles PR #130's 125 confirmed findings against merged PR #134 and `codex/project-completion-20260825`. It is a disposition record, not live-trading authority.

## Disposition

| Group | Count | Disposition |
|---|---:|---|
| High | 13 | Remediated in PR #134 or the completion branch; duplicate Sharpe finding counted once in implementation but retained in the report count. |
| Medium | 52 | Remediated in PR #134 or the completion branch, except M-42: Polygonscan's provider contract requires the API key query parameter, so the client retains it while preventing error-body leakage and adding typed rate-limit/error handling. Duplicate findings retain one implementation. |
| Low | 60 | Remediated in the completion branch, including correctness, test-quality, and low-severity security items. |

Total: 125/125 findings have a current disposition; none remain silently unclassified.

## Material completion changes

- Every scanned execution candidate requires fresh, finite, executable prices and depth for each required leg; unavailable CLOB data fails closed.
- Kalshi partial-fill and inventory hedges carry an authoritative maximum contract count so a reducing order cannot flip the position.
- Balance-cache invalidation uses a generation guard, and mutable calibration/latency/singleton state is synchronized.
- Broker outcome persistence failures halt all automatic processing; Supabase terminal events use an advisory-lock RPC for atomic write-once semantics.
- Supabase, exchange, proxy, dashboard, webhook, EDGAR, and deployment-check URLs enforce their intended public/TLS boundary.
- Dashboard passwordless access requires both a loopback peer and a loopback-bound listener.
- Shell-backed broker adapters, unsafe shebang indirection, unconstrained state paths, and unused SX Bet private-key loading are blocked.
- Fee, analytics, depth, ROI, price, correlation, sentiment, reward, and treasury calculations were reconciled with executable behavior.
- Kalshi fee assumptions were refreshed against the official schedule effective July 7, 2026: one-contract 10-cent and 85-cent taker fees are one cent, and legacy index prefixes no longer receive an unpublished half-rate default.
- Vacuous, contradictory, leaking, order-dependent, and path-dependent tests were repaired and new regression tests cover the completion fixes.

## Independent review

CodeRabbit CLI reviewed the full uncommitted completion diff in three completed passes and raised 20 issues total: 15 in the initial pass, 3 in the first post-fix pass, and 2 in the second post-fix pass. Each was verified against current code and all valid issues were corrected. An authenticated follow-up review on 2026-08-27 completed across the six changed source/test files with zero findings.

The legacy Codex Security Deep Scan retains scan ID `487c03f8-f9cc-481d-ae82-cc0f102dffc9` and remains blocked at its original host preflight. A fresh exact-range Codex Security diff scan, `dff40a30-2741-4409-b269-94733848489c`, completed against PR #135 head `811e79a1`. It reviewed all 65 executable files in scope and reported one low-severity execution-plan finding: inverted one-shot cross matches did not preserve the physical Kalshi-side mapping. The current diff persists `_pair_inverted`, always replaces action-time `_cross_legs`, and adds deterministic regression coverage. The finding is not represented as closed until the remediation is published and verified at the new PR head.

## Local verification

- `python -m pytest tests -q --tb=short`: 3,646 passed, 39 skipped.
- `python tests/integration/verify_fees.py`: all 24 cases across eight platforms passed.
- `python -m compileall -q .`: passed.
- `ruff check --select E9,F63,F7,F82 .`: passed.
- `git diff --check`: passed.

## Accepted non-fixes

- Polygonscan/Etherscan-compatible APIs require `apikey` in the query contract; M-42 is provider-required rather than locally removable.
- Betfair's normalized NO-side value for laying a YES outcome is `1 - 1 / decimal_odds`; the questioned conversion is economically correct.
- API-outage opportunity economics are expressed per contract, so per-contract `total_cost` is intentional.

## Remaining gates

- The Codex Security remediation must be published and completion-branch CI must be green at the resulting exact PR head.
- PRs #130 and #132 are closed as superseded; PR #135 remains draft and unmerged.
- Merging to `master` triggers Railway deployment and therefore requires separate action-time approval.
- Live Kalshi D0 still requires one operator message containing all five envelope fields plus credential, eligibility, limit, emergency-stop, and per-action approval checks.
