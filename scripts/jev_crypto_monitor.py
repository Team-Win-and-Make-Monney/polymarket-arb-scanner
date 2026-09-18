#!/usr/bin/env python3
"""Multi-Asset Jev System One Crypto Monitor (BTC, ETH, SOL, XRP).

Monitors live crypto prediction contracts on Polymarket against real-time
Binance.US spot prices, evaluating calibrated probabilities, edge, and
mispricings via TypeSafe's Jev-1.13 System One Decisions API.

All evaluations are logged to the TradeDB SQLite database (`jev_decisions` table)
for empirical calibration curves and Brier score tracking.

Usage:
    python scripts/jev_crypto_monitor.py --once
    python scripts/jev_crypto_monitor.py --interval 30
    python scripts/jev_crypto_monitor.py --stats
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
import urllib.request
from datetime import datetime, timezone

# Add project root to sys.path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config import OPENROUTER_API_KEY
from db import TradeDB
from jev_client import get_jev_client
from scans.jev_crypto import fetch_spot_prices, scan_jev_crypto

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("jev_monitor")


# ---------------------------------------------------------------------------
# Polymarket Crypto Market Fetcher
# ---------------------------------------------------------------------------


def fetch_polymarket_crypto_markets() -> dict[str, dict]:
    """Fetch active crypto strike markets from Polymarket Gamma API."""
    url = "https://gamma-api.polymarket.com/events?active=true&closed=false&order=volume24hr&ascending=false&limit=100"
    req = urllib.request.Request(
        url,
        headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            events = json.loads(resp.read().decode("utf-8"))
            markets_by_key = {}
            for ev in events:
                title = ev.get("title", "").lower()
                if any(w in title for w in ["bitcoin", "btc", "ethereum", "eth", "solana", "sol", "ripple", "xrp"]):
                    for mkt in ev.get("markets", []):
                        cid = mkt.get("conditionId") or mkt.get("condition_id") or mkt.get("question")
                        if cid:
                            markets_by_key[cid] = mkt
            return markets_by_key
    except Exception as e:
        logger.error("Failed to fetch Polymarket crypto markets: %s", e)
        return {}


# ---------------------------------------------------------------------------
# Calibration & Historical Statistics
# ---------------------------------------------------------------------------


def display_stats(db: TradeDB, asset_filter: str | None = None) -> None:
    """Display summary calibration stats from TradeDB."""
    records = db.get_jev_decisions(asset=asset_filter, limit=500)
    print("\n" + "=" * 75)
    print(" JEV SYSTEM ONE EMPIRICAL CALIBRATION DATABASE SUMMARY")
    print("=" * 75)

    if not records:
        print("No Jev decision records found in database.")
        print("=" * 75 + "\n")
        return

    print(f"Total Logged Decisions: {len(records)}")
    assets = sorted({r["asset"] for r in records})
    print(f"Covered Assets:         {', '.join(assets)}")

    # Breakdown by asset
    print("\nAsset Breakdown:")
    print(f"{'Asset':<8} {'Total':<8} {'Buy Yes':<10} {'Buy No':<10} {'Pass Fair':<10} {'Avg Conf':<10} {'Avg Edge'}")
    print("-" * 75)

    for asset in assets:
        asset_recs = [r for r in records if r["asset"] == asset]
        total = len(asset_recs)
        buy_yes = sum(1 for r in asset_recs if r["action"] == "buy_yes")
        buy_no = sum(1 for r in asset_recs if r["action"] == "buy_no")
        pass_fair = sum(1 for r in asset_recs if r["action"] == "pass_fair")
        confs = [r["confidence"] for r in asset_recs if r["confidence"] is not None]
        edges = [r["edge"] for r in asset_recs if r["edge"] is not None]

        avg_conf = (sum(confs) / len(confs)) if confs else 0.0
        avg_edge = (sum(edges) / len(edges)) if edges else 0.0

        print(
            f"{asset:<8} {total:<8} {buy_yes:<10} {buy_no:<10} {pass_fair:<10} "
            f"{avg_conf:<10.2f} {avg_edge:+.2%}"
        )

    print("-" * 75)
    print("Recent Decisions (Latest 5):")
    for r in records[:5]:
        ts = r["timestamp"][:19].replace("T", " ")
        m_prob = f"{r['market_prob']:.2f}" if r["market_prob"] is not None else "N/A"
        j_prob = f"{r['jev_prob']:.2f}" if r["jev_prob"] is not None else "N/A"
        edge_str = f"{r['edge']:+.2%}" if r["edge"] is not None else "N/A"
        print(
            f"  [{ts}] {r['asset']:<4} Strike=${r['strike']:<10.1f} Spot=${r['spot']:<10.1f} "
            f"Mkt={m_prob} Jev={j_prob} Action={r['action'].upper():<9} Edge={edge_str}"
        )
    print("=" * 75 + "\n")


# ---------------------------------------------------------------------------
# Monitoring Cycle
# ---------------------------------------------------------------------------


def run_monitor_cycle(db: TradeDB, asset_filter: str | None = None) -> None:
    """Run one monitoring iteration across spot venues, Polymarket, and Jev."""
    client = get_jev_client()
    if not client.is_available():
        logger.error("JevClient not available. Ensure OPENROUTER_API_KEY is configured.")
        return

    logger.info("Fetching Binance spot feeds for BTC, ETH, SOL, XRP...")
    spots = fetch_spot_prices()
    if not spots:
        logger.warning("Could not fetch spot prices. Retrying...")
        return

    spot_str = " | ".join(f"{a}: ${d['price']:,.2f} ({d['change_24h']:+.2f}%)" for a, d in spots.items())
    logger.info("Spot Prices: %s", spot_str)

    logger.info("Fetching Polymarket crypto markets...")
    markets = fetch_polymarket_crypto_markets()
    logger.info("Found %d active candidate contracts.", len(markets))

    if not markets:
        return

    # Filter markets by asset if specified
    if asset_filter:
        asset_norm = asset_filter.lower()
        markets = {
            k: v for k, v in markets.items()
            if asset_norm in v.get("question", "").lower()
        }
        logger.info("Filtered to %d markets matching %s.", len(markets), asset_filter)

    opps = scan_jev_crypto(
        markets_by_key=markets,
        spot_prices=spots,
        min_profit=0.005,
        jev_client=client,
        db=db,
    )

    print("\n" + "-" * 75)
    print(f" JEV DECISION SCAN RESULTS ({datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')})")
    print("-" * 75)
    if not opps:
        print("No mispriced opportunities passing deterministic risk/edge filters.")
        print("(Decisions and fair-pricing evaluations logged to database)")
    else:
        print(f"FOUND {len(opps)} STATISTICALLY MISPRICED OPPORTUNITIES:")
        for i, opp in enumerate(opps, 1):
            print(f"\n[{i}] {opp['market']}")
            print(f"    Action:     {opp['_action'].upper()}")
            print(f"    Exec Price: ${opp['_exec_price']:.3f} | Model Prob: {opp['_model_prob']:.1%}")
            print(f"    Confidence: {opp['_confidence']:.1%} | Risk Score: {opp['_risk_score']:.2f}")
            print(f"    Net Profit: ${opp['net_profit']:.2f} (ROI: {opp['net_roi']:.2%})")
            print(f"    Total Cost: {opp['total_cost']}")
    print("-" * 75 + "\n")


# ---------------------------------------------------------------------------
# CLI Entry Point
# ---------------------------------------------------------------------------


def main() -> None:
    """Parse CLI arguments and run monitor."""
    parser = argparse.ArgumentParser(description="Multi-Asset Jev Crypto Monitor (BTC, ETH, SOL, XRP)")
    parser.add_argument("--once", action="store_true", help="Run a single scan cycle and exit")
    parser.add_argument("--interval", type=int, default=60, help="Interval in seconds between scans (default: 60)")
    parser.add_argument("--stats", action="store_true", help="Display calibration statistics from database and exit")
    parser.add_argument("--asset", type=str, default=None, help="Filter by specific asset (BTC, ETH, SOL, XRP)")
    args = parser.parse_args()

    db = TradeDB()

    if args.stats:
        display_stats(db, asset_filter=args.asset)
        return

    logger.info("Starting Jev Multi-Asset Crypto Monitor...")
    logger.info("Target Assets: BTC, ETH, SOL, XRP")

    if args.once:
        run_monitor_cycle(db, asset_filter=args.asset)
        display_stats(db, asset_filter=args.asset)
        return

    logger.info("Entering continuous monitoring loop (interval: %ds). Press Ctrl+C to stop.", args.interval)
    try:
        while True:
            run_monitor_cycle(db, asset_filter=args.asset)
            time.sleep(args.interval)
    except KeyboardInterrupt:
        logger.info("Monitor interrupted by operator. Exiting.")
    finally:
        db.close()


if __name__ == "__main__":
    main()
