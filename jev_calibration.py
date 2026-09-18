"""Empirical Brier score and probability calibration analytics for TypeSafe Jev System One.

Calculates:
- Brier Score (BS): Mean squared error of calibrated model predictions vs ground truth outcomes.
- Brier Skill Score (BSS): Relative improvement of Jev over market-implied pricing baseline.
- Calibration Bins (Reliability Curve): Predicted probability vs observed empirical frequencies.
- Expected Calibration Error (ECE): Sample-weighted absolute calibration error across bins.
- Edge Realization & PnL: Win rate and realized returns for Jev-recommended trades.
- ASCII Reliability Diagram: Visual terminal representation of empirical calibration.
"""

from __future__ import annotations

import logging
import math
from typing import Any

from db import TradeDB

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Mathematical & Statistical Functions
# ---------------------------------------------------------------------------


def calculate_brier_score(forecasts: list[float], outcomes: list[float]) -> float:
    """Calculate the Brier Score for a set of binary probabilistic forecasts.

    BS = (1 / N) * sum((f_i - o_i)^2)
    where f_i in [0, 1] is forecast probability and o_i in {0, 1} is binary outcome.

    Args:
        forecasts: List of predicted probabilities (0.0 to 1.0).
        outcomes: List of binary outcomes (1.0 for Yes, 0.0 for No).

    Returns:
        Brier score between 0.0 (perfect prediction) and 1.0 (total divergence).
        Returns 0.0 if lists are empty.

    Raises:
        ValueError: If non-empty lists have mismatched lengths.
    """
    if not forecasts or not outcomes:
        return 0.0

    if len(forecasts) != len(outcomes):
        raise ValueError(
            f"Mismatched input lengths: forecasts({len(forecasts)}) != outcomes({len(outcomes)})"
        )

    total_sq_err = sum((f - o) ** 2 for f, o in zip(forecasts, outcomes))
    return total_sq_err / len(forecasts)


def calculate_brier_skill_score(
    model_forecasts: list[float],
    reference_forecasts: list[float],
    outcomes: list[float],
) -> float | None:
    """Calculate the Brier Skill Score (BSS) benchmarking model against a reference.

    BSS = 1 - (BS_model / BS_ref)

    A positive score indicates the model is more accurate and better calibrated
    than the reference baseline (e.g. market-implied prices).

    Args:
        model_forecasts: Predicted probabilities from the model (Jev).
        reference_forecasts: Baseline probabilities (e.g. market-implied Yes ask).
        outcomes: Binary outcomes (1.0 or 0.0).

    Returns:
        Brier Skill Score float, or None if inputs are insufficient.
    """
    if not model_forecasts or len(model_forecasts) != len(outcomes) or len(reference_forecasts) != len(outcomes):
        return None

    bs_model = calculate_brier_score(model_forecasts, outcomes)
    bs_ref = calculate_brier_score(reference_forecasts, outcomes)

    if bs_ref == 0.0:
        return 1.0 if bs_model == 0.0 else -math.inf

    return 1.0 - (bs_model / bs_ref)


def calculate_calibration_bins(
    forecasts: list[float],
    outcomes: list[float],
    num_bins: int = 10,
) -> list[dict[str, Any]]:
    """Partition predictions into equal-width bins for reliability diagram analysis.

    Args:
        forecasts: List of forecast probabilities in [0.0, 1.0].
        outcomes: List of ground-truth binary outcomes (0.0 or 1.0).
        num_bins: Number of probability bins (default 10: 0-10%, 10-20%, etc.).

    Returns:
        List of bin dicts with bin ranges, counts, mean forecast, observed freq,
        and calibration error.
    """
    if not forecasts or len(forecasts) != len(outcomes) or num_bins <= 0:
        return []

    bin_width = 1.0 / num_bins
    bins_data: list[dict[str, Any]] = []

    for b in range(num_bins):
        lower = b * bin_width
        upper = (b + 1) * bin_width
        # Include upper boundary 1.0 in final bin
        is_last = (b == num_bins - 1)

        b_forecasts: list[float] = []
        b_outcomes: list[float] = []

        for f, o in zip(forecasts, outcomes):
            if lower <= f < upper or (is_last and f == upper):
                b_forecasts.append(f)
                b_outcomes.append(o)

        count = len(b_forecasts)
        mean_forecast = (sum(b_forecasts) / count) if count > 0 else (lower + upper) / 2.0
        observed_freq = (sum(b_outcomes) / count) if count > 0 else 0.0
        cal_err = abs(mean_forecast - observed_freq) if count > 0 else 0.0

        bins_data.append({
            "bin_index": b,
            "range": f"[{lower:.1f}, {upper:.1f}{']' if is_last else ')'}",
            "lower": lower,
            "upper": upper,
            "center": (lower + upper) / 2.0,
            "count": count,
            "mean_forecast": round(mean_forecast, 4),
            "observed_frequency": round(observed_freq, 4),
            "calibration_error": round(cal_err, 4),
        })

    return bins_data


def calculate_expected_calibration_error(
    bins: list[dict[str, Any]],
    total_samples: int,
) -> float:
    """Calculate the Expected Calibration Error (ECE) across reliability bins.

    ECE = sum((N_k / N) * |mean_f_k - obs_freq_k|)

    Args:
        bins: Output list from calculate_calibration_bins.
        total_samples: Total number of evaluated prediction samples.

    Returns:
        Weighted average calibration error (0.0 to 1.0).
    """
    if total_samples <= 0 or not bins:
        return 0.0

    ece = sum((b["count"] / total_samples) * b["calibration_error"] for b in bins if b["count"] > 0)
    return round(ece, 4)


def calculate_edge_realization(decisions: list[dict[str, Any]], standard_stake: float = 50.0) -> dict[str, Any]:
    """Evaluate realized trading performance for decisions with recommended actions.

    Simulates returns on decisions where Jev recommended 'buy_yes' or 'buy_no'.

    Args:
        decisions: List of decision dicts from DB with resolved_outcome.
        standard_stake: Notional dollar stake per opportunity.

    Returns:
        Dict with total_recommended, wins, losses, win_rate, total_pnl, roi.
    """
    recommended = [d for d in decisions if d.get("action") in ("buy_yes", "buy_no") and d.get("resolved_outcome") is not None]

    if not recommended:
        return {
            "total_recommended": 0,
            "wins": 0,
            "losses": 0,
            "win_rate": 0.0,
            "total_pnl": 0.0,
            "total_cost": 0.0,
            "roi": 0.0,
        }

    wins = 0
    losses = 0
    total_pnl = 0.0
    total_cost = 0.0

    for d in recommended:
        action = d["action"]
        outcome = float(d["resolved_outcome"])
        # Market price at entry
        mkt_p = float(d.get("market_prob") or 0.5)

        if action == "buy_yes":
            cost = standard_stake * mkt_p
            # Payout: $1 per share if YES won (outcome == 1.0), else 0
            payout = standard_stake if outcome == 1.0 else 0.0
            pnl = payout - cost
            if outcome == 1.0:
                wins += 1
            else:
                losses += 1
        else:  # buy_no
            no_p = 1.0 - mkt_p
            cost = standard_stake * no_p
            # Payout: $1 per share if NO won (outcome == 0.0), else 0
            payout = standard_stake if outcome == 0.0 else 0.0
            pnl = payout - cost
            if outcome == 0.0:
                wins += 1
            else:
                losses += 1

        total_cost += cost
        total_pnl += pnl

    win_rate = (wins / len(recommended)) if recommended else 0.0
    roi = (total_pnl / total_cost) if total_cost > 0 else 0.0

    return {
        "total_recommended": len(recommended),
        "wins": wins,
        "losses": losses,
        "win_rate": round(win_rate, 4),
        "total_pnl": round(total_pnl, 2),
        "total_cost": round(total_cost, 2),
        "roi": round(roi, 4),
    }


# ---------------------------------------------------------------------------
# Report Generator
# ---------------------------------------------------------------------------


def generate_calibration_report(
    db: TradeDB,
    asset: str | None = None,
    num_bins: int = 10,
) -> dict[str, Any]:
    """Generate a comprehensive empirical calibration and Brier score report.

    Args:
        db: Initialized TradeDB instance.
        asset: Optional asset symbol filter (e.g. 'BTC', 'ETH', 'SOL', 'XRP', 'all').
        num_bins: Number of bins for reliability diagram (default 10).

    Returns:
        Structured dictionary containing metrics, per-asset breakdown, bins, and diagram.
    """
    target_asset = None if (not asset or asset.lower() == "all") else asset.upper()

    all_decisions = db.get_jev_decisions(asset=target_asset, limit=2000)
    resolved_decisions = db.get_jev_calibration_data(asset=target_asset, only_resolved=True, limit=2000)

    total_logged = len(all_decisions)
    total_resolved = len(resolved_decisions)

    if total_resolved == 0:
        return {
            "status": "pending_resolutions",
            "asset_filter": asset or "all",
            "total_logged": total_logged,
            "total_resolved": 0,
            "message": "No resolved decisions found in database. Run sync_jev_resolutions.py to populate ground truth outcomes.",
            "per_asset": {},
            "bins": [],
            "ascii_diagram": "",
        }

    jev_forecasts = [float(d["jev_prob"]) for d in resolved_decisions]
    mkt_forecasts = [float(d["market_prob"] or 0.5) for d in resolved_decisions]
    outcomes = [float(d["resolved_outcome"]) for d in resolved_decisions]

    jev_bs = calculate_brier_score(jev_forecasts, outcomes)
    mkt_bs = calculate_brier_score(mkt_forecasts, outcomes)
    bss = calculate_brier_skill_score(jev_forecasts, mkt_forecasts, outcomes)

    bins = calculate_calibration_bins(jev_forecasts, outcomes, num_bins=num_bins)
    ece = calculate_expected_calibration_error(bins, total_samples=total_resolved)
    edge_stats = calculate_edge_realization(resolved_decisions)

    # Per-asset breakdown
    assets = sorted(list(set(d["asset"] for d in all_decisions)))
    per_asset: dict[str, Any] = {}

    for sym in assets:
        sym_logged = [d for d in all_decisions if d["asset"] == sym]
        sym_resolved = [d for d in resolved_decisions if d["asset"] == sym]

        if not sym_resolved:
            per_asset[sym] = {
                "logged": len(sym_logged),
                "resolved": 0,
                "jev_brier_score": None,
                "market_brier_score": None,
                "brier_skill_score": None,
                "ece": None,
            }
            continue

        s_jev = [float(d["jev_prob"]) for d in sym_resolved]
        s_mkt = [float(d["market_prob"] or 0.5) for d in sym_resolved]
        s_out = [float(d["resolved_outcome"]) for d in sym_resolved]

        s_jbs = calculate_brier_score(s_jev, s_out)
        s_mbs = calculate_brier_score(s_mkt, s_out)
        s_bss = calculate_brier_skill_score(s_jev, s_mkt, s_out)
        s_bins = calculate_calibration_bins(s_jev, s_out, num_bins=num_bins)
        s_ece = calculate_expected_calibration_error(s_bins, total_samples=len(sym_resolved))

        per_asset[sym] = {
            "logged": len(sym_logged),
            "resolved": len(sym_resolved),
            "jev_brier_score": round(s_jbs, 4),
            "market_brier_score": round(s_mbs, 4),
            "brier_skill_score": round(s_bss, 4) if s_bss is not None else None,
            "ece": s_ece,
        }

    ascii_diagram = render_ascii_reliability_diagram(bins)

    return {
        "status": "success",
        "asset_filter": asset or "all",
        "total_logged": total_logged,
        "total_resolved": total_resolved,
        "jev_brier_score": round(jev_bs, 4),
        "market_brier_score": round(mkt_bs, 4),
        "brier_skill_score": round(bss, 4) if bss is not None else None,
        "expected_calibration_error": ece,
        "edge_realization": edge_stats,
        "per_asset": per_asset,
        "bins": bins,
        "ascii_diagram": ascii_diagram,
    }


# ---------------------------------------------------------------------------
# Visual ASCII Reliability Diagram
# ---------------------------------------------------------------------------


def render_ascii_reliability_diagram(bins: list[dict[str, Any]], width: int = 40, height: int = 10) -> str:
    """Render an ASCII calibration plot (reliability curve vs diagonal 45-degree line).

    Plot axis:
    Y-axis: Observed empirical frequency (0.0 to 1.0)
    X-axis: Mean forecast probability (0.0 to 1.0)
    '.' = Ideal 45-degree perfect calibration line
    '*' = Observed empirical bin point
    """
    if not bins:
        return "No bin data available to render diagram."

    # Initialize 2D grid
    grid = [[" " for _ in range(width)] for _ in range(height)]

    # Draw ideal diagonal line
    for c in range(width):
        norm_x = c / (width - 1)
        r = int(round((1.0 - norm_x) * (height - 1)))
        if 0 <= r < height:
            grid[r][c] = "."

    # Plot empirical points
    for b in bins:
        if b["count"] == 0:
            continue
        fx = b["mean_forecast"]
        oy = b["observed_frequency"]

        c = int(round(fx * (width - 1)))
        r = int(round((1.0 - oy) * (height - 1)))

        c = max(0, min(width - 1, c))
        r = max(0, min(height - 1, r))

        grid[r][c] = "*"

    lines: list[str] = []
    lines.append("   Reliability Diagram (Observed vs Forecast)")
    lines.append("   1.0 |" + "".join(grid[0]))
    for r in range(1, height - 1):
        y_val = 1.0 - (r / (height - 1))
        label = f"   {y_val:.1f} |"
        lines.append(label + "".join(grid[r]))
    lines.append("   0.0 |" + "".join(grid[height - 1]))
    lines.append("       +" + "-" * width)
    lines.append("        0.0" + " " * (width - 7) + "1.0")
    lines.append("        Legend:  (·) Ideal Calibration   (*) Empirical Observed")

    return "\n".join(lines)
