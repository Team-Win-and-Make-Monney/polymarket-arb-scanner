"""Tests for the weather paper-rule decision core (paper only)."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from weather_paper_rules import (  # noqa: E402
    PaperSide,
    WeatherSignal,
    calibrate,
    decide_paper_trade,
)


def _sig(model, market, fee=0.0):
    return WeatherSignal(market="NYC high >= 90F", model_prob=model, market_yes_prob=market, fee_per_contract=fee)


class TestWeatherPaperRules:
    # -----------------------------------------------------------------------
    # Calibration
    # -----------------------------------------------------------------------

    def test_calibrate_shifts_and_clamps(self):
        assert calibrate(0.50, 0.10) == pytest.approx(0.60)
        assert calibrate(0.95, 0.20) == 1.0     # clamped
        assert calibrate(0.05, -0.20) == 0.0    # clamped

    # -----------------------------------------------------------------------
    # Decision
    # -----------------------------------------------------------------------

    def test_buy_yes_when_model_above_market(self):
        d = decide_paper_trade(_sig(model=0.70, market=0.55))
        assert d.side is PaperSide.YES
        assert d.edge == pytest.approx(0.15)

    def test_buy_no_when_model_below_market(self):
        d = decide_paper_trade(_sig(model=0.40, market=0.60))
        assert d.side is PaperSide.NO
        assert d.edge == pytest.approx(0.20)

    def test_no_trade_when_aligned(self):
        assert decide_paper_trade(_sig(model=0.50, market=0.50)).side is PaperSide.NONE

    def test_fee_can_erase_the_edge(self):
        # 4-pt raw YES edge, but a 5¢ round-trip fee → below the 3% threshold.
        d = decide_paper_trade(_sig(model=0.54, market=0.50, fee=0.05))
        assert d.side is PaperSide.NONE

    def test_edge_just_below_threshold_is_no_trade(self):
        # 2-pt edge < 3% default threshold.
        assert decide_paper_trade(_sig(model=0.52, market=0.50)).side is PaperSide.NONE

    def test_custom_min_edge_lowers_the_bar(self):
        sig = _sig(model=0.52, market=0.50)
        assert decide_paper_trade(sig, min_edge=0.01).side is PaperSide.YES
