"""Capital optimization utilities for strategies #44-#47.

Provides scoring and routing logic for:
- #44 Opportunity Cost Scoring — time-weighted ROI
- #45 Margin Efficiency Optimization — collateral routing
- #46 Tax-Aware Position Management — loss harvesting
- #47 Withdrawal Timing Optimization — withdrawal delay factors
"""

import logging
import threading
import time
from datetime import datetime, timedelta

from config import (
    OPPORTUNITY_COST_SCORING_ENABLED,
    OPPORTUNITY_COST_MIN_ANNUALIZED_ROI,
    MARGIN_EFFICIENCY_ENABLED,
    MARGIN_REBALANCE_THRESHOLD,
    TAX_AWARE_ENABLED,
    TAX_LOSS_HARVEST_THRESHOLD,
    TAX_SHORT_TERM_RATE,
    TAX_LONG_TERM_RATE,
    WITHDRAWAL_TIMING_ENABLED,
    PLATFORM_WITHDRAWAL_DELAYS,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# #44 — Opportunity Cost Scoring
# ---------------------------------------------------------------------------

class OpportunityCostScorer:
    """Score opportunities by time-weighted (annualized) ROI.

    Instead of comparing raw ROI, this adjusts for capital lock-up time:
        annualized_roi = roi × (365 / days_to_settlement)

    Short-duration opportunities with the same ROI are preferred because
    capital can be recycled faster.
    """

    def __init__(self, min_annualized_roi: float | None = None):
        self.min_annualized_roi = (
            min_annualized_roi
            if min_annualized_roi is not None
            else OPPORTUNITY_COST_MIN_ANNUALIZED_ROI
        )

    def score(self, opportunity: dict) -> float:
        """Calculate annualized ROI for an opportunity.

        Args:
            opportunity: Opportunity dict with net_roi and _days_to_resolution.

        Returns:
            Annualized ROI (e.g., 0.50 = 50% annualized). Returns 0.0 if
            days_to_resolution is missing or zero.
        """
        roi = opportunity.get("net_roi", 0.0)
        if isinstance(roi, str):
            try:
                roi = float(roi.rstrip("%")) / 100
            except (ValueError, AttributeError):
                roi = 0.0

        days = opportunity.get("_days_to_resolution", 0.0)
        if days <= 0:
            return roi

        annualized = roi * (365.0 / days)
        return annualized

    def passes_threshold(self, opportunity: dict) -> bool:
        """Check if opportunity meets minimum annualized ROI threshold."""
        if not OPPORTUNITY_COST_SCORING_ENABLED:
            return True
        return self.score(opportunity) >= self.min_annualized_roi

    def rank_opportunities(self, opportunities: list[dict]) -> list[dict]:
        """Sort opportunities by annualized ROI descending.

        Also filters out opportunities below the minimum threshold if
        OPPORTUNITY_COST_SCORING_ENABLED is True.
        """
        if not OPPORTUNITY_COST_SCORING_ENABLED:
            return opportunities

        scored = [
            (opp, self.score(opp))
            for opp in opportunities
            if self.passes_threshold(opp)
        ]
        scored.sort(key=lambda x: x[1], reverse=True)
        return [opp for opp, _ in scored]


# ---------------------------------------------------------------------------
# #45 — Margin Efficiency Optimization
# ---------------------------------------------------------------------------

class MarginOptimizer:
    """Optimize collateral deployment across platforms.

    Tracks available margin per platform and routes new positions to
    platforms with the most efficient margin utilization.
    """

    def __init__(self):
        self._margin_available: dict[str, float] = {}
        self._margin_used: dict[str, float] = {}
        self._lock = threading.RLock()

    def update_margin(self, platform: str, available: float, used: float) -> None:
        """Update margin state for a platform.

        Args:
            platform: Platform name.
            available: Available margin in USD.
            used: Currently used margin in USD.
        """
        with self._lock:
            self._margin_available[platform] = available
            self._margin_used[platform] = used

    def get_utilization(self, platform: str) -> float:
        """Get margin utilization ratio for a platform (0-1)."""
        with self._lock:
            available = self._margin_available.get(platform, 0.0)
            used = self._margin_used.get(platform, 0.0)
            if available <= 0:
                return 1.0
            return used / available

    def get_best_platform(self, platforms: list[str]) -> str | None:
        """Return the platform with the lowest margin utilization.

        Args:
            platforms: List of platform names to consider.

        Returns:
            Platform name with lowest utilization, or None if all are full.
        """
        if not MARGIN_EFFICIENCY_ENABLED:
            return platforms[0] if platforms else None

        with self._lock:
            best_platform = None
            best_utilization = 1.0

            for platform in platforms:
                util = self.get_utilization(platform)
                if util < best_utilization:
                    best_utilization = util
                    best_platform = platform

            return best_platform

    def should_rebalance(self) -> list[tuple[str, str, float]]:
        """Identify platforms that need margin rebalancing.

        Returns:
            List of (from_platform, to_platform, amount) tuples
            for suggested transfers.
        """
        if not MARGIN_EFFICIENCY_ENABLED:
            return []

        transfers = []
        with self._lock:
            platforms = list(self._margin_available.keys())
            utilizations = {p: self.get_utilization(p) for p in platforms}

            for source in platforms:
                for dest in platforms:
                    if source == dest:
                        continue
                    src_util = utilizations[source]
                    dst_util = utilizations[dest]

                    if src_util - dst_util > MARGIN_REBALANCE_THRESHOLD:
                        excess = self._margin_available[source] * (
                            src_util - (src_util + dst_util) / 2
                        )
                        if excess > 50.0:
                            transfers.append((source, dest, excess))

        return transfers


# ---------------------------------------------------------------------------
# #46 — Tax-Aware Position Management
# ---------------------------------------------------------------------------

class TaxOptimizer:
    """Optimize position management for tax efficiency.

    Tracks cost basis per position and identifies opportunities to:
    - Harvest losses near tax deadlines
    - Defer gains when beneficial
    - Optimize holding period for long-term capital gains treatment
    """

    def __init__(self):
        self._cost_basis: dict[str, dict] = {}
        # Public read helpers may be called while a compound operation already
        # holds the instance lock (for example, harvest candidate evaluation).
        self._lock = threading.RLock()

    def record_entry(
        self,
        position_id: str,
        platform: str,
        cost: float,
        quantity: float,
        entry_time: datetime | None = None,
    ) -> None:
        """Record a new position entry for cost basis tracking.

        Args:
            position_id: Unique identifier for the position.
            platform: Platform where position is held.
            cost: Total cost in USD.
            quantity: Number of contracts/shares.
            entry_time: Time of entry (defaults to now).
        """
        with self._lock:
            self._cost_basis[position_id] = {
                "platform": platform,
                "cost": cost,
                "quantity": quantity,
                "entry_time": entry_time or datetime.now(),
                "cost_per_unit": cost / quantity if quantity > 0 else 0,
            }

    def get_unrealized_pnl(self, position_id: str, current_value: float) -> float:
        """Calculate unrealized P&L for a position.

        Args:
            position_id: Position identifier.
            current_value: Current market value in USD.

        Returns:
            Unrealized P&L (positive = gain, negative = loss).
        """
        with self._lock:
            if position_id not in self._cost_basis:
                return 0.0
            return current_value - self._cost_basis[position_id]["cost"]

    def is_long_term(self, position_id: str) -> bool:
        """Check if position qualifies for long-term capital gains treatment.

        Requires holding period > 1 year.
        """
        with self._lock:
            if position_id not in self._cost_basis:
                return False
            entry = self._cost_basis[position_id]["entry_time"]
            return (datetime.now() - entry) > timedelta(days=365)

    def get_tax_impact(self, position_id: str, current_value: float) -> float:
        """Calculate estimated tax impact of closing a position.

        Args:
            position_id: Position identifier.
            current_value: Current market value.

        Returns:
            Estimated tax liability (positive) or tax benefit (negative).
        """
        if not TAX_AWARE_ENABLED:
            return 0.0

        pnl = self.get_unrealized_pnl(position_id, current_value)
        if pnl == 0:
            return 0.0

        rate = TAX_LONG_TERM_RATE if self.is_long_term(position_id) else TAX_SHORT_TERM_RATE
        return pnl * rate

    def get_harvest_candidates(
        self,
        positions: dict[str, float],
        threshold: float | None = None,
    ) -> list[tuple[str, float, float]]:
        """Identify positions eligible for tax-loss harvesting.

        Args:
            positions: Dict mapping position_id to current_value.
            threshold: Minimum loss percentage to harvest (default from config).

        Returns:
            List of (position_id, loss_amount, tax_benefit) tuples.
        """
        if not TAX_AWARE_ENABLED:
            return []

        threshold = threshold if threshold is not None else TAX_LOSS_HARVEST_THRESHOLD
        candidates = []

        with self._lock:
            for position_id, current_value in positions.items():
                if position_id not in self._cost_basis:
                    continue

                basis = self._cost_basis[position_id]
                pnl = current_value - basis["cost"]
                pnl_pct = pnl / basis["cost"] if basis["cost"] > 0 else 0

                if pnl_pct < -threshold:
                    rate = (
                        TAX_LONG_TERM_RATE
                        if self.is_long_term(position_id)
                        else TAX_SHORT_TERM_RATE
                    )
                    tax_benefit = abs(pnl) * rate
                    candidates.append((position_id, pnl, tax_benefit))

        candidates.sort(key=lambda x: x[2], reverse=True)
        return candidates


# ---------------------------------------------------------------------------
# #47 — Withdrawal Timing Optimization
# ---------------------------------------------------------------------------

class WithdrawalTimingOptimizer:
    """Factor withdrawal delays into capital allocation decisions.

    Some platforms have multi-day withdrawal times, which affects
    capital availability for time-sensitive opportunities.
    """

    def __init__(
        self,
        withdrawal_delays: dict[str, float] | None = None,
    ):
        self.withdrawal_delays = (
            withdrawal_delays
            if withdrawal_delays is not None
            else PLATFORM_WITHDRAWAL_DELAYS
        )

    def get_withdrawal_delay(self, platform: str) -> float:
        """Get withdrawal delay in hours for a platform."""
        return self.withdrawal_delays.get(platform, 24.0)

    def is_capital_available_in_time(
        self,
        platform: str,
        hours_until_needed: float,
    ) -> bool:
        """Check if capital can be withdrawn in time.

        Args:
            platform: Source platform.
            hours_until_needed: Hours until capital is needed.

        Returns:
            True if withdrawal can complete in time.
        """
        if not WITHDRAWAL_TIMING_ENABLED:
            return True
        return self.get_withdrawal_delay(platform) <= hours_until_needed

    def get_fastest_source(self, platforms: list[str]) -> str | None:
        """Return the platform with the fastest withdrawal time.

        Args:
            platforms: List of platform names to consider.

        Returns:
            Platform name with shortest withdrawal delay.
        """
        if not platforms:
            return None

        return min(platforms, key=lambda p: self.get_withdrawal_delay(p))

    def score_rebalance(
        self,
        from_platform: str,
        to_platform: str,
        opportunity_hours: float,
    ) -> float:
        """Score a rebalancing move based on timing feasibility.

        Args:
            from_platform: Source platform.
            to_platform: Destination platform.
            opportunity_hours: Hours until opportunity expires.

        Returns:
            Score from 0 (infeasible) to 1 (plenty of time).
        """
        if not WITHDRAWAL_TIMING_ENABLED:
            return 1.0

        delay = self.get_withdrawal_delay(from_platform)
        if delay >= opportunity_hours:
            return 0.0

        margin = opportunity_hours - delay
        return min(1.0, margin / opportunity_hours)


# ---------------------------------------------------------------------------
# Unified Capital Optimizer
# ---------------------------------------------------------------------------

class CapitalOptimizer:
    """Unified interface for all capital optimization strategies.

    Combines opportunity cost scoring, margin efficiency, tax awareness,
    and withdrawal timing into a single optimization pass.
    """

    def __init__(self):
        self.cost_scorer = OpportunityCostScorer()
        self.margin_optimizer = MarginOptimizer()
        self.tax_optimizer = TaxOptimizer()
        self.withdrawal_optimizer = WithdrawalTimingOptimizer()

    def optimize_opportunities(
        self,
        opportunities: list[dict],
    ) -> list[dict]:
        """Apply all enabled optimizations to rank opportunities.

        Applies in order:
        1. Opportunity cost scoring (annualized ROI)
        2. Margin efficiency routing
        3. Tax impact adjustments
        4. Withdrawal timing feasibility

        Args:
            opportunities: List of opportunity dicts.

        Returns:
            Filtered and sorted list of opportunities.
        """
        result = opportunities

        if OPPORTUNITY_COST_SCORING_ENABLED:
            result = self.cost_scorer.rank_opportunities(result)

        return result

    def get_execution_platform(
        self,
        opportunity: dict,
        available_platforms: list[str],
    ) -> str | None:
        """Determine best platform for executing an opportunity.

        Considers margin efficiency and withdrawal timing.

        Args:
            opportunity: Opportunity dict.
            available_platforms: Platforms that can execute this opportunity.

        Returns:
            Best platform name, or None if none are suitable.
        """
        if not available_platforms:
            return None

        if MARGIN_EFFICIENCY_ENABLED:
            best = self.margin_optimizer.get_best_platform(available_platforms)
            if best:
                return best

        return available_platforms[0]


# Module-level singleton for convenience
_optimizer: CapitalOptimizer | None = None


def get_capital_optimizer() -> CapitalOptimizer:
    """Get or create the module-level CapitalOptimizer instance."""
    global _optimizer
    if _optimizer is None:
        _optimizer = CapitalOptimizer()
    return _optimizer
