#!/usr/bin/env python3
"""Read-only Polymarket US rewards monitor.

This script uses official Polymarket US public docs and public market-data
endpoints only. It does not authenticate, place orders, cancel orders, refer
users, deposit funds, or touch account state.
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

POLYMARKET_PUBLIC_BASE_URL = "https://gateway.polymarket.us"
DEFAULT_OUTPUT = Path("data/polymarket-rewards/latest.md")
DEFAULT_CSV_OUTPUT = Path("data/polymarket-rewards/latest.csv")
DEFAULT_JSON_OUTPUT = Path("data/polymarket-rewards/latest.json")

DOC_URLS = {
    "overview": "https://docs.polymarket.us/incentives/overview",
    "liquidity": "https://docs.polymarket.us/incentives/liquidity",
    "volume": "https://docs.polymarket.us/incentives/volume",
    "market_maker": "https://docs.polymarket.us/incentives/market-maker",
    "markets_api": "https://docs.polymarket.us/api-reference/market/overview",
    "api_intro": "https://docs.polymarket.us/api-reference/introduction",
    "fees": "https://docs.polymarket.us/fees",
    "changelog": "https://docs.polymarket.us/changelog",
}

PROGRAM_ROWS = [
    {
        "candidate_id": "polymarket-us-liquidity-incentive",
        "platform": "Polymarket US",
        "program": "Liquidity Incentive Program",
        "category": "prediction_market",
        "reward_type": "liquidity_pool",
        "source_status": "primary_verified",
        "safe_automation_score": 69,
        "required_action_type": "order_place",
        "required_work": "Place resting limit orders close to the best price on eligible contracts.",
        "reward_timing": "Daily reward periods, midnight-to-midnight Eastern Time.",
        "capital_intensity": "medium_high",
        "execution_risk": "high",
        "max_capital_usd": 0,
        "source_url": DOC_URLS["liquidity"],
        "direct_url": DOC_URLS["liquidity"],
        "validation_status": "official_docs_verified",
        "legal_tos_status": "not_reviewed",
        "confidence": "medium",
        "deadline": "",
        "risk_notes": "Requires live resting orders; fills create inventory and adverse-selection risk. Public gateway access may be restricted from this environment.",
        "manual_preflight_checks": "Confirm account eligibility; confirm exact reward table; inspect book depth; define max inventory loss; keep Polymarket non-live unless separately approved.",
    },
    {
        "candidate_id": "polymarket-us-volume-incentive",
        "platform": "Polymarket US",
        "program": "Volume Incentive Program",
        "category": "prediction_market",
        "reward_type": "volume_pool",
        "source_status": "primary_verified",
        "safe_automation_score": 42,
        "required_action_type": "trade",
        "required_work": "Execute eligible taker trades on reward-enabled contracts.",
        "reward_timing": "Contract-specific reward pools.",
        "capital_intensity": "medium_high",
        "execution_risk": "very_high",
        "max_capital_usd": 500,
        "source_url": DOC_URLS["volume"],
        "direct_url": DOC_URLS["volume"],
        "validation_status": "official_docs_verified",
        "legal_tos_status": "not_reviewed",
        "confidence": "medium",
        "deadline": "",
        "risk_notes": "Volume rewards require filled trades and can incentivize uneconomic volume. Minimum notional and price-band terms apply.",
        "manual_preflight_checks": "Confirm eligible contract; estimate fees/spread/slippage; confirm reward pool; reject if trading volume is uneconomic without reward.",
    },
    {
        "candidate_id": "polymarket-us-market-maker-program",
        "platform": "Polymarket US",
        "program": "Market Maker Program",
        "category": "prediction_market",
        "reward_type": "market_maker_program",
        "source_status": "primary_verified",
        "safe_automation_score": 38,
        "required_action_type": "order_place",
        "required_work": "Apply for a formal market-maker arrangement and provide stable liquidity if approved.",
        "reward_timing": "Program-specific.",
        "capital_intensity": "high",
        "execution_risk": "high",
        "max_capital_usd": 0,
        "source_url": DOC_URLS["market_maker"],
        "direct_url": DOC_URLS["market_maker"],
        "validation_status": "official_docs_verified",
        "legal_tos_status": "not_reviewed",
        "confidence": "low",
        "deadline": "",
        "risk_notes": "Requires application/approval and likely contractual obligations.",
        "manual_preflight_checks": "Review eligibility and obligations; do not apply or trade from automation.",
    },
]

FIELDS = [
    "candidate_id",
    "platform",
    "program",
    "category",
    "reward_type",
    "source_status",
    "safe_automation_score",
    "required_action_type",
    "required_work",
    "reward_timing",
    "capital_intensity",
    "execution_risk",
    "max_capital_usd",
    "source_url",
    "direct_url",
    "validation_status",
    "legal_tos_status",
    "confidence",
    "deadline",
    "risk_notes",
    "manual_preflight_checks",
    "market_slug",
    "question",
    "market_category",
    "volume_num",
    "liquidity_num",
    "volume_24h",
    "rewards_min_size",
    "minimum_trade_qty",
    "order_price_min_tick_size",
    "best_bid",
    "best_ask",
]


def _utc_now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _float(value: Any, default: float = 0.0) -> float:
    try:
        if value in (None, ""):
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _first(mapping: dict, *keys: str) -> Any:
    for key in keys:
        if key in mapping and mapping[key] not in (None, ""):
            return mapping[key]
    return ""


def _amount_value(value: Any) -> str:
    if isinstance(value, dict):
        return str(value.get("value") or "")
    return str(value or "")


def _fetch_json(path: str, params: dict[str, Any]) -> dict:
    query = urllib.parse.urlencode(params, doseq=True)
    url = f"{POLYMARKET_PUBLIC_BASE_URL}{path}?{query}"
    req = urllib.request.Request(
        url,
        headers={
            "Accept": "application/json",
            "User-Agent": "codex-readonly-polymarket-rewards-monitor/1.0",
        },
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def fetch_markets(limit: int = 50) -> tuple[list[dict], str]:
    """Fetch active public Polymarket US markets, returning an error string on failure."""
    params = {
        "active": "true",
        "closed": "false",
        "archived": "false",
        "limit": limit,
        "orderBy": "volumeNum",
        "orderDirection": "desc",
    }
    try:
        payload = _fetch_json("/v1/markets", params)
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        return [], f"{type(exc).__name__}: {exc}"

    if isinstance(payload, list):
        return payload, ""
    rows = payload.get("markets") or payload.get("data") or payload.get("items") or []
    return rows if isinstance(rows, list) else [], ""


def market_to_candidate(row: dict) -> dict:
    slug = str(_first(row, "slug", "marketSlug", "id"))
    question = str(_first(row, "question", "title", "description"))
    rewards_min_size = _float(_first(row, "rewardsMinSize", "rewards_min_size"))
    volume_num = _float(_first(row, "volumeNum", "volume", "notionalTraded"))
    liquidity_num = _float(_first(row, "liquidityNum", "liquidity"))
    spread = _float(row.get("spread"), 0)

    score = 25
    if rewards_min_size:
        score += 20
    if volume_num >= 10000:
        score += 12
    if liquidity_num >= 5000:
        score += 8
    if spread and spread <= 0.03:
        score += 5

    return {
        "candidate_id": f"polymarket-us-market-{slug}",
        "platform": "Polymarket US",
        "program": "Public Market Reward Candidate",
        "category": "prediction_market",
        "reward_type": "market_reward_candidate",
        "source_status": "public_api",
        "safe_automation_score": min(score, 70),
        "required_action_type": "order_place",
        "required_work": "Review market reward eligibility and book state before any manual order.",
        "reward_timing": "Market-specific.",
        "capital_intensity": "medium_high",
        "execution_risk": "high",
        "max_capital_usd": rewards_min_size or 0,
        "source_url": DOC_URLS["markets_api"],
        "direct_url": f"https://polymarket.us/event/{slug}" if slug else DOC_URLS["markets_api"],
        "validation_status": "public_api_observed",
        "legal_tos_status": "not_reviewed",
        "confidence": "low" if not rewards_min_size else "medium",
        "deadline": str(_first(row, "endDate", "end_date", "endDateIso")),
        "risk_notes": "Public market-data candidate only. Confirm incentives, jurisdiction, account eligibility, and exact terms manually.",
        "manual_preflight_checks": "Open market; confirm incentive eligibility; inspect BBO/book; estimate inventory and fill risk; do not trade from automation.",
        "market_slug": slug,
        "question": question[:240],
        "market_category": str(_first(row, "category", "subcategory")),
        "volume_num": f"{volume_num:.2f}",
        "liquidity_num": f"{liquidity_num:.2f}",
        "volume_24h": str(_first(row, "volume24hr", "volume24h")),
        "rewards_min_size": f"{rewards_min_size:.2f}" if rewards_min_size else "",
        "minimum_trade_qty": str(_first(row, "minimumTradeQty", "minimum_trade_qty")),
        "order_price_min_tick_size": str(_first(row, "orderPriceMinTickSize", "order_price_min_tick_size")),
        "best_bid": _amount_value(_first(row, "bestBid", "best_bid")),
        "best_ask": _amount_value(_first(row, "bestAsk", "best_ask")),
    }


def build_candidates(markets: list[dict]) -> list[dict]:
    rows = [dict(row) for row in PROGRAM_ROWS]
    rows.extend(market_to_candidate(row) for row in markets[:20])
    return sorted(rows, key=lambda row: _float(row.get("safe_automation_score")), reverse=True)


def _short(text: str, max_len: int = 170) -> str:
    text = text.replace("|", "/").replace("\n", " ").strip()
    if len(text) <= max_len:
        return text
    return text[: max_len - 3].rstrip() + "..."


def render_digest(candidates: list[dict], now: dt.datetime, gateway_error: str, limit: int) -> str:
    lines = [
        "# Polymarket Rewards Read-Only Digest",
        "",
        f"Generated: {now.isoformat(timespec='seconds')}",
        "Sources: official Polymarket US incentive docs plus public market-data gateway when reachable.",
        "",
        "Safety boundary: read-only discovery, ranking, docs verification, and approval-ticket generation only. No Polymarket credentials, authenticated API calls, account actions, deposits, referrals, orders, or trades.",
        "",
        "## Data Inputs",
        "",
        f"- Incentive overview: {DOC_URLS['overview']}",
        f"- Liquidity incentives: {DOC_URLS['liquidity']}",
        f"- Volume incentives: {DOC_URLS['volume']}",
        f"- Market maker program: {DOC_URLS['market_maker']}",
        f"- Public markets API docs: {DOC_URLS['markets_api']}",
        f"- Public API base: `{POLYMARKET_PUBLIC_BASE_URL}`",
        "",
    ]
    if gateway_error:
        lines.extend([
            "## Public Gateway Status",
            "",
            f"- Gateway fetch failed from this environment: `{_short(gateway_error, 260)}`",
            "- Digest continues from official docs and records the gateway failure as a blocker.",
            "",
        ])

    lines.extend([
        "## Top Manual-Review Candidates",
        "",
        "| Rank | Program / market | Score | Reward type | Action | Capital clue | Source | Why manual-gated |",
        "| ---: | --- | ---: | --- | --- | --- | --- | --- |",
    ])
    for idx, row in enumerate(candidates[:limit], start=1):
        name = row.get("program") or row.get("question") or row.get("market_slug") or row.get("candidate_id")
        source = row.get("source_url") or row.get("direct_url") or ""
        lines.append(
            "| {rank} | {name} | {score} | {rtype} | {action} | {capital} | [source]({source}) | {risk} |".format(
                rank=idx,
                name=_short(str(name), 80),
                score=row.get("safe_automation_score", ""),
                rtype=row.get("reward_type", ""),
                action=row.get("required_action_type", ""),
                capital=row.get("max_capital_usd", ""),
                source=source,
                risk=_short(str(row.get("risk_notes", "")), 130),
            )
        )

    lines.extend([
        "",
        "## Current Thesis",
        "",
        "Polymarket belongs in this rewards monitor as a public-source intelligence feed and manual ticket generator. It should not be promoted to live execution unless legal/TOS, jurisdiction, account eligibility, and execution access are separately proven and approved.",
        "",
    ])
    return "\n".join(lines)


def write_csv(candidates: list[dict], output: Path) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDS)
        writer.writeheader()
        for row in candidates:
            writer.writerow({field: row.get(field, "") for field in FIELDS})


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Generate a read-only Polymarket rewards digest.")
    parser.add_argument("--limit", type=int, default=12, help="Rows in top-ranked table.")
    parser.add_argument("--market-limit", type=int, default=50, help="Public markets to request from gateway.")
    parser.add_argument("--docs-only", action="store_true", help="Skip gateway fetch and use official docs seeds only.")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT, help="Markdown output path.")
    parser.add_argument("--csv-output", type=Path, default=DEFAULT_CSV_OUTPUT, help="CSV output path.")
    parser.add_argument("--json-output", type=Path, default=DEFAULT_JSON_OUTPUT, help="JSON output path.")
    parser.add_argument("--stdout-only", action="store_true", help="Print only; do not write files.")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    now = _utc_now()
    gateway_error = ""
    markets: list[dict] = []
    if not args.docs_only:
        markets, gateway_error = fetch_markets(args.market_limit)
    candidates = build_candidates(markets)
    digest = render_digest(candidates, now, gateway_error, args.limit)

    print(digest)
    if not args.stdout_only:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(digest + "\n", encoding="utf-8")
        write_csv(candidates, args.csv_output)
        args.json_output.write_text(
            json.dumps(
                {
                    "generated_at": now.isoformat(timespec="seconds"),
                    "gateway_error": gateway_error,
                    "doc_urls": DOC_URLS,
                    "candidates": candidates,
                },
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        print(f"\nWrote {args.output}")
        print(f"Wrote {args.csv_output}")
        print(f"Wrote {args.json_output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
