"""Tests for the unified cross-engine P&L aggregation core."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pnl_ledger import (  # noqa: E402
    PnlEntry,
    aggregate_pnl,
    clears_hurdle,
)


def _e(engine, lane, bucket, amount, trade_date="2026-06-13"):
    return PnlEntry(
        engine=engine, lane=lane, tax_bucket=bucket, amount_usd=amount, trade_date=trade_date
    )


class TestPnlLedger:
    # ---------------------------------------------------------------------------
    # Entry validation (tagging must be correct from trade one)
    # ---------------------------------------------------------------------------

    def test_rejects_unknown_tax_bucket(self):
        with pytest.raises(ValueError, match="tax_bucket"):
            _e("arbgrid", "prediction-markets", "capital_gains", 10.0)

    def test_requires_engine_and_lane(self):
        with pytest.raises(ValueError):
            _e("", "prediction-markets", "ordinary", 10.0)
        with pytest.raises(ValueError):
            _e("arbgrid", "", "ordinary", 10.0)

    def test_rejects_whitespace_only_tags(self):
        with pytest.raises(ValueError, match="engine and lane"):
            _e("   ", "prediction-markets", "ordinary", 10.0)

    def test_rejects_non_iso_trade_date(self):
        with pytest.raises(ValueError, match="ISO"):
            _e("arbgrid", "x", "ordinary", 1.0, trade_date="06/13/2026")

    def test_rejects_non_string_inputs(self):
        # None / int bypass strip() and fromisoformat() — must normalize to ValueError.
        with pytest.raises(ValueError):
            PnlEntry(engine=None, lane="x", tax_bucket="ordinary", amount_usd=1.0, trade_date="2026-06-13")
        with pytest.raises(ValueError, match="ISO"):
            PnlEntry(engine="arbgrid", lane="x", tax_bucket="ordinary", amount_usd=1.0, trade_date=20260613)

    def test_accepts_each_valid_bucket(self):
        for bucket in ("ordinary", "possible_1256", "gambling"):
            assert _e("arbgrid", "x", bucket, 1.0).tax_bucket == bucket

    # ---------------------------------------------------------------------------
    # Aggregation
    # ---------------------------------------------------------------------------

    def test_rolls_up_by_engine_lane_and_bucket(self):
        entries = [
            _e("arbgrid", "prediction-markets", "ordinary", 100.0),
            _e("arbgrid", "prediction-markets", "ordinary", -30.0),
            _e("quant", "perp_carry", "possible_1256", 50.0),
            _e("arbgrid", "sports", "gambling", 20.0),
        ]
        s = aggregate_pnl(entries)
        assert s.total_usd == pytest.approx(140.0)
        assert s.by_engine == {"arbgrid": pytest.approx(90.0), "quant": pytest.approx(50.0)}
        assert s.by_lane["prediction-markets"] == pytest.approx(70.0)
        assert s.by_lane["perp_carry"] == pytest.approx(50.0)
        assert s.by_tax_bucket == {
            "ordinary": pytest.approx(70.0),
            "possible_1256": pytest.approx(50.0),
            "gambling": pytest.approx(20.0),
        }

    def test_empty_is_zero(self):
        s = aggregate_pnl([])
        assert s.total_usd == 0.0
        assert s.by_engine == {}

    # ---------------------------------------------------------------------------
    # Capital-policy hurdle (4.70% LOC floor)
    # ---------------------------------------------------------------------------

    def test_hurdle_cleared_when_pnl_beats_loc_floor(self):
        # $10K deployed at the 4.70% LOC rate for a quarter → hurdle ≈ $117.
        cleared, hurdle = clears_hurdle(200.0, 0.047, 10_000.0, 91.25)
        assert hurdle == pytest.approx(117.5, abs=1.0)
        assert cleared is True

    def test_hurdle_not_cleared_when_pnl_below_floor(self):
        cleared, hurdle = clears_hurdle(50.0, 0.047, 10_000.0, 91.25)
        assert cleared is False
        assert hurdle > 50.0

    def test_hurdle_false_on_zero_capital(self):
        assert clears_hurdle(100.0, 0.047, 0.0, 30.0) == (False, 0.0)

    def test_hurdle_rejects_negative_rate(self):
        # A negative policy floor would mark almost any P&L as "cleared" — reject it.
        with pytest.raises(ValueError, match="non-negative"):
            clears_hurdle(100.0, -0.01, 10_000.0, 30.0)
