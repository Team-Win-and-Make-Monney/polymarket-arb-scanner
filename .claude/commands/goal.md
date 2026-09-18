---
description: Advance arbgrid toward the Go-Live Gate — one verified backlog item per cycle
---

# Goal: advance arbgrid toward the Go-Live Gate

You are continuing a long-running effort on arbgrid (this repo). Work in small verified
cycles. One item per invocation, done properly, beats three items rushed.

## Orient (always do this first)

1. Read `CLAUDE.md` → Current Status, and the project memory (auto-loaded).
2. Fetch the live project board — the shared state for this effort:
   `https://claude.ai/code/artifact/b995c054-d42a-41d1-a170-eeaead4a4443`
   (WebFetch it; the Remaining Work tab is the ranked backlog, the Go-Live Gate tab is
   the definition of done for the whole project.)
3. Check production reality before trusting any narrative: Railway service
   `polymarket-arb-scanner` (`railway logs`, and `/status` on
   `arb-scanner-production.up.railway.app` with Basic auth `DASHBOARD_USER` +
   `DASHBOARD_PASS` from `railway variables`). Confirm ALL safety controls are
   unchanged, not just one: `DRY_RUN=true`, `ENABLED_EXECUTION_PLATFORMS=kalshi`,
   `MAX_TRADE_SIZE=10`, and `DAILY_LOSS_LIMIT` / `MAX_OPEN_POSITIONS` at their
   recorded values. If ANY of these has drifted, stop — report the drift to
   Jonathon via AskUserQuestion before selecting or doing any work.

## Execute (one cycle)

4. Take the **top unblocked item** from the board's Remaining Work tab. If the top item
   is "collect paper data" and the collection window hasn't elapsed, take the next
   build item instead (execution-HIGH triage, scan performance, PR #62, etc.).
5. Work it end-to-end on a fresh worktree branched from `origin/master` (the local
   checkout may sit on an old branch — never build on it without checking).
   TDD for any code change: failing test first, then the fix, then the full suite.
   Known baseline: ~50 `test_dashboard*` failures are pre-existing in the local env —
   compare against pristine master before attributing failures to your change.
6. Verify against reality, not just tests — live API spot-checks, Railway logs after
   deploy. A detection claim is unverified until checked against the actual platform.
7. Ship per repo policy: feature branch → PR → auto-merge on green CI + CodeRabbit.
   Address CodeRabbit findings (fix valid ones, rebut invalid ones with evidence).
   Watch until merged and, if it changes runtime behavior, verify the Railway deploy.

## Close the loop

8. Refresh the project board artifact (update Status, Remaining Work, timeline, and
   Go-Live Gate tabs with what actually happened) — republish to the SAME URL above
   by passing it as `url` to the Artifact tool.
9. Update `CLAUDE.md` Current Status if it has drifted, and project memory if you
   learned something non-obvious.
10. Report: what shipped, what was verified (with output), what's now top of the
    backlog, and the current honest read on the edge question.

## Hard rules — never cross without asking Jonathon explicitly

- **Never set `DRY_RUN=false`**, enable a new execution platform, raise
  `MAX_TRADE_SIZE`, or change any risk limit. These require an explicit human
  decision via AskUserQuestion every single time.
- **Never touch money paths live**: no withdrawals, transfers, order placement, or
  anything that moves funds — including "just to test".
- Production env/config changes beyond read-only inspection: ask first. The only
  exception is reverting a non-safety value to its documented code default when it
  is demonstrably wrong — and this exception NEVER applies to `DRY_RUN`,
  `ENABLED_EXECUTION_PLATFORMS`, `MAX_TRADE_SIZE`, or any risk limit: those
  require explicit human approval every time, in every direction.
- Don't generate new audit-report PRs; fix or triage existing findings instead.
- If the paper record shows the Layer-1 edge is dead (a full week of honest zero),
  don't keep grinding arb fixes — surface the Layer-3 (rewards MM) pivot decision
  to Jonathon with the evidence.
