"""Per-strategy P&L analytics over trades.db using DuckDB.

Provides standalone analytics for querying per-strategy metrics (trade count, win rate,
Sharpe ratio, max drawdown) over a rolling 7-day window. Can be run on-demand or scheduled
without impacting live bot execution.

Sharpe ratio is annualized (sqrt(252)) for >= 20 trades; returns N/A for < 20 trades.
Max drawdown is calculated as peak cumulative PnL minus trough in the rolling window.
"""

import argparse
import csv
import duckdb
import json
import logging
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Add parent directory to path for config import
sys.path.insert(0, str(Path(__file__).parent.parent))
from config import LOG_LEVEL

logger = logging.getLogger(__name__)


def _setup_logging():
    """Configure logger with LOG_LEVEL from config."""
    logging.basicConfig(
        level=LOG_LEVEL,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
    )


def get_strategy_metrics(
    db_path: str = "trades.db",
    lookback_days: int = 7
) -> list[dict]:
    """
    Compute per-strategy metrics over a rolling lookback window.

    Args:
        db_path: Path to SQLite trades.db file
        lookback_days: Number of days to look back (default 7)

    Returns:
        List of dicts, one per strategy, with keys:
        - strategy: opportunity type (string)
        - trade_count: number of trades in window
        - wins: number of profitable trades
        - win_rate: wins / trade_count (N/A if trade_count == 0)
        - total_pnl: sum of net_profit
        - avg_pnl: average net_profit per trade
        - annual_sharpe: annualized Sharpe ratio (N/A if < 20 trades)
        - max_drawdown: peak cumulative PnL - trough cumulative PnL

    Query Pattern:
        - Uses window functions: ROW_NUMBER, SUM OVER for cumulative tracking
        - Filters opportunities by timestamp >= (now - lookback_days), action IN ('executed', 'filled', 'dry_run')
        - Computes STDDEV_POP * sqrt(252) for annualized Sharpe
        - Returns empty list if DB is empty or inaccessible
    """
    start_time = time.time()
    cutoff = (datetime.now(timezone.utc) - timedelta(days=lookback_days)).isoformat()

    try:
        conn = duckdb.connect(db_path, read_only=True)

        # SQL query using window functions for cumulative PnL tracking
        query = """
            WITH strategy_trades AS (
                SELECT
                    id,
                    type,
                    net_profit,
                    timestamp,
                    ROW_NUMBER() OVER (PARTITION BY type ORDER BY timestamp, id) as rn,
                    SUM(net_profit) OVER (
                        PARTITION BY type ORDER BY timestamp, id
                        ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW
                    ) as cumulative_pnl
                FROM opportunities
                WHERE timestamp >= ?
                  AND action IN ('executed', 'filled', 'dry_run')
            ),
            strategy_curve AS (
                SELECT
                    *,
                    GREATEST(
                        0,
                        MAX(cumulative_pnl) OVER (
                            PARTITION BY type ORDER BY timestamp, id
                            ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW
                        )
                    ) as running_peak
                FROM strategy_trades
            ),
            strategy_metrics AS (
                SELECT
                    type as strategy,
                    COUNT(*) as trade_count,
                    COUNT(CASE WHEN net_profit > 0 THEN 1 END) as wins,
                    COUNT(CASE WHEN net_profit > 0 THEN 1 END)::FLOAT / COUNT(*) as win_rate,
                    SUM(net_profit) as total_pnl,
                    AVG(net_profit) as avg_pnl,
                    STDDEV_POP(net_profit) as stddev,
                    MAX(running_peak - cumulative_pnl) as max_drawdown
                FROM strategy_curve
                GROUP BY type
            )
            SELECT
                strategy,
                trade_count,
                wins,
                CASE WHEN trade_count = 0 THEN NULL ELSE win_rate END as win_rate,
                total_pnl,
                avg_pnl,
                CASE
                    WHEN trade_count >= 20 AND stddev IS NOT NULL
                    THEN avg_pnl / NULLIF(stddev, 0) * SQRT(252)
                    ELSE NULL
                END as annual_sharpe,
                CASE WHEN trade_count = 0 THEN 0 ELSE max_drawdown END as max_drawdown
            FROM strategy_metrics
            ORDER BY total_pnl DESC
        """

        results = conn.execute(query, [cutoff]).fetchall()
        conn.close()

        elapsed = time.time() - start_time
        logger.debug("Fetched metrics for %d strategies in %.2fs", len(results), elapsed)

        # Convert DuckDB Row objects to dicts
        return [
            {
                "strategy": row[0],
                "trade_count": row[1],
                "wins": row[2],
                "win_rate": row[3] if row[3] is not None else "N/A",
                "total_pnl": row[4],
                "avg_pnl": row[5],
                "annual_sharpe": row[6] if row[6] is not None else "N/A",
                "max_drawdown": row[7],
            }
            for row in results
        ]

    except Exception as e:
        logger.error("Failed to fetch analytics from %s: %s", db_path, e)
        return []


def main():
    """CLI entry point for analytics script.

    Supports multiple output formats and configurable lookback period.
    Returns 0 on success, 1 on error.
    """
    parser = argparse.ArgumentParser(
        description="Per-strategy P&L analytics from trades.db using DuckDB",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python scripts/analytics.py
  python scripts/analytics.py --output-format csv
  python scripts/analytics.py --lookback-days 30 --output-format table
  python scripts/analytics.py --db-path /var/data/trades.db
        """
    )
    parser.add_argument(
        "--db-path",
        default="trades.db",
        help="Path to trades.db file (default: trades.db)"
    )
    parser.add_argument(
        "--lookback-days",
        type=int,
        default=7,
        help="Number of days to look back (default: 7)"
    )
    parser.add_argument(
        "--output-format",
        choices=["json", "csv", "table"],
        default="json",
        help="Output format: json, csv, or table (default: json)"
    )

    args = parser.parse_args()

    try:
        metrics = get_strategy_metrics(
            db_path=args.db_path,
            lookback_days=args.lookback_days
        )

        if args.output_format == "json":
            print(json.dumps(metrics, indent=2, default=str))

        elif args.output_format == "csv":
            if metrics:
                writer = csv.DictWriter(sys.stdout, fieldnames=metrics[0].keys())
                writer.writeheader()
                writer.writerows(metrics)
            else:
                print("No data available")

        elif args.output_format == "table":
            if metrics:
                # Simple table formatting
                headers = ["Strategy", "Trades", "Wins", "Win Rate", "Total PnL",
                          "Avg PnL", "Sharpe", "Max DD"]
                print("\n".join(headers))
                print("-" * 80)
                for m in metrics:
                    win_rate_str = f"{m['win_rate']:.1%}" if m['win_rate'] != "N/A" else "N/A"
                    sharpe_str = f"{m['annual_sharpe']:.2f}" if m['annual_sharpe'] != "N/A" else "N/A"
                    print(
                        f"{m['strategy']:20} {m['trade_count']:6} {m['wins']:5} "
                        f"{win_rate_str:9} ${m['total_pnl']:8.2f} ${m['avg_pnl']:8.2f} "
                        f"{sharpe_str:8} ${m['max_drawdown']:8.2f}"
                    )
            else:
                print("No data available")

        return 0

    except Exception as e:
        logger.error("Analytics script failed: %s", e)
        return 1


if __name__ == "__main__":
    _setup_logging()
    sys.exit(main())
