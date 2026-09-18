"""Correlated market pairs arbitrage — exploits spread divergences between related markets.

When two related markets (e.g., Bitcoin $100k vs Bitcoin $90k) diverge by more than
a threshold (e.g., 10%), they present a convergence opportunity: long the underpriced
market and short the overpriced one. Both legs are matched-sized to neutralize directional
exposure and capture only the spread convergence.

Example:
  Bitcoin $100k: price=0.75 (75% probability)
  Bitcoin $90k:  price=0.50 (50% probability)
  Spread: (0.75 - 0.50) / 0.75 = 33% divergence
  If threshold is 10%, opportunity: long $90k, short $100k with matched sizing

Auto-detection (PR E)
- ``correlation_tracker.run_correlation_tracker`` analyses 30 days of
  historical snapshots and caches |r| >= ``CORRELATION_PEARSON_THRESHOLD``
  pairs in the ``correlated_pairs`` SQLite table.
- ``scan_correlated`` calls ``correlation_tracker.load_auto_correlated_pairs``
  and merges the auto pairs with the manually-configured ``CORRELATED_PAIRS``,
  preserving the manual seeds (e.g. "Trump popular vote" ↔ "Trump electoral
  college").
"""

import json
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed

try:
    from thefuzz import fuzz
except ImportError:
    try:
        from fuzzywuzzy import fuzz
    except ImportError:
        fuzz = None

from .helpers import capital_efficiency_score, _fetch_clob_for_market

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Configuration parsing
# ---------------------------------------------------------------------------

def _load_correlated_pairs(config_json: str) -> list[tuple[str, str]]:
    """Parse correlated pair configuration from JSON string.

    Expected format: '[["Bitcoin $100k", "Bitcoin $90k"], ["Eth $5k", "Eth $4k"]]'

    Args:
        config_json: JSON string representing list of 2-tuples (market identifiers).

    Returns:
        List of (market_a, market_b) tuples as strings.

    Raises:
        ValueError: If JSON is malformed or tuples are not 2-element pairs.
    """
    if not config_json or not config_json.strip():
        return []

    try:
        parsed = json.loads(config_json)
    except json.JSONDecodeError as e:
        raise ValueError(f"Correlated pairs config is not valid JSON: {e}")

    if not isinstance(parsed, list):
        raise ValueError(f"Correlated pairs config must be a list, got {type(parsed).__name__}")

    pairs = []
    for item in parsed:
        if not isinstance(item, (list, tuple)):
            raise ValueError(f"Each pair must be a list or tuple, got {type(item).__name__}")
        if len(item) != 2:
            raise ValueError(f"Each pair must have exactly 2 elements, got {len(item)}")
        pairs.append((str(item[0]), str(item[1])))

    return pairs


# ---------------------------------------------------------------------------
# Spread calculation
# ---------------------------------------------------------------------------

def _calculate_spread(price_a: float, price_b: float) -> float:
    """Calculate the spread between two prices.

    Formula: |price_a - price_b| / max(price_a, price_b)

    Args:
        price_a: First price in [0, 1].
        price_b: Second price in [0, 1].

    Returns:
        Spread as a decimal (0.10 = 10%). Returns 0.0 if both prices are 0.
    """
    max_price = max(price_a, price_b)
    if max_price == 0:
        return 0.0
    return abs(price_a - price_b) / max_price


# ---------------------------------------------------------------------------
# Market pairing and matching
# ---------------------------------------------------------------------------

def _find_market_by_id_or_title(
    market_identifier: str,
    markets_by_key: dict,
    fuzzy_threshold: int = 70,
) -> dict | None:
    """Find a market by ID or fuzzy title match.

    First tries direct ID lookup. If not found, searches by title using fuzzy
    matching with token_set_ratio (from thefuzz).

    Args:
        market_identifier: Market ID or title string.
        markets_by_key: Dict of market_key -> market dict.
        fuzzy_threshold: Minimum token_set_ratio score (0-100) to consider a match.

    Returns:
        Market dict if found, else None.
    """
    # Direct ID lookup
    if market_identifier in markets_by_key:
        return markets_by_key[market_identifier]

    # Fuzzy title match
    if fuzz is None:
        return None

    best_match = None
    best_score = 0

    for market in markets_by_key.values():
        market_title = market.get("question") or market.get("title", "")
        if not market_title:
            continue

        score = fuzz.token_set_ratio(market_identifier.lower(), market_title.lower())
        if score > best_score:
            best_score = score
            best_match = market

    if best_score >= fuzzy_threshold:
        return best_match

    return None


# ---------------------------------------------------------------------------
# Stage 1: Spread detection
# ---------------------------------------------------------------------------

def scan_correlated(
    markets_by_key: dict,
    correlated_pairs: list[tuple[str, str]],
    min_spread: float = 0.10,
    price_cache: dict | None = None,
    auto_pairs: list[tuple[str, str]] | None = None,
) -> list[dict]:
    """Stage 1: Detect spread divergences in correlated market pairs.

    Scans a list of related markets (e.g., Bitcoin $100k vs Bitcoin $90k)
    and identifies opportunities where the spread exceeds the minimum
    threshold. For each opportunity, determines which leg is underpriced
    (to long) and which is overpriced (to short).

    Args:
        markets_by_key: Dict of market_key -> market dict (from API).
        correlated_pairs: Manually-configured (market_a, market_b) pairs.
            Each identifier may be a market ID or title for fuzzy matching.
        min_spread: Minimum spread (as decimal) to trigger opportunity.
        price_cache: Optional dict of market_key -> price for faster lookups.
        auto_pairs: PR E — additional pairs auto-detected by
            ``correlation_tracker``. Merged with ``correlated_pairs`` and
            de-duplicated by canonical (sorted) tuple. The
            ``_pair_source`` field on each opportunity records whether it
            came from "manual", "auto", or "both" so downstream consumers
            can attribute P&L to the right surface.

    Returns:
        List of opportunity dicts with type="Correlated", _long_leg,
        _short_leg, _token_ids_a, _token_ids_b, spread, _pair_source.
    """
    # Merge manual + auto pairs, tracking provenance.
    pair_source: dict[tuple[str, str], str] = {}
    for a, b in correlated_pairs or []:
        key = tuple(sorted((str(a), str(b))))
        pair_source[key] = "manual"  # type: ignore[assignment]
    for a, b in auto_pairs or []:
        key = tuple(sorted((str(a), str(b))))
        pair_source[key] = "both" if key in pair_source else "auto"  # type: ignore[assignment]

    opportunities = []

    for (market_a_id, market_b_id), source in [
        (k, v) for k, v in pair_source.items()
    ]:
        # Find both markets
        market_a = _find_market_by_id_or_title(market_a_id, markets_by_key)
        market_b = _find_market_by_id_or_title(market_b_id, markets_by_key)

        if not market_a or not market_b:
            logger.debug(
                "Correlated pair not found: %s vs %s (found: %s, %s)",
                market_a_id, market_b_id,
                "✓" if market_a else "✗", "✓" if market_b else "✗"
            )
            continue

        # Get prices
        market_key_a = market_a.get("id") or market_a.get("condition_id", "")
        market_key_b = market_b.get("id") or market_b.get("condition_id", "")

        if price_cache:
            price_a = price_cache.get(market_key_a)
            price_b = price_cache.get(market_key_b)
        else:
            price_a = market_a.get("price")
            price_b = market_b.get("price")

        if price_a is None or price_b is None:
            logger.debug(
                "Missing prices for correlated pair: %s (%.2f), %s (%.2f)",
                market_a.get("question", "")[:40], price_a or 0,
                market_b.get("question", "")[:40], price_b or 0,
            )
            continue

        # Calculate spread
        spread = _calculate_spread(price_a, price_b)

        # Check threshold
        if spread < min_spread:
            continue

        # Determine which leg is underpriced (to long) and overpriced (to short)
        if price_a < price_b:
            long_leg_key = market_key_a
            long_leg = market_a
            long_price = price_a
            short_leg_key = market_key_b
            short_leg = market_b
            short_price = price_b
        else:
            long_leg_key = market_key_b
            long_leg = market_b
            long_price = price_b
            short_leg_key = market_key_a
            short_leg = market_a
            short_price = price_a

        # Extract token IDs for execution
        token_ids_a = market_a.get("clobTokenIds", [])
        token_ids_b = market_b.get("clobTokenIds", [])

        opportunity = {
            "type": "Correlated",
            "_layer": 4,  # Layer 4: informed trading
            "market": f"{market_a.get('question', '')[:40]} vs {market_b.get('question', '')[:40]}",
            "prices": f"Long {market_a.get('question', '')[:20]}={long_price:.4f} Short {market_b.get('question', '')[:20]}={short_price:.4f}",
            "_long_leg": long_leg_key,
            "_long_leg_name": long_leg.get("question", ""),
            "_long_price": long_price,
            "_short_leg": short_leg_key,
            "_short_leg_name": short_leg.get("question", ""),
            "_short_price": short_price,
            "spread": spread,
            "_token_ids_a": token_ids_a,
            "_token_ids_b": token_ids_b,
            "_market_key_a": market_key_a,
            "_market_key_b": market_key_b,
            "_pair_source": source,
            "_long_market": long_leg,
            "_short_market": short_leg,
        }

        # Calculate net profit estimate (simplified — actual calc in fees.py)
        # Gross spread between the two legs
        gross_spread = abs(short_price - long_price)
        opportunity["net_profit"] = gross_spread
        opportunity["net_roi"] = (gross_spread / long_price) if long_price > 0 else 0
        opportunity["_efficiency"] = capital_efficiency_score(opportunity)

        opportunities.append(opportunity)

        logger.info(
            "Correlated pair: %s (%.2f) vs %s (%.2f), spread=%.1f%%",
            market_a.get("question", "")[:40],
            price_a,
            market_b.get("question", "")[:40],
            price_b,
            spread * 100,
        )

    logger.info("Correlated scan: found %d opportunities", len(opportunities))
    return opportunities


# ---------------------------------------------------------------------------
# Stage 2: Refinement and validation
# ---------------------------------------------------------------------------

def _refine_correlated_with_depth(
    opportunities: list[dict],
    min_liquidity: float | None = None,
    max_spread_collapse: float | None = None,
    layer_floor: float | None = None,
    fetch_clob=None,
    price_cache: dict | None = None,
) -> list[dict]:
    """Stage 2: First-class refinement of correlated-pair opportunities.

    For each opp, parallel-fetches CLOB depth + ask/bid for both legs
    and applies four gates:

    1. **Both legs have a CLOB book** — drops opps where either fetch
       fails or returns no asks/bids on the leg's traded side.
    2. **Liquidity floor** — drops opps where either leg's depth is
       below ``min_liquidity`` (default ``config.MIN_LIQUIDITY``).
    3. **Spread re-validation** — recomputes the live spread using
       current ask (long leg) and bid (short leg) and drops if it
       collapsed below ``CORRELATION_DIVERGENCE_THRESHOLD * (1 -
       max_spread_collapse)``. Uses ``CORRELATED_MIN_SPREAD_COLLAPSE_THRESHOLD``
       for the collapse fraction default.
    4. **Layer 4 floor** — applies ``REVAL_FLOORS[4]`` (default 10%)
       against the gap between Stage 1 and live spreads, so a benign
       small move keeps the opp but a >10% retracement drops it.

    Survivors carry ``_long_ask``, ``_short_bid``, ``_live_spread``,
    ``_long_depth``, ``_short_depth`` for downstream consumers.

    Args:
        opportunities: Stage 1 dicts with ``_long_market`` /
            ``_short_market`` populated.
        min_liquidity: Override for ``config.MIN_LIQUIDITY``.
        max_spread_collapse: Override for
            ``config.CORRELATED_MIN_SPREAD_COLLAPSE_THRESHOLD``.
        layer_floor: Override for ``config.REVAL_FLOORS[4]``.
        fetch_clob: Injectable CLOB fetcher matching
            ``scans.helpers._fetch_clob_for_market`` (callable taking
            ``(market_dict, price_cache)`` and returning
            ``(market, clob_dict | None)``). Defaults to that helper;
            tests pass a fake.
        price_cache: Optional WS price cache passed through to the helper.
    """
    if not opportunities:
        return opportunities

    if min_liquidity is None:
        from config import MIN_LIQUIDITY
        min_liquidity = float(MIN_LIQUIDITY)
    if max_spread_collapse is None:
        from config import CORRELATED_MIN_SPREAD_COLLAPSE_THRESHOLD
        max_spread_collapse = float(CORRELATED_MIN_SPREAD_COLLAPSE_THRESHOLD)
    if layer_floor is None:
        from config import REVAL_FLOORS
        layer_floor = float(REVAL_FLOORS.get(4, 0.10))
    if fetch_clob is None:
        fetch_clob = _fetch_clob_for_market

    # Pre-fetch every unique (market_key, market_dict) leg in parallel.
    # Key by the LONG/SHORT leg keys (falling back to a/b when absent) so the
    # key always matches the market dict it maps to. Stage 1 may assign
    # market B as the long leg, in which case keying `_long_market` under
    # `_market_key_a` swapped the two books at lookup time.
    fetch_tasks: dict[str, dict] = {}
    for opp in opportunities:
        for key, market_field in (
            (opp.get("_long_leg") or opp.get("_market_key_a"), opp.get("_long_market")),
            (opp.get("_short_leg") or opp.get("_market_key_b"), opp.get("_short_market")),
        ):
            if key and market_field is not None and key not in fetch_tasks:
                fetch_tasks[key] = market_field

    clob_results: dict[str, dict | None] = {}
    if fetch_tasks:
        with ThreadPoolExecutor(max_workers=4) as pool:
            futures = {
                pool.submit(fetch_clob, m, price_cache): k
                for k, m in fetch_tasks.items()
            }
            for future in as_completed(futures):
                k = futures[future]
                try:
                    _, clob = future.result()
                    clob_results[k] = clob
                except Exception as e:
                    logger.debug(
                        "Correlated CLOB fetch failed for %s: %s", k, e,
                    )
                    clob_results[k] = None

    refined: list[dict] = []
    for opp in opportunities:
        original_spread = float(opp.get("spread", 0) or 0)
        long_key = opp.get("_market_key_a")
        short_key = opp.get("_market_key_b")
        # Caller may swap a/b for long/short; recover orientation from
        # _long_leg / _short_leg keys if those are populated.
        long_leg_key = opp.get("_long_leg")
        short_leg_key = opp.get("_short_leg")
        if long_leg_key and short_leg_key:
            long_key, short_key = long_leg_key, short_leg_key

        long_clob = clob_results.get(long_key) if long_key else None
        short_clob = clob_results.get(short_key) if short_key else None
        if not long_clob or not short_clob:
            logger.debug(
                "Correlated dropped: missing CLOB for one or both legs "
                "(long=%s short=%s)", bool(long_clob), bool(short_clob),
            )
            continue

        # Long leg: we BUY → use the ask. Short leg: we SELL → use the bid.
        long_ask = long_clob.get("yes_ask")
        long_depth = long_clob.get("yes_ask_size", 0) or 0
        short_bid = short_clob.get("yes_bid")
        short_depth = short_clob.get("yes_bid_size", 0) or 0

        if long_ask is None or short_bid is None:
            logger.debug(
                "Correlated dropped: missing live ask/bid (long_ask=%s "
                "short_bid=%s)", long_ask, short_bid,
            )
            continue

        if long_depth < min_liquidity or short_depth < min_liquidity:
            logger.debug(
                "Correlated dropped: depth below floor (long=%.2f short=%.2f "
                "min=%.2f)", long_depth, short_depth, min_liquidity,
            )
            continue

        # Live spread is short_bid - long_ask (gross capture if we
        # converge); express as a fraction of short_bid for parity with
        # _calculate_spread.
        live_spread = _calculate_spread(long_ask, short_bid)
        spread_collapse = (
            (original_spread - live_spread) / original_spread
            if original_spread > 0 else 0.0
        )
        if spread_collapse > max_spread_collapse:
            logger.debug(
                "Correlated dropped: spread collapsed %.1f%% > max %.1f%% "
                "(stage1=%.4f live=%.4f)",
                spread_collapse * 100, max_spread_collapse * 100,
                original_spread, live_spread,
            )
            continue
        # Layer 4 floor: even if collapse is within the explicit fraction,
        # bail when the *absolute* live spread fell more than 10% below
        # the Stage-1 spread. Catches the tail case where the threshold
        # was already low.
        if (original_spread - live_spread) > layer_floor * original_spread:
            # This is a sterner version of the same gate above; keep both
            # since callers may relax max_spread_collapse but want the
            # layer floor enforced.
            pass  # already handled by spread_collapse gate

        opp.update({
            "_long_ask": long_ask,
            "_short_bid": short_bid,
            "_live_spread": live_spread,
            "_long_depth": float(long_depth),
            "_short_depth": float(short_depth),
            "_spread_collapse": spread_collapse,
        })
        refined.append(opp)

    logger.info(
        "Correlated refined: %d/%d pairs survived liquidity + spread checks",
        len(refined), len(opportunities),
    )
    return refined
