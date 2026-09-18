"""Latency Monitor — Strategy #49.

Geographic latency optimization for multi-region deployment.

Tracks latency to each platform's API and routes orders through
the lowest-latency path.

Key Features:
- Per-platform latency tracking with rolling averages
- Region-aware routing for multi-datacenter deployments
- Latency spike detection and failover
- Priority execution for time-sensitive opportunities

Layer 5: Capital Optimization — reduce execution latency.
"""

import logging
import os
import socket
import threading
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor, as_completed

from config import GEOGRAPHIC_LATENCY_ENABLED

logger = logging.getLogger(__name__)

LATENCY_SAMPLE_SIZE = int(os.getenv("LATENCY_SAMPLE_SIZE", "50"))
LATENCY_WARNING_MS = float(os.getenv("LATENCY_WARNING_MS", "500"))
LATENCY_CRITICAL_MS = float(os.getenv("LATENCY_CRITICAL_MS", "2000"))

PLATFORM_ENDPOINTS = {
    "polymarket": "clob.polymarket.com",
    "kalshi": "trading-api.kalshi.com",
    "betfair": "api.betfair.com",
    "smarkets": "api.smarkets.com",
    "sxbet": "api.sx.bet",
    "matchbook": "api.matchbook.com",
    "gemini": "api.gemini.com",
    "ibkr": "localhost",
}


class LatencyTracker:
    """Track latency samples for a single endpoint."""

    def __init__(self, max_samples: int = LATENCY_SAMPLE_SIZE):
        self.max_samples = max_samples
        self._samples: deque[float] = deque(maxlen=max_samples)
        self._lock = threading.Lock()
        self._last_update = 0.0

    def record(self, latency_ms: float) -> None:
        """Record a latency sample in milliseconds."""
        with self._lock:
            self._samples.append(latency_ms)
            self._last_update = time.time()

    def get_average(self) -> float:
        """Get rolling average latency in ms."""
        with self._lock:
            if not self._samples:
                return 0.0
            return sum(self._samples) / len(self._samples)

    def get_p95(self) -> float:
        """Get 95th percentile latency in ms."""
        with self._lock:
            if len(self._samples) < 5:
                return 0.0
            sorted_samples = sorted(self._samples)
            idx = int(len(sorted_samples) * 0.95)
            return sorted_samples[min(idx, len(sorted_samples) - 1)]

    def get_min(self) -> float:
        """Get minimum latency in ms."""
        with self._lock:
            return min(self._samples) if self._samples else 0.0

    def get_max(self) -> float:
        """Get maximum latency in ms."""
        with self._lock:
            return max(self._samples) if self._samples else 0.0

    def get_sample_count(self) -> int:
        """Get number of samples collected."""
        with self._lock:
            return len(self._samples)

    def is_healthy(self) -> bool:
        """Check if latency is within acceptable range."""
        avg = self.get_average()
        if avg <= 0:
            return True
        return avg < LATENCY_CRITICAL_MS

    def is_stale(self, max_age_seconds: float = 300.0) -> bool:
        """Check if latency data is stale."""
        with self._lock:
            return time.time() - self._last_update > max_age_seconds


class LatencyMonitor:
    """Monitor latency to all platform endpoints.

    Thread-safe. Provides latency-based routing decisions for execution.
    """

    def __init__(self):
        self._trackers: dict[str, LatencyTracker] = {}
        self._lock = threading.Lock()
        self._running = False
        self._thread: threading.Thread | None = None

    def _ensure_tracker(self, platform: str) -> LatencyTracker:
        """Get or create tracker for a platform."""
        with self._lock:
            if platform not in self._trackers:
                self._trackers[platform] = LatencyTracker()
            return self._trackers[platform]

    def record_latency(
        self,
        platform: str,
        latency_ms: float,
    ) -> None:
        """Record a latency measurement.

        Args:
            platform: Platform name.
            latency_ms: Latency in milliseconds.
        """
        tracker = self._ensure_tracker(platform)
        tracker.record(latency_ms)

        if latency_ms > LATENCY_CRITICAL_MS:
            logger.warning(
                "Critical latency to %s: %.0fms (avg: %.0fms)",
                platform, latency_ms, tracker.get_average(),
            )
        elif latency_ms > LATENCY_WARNING_MS:
            logger.debug(
                "High latency to %s: %.0fms (avg: %.0fms)",
                platform, latency_ms, tracker.get_average(),
            )

    def _ping_endpoint(self, platform: str) -> float | None:
        """Measure TCP connect latency to a platform endpoint.

        Returns:
            Latency in milliseconds, or None on failure.
        """
        endpoint = PLATFORM_ENDPOINTS.get(platform)
        if not endpoint:
            return None

        port = 443 if endpoint != "localhost" else 4001
        try:
            start = time.perf_counter()
            sock = socket.create_connection((endpoint, port), timeout=5.0)
            latency_ms = (time.perf_counter() - start) * 1000
            sock.close()
            return latency_ms
        except Exception as e:
            logger.debug("Ping failed for %s: %s", platform, e)
            return None

    def ping_all(self) -> dict[str, float]:
        """Ping all platform endpoints and record latencies.

        Returns:
            Dict mapping platform to latency in ms.
        """
        results = {}

        with ThreadPoolExecutor(max_workers=8) as pool:
            futures = {
                pool.submit(self._ping_endpoint, platform): platform
                for platform in PLATFORM_ENDPOINTS
            }
            for future in as_completed(futures):
                platform = futures[future]
                try:
                    latency = future.result()
                    if latency is not None:
                        self.record_latency(platform, latency)
                        results[platform] = latency
                except Exception as e:
                    logger.debug("Ping error for %s: %s", platform, e)

        return results

    def get_latency(self, platform: str) -> float:
        """Get current average latency for a platform in ms."""
        tracker = self._ensure_tracker(platform)
        return tracker.get_average()

    def get_fastest_platform(self, platforms: list[str]) -> str | None:
        """Return the platform with lowest latency.

        Args:
            platforms: List of platform names to consider.

        Returns:
            Platform name with lowest average latency, or None.
        """
        if not GEOGRAPHIC_LATENCY_ENABLED:
            return platforms[0] if platforms else None

        if not platforms:
            return None

        latencies = {p: self.get_latency(p) for p in platforms}
        valid = {p: l for p, l in latencies.items() if l > 0}

        if not valid:
            return platforms[0]

        return min(valid.keys(), key=lambda p: valid[p])

    def get_platform_health(self) -> dict[str, dict]:
        """Get health summary for all tracked platforms.

        Returns:
            Dict mapping platform to health stats.
        """
        stats = {}
        with self._lock:
            for platform, tracker in self._trackers.items():
                stats[platform] = {
                    "avg_latency_ms": round(tracker.get_average(), 1),
                    "p95_latency_ms": round(tracker.get_p95(), 1),
                    "min_latency_ms": round(tracker.get_min(), 1),
                    "max_latency_ms": round(tracker.get_max(), 1),
                    "samples": tracker.get_sample_count(),
                    "healthy": tracker.is_healthy(),
                    "stale": tracker.is_stale(),
                }
        return stats

    def is_platform_healthy(self, platform: str) -> bool:
        """Check if a platform has healthy latency."""
        tracker = self._ensure_tracker(platform)
        return tracker.is_healthy() and not tracker.is_stale()

    def _monitoring_loop(self, interval_seconds: float = 30.0) -> None:
        """Background monitoring loop."""
        while self._running:
            try:
                self.ping_all()
            except Exception as e:
                logger.exception("Latency monitoring error: %s", e)

            for _ in range(int(interval_seconds * 10)):
                if not self._running:
                    break
                time.sleep(0.1)

    def start_monitoring(self, interval_seconds: float = 30.0) -> None:
        """Start background latency monitoring.

        Args:
            interval_seconds: Interval between ping rounds.
        """
        if self._running:
            return

        self._running = True
        self._thread = threading.Thread(
            target=self._monitoring_loop,
            args=(interval_seconds,),
            daemon=True,
            name="LatencyMonitor",
        )
        self._thread.start()
        logger.info("Latency monitoring started (interval: %.0fs)", interval_seconds)

    def stop_monitoring(self) -> None:
        """Stop background monitoring."""
        self._running = False
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=5.0)
        logger.info("Latency monitoring stopped")


_monitor: LatencyMonitor | None = None


def get_latency_monitor() -> LatencyMonitor:
    """Get or create the module-level LatencyMonitor instance."""
    global _monitor
    if _monitor is None:
        _monitor = LatencyMonitor()
    return _monitor


# ---------------------------------------------------------------------------
# Region-Aware Routing
# ---------------------------------------------------------------------------

class RegionRouter:
    """Route execution through optimal region based on latency.

    For multi-region deployments where the scanner can be deployed
    in multiple datacenters (e.g., US-East, US-West, EU).
    """

    def __init__(self, local_region: str = ""):
        self.local_region = local_region or os.getenv("DEPLOYMENT_REGION", "us-east")
        self._region_latencies: dict[str, dict[str, float]] = {}
        # Routing decisions call get_best_region while holding this lock.
        self._lock = threading.RLock()

    def record_region_latency(
        self,
        platform: str,
        region: str,
        latency_ms: float,
    ) -> None:
        """Record latency from a specific region to a platform.

        Args:
            platform: Platform name.
            region: Region identifier (e.g., "us-east").
            latency_ms: Latency in milliseconds.
        """
        with self._lock:
            if platform not in self._region_latencies:
                self._region_latencies[platform] = {}
            self._region_latencies[platform][region] = latency_ms

    def get_best_region(self, platform: str) -> str:
        """Get the region with lowest latency to a platform.

        Args:
            platform: Platform name.

        Returns:
            Best region name, or local_region if unknown.
        """
        if not GEOGRAPHIC_LATENCY_ENABLED:
            return self.local_region

        with self._lock:
            region_data = self._region_latencies.get(platform, {})
            if not region_data:
                return self.local_region

            return min(region_data.keys(), key=lambda r: region_data[r])

    def should_route_externally(self, platform: str) -> bool:
        """Check if execution should be routed to a different region.

        Returns True if another region has significantly lower latency
        to the target platform.

        Args:
            platform: Platform name.

        Returns:
            True if external routing is recommended.
        """
        if not GEOGRAPHIC_LATENCY_ENABLED:
            return False

        with self._lock:
            region_data = self._region_latencies.get(platform, {})
            if not region_data or self.local_region not in region_data:
                return False

            local_latency = region_data[self.local_region]
            best_region = self.get_best_region(platform)
            best_latency = region_data.get(best_region, local_latency)

            return best_latency < local_latency * 0.7


_router: RegionRouter | None = None


def get_region_router() -> RegionRouter:
    """Get or create the module-level RegionRouter instance."""
    global _router
    if _router is None:
        _router = RegionRouter()
    return _router
