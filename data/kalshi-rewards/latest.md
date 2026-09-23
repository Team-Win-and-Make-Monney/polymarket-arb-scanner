# Kalshi Rewards Read-Only Digest

Generated: 2026-08-05T04:27:53.003248+00:00
Official source reviewed: 2026-08-05T04:27:53.003233+00:00
Source: Kalshi public `/trade-api/v2/incentive_programs?status=active&type=all`.

Safety boundary: this digest is read-only. It does not use Kalshi credentials, place orders, cancel orders, copy referral links, or complete account actions.

## How These Rewards Work

- Liquidity rewards are market-making contests. You earn only by providing qualifying resting liquidity during the listed window; the pool is split by Kalshi's program rules, so the displayed reward is not guaranteed.
- Liquidity rewards do not require your order to fill, but you must place real resting orders. If another trader takes your order, you now have a live position and inventory risk.
- Volume rewards are different: they require actual eligible trades. This digest requests all active incentive types and labels each group below.
- `target_size_fp` is the posted-liquidity size target shown by the API. A 1000 target is much harder for a small account than a 300 target.
- Competition matters. The public API gives reward size, target size, and timing, but not the UI's exact Competition label, so the competition column below is a proxy.
- Manual gate before any trade: open the incentive row, confirm exact terms, verify the order book, define max inventory loss, then place only orders you personally approve.

Active incentive rows by type: liquidity=4170.

## Best Manual-Review Candidates

| Rank | Reward pool | Score | Competition proxy | Category | Total | Markets | Avg/market | Target | Time left | Required action | Why |
| ---: | --- | ---: | --- | --- | ---: | ---: | ---: | ---: | ---: | --- | --- |
| 1 | `KXMAMDANIEO-26AUG08` | 94.0 | High bounty | other | $1,000 | 1 | $1,000 | 300 | 4.4d | Post qualifying resting limit orders during the active window; score depends on size, uptime, and distance from best bid/ask. | small target, low complexity |
| 2 | `KXEOWEEK-26JUL25` | 74.7 | Medium | macro/markets | $333 | 1 | $333 | 300 | 3.4d | Post qualifying resting limit orders during the active window; score depends on size, uptime, and distance from best bid/ask. | small target, researchable, low complexity |
| 3 | `KXEOWEEK-26AUG08` | 68.7 | Medium | macro/markets | $1,000 | 3 | $333 | 300 | 17.4d | Post qualifying resting limit orders during the active window; score depends on size, uptime, and distance from best bid/ask. | small target, longer runway, researchable, low complexity |
| 4 | `KXEOWEEK-26AUG01` | 68.7 | Medium | macro/markets | $1,000 | 3 | $333 | 300 | 10.4d | Post qualifying resting limit orders during the active window; score depends on size, uptime, and distance from best bid/ask. | small target, longer runway, researchable, low complexity |
| 5 | `KXTRUMPENDORSEMENTS-26AUG07` | 62.0 | Medium-Low | politics/policy | $1,000 | 7 | $143 | 300 | 3.4d | Post qualifying resting limit orders during the active window; score depends on size, uptime, and distance from best bid/ask. | small target |
| 6 | `KXSENATEMND-26` | 61.5 | Medium-High | politics/policy | $1,000 | 2 | $500 | 1000 | 7.0d | Post qualifying resting limit orders during the active window; score depends on size, uptime, and distance from best bid/ask. | large target, low complexity |
| 7 | `KXTRUMPACT-26AUG02` | 60.5 | Medium-Low | politics/policy | $1,000 | 10 | $100 | 300 | 4.4d | Post qualifying resting limit orders during the active window; score depends on size, uptime, and distance from best bid/ask. | small target |
| 8 | `KXAPRPOTUS-26AUG07` | 60.4 | Medium-Low | other | $1,000 | 8 | $125 | 300 | 2.4d | Post qualifying resting limit orders during the active window; score depends on size, uptime, and distance from best bid/ask. | small target |
| 9 | `KXTRUTHSOCIAL-26AUG08` | 59.5 | Medium-Low | other | $1,000 | 10 | $100 | 300 | 4.4d | Post qualifying resting limit orders during the active window; score depends on size, uptime, and distance from best bid/ask. | small target |
| 10 | `KXPOKEMON-26AUG151ULTCO` | 55.1 | Medium-High | other | $345 | 1 | $345 | 1000 | 4.0d | Post qualifying resting limit orders during the active window; score depends on size, uptime, and distance from best bid/ask. | large target, low complexity |
| 11 | `KXPOKEMON-26AUGASCHEREL` | 55.1 | Medium-High | other | $345 | 1 | $345 | 1000 | 4.0d | Post qualifying resting limit orders during the active window; score depends on size, uptime, and distance from best bid/ask. | large target, low complexity |
| 12 | `KXPOKEMON-26AUGASCHERPO` | 55.1 | Medium-High | other | $345 | 1 | $345 | 1000 | 4.0d | Post qualifying resting limit orders during the active window; score depends on size, uptime, and distance from best bid/ask. | large target, low complexity |

## Largest Reward Pools

| Rank | Reward pool | Score | Competition proxy | Category | Total | Markets | Avg/market | Target | Time left | Required action | Why |
| ---: | --- | ---: | --- | --- | ---: | ---: | ---: | ---: | ---: | --- | --- |
| 1 | `KXTRUMPSAY-26AUG10` | 38.5 | High | politics/policy | $3,500 | 35 | $100 | 1000 | 5.4d | Post qualifying resting limit orders during the active window; score depends on size, uptime, and distance from best bid/ask. | large target, many markets |
| 2 | `KXTRUMPMENTION-26AUG04` | 32.5 | High | politics/policy | $3,500 | 35 | $100 | 1000 | 14.4d | Post qualifying resting limit orders during the active window; score depends on size, uptime, and distance from best bid/ask. | large target, longer runway, many markets |
| 3 | `KXTRUMPMENTION-26AUG05` | 32.5 | High | politics/policy | $3,300 | 33 | $100 | 1000 | 15.4d | Post qualifying resting limit orders during the active window; score depends on size, uptime, and distance from best bid/ask. | large target, longer runway, many markets |
| 4 | `KXTRUMPSAYMONTH-26SEP01` | 32.5 | High | politics/policy | $3,100 | 31 | $100 | 1000 | 10.0d | Post qualifying resting limit orders during the active window; score depends on size, uptime, and distance from best bid/ask. | large target, longer runway, many markets |
| 5 | `KXTRUMPSAYCOMPANY-26SEP01` | 32.5 | High | politics/policy | $2,900 | 29 | $100 | 1000 | 10.0d | Post qualifying resting limit orders during the active window; score depends on size, uptime, and distance from best bid/ask. | large target, longer runway, many markets |
| 6 | `KXDIESELW-26AUG09` | 41.2 | High | other | $2,520 | 21 | $120 | 1000 | 4.0d | Post qualifying resting limit orders during the active window; score depends on size, uptime, and distance from best bid/ask. | large target, many markets |
| 7 | `KXTRUMPMENTIONB-26AUG04` | 35.5 | High | politics/policy | $2,500 | 25 | $100 | 1000 | 21.4d | Post qualifying resting limit orders during the active window; score depends on size, uptime, and distance from best bid/ask. | large target, longer runway, many markets |
| 8 | `KXEURUSDAW-26AUG07` | 41.2 | High | other | $2,400 | 20 | $120 | 1000 | 2.7d | Post qualifying resting limit orders during the active window; score depends on size, uptime, and distance from best bid/ask. | large target, many markets |
| 9 | `KXBOND-30` | 40.5 | High | other | $2,400 | 24 | $100 | 1000 | 4.0d | Post qualifying resting limit orders during the active window; score depends on size, uptime, and distance from best bid/ask. | large target, many markets |
| 10 | `KXHOODA-28JANFUNDED` | 34.5 | High | other | $2,100 | 21 | $100 | 1000 | 10.0d | Post qualifying resting limit orders during the active window; score depends on size, uptime, and distance from best bid/ask. | large target, longer runway, many markets |
| 11 | `KXDIESELD-26AUG05` | 23.5 | High | other | $2,100 | 21 | $100 | 1000 | 1.5h | Post qualifying resting limit orders during the active window; score depends on size, uptime, and distance from best bid/ask. | large target, near deadline, many markets |
| 12 | `KXVOTEPRIMARY-SENATEMID26AELSAELS` | 40.8 | High | politics/policy | $2,000 | 25 | $80.00 | 1000 | 4.0d | Post qualifying resting limit orders during the active window; score depends on size, uptime, and distance from best bid/ask. | large target, many markets |

## Small-Target Watchlist

| Rank | Reward pool | Score | Competition proxy | Category | Total | Markets | Avg/market | Target | Time left | Required action | Why |
| ---: | --- | ---: | --- | --- | ---: | ---: | ---: | ---: | ---: | --- | --- |
| 1 | `KXMAMDANIEO-26AUG08` | 94.0 | High bounty | other | $1,000 | 1 | $1,000 | 300 | 4.4d | Post qualifying resting limit orders during the active window; score depends on size, uptime, and distance from best bid/ask. | small target, low complexity |
| 2 | `KXEOWEEK-26JUL25` | 74.7 | Medium | macro/markets | $333 | 1 | $333 | 300 | 3.4d | Post qualifying resting limit orders during the active window; score depends on size, uptime, and distance from best bid/ask. | small target, researchable, low complexity |
| 3 | `KXEOWEEK-26AUG08` | 68.7 | Medium | macro/markets | $1,000 | 3 | $333 | 300 | 17.4d | Post qualifying resting limit orders during the active window; score depends on size, uptime, and distance from best bid/ask. | small target, longer runway, researchable, low complexity |
| 4 | `KXEOWEEK-26AUG01` | 68.7 | Medium | macro/markets | $1,000 | 3 | $333 | 300 | 10.4d | Post qualifying resting limit orders during the active window; score depends on size, uptime, and distance from best bid/ask. | small target, longer runway, researchable, low complexity |
| 5 | `KXTRUMPENDORSEMENTS-26AUG07` | 62.0 | Medium-Low | politics/policy | $1,000 | 7 | $143 | 300 | 3.4d | Post qualifying resting limit orders during the active window; score depends on size, uptime, and distance from best bid/ask. | small target |
| 6 | `KXTRUMPACT-26AUG02` | 60.5 | Medium-Low | politics/policy | $1,000 | 10 | $100 | 300 | 4.4d | Post qualifying resting limit orders during the active window; score depends on size, uptime, and distance from best bid/ask. | small target |
| 7 | `KXAPRPOTUS-26AUG07` | 60.4 | Medium-Low | other | $1,000 | 8 | $125 | 300 | 2.4d | Post qualifying resting limit orders during the active window; score depends on size, uptime, and distance from best bid/ask. | small target |
| 8 | `KXTRUTHSOCIAL-26AUG08` | 59.5 | Medium-Low | other | $1,000 | 10 | $100 | 300 | 4.4d | Post qualifying resting limit orders during the active window; score depends on size, uptime, and distance from best bid/ask. | small target |
| 9 | `KXGOLD15M-26AUG050030` | 42.7 | Medium | other | $20.00 | 1 | $20.00 | 300 | 0.0h | Post qualifying resting limit orders during the active window; score depends on size, uptime, and distance from best bid/ask. | small target, near deadline, low complexity |
| 10 | `KXSILVER15M-26AUG050030` | 42.7 | Medium | other | $20.00 | 1 | $20.00 | 300 | 0.0h | Post qualifying resting limit orders during the active window; score depends on size, uptime, and distance from best bid/ask. | small target, near deadline, low complexity |

## Current Thesis

For this account, the best automation target is discovery, ranking, and alerts. The reward pools are not claim buttons; they require live liquidity provision and can lose money through adverse selection, fills, wide markets, and stale quotes.

Near-term review priority: small-target, low-complexity rows first; researchable macro/company rows second; large entertainment or sports pools last unless the UI shows unusually low competition and the order book is calm.
