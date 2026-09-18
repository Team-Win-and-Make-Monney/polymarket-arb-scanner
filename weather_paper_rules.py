"""Weather paper-trading rules — decision core (paper only, instrumentation).

Given a calibrated model probability for a Kalshi weather event (from NBM, the
National Blend of Models) and the market's implied probability, decide a PAPER
position when the fee-adjusted edge clears a threshold. Paper only — this is the
instrumentation that logs would-be trades toward the 7/31 verdict (≥300 paper
trades, ≥+2¢/contract net, t≥2 → $500 live ramp). It never places an order.

NBM forecasts are not perfectly calibrated, so a calibration shift (derived from
the 3-year NBM error CDFs — a separate data pipeline) is applied to the raw model
probability via ``calibrate`` before it is compared to the market. This core
takes the calibrated probability + the market price + the fee and decides.

Pure + deterministic.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class PaperSide(str, Enum):
    YES = "yes"
    NO = "no"
    NONE = "none"


@dataclass(frozen=True)
class WeatherSignal:
    market: str             # Kalshi weather market label
    model_prob: float       # calibrated NBM probability of YES, in [0, 1]
    market_yes_prob: float  # market implied YES probability, in [0, 1] (yes_price / 100)
    fee_per_contract: float = 0.0  # round-trip fee as a probability fraction ($0.01 -> 0.01)


@dataclass(frozen=True)
class PaperDecision:
    side: PaperSide
    edge: float             # fee-adjusted edge on the chosen side (or the best available)
    note: str


def calibrate(raw_prob: float, shift: float = 0.0) -> float:
    """Apply the NBM calibration shift and clamp to [0, 1]."""
    return max(0.0, min(1.0, raw_prob + shift))


def decide_paper_trade(signal: WeatherSignal, min_edge: float = 0.03) -> PaperDecision:
    """Decide a paper position when the fee-adjusted edge clears ``min_edge``.

    YES edge = model - market - fee  (model thinks YES is underpriced -> buy YES)
    NO  edge = market - model - fee  (model thinks YES is overpriced  -> buy NO)
    Take the larger side if it clears the threshold; otherwise no trade.
    """
    p = signal.model_prob
    m = signal.market_yes_prob
    fee = signal.fee_per_contract
    yes_edge = p - m - fee
    no_edge = m - p - fee

    if yes_edge >= min_edge and yes_edge >= no_edge:
        return PaperDecision(PaperSide.YES, yes_edge, f"model {p:.2f} > market {m:.2f} — paper BUY YES")
    if no_edge >= min_edge:
        return PaperDecision(PaperSide.NO, no_edge, f"model {p:.2f} < market {m:.2f} — paper BUY NO")
    return PaperDecision(PaperSide.NONE, max(yes_edge, no_edge), "edge below threshold — no paper trade")
