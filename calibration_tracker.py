"""Calibration Tracker — Strategy #41.

Track historical platform accuracy and adjust signal weights accordingly.

Different platforms have different calibration characteristics:
- Polymarket: Generally well-calibrated on political/crypto markets
- Kalshi: Better on finance/macro markets
- Metaculus: Excellent calibration, esp. on science/tech
- Manifold: Varies by market creator

This module tracks resolution outcomes over time and adjusts
signal aggregation weights based on historical accuracy.

Layer 4: Capital Optimization — improved signal weighting.
"""

import logging
import math
import sqlite3
import threading
import time
from pathlib import Path

from config import (
    CALIBRATION_WEIGHTING_ENABLED,
    DATA_DIR,
)

logger = logging.getLogger(__name__)

CALIBRATION_DB_PATH = Path(DATA_DIR) / "calibration.db"


class CalibrationTracker:
    """Track platform calibration and adjust weights.

    Thread-safe. Persists calibration data to SQLite.
    """

    def __init__(self, db_path: str | Path | None = None):
        """Initialize the tracker.

        Args:
            db_path: Path to calibration database file.
        """
        self.db_path = str(db_path or CALIBRATION_DB_PATH)
        self._lock = threading.Lock()
        self._init_db()

        self._in_memory_cache: dict[str, dict] = {}
        self._cache_generation = 0

    def _init_db(self) -> None:
        """Initialize the SQLite database."""
        with self._lock:
            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS calibration_records (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    platform TEXT NOT NULL,
                    category TEXT DEFAULT 'general',
                    market_key TEXT NOT NULL,
                    prediction REAL NOT NULL,
                    outcome INTEGER NOT NULL,
                    timestamp REAL NOT NULL,
                    UNIQUE(platform, market_key)
                )
            """)
            cursor.execute("""
                CREATE INDEX IF NOT EXISTS idx_platform_category
                ON calibration_records(platform, category)
            """)
            conn.commit()
            conn.close()

    def record_resolution(
        self,
        platform: str,
        market_key: str,
        prediction: float,
        outcome: int,
        category: str = "general",
    ) -> None:
        """Record a market resolution for calibration tracking.

        Args:
            platform: Platform name (polymarket, kalshi, metaculus, etc.).
            market_key: Unique market identifier.
            prediction: Platform's probability before resolution (0-1).
            outcome: Actual outcome (1=yes, 0=no).
            category: Market category for category-specific calibration.
        """
        with self._lock:
            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()
            cursor.execute("""
                INSERT OR REPLACE INTO calibration_records
                (platform, category, market_key, prediction, outcome, timestamp)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (platform, category, market_key, prediction, outcome, time.time()))
            conn.commit()
            conn.close()
            keys_to_delete = [
                k for k in self._in_memory_cache if k.startswith(f"{platform}:")
            ]
            for key in keys_to_delete:
                del self._in_memory_cache[key]
            self._cache_generation += 1

    def get_platform_brier_score(
        self,
        platform: str,
        category: str | None = None,
        lookback_days: int = 365,
    ) -> float | None:
        """Calculate Brier score for a platform.

        Brier score = mean((prediction - outcome)^2)
        Lower is better. Perfect = 0, random = 0.25.

        Args:
            platform: Platform name.
            category: Optional category filter.
            lookback_days: Days of history to consider.

        Returns:
            Brier score (0-1), or None if insufficient data.
        """
        cutoff = time.time() - (lookback_days * 86400)

        with self._lock:
            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()

            if category:
                cursor.execute("""
                    SELECT prediction, outcome FROM calibration_records
                    WHERE platform = ? AND category = ? AND timestamp > ?
                """, (platform, category, cutoff))
            else:
                cursor.execute("""
                    SELECT prediction, outcome FROM calibration_records
                    WHERE platform = ? AND timestamp > ?
                """, (platform, cutoff))

            rows = cursor.fetchall()
            conn.close()

        if len(rows) < 10:
            return None

        brier_sum = sum((pred - outcome) ** 2 for pred, outcome in rows)
        return brier_sum / len(rows)

    def get_platform_calibration_error(
        self,
        platform: str,
        num_bins: int = 10,
        lookback_days: int = 365,
    ) -> float | None:
        """Calculate calibration error for a platform.

        Groups predictions into bins and measures deviation of
        actual outcome rate from predicted probability.

        Args:
            platform: Platform name.
            num_bins: Number of probability bins.
            lookback_days: Days of history to consider.

        Returns:
            Mean calibration error (0-1), or None if insufficient data.
        """
        cutoff = time.time() - (lookback_days * 86400)

        with self._lock:
            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()
            cursor.execute("""
                SELECT prediction, outcome FROM calibration_records
                WHERE platform = ? AND timestamp > ?
            """, (platform, cutoff))
            rows = cursor.fetchall()
            conn.close()

        if len(rows) < 50:
            return None

        bins = [[] for _ in range(num_bins)]
        for pred, outcome in rows:
            bin_idx = min(int(pred * num_bins), num_bins - 1)
            bins[bin_idx].append((pred, outcome))

        errors = []
        for i, bin_data in enumerate(bins):
            if len(bin_data) < 3:
                continue
            bin_center = (i + 0.5) / num_bins
            avg_outcome = sum(o for _, o in bin_data) / len(bin_data)
            errors.append(abs(avg_outcome - bin_center))

        if not errors:
            return None

        return sum(errors) / len(errors)

    def get_weight_multiplier(
        self,
        platform: str,
        category: str | None = None,
        base_weight: float = 1.0,
    ) -> float:
        """Get weight multiplier for a platform based on calibration.

        Better-calibrated platforms get higher weights.

        Args:
            platform: Platform name.
            category: Optional category filter.
            base_weight: Base weight before adjustment.

        Returns:
            Weight multiplier (0.5 to 2.0).
        """
        if not CALIBRATION_WEIGHTING_ENABLED:
            return base_weight

        cache_key = f"{platform}:{category or 'all'}"
        with self._lock:
            cached = self._in_memory_cache.get(cache_key)
            generation = self._cache_generation
        if cached is not None:
            if time.time() < cached.get("_expires", 0):
                return cached.get("_weight", base_weight)

        brier = self.get_platform_brier_score(platform, category)
        if brier is None:
            return base_weight

        if brier < 0.10:
            multiplier = 2.0
        elif brier < 0.15:
            multiplier = 1.5
        elif brier < 0.20:
            multiplier = 1.2
        elif brier < 0.25:
            multiplier = 1.0
        elif brier < 0.30:
            multiplier = 0.8
        else:
            multiplier = 0.5

        weight = base_weight * multiplier

        with self._lock:
            if generation == self._cache_generation:
                self._in_memory_cache[cache_key] = {
                    "_weight": weight,
                    "_brier": brier,
                    "_expires": time.time() + 3600,
                }

        return weight

    def get_all_platform_stats(self, lookback_days: int = 365) -> dict[str, dict]:
        """Get calibration statistics for all platforms.

        Returns:
            Dict mapping platform name to stats dict.
        """
        cutoff = time.time() - (lookback_days * 86400)

        with self._lock:
            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()
            cursor.execute("""
                SELECT DISTINCT platform FROM calibration_records
                WHERE timestamp > ?
            """, (cutoff,))
            platforms = [row[0] for row in cursor.fetchall()]
            conn.close()

        stats = {}
        for platform in platforms:
            brier = self.get_platform_brier_score(platform, lookback_days=lookback_days)
            cal_error = self.get_platform_calibration_error(platform, lookback_days=lookback_days)
            weight = self.get_weight_multiplier(platform)

            stats[platform] = {
                "brier_score": brier,
                "calibration_error": cal_error,
                "weight_multiplier": weight,
            }

        return stats

    def adjust_consensus_weights(
        self,
        platform_probabilities: dict[str, float],
        category: str | None = None,
    ) -> dict[str, float]:
        """Adjust platform weights in a consensus calculation.

        Args:
            platform_probabilities: Dict mapping platform to probability.
            category: Optional market category for category-specific weighting.

        Returns:
            Dict mapping platform to adjusted weight (sum to 1.0).
        """
        if not CALIBRATION_WEIGHTING_ENABLED:
            n = len(platform_probabilities)
            return {p: 1.0 / n for p in platform_probabilities}

        raw_weights = {}
        for platform in platform_probabilities:
            raw_weights[platform] = self.get_weight_multiplier(platform, category)

        total = sum(raw_weights.values())
        if total <= 0:
            n = len(platform_probabilities)
            return {p: 1.0 / n for p in platform_probabilities}

        return {p: w / total for p, w in raw_weights.items()}

    def calculate_weighted_consensus(
        self,
        platform_probabilities: dict[str, float],
        category: str | None = None,
    ) -> float:
        """Calculate calibration-weighted consensus probability.

        Args:
            platform_probabilities: Dict mapping platform to probability.
            category: Optional market category.

        Returns:
            Weighted consensus probability.
        """
        weights = self.adjust_consensus_weights(platform_probabilities, category)

        consensus = sum(
            platform_probabilities[p] * weights[p]
            for p in platform_probabilities
        )

        return consensus


_calibration_tracker: CalibrationTracker | None = None


def get_calibration_tracker() -> CalibrationTracker:
    """Get or create the module-level CalibrationTracker."""
    global _calibration_tracker
    if _calibration_tracker is None:
        _calibration_tracker = CalibrationTracker()
    return _calibration_tracker
