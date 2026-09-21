# Polymarket Rewards Read-Only Digest

Generated: 2026-08-04T20:52:29+00:00
Sources: official Polymarket US incentive docs plus public market-data gateway when reachable.

Safety boundary: read-only discovery, ranking, docs verification, and approval-ticket generation only. No Polymarket credentials, authenticated API calls, account actions, deposits, referrals, orders, or trades.

## Data Inputs

- Incentive overview: https://docs.polymarket.us/incentives/overview
- Liquidity incentives: https://docs.polymarket.us/incentives/liquidity
- Volume incentives: https://docs.polymarket.us/incentives/volume
- Market maker program: https://docs.polymarket.us/incentives/market-maker
- Public markets API docs: https://docs.polymarket.us/api-reference/market/overview
- Public API base: `https://gateway.polymarket.us`

## Public Gateway Status

- Gateway fetch failed from this environment: `URLError: <urlopen error [SSL: TLSV1_ALERT_ACCESS_DENIED] tlsv1 alert access denied (_ssl.c:1082)>`
- Digest continues from official docs and records the gateway failure as a blocker.

## Top Manual-Review Candidates

| Rank | Program / market | Score | Reward type | Action | Capital clue | Source | Why manual-gated |
| ---: | --- | ---: | --- | --- | --- | --- | --- |
| 1 | Liquidity Incentive Program | 69 | liquidity_pool | order_place | 0 | [source](https://docs.polymarket.us/incentives/liquidity) | Requires live resting orders; fills create inventory and adverse-selection risk. Public gateway access may be restricted from t... |
| 2 | Volume Incentive Program | 42 | volume_pool | trade | 500 | [source](https://docs.polymarket.us/incentives/volume) | Volume rewards require filled trades and can incentivize uneconomic volume. Minimum notional and price-band terms apply. |
| 3 | Market Maker Program | 38 | market_maker_program | order_place | 0 | [source](https://docs.polymarket.us/incentives/market-maker) | Requires application/approval and likely contractual obligations. |

## Current Thesis

Polymarket belongs in this rewards monitor as a public-source intelligence feed and manual ticket generator. It should not be promoted to live execution unless legal/TOS, jurisdiction, account eligibility, and execution access are separately proven and approved.
