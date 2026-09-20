"""Kalshi ticker parsing and temporal relation discovery.

Parses structured Kalshi ticker symbols (e.g. KXBTC-26FEB07-T101999.99), classifies
cumulative ('by-deadline') vs at-expiry range markets, and discovers nested
temporal pairs (D_early < D_late) where monotonicity bounds apply:
    P(Event by D_early) <= P(Event by D_late)
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Month Mapping & Regex Patterns
# ---------------------------------------------------------------------------

_MONTHS: dict[str, int] = {
    "JAN": 1, "FEB": 2, "MAR": 3, "APR": 4, "MAY": 5, "JUN": 6,
    "JUL": 7, "AUG": 8, "SEP": 9, "OCT": 10, "NOV": 11, "DEC": 12,
}

# Regex matching structured Kalshi strike tickers:
# e.g. KXBTC-26FEB07-T101999.99 or KXBTC-26APR2717-T87749.99
# Groups: 1=series/asset, 2=YY, 3=MMM, 4=DD, 5=optional HH, 6=T/B, 7=strike
_KALSHI_TICKER_RE = re.compile(
    r"^([A-Z0-9]+?)-([0-9]{2})([A-Z]{3})([0-9]{2})([0-9]{2})?-([TB])([0-9]+(?:\.[0-9]+)?)$",
    re.IGNORECASE,
)


def parse_kalshi_ticker(ticker: str) -> dict | None:
    """Parse structured Kalshi ticker symbol into component metadata.

    Args:
        ticker: Kalshi ticker string, e.g. 'KXBTC-26FEB07-T101999.99'

    Returns:
        Dict with keys: series, asset, datetime, strike, direction, type_code, ticker
        or None if pattern does not match or date is invalid.
    """
    if not ticker or not isinstance(ticker, str):
        return None

    m = _KALSHI_TICKER_RE.match(ticker.strip())
    if not m:
        return None

    series, yy_str, mon_str, dd_str, hh_str, type_code, strike_str = m.groups()
    mon_key = mon_str.upper()
    if mon_key not in _MONTHS:
        return None

    try:
        year = 2000 + int(yy_str)
        month = _MONTHS[mon_key]
        day = int(dd_str)
        hour = int(hh_str) if hh_str is not None else 0
        dt = datetime(year, month, day, hour, tzinfo=timezone.utc)
        strike = float(strike_str)
    except (ValueError, TypeError):
        return None

    series_upper = series.upper()
    asset = series_upper[2:] if series_upper.startswith("KX") and len(series_upper) > 2 else series_upper
    direction = "above" if type_code.upper() == "T" else "below"

    return {
        "series": series_upper,
        "asset": asset,
        "datetime": dt,
        "strike": strike,
        "direction": direction,
        "type_code": type_code.upper(),
        "ticker": ticker.strip(),
    }


def is_cumulative(market: dict) -> bool:
    """Classify whether a market is a cumulative 'by-deadline' contract.

    Monotonicity holds across dates ONLY for cumulative events ('will hit by date D').
    Range markets ('between X and Y') or point-in-time expiry contracts
    ('price at 5pm on date D') are non-monotonic and must be excluded.

    Args:
        market: Market dictionary containing title, question, subtitle, etc.

    Returns:
        True if the market has cumulative threshold semantics, False otherwise.
    """
    title = (market.get("title") or market.get("question") or "").lower()
    subtitle = (market.get("subtitle") or "").lower()
    yes_sub = (market.get("yes_sub_title") or "").lower()
    combined = f"{title} {subtitle}".strip()

    if not combined:
        return False

    # Negative exclusions: range markets or point-in-time snapshots
    exclusions = [
        "between",
        "close at",
        "at expiry",
        "price at ",
        "settle at",
        "closing price",
    ]
    if any(ex in combined for ex in exclusions):
        return False

    # Check for range indicator in subtitle (e.g. 90,000 - 100,000)
    if " - " in yes_sub or ("-" in yes_sub and not yes_sub.startswith("-")):
        return False

    # Positive cumulative triggers
    triggers = [
        "by ",
        "before ",
        "reach",
        "touch",
        "hit ",
    ]
    if not any(trig in combined for trig in triggers):
        return False

    return True


def find_temporal_pairs(markets: list[dict] | dict[str, dict]) -> list[dict]:
    """Find nested temporal pairs (D_early < D_late) for identical asset/strike/direction.

    Args:
        markets: List or dictionary of Kalshi market dictionaries.

    Returns:
        List of candidate temporal pair dicts containing early (sub) and late (sup) markets.
    """
    if isinstance(markets, dict):
        market_list = list(markets.values())
    else:
        market_list = list(markets or [])

    parsed_markets: list[tuple[dict, dict]] = []
    for mkt in market_list:
        if not is_cumulative(mkt):
            continue
        ticker = mkt.get("ticker", "")
        parsed = parse_kalshi_ticker(ticker)
        if parsed is None:
            continue
        parsed_markets.append((mkt, parsed))

    # Group by (asset, strike, direction)
    groups: dict[tuple[str, float, str], list[tuple[dict, dict]]] = {}
    for mkt, parsed in parsed_markets:
        key = (parsed["asset"], parsed["strike"], parsed["direction"])
        groups.setdefault(key, []).append((mkt, parsed))

    pairs: list[dict] = []
    for (asset, strike, direction), group_items in groups.items():
        if len(group_items) < 2:
            continue

        # Sort by datetime ascending
        sorted_items = sorted(group_items, key=lambda x: x[1]["datetime"])

        # Compare pairs (early, late)
        for i in range(len(sorted_items)):
            early_mkt, early_parsed = sorted_items[i]
            dt_early = early_parsed["datetime"]
            for j in range(i + 1, len(sorted_items)):
                late_mkt, late_parsed = sorted_items[j]
                dt_late = late_parsed["datetime"]
                if dt_early < dt_late:
                    pairs.append({
                        "sub": early_mkt,
                        "sup": late_mkt,
                        "early_ticker": early_parsed["ticker"],
                        "late_ticker": late_parsed["ticker"],
                        "asset": asset,
                        "strike": strike,
                        "direction": direction,
                        "dt_early": dt_early,
                        "dt_late": dt_late,
                        "platform": "kalshi",
                    })

    return pairs
