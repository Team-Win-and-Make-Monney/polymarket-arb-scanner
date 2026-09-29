# Adopt reproducible research and security checks

This PR adds offline research evidence validation, dependency review, CodeQL for
Python/JavaScript/Actions and a synthetic evidence artifact. It preserves the existing
Depot test job, scheduled scans, notifications, runtime configuration and deployment triggers.
Actions are pinned to commit SHAs and new checkouts do not retain credentials.

The research workflow uses the Python standard library, no venue client imports and
no secrets. PRs can upload a clearly synthetic report. Only a push to `master` can
run the separate attestation job, which grants OIDC and attestation-write permissions
only to that job. Verify an artifact with:

```sh
gh attestation verify fixture-validation.json --repo tamm-labs/polymarket-arb-scanner
```

A signed fixture proves build provenance, not source truth or financial performance.
The real-data protocol is [PAPER-PROTOCOL.md](PAPER-PROTOCOL.md).

## Account setup and deployment gate

The repository is public under the separate `tamm-labs` Team organization. Public
repositories can use CodeQL, dependency review, secret scanning, push protection and
public artifact attestations without migration into an Enterprise organization.
Enterprise central policies do not automatically cover this repository. Moving it
would require a separate review of ownership, integrations, URLs and deployment effects.

At the 2026-09-29 audit, repository secret scanning/push protection were disabled;
CodeQL default setup was not configured. The new advanced CodeQL workflow is an
alternative to default setup, so do not enable both. Enabling secret scanning and
push protection is a separately reviewable account-settings change. Dependabot
security updates were already enabled; the default workflow token was read-only
and PR approval by Actions was disabled.

After the new checks have passed consistently on this PR, consider requiring them
in branch protection with the exact observed check names. Preserve existing `test`
and `CodeRabbit` requirements and administrator enforcement. Do not replace the
existing ruleset or bypass it to merge. SHA enforcement is useful after verifying
that every existing workflow already satisfies it.

Repository instructions state that pushing to `master` triggers Railway deployment.
This PR must remain unmerged until the operator approves the exact deployment effect.
No attempt was made to manufacture a safe merge by changing trading state.

## Implementation evidence (retrieved 2026-09-29)

| Component | Official source | Consequence |
|---|---|---|
| CodeQL setup | https://docs.github.com/en/code-security/code-scanning/enabling-code-scanning/configuring-default-setup-for-code-scanning | Public repositories qualify; advanced and default setup must not conflict. |
| Market data | https://docs.kalshi.com/getting_started/quick_start_market_data.md | Public REST data can be obtained without execution credentials. This PR makes no market-data requests. |
| Orderbook | https://docs.kalshi.com/api-reference/market/get-market-orderbook | YES/NO bid books imply opposite-side asks; fixed-point prices and quantities must be parsed deliberately. |
| Fee contract | https://docs.kalshi.com/api-reference/events/get-event-fee-changes.md | Event overrides may replace series fees; capture effective times rather than applying a universal fee. |
| Fee rounding | https://docs.kalshi.com/getting_started/fee_rounding.md | Member precision and accumulated rounding affect total fees; a quote alone cannot establish the applicable net fee. |

The standalone evaluator introduces no dependency. Existing runtime dependencies
and their upgrade cadence are unchanged. Existing private logs and operational
account information are not included in this public PR.
