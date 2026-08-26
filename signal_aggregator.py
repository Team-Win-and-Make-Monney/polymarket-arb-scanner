"""Multi-source probability signal aggregation.

Aggregates probability estimates from multiple prediction market and
forecasting sources (Metaculus, Manifold Markets) into a weighted
consensus probability.  Used by the event monitor and convergence
scanner to make more informed directional trades.
"""

import logging
import time
import threading

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Source weight defaults — higher weight = more influence on consensus
# ---------------------------------------------------------------------------

DEFAULT_SOURCE_WEIGHTS: dict[str, float] = {
    "metaculus": 1.5,     # Strong calibration, expert crowd
    "manifold": 1.0,      # Good liquidity-weighted signal
    "polymarket": 1.2,    # High liquidity, real money
    "kalshi": 1.1,        # Regulated, real money
    "betfair": 1.0,       # Deep exchange liquidity
    "smarkets": 0.8,      # Moderate liquidity
    "sxbet": 0.7,         # Lower liquidity
    "matchbook": 0.7,     # Lower liquidity
    "gemini": 0.6,        # New market, lower confidence
    "ibkr": 0.8,          # Moderate liquidity but institutional
}


class SignalAggregator:
    """Aggregates probability signals from multiple sources into a consensus.

    Thread-safe.  Signals are cached with TTL to avoid redundant API calls.
    """

    def __init__(
        self,
        source_weights: dict[str, float] | None = None,
        cache_ttl: float = 300.0,
        metaculus_client=None,
        manifold_client=None,
    ):
        """Initialize the signal aggregator.

        Args:
            source_weights: Override default source weight map.
            cache_ttl: Seconds before a cached signal expires.
            metaculus_client: Optional MetaculusClient instance.
            manifold_client: Optional ManifoldClient instance.
        """
        self.weights = source_weights or DEFAULT_SOURCE_WEIGHTS.copy()
        self.cache_ttl = cache_ttl
        self.metaculus = metaculus_client
        self.manifold = manifold_client
        self._cache: dict[str, dict] = {}
        self._lock = threading.Lock()

    # ---------------------------------------------------------------------------
    # Public API
    # ---------------------------------------------------------------------------

    def add_signal(
        self,
        market_key: str,
        source: str,
        probability: float,
        metadata: dict | None = None,
    ) -> None:
        """Record a probability signal for a market.

        Args:
            market_key: Unique identifier for the market/question.
            source: Name of the source (e.g. "metaculus", "polymarket").
            probability: Probability estimate (0-1).
            metadata: Optional extra data (volume, sample size, etc.).
        """
        if not 0 <= probability <= 1:
            logger.warning("Invalid probability %.4f from %s for %s", probability, source, market_key)
            return

        with self._lock:
            if market_key not in self._cache:
                self._cache[market_key] = {}
            self._cache[market_key][source] = {
                "probability": probability,
                "timestamp": time.time(),
                "metadata": metadata or {},
            }

    def get_consensus(self, market_key: str) -> dict | None:
        """Calculate weighted consensus probability for a market.

        Returns:
            Dict with ``probability``, ``sources``, ``confidence``,
            ``spread`` (max-min across sources), or None if no data.
        """
        with self._lock:
            signals = self._get_fresh_signals(market_key)

        return self._consensus_from_signals(signals)

    def _consensus_from_signals(self, signals: dict[str, dict]) -> dict | None:
        """Calculate a consensus from one immutable signal snapshot."""

        if not signals:
            return None

        weighted_sum = 0.0
        total_weight = 0.0
        probs = []

        for source, data in signals.items():
            prob = data["probability"]
            weight = self.weights.get(source, 1.0)
            weighted_sum += prob * weight
            total_weight += weight
            probs.append(prob)

        if total_weight <= 0:
            return None

        consensus_prob = weighted_sum / total_weight
        spread = max(probs) - min(probs) if len(probs) > 1 else 0.0
        confidence = _consensus_confidence(len(signals), spread)

        return {
            "probability": round(consensus_prob, 4),
            "sources": list(signals.keys()),
            "num_sources": len(signals),
            "confidence": round(confidence, 3),
            "spread": round(spread, 4),
            "min": min(probs),
            "max": max(probs),
        }

    def get_divergences(
        self,
        market_key: str,
        min_divergence: float = 0.10,
    ) -> list[dict]:
        """Find sources that diverge from the consensus.

        Args:
            market_key: Market identifier.
            min_divergence: Minimum absolute divergence to report.

        Returns:
            List of dicts with ``source``, ``probability``,
            ``consensus``, ``divergence``.
        """
        with self._lock:
            signals = self._get_fresh_signals(market_key)

        consensus = self._consensus_from_signals(signals)
        if consensus is None:
            return []

        divergences = []
        for source, data in signals.items():
            div = data["probability"] - consensus["probability"]
            if abs(div) >= min_divergence:
                divergences.append({
                    "source": source,
                    "probability": data["probability"],
                    "consensus": consensus["probability"],
                    "divergence": round(div, 4),
                })

        divergences.sort(key=lambda d: abs(d["divergence"]), reverse=True)
        return divergences

    def fetch_external_signals(self, market_key: str, title: str = "") -> int:
        """Fetch signals from external sources (Metaculus, Manifold).

        Args:
            market_key: Market identifier for caching.
            title: Market title for fuzzy matching on external platforms.

        Returns:
            Number of new signals added.
        """
        count = 0

        if self.metaculus:
            prob = self._fetch_metaculus(title)
            if prob is not None:
                self.add_signal(market_key, "metaculus", prob)
                count += 1

        if self.manifold:
            prob = self._fetch_manifold(title)
            if prob is not None:
                self.add_signal(market_key, "manifold", prob)
                count += 1

        return count

    def cleanup(self, max_age: float | None = None) -> int:
        """Remove stale signals older than max_age seconds.

        Returns number of entries removed.
        """
        cutoff = time.time() - (max_age or self.cache_ttl * 2)
        removed = 0
        with self._lock:
            keys_to_remove = []
            for market_key, sources in self._cache.items():
                stale = [s for s, d in sources.items() if d["timestamp"] < cutoff]
                for s in stale:
                    del sources[s]
                    removed += 1
                if not sources:
                    keys_to_remove.append(market_key)
            for k in keys_to_remove:
                del self._cache[k]
        return removed

    # ---------------------------------------------------------------------------
    # Internal
    # ---------------------------------------------------------------------------

    def _get_fresh_signals(self, market_key: str) -> dict[str, dict]:
        """Return cached signals that are still within TTL (caller holds lock)."""
        signals = self._cache.get(market_key, {})
        cutoff = time.time() - self.cache_ttl
        return {
            source: data
            for source, data in signals.items()
            if data["timestamp"] >= cutoff
        }

    def _fetch_metaculus(self, title: str) -> float | None:
        """Search Metaculus for a matching question and return its probability."""
        try:
            results = self.metaculus.search_questions(title, limit=3)
            if results:
                # Return the first active, binary question's community prediction
                for q in results:
                    prob = q.get("community_prediction", {}).get("full", {}).get("q2")
                    if prob is not None:
                        return float(prob)
        except Exception as exc:
            logger.debug("Metaculus fetch failed for %r: %s", title[:40], exc)
        return None

    def _fetch_manifold(self, title: str) -> float | None:
        """Search Manifold for a matching market and return its probability."""
        try:
            results = self.manifold.search_markets(title, limit=3)
            if results:
                for market in results:
                    prob = market.get("probability")
                    if prob is not None and not market.get("isResolved", False):
                        return float(prob)
        except Exception as exc:
            logger.debug("Manifold fetch failed for %r: %s", title[:40], exc)
        return None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _consensus_confidence(num_sources: int, spread: float) -> float:
    """Estimate confidence in the consensus estimate.

    Higher with more sources and lower spread (agreement).
    """
    # Source count factor: 1 source = 0.3, 5+ sources = 0.9+
    source_factor = min(0.95, 0.2 + num_sources * 0.15)

    # Agreement factor: low spread = high confidence
    if spread < 0.03:
        agreement_factor = 1.0
    elif spread < 0.08:
        agreement_factor = 0.85
    elif spread < 0.15:
        agreement_factor = 0.65
    else:
        agreement_factor = 0.4

    return source_factor * agreement_factor
