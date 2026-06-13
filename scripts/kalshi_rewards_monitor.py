#!/usr/bin/env python3
"""Read-only Kalshi rewards monitor.

This script uses Kalshi's public incentive-programs endpoint only. It does not
read API keys, authenticate, place orders, cancel orders, or touch account state.
"""

import argparse
import csv
import datetime as dt
import json
import sys
import urllib.parse
import urllib.request
from collections import Counter, defaultdict
from pathlib import Path

KALSHI_PUBLIC_BASE_URL = "https://external-api.kalshi.com/trade-api/v2"
PERIOD_REWARD_DOLLAR_DIVISOR = 10000.0
DEFAULT_OUTPUT = Path("data/kalshi-rewards/latest.md")
DEFAULT_CSV_OUTPUT = Path("data/kalshi-rewards/latest.csv")


def _utc_now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _parse_time(value: str) -> dt.datetime:
    return dt.datetime.fromisoformat(value.replace("Z", "+00:00"))


def _format_money(value: float) -> str:
    if value >= 100:
        return f"${value:,.0f}"
    return f"${value:,.2f}"


def _format_hours(hours: float) -> str:
    if hours < 0:
        return "ended"
    if hours < 48:
        return f"{hours:.1f}h"
    return f"{hours / 24:.1f}d"


def period_reward_dollars(raw_value: int | float | str | None) -> float:
    """Convert Kalshi period_reward units to dollars."""
    if raw_value in (None, ""):
        return 0.0
    return float(raw_value) / PERIOD_REWARD_DOLLAR_DIVISOR


def _fetch_json(path: str, params: dict) -> dict:
    query = urllib.parse.urlencode(params)
    url = f"{KALSHI_PUBLIC_BASE_URL}{path}?{query}"
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def fetch_incentives(status: str = "active", incentive_type: str = "liquidity", limit: int = 10000) -> list[dict]:
    """Fetch incentive programs from Kalshi's public read-only endpoint."""
    incentives = []
    cursor = None

    while True:
        params = {"status": status, "type": incentive_type, "limit": limit}
        if cursor:
            params["cursor"] = cursor

        payload = _fetch_json("/incentive_programs", params)
        incentives.extend(payload.get("incentive_programs", []))
        cursor = payload.get("next_cursor") or payload.get("cursor")
        if not cursor:
            break

    return incentives


def fetch_active_incentive_type_counts() -> Counter:
    """Count active incentive program types from the public endpoint."""
    payload = _fetch_json("/incentive_programs", {"status": "active", "type": "all", "limit": 10000})
    return Counter(row.get("incentive_type") or "unknown" for row in payload.get("incentive_programs", []))


def _group_key(ticker: str, base_counts: Counter) -> str:
    """Derive a stable event-like key from market tickers.

    Kalshi incentive rows are market-level. UI reward rows usually group related
    market tickers into event pools, but the exact event ticker is not returned
    in the incentive payload. This heuristic keeps one-off dated markets intact,
    groups multi-outcome markets by dropping the final outcome leg, and groups
    repeated two-part collections by their base ticker.
    """
    parts = ticker.split("-")
    if len(parts) <= 1:
        return ticker

    base = parts[0]
    if len(parts) == 2:
        return base if base_counts[base] >= 3 else ticker

    return "-".join(parts[:-1])


def _category_hint(group_key: str) -> str:
    key = group_key.upper()
    if any(token in key for token in ("CPI", "GDP", "U3", "INX", "EOWEEK")):
        return "macro/markets"
    if any(token in key for token in ("BILLS", "MAYOR", "TRUMP", "GOV", "SENATE")):
        return "politics/policy"
    if any(token in key for token in ("LIUSA", "LOVEISLAND", "TAYLOR", "RANKLISTSONG", "GRAM")):
        return "entertainment"
    if any(token in key for token in ("UFC", "MLB", "WCPRICE")):
        return "sports"
    if any(token in key for token in ("OPENAI", "B200", "RTX", "MUSK", "USACOMPANY")):
        return "company/tech"
    if "HURRICANE" in key:
        return "weather"
    return "other"


def _competition_proxy(summary: dict) -> str:
    """Estimate contest difficulty from public fields.

    The Kalshi UI may show a direct Competition label. The public incentive API
    does not expose that label, so this is a conservative proxy.
    """
    total_reward = summary["total_reward"]
    avg_reward = summary["avg_reward"]
    avg_target = summary["avg_target"]
    markets = summary["markets"]

    if avg_target >= 1000 and (total_reward >= 2500 or markets >= 20):
        return "High"
    if avg_reward >= 750:
        return "High bounty"
    if avg_target <= 300 and markets <= 3:
        return "Medium"
    if avg_target >= 1000:
        return "Medium-High"
    return "Medium-Low"


def _manual_review_score(summary: dict, now: dt.datetime) -> float:
    avg_reward = summary["avg_reward"]
    avg_target = summary["avg_target"]
    markets = summary["markets"]
    hours_left = (summary["earliest_end"] - now).total_seconds() / 3600
    descriptions = summary["descriptions"]
    category = summary["category"]

    reward_score = min(avg_reward / 1000.0, 1.0) * 35.0

    if avg_target <= 300:
        target_score = 25.0
    elif avg_target <= 500:
        target_score = 18.0
    elif avg_target <= 1000:
        target_score = 9.0
    else:
        target_score = 3.0

    if hours_left < 0:
        time_score = 0.0
    elif hours_left < 6:
        time_score = 3.0
    elif hours_left < 24:
        time_score = 8.0
    elif hours_left <= 168:
        time_score = 20.0
    else:
        time_score = 14.0

    if markets <= 3:
        complexity_score = 10.0
    elif markets <= 10:
        complexity_score = 7.0
    elif markets <= 25:
        complexity_score = 4.0
    else:
        complexity_score = 1.0

    category_score = {
        "macro/markets": 8.0,
        "company/tech": 7.0,
        "politics/policy": 5.0,
        "weather": 5.0,
        "other": 4.0,
        "entertainment": 2.0,
        "sports": 2.0,
    }.get(category, 4.0)

    description_penalty = 3.0 if "long_dated" in descriptions else 0.0
    return round(reward_score + target_score + time_score + complexity_score + category_score - description_penalty, 1)


def summarize_incentives(incentives: list[dict], now: dt.datetime | None = None) -> list[dict]:
    now = now or _utc_now()
    base_counts = Counter((row.get("market_ticker") or "").split("-")[0] for row in incentives)
    grouped = defaultdict(list)

    for row in incentives:
        ticker = row.get("market_ticker") or ""
        if not ticker:
            continue
        grouped[_group_key(ticker, base_counts)].append(row)

    summaries = []
    for group_key, rows in grouped.items():
        rewards = [period_reward_dollars(row.get("period_reward")) for row in rows]
        targets = [float(row.get("target_size_fp") or 0.0) for row in rows]
        ends = [_parse_time(row["end_date"]) for row in rows if row.get("end_date")]
        descriptions = {row.get("incentive_description") or "" for row in rows}
        incentive_types = {row.get("incentive_type") or "liquidity" for row in rows}
        earliest_end = min(ends) if ends else now
        category = _category_hint(group_key)
        total_reward = sum(rewards)
        avg_reward = total_reward / len(rows)
        avg_target = sum(targets) / len(targets) if targets else 0.0

        summary = {
            "group_key": group_key,
            "category": category,
            "markets": len(rows),
            "total_reward": total_reward,
            "avg_reward": avg_reward,
            "avg_target": avg_target,
            "earliest_end": earliest_end,
            "hours_left": (earliest_end - now).total_seconds() / 3600,
            "descriptions": descriptions,
            "incentive_types": incentive_types,
            "sample_tickers": sorted(row.get("market_ticker") or "" for row in rows)[:5],
        }
        summary["required_action"] = required_action(summary)
        summary["exposure_risk"] = exposure_risk(summary)
        summary["automation_mode"] = automation_mode(summary)
        summary["competition_proxy"] = _competition_proxy(summary)
        summary["manual_review_score"] = _manual_review_score(summary, now)
        summaries.append(summary)

    return sorted(
        summaries,
        key=lambda row: (row["manual_review_score"], row["avg_reward"], -row["avg_target"], row["total_reward"]),
        reverse=True,
    )


def _why(summary: dict) -> str:
    notes = []
    if summary["avg_target"] <= 300:
        notes.append("small target")
    elif summary["avg_target"] >= 1000:
        notes.append("large target")

    if summary["hours_left"] < 24:
        notes.append("near deadline")
    elif summary["hours_left"] > 168:
        notes.append("longer runway")

    if summary["category"] in {"macro/markets", "company/tech"}:
        notes.append("researchable")
    elif summary["category"] in {"entertainment", "sports"}:
        notes.append("higher info risk")

    if summary["markets"] <= 3:
        notes.append("low complexity")
    elif summary["markets"] >= 20:
        notes.append("many markets")

    return ", ".join(notes) or "review manually"


def required_action(summary: dict) -> str:
    incentive_types = summary.get("incentive_types") or {"liquidity"}
    if incentive_types == {"liquidity"}:
        return "Post qualifying resting limit orders during the active window; score depends on size, uptime, and distance from best bid/ask."
    if incentive_types == {"volume"}:
        return "Execute eligible trades during the active window; reward share depends on eligible volume."
    return "Review mixed incentive terms in Kalshi before acting."


def exposure_risk(summary: dict) -> str:
    incentive_types = summary.get("incentive_types") or {"liquidity"}
    target = summary.get("avg_target") or 0
    if incentive_types == {"liquidity"}:
        return (
            f"Unfilled orders may score, but fills create inventory. Target is about {target:.0f} contracts per market; "
            "worst-case contract exposure can approach $1 per filled contract before offsetting exits."
        )
    if incentive_types == {"volume"}:
        return "Requires filled trades, so exposure is immediate unless offset with an approved hedge or exit."
    return "Exposure depends on the exact incentive mix and order terms."


def automation_mode(summary: dict) -> str:
    return "Read-only discovery and paper scoring only; live orders require human approval."


def _render_table(title: str, rows: list[dict], limit: int) -> list[str]:
    lines = [
        f"## {title}",
        "",
        "| Rank | Reward pool | Score | Competition proxy | Category | Total | Markets | Avg/market | Target | Time left | Required action | Why |",
        "| ---: | --- | ---: | --- | --- | ---: | ---: | ---: | ---: | ---: | --- | --- |",
    ]
    for idx, row in enumerate(rows[:limit], start=1):
        lines.append(
            "| {rank} | `{key}` | {score:.1f} | {comp} | {category} | {total} | {markets} | {avg} | {target:.0f} | {time} | {action} | {why} |".format(
                rank=idx,
                key=row["group_key"],
                score=row["manual_review_score"],
                comp=row["competition_proxy"],
                category=row["category"],
                total=_format_money(row["total_reward"]),
                markets=row["markets"],
                avg=_format_money(row["avg_reward"]),
                target=row["avg_target"],
                time=_format_hours(row["hours_left"]),
                action=row["required_action"],
                why=_why(row),
            )
        )
    lines.append("")
    return lines


def render_digest(summaries: list[dict], now: dt.datetime | None = None, limit: int = 12) -> str:
    now = now or _utc_now()
    active = [row for row in summaries if row["hours_left"] > 0]
    largest = sorted(active, key=lambda row: row["total_reward"], reverse=True)
    small_target = sorted(
        [row for row in active if row["avg_target"] <= 300],
        key=lambda row: (row["manual_review_score"], row["avg_reward"]),
        reverse=True,
    )

    type_counts = Counter()
    for row in active:
        for incentive_type in row.get("incentive_types", {"unknown"}):
            type_counts[incentive_type] += row["markets"]

    lines = [
        "# Kalshi Rewards Read-Only Digest",
        "",
        f"Generated: {now.isoformat(timespec='seconds')}",
        "Source: Kalshi public `/trade-api/v2/incentive_programs?status=active&type=liquidity`.",
        "",
        "Safety boundary: this digest is read-only. It does not use Kalshi credentials, place orders, cancel orders, copy referral links, or complete account actions.",
        "",
        "## How These Rewards Work",
        "",
        "- Liquidity rewards are market-making contests. You earn only by providing qualifying resting liquidity during the listed window; the pool is split by Kalshi's program rules, so the displayed reward is not guaranteed.",
        "- Liquidity rewards do not require your order to fill, but you must place real resting orders. If another trader takes your order, you now have a live position and inventory risk.",
        "- Volume rewards are different: they require actual eligible trades. The current active public feed is liquidity-only unless the type count below changes.",
        "- `target_size_fp` is the posted-liquidity size target shown by the API. A 1000 target is much harder for a small account than a 300 target.",
        "- Competition matters. The public API gives reward size, target size, and timing, but not the UI's exact Competition label, so the competition column below is a proxy.",
        "- Manual gate before any trade: open the incentive row, confirm exact terms, verify the order book, define max inventory loss, then place only orders you personally approve.",
        "",
        f"Active incentive rows by type: {', '.join(f'{k}={v}' for k, v in sorted(type_counts.items())) or 'none'}.",
        "",
    ]
    lines.extend(_render_table("Best Manual-Review Candidates", active, limit))
    lines.extend(_render_table("Largest Reward Pools", largest, limit))
    lines.extend(_render_table("Small-Target Watchlist", small_target, min(limit, 10)))

    lines.extend([
        "## Current Thesis",
        "",
        "For this account, the best automation target is discovery, ranking, and alerts. The reward pools are not claim buttons; they require live liquidity provision and can lose money through adverse selection, fills, wide markets, and stale quotes.",
        "",
        "Near-term review priority: small-target, low-complexity rows first; researchable macro/company rows second; large entertainment or sports pools last unless the UI shows unusually low competition and the order book is calm.",
        "",
    ])
    return "\n".join(lines)


def write_csv(summaries: list[dict], output: Path) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    fields = [
        "group_key",
        "incentive_types",
        "category",
        "markets",
        "total_reward",
        "avg_reward",
        "avg_target",
        "earliest_end",
        "hours_left",
        "competition_proxy",
        "manual_review_score",
        "required_action",
        "exposure_risk",
        "automation_mode",
        "sample_tickers",
    ]
    with output.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        for row in summaries:
            writer.writerow({
                "group_key": row["group_key"],
                "incentive_types": ",".join(sorted(row.get("incentive_types", []))),
                "category": row["category"],
                "markets": row["markets"],
                "total_reward": f"{row['total_reward']:.4f}",
                "avg_reward": f"{row['avg_reward']:.4f}",
                "avg_target": f"{row['avg_target']:.2f}",
                "earliest_end": row["earliest_end"].isoformat(),
                "hours_left": f"{row['hours_left']:.2f}",
                "competition_proxy": row["competition_proxy"],
                "manual_review_score": f"{row['manual_review_score']:.1f}",
                "required_action": row["required_action"],
                "exposure_risk": row["exposure_risk"],
                "automation_mode": row["automation_mode"],
                "sample_tickers": ",".join(row["sample_tickers"]),
            })


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Generate a read-only Kalshi rewards digest.")
    parser.add_argument("--limit", type=int, default=12, help="Rows per table.")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT, help="Markdown output path.")
    parser.add_argument("--csv-output", type=Path, default=DEFAULT_CSV_OUTPUT, help="Full event-level CSV output path.")
    parser.add_argument("--stdout-only", action="store_true", help="Print only; do not write a file.")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    now = _utc_now()
    incentives = fetch_incentives()
    summaries = summarize_incentives(incentives, now)
    digest = render_digest(summaries, now, args.limit)

    print(digest)
    if not args.stdout_only:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(digest + "\n", encoding="utf-8")
        write_csv(summaries, args.csv_output)
        print(f"\nWrote {args.output}")
        print(f"Wrote {args.csv_output}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
