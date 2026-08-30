"""Cross-Category Correlation Signals — Strategy #43.

Trade when correlated external signals diverge from market prices.

Examples:
- BTC price vs "Bitcoin > $100k by year end" market
- S&P 500 vs "Recession in 2024" market
- Oil prices vs "Gas prices > $5" market
- Interest rates vs "Fed rate cut" markets

Strategy:
1. Track external price feeds (crypto, stocks, commodities)
2. Map to related prediction markets
3. When external signal implies different probability, bet on convergence

Layer 4: Informed trading — exploit slow price discovery.
"""

import logging
import time
from dataclasses import dataclass

from config import (
    CROSS_CATEGORY_ENABLED,
    CROSS_CATEGORY_MIN_DIVERGENCE,
)
from .helpers import capital_efficiency_score, filter_dust

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Correlation definitions
# ---------------------------------------------------------------------------

@dataclass
class CorrelationRule:
    """Defines relationship between external signal and market."""
    name: str
    keywords: list[str]
    signal_source: str
    threshold_value: float
    direction: str
    confidence_multiplier: float = 1.0


CORRELATION_RULES = [
    CorrelationRule(
        name="btc_100k",
        keywords=["bitcoin", "btc", "100k", "100,000"],
        signal_source="btc_price",
        threshold_value=100000,
        direction="above",
    ),
    CorrelationRule(
        name="btc_150k",
        keywords=["bitcoin", "btc", "150k", "150,000"],
        signal_source="btc_price",
        threshold_value=150000,
        direction="above",
    ),
    CorrelationRule(
        name="eth_10k",
        keywords=["ethereum", "eth", "10k", "10,000"],
        signal_source="eth_price",
        threshold_value=10000,
        direction="above",
    ),
    CorrelationRule(
        name="eth_5k",
        keywords=["ethereum", "eth", "5k", "5,000"],
        signal_source="eth_price",
        threshold_value=5000,
        direction="above",
    ),
]


# ---------------------------------------------------------------------------
# External signal fetcher
# ---------------------------------------------------------------------------

class ExternalSignalFetcher:
    """Fetch external price signals for correlation analysis."""

    def __init__(self, cache_ttl: float = 60.0):
        """Initialize the fetcher.

        Args:
            cache_ttl: Cache TTL in seconds.
        """
        self.cache_ttl = cache_ttl
        self._cache: dict[str, dict] = {}

    def _get_cached(self, key: str) -> float | None:
        """Get cached value if not expired."""
        if key not in self._cache:
            return None
        entry = self._cache[key]
        if time.time() > entry.get("expires", 0):
            del self._cache[key]
            return None
        return entry.get("value")

    def _set_cached(self, key: str, value: float) -> None:
        """Cache a value."""
        self._cache[key] = {
            "value": value,
            "expires": time.time() + self.cache_ttl,
        }

    def get_btc_price(self) -> float | None:
        """Get current BTC price in USD."""
        cached = self._get_cached("btc_price")
        if cached is not None:
            return cached

        try:
            import requests
            resp = requests.get(
                "https://api.coingecko.com/api/v3/simple/price",
                params={"ids": "bitcoin", "vs_currencies": "usd"},
                timeout=5,
            )
            resp.raise_for_status()
            price = resp.json()["bitcoin"]["usd"]
            if not isinstance(price, (int, float)) or isinstance(price, bool) or not 0 < price <= 1e9:
                raise ValueError("CoinGecko returned an invalid BTC price")
            self._set_cached("btc_price", price)
            return price
        except Exception as e:
            logger.debug("Failed to fetch BTC price: %s", e)
            return None

    def get_eth_price(self) -> float | None:
        """Get current ETH price in USD."""
        cached = self._get_cached("eth_price")
        if cached is not None:
            return cached

        try:
            import requests
            resp = requests.get(
                "https://api.coingecko.com/api/v3/simple/price",
                params={"ids": "ethereum", "vs_currencies": "usd"},
                timeout=5,
            )
            resp.raise_for_status()
            price = resp.json()["ethereum"]["usd"]
            if not isinstance(price, (int, float)) or isinstance(price, bool) or not 0 < price <= 1e9:
                raise ValueError("CoinGecko returned an invalid ETH price")
            self._set_cached("eth_price", price)
            return price
        except Exception as e:
            logger.debug("Failed to fetch ETH price: %s", e)
            return None

    def get_signal(self, source: str) -> float | None:
        """Get signal value by source name.

        Args:
            source: Signal source identifier.

        Returns:
            Signal value, or None if unavailable.
        """
        if source == "btc_price":
            return self.get_btc_price()
        elif source == "eth_price":
            return self.get_eth_price()
        return None


_signal_fetcher: ExternalSignalFetcher | None = None


def get_signal_fetcher() -> ExternalSignalFetcher:
    """Get or create the module-level ExternalSignalFetcher."""
    global _signal_fetcher
    if _signal_fetcher is None:
        _signal_fetcher = ExternalSignalFetcher()
    return _signal_fetcher


# ---------------------------------------------------------------------------
# Implied probability calculation
# ---------------------------------------------------------------------------

def _calculate_implied_prob(
    current_value: float,
    threshold: float,
    direction: str,
    days_remaining: float = 30,
) -> float:
    """Calculate implied probability based on current value and threshold.

    Uses a simple distance-based model. More sophisticated models
    could use volatility, trend, or options-implied probabilities.

    Args:
        current_value: Current signal value.
        threshold: Target threshold value.
        direction: "above" or "below".
        days_remaining: Days until resolution (affects probability).

    Returns:
        Implied probability (0-1).
    """
    if direction == "above":
        distance_pct = (threshold - current_value) / current_value if current_value > 0 else 1
        if distance_pct <= 0:
            return 0.90
        elif distance_pct < 0.10:
            return 0.70
        elif distance_pct < 0.20:
            return 0.50
        elif distance_pct < 0.50:
            return 0.25
        else:
            return 0.10
    else:
        distance_pct = (current_value - threshold) / current_value if current_value > 0 else 1
        if distance_pct <= 0:
            return 0.90
        elif distance_pct < 0.10:
            return 0.70
        elif distance_pct < 0.20:
            return 0.50
        elif distance_pct < 0.50:
            return 0.25
        else:
            return 0.10


def _match_rule(market_title: str) -> CorrelationRule | None:
    """Find a correlation rule matching a market title.

    Args:
        market_title: Market title to match.

    Returns:
        Matching CorrelationRule, or None.
    """
    title_lower = market_title.lower()

    for rule in CORRELATION_RULES:
        matches = sum(1 for kw in rule.keywords if kw.lower() in title_lower)
        if matches >= 2:
            return rule

    return None


# ---------------------------------------------------------------------------
# Scan function
# ---------------------------------------------------------------------------

def scan_cross_category(
    markets: list[dict],
    platform: str = "polymarket",
    signal_fetcher: ExternalSignalFetcher | None = None,
    min_divergence: float | None = None,
    min_profit: float = 0.005,
) -> list[dict]:
    """Scan for cross-category correlation opportunities.

    Identifies markets where external signals imply a different
    probability than the current market price.

    Args:
        markets: List of market dicts.
        platform: Platform name for the markets.
        signal_fetcher: ExternalSignalFetcher instance.
        min_divergence: Minimum divergence to flag (default from config).
        min_profit: Minimum net profit threshold.

    Returns:
        List of opportunity dicts sorted by net_profit descending.
    """
    if not CROSS_CATEGORY_ENABLED:
        return []

    fetcher = signal_fetcher or get_signal_fetcher()
    if min_divergence is None:
        min_divergence = CROSS_CATEGORY_MIN_DIVERGENCE
    opportunities = []

    for market in markets:
        title = market.get("title") or market.get("question", "")
        market_price = market.get("yes_price") or market.get("yes_mid", 0)

        if not title or not market_price or market_price <= 0 or market_price >= 1:
            continue

        rule = _match_rule(title)
        if rule is None:
            continue

        signal_value = fetcher.get_signal(rule.signal_source)
        if signal_value is None:
            continue

        implied_prob = _calculate_implied_prob(
            current_value=signal_value,
            threshold=rule.threshold_value,
            direction=rule.direction,
        )

        divergence = implied_prob - market_price

        if abs(divergence) < min_divergence:
            continue

        if divergence > 0:
            direction = "BUY_YES"
            entry_price = market_price
        else:
            direction = "BUY_NO"
            entry_price = 1.0 - market_price

        from fees import net_profit_cross_category
        result = net_profit_cross_category(
            market_price=entry_price,
            implied_prob=implied_prob if direction == "BUY_YES" else (1.0 - implied_prob),
            platform=platform,
        )

        if result["net_profit"] < min_profit:
            continue

        confidence = min(0.70, 0.45 + abs(divergence) * 0.5) * rule.confidence_multiplier

        opp = {
            "type": "CrossCategory",
            "_layer": 4,
            "market": f"{title[:40]}... (cross-category)",
            "prices": f"market={market_price:.3f} implied={implied_prob:.3f} ({rule.signal_source}={signal_value:.0f})",
            "total_cost": f"${entry_price:.4f}",
            "net_profit": result["net_profit"],
            "net_roi": result.get("net_roi", 0),
            "confidence": confidence,
            "_market_key": market.get("condition_id") or market.get("id", ""),
            "_platform": platform,
            "_market": market,
            "_market_price": market_price,
            "_implied_prob": implied_prob,
            "_signal_source": rule.signal_source,
            "_signal_value": signal_value,
            "_threshold": rule.threshold_value,
            "_divergence": divergence,
            "_direction": direction,
        }
        opp["_efficiency"] = capital_efficiency_score(opp)
        opportunities.append(opp)

    opportunities = filter_dust(opportunities, min_amount=min_profit)
    opportunities.sort(key=lambda o: o["net_profit"], reverse=True)

    logger.info("Cross-category scan: found %d opportunities", len(opportunities))
    return opportunities
