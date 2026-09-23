---
project_type: dev-only
status: active
last_updated: "2026-08-02"
linear_workspace: https://linear.app/johnsnow
linear_project: Polymarket Arb Scanner
portfolio_linear_project: Financial Markets — Algorithmic Capture
---

# State — Polymarket Arb Scanner

## Current state

- Canonical repository and service name: `polymarket-arb-scanner`; internal product name: `arbgrid`.
- This repository owns prediction-market detection and execution code. Portfolio capital, tax, secrets, and cross-engine P&L remain owned by `/Users/jonathontamm/Financial Markets with AI`.
- The current checkout is `fix/layer4-continuous-wiring` at `a9bbb62`. It matches its tracking branch, is 4 commits ahead and 47 commits behind the locally known `origin/master` (`c248180`), and contains uncommitted and untracked user work that must be preserved.
- The July 17 audit is historical evidence, not current-head status. Its Kalshi WebSocket C-1 landed on `origin/master` in PR #98. Current production state was not re-verified in this documentation pass; keep `DRY_RUN=true` unless the separate go-live gate is explicitly satisfied.
- Tasks and ownership live in the [johnsnow Linear workspace](https://linear.app/johnsnow): engine work in **Polymarket Arb Scanner** and portfolio milestones in **Financial Markets — Algorithmic Capture**.

## Single next action

From a clean worktree based on current `origin/master`, execute the checkout-hygiene and salvage decision already tracked in [JOH-251](https://linear.app/johnsnow/issue/JOH-251/local-checkout-hygiene-leave-stale-branch-commit-or-discard-untracked) and [JOH-398](https://linear.app/johnsnow/issue/JOH-398/decide-salvage-of-fixlayer4-continuous-wiring-remnants-firecrawl-news), preserving all user work and without deploying or changing `DRY_RUN`.
