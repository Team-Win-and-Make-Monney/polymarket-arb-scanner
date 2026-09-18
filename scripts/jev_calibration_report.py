#!/usr/bin/env python3
"""CLI utility for TypeSafe Jev System One empirical calibration and Brier score reporting.

Generates statistical calibration metrics, Brier skill score benchmarking,
reliability diagrams, and edge realization PnL from the SQLite database.

Usage:
    python scripts/jev_calibration_report.py [--asset BTC] [--sync] [--json] [--markdown]
"""

from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config import DATA_DIR
from db import TradeDB
from jev_calibration import generate_calibration_report
from scripts.sync_jev_resolutions import sync_resolutions


def format_terminal_report(report: dict) -> str:
    """Format the calibration report into a rich terminal string."""
    lines = []
    w = 75
    lines.append("=" * w)
    lines.append("  TYPESAFE JEV SYSTEM ONE — EMPIRICAL CALIBRATION & BRIER SCORE REPORT")
    lines.append("=" * w)

    asset_filt = report.get("asset_filter", "all").upper()
    lines.append(f"Asset Filter:       {asset_filt}")
    lines.append(f"Total Evaluated:    {report.get('total_logged', 0)}")
    lines.append(f"Total Resolved:     {report.get('total_resolved', 0)}")

    if report.get("status") == "pending_resolutions":
        lines.append("-" * w)
        lines.append("NOTICE: " + report.get("message", "No resolved decisions found."))
        lines.append("-" * w)
        return "\n".join(lines)

    j_bs = report.get("jev_brier_score", 0.0)
    m_bs = report.get("market_brier_score", 0.0)
    bss = report.get("brier_skill_score")
    ece = report.get("expected_calibration_error", 0.0)

    lines.append("-" * w)
    lines.append("CORE PROBABILITY CALIBRATION METRICS")
    lines.append("-" * w)
    lines.append(f"  Jev Brier Score (BS):          {j_bs:.4f}  (0.0000 = perfect clairvoyance)")
    lines.append(f"  Market Pricing Brier Score:    {m_bs:.4f}")
    if bss is not None:
        bss_str = f"{bss:+.4f}"
        advantage = "Jev Outperformed Market" if bss > 0 else "Market Closer to Outcome"
        lines.append(f"  Brier Skill Score (BSS):       {bss_str}  [{advantage}]")
    else:
        lines.append("  Brier Skill Score (BSS):       N/A")
    lines.append(f"  Expected Calibration Error:    {ece:.4f}  (Lower = tighter probability calibration)")

    # Edge realization
    edge = report.get("edge_realization", {})
    if edge and edge.get("total_recommended", 0) > 0:
        lines.append("-" * w)
        lines.append("EDGE REALIZATION & SIMULATED EXECUTION PERFORMANCE")
        lines.append("-" * w)
        rec = edge.get("total_recommended", 0)
        w_cnt = edge.get("wins", 0)
        l_cnt = edge.get("losses", 0)
        wr = edge.get("win_rate", 0.0) * 100
        pnl = edge.get("total_pnl", 0.0)
        roi = edge.get("roi", 0.0) * 100
        lines.append(f"  Actionable Decisions:          {rec} trades (buy_yes / buy_no)")
        lines.append(f"  Win / Loss Record:             {w_cnt}W - {l_cnt}L ({wr:.1f}% win rate)")
        lines.append(f"  Simulated Net PnL ($50 stake): ${pnl:+.2f} ({roi:+.2f}% ROI)")

    # Asset breakdown table
    per_asset = report.get("per_asset", {})
    if per_asset:
        lines.append("-" * w)
        lines.append("PER-ASSET BREAKDOWN")
        lines.append("-" * w)
        lines.append(f"{'Asset':<6} {'Logged':<8} {'Resolved':<10} {'Jev BS':<10} {'Mkt BS':<10} {'BSS':<10} {'ECE':<8}")
        lines.append("-" * w)
        for sym, d in per_asset.items():
            log_c = d.get("logged", 0)
            res_c = d.get("resolved", 0)
            j_str = f"{d['jev_brier_score']:.4f}" if d.get("jev_brier_score") is not None else "-"
            m_str = f"{d['market_brier_score']:.4f}" if d.get("market_brier_score") is not None else "-"
            bss_val = d.get("brier_skill_score")
            bss_str = f"{bss_val:+.4f}" if bss_val is not None else "-"
            ece_val = d.get("ece")
            ece_str = f"{ece_val:.4f}" if ece_val is not None else "-"
            lines.append(f"{sym:<6} {log_c:<8} {res_c:<10} {j_str:<10} {m_str:<10} {bss_str:<10} {ece_str:<8}")

    # Reliability diagram
    ascii_diag = report.get("ascii_diagram")
    if ascii_diag:
        lines.append("-" * w)
        lines.append(ascii_diag)

    # Bin detail table
    bins = report.get("bins", [])
    if bins:
        lines.append("-" * w)
        lines.append("RELIABILITY BINS TABLE")
        lines.append("-" * w)
        lines.append(f"{'Bin':<12} {'Count':<8} {'Mean Forecast':<16} {'Observed Freq':<16} {'Error':<10}")
        lines.append("-" * w)
        for b in bins:
            if b["count"] > 0:
                lines.append(
                    f"{b['range']:<12} {b['count']:<8} {b['mean_forecast']:<16.4f} "
                    f"{b['observed_frequency']:<16.4f} {b['calibration_error']:<10.4f}"
                )

    lines.append("=" * w)
    return "\n".join(lines)


def format_markdown_report(report: dict) -> str:
    """Format calibration report as a Markdown document."""
    lines = []
    lines.append("# TypeSafe Jev System One Empirical Calibration Report")
    lines.append("")
    lines.append(f"- **Asset Filter**: `{report.get('asset_filter', 'all')}`")
    lines.append(f"- **Total Evaluated Decisions**: `{report.get('total_logged', 0)}`")
    lines.append(f"- **Resolved Decisions**: `{report.get('total_resolved', 0)}`")
    lines.append("")

    if report.get("status") == "pending_resolutions":
        lines.append("> [!NOTE]")
        lines.append(f"> {report.get('message')}")
        return "\n".join(lines)

    lines.append("## Summary Metrics")
    lines.append("")
    lines.append("| Metric | Value | Reference / Meaning |")
    lines.append("| :--- | :--- | :--- |")
    lines.append(f"| **Jev Brier Score** | `{report.get('jev_brier_score', 0.0):.4f}` | 0.0 = perfect accuracy |")
    lines.append(f"| **Market Brier Score** | `{report.get('market_brier_score', 0.0):.4f}` | Market-implied probability accuracy |")
    bss = report.get("brier_skill_score")
    bss_str = f"`{bss:+.4f}`" if bss is not None else "`N/A`"
    lines.append(f"| **Brier Skill Score (BSS)** | {bss_str} | `> 0` indicates Jev outperforms market pricing |")
    lines.append(f"| **Expected Calibration Error (ECE)** | `{report.get('expected_calibration_error', 0.0):.4f}` | Weighted average reliability bin error |")
    lines.append("")

    edge = report.get("edge_realization", {})
    if edge and edge.get("total_recommended", 0) > 0:
        lines.append("## Simulated Edge Realization")
        lines.append("")
        wr = edge.get("win_rate", 0.0) * 100
        roi = edge.get("roi", 0.0) * 100
        lines.append(f"- **Actionable Trades**: {edge.get('total_recommended')} (`{edge.get('wins')}W - {edge.get('losses')}L`)")
        lines.append(f"- **Win Rate**: `{wr:.1f}%`")
        lines.append(rf"- **Simulated PnL**: `\${edge.get('total_pnl'):+.2f}` (`{roi:+.2f}%` ROI)")
        lines.append("")

    per_asset = report.get("per_asset", {})
    if per_asset:
        lines.append("## Asset Breakdown")
        lines.append("")
        lines.append("| Asset | Logged | Resolved | Jev BS | Market BS | Brier Skill Score | ECE |")
        lines.append("| :--- | :--- | :--- | :--- | :--- | :--- | :--- |")
        for sym, d in per_asset.items():
            j_str = f"`{d['jev_brier_score']:.4f}`" if d.get("jev_brier_score") is not None else "-"
            m_str = f"`{d['market_brier_score']:.4f}`" if d.get("market_brier_score") is not None else "-"
            bss_val = d.get("brier_skill_score")
            bss_str = f"`{bss_val:+.4f}`" if bss_val is not None else "-"
            ece_val = d.get("ece")
            ece_str = f"`{ece_val:.4f}`" if ece_val is not None else "-"
            lines.append(f"| **{sym}** | {d.get('logged', 0)} | {d.get('resolved', 0)} | {j_str} | {m_str} | {bss_str} | {ece_str} |")
        lines.append("")

    ascii_diag = report.get("ascii_diagram")
    if ascii_diag:
        lines.append("## Reliability Diagram")
        lines.append("```")
        lines.append(ascii_diag)
        lines.append("```")

    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description="TypeSafe Jev Empirical Calibration & Brier Score Analytics")
    parser.add_argument("--asset", type=str, default="all", help="Asset filter (BTC, ETH, SOL, XRP, or all)")
    parser.add_argument("--sync", action="store_true", help="Sync latest Polymarket resolutions before reporting")
    parser.add_argument("--bins", type=int, default=10, help="Number of reliability bins (default 10)")
    parser.add_argument("--json", action="store_true", help="Output report as JSON")
    parser.add_argument("--markdown", action="store_true", help="Output report formatted as Markdown")
    parser.add_argument("--db", type=str, default=None, help="Custom SQLite database path")
    args = parser.parse_args()

    db_path = args.db or os.path.join(DATA_DIR, "trades.db")
    db = TradeDB(db_path)

    try:
        if args.sync:
            sync_resolutions(db, limit=200)

        report = generate_calibration_report(db, asset=args.asset, num_bins=args.bins)

        if args.json:
            print(json.dumps(report, indent=2))
        elif args.markdown:
            print(format_markdown_report(report))
        else:
            print(format_terminal_report(report))
    finally:
        db.close()


if __name__ == "__main__":
    main()
