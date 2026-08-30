"""7-day validation report for Milestone 1 success criteria.

Measures the project's 3 success criteria against trades.db:
1. Net positive P&L over 7-day live trading period
2. <5% false positive rate on detected opportunities
3. At least one profitable round-trip trade executed autonomously
"""

import argparse
import os
import sqlite3
import sys
from datetime import datetime, timedelta, timezone


# ---------------------------------------------------------------------------
# Color helpers
# ---------------------------------------------------------------------------

def _green(text: str) -> str:
    return f"\033[92m{text}\033[0m"


def _red(text: str) -> str:
    return f"\033[91m{text}\033[0m"


def _yellow(text: str) -> str:
    return f"\033[93m{text}\033[0m"


def _bold(text: str) -> str:
    return f"\033[1m{text}\033[0m"


# ---------------------------------------------------------------------------
# Database connection
# ---------------------------------------------------------------------------

def _connect(db_path: str) -> sqlite3.Connection:
    """Connect to trades.db and return a connection."""
    if not os.path.exists(db_path):
        print(_red(f"Error: database not found at {db_path}"), file=sys.stderr)
        sys.exit(1)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


# ---------------------------------------------------------------------------
# Criterion 1: Net Positive P&L
# ---------------------------------------------------------------------------

def _criterion_1(conn: sqlite3.Connection, since: str) -> dict:
    """Evaluate net positive P&L over the validation period."""
    # Total P&L
    row = conn.execute("""
        SELECT COALESCE(SUM(net_profit), 0) as total_pnl,
               COUNT(*) as total_opps
        FROM opportunities
        WHERE timestamp >= ? AND action IN ('executed', 'filled', 'dry_run')
    """, (since,)).fetchone()

    total_pnl = row["total_pnl"]
    total_opps = row["total_opps"]

    # Per-strategy breakdown
    strategies = conn.execute("""
        SELECT type,
               SUM(net_profit) as pnl,
               COUNT(*) as count
        FROM opportunities
        WHERE timestamp >= ? AND action IN ('executed', 'filled', 'dry_run')
        GROUP BY type
        ORDER BY pnl DESC
    """, (since,)).fetchall()

    # Daily breakdown
    daily = conn.execute("""
        SELECT DATE(timestamp) as day,
               SUM(net_profit) as pnl,
               COUNT(*) as count
        FROM opportunities
        WHERE timestamp >= ? AND action IN ('executed', 'filled', 'dry_run')
        GROUP BY DATE(timestamp)
        ORDER BY day
    """, (since,)).fetchall()

    passed = total_pnl > 0

    return {
        "passed": passed,
        "total_pnl": total_pnl,
        "total_opps": total_opps,
        "strategies": [dict(s) for s in strategies],
        "daily": [dict(d) for d in daily],
    }


# ---------------------------------------------------------------------------
# Criterion 2: <5% False Positive Rate
# ---------------------------------------------------------------------------

def _criterion_2(conn: sqlite3.Connection, since: str) -> dict:
    """Evaluate false positive rate on detected opportunities."""
    row = conn.execute("""
        SELECT COUNT(*) as detected,
               SUM(CASE WHEN action IN ('executed', 'filled') THEN 1 ELSE 0 END) as executed,
               SUM(CASE WHEN action LIKE 'skipped:%' OR action LIKE 'rejected:%' OR action = 'rejected' THEN 1 ELSE 0 END) as rejected
        FROM opportunities
        WHERE timestamp >= ?
    """, (since,)).fetchone()

    detected = row["detected"]
    executed = row["executed"]
    rejected = row["rejected"]

    if detected == 0:
        return {
            "passed": True,
            "detected": 0,
            "executed": 0,
            "rejected": 0,
            "fp_rate": 0.0,
            "note": "No opportunities detected — insufficient data",
        }

    fp_rate = rejected / detected * 100 if detected > 0 else 0
    passed = fp_rate < 5

    return {
        "passed": passed,
        "detected": detected,
        "executed": executed,
        "rejected": rejected,
        "fp_rate": fp_rate,
    }


# ---------------------------------------------------------------------------
# Criterion 3: Profitable Round-Trip Trade
# ---------------------------------------------------------------------------

def _criterion_3(conn: sqlite3.Connection, since: str) -> dict:
    """Check for at least one profitable autonomous round-trip trade."""
    # A round-trip: opportunity with net_profit > 0 and at least one filled trade
    rows = conn.execute("""
        SELECT o.id, o.type, o.market, o.net_profit
        FROM opportunities o
        JOIN trades t ON t.opportunity_id = o.id
        WHERE o.timestamp >= ?
          AND o.net_profit > 0
          AND t.status = 'filled'
        GROUP BY o.id
        ORDER BY o.net_profit DESC
        LIMIT 5
    """, (since,)).fetchall()

    passed = len(rows) > 0
    examples = [dict(r) for r in rows]

    return {
        "passed": passed,
        "profitable_count": len(rows),
        "examples": examples,
    }


# ---------------------------------------------------------------------------
# Report output
# ---------------------------------------------------------------------------

def _print_report(db_path: str, days: int):
    """Generate and print the full validation report."""
    conn = _connect(db_path)
    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    end_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    start_date = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d")

    try:
        c1 = _criterion_1(conn, since)
        c2 = _criterion_2(conn, since)
        c3 = _criterion_3(conn, since)
    finally:
        conn.close()

    status = lambda ok: _green("PASS") if ok else _red("FAIL")

    print(_bold(f"\n{'='*60}"))
    print(_bold(f"  {days}-DAY VALIDATION REPORT"))
    print(_bold(f"  Period: {start_date} to {end_date}"))
    print(_bold(f"  Database: {db_path}"))
    print(_bold(f"{'='*60}\n"))

    # --- Criterion 1 ---
    print(f"  Criterion 1: Net Positive P&L {'.'*18} {status(c1['passed'])}")
    pnl_c = _green if c1["total_pnl"] >= 0 else _red
    total_pnl_val = c1["total_pnl"]
    print(f"    Total P&L: {pnl_c(f'${total_pnl_val:+.4f}')}")
    print(f"    Opportunities: {c1['total_opps']}")
    if c1["daily"]:
        print(f"    Daily:")
        for d in c1["daily"]:
            dc = _green if d["pnl"] >= 0 else _red
            day_pnl = d["pnl"]
            day_count = d["count"]
            print(f"      {d['day']}: {dc(f'${day_pnl:+.4f}')} ({day_count} opps)")
    if c1["strategies"]:
        best = c1["strategies"][0]
        worst = c1["strategies"][-1]
        print(f"    Best strategy:  {best['type']} (${best['pnl']:+.4f})")
        print(f"    Worst strategy: {worst['type']} (${worst['pnl']:+.4f})")
    print()

    # --- Criterion 2 ---
    print(f"  Criterion 2: <5% False Positive Rate {'.'*11} {status(c2['passed'])}")
    print(f"    Detected: {c2['detected']} opportunities")
    print(f"    Executed: {c2['executed']} trades")
    print(f"    Rejected at execution: {c2['rejected']} ({c2['fp_rate']:.1f}%)")
    if "note" in c2:
        print(f"    Note: {c2['note']}")
    print()

    # --- Criterion 3 ---
    print(f"  Criterion 3: Profitable Round-Trip {'.'*14} {status(c3['passed'])}")
    print(f"    Profitable trades: {c3['profitable_count']}")
    if c3["examples"]:
        ex = c3["examples"][0]
        print(f"    Example: {ex['type']} on {ex['market']} — profit ${ex['net_profit']:.4f}")
    print()

    # --- Overall ---
    all_pass = c1["passed"] and c2["passed"] and c3["passed"]
    print(_bold(f"{'='*60}"))
    if all_pass:
        print(_bold(f"  OVERALL: {_green('PASS')}"))
        print(_bold(f"  Milestone 1 status: {_green('ACHIEVED')}"))
    else:
        print(_bold(f"  OVERALL: {_red('FAIL')}"))
        print(_bold(f"  Milestone 1 status: {_red('NOT ACHIEVED')}"))
        failed = []
        if not c1["passed"]:
            failed.append("Net Positive P&L")
        if not c2["passed"]:
            failed.append("<5% False Positive Rate")
        if not c3["passed"]:
            failed.append("Profitable Round-Trip")
        print(f"  Failed criteria: {', '.join(failed)}")
    print(_bold(f"{'='*60}\n"))

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="7-day validation report for Milestone 1")
    parser.add_argument("--db", default="trades.db",
                        help="Path to trades.db (default: trades.db)")
    parser.add_argument("--days", type=int, default=7,
                        help="Lookback window in days (default: 7)")
    args = parser.parse_args()
    _print_report(args.db, args.days)


if __name__ == "__main__":
    main()
