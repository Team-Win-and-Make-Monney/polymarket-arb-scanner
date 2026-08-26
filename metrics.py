"""Lightweight metrics collection for monitoring scan performance, execution, and risk.

Dependency-free implementation using only stdlib. Provides counter, gauge,
and histogram metric types with thread-safe access and Prometheus text
exposition format output.
"""

import logging
import math
import threading
import time
from collections import defaultdict

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Metric types
# ---------------------------------------------------------------------------

class _Counter:
    """Monotonically increasing counter."""

    def __init__(self):
        self._values: dict[tuple, float] = defaultdict(float)
        self._lock = threading.Lock()

    def inc(self, labels: tuple = (), value: float = 1):
        with self._lock:
            self._values[labels] += value

    def get(self) -> dict[tuple, float]:
        with self._lock:
            return dict(self._values)


class _Gauge:
    """Point-in-time value that can go up or down."""

    def __init__(self):
        self._values: dict[tuple, float] = {}
        self._lock = threading.Lock()

    def set(self, labels: tuple = (), value: float = 0):
        with self._lock:
            self._values[labels] = value

    def get(self) -> dict[tuple, float]:
        with self._lock:
            return dict(self._values)


class _Histogram:
    """Distribution tracker with configurable buckets.

    Default buckets are tuned for latency measurements in seconds.
    """

    DEFAULT_BUCKETS = (0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0,
                       2.5, 5.0, 10.0, float("inf"))

    def __init__(self, buckets=None):
        self._buckets = buckets or self.DEFAULT_BUCKETS
        # Per-label: {bucket_upper -> count}, _count, _sum
        self._data: dict[tuple, dict] = {}
        self._lock = threading.Lock()

    def observe(self, labels: tuple = (), value: float = 0):
        with self._lock:
            if labels not in self._data:
                self._data[labels] = {
                    "buckets": {b: 0 for b in self._buckets},
                    "count": 0,
                    "sum": 0.0,
                }
            entry = self._data[labels]
            entry["count"] += 1
            entry["sum"] += value
            for b in self._buckets:
                if value <= b:
                    entry["buckets"][b] += 1

    def get(self) -> dict[tuple, dict]:
        with self._lock:
            return {k: dict(v) for k, v in self._data.items()}


# ---------------------------------------------------------------------------
# Label helpers
# ---------------------------------------------------------------------------

def _labels_to_tuple(labels: dict | None) -> tuple:
    """Convert a labels dict to a sorted tuple of (key, value) pairs."""
    if not labels:
        return ()
    return tuple(sorted(labels.items()))


def _labels_to_prom(labels_tuple: tuple) -> str:
    """Convert a labels tuple to Prometheus label string."""
    if not labels_tuple:
        return ""
    parts = [f'{k}="{v}"' for k, v in labels_tuple]
    return "{" + ",".join(parts) + "}"


# ---------------------------------------------------------------------------
# MetricsCollector — singleton interface
# ---------------------------------------------------------------------------

class MetricsCollector:
    """Thread-safe metrics collection with counter, gauge, and histogram support.

    Usage::

        from metrics import metrics
        metrics.inc("scans_total")
        metrics.set("active_positions", value=5)
        metrics.observe("scan_duration_seconds", value=1.23)
    """

    def __init__(self):
        self._counters: dict[str, _Counter] = {}
        self._gauges: dict[str, _Gauge] = {}
        self._histograms: dict[str, _Histogram] = {}
        self._lock = threading.Lock()
        self._start_time = time.time()

        # Pre-register known metrics so get_all / prometheus output is consistent
        self._counter_names = {
            "scans_total",
            "opportunities_found",
            "trades_executed",
            "trades_failed",
            "revalidation_failures",
            "risk_rejections",
            "ws_messages_received",
            "ws_reconnections",
            # Phase 2 (WS-driven Cross): counts how often the new event-driven
            # path is being exercised. eval_attempts ticks every time a WS
            # price update touches an indexed pair; eval_hits ticks when
            # evaluate() returned a positive opp (ready to trade); triggers
            # ticks when the opp made it onto the priority queue.
            "cross_pair_eval_attempts",
            "cross_pair_eval_hits",
            "cross_pair_triggers",
        }
        self._gauge_names = {
            "active_positions",
            "daily_pnl",
            "scan_cycle_duration_seconds",
            "best_opportunity_roi",
            "ws_connected",
            # Number of indexed Cross pairs available for WS-driven evaluation.
            # Updated after every CrossPairIndex.rebuild call.
            "cross_pair_index_size",
        }
        self._histogram_names = {
            "scan_duration_seconds",
            "execution_latency_seconds",
            "opportunity_profit",
        }

        for name in self._counter_names:
            self._counters[name] = _Counter()
        for name in self._gauge_names:
            self._gauges[name] = _Gauge()
        for name in self._histogram_names:
            self._histograms[name] = _Histogram()

    # -----------------------------------------------------------------------
    # Public API
    # -----------------------------------------------------------------------

    def inc(self, name: str, labels: dict | None = None, value: float = 1):
        """Increment a counter metric."""
        counter = self._get_or_create_counter(name)
        counter.inc(_labels_to_tuple(labels), value)

    def set(self, name: str, labels: dict | None = None, value: float = 0):
        """Set a gauge metric to a specific value."""
        gauge = self._get_or_create_gauge(name)
        gauge.set(_labels_to_tuple(labels), value)

    def observe(self, name: str, labels: dict | None = None, value: float = 0):
        """Record a histogram observation."""
        histogram = self._get_or_create_histogram(name)
        histogram.observe(_labels_to_tuple(labels), value)

    def get_all(self) -> dict:
        """Return all metrics as a nested dict suitable for JSON serialization."""
        result = {
            "counters": {},
            "gauges": {},
            "histograms": {},
            "uptime_seconds": round(time.time() - self._start_time, 1),
        }

        for name, counter in self._counters.items():
            values = counter.get()
            if not values:
                result["counters"][name] = 0
            elif len(values) == 1 and () in values:
                result["counters"][name] = values[()]
            else:
                result["counters"][name] = {
                    str(dict(k)) if k else "total": v
                    for k, v in values.items()
                }

        for name, gauge in self._gauges.items():
            values = gauge.get()
            if not values:
                result["gauges"][name] = 0
            elif len(values) == 1 and () in values:
                result["gauges"][name] = values[()]
            else:
                result["gauges"][name] = {
                    str(dict(k)) if k else "value": v
                    for k, v in values.items()
                }

        for name, histogram in self._histograms.items():
            values = histogram.get()
            if not values:
                result["histograms"][name] = {"count": 0, "sum": 0}
            elif len(values) == 1 and () in values:
                entry = values[()]
                result["histograms"][name] = {
                    "count": entry["count"],
                    "sum": round(entry["sum"], 6),
                }
            else:
                result["histograms"][name] = {}
                for k, entry in values.items():
                    label_str = str(dict(k)) if k else "total"
                    result["histograms"][name][label_str] = {
                        "count": entry["count"],
                        "sum": round(entry["sum"], 6),
                    }

        return result

    def get_prometheus_text(self) -> str:
        """Return metrics in Prometheus text exposition format.

        See: https://prometheus.io/docs/instrumenting/exposition_formats/
        """
        lines = []

        # Counters
        for name, counter in self._counters.items():
            prom_name = f"arb_{name}"
            lines.append(f"# HELP {prom_name} Counter metric")
            lines.append(f"# TYPE {prom_name} counter")
            values = counter.get()
            if not values:
                lines.append(f"{prom_name} 0")
            else:
                for labels_tuple, val in values.items():
                    lines.append(f"{prom_name}{_labels_to_prom(labels_tuple)} {val}")

        # Gauges
        for name, gauge in self._gauges.items():
            prom_name = f"arb_{name}"
            lines.append(f"# HELP {prom_name} Gauge metric")
            lines.append(f"# TYPE {prom_name} gauge")
            values = gauge.get()
            if not values:
                lines.append(f"{prom_name} 0")
            else:
                for labels_tuple, val in values.items():
                    lines.append(f"{prom_name}{_labels_to_prom(labels_tuple)} {val}")

        # Histograms
        for name, histogram in self._histograms.items():
            prom_name = f"arb_{name}"
            lines.append(f"# HELP {prom_name} Histogram metric")
            lines.append(f"# TYPE {prom_name} histogram")
            data = histogram.get()
            if not data:
                lines.append(f"{prom_name}_count 0")
                lines.append(f"{prom_name}_sum 0")
            else:
                for labels_tuple, entry in data.items():
                    label_str = _labels_to_prom(labels_tuple)
                    for bucket_bound in sorted(entry["buckets"].keys()):
                        cumulative = entry["buckets"][bucket_bound]
                        if bucket_bound == float("inf"):
                            le_str = "+Inf"
                        else:
                            le_str = str(bucket_bound)
                        if labels_tuple:
                            # Merge le into existing labels
                            combined = dict(labels_tuple)
                            combined["le"] = le_str
                            combined_str = _labels_to_prom(tuple(sorted(combined.items())))
                        else:
                            combined_str = f'{{le="{le_str}"}}'
                        lines.append(f"{prom_name}_bucket{combined_str} {cumulative}")
                    lines.append(f"{prom_name}_count{label_str} {entry['count']}")
                    lines.append(f"{prom_name}_sum{label_str} {round(entry['sum'], 6)}")

        # Uptime gauge
        lines.append("# HELP arb_uptime_seconds Time since metrics collector started")
        lines.append("# TYPE arb_uptime_seconds gauge")
        lines.append(f"arb_uptime_seconds {round(time.time() - self._start_time, 1)}")

        lines.append("")  # trailing newline
        return "\n".join(lines)

    def reset_daily(self):
        """Reset daily accumulators (counters that should reset at midnight)."""
        daily_counters = {"trades_executed", "trades_failed", "revalidation_failures",
                          "risk_rejections", "opportunities_found", "scans_total"}
        for name in daily_counters:
            if name in self._counters:
                with self._counters[name]._lock:
                    self._counters[name]._values.clear()

    # -----------------------------------------------------------------------
    # Internal helpers
    # -----------------------------------------------------------------------

    def _get_or_create_counter(self, name: str) -> _Counter:
        if name not in self._counters:
            with self._lock:
                if name not in self._counters:
                    self._counters[name] = _Counter()
        return self._counters[name]

    def _get_or_create_gauge(self, name: str) -> _Gauge:
        if name not in self._gauges:
            with self._lock:
                if name not in self._gauges:
                    self._gauges[name] = _Gauge()
        return self._gauges[name]

    def _get_or_create_histogram(self, name: str) -> _Histogram:
        if name not in self._histograms:
            with self._lock:
                if name not in self._histograms:
                    self._histograms[name] = _Histogram()
        return self._histograms[name]


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------

metrics = MetricsCollector()
