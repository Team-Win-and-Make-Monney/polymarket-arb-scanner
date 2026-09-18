#!/usr/bin/env python3
"""Sync Polymarket resolution outcomes for logged Jev decisions.

Queries unresolved decision records in `jev_decisions` from SQLite,
checks Polymarket Gamma API for closed/settled status, and updates
records with ground-truth binary outcomes (1.0 = YES, 0.0 = NO).

Usage:
    python scripts/sync_jev_resolutions.py [--dry-run] [--limit 100]
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config import DATA_DIR
from db import TradeDB

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)

GAMMA_BASE = "https://gamma-api.polymarket.com"


def fetch_market_by_question(question: str) -> dict | None:
    """Fetch market state from Polymarket Gamma API by question string."""
    try:
        encoded_q = urllib.parse.quote(question)
        url = f"{GAMMA_BASE}/markets?search={encoded_q}&limit=5"
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            markets = json.loads(resp.read().decode("utf-8"))
            if not markets:
                return None
            for m in markets:
                if m.get("question", "").strip().lower() == question.strip().lower():
                    return m
            return None
    except Exception as e:
        logger.debug("Failed to fetch market for question '%s': %s", question, e)
        return None


def parse_resolution_outcome(market: dict) -> float | None:
    """Parse ground-truth resolution outcome from a Polymarket market dict.

    Returns:
        1.0 for Yes, 0.0 for No, or None if still active/unresolved.
    """
    if not market.get("closed") and not market.get("resolvedOutcome"):
        return None

    res_outcome = market.get("resolvedOutcome")
    if res_outcome is not None:
        val_str = str(res_outcome).strip().lower()
        if val_str in ("yes", "1", "true"):
            return 1.0
        elif val_str in ("no", "0", "false"):
            return 0.0

    raw_prices = market.get("outcomePrices")
    if raw_prices:
        try:
            prices = json.loads(raw_prices) if isinstance(raw_prices, str) else raw_prices
            yes_p = float(prices[0])
            no_p = float(prices[1])
            if yes_p >= 0.99:
                return 1.0
            if no_p >= 0.99:
                return 0.0
        except (ValueError, IndexError, TypeError):
            pass

    tokens = market.get("tokens", [])
    if len(tokens) >= 2:
        if tokens[0].get("winner") is True:
            return 1.0
        if tokens[1].get("winner") is True:
            return 0.0

    return None


def sync_resolutions(
    db: TradeDB,
    dry_run: bool = False,
    limit: int = 100,
) -> dict[str, int]:
    """Check and update resolutions for pending Jev decisions.

    Args:
        db: TradeDB instance.
        dry_run: If True, do not commit changes to database.
        limit: Max unresolved decisions to process.

    Returns:
        Summary dict with checked, resolved, and skipped counts.
    """
    with db._lock:
        cur = db.conn.execute(
            """SELECT id, asset, strike, details FROM jev_decisions
               WHERE resolved_outcome IS NULL
               ORDER BY id ASC LIMIT ?""",
            (limit,),
        )
        unresolved = [dict(r) for r in cur.fetchall()]

    if not unresolved:
        logger.info("No unresolved Jev decisions found.")
        return {"checked": 0, "resolved": 0, "skipped": 0}

    logger.info("Checking %d unresolved decisions for settlement...", len(unresolved))

    # Cache market lookups by question to minimize API calls
    market_cache: dict[str, dict | None] = {}
    resolved_count = 0
    skipped_count = 0

    for dec in unresolved:
        details_raw = dec.get("details") or ""
        try:
            details = json.loads(details_raw) if isinstance(details_raw, str) else details_raw
        except Exception:
            details = {}

        question = details.get("question") if isinstance(details, dict) else None
        if not question:
            skipped_count += 1
            continue

        if question not in market_cache:
            market_cache[question] = fetch_market_by_question(question)

        mkt = market_cache[question]
        if not mkt:
            skipped_count += 1
            continue

        outcome = parse_resolution_outcome(mkt)
        if outcome is not None:
            resolved_at = mkt.get("endDate") or datetime.now(timezone.utc).isoformat()
            if not dry_run:
                db.update_jev_resolution(dec["id"], outcome=outcome, resolved_at=resolved_at)
            outcome_label = "YES (1.0)" if outcome == 1.0 else "NO (0.0)"
            logger.info("Decision #%d resolved: %s -> %s", dec["id"], question, outcome_label)
            resolved_count += 1
        else:
            skipped_count += 1

    logger.info(
        "Resolution sync complete: %d checked, %d resolved, %d still open.",
        len(unresolved),
        resolved_count,
        skipped_count,
    )

    return {
        "checked": len(unresolved),
        "resolved": resolved_count,
        "skipped": skipped_count,
    }


def main():
    parser = argparse.ArgumentParser(description="Sync Polymarket resolution outcomes for Jev decisions")
    parser.add_argument("--dry-run", action="store_true", help="Inspect without updating database")
    parser.add_argument("--limit", type=int, default=100, help="Max decisions to evaluate")
    parser.add_argument("--db", type=str, default=None, help="Custom SQLite database path")
    args = parser.parse_args()

    db_path = args.db or os.path.join(DATA_DIR, "trades.db")
    db = TradeDB(db_path)
    try:
        sync_resolutions(db, dry_run=args.dry_run, limit=args.limit)
    finally:
        db.close()


if __name__ == "__main__":
    main()
