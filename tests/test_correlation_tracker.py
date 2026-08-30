"""Tests for correlation_tracker.py — auto-detect correlated market pairs.

Coverage:
- Pearson r stdlib implementation (positive, negative, near-zero, edge cases)
- Bucketing semantics (multiple snapshots per bucket → averaged)
- compute_correlated_pairs threshold + min_samples gates
- Pair canonicalisation (sorted (a, b))
- run_correlation_tracker end-to-end against an in-memory
  SnapshotRecorder

Module-reference pattern (``import correlation_tracker as ct``)
follows the convention from PR B/C/D — see
tests/test_time_decay_refiner.py for the rationale.
"""

import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import correlation_tracker as ct  # noqa: E402
from snapshot import SnapshotRecorder  # noqa: E402


# ---------------------------------------------------------------------------
# Pearson sanity
# ---------------------------------------------------------------------------


class TestPearsonR:
    def test_perfect_positive_correlation(self):
        xs = [1.0, 2.0, 3.0, 4.0]
        ys = [2.0, 4.0, 6.0, 8.0]
        assert abs(ct.pearson_r(xs, ys) - 1.0) < 1e-9  # type: ignore[operator]

    def test_perfect_negative_correlation(self):
        xs = [1.0, 2.0, 3.0, 4.0]
        ys = [4.0, 3.0, 2.0, 1.0]
        assert abs(ct.pearson_r(xs, ys) - (-1.0)) < 1e-9  # type: ignore[operator]

    def test_no_correlation(self):
        xs = [1.0, 2.0, 3.0, 4.0]
        ys = [2.5, 2.5, 2.5, 2.5]  # constant — undefined correlation
        assert ct.pearson_r(xs, ys) is None

    def test_too_few_samples(self):
        assert ct.pearson_r([1.0], [2.0]) is None
        assert ct.pearson_r([], []) is None

    def test_mismatched_lengths(self):
        assert ct.pearson_r([1.0, 2.0], [3.0]) is None


# ---------------------------------------------------------------------------
# Bucketing
# ---------------------------------------------------------------------------


def _row(market: str, ts: datetime, price: float):
    return {
        "market": market,
        "timestamp": ts.isoformat(),
        "price_a": price,
    }


class TestBucketing:
    def test_averages_multiple_snapshots_in_same_bucket(self):
        base = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
        snapshots = [
            _row("A", base, 0.50),
            _row("A", base + timedelta(minutes=10), 0.52),
            _row("A", base + timedelta(minutes=30), 0.48),
        ]
        series = ct.build_bucketed_series(snapshots, bucket_seconds=3600)
        assert "A" in series
        # All three rows are in the same hour bucket → average is 0.50.
        assert len(series["A"]) == 1
        bucket_value = next(iter(series["A"].values()))
        assert abs(bucket_value - 0.50) < 1e-6

    def test_separates_buckets(self):
        base = datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
        snapshots = [
            _row("A", base, 0.50),
            _row("A", base + timedelta(hours=2), 0.60),
        ]
        series = ct.build_bucketed_series(snapshots, bucket_seconds=3600)
        assert len(series["A"]) == 2

    def test_drops_invalid_rows(self):
        base = datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
        snapshots = [
            _row("A", base, 0.50),
            {"market": "A", "timestamp": "garbage", "price_a": 0.4},
            {"market": "A", "timestamp": base.isoformat(), "price_a": "nope"},
            {"market": None, "timestamp": base.isoformat(), "price_a": 0.5},
        ]
        series = ct.build_bucketed_series(snapshots, bucket_seconds=3600)
        assert "A" in series
        # Only the first row survived.
        bucket_value = next(iter(series["A"].values()))
        assert abs(bucket_value - 0.50) < 1e-6

    def test_drops_zero_or_one_prices(self):
        # 0 and 1 are placeholder values for non-binary opp shapes; skip them.
        base = datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
        snapshots = [
            _row("A", base, 0.0),
            _row("A", base + timedelta(hours=1), 1.0),
        ]
        series = ct.build_bucketed_series(snapshots, bucket_seconds=3600)
        assert "A" not in series

    def test_drops_non_finite_prices(self):
        base = datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
        snapshots = [
            _row("A", base, float("nan")),
            _row("A", base + timedelta(hours=1), float("inf")),
            _row("A", base + timedelta(hours=2), float("-inf")),
        ]
        series = ct.build_bucketed_series(snapshots, bucket_seconds=3600)
        assert "A" not in series

    def test_aligned_series_takes_overlap(self):
        a = {1: 0.1, 2: 0.2, 3: 0.3}
        b = {2: 0.5, 3: 0.6, 4: 0.7}
        xs, ys = ct.aligned_series(a, b)
        assert xs == [0.2, 0.3]
        assert ys == [0.5, 0.6]


# ---------------------------------------------------------------------------
# compute_correlated_pairs
# ---------------------------------------------------------------------------


def _series_to_snapshots(market: str, prices: list[float], base: datetime):
    return [
        _row(market, base + timedelta(hours=i), p)
        for i, p in enumerate(prices)
    ]


class TestComputeCorrelatedPairs:
    def _base(self):
        return datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc)

    def test_finds_perfectly_correlated_pair(self):
        base = self._base()
        # 24 hourly buckets, A == B (perfect r=1)
        prices = [0.40 + 0.01 * i for i in range(24)]
        snapshots = (
            _series_to_snapshots("A", prices, base)
            + _series_to_snapshots("B", prices, base)
        )
        pairs = ct.compute_correlated_pairs(
            snapshots,
            bucket_seconds=3600,
            min_samples=24,
            pearson_threshold=0.85,
        )
        assert len(pairs) == 1
        assert pairs[0]["market_a"] == "A"
        assert pairs[0]["market_b"] == "B"
        assert pairs[0]["pearson_r"] == 1.0
        assert pairs[0]["sample_count"] == 24

    def test_keeps_anti_correlated_pair(self):
        base = self._base()
        prices_a = [0.40 + 0.01 * i for i in range(24)]
        prices_b = list(reversed(prices_a))
        snapshots = (
            _series_to_snapshots("A", prices_a, base)
            + _series_to_snapshots("B", prices_b, base)
        )
        pairs = ct.compute_correlated_pairs(
            snapshots,
            bucket_seconds=3600,
            min_samples=24,
            pearson_threshold=0.85,
        )
        assert len(pairs) == 1
        assert pairs[0]["pearson_r"] == -1.0

    def test_drops_uncorrelated_pair(self):
        base = self._base()
        # Sawtooth vs flat-ish — Pearson approximately 0
        prices_a = [0.4, 0.6] * 12
        prices_b = [0.5 + 1e-9 * i for i in range(24)]
        snapshots = (
            _series_to_snapshots("A", prices_a, base)
            + _series_to_snapshots("B", prices_b, base)
        )
        pairs = ct.compute_correlated_pairs(
            snapshots,
            bucket_seconds=3600,
            min_samples=24,
            pearson_threshold=0.85,
        )
        assert pairs == []

    def test_drops_below_min_samples(self):
        base = self._base()
        prices = [0.40 + 0.01 * i for i in range(10)]
        snapshots = (
            _series_to_snapshots("A", prices, base)
            + _series_to_snapshots("B", prices, base)
        )
        pairs = ct.compute_correlated_pairs(
            snapshots,
            bucket_seconds=3600,
            min_samples=24,
            pearson_threshold=0.85,
        )
        assert pairs == []

    def test_canonicalises_pair_order(self):
        base = self._base()
        prices = [0.40 + 0.01 * i for i in range(24)]
        snapshots = (
            _series_to_snapshots("ZZZ", prices, base)
            + _series_to_snapshots("AAA", prices, base)
        )
        pairs = ct.compute_correlated_pairs(
            snapshots,
            bucket_seconds=3600,
            min_samples=24,
            pearson_threshold=0.85,
        )
        assert pairs[0]["market_a"] == "AAA"
        assert pairs[0]["market_b"] == "ZZZ"

    def test_threshold_validation(self):
        import pytest
        with pytest.raises(ValueError):
            ct.compute_correlated_pairs(
                [], bucket_seconds=3600, min_samples=24, pearson_threshold=1.5,
            )


# ---------------------------------------------------------------------------
# run_correlation_tracker against in-memory SnapshotRecorder
# ---------------------------------------------------------------------------


class TestRunCorrelationTracker:
    def _build_recorder_with_data(self):
        rec = SnapshotRecorder(db_path=":memory:")
        # Hand-write directly into the table so we can control timestamps
        # without going through opp-shape conversion.
        base = datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
        rows = []
        for i in range(24):
            ts = (base + timedelta(hours=i)).isoformat()
            for market, price in [("A", 0.40 + 0.01 * i), ("B", 0.40 + 0.01 * i)]:
                rows.append((
                    ts, market, "polymarket", "polymarket",
                    price, None, 0.0, 0.0, 0.0, "Binary", "", None, 1,
                ))
        rec.conn.executemany(
            """INSERT INTO price_snapshots
               (timestamp, market, platform_a, platform_b,
                price_a, price_b, gross_spread, fees, net_profit,
                opp_type, direction, confidence, strategy_layer)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            rows,
        )
        rec.conn.commit()
        return rec

    def test_writes_pairs_to_correlated_pairs_table(self):
        rec = self._build_recorder_with_data()
        try:
            count = ct.run_correlation_tracker(
                rec,
                lookback_days=2,
                bucket_seconds=3600,
                min_samples=24,
                pearson_threshold=0.85,
                # Anchor "now" so the lookback covers the synthetic data.
                now=datetime(2026, 1, 2, 0, 0, 0, tzinfo=timezone.utc),
            )
            assert count == 1
            cached = rec.get_correlated_pairs(min_abs_r=0.85)
            assert len(cached) == 1
            assert cached[0]["market_a"] == "A"
            assert cached[0]["market_b"] == "B"
            assert cached[0]["pearson_r"] == 1.0
        finally:
            rec.close()

    def test_load_auto_correlated_pairs_returns_tuples(self):
        rec = self._build_recorder_with_data()
        try:
            ct.run_correlation_tracker(
                rec,
                lookback_days=2,
                bucket_seconds=3600,
                min_samples=24,
                pearson_threshold=0.85,
                now=datetime(2026, 1, 2, 0, 0, 0, tzinfo=timezone.utc),
            )
            tuples = ct.load_auto_correlated_pairs(rec, min_abs_r=0.85)
            assert tuples == [("A", "B")]
        finally:
            rec.close()

    def test_replaces_previous_cache_on_rerun(self):
        rec = self._build_recorder_with_data()
        try:
            ct.run_correlation_tracker(
                rec,
                lookback_days=2,
                bucket_seconds=3600,
                min_samples=24,
                pearson_threshold=0.85,
                now=datetime(2026, 1, 2, 0, 0, 0, tzinfo=timezone.utc),
            )
            # Clear the snapshots and re-run; cache should empty.
            rec.conn.execute("DELETE FROM price_snapshots")
            rec.conn.commit()
            count = ct.run_correlation_tracker(
                rec,
                lookback_days=2,
                bucket_seconds=3600,
                min_samples=24,
                pearson_threshold=0.85,
                now=datetime(2026, 1, 2, 0, 0, 0, tzinfo=timezone.utc),
            )
            assert count == 0
            assert rec.get_correlated_pairs(min_abs_r=0.0) == []
        finally:
            rec.close()
