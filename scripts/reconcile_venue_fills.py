#!/usr/bin/env python3
"""Reconcile one venue account's fills for one America/Detroit day.

Read-only toward the venue: the Kalshi transport allows only GET on
/portfolio/fills, /historical/fills and /historical/cutoff. Reads the
Supabase ledger mirror, never the local trades.db. Prints the reconciliation
row as JSON; writes it to ledger_venue_reconciliations only with --write.

Run it as its own process (its own Kalshi rate limiter and circuit breaker),
never inside a trading service.

Environment:
  LEDGER_KALSHI_SCOPE   operator-verified scope JSON (see
                        kalshi_fill_collector.parse_kalshi_scope); non-secret
  KALSHI_API_KEY_ID + KALSHI_PRIVATE_KEY_PATH|KALSHI_PRIVATE_KEY_BASE64
                        existing Kalshi credential (read endpoints only)
  SUPABASE_URL + SUPABASE_SERVICE_KEY
                        existing backend credential for the mirror
  LEDGER_RECON_FINALITY_SECONDS  seconds after a day ends before it is checked
                                 (default 900; finite, >= 0)
  LEDGER_RECON_MAX_SOURCE_AGE_SECONDS
                                 a ledger source must have exported successfully
                                 within this many seconds (default 3600; finite, > 0)

Examples:
  # Print the key fingerprint to put in LEDGER_KALSHI_SCOPE (no network)
  python scripts/reconcile_venue_fills.py --print-key-fingerprint
  # Check yesterday (Detroit), print only
  python scripts/reconcile_venue_fills.py --venue kalshi --day 2026-09-28
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import os
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from kalshi_fill_collector import (  # noqa: E402
    VENUE,
    KalshiFillCollector,
    key_fingerprint,
    parse_kalshi_scope,
    read_only_transport,
)
from venue_reconciliation import (  # noqa: E402
    DEFAULT_MAX_SOURCE_AGE_SECONDS,
    REPORTING_TZ,
    PostgrestLedgerMirror,
    run_reconciliation,
)

logger = logging.getLogger("reconcile_venue_fills")


def _yesterday_local() -> date:
    return (datetime.now(ZoneInfo(REPORTING_TZ)) - timedelta(days=1)).date()


def _env_seconds(name: str, default: float, *, positive: bool) -> float:
    """A finite, non-negative (or positive) number of seconds from the environment."""
    raw = os.getenv(name)
    try:
        value = float(raw) if raw not in (None, "") else float(default)
    except ValueError:
        raise ValueError(f"{name} is not a number") from None
    if not math.isfinite(value) or value < 0 or (positive and value == 0):
        raise ValueError(f"{name} must be finite and {'> 0' if positive else '>= 0'}")
    return value


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--venue", choices=[VENUE], default=VENUE)
    parser.add_argument("--day", type=date.fromisoformat, default=None,
                        help=f"{REPORTING_TZ} calendar day (default: yesterday)")
    parser.add_argument("--write", action="store_true",
                        help="persist the row to ledger_venue_reconciliations")
    parser.add_argument("--print-key-fingerprint", action="store_true",
                        help="print the fingerprint of KALSHI_API_KEY_ID and exit")
    args = parser.parse_args(argv)
    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"), format="%(levelname)s %(name)s %(message)s")

    if args.print_key_fingerprint:
        key_id = os.getenv("KALSHI_API_KEY_ID")
        if not key_id:
            print("KALSHI_API_KEY_ID is not set", file=sys.stderr)
            return 2
        print(key_fingerprint(key_id))
        return 0

    try:
        scope = parse_kalshi_scope(json.loads(os.getenv("LEDGER_KALSHI_SCOPE") or "null"))
    except (ValueError, json.JSONDecodeError) as exc:
        print(f"LEDGER_KALSHI_SCOPE is missing or invalid: {exc}", file=sys.stderr)
        return 2
    try:
        lag = _env_seconds("LEDGER_RECON_FINALITY_SECONDS", 900, positive=False)
        max_age = _env_seconds("LEDGER_RECON_MAX_SOURCE_AGE_SECONDS", DEFAULT_MAX_SOURCE_AGE_SECONDS, positive=True)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    url, key = os.getenv("SUPABASE_URL"), os.getenv("SUPABASE_SERVICE_KEY")
    if not url or not key:
        print("SUPABASE_URL and SUPABASE_SERVICE_KEY must be set", file=sys.stderr)
        return 2

    import kalshi_api

    client = kalshi_api.build_client_from_env()
    if client is None:
        print("Kalshi credentials missing or authentication failed", file=sys.stderr)
        return 2
    collector = KalshiFillCollector(read_only_transport(client), client.api_key_id, scope,
                                    finality_lag_seconds=lag)
    mirror = PostgrestLedgerMirror(url, key)
    day = args.day or _yesterday_local()
    record = run_reconciliation(collector, scope, mirror, day, venue=args.venue, finality_lag_seconds=lag,
                                max_source_age_seconds=max_age)
    print(json.dumps(record, indent=2, sort_keys=True, default=str))
    if args.write:
        mirror.write_reconciliation(record)
        logger.info("Wrote reconciliation %s (%s)", record["run_id"], record["status"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
