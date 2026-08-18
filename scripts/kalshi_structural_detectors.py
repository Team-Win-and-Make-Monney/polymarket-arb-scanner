#!/usr/bin/env python3
"""Live-legal Kalshi structural detectors (public data, no orders).

Flags complete-set underprice, late-window unpinned binaries, and parlay-shaped
events on Kalshi non-sports books. Output is a selection feed for the D0 MM
pilot — not a paper-producer product and not an order path.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from kalshi_policy import event_blocked  # noqa: E402

EVENTS_URL = "https://api.elections.kalshi.com/trade-api/v2/events"


def _get_json(url: str) -> dict:
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _hours_to_close(market: dict) -> float | None:
    close = market.get("close_time") or market.get("expected_expiration_time")
    if not close:
        return None
    try:
        when = dt.datetime.fromisoformat(str(close).replace("Z", "+00:00"))
    except ValueError:
        return None
    now = dt.datetime.now(dt.timezone.utc)
    if when.tzinfo is None:
        when = when.replace(tzinfo=dt.timezone.utc)
    return (when - now).total_seconds() / 3600.0


def detect_event(event: dict) -> list[dict]:
    flags: list[dict] = []
    if event_blocked(event):
        return flags
    markets = event.get("markets") or []
    yes_asks = []
    for market in markets:
        ticker = market.get("ticker")
        yes_ask = market.get("yes_ask") or market.get("yes_ask_dollars")
        try:
            yes_ask_f = float(yes_ask) if yes_ask is not None else None
        except (TypeError, ValueError):
            yes_ask_f = None
        if yes_ask_f is not None and yes_ask_f > 1:
            yes_ask_f = yes_ask_f / 100.0
        hours = _hours_to_close(market)
        if (
            yes_ask_f is not None
            and hours is not None
            and hours <= 6
            and 0.05 < yes_ask_f < 0.95
        ):
            flags.append({
                "kind": "late_window_unpinned",
                "event_ticker": event.get("event_ticker"),
                "ticker": ticker,
                "yes_ask": yes_ask_f,
                "hours_to_close": round(hours, 2),
            })
        if yes_ask_f is not None:
            yes_asks.append(yes_ask_f)
    if len(yes_asks) >= 2:
        total = sum(yes_asks)
        if total < 0.98:
            flags.append({
                "kind": "complete_set_underprice",
                "event_ticker": event.get("event_ticker"),
                "yes_ask_sum": round(total, 4),
                "n_outcomes": len(yes_asks),
            })
    title = (event.get("title") or "").lower()
    if "parlay" in title or "same game" in title or "sgp" in title:
        flags.append({
            "kind": "parlay_shaped",
            "event_ticker": event.get("event_ticker"),
            "title": event.get("title"),
        })
    return flags


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, default=200)
    parser.add_argument("--out", type=Path, default=Path("artifacts/kalshi-structural-live.jsonl"))
    args = parser.parse_args()

    flags: list[dict] = []
    cursor = None
    fetched = 0
    while fetched < args.limit:
        params = {"limit": min(200, args.limit - fetched), "with_nested_markets": "true", "status": "open"}
        if cursor:
            params["cursor"] = cursor
        url = f"{EVENTS_URL}?{urllib.parse.urlencode(params)}"
        payload = _get_json(url)
        events = payload.get("events") or []
        if not events:
            break
        for event in events:
            flags.extend(detect_event(event))
        fetched += len(events)
        cursor = payload.get("cursor")
        if not cursor:
            break

    args.out.parent.mkdir(parents=True, exist_ok=True)
    with args.out.open("a", encoding="utf-8") as handle:
        for row in flags:
            row["ts_utc"] = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            handle.write(json.dumps(row) + "\n")

    counts: dict[str, int] = {}
    for row in flags:
        counts[row["kind"]] = counts.get(row["kind"], 0) + 1
    print(f"wrote {len(flags)} flags to {args.out} counts={counts}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
