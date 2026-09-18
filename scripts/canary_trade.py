#!/usr/bin/env python3
"""Paper / tiny-live canary on real Polymarket↔Kalshi matched pairs.

Stages:
  paper  — scan live books, build legs, log to SQLite as dry_run (no orders)
  live   — place real orders with hard size/count caps (requires ACK)

Examples:
  # Paper canary (safe — no orders)
  infisical run --env=dev -- python scripts/canary_trade.py --mode paper

  # Tiny live canary (real money, max $1, max 1 trade)
  infisical run --env=dev -- python scripts/canary_trade.py --mode live \\
      --max-trade 1 --max-trades 1 \\
      --ack I_ACCEPT_LIVE_CANARY
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

logger = logging.getLogger("canary_trade")

_LIVE_ACK = "I_ACCEPT_LIVE_CANARY"


def _parse_roi(opp: dict) -> float:
    roi = opp.get("net_roi", 0)
    if isinstance(roi, str):
        return float(roi.replace("%", "").strip() or 0) / 100.0
    return float(roi or 0)


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Paper/live canary on matched PM↔Kalshi pairs")
    p.add_argument(
        "--mode",
        choices=["paper", "live"],
        required=True,
        help="paper = log only; live = place real orders under hard caps",
    )
    p.add_argument("--max-trade", type=float, default=None, help="Max $ per trade (default: 1.0)")
    p.add_argument("--max-trades", type=int, default=None, help="Max opportunities to act on (default: 1)")
    p.add_argument("--min-roi", type=float, default=None, help="Min net ROI as decimal (default: 0.01)")
    p.add_argument(
        "--min-confidence",
        choices=["LOW", "MEDIUM", "HIGH"],
        default="LOW",
        help="Fuzzy match confidence floor (default: LOW)",
    )
    p.add_argument(
        "--ack",
        default="",
        help=f"Required for --mode live: {_LIVE_ACK}",
    )
    p.add_argument(
        "--db",
        default=None,
        help="SQLite path (default: DATA_DIR/canary_trades.db)",
    )
    p.add_argument("--json", action="store_true", help="Emit machine-readable summary")
    return p.parse_args()


def main() -> int:
    args = _parse_args()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    # Keep POLYMARKET_PROXY_URL for CLOB geo, but do not let a global HTTP(S)_PROXY
    # (sometimes injected by shells/tools) route Kalshi through the Ireland proxy.
    for _k in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
        os.environ.pop(_k, None)

    # Apply canary env BEFORE importing config/executor so validate_config sees them.
    max_trade = args.max_trade if args.max_trade is not None else float(
        os.getenv("CANARY_MAX_TRADE_SIZE", "1.0")
    )
    max_trades = args.max_trades if args.max_trades is not None else int(
        os.getenv("CANARY_MAX_TRADES", "1")
    )
    min_roi = args.min_roi if args.min_roi is not None else float(
        os.getenv("CANARY_MIN_NET_ROI", "0.01")
    )

    if args.mode == "live":
        if args.ack != _LIVE_ACK and os.getenv("CANARY_LIVE_ACK") != _LIVE_ACK:
            logger.error(
                "Live canary refused: pass --ack %s (or set CANARY_LIVE_ACK)",
                _LIVE_ACK,
            )
            return 2
        if max_trade > 5.0:
            logger.error("Live canary hard cap: --max-trade must be <= 5.0 (got %s)", max_trade)
            return 2
        if max_trades > 3:
            logger.error("Live canary hard cap: --max-trades must be <= 3 (got %s)", max_trades)
            return 2
        os.environ["CANARY_LIVE_ACK"] = _LIVE_ACK
        os.environ["DRY_RUN"] = "false"
        os.environ["EXECUTION_MODE"] = "full-auto"
    else:
        os.environ["DRY_RUN"] = "true"
        os.environ["EXECUTION_MODE"] = "full-auto"

    os.environ["CANARY_MODE"] = args.mode
    os.environ["CANARY_MAX_TRADE_SIZE"] = str(max_trade)
    os.environ["CANARY_MAX_TRADES"] = str(max_trades)
    os.environ["CANARY_MIN_NET_ROI"] = str(min_roi)
    os.environ.setdefault("CANARY_PLATFORMS", "polymarket,kalshi")
    os.environ.setdefault("ENABLED_EXECUTION_PLATFORMS", "polymarket,kalshi")
    # Paper canary needs a wider resolution window than the 7-day production
    # arb filter — otherwise matched pairs with later end dates never surface.
    # Force-overwrite: Infisical may inject MAX_RESOLUTION_DAYS=7.
    if args.mode == "paper":
        os.environ["MAX_RESOLUTION_DAYS"] = "90"

    data_dir = os.getenv("DATA_DIR", str(ROOT))
    db_path = args.db or os.path.join(data_dir, "canary_trades.db")
    os.environ["DATA_DIR"] = str(Path(db_path).parent)

    # Late imports after env is set
    import config  # noqa: F401 — validate_config at import
    from config import (
        CANARY_MAX_TRADE_SIZE,
        CANARY_MAX_TRADES,
        CANARY_MIN_NET_ROI,
        CANARY_MODE,
    )
    from db import TradeDB
    from executor import ArbitrageExecutor
    from kalshi_api import KalshiClient
    from polymarket_api import PolymarketTrader, fetch_all_markets
    from risk_manager import RiskManager
    from scans.cross import scan_cross_platform
    from scans.helpers import capital_efficiency_score

    logger.info(
        "=== CANARY %s | max_trade=$%.2f | max_trades=%d | min_roi=%.2f%% | db=%s ===",
        CANARY_MODE.upper(),
        CANARY_MAX_TRADE_SIZE,
        CANARY_MAX_TRADES,
        CANARY_MIN_NET_ROI * 100,
        db_path,
    )

    db = TradeDB(db_path)
    risk = RiskManager({
        "max_trade_size": CANARY_MAX_TRADE_SIZE,
        "daily_loss_limit": float(os.getenv("DAILY_LOSS_LIMIT", "5.0")),
        "max_open_positions": int(os.getenv("MAX_OPEN_POSITIONS", "2")),
        "max_daily_trades": CANARY_MAX_TRADES,
        "min_liquidity": float(os.getenv("MIN_LIQUIDITY", "10.0")),
        "min_liquidity_high_roi": float(os.getenv("MIN_LIQUIDITY_HIGH_ROI", "5.0")),
        "min_net_roi": CANARY_MIN_NET_ROI,
        "allow_better_reentry": False,
        "reentry_improvement_threshold": 0.20,
    })

    # Kalshi (needed for scan + optional live)
    kalshi = KalshiClient()
    key_id = os.getenv("KALSHI_API_KEY_ID")
    key_path = os.getenv("KALSHI_PRIVATE_KEY_PATH")
    key_b64 = os.getenv("KALSHI_PRIVATE_KEY_BASE64")
    if not key_id or not (key_path or key_b64):
        logger.error("Kalshi credentials missing")
        return 1
    if not kalshi.login_with_api_key(key_id, private_key_path=key_path, private_key_base64=key_b64):
        logger.error("Kalshi auth failed")
        return 1
    logger.info("Kalshi authenticated.")

    pm_trader = None
    if args.mode == "live":
        pk = os.getenv("POLYMARKET_PRIVATE_KEY")
        if not pk:
            logger.error("POLYMARKET_PRIVATE_KEY required for live canary")
            return 1
        funder = os.getenv("POLYMARKET_FUNDER_ADDRESS")
        sig = int(os.getenv("POLYMARKET_SIGNATURE_TYPE", "0"))
        pm_trader = PolymarketTrader(pk, chain_id=137, funder=funder, signature_type=sig)
        bal = pm_trader.get_balance()
        logger.info("Polymarket trader ready (balance=%s, sig=%d)", bal, sig)

    executor = ArbitrageExecutor(
        pm_trader=pm_trader,
        kalshi_client=kalshi,
        db=db,
        risk_manager=risk,
        dry_run=(args.mode == "paper"),
        exec_mode="full-auto",
        max_trade_size=CANARY_MAX_TRADE_SIZE,
        concurrent_execution=False,  # sequential for canary observability
        dynamic_sizing=False,
    )

    logger.info("Fetching Polymarket markets...")
    # Gamma caps page size ~100; requesting limit>page_size makes fetch_all_markets
    # stop after page 1 (len(page) < limit). Use limit=100 + many pages.
    poly_markets = fetch_all_markets(limit=100, max_pages=20)
    if not poly_markets:
        logger.error("No Polymarket markets fetched")
        return 1
    logger.info("Fetched %d Polymarket markets", len(poly_markets))

    logger.info("Scanning Polymarket↔Kalshi matched pairs...")
    # Temporarily lower min_profit to 0 so we can observe mid-price near-misses
    # that CLOB refine drops; live mode still only executes when ROI clears the gate.
    scan_min = 0.0 if args.mode == "paper" else CANARY_MIN_NET_ROI
    opps = scan_cross_platform(
        poly_markets,
        kalshi,
        min_profit=scan_min,
        min_confidence=args.min_confidence,
    )
    logger.info("Scan returned %d cross opportunities (after CLOB refine)", len(opps))

    opps = sorted(opps, key=capital_efficiency_score, reverse=True)

    summary: list[dict] = []
    acted = 0

    # Paper observability: if refine dropped everything, still log the best
    # mid-price near-misses by re-scanning with a patched refine that keeps mids.
    if args.mode == "paper" and not opps:
        logger.info(
            "No post-refine opps — logging mid-price near-misses for paper evidence"
        )
        from unittest.mock import patch as _patch

        def _identity_refine(candidates, *a, **kw):
            return candidates

        with _patch("scans.cross._refine_cross_with_clob", side_effect=_identity_refine):
            mid_opps = scan_cross_platform(
                poly_markets,
                kalshi,
                min_profit=0.0,
                min_confidence=args.min_confidence,
            )
        mid_opps = sorted(mid_opps, key=capital_efficiency_score, reverse=True)[:CANARY_MAX_TRADES]
        for opp in mid_opps:
            legs = executor._build_legs(opp, CANARY_MAX_TRADE_SIZE)
            if not legs:
                continue
            opp_id = db.log_opportunity(
                opp_type=opp.get("type", "Cross"),
                market=opp.get("market", ""),
                prices=opp.get("prices", ""),
                total_cost=float(str(opp.get("total_cost", "0")).replace("$", "") or 0),
                net_profit=float(opp.get("net_profit", 0) or 0),
                net_roi=_parse_roi(opp),
                depth=float(opp.get("_clob_depth", 0) or 0),
                action="paper_near_miss",
            )
            for leg in legs:
                db.log_trade(
                    opportunity_id=opp_id,
                    platform=leg["platform"],
                    side=leg.get("side", ""),
                    price=leg.get("price", 0),
                    size=CANARY_MAX_TRADE_SIZE,
                    status="paper_near_miss",
                )
            summary.append({
                "market": opp.get("market"),
                "type": opp.get("type"),
                "prices": opp.get("prices"),
                "net_profit": opp.get("net_profit"),
                "net_roi": opp.get("net_roi"),
                "executed": False,
                "note": "mid-price candidate; would be dropped at CLOB ask",
                "legs": [
                    {
                        "platform": leg.get("platform"),
                        "side": leg.get("side"),
                        "price": leg.get("price"),
                        "ticker": leg.get("_ticker"),
                    }
                    for leg in legs
                ],
            })
            acted += 1
            logger.info(
                "Paper near-miss #%d: %s | %s | profit=%s roi=%s",
                acted, opp.get("market"), opp.get("prices"),
                opp.get("net_profit"), opp.get("net_roi"),
            )

    attempts = 0
    for opp in opps:
        if acted >= CANARY_MAX_TRADES or attempts >= CANARY_MAX_TRADES:
            break
        roi = _parse_roi(opp)
        if roi < CANARY_MIN_NET_ROI:
            continue
        platforms = set()
        # Peek legs without executing
        legs = executor._build_legs(opp, CANARY_MAX_TRADE_SIZE)
        if not legs or len(legs) < 2:
            continue
        platforms = {leg.get("platform") for leg in legs}
        if platforms != {"polymarket", "kalshi"}:
            continue

        attempts += 1
        row = {
            "market": opp.get("market"),
            "type": opp.get("type"),
            "prices": opp.get("prices"),
            "net_profit": opp.get("net_profit"),
            "net_roi": opp.get("net_roi"),
            "legs": [
                {
                    "platform": leg.get("platform"),
                    "side": leg.get("side"),
                    "price": leg.get("price"),
                    "token": leg.get("token"),
                    "ticker": leg.get("_ticker"),
                }
                for leg in legs
            ],
        }
        logger.info(
            "Candidate #%d: %s | roi=%s profit=%s | legs=%s",
            acted + 1,
            opp.get("market"),
            opp.get("net_roi"),
            opp.get("net_profit"),
            row["legs"],
        )

        ok = executor.execute(opp)
        row["executed"] = bool(ok)
        # Attach order IDs from DB if any
        if ok:
            # Most recent opportunity for this market
            trades = []
            try:
                with db._lock:
                    cur = db.conn.execute(
                        """SELECT id, platform, status, order_id, fill_price, fill_qty, size
                           FROM trades ORDER BY id DESC LIMIT 10"""
                    )
                    trades = [dict(r) for r in cur.fetchall()]
            except Exception as e:
                logger.warning("Could not read back trades: %s", e)
            row["recent_trades"] = trades
            acted += 1
        summary.append(row)

    result = {
        "mode": args.mode,
        "candidates_acted": acted,
        "attempts": attempts,
        "max_trades": CANARY_MAX_TRADES,
        "max_trade_size": CANARY_MAX_TRADE_SIZE,
        "min_roi": CANARY_MIN_NET_ROI,
        "scan_count": len(opps),
        "actions": summary,
        "db": db_path,
    }

    if args.json:
        print(json.dumps(result, indent=2, default=str))
    else:
        logger.info("=== CANARY SUMMARY ===")
        logger.info(
            "mode=%s acted=%d/%d scan=%d db=%s",
            args.mode, acted, CANARY_MAX_TRADES, len(opps), db_path,
        )
        for a in summary:
            logger.info(
                "  %s executed=%s roi=%s order_ids=%s",
                a.get("market"),
                a.get("executed"),
                a.get("net_roi"),
                [
                    t.get("order_id")
                    for t in a.get("recent_trades", [])
                    if t.get("order_id")
                ],
            )

    if acted == 0:
        logger.warning(
            "No canary actions taken — no matched PM↔Kalshi pairs met "
            "min_roi=%.2f%% with buildable legs. Paper/live path is wired; "
            "edge may simply be absent right now.",
            CANARY_MIN_NET_ROI * 100,
        )
        return 0  # not a failure of the tooling
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
