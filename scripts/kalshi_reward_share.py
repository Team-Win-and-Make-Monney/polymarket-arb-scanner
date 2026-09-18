#!/usr/bin/env python3
"""Live Kalshi LIP qualifying-share logger (public data, no orders).

Measures the operator's expected pool share if quoting ``quote_size`` contracts
at the published Reference Price. HTTP/parse failures stay failures — they are
never coerced to zero share. Does not authenticate or place orders.
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

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from kalshi_lip import qualifying_share  # noqa: E402
from kalshi_policy import event_blocked  # noqa: E402
from kalshi_rewards_monitor import fetch_incentives, period_reward_dollars  # noqa: E402

ORDERBOOK_URL = "https://api.elections.kalshi.com/trade-api/v2/markets/{ticker}/orderbook"
MARKET_URL = "https://api.elections.kalshi.com/trade-api/v2/markets/{ticker}"


def _utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _get_json(url: str) -> dict:
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _levels(book: dict, side: str) -> list[tuple[float, float]]:
    raw = book.get(f"{side}_dollars") or book.get(side) or []
    levels: list[tuple[float, float]] = []
    for row in raw:
        if isinstance(row, (list, tuple)) and len(row) >= 2:
            levels.append((float(row[0]), float(row[1])))
        elif isinstance(row, dict):
            price = row.get("price") or row.get("yes") or row.get("no")
            size = row.get("size") or row.get("count") or row.get("quantity")
            if price is None or size is None:
                continue
            levels.append((float(price), float(size)))
    return levels


def fetch_orderbook(ticker: str, depth: int = 100) -> dict:
    url = ORDERBOOK_URL.format(ticker=urllib.parse.quote(ticker, safe=""))
    url = f"{url}?{urllib.parse.urlencode({'depth': depth})}"
    payload = _get_json(url)
    return payload.get("orderbook") or payload.get("orderbook_fp") or payload


def fetch_market(ticker: str) -> dict:
    url = MARKET_URL.format(ticker=urllib.parse.quote(ticker, safe=""))
    payload = _get_json(url)
    return payload.get("market") or payload


def score_market(
    ticker: str,
    target_size: float,
    discount_factor: float,
    quote_size: float,
) -> dict:
    row = {
        "ts_utc": _utc_now(),
        "ticker": ticker,
        "share": None,
        "skip_reason": None,
        "quote_size": quote_size,
        "target_size": target_size,
        "discount_factor": discount_factor,
    }
    try:
        book = fetch_orderbook(ticker)
        yes_levels = _levels(book, "yes")
        from kalshi_lip import reference_price
        ref = reference_price(yes_levels, target_size)
        if ref is None:
            row["skip_reason"] = "no_refprice"
            return row
        share, reason = qualifying_share(
            yes_levels, target_size, discount_factor, quote_price=ref, quote_size=quote_size
        )
        row["share"] = share
        row["skip_reason"] = reason
        row["reference_price"] = ref
    except urllib.error.HTTPError as exc:
        row["skip_reason"] = f"http_{exc.code}"
    except urllib.error.URLError as exc:
        row["skip_reason"] = f"url_error:{exc.reason}"
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        row["skip_reason"] = f"parse:{exc}"
    return row


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, default=25)
    parser.add_argument("--quote-size", type=float, default=200.0)
    parser.add_argument("--out", type=Path, default=Path("artifacts/kalshi-reward-share-live.jsonl"))
    args = parser.parse_args()

    incentives = fetch_incentives(status="active", incentive_type="liquidity")
    rows = []
    scanned = 0
    for program in incentives:
        if scanned >= args.limit:
            break
        ticker = program.get("market_ticker") or program.get("ticker")
        if not ticker:
            continue
        category = {
            "category": program.get("category") or "",
            "event_ticker": program.get("event_ticker") or ticker,
        }
        if event_blocked(category):
            continue
        target = float(program.get("target_size") or program.get("target_size_fp") or 200)
        raw_df = program.get("discount_factor")
        if raw_df is None:
            raw_df = program.get("discount_factor_bps")
        discount = float(raw_df if raw_df is not None else 0.5)
        if discount > 1:
            discount = discount / 10000.0 if discount >= 100 else discount / 100.0
        row = score_market(ticker, target, discount, args.quote_size)
        row["period_reward_usd"] = period_reward_dollars(program.get("period_reward"))
        rows.append(row)
        scanned += 1

    args.out.parent.mkdir(parents=True, exist_ok=True)
    with args.out.open("a", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row) + "\n")

    shares = [r["share"] for r in rows if isinstance(r["share"], float)]
    csv_path = args.out.with_suffix(".csv")
    with csv_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=sorted({k for r in rows for k in r}))
        writer.writeheader()
        writer.writerows(rows)

    print(f"wrote {len(rows)} rows to {args.out}")
    print(f"scored_share_n={len(shares)} median={_median(shares)}")
    failed = [r for r in rows if r["share"] is None]
    print(f"unscored={len(failed)} (HTTP/parse/below-target — not coerced to 0)")
    return 0


def _median(values: list[float]) -> str:
    if not values:
        return "n/a"
    ordered = sorted(values)
    mid = len(ordered) // 2
    if len(ordered) % 2:
        return f"{ordered[mid]:.4f}"
    return f"{(ordered[mid - 1] + ordered[mid]) / 2:.4f}"


if __name__ == "__main__":
    raise SystemExit(main())
