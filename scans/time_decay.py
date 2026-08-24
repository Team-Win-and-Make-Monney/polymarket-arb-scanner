"""Time decay convergence — buy near-certain outcomes approaching market expiry."""

import logging
import math
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

from .helpers import capital_efficiency_score, _fetch_clob_for_market

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Time decay detection
# ---------------------------------------------------------------------------

def scan_time_decay(
    markets_by_key: dict,
    signal_aggregator,
    min_hours_to_expiry: int = 48,
    min_consensus: float = 0.90,
    buy_below_price: float = 0.95,
    price_cache: dict | None = None,
) -> list[dict]:
    """Detect near-expiry markets with high consensus for profitable convergence.

    Stage 1 scan: Find markets <48h from resolution with >90% consensus and
    current price < 0.95 (buy at discount, hold to resolution, capture 5%+ gain).

    Args:
        markets_by_key: Dict of market_key -> market dict (must have
            resolutionSource with timestamp field).
        signal_aggregator: SignalAggregator instance with get_consensus() method.
        min_hours_to_expiry: Minimum hours remaining for sweet spot (default 48).
        min_consensus: Minimum consensus probability to consider (default 0.90).
        buy_below_price: Maximum entry price for opportunity (default 0.95).
        price_cache: Optional dict of recent prices for efficiency.

    Returns:
        List of opportunity dicts with type="TimeDecay", _hours_to_expiry,
        _consensus_side, _consensus_prob, _target_price, _guaranteed_gain.
    """
    opportunities = []
    now = time.time()

    for market_key, market in markets_by_key.items():
        resolution_source = market.get("resolutionSource")
        if not isinstance(resolution_source, dict):
            logger.debug("TimeDecay skipped: %s has invalid resolutionSource", market_key)
            continue
        resolution_timestamp = resolution_source.get("timestamp")
        if (
            isinstance(resolution_timestamp, bool)
            or not isinstance(resolution_timestamp, (int, float))
            or not math.isfinite(resolution_timestamp)
        ):
            logger.debug("TimeDecay skipped: %s has invalid resolution timestamp", market_key)
            continue
        # Check time to resolution — must be in sweet spot (1 < hours <= min_hours_to_expiry)
        hours_left = _check_time_to_expiry(
            resolution_timestamp,
            min_hours=min_hours_to_expiry
        )
        if hours_left is None:
            continue

        # Get consensus probability from signal aggregator
        consensus_data = signal_aggregator.get_consensus(market_key)
        if consensus_data is None:
            continue

        consensus_prob = consensus_data.get("probability") if isinstance(consensus_data, dict) else consensus_data
        if isinstance(consensus_prob, bool) or not isinstance(consensus_prob, (int, float)):
            continue

        # Validate consensus meets threshold
        if not _validate_consensus(consensus_prob, min_threshold=min_consensus):
            continue

        # Determine consensus side: YES if consensus > 0.50, else NO
        consensus_side = "YES" if consensus_prob >= 0.50 else "NO"

        # Target price is the consensus probability (or 1 - consensus if NO side)
        target_price = consensus_prob if consensus_side == "YES" else 1.0 - consensus_prob

        market_yes_price = market.get("price")
        if (
            isinstance(market_yes_price, bool)
            or not isinstance(market_yes_price, (int, float))
            or not math.isfinite(market_yes_price)
            or not 0.0 <= market_yes_price <= 1.0
        ):
            continue
        current_price = market_yes_price if consensus_side == "YES" else 1.0 - market_yes_price
        if current_price >= buy_below_price or current_price >= target_price:
            continue

        guaranteed_gain = target_price - current_price

        opportunities.append({
            "type": "TimeDecay",
            "market": market.get("question", ""),
            "market_key": market_key,
            "_hours_to_expiry": hours_left,
            "_consensus_side": consensus_side,
            "_consensus_prob": consensus_prob,
            "_target_price": target_price,
            "_guaranteed_gain": guaranteed_gain,
            "_current_price": current_price,
        })

        logger.info(
            "TimeDecay: %s, expiry=%.1f hours, consensus=%.1f%%, gain=%.1f%%",
            market.get("question", market_key),
            hours_left,
            consensus_prob * 100,
            guaranteed_gain * 100
        )

    return opportunities


def _check_time_to_expiry(resolution_timestamp: int | None, min_hours: int = 48) -> float | None:
    """Check if market is in sweet spot for time decay trading (1 < hours <= min_hours).

    Args:
        resolution_timestamp: Unix seconds timestamp of market resolution.
        min_hours: Maximum hours for the sweet spot (default 48).

    Returns:
        Float hours remaining if in sweet spot [1, min_hours], None otherwise.
    """
    if resolution_timestamp is None:
        logger.warning("No resolution timestamp provided")
        return None

    now = time.time()
    hours_left = (resolution_timestamp - now) / 3600.0

    # Sweet spot: 1 < hours <= min_hours
    if hours_left < 1.0:
        logger.debug("Market too close to resolution: %.1f hours left", hours_left)
        return None

    if hours_left > min_hours:
        logger.debug("Market too far from resolution: %.1f hours left", hours_left)
        return None

    return hours_left


def _validate_consensus(consensus: float, min_threshold: float = 0.90) -> bool:
    """Validate consensus probability meets minimum threshold.

    Args:
        consensus: Probability estimate (0-1).
        min_threshold: Minimum threshold for acceptance (default 0.90).

    Returns:
        True if consensus >= min_threshold, False otherwise.
    """
    if isinstance(consensus, bool) or not isinstance(consensus, (int, float)):
        logger.warning("Invalid consensus type: %s", type(consensus))
        return False

    if not 0.0 <= consensus <= 1.0:
        logger.warning("Consensus probability out of range: %s", consensus)
        return False

    if max(consensus, 1.0 - consensus) < min_threshold:
        logger.debug("Consensus %.1f%% below threshold %.1f%%", consensus * 100, min_threshold * 100)
        return False

    return True


def _refine_time_decay_with_prices(
    opportunities: list[dict],
    current_prices: dict | None = None,
    current_time: float | None = None,
    markets_by_key: dict | None = None,
    signal_aggregator=None,
    price_cache: dict | None = None,
    min_consensus: float = 0.90,
) -> list[dict]:
    """Stage 2: Re-validate time decay opportunities against live data.

    First-class refinement (PR B): re-fetch CLOB asks for the
    Polymarket-side market, re-fetch the current consensus, and re-check
    hours-to-expiry against the live ``resolutionSource.timestamp``. Falls
    back to the legacy stored-price behaviour when ``markets_by_key`` is
    not provided so existing call sites keep working.

    Drops opportunities where:
    - Hours to expiry has dropped below 1 hour (too late to enter)
    - Live ask price >= target (no profitable convergence left)
    - Live consensus has fallen below ``min_consensus`` (signal decayed)
    - Sentiment has flipped (consensus side no longer matches Stage 1)

    Args:
        opportunities: List of opportunity dicts from Stage 1.
        current_prices: Legacy dict of market_key -> current_price.
        current_time: Optional current timestamp (defaults to time.time()).
        markets_by_key: Optional dict of market_key -> market dict. When
            provided, refiner re-fetches CLOB asks for each market in
            parallel (mirrors the canonical pattern from
            ``scans/cross.py:_refine_cross_with_clob``).
        signal_aggregator: Optional aggregator with ``get_consensus()``
            for live consensus refresh.
        price_cache: Optional WS price cache passed through to
            ``_fetch_clob_for_market``.
        min_consensus: Minimum consensus to retain after refinement.

    Returns:
        Refined list of opportunities still meeting criteria.
    """
    if not opportunities:
        return opportunities

    if not current_prices:
        current_prices = {}

    if current_time is None:
        current_time = time.time()

    # Parallel CLOB ask refresh — only when markets_by_key is supplied.
    clob_results: dict = {}
    if markets_by_key:
        fetch_tasks = {}
        for opp in opportunities:
            mk = opp.get("market_key")
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
                        logger.debug("TimeDecay CLOB fetch failed for %s: %s", mk, e)
                        clob_results[mk] = None

    refined: list[dict] = []
    for opp in opportunities:
        market_key = opp.get("market_key")
        market = markets_by_key.get(market_key) if markets_by_key else None

        # Re-check hours-to-expiry from live resolution timestamp when available.
        live_hours_left = opp.get("_hours_to_expiry", 0.0)
        if market is not None:
            resolution_source = market.get("resolutionSource")
            resolution_ts = resolution_source.get("timestamp") if isinstance(resolution_source, dict) else None
            if resolution_ts is not None:
                if (
                    isinstance(resolution_ts, bool)
                    or not isinstance(resolution_ts, (int, float))
                    or not math.isfinite(resolution_ts)
                ):
                    logger.debug("TimeDecay dropped: %s invalid live resolution timestamp", market_key)
                    continue
                live_hours_left = (resolution_ts - current_time) / 3600.0
                opp["_hours_to_expiry"] = live_hours_left

        if live_hours_left < 1.0:
            logger.debug("TimeDecay dropped: %s expired (<1h remaining)", market_key)
            continue

        # Re-fetch live consensus if a signal aggregator is provided.
        if signal_aggregator is not None and market_key:
            try:
                consensus_data = signal_aggregator.get_consensus(market_key)
            except Exception as e:
                logger.debug("TimeDecay consensus refresh failed for %s: %s", market_key, e)
                continue

            if consensus_data is not None:
                live_prob = (
                    consensus_data.get("probability")
                    if isinstance(consensus_data, dict)
                    else consensus_data
                )
                if not isinstance(live_prob, bool) and isinstance(live_prob, (int, float)):
                    if not _validate_consensus(live_prob, min_consensus):
                        logger.debug("TimeDecay dropped: %s consensus %.2f < %.2f",
                                    market_key, live_prob, min_consensus)
                        continue
                    live_side = "YES" if live_prob >= 0.50 else "NO"
                    if opp.get("_consensus_side") and live_side != opp["_consensus_side"]:
                        logger.debug("TimeDecay dropped: %s consensus side flipped %s->%s",
                                    market_key, opp.get("_consensus_side"), live_side)
                        continue
                    opp["_consensus_prob"] = live_prob
                    opp["_target_price"] = live_prob if live_side == "YES" else 1.0 - live_prob
                else:
                    logger.debug("TimeDecay dropped: %s invalid live consensus", market_key)
                    continue
            else:
                logger.debug("TimeDecay dropped: %s live consensus unavailable", market_key)
                continue

        # Live ask comparison from CLOB. A supplied market must have a real,
        # executable selected-side ask; bids are never converted into asks.
        live_ask = None
        clob = clob_results.get(market_key) if market_key else None
        if market is not None and not clob:
            logger.debug("TimeDecay dropped: %s executable CLOB unavailable", market_key)
            continue
        if clob:
            consensus_side = opp.get("_consensus_side", "YES")
            live_ask = clob.get("yes_ask") if consensus_side == "YES" else clob.get("no_ask")
        elif market is None:
            live_ask = current_prices.get(market_key, opp.get("_current_price"))

        if (
            isinstance(live_ask, bool)
            or not isinstance(live_ask, (int, float))
            or not math.isfinite(live_ask)
            or not 0.0 < live_ask < 1.0
        ):
            logger.debug("TimeDecay dropped: %s invalid executable ask", market_key)
            continue

        opp["_current_price"] = live_ask
        if clob:
            opp["_clob_depth"] = clob.get(
                "yes_ask_size" if opp.get("_consensus_side", "YES") == "YES" else "no_ask_size", 0
            ) or 0

        target_price = opp.get("_target_price", 0.95)
        if live_ask >= target_price:
            logger.debug("TimeDecay dropped: %s ask %.3f >= target %.3f",
                        market_key, live_ask, target_price)
            continue
        opp["_guaranteed_gain"] = target_price - live_ask

        refined.append(opp)

    logger.info("TimeDecay refined: %d/%d still profitable at live prices",
                len(refined), len(opportunities))
    return refined
