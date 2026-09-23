"""Funnel telemetry tracker for prediction market arbitrage detection.

Tracks the full lifecycle of opportunities through detection stages:
Stage 1 (Mid-price screening) -> Stage 2 (CLOB orderbook refine) ->
Stage 3 (Fee & liquidity/depth thresholds) -> Surfaced for execution.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# FunnelStats dataclass
# ---------------------------------------------------------------------------

@dataclass
class FunnelStats:
    """Funnel metrics for a single scan cycle or cumulative run."""

    screened: int = 0
    mid_candidates: int = 0
    clob_evaluated: int = 0
    clob_dropped: int = 0
    fee_dropped: int = 0
    roi_dropped: int = 0
    depth_dropped: int = 0
    surfaced: int = 0

    def to_dict(self) -> dict[str, int]:
        """Convert stats to a dictionary for dashboard and API exposure."""
        return {
            "screened": self.screened,
            "mid_candidates": self.mid_candidates,
            "clob_evaluated": self.clob_evaluated,
            "clob_dropped": self.clob_dropped,
            "fee_dropped": self.fee_dropped,
            "roi_dropped": self.roi_dropped,
            "depth_dropped": self.depth_dropped,
            "surfaced": self.surfaced,
        }


# ---------------------------------------------------------------------------
# ScanFunnelTracker
# ---------------------------------------------------------------------------

class ScanFunnelTracker:
    """Thread-safe funnel tracker managing per-cycle and cumulative stats."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.current_cycle = FunnelStats()
        self.cumulative = FunnelStats()
        self.cycle_history: list[dict[str, int]] = []

    def start_cycle(self) -> None:
        """Start a new scan cycle, clearing current cycle stats."""
        with self._lock:
            self.current_cycle = FunnelStats()

    def record_screened(self, count: int) -> None:
        """Record number of markets evaluated."""
        if count <= 0:
            return
        with self._lock:
            self.current_cycle.screened += count
            self.cumulative.screened += count

    def record_mid_candidates(self, count: int) -> None:
        """Record mid-price candidate count."""
        if count <= 0:
            return
        with self._lock:
            self.current_cycle.mid_candidates += count
            self.cumulative.mid_candidates += count

    def record_clob_evaluated(self, count: int) -> None:
        """Record count of candidates evaluated against CLOB orderbooks."""
        if count <= 0:
            return
        with self._lock:
            self.current_cycle.clob_evaluated += count
            self.cumulative.clob_evaluated += count

    def record_clob_dropped(self, count: int = 1) -> None:
        """Record candidates dropped at CLOB orderbook evaluation."""
        if count <= 0:
            return
        with self._lock:
            self.current_cycle.clob_dropped += count
            self.cumulative.clob_dropped += count

    def record_fee_dropped(self, count: int = 1) -> None:
        """Record candidates dropped specifically because fees exceeded spread."""
        if count <= 0:
            return
        with self._lock:
            self.current_cycle.fee_dropped += count
            self.cumulative.fee_dropped += count

    def record_roi_dropped(self, count: int = 1) -> None:
        """Record candidates dropped because ROI fell below MIN_NET_ROI."""
        if count <= 0:
            return
        with self._lock:
            self.current_cycle.roi_dropped += count
            self.cumulative.roi_dropped += count

    def record_depth_dropped(self, count: int = 1) -> None:
        """Record opportunities dropped because orderbook depth was below min_depth."""
        if count <= 0:
            return
        with self._lock:
            self.current_cycle.depth_dropped += count
            self.cumulative.depth_dropped += count

    def record_surfaced(self, count: int) -> None:
        """Record final surfaced opportunities eligible for execution."""
        if count < 0:
            return
        with self._lock:
            self.current_cycle.surfaced += count
            self.cumulative.surfaced += count

    def finish_cycle(self) -> dict[str, int]:
        """Finalize the current cycle and archive to history."""
        with self._lock:
            stats = self.current_cycle.to_dict()
            self.cycle_history.append(stats)
            if len(self.cycle_history) > 100:
                self.cycle_history.pop(0)
            return stats

    def summary_log(self, cycle_num: int) -> str:
        """Generate human-readable funnel summary log line."""
        with self._lock:
            c = self.current_cycle
            return (
                f"Funnel #{cycle_num}: screened={c.screened:,} -> "
                f"mid_cand={c.mid_candidates:,} -> "
                f"clob_drop={c.clob_dropped:,} -> "
                f"fee_drop={c.fee_dropped:,} -> "
                f"roi_drop={c.roi_dropped:,} -> "
                f"depth_drop={c.depth_dropped:,} -> "
                f"surfaced={c.surfaced:,}"
            )


# ---------------------------------------------------------------------------
# Singleton instance and accessors
# ---------------------------------------------------------------------------

_global_funnel_tracker = ScanFunnelTracker()


def get_funnel_tracker() -> ScanFunnelTracker:
    """Get the global funnel tracker singleton."""
    return _global_funnel_tracker


def reset_funnel_tracker() -> None:
    """Reset the global funnel tracker (primarily for testing)."""
    global _global_funnel_tracker
    _global_funnel_tracker = ScanFunnelTracker()
