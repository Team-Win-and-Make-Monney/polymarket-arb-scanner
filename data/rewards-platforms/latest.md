# Market Rewards Platform Catalog

Generated: 2026-08-05T04:08:25+00:00
Official sources reviewed: 2026-08-05
Safety boundary: Read-only discovery, ranking, monitoring, and approval-ticket generation only. No unattended trading, staking, lending, borrowing, bridging, wallet signing, order placement, claiming, referral spam, account creation, or KYC/account actions.

## Thesis

The best automation target is not autonomous capture. It is autonomous discovery, source verification, reward-density scoring, risk math, and approval-ready tickets. The programs that actually pay meaningful rewards almost always require one of four things: live orders, filled trades, wallet transactions, or account-level opt-ins. Those are financial actions and stay behind a manual gate.

Highest-value safe lanes: Kalshi public incentive monitoring, Polymarket/Polymarket US reward schedule monitoring, Merkl campaign discovery, and broker stock-lending/cash-rate watchlists. The lowest-value lanes for this account are institutional exchange market-maker programs and Sybil-heavy airdrop/task farming.

## Highest Safe Automation Value

| Rank | Platform | Program | Score | Category | Capital | Risk | Safe automation | Required work |
| ---: | --- | --- | ---: | --- | --- | --- | --- | --- |
| 1 | Merkl | Live DeFi Campaigns | 73 | defi_incentive_aggregator | low | high | monitor_rank_and_manual_wallet_ticket | Participate in eligible DeFi campaigns such as concentrated liquidity, lending/borrowing, token holding, airdrops, or points programs. |
| 2 | Polymarket | Maker Rebates | 67 | prediction_market | medium | high | monitor_and_paper_score | Place maker orders that add liquidity and later fill on markets with fees enabled. |
| 3 | Polymarket | Global Liquidity Rewards | 67 | prediction_market | medium | high | monitor_and_paper_score | Post resting limit orders on reward-enabled markets. Score depends on two-sided depth, spread, size, and proximity to the adjusted midpoint. |
| 4 | Kalshi | Liquidity Incentive Program | 67 | prediction_market | medium | high | monitor_and_approval_ticket | Eligible members post qualifying resting limit orders. Random snapshots score order size, uptime, and proximity to the best bid/ask; pools are split pro rata. |
| 5 | Robinhood | High-Yield Cash Program | 66 | investment_broker | low | low | rate_watch_and_manual_ticket | Hold eligible settled cash and meet program requirements, including Robinhood Gold where applicable. |
| 6 | Robinhood | Stock Lending | 58 | investment_broker | low | medium | manual_account_setting_ticket | Enable Stock Lending if eligible; Robinhood may borrow eligible whole fully paid shares. |
| 7 | Kalshi | Volume Incentive Program | 56 | prediction_market | medium | high | monitor_and_manual_review | Execute eligible trades in eligible markets during active windows. Reward share is based on eligible volume, with trades generally needing prices above $0.03 and below... |
| 8 | Interactive Brokers | Stock Yield Enhancement Program | 52 | investment_broker | medium | medium | manual_account_setting_ticket | Enroll eligible fully paid or excess-margin shares so IBKR can lend them when borrow demand exists. |
| 9 | Polymarket US | Liquidity Incentive Program | 49 | prediction_market | high | high | monitor_and_approval_ticket | Place resting orders close to the best price. Random order-book snapshots score size by tick distance and target-size eligibility. |
| 10 | Polymarket | Taker Rebates | 49 | prediction_market | high | high | monitor_and_manual_review | Generate eligible weighted taker volume by category and tier while avoiding abusive or wash activity. |
| 11 | Aerodrome | LP Emissions and veAERO Incentives | 42 | defi_protocol | medium | high | watchlist_only | Provide liquidity to pools, or lock/vote veAERO to direct emissions and receive pool fees/incentives. |
| 12 | Aave | Liquidity Incentives, Safety Module, and Merit | 42 | defi_protocol | medium | high | watchlist_only | Supply, borrow, stake, or perform Aave-aligned actions eligible for DAO or external incentives. |

## Platform Catalog

| Platform | Program | Reward type | Source | Competition | Reward timing | Can Codex capture? | Why not autonomous | Sources |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| Merkl | Live DeFi Campaigns | lp_lending_airdrop_points | primary_verified | Varies by campaign; APRs decay as capital enters. | Rewards are computed periodically and become claimable after Merkle-root updates and dispute windows. | No | Capturing requires wallet signatures and onchain LP, lending, borrowing, holding, or claim transactions. | [source](https://docs.merkl.xyz/merkl-mechanisms/incentive-mechanisms)<br>[source](https://merkl.xyz/chain-wide-incentives)<br>[source](https://app.merkl.xyz/) |
| Polymarket | Maker Rebates | maker_rebate | primary_verified | Medium-high; depends on category fee rates and fill quality. | Paid daily in pUSD when accrued rebate is at least $1. | No | Maker rebates require live filled orders and position risk. | [source](https://docs.polymarket.com/market-makers/maker-rebates) |
| Polymarket | Global Liquidity Rewards | liquidity_pool | primary_verified | High in liquid events; lower in niche markets but with worse adverse-selection risk. | Daily distribution at midnight UTC; minimum payout disclosed as $1. | No | Requires live CLOB orders, possible fills, wallet/account access, and geographic eligibility checks. | [source](https://docs.polymarket.com/market-makers/liquidity-rewards) |
| Kalshi | Liquidity Incentive Program | liquidity_pool | primary_verified | High in large pools, medium in smaller target-size markets. | Daily program pools; minimum payout disclosed as $1. | No | Capturing requires live resting orders that can fill and create regulated-market inventory risk. | [source](https://help.kalshi.com/en/articles/13823851-liquidity-incentive-program)<br>[source](https://external-api.kalshi.com/trade-api/v2/incentive_programs) |
| Robinhood | High-Yield Cash Program | cash_sweep_interest | primary_verified | No pool competition. | Monthly interest; APY changes over time. | No | Requires account/subscription and cash allocation decisions. | [source](https://robinhood.com/us/en/support/articles/cash-program-interest-rate/) |
| Robinhood | Stock Lending | securities_lending_income | primary_verified | Demand-driven rather than pool competition. | Monthly payment when borrowed shares generate at least one cent of monthly rebate. | No | Requires account setting changes and acceptance of share-lending risks. | [source](https://robinhood.com/us/en/support/articles/stock-lending/) |
| Kalshi | Volume Incentive Program | volume_pool | primary_verified | High because volume programs favor active traders with lower friction and hedging. | Program-specific; capped by Kalshi rules including a max reward per contract. | No | Requires filled trades, immediate exposure, and venue rule compliance. | [source](https://help.kalshi.com/en/articles/13823850-what-is-the-kalshi-volume-incentive-program) |
| Interactive Brokers | Stock Yield Enhancement Program | securities_lending_income | primary_verified | Demand-driven rather than pool competition. | Daily accrual when shares are on loan; IBKR says it pays 50% of a market-based rate. | No | Requires brokerage account enrollment and accepts SIPC, voting, dividend-tax, and borrower-default tradeoffs. | [source](https://www.interactivebrokers.com/en/pricing/stock-yield-enhancement-program.php) |
| Polymarket US | Liquidity Incentive Program | liquidity_pool | primary_verified | High in major sports pools; medium in smaller categories. | Calculated after time periods; rewards under $1 are not paid. | No | Requires live orders and venue eligibility; reward pools do not remove inventory loss risk. | [source](https://docs.polymarket.us/incentives/liquidity) |
| Polymarket | Taker Rebates | taker_rebate | primary_verified | High; favors traders with real flow and a reason to cross spreads. | Daily pUSD rebates and tier updates. | No | Requires intentional taker trading and could incentivize uneconomic volume. | [source](https://docs.polymarket.com/trading/taker-rebates) |
| Aerodrome | LP Emissions and veAERO Incentives | lp_emissions_voting_incentives | primary_verified | High; Base-native yield farmers and vote markets make rewards efficient quickly. | Weekly epoch mechanics. | No | LP and ve-locking actions create impermanent loss, lockup, and smart-contract risk. | [source](https://aerodrome.finance/docs)<br>[source](https://aerodrome.finance/documents/AERO/legal-disclosures.pdf) |
| Aave | Liquidity Incentives, Safety Module, and Merit | supply_borrow_staking_airdrop | primary_verified | Medium; rewards are usually arbitraged down by professional DeFi capital. | Continuous or periodic depending on reserve, controller, and Merit campaign. | No | Requires onchain transactions, smart-contract risk, liquidation risk, and governance-specific terms. | [source](https://aave.com/docs/aave-v3/concepts/incentives) |
| Kalshi | Designated Liquidity Provider Program | market_maker_agreement | primary_verified | Institutional. | Program-specific and agreement-specific. | No | Requires account review, agreement, capital, and professional market-making operations. | [source](https://help.kalshi.com/en/articles/15410219-liquidity-provider-program)<br>[source](https://help.kalshi.com/en/articles/13823819-how-to-become-a-market-maker-on-kalshi) |
| dYdX | Rewards Directory and Surge | trading_competition_fee_rebate_referral | primary_verified | Very high; leaderboard and taker-volume rewards favor sophisticated traders. | Seasonal campaigns and monthly/weekly competitions. | No | dYdX states it is not available in the U.S. or to restricted persons, and capture requires leveraged trading. | [source](https://www.dydx.xyz/rewards)<br>[source](https://www.dydx.xyz/blog/dydx-surge) |
| Hyperliquid | Maker Rebates, Fee Tiers, Staking Discounts, Referrals | maker_rebate_fee_discount_referral | primary_verified | Very high; maker rebate thresholds start at meaningful share of venue maker volume. | Maker rebates are paid continuously on each trade; volume tiers are assessed daily UTC. | No | Requires active leveraged trading, wallet/account actions, and jurisdiction checks; app shows restricted-jurisdiction gating. | [source](https://hyperliquid.gitbook.io/hyperliquid-docs/trading/fees)<br>[source](https://app.hyperliquid.xyz/trade) |
| Kraken | Market Participation Program and Market Maker Incentives | institutional_fee_rebate_equity_linked_incentive | primary_verified | Institutional. | Weekly calculation/allocation and end-of-program distribution for MPP. | No | Requires institutional eligibility and live spot/futures market participation. | [source](https://www.kraken.com/institutions/market-participation-program)<br>[source](https://www.kraken.com/institutions/market-makers) |
| Coinbase International Exchange | Liquidity Program | institutional_liquidity_rebate | primary_verified | Institutional. | Monthly evaluation; rebates reflected on fills. | No | Eligibility is jurisdiction/account dependent and capture requires active exchange trading. | [source](https://help.coinbase.com/en/international-exchange/trading-deposits-withdrawals/international-exchange-liquidity-program) |
| Coinbase Exchange | Liquidity Program | large_liquidity_provider_discount | primary_verified | Institutional. | Program-specific. | No | Requires exchange account eligibility, high volume, and live trading. | [source](https://www.coinbase.com/exchange/liquidity-program)<br>[source](https://www.coinbase.com/developer-platform/products/exchange-api) |
| Binance.US | Market Maker Program | market_maker_rebate | primary_verified | Institutional or professional. | Ongoing fee/rebate benefits for top participants. | No | Requires approval and professional market-making scale. | [source](https://support.binance.us/en/articles/9842933-what-is-the-binance-us-market-maker-program) |
| Binance | Fiat Liquidity Provider Promotion | institutional_maker_rebate | primary_verified | Institutional. | Weekly review and hourly rebate updates during the promotion window. | No | Requires exchange approval, major trading volume, and live market making. | [source](https://www.binance.com/en/square/post/326807011037522) |
| Airdrop and Task Platforms | Grass, Teneo, Monad, Backpack, MetaMask, Base, Yieldbay, Silencio watchlist | points_airdrop_task_bounty | secondary_needs_verification | Very high and Sybil-heavy. | Speculative or campaign-specific. | No | High scam/Sybil/compliance risk and often requires wallets, extensions, social accounts, or fake activity. | Needs primary-source verification |

## Category Counts

- crypto_airdrop_tasks: 1
- crypto_exchange: 5
- crypto_perp_dex: 2
- defi_incentive_aggregator: 1
- defi_protocol: 2
- investment_broker: 3
- prediction_market: 7

## Automation Buildout

1. Discovery monitor: refresh this catalog and the Kalshi public rewards digest on a schedule.
2. Source verifier: flag programs with `secondary_needs_verification` until primary official docs are found.
3. Reward scorer: estimate reward density, capital requirement, fees, gas, slippage, borrow/liquidation risk, and eligibility.
4. Approval ticket: produce a human-review checklist with max loss, expected reward, required action, and exact source links.
5. Execution gate: no live orders, account changes, wallet signatures, claims, or trades unless you explicitly do them yourself.
