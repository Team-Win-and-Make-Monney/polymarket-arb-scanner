"""Unified cross-engine P&L ledger — aggregation core (data-layer integration).

The portfolio integrates ONLY at the data layer: every fill/period across every
engine (arbgrid, quant-engine, ...) lands in one shared ``pnl`` table tagged with
``engine``, ``lane``, and ``tax_bucket``. This is the pure roll-up the daily
digest renders and the day-90 review consolidates: P&L by engine, by lane, and
by tax bucket — plus the capital-policy hurdle check (did a lane beat the 4.70%
LOC floor, or should its capital go to the LOC instead?).

Tax buckets are fixed (the three-bucket model): ``ordinary``, ``possible_1256``
(US-DCM perps / Kalshi), and ``gambling`` (sports). An entry with any other
bucket is rejected — tagging must be correct from trade one.

Pure + deterministic, no I/O. The Supabase schema is
``supabase/migrations/0003_pnl_schema.sql``; the row-sync and the digest
formatter (``digest.py``) are separate.
"""
from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import dataclass
from datetime import date

logger = logging.getLogger(__name__)

# The fixed three-bucket tax model (see command-center 14-TAX-TRACKER).
TAX_BUCKETS = frozenset({"ordinary", "possible_1256", "gambling"})


@dataclass(frozen=True)
class PnlEntry:
    engine: str          # 'arbgrid' | 'quant' | ...
    lane: str            # 'prediction-markets' | 'perp_carry' | 'sports' | ...
    tax_bucket: str      # must be in TAX_BUCKETS
    amount_usd: float    # signed realized P&L for this fill/period
    trade_date: str      # ISO date (YYYY-MM-DD)

    def __post_init__(self) -> None:
        if self.tax_bucket not in TAX_BUCKETS:
            raise ValueError(f"tax_bucket {self.tax_bucket!r} not in {sorted(TAX_BUCKETS)}")
        if (not isinstance(self.engine, str) or not isinstance(self.lane, str)
                or not self.engine.strip() or not self.lane.strip()):
            raise ValueError("engine and lane must be non-empty strings on every P&L entry")
        if not isinstance(self.trade_date, str):
            raise ValueError("trade_date must be ISO format YYYY-MM-DD")
        try:
            date.fromisoformat(self.trade_date)
        except ValueError as exc:
            raise ValueError("trade_date must be ISO format YYYY-MM-DD") from exc


@dataclass(frozen=True)
class PnlSummary:
    total_usd: float
    by_engine: dict[str, float]
    by_lane: dict[str, float]
    by_tax_bucket: dict[str, float]


def aggregate_pnl(entries) -> PnlSummary:
    """Roll up signed P&L by engine, lane, and tax bucket."""
    by_engine: dict[str, float] = defaultdict(float)
    by_lane: dict[str, float] = defaultdict(float)
    by_bucket: dict[str, float] = defaultdict(float)
    total = 0.0
    for e in entries:
        total += e.amount_usd
        by_engine[e.engine] += e.amount_usd
        by_lane[e.lane] += e.amount_usd
        by_bucket[e.tax_bucket] += e.amount_usd
    return PnlSummary(
        total_usd=total,
        by_engine=dict(by_engine),
        by_lane=dict(by_lane),
        by_tax_bucket=dict(by_bucket),
    )


def clears_hurdle(
    realized_usd: float,
    hurdle_rate_annual: float,
    deployed_capital_usd: float,
    days_held: float,
) -> tuple[bool, float]:
    """Did realized P&L beat the capital-policy hurdle over the period?

    The policy: a lane that can't beat the 4.70% LOC rate (after tax + labor)
    should send its capital to the LOC instead. Returns ``(cleared, hurdle_usd)``.
    Non-positive capital or days returns ``(False, 0.0)`` — nothing to beat.
    The hurdle rate is a policy floor; a negative rate is rejected so a config
    mistake can't silently mark a losing lane as having cleared the bar.
    """
    if hurdle_rate_annual < 0:
        raise ValueError("hurdle_rate_annual is a policy floor and must be non-negative")
    if deployed_capital_usd <= 0 or days_held <= 0:
        return False, 0.0
    hurdle_usd = deployed_capital_usd * hurdle_rate_annual * (days_held / 365.0)
    return realized_usd >= hurdle_usd, hurdle_usd
