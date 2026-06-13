#!/usr/bin/env python3
"""Read-only rewards platform catalog.

This script turns primary-source rewards research into a local catalog. It does
not read credentials, connect to wallets, place trades, sign transactions,
claim rewards, or touch account state.
"""

import argparse
import csv
import datetime as dt
import json
import sys
from pathlib import Path

DEFAULT_OUTPUT = Path("data/rewards-platforms/latest.md")
DEFAULT_CSV_OUTPUT = Path("data/rewards-platforms/latest.csv")
DEFAULT_JSON_OUTPUT = Path("data/rewards-platforms/latest.json")

SAFETY_BOUNDARY = (
    "Read-only discovery, ranking, monitoring, and approval-ticket generation only. "
    "No unattended trading, staking, lending, borrowing, bridging, wallet signing, order placement, "
    "claiming, referral spam, account creation, or KYC/account actions."
)


def _utc_now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def platform_catalog() -> list[dict]:
    """Return the researched rewards platform catalog."""
    return [
        {
            "platform": "Kalshi",
            "program": "Liquidity Incentive Program",
            "category": "prediction_market",
            "reward_type": "liquidity_pool",
            "source_status": "primary_verified",
            "official_urls": [
                "https://help.kalshi.com/en/articles/13823851-liquidity-incentive-program",
                "https://external-api.kalshi.com/trade-api/v2/incentive_programs",
            ],
            "required_work": (
                "Eligible members post qualifying resting limit orders. Random snapshots score order size, "
                "uptime, and proximity to the best bid/ask; pools are split pro rata."
            ),
            "reward_timing": "Daily program pools; minimum payout disclosed as $1.",
            "competition": "High in large pools, medium in smaller target-size markets.",
            "capital_intensity": "medium",
            "monitorability": "public_api",
            "execution_risk": "high",
            "automation_mode": "monitor_and_approval_ticket",
            "can_codex_capture": "No",
            "why_not_autonomous": "Capturing requires live resting orders that can fill and create regulated-market inventory risk.",
            "safe_next_action": "Keep the existing Kalshi public API monitor, rank pools, and draft manual order tickets.",
        },
        {
            "platform": "Kalshi",
            "program": "Volume Incentive Program",
            "category": "prediction_market",
            "reward_type": "volume_pool",
            "source_status": "primary_verified",
            "official_urls": [
                "https://help.kalshi.com/en/articles/13823850-what-is-the-kalshi-volume-incentive-program",
            ],
            "required_work": (
                "Execute eligible trades in eligible markets during active windows. Reward share is based on "
                "eligible volume, with trades generally needing prices above $0.03 and below $0.97."
            ),
            "reward_timing": "Program-specific; capped by Kalshi rules including a max reward per contract.",
            "competition": "High because volume programs favor active traders with lower friction and hedging.",
            "capital_intensity": "medium",
            "monitorability": "public_docs",
            "execution_risk": "high",
            "automation_mode": "monitor_and_manual_review",
            "can_codex_capture": "No",
            "why_not_autonomous": "Requires filled trades, immediate exposure, and venue rule compliance.",
            "safe_next_action": "Monitor program availability and calculate break-even fee/reward thresholds before any manual trade.",
        },
        {
            "platform": "Kalshi",
            "program": "Designated Liquidity Provider Program",
            "category": "prediction_market",
            "reward_type": "market_maker_agreement",
            "source_status": "primary_verified",
            "official_urls": [
                "https://help.kalshi.com/en/articles/15410219-liquidity-provider-program",
                "https://help.kalshi.com/en/articles/13823819-how-to-become-a-market-maker-on-kalshi",
            ],
            "required_work": "Apply, qualify, sign a market maker agreement, and quote required markets under negotiated terms.",
            "reward_timing": "Program-specific and agreement-specific.",
            "competition": "Institutional.",
            "capital_intensity": "institutional",
            "monitorability": "public_docs",
            "execution_risk": "high",
            "automation_mode": "eligibility_watch_only",
            "can_codex_capture": "No",
            "why_not_autonomous": "Requires account review, agreement, capital, and professional market-making operations.",
            "safe_next_action": "Track eligibility and maintain a future application checklist only.",
        },
        {
            "platform": "Polymarket",
            "program": "Global Liquidity Rewards",
            "category": "prediction_market",
            "reward_type": "liquidity_pool",
            "source_status": "primary_verified",
            "official_urls": [
                "https://docs.polymarket.com/market-makers/liquidity-rewards",
            ],
            "required_work": (
                "Post resting limit orders on reward-enabled markets. Score depends on two-sided depth, "
                "spread, size, and proximity to the adjusted midpoint."
            ),
            "reward_timing": "Daily distribution at midnight UTC; minimum payout disclosed as $1.",
            "competition": "High in liquid events; lower in niche markets but with worse adverse-selection risk.",
            "capital_intensity": "medium",
            "monitorability": "public_api",
            "execution_risk": "high",
            "automation_mode": "monitor_and_paper_score",
            "can_codex_capture": "No",
            "why_not_autonomous": "Requires live CLOB orders, possible fills, wallet/account access, and geographic eligibility checks.",
            "safe_next_action": "Use public market fields to rank reward density and simulate quote profitability before manual review.",
        },
        {
            "platform": "Polymarket US",
            "program": "Liquidity Incentive Program",
            "category": "prediction_market",
            "reward_type": "liquidity_pool",
            "source_status": "primary_verified",
            "official_urls": [
                "https://docs.polymarket.us/incentives/liquidity",
            ],
            "required_work": (
                "Place resting orders close to the best price. Random order-book snapshots score size by tick "
                "distance and target-size eligibility."
            ),
            "reward_timing": "Calculated after time periods; rewards under $1 are not paid.",
            "competition": "High in major sports pools; medium in smaller categories.",
            "capital_intensity": "high",
            "monitorability": "public_docs",
            "execution_risk": "high",
            "automation_mode": "monitor_and_approval_ticket",
            "can_codex_capture": "No",
            "why_not_autonomous": "Requires live orders and venue eligibility; reward pools do not remove inventory loss risk.",
            "safe_next_action": "Create a pool tracker from published schedules, then require manual approval for any quote.",
        },
        {
            "platform": "Polymarket",
            "program": "Maker Rebates",
            "category": "prediction_market",
            "reward_type": "maker_rebate",
            "source_status": "primary_verified",
            "official_urls": [
                "https://docs.polymarket.com/market-makers/maker-rebates",
            ],
            "required_work": "Place maker orders that add liquidity and later fill on markets with fees enabled.",
            "reward_timing": "Paid daily in pUSD when accrued rebate is at least $1.",
            "competition": "Medium-high; depends on category fee rates and fill quality.",
            "capital_intensity": "medium",
            "monitorability": "public_api",
            "execution_risk": "high",
            "automation_mode": "monitor_and_paper_score",
            "can_codex_capture": "No",
            "why_not_autonomous": "Maker rebates require live filled orders and position risk.",
            "safe_next_action": "Estimate rebate minus adverse-selection loss from public market data.",
        },
        {
            "platform": "Polymarket",
            "program": "Taker Rebates",
            "category": "prediction_market",
            "reward_type": "taker_rebate",
            "source_status": "primary_verified",
            "official_urls": [
                "https://docs.polymarket.com/trading/taker-rebates",
            ],
            "required_work": "Generate eligible weighted taker volume by category and tier while avoiding abusive or wash activity.",
            "reward_timing": "Daily pUSD rebates and tier updates.",
            "competition": "High; favors traders with real flow and a reason to cross spreads.",
            "capital_intensity": "high",
            "monitorability": "public_docs",
            "execution_risk": "high",
            "automation_mode": "monitor_and_manual_review",
            "can_codex_capture": "No",
            "why_not_autonomous": "Requires intentional taker trading and could incentivize uneconomic volume.",
            "safe_next_action": "Only evaluate after true expected-value trades are found; never trade just to earn rebates.",
        },
        {
            "platform": "Merkl",
            "program": "Live DeFi Campaigns",
            "category": "defi_incentive_aggregator",
            "reward_type": "lp_lending_airdrop_points",
            "source_status": "primary_verified",
            "official_urls": [
                "https://docs.merkl.xyz/merkl-mechanisms/incentive-mechanisms",
                "https://merkl.xyz/chain-wide-incentives",
                "https://app.merkl.xyz/",
            ],
            "required_work": (
                "Participate in eligible DeFi campaigns such as concentrated liquidity, lending/borrowing, "
                "token holding, airdrops, or points programs."
            ),
            "reward_timing": "Rewards are computed periodically and become claimable after Merkle-root updates and dispute windows.",
            "competition": "Varies by campaign; APRs decay as capital enters.",
            "capital_intensity": "low",
            "monitorability": "public_api",
            "execution_risk": "high",
            "automation_mode": "monitor_rank_and_manual_wallet_ticket",
            "can_codex_capture": "No",
            "why_not_autonomous": "Capturing requires wallet signatures and onchain LP, lending, borrowing, holding, or claim transactions.",
            "safe_next_action": "Build a campaign monitor for APR, TVL, chain, required action, gas, and smart-contract risk.",
        },
        {
            "platform": "Aave",
            "program": "Liquidity Incentives, Safety Module, and Merit",
            "category": "defi_protocol",
            "reward_type": "supply_borrow_staking_airdrop",
            "source_status": "primary_verified",
            "official_urls": [
                "https://aave.com/docs/aave-v3/concepts/incentives",
            ],
            "required_work": "Supply, borrow, stake, or perform Aave-aligned actions eligible for DAO or external incentives.",
            "reward_timing": "Continuous or periodic depending on reserve, controller, and Merit campaign.",
            "competition": "Medium; rewards are usually arbitraged down by professional DeFi capital.",
            "capital_intensity": "medium",
            "monitorability": "public_docs",
            "execution_risk": "high",
            "automation_mode": "watchlist_only",
            "can_codex_capture": "No",
            "why_not_autonomous": "Requires onchain transactions, smart-contract risk, liquidation risk, and governance-specific terms.",
            "safe_next_action": "Monitor via Merkl and official Aave incentive pages, then calculate net APR after borrow, liquidation, and gas risk.",
        },
        {
            "platform": "Aerodrome",
            "program": "LP Emissions and veAERO Incentives",
            "category": "defi_protocol",
            "reward_type": "lp_emissions_voting_incentives",
            "source_status": "primary_verified",
            "official_urls": [
                "https://aerodrome.finance/docs",
                "https://aerodrome.finance/documents/AERO/legal-disclosures.pdf",
            ],
            "required_work": "Provide liquidity to pools, or lock/vote veAERO to direct emissions and receive pool fees/incentives.",
            "reward_timing": "Weekly epoch mechanics.",
            "competition": "High; Base-native yield farmers and vote markets make rewards efficient quickly.",
            "capital_intensity": "medium",
            "monitorability": "public_docs",
            "execution_risk": "high",
            "automation_mode": "watchlist_only",
            "can_codex_capture": "No",
            "why_not_autonomous": "LP and ve-locking actions create impermanent loss, lockup, and smart-contract risk.",
            "safe_next_action": "Track candidate pools only when APR beats impermanent-loss and gas-adjusted thresholds.",
        },
        {
            "platform": "dYdX",
            "program": "Rewards Directory and Surge",
            "category": "crypto_perp_dex",
            "reward_type": "trading_competition_fee_rebate_referral",
            "source_status": "primary_verified",
            "official_urls": [
                "https://www.dydx.xyz/rewards",
                "https://www.dydx.xyz/blog/dydx-surge",
            ],
            "required_work": "Execute eligible taker trades, use eligible interfaces, stake DYDX, trade boosted markets, or join listed campaigns.",
            "reward_timing": "Seasonal campaigns and monthly/weekly competitions.",
            "competition": "Very high; leaderboard and taker-volume rewards favor sophisticated traders.",
            "capital_intensity": "high",
            "monitorability": "public_docs",
            "execution_risk": "high",
            "automation_mode": "watchlist_only",
            "can_codex_capture": "No",
            "why_not_autonomous": "dYdX states it is not available in the U.S. or to restricted persons, and capture requires leveraged trading.",
            "safe_next_action": "Monitor rewards pages for research only; do not automate participation from a U.S. account.",
        },
        {
            "platform": "Hyperliquid",
            "program": "Maker Rebates, Fee Tiers, Staking Discounts, Referrals",
            "category": "crypto_perp_dex",
            "reward_type": "maker_rebate_fee_discount_referral",
            "source_status": "primary_verified",
            "official_urls": [
                "https://hyperliquid.gitbook.io/hyperliquid-docs/trading/fees",
                "https://app.hyperliquid.xyz/trade",
            ],
            "required_work": (
                "Generate maker volume share for rebates, reach rolling volume tiers, stake HYPE for discounts, "
                "or claim referral rewards when eligible."
            ),
            "reward_timing": "Maker rebates are paid continuously on each trade; volume tiers are assessed daily UTC.",
            "competition": "Very high; maker rebate thresholds start at meaningful share of venue maker volume.",
            "capital_intensity": "high",
            "monitorability": "public_docs",
            "execution_risk": "high",
            "automation_mode": "watchlist_only",
            "can_codex_capture": "No",
            "why_not_autonomous": "Requires active leveraged trading, wallet/account actions, and jurisdiction checks; app shows restricted-jurisdiction gating.",
            "safe_next_action": "Use public docs for fee modeling only; no account-side automation.",
        },
        {
            "platform": "Binance",
            "program": "Fiat Liquidity Provider Promotion",
            "category": "crypto_exchange",
            "reward_type": "institutional_maker_rebate",
            "source_status": "primary_verified",
            "official_urls": [
                "https://www.binance.com/en/square/post/326807011037522",
            ],
            "required_work": "Newly enrolled market makers qualify by maker volume share in eligible fiat markets.",
            "reward_timing": "Weekly review and hourly rebate updates during the promotion window.",
            "competition": "Institutional.",
            "capital_intensity": "institutional",
            "monitorability": "public_docs",
            "execution_risk": "high",
            "automation_mode": "eligibility_watch_only",
            "can_codex_capture": "No",
            "why_not_autonomous": "Requires exchange approval, major trading volume, and live market making.",
            "safe_next_action": "Track program changes and preserve as future institutional path, not current retail automation.",
        },
        {
            "platform": "Binance.US",
            "program": "Market Maker Program",
            "category": "crypto_exchange",
            "reward_type": "market_maker_rebate",
            "source_status": "primary_verified",
            "official_urls": [
                "https://support.binance.us/en/articles/9842933-what-is-the-binance-us-market-maker-program",
            ],
            "required_work": "Apply with at least $10M monthly volume and maintain competitive spread/depth rankings.",
            "reward_timing": "Ongoing fee/rebate benefits for top participants.",
            "competition": "Institutional or professional.",
            "capital_intensity": "institutional",
            "monitorability": "public_docs",
            "execution_risk": "high",
            "automation_mode": "eligibility_watch_only",
            "can_codex_capture": "No",
            "why_not_autonomous": "Requires approval and professional market-making scale.",
            "safe_next_action": "Keep in catalog for strategic awareness only.",
        },
        {
            "platform": "Coinbase International Exchange",
            "program": "Liquidity Program",
            "category": "crypto_exchange",
            "reward_type": "institutional_liquidity_rebate",
            "source_status": "primary_verified",
            "official_urls": [
                "https://help.coinbase.com/en/international-exchange/trading-deposits-withdrawals/international-exchange-liquidity-program",
            ],
            "required_work": "Meet prior-month adjusted volume, open-interest, or USDC-balance thresholds for perps or spot tiers.",
            "reward_timing": "Monthly evaluation; rebates reflected on fills.",
            "competition": "Institutional.",
            "capital_intensity": "institutional",
            "monitorability": "public_docs",
            "execution_risk": "high",
            "automation_mode": "eligibility_watch_only",
            "can_codex_capture": "No",
            "why_not_autonomous": "Eligibility is jurisdiction/account dependent and capture requires active exchange trading.",
            "safe_next_action": "Track only if an eligible non-U.S. institutional account exists.",
        },
        {
            "platform": "Coinbase Exchange",
            "program": "Liquidity Program",
            "category": "crypto_exchange",
            "reward_type": "large_liquidity_provider_discount",
            "source_status": "primary_verified",
            "official_urls": [
                "https://www.coinbase.com/exchange/liquidity-program",
                "https://www.coinbase.com/developer-platform/products/exchange-api",
            ],
            "required_work": "Operate as a large liquidity provider through Coinbase Exchange and API infrastructure.",
            "reward_timing": "Program-specific.",
            "competition": "Institutional.",
            "capital_intensity": "institutional",
            "monitorability": "public_docs",
            "execution_risk": "high",
            "automation_mode": "eligibility_watch_only",
            "can_codex_capture": "No",
            "why_not_autonomous": "Requires exchange account eligibility, high volume, and live trading.",
            "safe_next_action": "Catalog for strategic awareness and future API-readiness only.",
        },
        {
            "platform": "Kraken",
            "program": "Market Participation Program and Market Maker Incentives",
            "category": "crypto_exchange",
            "reward_type": "institutional_fee_rebate_equity_linked_incentive",
            "source_status": "primary_verified",
            "official_urls": [
                "https://www.kraken.com/institutions/market-participation-program",
                "https://www.kraken.com/institutions/market-makers",
            ],
            "required_work": "Qualify as an institution/professional/accredited participant and reach volume/share thresholds.",
            "reward_timing": "Weekly calculation/allocation and end-of-program distribution for MPP.",
            "competition": "Institutional.",
            "capital_intensity": "institutional",
            "monitorability": "public_docs",
            "execution_risk": "high",
            "automation_mode": "eligibility_watch_only",
            "can_codex_capture": "No",
            "why_not_autonomous": "Requires institutional eligibility and live spot/futures market participation.",
            "safe_next_action": "Track as a future institutional venue; not a small-account reward capture lane.",
        },
        {
            "platform": "Interactive Brokers",
            "program": "Stock Yield Enhancement Program",
            "category": "investment_broker",
            "reward_type": "securities_lending_income",
            "source_status": "primary_verified",
            "official_urls": [
                "https://www.interactivebrokers.com/en/pricing/stock-yield-enhancement-program.php",
            ],
            "required_work": "Enroll eligible fully paid or excess-margin shares so IBKR can lend them when borrow demand exists.",
            "reward_timing": "Daily accrual when shares are on loan; IBKR says it pays 50% of a market-based rate.",
            "competition": "Demand-driven rather than pool competition.",
            "capital_intensity": "medium",
            "monitorability": "public_docs",
            "execution_risk": "medium",
            "automation_mode": "manual_account_setting_ticket",
            "can_codex_capture": "No",
            "why_not_autonomous": "Requires brokerage account enrollment and accepts SIPC, voting, dividend-tax, and borrower-default tradeoffs.",
            "safe_next_action": "Prepare a manual opt-in/risk checklist if you already hold eligible fully paid securities.",
        },
        {
            "platform": "Robinhood",
            "program": "Stock Lending",
            "category": "investment_broker",
            "reward_type": "securities_lending_income",
            "source_status": "primary_verified",
            "official_urls": [
                "https://robinhood.com/us/en/support/articles/stock-lending/",
            ],
            "required_work": "Enable Stock Lending if eligible; Robinhood may borrow eligible whole fully paid shares.",
            "reward_timing": "Monthly payment when borrowed shares generate at least one cent of monthly rebate.",
            "competition": "Demand-driven rather than pool competition.",
            "capital_intensity": "low",
            "monitorability": "public_docs",
            "execution_risk": "medium",
            "automation_mode": "manual_account_setting_ticket",
            "can_codex_capture": "No",
            "why_not_autonomous": "Requires account setting changes and acceptance of share-lending risks.",
            "safe_next_action": "If you use Robinhood, inspect eligibility and expected holdings before manually enabling.",
        },
        {
            "platform": "Robinhood",
            "program": "High-Yield Cash Program",
            "category": "investment_broker",
            "reward_type": "cash_sweep_interest",
            "source_status": "primary_verified",
            "official_urls": [
                "https://robinhood.com/us/en/support/articles/cash-program-interest-rate/",
            ],
            "required_work": "Hold eligible settled cash and meet program requirements, including Robinhood Gold where applicable.",
            "reward_timing": "Monthly interest; APY changes over time.",
            "competition": "No pool competition.",
            "capital_intensity": "low",
            "monitorability": "public_docs",
            "execution_risk": "low",
            "automation_mode": "rate_watch_and_manual_ticket",
            "can_codex_capture": "No",
            "why_not_autonomous": "Requires account/subscription and cash allocation decisions.",
            "safe_next_action": "Monitor APY versus Treasury money-market alternatives before manual allocation.",
        },
        {
            "platform": "Airdrop and Task Platforms",
            "program": "Grass, Teneo, Monad, Backpack, MetaMask, Base, Yieldbay, Silencio watchlist",
            "category": "crypto_airdrop_tasks",
            "reward_type": "points_airdrop_task_bounty",
            "source_status": "secondary_needs_verification",
            "official_urls": [],
            "required_work": (
                "Varies: browser extensions, nodes, testnet actions, trading volume, bridging, social tasks, "
                "or app usage. Each must be verified from official docs before consideration."
            ),
            "reward_timing": "Speculative or campaign-specific.",
            "competition": "Very high and Sybil-heavy.",
            "capital_intensity": "low",
            "monitorability": "secondary_only",
            "execution_risk": "high",
            "automation_mode": "verification_queue_only",
            "can_codex_capture": "No",
            "why_not_autonomous": "High scam/Sybil/compliance risk and often requires wallets, extensions, social accounts, or fake activity.",
            "safe_next_action": "Only create official-source verification tasks; do not automate transactions or account farming.",
        },
    ]


def score_record(record: dict) -> int:
    """Score how useful the record is for safe monitoring and approval workflows."""
    source_score = {
        "primary_verified": 20,
        "secondary_needs_verification": 5,
    }.get(record["source_status"], 0)
    monitor_score = {
        "public_api": 25,
        "public_docs": 14,
        "secondary_only": 2,
    }.get(record["monitorability"], 0)
    capital_score = {
        "low": 18,
        "medium": 12,
        "high": 5,
        "institutional": 1,
    }.get(record["capital_intensity"], 0)
    risk_score = {
        "low": 14,
        "medium": 6,
        "high": -4,
    }.get(record["execution_risk"], 0)

    mode_bonus = 8 if "monitor" in record["automation_mode"] else 0
    category_bonus = 6 if record["category"] in {"prediction_market", "defi_incentive_aggregator"} else 0
    return source_score + monitor_score + capital_score + risk_score + mode_bonus + category_bonus


def ranked_catalog(records: list[dict]) -> list[dict]:
    ranked = []
    for record in records:
        row = dict(record)
        row["safe_automation_score"] = score_record(record)
        ranked.append(row)
    return sorted(
        ranked,
        key=lambda row: (row["safe_automation_score"], row["source_status"], row["platform"], row["program"]),
        reverse=True,
    )


def _format_urls(urls: list[str]) -> str:
    if not urls:
        return "Needs primary-source verification"
    return "<br>".join(f"[source]({url})" for url in urls)


def _shorten(value: str, limit: int = 170) -> str:
    if len(value) <= limit:
        return value
    return value[: limit - 3].rstrip() + "..."


def render_digest(records: list[dict], now: dt.datetime | None = None, limit: int = 12) -> str:
    now = now or _utc_now()
    ranked = ranked_catalog(records)
    top = ranked[:limit]
    by_category = {}
    for record in ranked:
        by_category.setdefault(record["category"], []).append(record)

    lines = [
        "# Market Rewards Platform Catalog",
        "",
        f"Generated: {now.isoformat(timespec='seconds')}",
        f"Safety boundary: {SAFETY_BOUNDARY}",
        "",
        "## Thesis",
        "",
        "The best automation target is not autonomous capture. It is autonomous discovery, source verification, reward-density scoring, risk math, and approval-ready tickets. The programs that actually pay meaningful rewards almost always require one of four things: live orders, filled trades, wallet transactions, or account-level opt-ins. Those are financial actions and stay behind a manual gate.",
        "",
        "Highest-value safe lanes: Kalshi public incentive monitoring, Polymarket/Polymarket US reward schedule monitoring, Merkl campaign discovery, and broker stock-lending/cash-rate watchlists. The lowest-value lanes for this account are institutional exchange market-maker programs and Sybil-heavy airdrop/task farming.",
        "",
        "## Highest Safe Automation Value",
        "",
        "| Rank | Platform | Program | Score | Category | Capital | Risk | Safe automation | Required work |",
        "| ---: | --- | --- | ---: | --- | --- | --- | --- | --- |",
    ]
    for idx, row in enumerate(top, start=1):
        lines.append(
            "| {rank} | {platform} | {program} | {score} | {category} | {capital} | {risk} | {mode} | {work} |".format(
                rank=idx,
                platform=row["platform"],
                program=row["program"],
                score=row["safe_automation_score"],
                category=row["category"],
                capital=row["capital_intensity"],
                risk=row["execution_risk"],
                mode=row["automation_mode"],
                work=_shorten(row["required_work"]),
            )
        )

    lines.extend([
        "",
        "## Platform Catalog",
        "",
        "| Platform | Program | Reward type | Source | Competition | Reward timing | Can Codex capture? | Why not autonomous | Sources |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- |",
    ])
    for row in ranked:
        lines.append(
            "| {platform} | {program} | {reward_type} | {source_status} | {competition} | {timing} | {capture} | {why} | {sources} |".format(
                platform=row["platform"],
                program=row["program"],
                reward_type=row["reward_type"],
                source_status=row["source_status"],
                competition=_shorten(row["competition"], 120),
                timing=_shorten(row["reward_timing"], 140),
                capture=row["can_codex_capture"],
                why=_shorten(row["why_not_autonomous"], 160),
                sources=_format_urls(row["official_urls"]),
            )
        )

    lines.extend([
        "",
        "## Category Counts",
        "",
    ])
    for category, category_rows in sorted(by_category.items()):
        lines.append(f"- {category}: {len(category_rows)}")

    lines.extend([
        "",
        "## Automation Buildout",
        "",
        "1. Discovery monitor: refresh this catalog and the Kalshi public rewards digest on a schedule.",
        "2. Source verifier: flag programs with `secondary_needs_verification` until primary official docs are found.",
        "3. Reward scorer: estimate reward density, capital requirement, fees, gas, slippage, borrow/liquidation risk, and eligibility.",
        "4. Approval ticket: produce a human-review checklist with max loss, expected reward, required action, and exact source links.",
        "5. Execution gate: no live orders, account changes, wallet signatures, claims, or trades unless you explicitly do them yourself.",
        "",
    ])
    return "\n".join(lines)


def write_csv(records: list[dict], output: Path) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    fields = [
        "platform",
        "program",
        "category",
        "reward_type",
        "source_status",
        "safe_automation_score",
        "required_work",
        "reward_timing",
        "competition",
        "capital_intensity",
        "monitorability",
        "execution_risk",
        "automation_mode",
        "can_codex_capture",
        "why_not_autonomous",
        "safe_next_action",
        "official_urls",
    ]
    with output.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        for row in ranked_catalog(records):
            writer.writerow({
                "platform": row["platform"],
                "program": row["program"],
                "category": row["category"],
                "reward_type": row["reward_type"],
                "source_status": row["source_status"],
                "safe_automation_score": row["safe_automation_score"],
                "required_work": row["required_work"],
                "reward_timing": row["reward_timing"],
                "competition": row["competition"],
                "capital_intensity": row["capital_intensity"],
                "monitorability": row["monitorability"],
                "execution_risk": row["execution_risk"],
                "automation_mode": row["automation_mode"],
                "can_codex_capture": row["can_codex_capture"],
                "why_not_autonomous": row["why_not_autonomous"],
                "safe_next_action": row["safe_next_action"],
                "official_urls": " ".join(row["official_urls"]),
            })


def write_json(records: list[dict], output: Path) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "generated_at": _utc_now().isoformat(timespec="seconds"),
        "safety_boundary": SAFETY_BOUNDARY,
        "records": ranked_catalog(records),
    }
    output.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Generate a read-only market rewards platform catalog.")
    parser.add_argument("--limit", type=int, default=12, help="Rows in the highest-value table.")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT, help="Markdown output path.")
    parser.add_argument("--csv-output", type=Path, default=DEFAULT_CSV_OUTPUT, help="CSV output path.")
    parser.add_argument("--json-output", type=Path, default=DEFAULT_JSON_OUTPUT, help="JSON output path.")
    parser.add_argument("--stdout-only", action="store_true", help="Print only; do not write files.")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    records = platform_catalog()
    digest = render_digest(records, _utc_now(), args.limit)
    print(digest)

    if not args.stdout_only:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(digest + "\n", encoding="utf-8")
        write_csv(records, args.csv_output)
        write_json(records, args.json_output)
        print(f"\nWrote {args.output}")
        print(f"Wrote {args.csv_output}")
        print(f"Wrote {args.json_output}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
