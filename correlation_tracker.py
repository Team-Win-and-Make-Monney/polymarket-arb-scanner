"""Auto-detect correlated market pairs from historical snapshots.

Reads the ``price_snapshots`` table populated by ``snapshot.py``,
buckets prices per market into a uniform time grid, computes the
Pearson correlation coefficient between every pair of markets, and
caches the high-|r| pairs to ``correlated_pairs`` for
``scans/correlated.py:scan_correlated`` to consume.

Design choices
- Stdlib-only Pearson (no numpy/scipy required) so the project stays
  thin and the tracker can run on Railway without extra deps.
- Anti-correlated pairs (negative r) are kept — a confident negative
  correlation is just as actionable as a positive one for the
  divergence-convergence trade (one leg LONG, the other SHORT).
- Pair canonicalisation is done at the SnapshotRecorder.upsert_correlated_pairs
  layer (sorted (a, b)) so we don't double-count.

Used by
- ``continuous.py`` runs ``run_correlation_tracker`` every
  ``CORRELATION_TRACKER_INTERVAL`` seconds (default 24h).
- ``scans/correlated.py:scan_correlated`` calls
  ``load_auto_correlated_pairs()`` to merge auto pairs with manual ones.
"""

from __future__ import annotations

import logging
import math
from datetime import datetime, timedelta, timezone

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Pearson (stdlib)
# ---------------------------------------------------------------------------


def pearson_r(xs: list[float], ys: list[float]) -> float | None:
    """Return Pearson r for two equal-length numeric sequences.

    Returns ``None`` when the sample is too small (< 2), the variance
    in either series is zero, or the inputs are mismatched. Stdlib-only
    so the tracker works without numpy.
    """
    if len(xs) != len(ys):
        return None
    n = len(xs)
    if n < 2:
        return None
    mean_x = sum(xs) / n
    mean_y = sum(ys) / n
    sxx = sum((x - mean_x) ** 2 for x in xs)
    syy = sum((y - mean_y) ** 2 for y in ys)
    if sxx == 0 or syy == 0:
        return None
    sxy = sum((xs[i] - mean_x) * (ys[i] - mean_y) for i in range(n))
    denom = math.sqrt(sxx * syy)
    if denom == 0:
        return None
    return sxy / denom


# ---------------------------------------------------------------------------
# Bucketing helpers
# ---------------------------------------------------------------------------


def _parse_iso(ts: str) -> datetime | None:
    """Parse a snapshot ``timestamp`` string. Returns None on failure."""
    if not ts:
        return None
    try:
        # snapshots are written via datetime.now(timezone.utc).isoformat()
        # which is "+00:00" on the end. ``fromisoformat`` handles that.
        s = ts.replace("Z", "+00:00") if ts.endswith("Z") else ts
        return datetime.fromisoformat(s)
    except (ValueError, TypeError):
        return None


def _bucket_key(ts: datetime, bucket_seconds: int) -> int:
    """Return an int bucket id for a timestamp at the given grid width."""
    epoch = int(ts.timestamp())
    return epoch // bucket_seconds


def build_bucketed_series(
    snapshots: list[dict],
    bucket_seconds: int,
) -> dict[str, dict[int, float]]:
    """Aggregate snapshot rows into ``{market: {bucket_id: avg_price}}``.

    Multiple snapshots in the same bucket are averaged. Rows missing a
    parseable timestamp or a numeric ``price_a`` are skipped.
    """
    raw: dict[str, dict[int, list[float]]] = {}
    for row in snapshots:
        market = row.get("market")
        price_a = row.get("price_a")
        if not market or price_a is None:
            continue
        try:
            price = float(price_a)
        except (TypeError, ValueError):
            continue
        # Pearson is undefined for constant-zero rows; skip.
        if not math.isfinite(price) or price <= 0 or price >= 1:
            # CTF token prices live strictly inside (0, 1). Drop invalid values.
            continue
        ts = _parse_iso(row.get("timestamp", ""))
        if ts is None:
            continue
        bucket = _bucket_key(ts, bucket_seconds)
        raw.setdefault(market, {}).setdefault(bucket, []).append(price)

    series: dict[str, dict[int, float]] = {}
    for market, buckets in raw.items():
        series[market] = {b: sum(v) / len(v) for b, v in buckets.items()}
    return series


def aligned_series(
    series_a: dict[int, float],
    series_b: dict[int, float],
) -> tuple[list[float], list[float]]:
    """Return overlapping price values for two market series.

    Pearson is computed only on buckets where both series have a value.
    """
    common = sorted(set(series_a.keys()) & set(series_b.keys()))
    xs = [series_a[b] for b in common]
    ys = [series_b[b] for b in common]
    return xs, ys


# ---------------------------------------------------------------------------
# Public tracker entry point
# ---------------------------------------------------------------------------


def compute_correlated_pairs(
    snapshots: list[dict],
    *,
    bucket_seconds: int,
    min_samples: int,
    pearson_threshold: float,
) -> list[dict]:
    """Pure function: snapshots in, list of correlated-pair dicts out.

    ``snapshots`` is the row dump from ``SnapshotRecorder.get_snapshots``.
    Returned dicts match the shape ``upsert_correlated_pairs`` expects:

        {"market_a": str, "market_b": str, "pearson_r": float,
         "sample_count": int}

    Pairs are canonicalised by sorted (market_a, market_b) so each
    relation appears once.
    """
    if pearson_threshold < 0 or pearson_threshold > 1:
        raise ValueError(
            f"pearson_threshold must be in [0,1], got {pearson_threshold!r}"
        )

    series = build_bucketed_series(snapshots, bucket_seconds)
    markets = sorted(series.keys())
    if len(markets) < 2:
        return []

    out: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for i in range(len(markets)):
        for j in range(i + 1, len(markets)):
            a, b = markets[i], markets[j]
            xs, ys = aligned_series(series[a], series[b])
            if len(xs) < min_samples:
                continue
            r = pearson_r(xs, ys)
            if r is None:
                continue
            if abs(r) < pearson_threshold:
                continue
            sorted_pair = sorted((a, b))
            key: tuple[str, str] = (sorted_pair[0], sorted_pair[1])
            if key in seen:
                continue
            seen.add(key)
            out.append({
                "market_a": key[0],
                "market_b": key[1],
                "pearson_r": float(r),
                "sample_count": len(xs),
            })

    out.sort(key=lambda p: abs(p["pearson_r"]), reverse=True)
    return out


def run_correlation_tracker(
    snapshot_recorder,
    *,
    lookback_days: int | None = None,
    bucket_seconds: int | None = None,
    min_samples: int | None = None,
    pearson_threshold: float | None = None,
    now: datetime | None = None,
) -> int:
    """Refresh the cached correlated_pairs table from recent snapshots.

    Pulls snapshots within the lookback window, runs
    ``compute_correlated_pairs``, and writes the result via
    ``snapshot_recorder.upsert_correlated_pairs``. Returns the number
    of pairs written.

    All knobs default to the corresponding ``config.*`` value when
    omitted.
    """
    if lookback_days is None:
        from config import CORRELATION_LOOKBACK_DAYS
        lookback_days = int(CORRELATION_LOOKBACK_DAYS)
    if bucket_seconds is None:
        from config import CORRELATION_BUCKET_SECONDS
        bucket_seconds = int(CORRELATION_BUCKET_SECONDS)
    if min_samples is None:
        from config import CORRELATION_MIN_SAMPLES
        min_samples = int(CORRELATION_MIN_SAMPLES)
    if pearson_threshold is None:
        from config import CORRELATION_PEARSON_THRESHOLD
        pearson_threshold = float(CORRELATION_PEARSON_THRESHOLD)

    if now is None:
        now = datetime.now(timezone.utc)
    start = now - timedelta(days=lookback_days)
    snapshots = snapshot_recorder.get_snapshots(
        start_time=start.isoformat(),
        end_time=now.isoformat(),
    )

    pairs = compute_correlated_pairs(
        snapshots,
        bucket_seconds=bucket_seconds,
        min_samples=min_samples,
        pearson_threshold=pearson_threshold,
    )
    written = snapshot_recorder.upsert_correlated_pairs(pairs)
    logger.info(
        "correlation_tracker: %d snapshot rows -> %d correlated pairs "
        "(|r| >= %.2f, >= %d samples each, %dh buckets)",
        len(snapshots), written, pearson_threshold, min_samples,
        bucket_seconds // 3600,
    )
    return written


# ---------------------------------------------------------------------------
# Consumer-side helper for scans/correlated.py
# ---------------------------------------------------------------------------


def load_auto_correlated_pairs(
    snapshot_recorder,
    min_abs_r: float | None = None,
) -> list[tuple[str, str]]:
    """Read cached pairs as a list of (market_a, market_b) tuples.

    Defaults to the configured ``CORRELATION_PEARSON_THRESHOLD`` so the
    consumer side is consistent with what the tracker wrote.
    """
    if min_abs_r is None:
        from config import CORRELATION_PEARSON_THRESHOLD
        min_abs_r = float(CORRELATION_PEARSON_THRESHOLD)
    rows = snapshot_recorder.get_correlated_pairs(min_abs_r=min_abs_r)
    return [(r["market_a"], r["market_b"]) for r in rows]
