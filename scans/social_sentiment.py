"""Social Sentiment Signals — Strategy #39.

Trade when social media sentiment diverges significantly from market price.

Sources:
- Twitter: Real-time sentiment from tweets mentioning market topics
- Reddit: Community sentiment from prediction/trading subreddits

Strategy:
1. Aggregate sentiment scores from Twitter and Reddit
2. Convert to implied probability (bullish=higher, bearish=lower)
3. Compare against market price
4. When divergence exceeds threshold, bet on convergence

Layer 4: Informed trading — directional bet based on sentiment edge.
"""

import logging
import time

from config import (
    SOCIAL_SENTIMENT_ENABLED,
    SOCIAL_SENTIMENT_MIN_DIVERGENCE,
    SOCIAL_SENTIMENT_MIN_SAMPLE_SIZE,
    SOCIAL_SENTIMENT_WEIGHT_TWITTER,
    SOCIAL_SENTIMENT_WEIGHT_REDDIT,
)
from .helpers import capital_efficiency_score, filter_dust

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Sentiment aggregation
# ---------------------------------------------------------------------------

def _aggregate_sentiment(
    twitter_client=None,
    reddit_client=None,
    market_title: str = "",
    market_key: str = "",
) -> dict | None:
    """Aggregate sentiment from Twitter and Reddit.

    Args:
        twitter_client: TwitterClient instance.
        reddit_client: RedditClient instance.
        market_title: Market title for search queries.
        market_key: Market identifier for caching.

    Returns:
        Dict with score, implied_prob, sample_size, or None.
    """
    if not market_title:
        return None

    scores = []
    weights = []
    total_samples = 0

    if twitter_client:
        try:
            result = twitter_client.get_market_sentiment(market_title)
            if result and result.get("sample_size", 0) >= 5:
                scores.append(result["sentiment_score"])
                weights.append(SOCIAL_SENTIMENT_WEIGHT_TWITTER)
                total_samples += result["sample_size"]
        except Exception as e:
            logger.debug("Twitter sentiment failed for %s: %s", market_title[:30], e)

    if reddit_client:
        try:
            result = reddit_client.get_market_sentiment(market_title)
            if result and result.get("sample_size", 0) >= 3:
                scores.append(result["sentiment_score"])
                weights.append(SOCIAL_SENTIMENT_WEIGHT_REDDIT)
                total_samples += result["sample_size"]
        except Exception as e:
            logger.debug("Reddit sentiment failed for %s: %s", market_title[:30], e)

    if not scores:
        return None

    weighted_score = sum(s * w for s, w in zip(scores, weights)) / sum(weights)

    implied_prob = max(0.0, min(1.0, (weighted_score + 1) / 2))

    return {
        "score": weighted_score,
        "implied_prob": implied_prob,
        "sample_size": total_samples,
        "sources": len(scores),
    }


# ---------------------------------------------------------------------------
# Scan function
# ---------------------------------------------------------------------------

def scan_social_sentiment(
    markets: list[dict],
    platform: str = "polymarket",
    twitter_client=None,
    reddit_client=None,
    min_divergence: float | None = None,
    min_samples: int | None = None,
    min_profit: float = 0.005,
) -> list[dict]:
    """Scan for social sentiment divergence opportunities.

    Identifies markets where social media sentiment diverges
    significantly from the current market price.

    Args:
        markets: List of market dicts.
        platform: Platform name for the markets.
        twitter_client: TwitterClient instance.
        reddit_client: RedditClient instance.
        min_divergence: Minimum divergence to flag (default from config).
        min_samples: Minimum sample size (default from config).
        min_profit: Minimum net profit threshold.

    Returns:
        List of opportunity dicts sorted by net_profit descending.
    """
    if not SOCIAL_SENTIMENT_ENABLED:
        return []

    if twitter_client is None and reddit_client is None:
        logger.debug("Social sentiment scan requires at least one client")
        return []

    min_divergence = min_divergence or SOCIAL_SENTIMENT_MIN_DIVERGENCE
    min_samples = min_samples or SOCIAL_SENTIMENT_MIN_SAMPLE_SIZE
    opportunities = []

    for market in markets:
        title = market.get("title") or market.get("question", "")
        market_price = market.get("yes_price") or market.get("yes_mid", 0)

        if not title or not market_price or market_price <= 0 or market_price >= 1:
            continue

        sentiment = _aggregate_sentiment(
            twitter_client=twitter_client,
            reddit_client=reddit_client,
            market_title=title,
            market_key=market.get("condition_id") or market.get("id", ""),
        )

        if sentiment is None:
            continue

        if sentiment["sample_size"] < min_samples:
            continue

        implied_prob = sentiment["implied_prob"]
        divergence = implied_prob - market_price

        if abs(divergence) < min_divergence:
            continue

        if divergence > 0:
            direction = "BUY_YES"
            entry_price = market_price
            edge = divergence
        else:
            direction = "BUY_NO"
            entry_price = 1.0 - market_price
            edge = -divergence

        from fees import net_profit_social_sentiment
        result = net_profit_social_sentiment(
            market_price=entry_price,
            implied_prob=implied_prob if direction == "BUY_YES" else (1.0 - implied_prob),
            platform=platform,
        )

        if result["net_profit"] < min_profit:
            continue

        confidence = min(0.75, 0.40 + (sentiment["sample_size"] / 100) + (abs(divergence) * 0.5))

        opp = {
            "type": "SocialSentiment",
            "_layer": 4,
            "market": f"{title[:40]}... (social sentiment)",
            "prices": f"market={market_price:.3f} sentiment={implied_prob:.3f} (div={divergence:+.3f})",
            "total_cost": f"${entry_price:.4f}",
            "net_profit": result["net_profit"],
            "net_roi": result.get("net_roi", 0),
            "confidence": confidence,
            "_market_key": market.get("condition_id") or market.get("id", ""),
            "_platform": platform,
            "_market": market,
            "_market_price": market_price,
            "_implied_prob": implied_prob,
            "_sentiment_score": sentiment["score"],
            "_sample_size": sentiment["sample_size"],
            "_divergence": divergence,
            "_direction": direction,
        }
        opp["_efficiency"] = capital_efficiency_score(opp)
        opportunities.append(opp)

    opportunities = filter_dust(opportunities, min_amount=min_profit)
    opportunities.sort(key=lambda o: o["net_profit"], reverse=True)

    logger.info("Social sentiment scan: found %d opportunities", len(opportunities))
    return opportunities
