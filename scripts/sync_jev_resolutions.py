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


def fetch_market_by_id(market_id: str) -> dict | None:
    """Fetch and verify an exact condition ID; title searches are not identity."""
    try:
        query = urllib.parse.urlencode({"condition_ids": market_id, "limit": 2})
        req = urllib.request.Request(f"{GAMMA_BASE}/markets?{query}", headers={"User-Agent": "JevResearch/1"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            markets = json.loads(resp.read().decode("utf-8"))
        matches = [m for m in markets if m.get("conditionId") == market_id]
        return matches[0] if len(matches) == 1 else None
    except Exception as exc:
        logger.debug("Market resolution lookup failed: %s", type(exc).__name__)
        return None


def parse_resolution_outcome(market: dict) -> float | None:
    """Require final UMA status and exact binary settlement, with explicit labels.

    Closed markets, proposed/disputed outcomes and 0.99 prices are not finality.
    Non-binary/split settlements are excluded from binary calibration.
    """
    if market.get("closed") is not True or market.get("umaResolutionStatus") != "resolved":
        return None
    try:
        outcomes = market.get("outcomes", [])
        prices = market.get("outcomePrices", [])
        outcomes = json.loads(outcomes) if isinstance(outcomes, str) else outcomes
        prices = json.loads(prices) if isinstance(prices, str) else prices
        if not isinstance(outcomes, list) or not isinstance(prices, list) or len(outcomes) != 2 or len(prices) != 2:
            return None
        labels = [str(label).strip().lower() for label in outcomes]
        if set(labels) != {"yes", "no"}:
            return None
        values = dict(zip(labels, map(float, prices)))
        if (values["yes"], values["no"]) in ((1.0, 0.0), (0.0, 1.0)):
            return values["yes"]
    except (ValueError, TypeError):
        logger.debug("Malformed settlement values")
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

    # Cache market lookups by stable condition ID to minimize API calls
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
        market_id = details.get("market_id") if isinstance(details, dict) else None
        if not isinstance(market_id, str) or not market_id:
            skipped_count += 1
            continue

        if market_id not in market_cache:
            market_cache[market_id] = fetch_market_by_id(market_id)

        mkt = market_cache[market_id]
        if not mkt:
            skipped_count += 1
            continue

        outcome = parse_resolution_outcome(mkt)
        if outcome is not None:
            # This is when finality was observed, never an invented event-resolution time.
            resolved_at = datetime.now(timezone.utc).isoformat()
            details["resolution_source"] = "gamma_final_uma"
            details["resolution_checked_at"] = resolved_at
            details["resolution_status"] = mkt["umaResolutionStatus"]
            details["resolution_outcomes"] = mkt.get("outcomes")
            details["resolution_prices"] = mkt.get("outcomePrices")
            if not dry_run:
                with db._lock, db.conn:
                    db.conn.execute(
                        "UPDATE jev_decisions SET resolved_outcome=?, resolved_at=?, details=? "
                        "WHERE id=? AND resolved_outcome IS NULL",
                        (outcome, resolved_at, json.dumps(details), dec["id"]),
                    )
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
