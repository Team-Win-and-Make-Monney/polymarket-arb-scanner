# HyperLiquid Builder Routing Integration Plan

Date: 2026-07-06

This plan documents how `polymarket-arb-scanner` should consume the local HyperLiquid builder-code tooling if HyperLiquid becomes part of the cross-venue incentive router.

## Source Of Truth

- Builder integration kit: `/Users/jonathontamm/.codex/reports/hyperliquid-incentives/builder-integration-kit-latest.md`
- Builder routing config: `/Users/jonathontamm/.codex/reports/hyperliquid-incentives/builder-routing-config-latest.json`
- Operator dashboard: `/Users/jonathontamm/.codex/reports/hyperliquid-incentives/operator-dashboard-latest.md`
- Approval queue: `/Users/jonathontamm/.codex/reports/hyperliquid-incentives/approval-queue-latest.md`

## Adapter Boundary

Do not wire HyperLiquid live execution directly into existing scanner flows until the account-side blockers are cleared and the exact execution gate is reviewed. The first adapter should be dry-run only and should produce review packets that contain:

- coin
- side
- size
- limit price
- post-only time-in-force
- builder address
- builder fee in tenths of basis points
- cancel-path plan
- expected fees, slippage, and incentive/reward rationale

## Current Defaults

- Builder address: `0x0CAC28016e5B64DB1A981D56AF67f6b07552733E`
- Default builder fee: `10` tenths of a basis point
- Default network for order tests: `testnet`
- Required confirmation phrase for any write: `I_APPROVE_THIS_HYPERLIQUID_WRITE`

## Implementation Sequence

1. Add a dry-run HyperLiquid incentive adapter that reads the builder integration kit JSON.
2. Emit normalized opportunity/review packets, not orders.
3. Add tests that prove the adapter refuses execution unless the packet has an explicit approval state.
4. Only after builder-fee approval, sufficient funding, and cancel-path verification, consider a tiny testnet order path.
5. Keep live mainnet execution disabled until separately approved with an exact payload.

## Safety

This repo can support cross-venue incentive research and dry-run packet generation. It must not approve builder fees, place/cancel HyperLiquid orders, revoke wallets, deposit, withdraw, bridge, swap, claim rewards, or sign wallet/account actions without a separate explicit user approval.
