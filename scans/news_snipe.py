"""News-driven resolution sniping strategy using Finnhub news headlines."""

import logging
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

from thefuzz import fuzz

from .helpers import _fetch_clob_for_market

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Sentiment keyword definitions
# ---------------------------------------------------------------------------

YES_KEYWORDS = [
    "approved",
    "confirmed",
    "passed",
    "granted",
    "successful",
    "adopted",
    "launched",
    "completed",
]

NO_KEYWORDS = [
    "rejected",
    "failed",
    "denied",
    "blocked",
    "withdrawn",
    "cancelled",
    "delayed",
]


# ---------------------------------------------------------------------------
# Stage 1: News scanning and signal extraction
# ---------------------------------------------------------------------------


def scan_news_snipe(
    markets_by_key: dict,
    finnhub_client,
    cooldown_cache: dict | None = None,
    fuzzy_threshold: int = 70,
) -> list[dict]:
    """Stage 1: Scan news headlines and extract trading signals.

    Fetches recent news, matches to markets, scores sentiment, and applies cooldown.

    Args:
        markets_by_key: Dict of market_key -> market dict (with 'question' field).
        finnhub_client: FinnhubNewsClient instance for fetching news.
        cooldown_cache: Dict tracking market_key -> last_execution_time for deduplication.
        fuzzy_threshold: Fuzzy match threshold (0-100, default 70).

    Returns:
        List of opportunity dicts with type='NewsSnipe', _headline, _sentiment, _confidence.
    """
    if cooldown_cache is None:
        cooldown_cache = {}

    opportunities = []
    current_time = time.time()

    # Build list of symbols from markets (simplified: extract from market titles)
    symbols = _extract_symbols_from_markets(markets_by_key)

    # Fetch headlines for each symbol
    all_headlines = []
    for symbol in symbols[:10]:  # Limit to 10 symbols to avoid rate limits
        try:
            from datetime import datetime, timedelta, timezone
            to_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            from_date = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%d")
            headlines = finnhub_client.fetch_company_news(symbol, from_date, to_date)
            all_headlines.extend(headlines)
        except Exception as e:
            logger.warning("Failed to fetch news for %s: %s", symbol, str(e))
            continue

    # Extract signals from headlines
    signals = extract_news_signals(all_headlines, markets_by_key, fuzzy_threshold)

    # Apply cooldown filter
    for signal in signals:
        market_key = signal.get("_market_key", "")
        if market_key in cooldown_cache:
            if cooldown_cache[market_key] > current_time:
                logger.info(
                    "NewsSnipe: cooldown active for %s, skipping",
                    market_key,
                )
                continue

        opportunities.append(signal)

    logger.info(
        "NewsSnipe: matched %d headlines to %d opportunities",
        len(all_headlines),
        len(opportunities),
    )

    return opportunities


def extract_news_signals(
    headlines: list[dict],
    markets_by_key: dict,
    fuzzy_threshold: int = 70,
) -> list[dict]:
    """Extract trading signals from news headlines via fuzzy matching.

    For each headline, attempts to match to a market question. If matched and
    sentiment is detected, returns opportunity.

    Args:
        headlines: List of headline dicts from Finnhub (headline, summary, url, datetime).
        markets_by_key: Dict of market_key -> market dict.
        fuzzy_threshold: Fuzzy match threshold.

    Returns:
        List of signal dicts with _sentiment, _confidence, _headline, _market_key.
    """
    signals = []

    for headline in headlines:
        headline_text = headline.get("headline", "")
        summary_text = headline.get("summary", "")
        full_text = (headline_text + " " + summary_text).lower()

        if not headline_text:
            continue

        # Try to match to a market
        for market_key, market in markets_by_key.items():
            market_question = market.get("question", "").lower()
            if not market_question:
                continue

            # Fuzzy match headline to market question
            similarity = fuzz.token_set_ratio(headline_text.lower(), market_question)
            if similarity < fuzzy_threshold:
                continue

            # Score sentiment
            sentiment_result = _score_sentiment(full_text)
            if sentiment_result["sentiment"] is None:
                continue

            # Record signal — keep the headline timestamp so Stage 2 can
            # drop signals older than NEWS_SNIPE_MAX_AGE_MINUTES.
            signal = {
                "type": "NewsSnipe",
                "market": market.get("question", ""),
                "_headline": headline_text,
                "_sentiment": sentiment_result["sentiment"],
                "_confidence": sentiment_result["confidence"],
                "_market_key": market_key,
                "_news_timestamp": headline.get("datetime"),
            }
            signals.append(signal)
            logger.info(
                "Sentiment matched: %s in '%s' (confidence: %.2f)",
                sentiment_result["sentiment"],
                headline_text[:100],
                sentiment_result["confidence"],
            )
            break  # One market per headline

    return signals


def _score_sentiment(text: str) -> dict:
    """Score sentiment of text based on keyword presence.

    Searches for YES and NO keywords in the text. Returns first match found.
    Confidence: 0.8 if keyword in headline, 0.6 if elsewhere.

    Args:
        text: Full text (headline + summary).

    Returns:
        Dict with 'sentiment' (YES/NO/None) and 'confidence' (0.0-1.0).
    """
    # Search for YES keywords first
    for keyword in YES_KEYWORDS:
        if re.search(rf"\b{re.escape(keyword)}\b", text):
            return {"sentiment": "YES", "confidence": 0.8}

    # Then search for NO keywords
    for keyword in NO_KEYWORDS:
        if re.search(rf"\b{re.escape(keyword)}\b", text):
            return {"sentiment": "NO", "confidence": 0.8}

    return {"sentiment": None, "confidence": 0.0}


# ---------------------------------------------------------------------------
# Stage 2: Refinement and validation
# ---------------------------------------------------------------------------


def _refine_news_with_confidence(
    opportunities: list[dict],
    confidence_floor: float = 0.5,
    cooldown_cache: dict | None = None,
    markets_by_key: dict | None = None,
    price_cache: dict | None = None,
    max_age_minutes: int | None = None,
    current_time: float | None = None,
) -> list[dict]:
    """Stage 2: First-class refinement of news snipe opportunities.

    Filters in this order:
    1. Confidence below floor (existing behaviour)
    2. Cooldown active for this market (existing behaviour)
    3. Headline staleness — drop if older than ``max_age_minutes``
    4. CLOB ask already crossed — drop if the market has moved past the
       sentiment direction (e.g. YES sentiment but ask already > 0.50, or
       NO sentiment but ask already < 0.50). Reuses
       ``scans.helpers._fetch_clob_for_market`` parallel pattern from
       ``scans/cross.py:_refine_cross_with_clob``.

    Args:
        opportunities: Stage 1 signals.
        confidence_floor: Minimum confidence to keep.
        cooldown_cache: Dict tracking market_key -> last_execution_time.
        markets_by_key: Optional Polymarket market lookup. When supplied,
            triggers parallel CLOB ask re-fetch.
        price_cache: WS price cache passed through to the helper.
        max_age_minutes: Optional override for staleness window
            (defaults to ``config.NEWS_SNIPE_MAX_AGE_MINUTES``).
        current_time: Optional fixed timestamp for deterministic tests.

    Returns:
        Refined list of opportunities.
    """
    if not opportunities:
        return opportunities

    if cooldown_cache is None:
        cooldown_cache = {}

    if current_time is None:
        current_time = time.time()

    if max_age_minutes is None:
        from config import NEWS_SNIPE_MAX_AGE_MINUTES
        max_age_minutes = int(NEWS_SNIPE_MAX_AGE_MINUTES)
    max_age_seconds = int(max_age_minutes) * 60

    # Parallel CLOB fetch when markets are available.
    clob_results: dict = {}
    if markets_by_key:
        fetch_tasks = {}
        for opp in opportunities:
            mk = opp.get("_market_key")
            market = markets_by_key.get(mk) if mk else None
            if market is not None and mk not in fetch_tasks:
                fetch_tasks[mk] = market

        if fetch_tasks:
            with ThreadPoolExecutor(max_workers=4) as pool:
                futures = {
                    pool.submit(_fetch_clob_for_market, m, price_cache): mk
                    for mk, m in fetch_tasks.items()
                }
                for future in as_completed(futures):
                    mk = futures[future]
                    try:
                        _, clob = future.result()
                        clob_results[mk] = clob
                    except Exception as e:
                        logger.debug("NewsSnipe CLOB fetch failed for %s: %s", mk, e)
                        clob_results[mk] = None

    refined: list[dict] = []
    for opp in opportunities:
        confidence = opp.get("_confidence", 0.0)

        # 1. Confidence floor.
        if confidence < confidence_floor:
            logger.info(
                "NewsSnipe dropped: confidence %.2f < %.2f floor",
                confidence, confidence_floor,
            )
            continue

        market_key = opp.get("_market_key", "")

        # 2. Cooldown.
        if market_key in cooldown_cache:
            if cooldown_cache[market_key] > current_time:
                logger.info("NewsSnipe dropped: %s on cooldown", market_key)
                continue

        # 3. Headline freshness.
        news_ts = opp.get("_news_timestamp")
        if isinstance(news_ts, (int, float)) and news_ts > 0:
            age_sec = current_time - float(news_ts)
            if age_sec > max_age_seconds:
                logger.info(
                    "NewsSnipe dropped: %s headline age %.0fs > %ds (%.0f min cap)",
                    market_key, age_sec, max_age_seconds, max_age_minutes,
                )
                continue

        # 4. Live CLOB ask check — drop if the market has already priced in
        #    the direction implied by the news sentiment.
        clob = clob_results.get(market_key) if market_key else None
        if clob:
            sentiment = (opp.get("_sentiment") or "").upper()
            yes_ask = clob.get("yes_ask")
            no_ask = clob.get("no_ask")
            opp["_clob_yes_ask"] = yes_ask
            opp["_clob_no_ask"] = no_ask

            # If YES sentiment but the YES ask is already >= 0.50, we'd be
            # buying after the news has already moved the market. Same logic
            # mirrored for NO. This is a coarse but informative gate.
            if sentiment == "YES" and yes_ask is not None and yes_ask >= 0.50:
                logger.info(
                    "NewsSnipe dropped: %s YES sentiment but YES ask %.3f already >= 0.50",
                    market_key, yes_ask,
                )
                continue
            if sentiment == "NO" and no_ask is not None and no_ask >= 0.50:
                logger.info(
                    "NewsSnipe dropped: %s NO sentiment but NO ask %.3f already >= 0.50",
                    market_key, no_ask,
                )
                continue

        refined.append(opp)

    logger.info(
        "NewsSnipe refined: %d/%d opportunities passed all gates",
        len(refined),
        len(opportunities),
    )

    return refined


# ---------------------------------------------------------------------------
# Helper functions
# ---------------------------------------------------------------------------


def _extract_symbols_from_markets(markets_by_key: dict) -> list[str]:
    """Extract stock symbols from market questions.

    Simple heuristic: look for capital letters that appear alone or in pairs
    in the question text.

    Args:
        markets_by_key: Dict of markets.

    Returns:
        List of symbols (up to 10 for rate limit safety).
    """
    symbols = set()

    for market in markets_by_key.values():
        question = market.get("question", "")

        # Simple extraction: look for capitalized words of 1-5 chars
        import re
        candidates = re.findall(r"\b[A-Z]{1,5}\b", question)
        for candidate in candidates:
            if len(candidate) >= 1 and candidate not in ("THE", "AND", "FOR", "WITH"):
                symbols.add(candidate)

    return list(symbols)[:10]
