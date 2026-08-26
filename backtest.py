#!/usr/bin/env python3
"""Backtesting engine — replays historical snapshots through simulated execution."""

import argparse
import logging
import math
import os
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone

# Ensure parent directory is importable
sys.path.insert(0, os.path.dirname(__file__))

from config import BACKTEST_INITIAL_BALANCE, STRATEGY_LAYERS, get_layer
from snapshot import SnapshotRecorder

try:
    from fees import (
        net_profit_binary_internal,
        net_profit_negrisk_internal,
        net_profit_cross_platform,
        net_profit_kalshi_binary,
        net_profit_kalshi_multi,
        net_profit_triangular,
        net_profit_gemini_binary,
        net_profit_ibkr_binary,
        net_profit_multi_cross,
        net_profit_betfair_backall,
        net_profit_betfair_backlay,
        net_profit_smarkets_backall,
        net_profit_smarkets_backlay,
        net_profit_sxbet_backall,
        net_profit_sxbet_backlay,
        net_profit_matchbook_backall,
        net_profit_matchbook_backlay,
        net_profit_cross_generic,
    )
except ImportError:
    # Graceful fallback if fee functions are unavailable
    pass

# STRATEGY_LAYERS and get_layer() are imported from config (single source of truth)

LAYER_NAMES = {
    1: "Pure Arbitrage",
    2: "Near-Arbitrage",
    3: "Market Making",
    4: "Informed Trading",
    5: "Capital Optimization",
}

logger = logging.getLogger(__name__)


@dataclass
class BacktestResult:
    """Results of a backtest run."""
    total_trades: int = 0
    winning_trades: int = 0
    losing_trades: int = 0
    win_rate: float = 0.0
    total_pnl: float = 0.0
    max_drawdown: float = 0.0
    sharpe_ratio: float = 0.0
    initial_balance: float = 0.0
    final_balance: float = 0.0
    trades_by_type: dict = field(default_factory=dict)
    trade_log: list = field(default_factory=list)

    def summary(self) -> str:
        """Return a human-readable summary of the backtest."""
        lines = [
            "=" * 60,
            "BACKTEST RESULTS",
            "=" * 60,
            f"Initial balance:  ${self.initial_balance:.2f}",
            f"Final balance:    ${self.final_balance:.2f}",
            f"Total P&L:        ${self.total_pnl:+.2f}",
            f"Return:           {(self.total_pnl / self.initial_balance * 100) if self.initial_balance > 0 else 0:.2f}%",
            f"Total trades:     {self.total_trades}",
            f"Win rate:         {self.win_rate:.1%}",
            f"Max drawdown:     ${self.max_drawdown:.2f}",
            f"Sharpe ratio:     {self.sharpe_ratio:.3f}",
        ]

        # Group by strategy layer
        layer_stats: dict[int, dict] = {}
        for opp_type, stats in self.trades_by_type.items():
            layer = get_layer(opp_type)
            if layer not in layer_stats:
                layer_stats[layer] = {"count": 0, "pnl": 0.0, "wins": 0}
            layer_stats[layer]["count"] += stats["count"]
            layer_stats[layer]["pnl"] += stats["pnl"]
            layer_stats[layer]["wins"] += stats["wins"]

        if layer_stats:
            lines.append("")
            lines.append("P&L by layer:")
            for layer_num in sorted(layer_stats.keys()):
                ls = layer_stats[layer_num]
                name = LAYER_NAMES.get(layer_num, f"Layer {layer_num}")
                wr = ls["wins"] / ls["count"] if ls["count"] > 0 else 0
                lines.append(
                    f"  L{layer_num} {name}: {ls['count']} trades, "
                    f"P&L ${ls['pnl']:+.4f}, win rate {wr:.1%}"
                )

        lines.append("")
        lines.append("Trades by type:")
        for opp_type, stats in sorted(self.trades_by_type.items()):
            lines.append(
                f"  {opp_type}: {stats['count']} trades, "
                f"P&L ${stats['pnl']:+.4f}, "
                f"win rate {stats['win_rate']:.1%}"
            )
        lines.append("=" * 60)
        return "\n".join(lines)


class BacktestEngine:
    """Replays historical snapshots to simulate trading and calculate P&L."""

    def __init__(
        self,
        recorder: SnapshotRecorder | None = None,
        initial_balance: float | None = None,
    ):
        self.recorder = recorder or SnapshotRecorder()
        self.initial_balance = initial_balance or BACKTEST_INITIAL_BALANCE

    def run(
        self,
        start_time: str,
        end_time: str,
        min_roi: float = 0.0,
        min_profit: float = 0.0,
        max_trade_size: float = 5.0,
        opp_type_filter: str | None = None,
        layer_filter: int | None = None,
    ) -> BacktestResult:
        """Run a backtest over historical snapshots.

        Args:
            start_time: ISO format start timestamp.
            end_time: ISO format end timestamp.
            min_roi: Minimum ROI threshold to enter a trade (e.g. 0.05 for 5%).
            min_profit: Minimum absolute net profit to enter a trade.
            max_trade_size: Maximum trade size per leg in dollars.
            opp_type_filter: Optional filter for specific opportunity types.
            layer_filter: Optional filter by strategy layer (1-5).

        Returns:
            BacktestResult with performance metrics.
        """
        snapshots = self.recorder.get_snapshots(start_time, end_time, opp_type_filter)

        if not snapshots:
            logger.warning("No snapshots found for the given time range.")
            result = BacktestResult()
            result.initial_balance = self.initial_balance
            result.final_balance = self.initial_balance
            return result

        logger.info(
            "Backtesting %d snapshots from %s to %s",
            len(snapshots), start_time, end_time,
        )

        balance = self.initial_balance
        peak_balance = balance
        max_drawdown = 0.0
        trade_log = []
        returns = []  # Per-trade returns for Sharpe calculation
        trades_by_type: dict[str, dict] = {}

        for snap in snapshots:
            net_profit = snap.get("net_profit", 0)
            gross_spread = snap.get("gross_spread", 0)
            fees = snap.get("fees", 0)
            opp_t = snap.get("opp_type", "")
            market = snap.get("market", "")
            price_a = snap.get("price_a")
            price_b = snap.get("price_b")

            # Skip if net_profit is non-positive
            if net_profit <= 0:
                continue

            # Layer filter: skip snapshots from the wrong layer
            if layer_filter is not None and get_layer(opp_t) != layer_filter:
                continue

            # Calculate total cost from prices
            total_cost = 0.0
            if price_a is not None and price_b is not None:
                total_cost = price_a + price_b
            elif price_a is not None:
                total_cost = price_a
            elif gross_spread > 0:
                total_cost = 1.0 - gross_spread

            if total_cost <= 0:
                continue

            # ROI filter
            roi = net_profit / total_cost if total_cost > 0 else 0
            if roi < min_roi:
                continue

            # Minimum profit filter
            if net_profit < min_profit:
                continue

            # Size the trade
            trade_size = min(max_trade_size, balance * 0.1)  # Max 10% of balance per trade
            if trade_size <= 0:
                continue

            # Scale profit by trade size (snapshots record per-unit profit)
            # Number of contracts we can buy at total_cost per unit
            units = trade_size / total_cost if total_cost > 0 else 0
            realized_profit = net_profit * units

            # Recalculate fees using fee functions if available
            realized_profit = self._recalc_profit_with_fees(
                opp_t, price_a, price_b, units, net_profit, snap=snap,
            )

            # Execute the simulated trade
            balance += realized_profit

            # Track peak and drawdown
            if balance > peak_balance:
                peak_balance = balance
            drawdown = peak_balance - balance
            if drawdown > max_drawdown:
                max_drawdown = drawdown

            # Track returns for Sharpe
            trade_return = realized_profit / trade_size if trade_size > 0 else 0
            returns.append(trade_return)

            # Log the trade
            trade_entry = {
                "timestamp": snap.get("timestamp", ""),
                "market": market,
                "type": opp_t,
                "price_a": price_a,
                "price_b": price_b,
                "net_profit": realized_profit,
                "balance_after": balance,
                "roi": roi,
            }
            trade_log.append(trade_entry)

            # Track by type
            if opp_t not in trades_by_type:
                trades_by_type[opp_t] = {
                    "count": 0, "wins": 0, "pnl": 0.0, "win_rate": 0.0,
                }
            trades_by_type[opp_t]["count"] += 1
            trades_by_type[opp_t]["pnl"] += realized_profit
            if realized_profit > 0:
                trades_by_type[opp_t]["wins"] += 1

        # Compute final stats
        total_trades = len(trade_log)
        winning = sum(1 for t in trade_log if t["net_profit"] > 0)
        losing = total_trades - winning

        # Win rates per type
        for stats in trades_by_type.values():
            stats["win_rate"] = stats["wins"] / stats["count"] if stats["count"] > 0 else 0.0

        # Sharpe ratio (annualized, assuming daily frequency)
        sharpe = 0.0
        if returns:
            avg_return = sum(returns) / len(returns)
            if len(returns) > 1:
                variance = sum((r - avg_return) ** 2 for r in returns) / (len(returns) - 1)
                std_return = math.sqrt(variance)
                if std_return > 0:
                    sharpe = (avg_return / std_return) * math.sqrt(252)

        return BacktestResult(
            total_trades=total_trades,
            winning_trades=winning,
            losing_trades=losing,
            win_rate=winning / total_trades if total_trades > 0 else 0.0,
            total_pnl=balance - self.initial_balance,
            max_drawdown=max_drawdown,
            sharpe_ratio=sharpe,
            initial_balance=self.initial_balance,
            final_balance=balance,
            trades_by_type=trades_by_type,
            trade_log=trade_log,
        )

    def _recalc_profit_with_fees(
        self,
        opp_type: str,
        price_a: float | None,
        price_b: float | None,
        units: float,
        fallback_profit: float,
        snap: dict | None = None,
    ) -> float:
        """Recalculate profit using fee functions from fees.py.

        Falls back to snapshot net_profit * units if fee functions are
        unavailable or prices are missing.
        """
        if price_a is None or price_b is None:
            return fallback_profit * units

        try:
            if opp_type == "Binary":
                result = net_profit_binary_internal(price_a, price_b)
            elif opp_type.startswith("NegRisk"):
                # Multi-outcome: price_a = cheapest, price_b = total_cost
                result = net_profit_negrisk_internal([price_a, price_b])
            elif opp_type.startswith("KalshiBinary"):
                result = net_profit_kalshi_binary(price_a, price_b)
            elif opp_type.startswith("KalshiMulti"):
                result = net_profit_kalshi_multi([price_a, price_b])
            elif opp_type == "TriangularCross":
                plat_a = (snap or {}).get("platform_a", "polymarket")
                plat_b = (snap or {}).get("platform_b", "kalshi")
                result = net_profit_triangular(price_a, price_b, plat_a, plat_b)
            elif opp_type.startswith("MultiCross"):
                plats = (snap or {}).get("platforms", ["polymarket", "kalshi"])
                result = net_profit_multi_cross([price_a, price_b], plats[:2])
            elif opp_type == "BetfairBackAll":
                result = net_profit_betfair_backall([price_a, price_b])
            elif opp_type == "BetfairBackLay":
                result = net_profit_betfair_backlay(price_a, price_b)
            elif opp_type == "SmarketsBackAll":
                result = net_profit_smarkets_backall([price_a, price_b])
            elif opp_type == "SmarketsBackLay":
                result = net_profit_smarkets_backlay(price_a, price_b)
            elif opp_type == "SXBetBackAll":
                result = net_profit_sxbet_backall([price_a, price_b])
            elif opp_type == "SXBetBackLay":
                result = net_profit_sxbet_backlay(price_a, price_b)
            elif opp_type == "MatchbookBackAll":
                result = net_profit_matchbook_backall([price_a, price_b])
            elif opp_type == "MatchbookBackLay":
                result = net_profit_matchbook_backlay(price_a, price_b)
            elif opp_type.startswith("Gemini"):
                result = net_profit_gemini_binary(price_a, price_b)
            elif opp_type.startswith("IBKR"):
                result = net_profit_ibkr_binary(price_a, price_b)
            elif opp_type.startswith("Cross"):
                plat_a = (snap or {}).get("platform_a", "polymarket")
                plat_b = (snap or {}).get("platform_b", "kalshi")
                result = net_profit_cross_generic(price_a, price_b, "yes", "no",
                                                  plat_a, plat_b)
            elif opp_type in ("StalePriceOpp", "ResolutionSnipeOpp", "ConvergenceOpp",
                              "EventDivergence", "MarketMake"):
                # Signal/directional trades — profit is estimated at scan time
                # Use the snapshot's stored net_profit directly
                return fallback_profit * units
            else:
                return fallback_profit * units

            return result.get("net_profit", fallback_profit) * units
        except (NameError, TypeError, Exception):
            return fallback_profit * units


# ---------------------------------------------------------------------------
# Nightly backtest-to-config feedback loop (OPTIMIZE-02)
# ---------------------------------------------------------------------------

def _suggest_min_roi(result: BacktestResult) -> float:
    """Suggest a new MIN_NET_ROI based on backtest win rate.

    - win_rate > 0.7: lower by 10% (safe to capture more opportunities)
    - win_rate < 0.5: raise by 20% (be more selective)
    - Otherwise: keep current value unchanged.
    Soft-clamped to [0.001, 0.05]: the clamp must never *reverse* the
    direction of the recommendation. Relaxing must never raise the
    threshold above current; tightening must never lower it below.
    """
    import config as _config
    current = _config.MIN_NET_ROI
    if result.win_rate > 0.7:
        if current <= 0:
            return current
        suggested = current * 0.90
        clamped = max(0.001, min(0.05, suggested))
        return min(clamped, current)
    elif result.win_rate < 0.5:
        suggested = current * 1.20 if current > 0 else 0.005
        clamped = max(0.001, min(0.05, suggested))
        return max(clamped, current)
    return current


def _suggest_fuzzy_threshold(result: BacktestResult) -> int:
    """Suggest a new FUZZY_MATCH_THRESHOLD based on backtest win rate.

    If win_rate < 0.5 (likely false positives from loose matching),
    raise threshold by 3. Otherwise keep current.
    Clamped to [60, 90].
    """
    import config as _config
    current = _config.FUZZY_MATCH_THRESHOLD
    if result.win_rate < 0.5:
        suggested = current + 3
    else:
        suggested = current
    return max(60, min(90, suggested))


def _suggest_min_profit(result: BacktestResult) -> float:
    """Suggest a new MIN_PROFIT_THRESHOLD based on average losing trade.

    If there are losing trades, sets min profit to 1.2x the average
    losing trade size to ensure we only trade when we can beat typical
    losses. Falls back to current config value if no losing trades.
    """
    import config as _config
    current = _config.DEFAULT_MIN_PROFIT
    if result.losing_trades > 0 and result.trade_log:
        losing = [abs(t["net_profit"]) for t in result.trade_log if t["net_profit"] < 0]
        if losing:
            avg_loss = sum(losing) / len(losing)
            suggested = avg_loss * 1.2
            return max(0.001, min(0.05, suggested))
    return current


PER_STRATEGY_MIN_TRADES = 10


def _suggest_strategy_thresholds(stats: dict) -> dict:
    """Per-strategy MIN_NET_ROI + FUZZY_MATCH_THRESHOLD from a stats row.

    Uses the per-strategy win rate to nudge global defaults the same way
    _suggest_min_roi / _suggest_fuzzy_threshold do at the portfolio level.
    Returns clamped values inside the safe operating ranges.
    """
    import config as _config

    count = max(int(stats.get("count", 0)), 1)
    wins = int(stats.get("wins", 0))
    win_rate = stats.get("win_rate")
    if win_rate is None:
        win_rate = wins / count

    base_roi = _config.MIN_NET_ROI
    base_fuzzy = _config.FUZZY_MATCH_THRESHOLD

    if win_rate > 0.7:
        roi = base_roi * 0.90
        fuzzy = base_fuzzy
    elif win_rate < 0.5:
        roi = (base_roi * 1.20) if base_roi > 0 else 0.005
        fuzzy = base_fuzzy + 3
    else:
        roi = base_roi
        fuzzy = base_fuzzy

    return {
        "MIN_NET_ROI": max(0.001, min(0.05, roi)),
        "FUZZY_MATCH_THRESHOLD": max(60, min(90, int(fuzzy))),
    }


def build_recommendations(result: BacktestResult) -> dict:
    """Build a recommendations dict from a BacktestResult.

    Returns a dict with suggested threshold adjustments, current values,
    per-strategy breakdown, and per-strategy threshold overrides for any
    strategy with at least PER_STRATEGY_MIN_TRADES backtested trades.
    Safe to call with zero-trade results.

    Each ``by_strategy`` entry additively carries ``MIN_NET_ROI`` and
    ``FUZZY_MATCH_THRESHOLD`` alongside ``win_rate`` / ``avg_profit`` so
    callers that only inspect ``by_strategy`` still get per-strategy
    threshold guidance. When the backtest produced zero trades, a
    synthetic ``__default__`` entry carries the portfolio-wide
    recommendation so the dict is never empty.
    """
    import config as _config
    from datetime import datetime, timezone

    global_roi = _suggest_min_roi(result)
    global_fuzzy = _suggest_fuzzy_threshold(result)

    by_strategy: dict = {}
    for strategy, stats in result.trades_by_type.items():
        strat_recs = _suggest_strategy_thresholds(stats)
        count = max(int(stats.get("count", 0)), 1)
        by_strategy[strategy] = {
            "win_rate": stats["wins"] / count,
            "avg_profit": stats["pnl"] / count,
            "count": int(stats.get("count", 0)),
            "MIN_NET_ROI": strat_recs["MIN_NET_ROI"],
            "FUZZY_MATCH_THRESHOLD": strat_recs["FUZZY_MATCH_THRESHOLD"],
        }

    if not by_strategy:
        by_strategy["__default__"] = {
            "win_rate": result.win_rate,
            "avg_profit": 0.0,
            "count": int(result.total_trades),
            "MIN_NET_ROI": global_roi,
            "FUZZY_MATCH_THRESHOLD": global_fuzzy,
        }

    recommended_by_strategy = {
        strategy: _suggest_strategy_thresholds(stats)
        for strategy, stats in result.trades_by_type.items()
        if int(stats.get("count", 0)) >= PER_STRATEGY_MIN_TRADES
    }

    return {
        "generated_at": datetime.now(timezone.utc).replace(tzinfo=None).isoformat() + "Z",
        "period_days": 7,
        "total_trades": result.total_trades,
        "win_rate": result.win_rate,
        "recommended": {
            "MIN_NET_ROI": global_roi,
            "FUZZY_MATCH_THRESHOLD": global_fuzzy,
            "MIN_PROFIT_THRESHOLD": _suggest_min_profit(result),
        },
        "current": {
            "MIN_NET_ROI": _config.MIN_NET_ROI,
            "FUZZY_MATCH_THRESHOLD": _config.FUZZY_MATCH_THRESHOLD,
            "MIN_PROFIT_THRESHOLD": _config.DEFAULT_MIN_PROFIT,
        },
        "by_strategy": by_strategy,
        "recommended_by_strategy": recommended_by_strategy,
    }


def write_recommendations(result: BacktestResult, data_dir: str) -> str:
    """Write backtest recommendations to a JSON file in data_dir.

    Creates DATA_DIR/backtest_recommendations.json with suggested threshold
    adjustments. Returns the absolute path of the written file.
    """
    import json
    rec = build_recommendations(result)
    path = os.path.join(data_dir, "backtest_recommendations.json")
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(rec, fh, indent=2)
    logger.info("Backtest recommendations written to %s", path)
    return path


# ---------------------------------------------------------------------------
# Grid-sweep tuning (concept ported from the archived arbgrid scaffold's tune.py)
# ---------------------------------------------------------------------------
#
# OPTIMIZE-02 above nudges a single threshold by a win-rate heuristic. The sweep
# below instead replays the recorded snapshots under every (min_roi, min_profit)
# combination in a grid and reports the resulting P&L / win-rate / Sharpe per
# cell, so a production threshold can be chosen from the actual yield curve
# rather than a one-step nudge. Strictly read-only — it never places orders.

@dataclass
class SweepCell:
    """One cell of the (min_roi, min_profit) tuning grid."""
    min_roi: float
    min_profit: float
    total_trades: int
    win_rate: float
    total_pnl: float
    final_balance: float
    max_drawdown: float
    sharpe_ratio: float


def _parse_grid(spec: str) -> list[float]:
    """Parse a comma-separated grid spec like '0,0.01,0.05' into sorted floats.

    Values > 1 are treated as percentages (5 -> 0.05), matching the --min-roi
    convention used elsewhere in this CLI.
    """
    out: list[float] = []
    for tok in spec.split(","):
        tok = tok.strip()
        if not tok:
            continue
        v = float(tok)
        out.append(v / 100.0 if v > 1.0 else v)
    return sorted(set(out))


def run_tuning_sweep(
    engine: "BacktestEngine",
    start_time: str,
    end_time: str,
    *,
    min_roi_grid: list[float],
    min_profit_grid: list[float],
    max_trade_size: float = 5.0,
    opp_type_filter: str | None = None,
    layer_filter: int | None = None,
) -> list[SweepCell]:
    """Replay the snapshots once per (min_roi, min_profit) grid cell.

    Returns one SweepCell per combination in deterministic (min_roi, min_profit)
    order. Reuses BacktestEngine.run, so it inherits the same fee model and
    sizing — strictly read-only simulation.
    """
    cells: list[SweepCell] = []
    for min_roi in sorted(set(min_roi_grid)):
        for min_profit in sorted(set(min_profit_grid)):
            result = engine.run(
                start_time=start_time,
                end_time=end_time,
                min_roi=min_roi,
                min_profit=min_profit,
                max_trade_size=max_trade_size,
                opp_type_filter=opp_type_filter,
                layer_filter=layer_filter,
            )
            cells.append(SweepCell(
                min_roi=min_roi,
                min_profit=min_profit,
                total_trades=result.total_trades,
                win_rate=result.win_rate,
                total_pnl=result.total_pnl,
                final_balance=result.final_balance,
                max_drawdown=result.max_drawdown,
                sharpe_ratio=result.sharpe_ratio,
            ))
    return cells


def _best_cell(cells: list[SweepCell], min_trades: int = 1) -> "SweepCell | None":
    """Pick the highest-P&L cell that booked at least `min_trades` trades."""
    eligible = [c for c in cells if c.total_trades >= min_trades]
    if not eligible:
        return None
    return max(eligible, key=lambda c: (c.total_pnl, c.sharpe_ratio))


def render_sweep_markdown(cells: list[SweepCell], generated_at: datetime | None = None) -> str:
    """Render a tuning sweep to a self-contained markdown report."""
    ts = generated_at or datetime.now(timezone.utc)
    best = _best_cell(cells)
    lines: list[str] = []
    lines.append(f"# Backtest tuning sweep — {ts.strftime('%Y-%m-%d %H:%M UTC')}")
    lines.append("")
    lines.append(
        "Replays the recorded snapshots under each `(min_roi, min_profit)` cell and "
        "reports P&L, win rate, drawdown, and Sharpe. Pick the threshold pair that "
        "maximizes P&L while keeping trade count sustainable for the executor."
    )
    lines.append("")
    if best is not None:
        lines.append(
            f"**Recommended:** `MIN_NET_ROI={best.min_roi:.4f}`, "
            f"`MIN_PROFIT={best.min_profit:.4f}` -> P&L ${best.total_pnl:+.4f} over "
            f"{best.total_trades} trades (win rate {best.win_rate:.1%}, "
            f"Sharpe {best.sharpe_ratio:.3f})."
        )
    else:
        lines.append("_No cell booked any trades — widen the grid or record more snapshots._")
    lines.append("")
    lines.append("| min_roi | min_profit | trades | win_rate | total_pnl | max_dd | sharpe |")
    lines.append("|---------|-----------|--------|----------|-----------|--------|--------|")
    for c in cells:
        marker = " (best)" if best is not None and c is best else ""
        lines.append(
            f"| {c.min_roi:.4f} | {c.min_profit:.4f} | {c.total_trades} | "
            f"{c.win_rate:.1%} | {c.total_pnl:+.4f} | {c.max_drawdown:.4f} | "
            f"{c.sharpe_ratio:.3f}{marker} |"
        )
    lines.append("")
    lines.append("---")
    lines.append("")
    lines.append("Generated by `python backtest.py --tune`.")
    return "\n".join(lines) + "\n"


def write_sweep_report(cells: list[SweepCell], data_dir: str) -> str:
    """Write the tuning sweep markdown to DATA_DIR/backtest_tuning_report.md."""
    path = os.path.join(data_dir, "backtest_tuning_report.md")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(render_sweep_markdown(cells))
    logger.info("Backtest tuning report written to %s", path)
    return path


def main():
    """CLI entry point for backtesting."""
    parser = argparse.ArgumentParser(
        description="Backtest arbitrage strategies on historical price snapshots.",
    )
    parser.add_argument(
        "--start", required=True,
        help="Start date (YYYY-MM-DD or ISO format)",
    )
    parser.add_argument(
        "--end", required=True,
        help="End date (YYYY-MM-DD or ISO format)",
    )
    parser.add_argument(
        "--min-roi", type=float, default=0.0,
        help="Minimum ROI threshold (e.g. 5.0 for 5%%)",
    )
    parser.add_argument(
        "--min-profit", type=float, default=0.0,
        help="Minimum absolute net profit per trade",
    )
    parser.add_argument(
        "--max-trade-size", type=float, default=5.0,
        help="Maximum trade size per leg in dollars",
    )
    parser.add_argument(
        "--balance", type=float, default=None,
        help=f"Initial balance (default: ${BACKTEST_INITIAL_BALANCE:.0f})",
    )
    parser.add_argument(
        "--type", dest="opp_type", default=None,
        help="Filter by opportunity type (e.g. Binary, Cross, KalshiBinary)",
    )
    parser.add_argument(
        "--strategy", default=None,
        help="Filter by strategy keyword (e.g. 'all', 'arb', 'mm', 'stale')",
    )
    parser.add_argument(
        "--layer", type=int, default=None, choices=[1, 2, 3, 4, 5],
        help="Filter by strategy layer (1=Pure Arb, 2=Near-Arb, 3=MM, 4=Informed, 5=Optimization)",
    )
    parser.add_argument(
        "--db", default=None,
        help="Path to snapshots.db file",
    )
    parser.add_argument(
        "--verbose", "-v", action="store_true",
        help="Print individual trades",
    )
    parser.add_argument(
        "--tune", action="store_true",
        help="Run a (min_roi x min_profit) grid sweep and write a markdown tuning report",
    )
    parser.add_argument(
        "--min-roi-grid", default="0,0.01,0.02,0.05",
        help="Comma-separated min_roi grid for --tune (values >1 treated as %%)",
    )
    parser.add_argument(
        "--min-profit-grid", default="0,0.005,0.01",
        help="Comma-separated min_profit grid for --tune",
    )

    args = parser.parse_args()

    # Setup logging
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    # Normalize date inputs to ISO format
    start = _normalize_date(args.start, is_start=True)
    end = _normalize_date(args.end, is_start=False)

    # Convert min_roi from percentage to fraction
    min_roi = args.min_roi / 100.0 if args.min_roi > 1.0 else args.min_roi

    # Resolve --strategy keyword to opp_type_filter if --type not set
    opp_type_filter = args.opp_type
    if not opp_type_filter and args.strategy:
        _STRATEGY_KEYWORDS = {
            "arb": "Binary",
            "binary": "Binary",
            "negrisk": "NegRisk",
            "cross": "Cross",
            "kalshi": "Kalshi",
            "betfair": "Betfair",
            "smarkets": "Smarkets",
            "sxbet": "SXBet",
            "matchbook": "Matchbook",
            "gemini": "Gemini",
            "ibkr": "IBKR",
            "triangular": "TriangularCross",
            "multi-cross": "MultiCross",
            "stale": "StalePriceOpp",
            "resolution": "ResolutionSnipeOpp",
            "convergence": "ConvergenceOpp",
            "mm": "MarketMake",
            "event": "EventDivergence",
        }
        if args.strategy != "all":
            opp_type_filter = _STRATEGY_KEYWORDS.get(args.strategy.lower(), args.strategy)

    recorder = SnapshotRecorder(db_path=args.db)
    engine = BacktestEngine(recorder=recorder, initial_balance=args.balance)

    if args.tune:
        from config import DATA_DIR
        cells = run_tuning_sweep(
            engine, start, end,
            min_roi_grid=_parse_grid(args.min_roi_grid),
            min_profit_grid=_parse_grid(args.min_profit_grid),
            max_trade_size=args.max_trade_size,
            opp_type_filter=opp_type_filter,
            layer_filter=args.layer,
        )
        print(render_sweep_markdown(cells))
        write_sweep_report(cells, DATA_DIR)
        recorder.close()
        return

    result = engine.run(
        start_time=start,
        end_time=end,
        min_roi=min_roi,
        min_profit=args.min_profit,
        max_trade_size=args.max_trade_size,
        opp_type_filter=opp_type_filter,
        layer_filter=args.layer,
    )

    print(result.summary())

    if args.verbose and result.trade_log:
        print("\nTrade Log:")
        print("-" * 80)
        for i, trade in enumerate(result.trade_log, 1):
            print(
                f"  {i:4d}. {trade['timestamp'][:19]}  "
                f"{trade['type']:20s}  "
                f"P&L ${trade['net_profit']:+.4f}  "
                f"Bal ${trade['balance_after']:.2f}  "
                f"{trade['market'][:40]}"
            )

    recorder.close()


def _normalize_date(date_str: str, is_start: bool = True) -> str:
    """Convert a date string to ISO format.

    Accepts YYYY-MM-DD or full ISO timestamps. For plain dates,
    appends T00:00:00Z (start) or T23:59:59Z (end).
    """
    if "T" in date_str:
        return date_str
    if is_start:
        return f"{date_str}T00:00:00+00:00"
    else:
        return f"{date_str}T23:59:59+00:00"


if __name__ == "__main__":
    main()
