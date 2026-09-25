"""Pair relations discovery for logical and conditional arbitrage.

Identifies subset / implication relationships (A ⊆ B) across prediction markets
so that coherence bounds (P(A) ≤ P(B)) can be scanned and enforced.

Supports:
1. Structured numeric threshold parsing (e.g. BTC > $100k ⊆ BTC > $90k).
2. Kalshi structured strike tickers (e.g. KXBTC-26DEC31-T100000 ⊆ KXBTC-26DEC31-T90000).
3. Curated manual implication rules.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Threshold Data Structures & Regex Patterns
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ThresholdSpec:
    """Parsed threshold event specification."""
    underlying: str
    direction: str  # "above" (>=, >, over) or "below" (<=, <, under)
    strike: float
    date_key: str
    platform: str
    market: dict
    # Polymarket event id: strike ladders are one event, and only rungs of the same
    # ladder share resolution rules. "" when unknown (Kalshi tickers, synthetic data).
    event_key: str = ""


# Kalshi ticker pattern: e.g. KXBTC-26DEC31-T100000 or KXINFL-26DEC-B2.5
_KALSHI_TICKER_RE = re.compile(
    r"^([A-Z0-9]+)-([0-9]{2}[A-Z]{3}[0-9]{0,4})-([TB])([0-9]+(?:\.[0-9]+)?)$",
    re.IGNORECASE,
)

# Magnitude units that may follow a number. The unit must sit directly after the
# number and end on a word boundary ("$1T", "$800B", "$2.5 million"), never a letter
# that merely starts the next word ("$100 by March").
_UNIT_MULTIPLIERS = {
    "k": 1e3, "thousand": 1e3,
    "m": 1e6, "million": 1e6,
    "b": 1e9, "bn": 1e9, "billion": 1e9,
    "t": 1e12, "tn": 1e12, "trillion": 1e12,
}
_UNIT_ALT = "thousand|million|billion|trillion|bn|tn|k|m|b|t"
_NUM = r"([0-9]+(?:,[0-9]{3})*(?:\.[0-9]+)?)"
_UNIT = r"(?:\s*(" + _UNIT_ALT + r")\b)?"

# Text threshold patterns for title parsing: groups are (underlying, number, unit, rest)
_ABOVE_PATTERNS = [
    re.compile(r"(.+?)\s+(?:above|over|greater than|at or above|≥|\>\=?)\s*\$?" + _NUM + _UNIT + r"\s*(.*)", re.IGNORECASE),
    re.compile(r"(.+?)\s+\$?" + _NUM + _UNIT + r"\s*\+\s*(.*)", re.IGNORECASE),
]

_BELOW_PATTERNS = [
    re.compile(r"(.+?)\s+(?:below|under|less than|at or below|≤|\<\=?)\s*\$?" + _NUM + _UNIT + r"\s*(.*)", re.IGNORECASE),
]


def _normalize_num(num_str: str, full_match_str: str = "") -> float | None:
    """Parse a number, applying the k/m/b/t (or word) unit written directly after it.

    ``full_match_str`` is the text the number came from. Only a unit immediately
    following this exact number counts; a digit or decimal just before it (``11``
    for ``1``) or a longer word after it (``by`` for ``b``) does not.
    """
    raw = num_str.strip()
    clean = raw.replace(",", "")
    try:
        val = float(clean)
    except ValueError:
        return None

    if full_match_str:
        unit = re.search(
            r"(?<![0-9.,])" + re.escape(raw.lower()) + r"\s*(" + _UNIT_ALT + r")\b",
            full_match_str.lower(),
        )
        if unit:
            val *= _UNIT_MULTIPLIERS[unit.group(1)]
    return val


def _event_key(market: dict) -> str:
    """Stable id of the market's parent event, or "" when the market carries none."""
    events = market.get("events")
    if isinstance(events, list) and events and isinstance(events[0], dict):
        return str(events[0].get("id") or events[0].get("slug") or "")
    return str(market.get("event_id") or market.get("eventSlug") or "")


def _normalize_underlying(raw: str) -> str:
    """Normalize underlying asset/event text for grouping."""
    s = raw.lower().strip()
    # Strip common prefixes
    for prefix in ("will ", "is ", "the ", "price of ", "market close: "):
        if s.startswith(prefix):
            s = s[len(prefix):].strip()
    # Strip common verb suffixes before comparison operators
    for suffix in (" be", " hit", " reach", " trade", " close", " stay", " drop", " go"):
        if s.endswith(suffix):
            s = s[:-len(suffix)].strip()
    # Standardize crypto aliases
    if s in ("btc", "bitcoin"):
        return "btc"
    if s in ("eth", "ethereum"):
        return "eth"
    if s in ("sol", "solana"):
        return "sol"
    return re.sub(r"\s+", " ", re.sub(r"[^\w\s]", "", s))


def parse_threshold_market(market: dict, platform: str = "polymarket") -> ThresholdSpec | None:
    """Extract structured threshold metadata from a market dictionary.

    Supports Kalshi structured tickers and Polymarket / generic title patterns.
    """
    ticker = market.get("ticker", "")
    title = market.get("title") or market.get("question") or market.get("description", "")
    end_date = (
        market.get("end_date_iso")
        or market.get("endDateIso")
        or market.get("endDate")
        or market.get("close_time")
        or market.get("expiration_time")
        or ""
    )
    date_key = str(end_date)[:10] if end_date else "unknown_date"

    # 1. Try Kalshi structured ticker
    if ticker:
        m = _KALSHI_TICKER_RE.match(ticker.strip())
        if m:
            series, date_str, type_code, strike_str = m.groups()
            direction = "above" if type_code.upper() == "T" else "below"
            try:
                strike_val = float(strike_str)
                return ThresholdSpec(
                    underlying=_normalize_underlying(series),
                    direction=direction,
                    strike=strike_val,
                    date_key=date_str.upper(),
                    platform="kalshi",
                    market=market,
                )
            except ValueError:
                pass

    if not title:
        return None

    # 2. Try title patterns for "above" / "over"
    for pat in _ABOVE_PATTERNS:
        match = pat.search(title)
        if match:
            groups = match.groups()
            raw_underlying = groups[0]
            num_str = groups[1]
            strike_val = _normalize_num(num_str, num_str + (groups[2] or ""))
            if strike_val is not None:
                return ThresholdSpec(
                    underlying=_normalize_underlying(raw_underlying),
                    direction="above",
                    strike=strike_val,
                    date_key=date_key,
                    platform=platform,
                    market=market,
                    event_key=_event_key(market),
                )

    # 3. Try title patterns for "below" / "under"
    for pat in _BELOW_PATTERNS:
        match = pat.search(title)
        if match:
            groups = match.groups()
            raw_underlying = groups[0]
            num_str = groups[1]
            strike_val = _normalize_num(num_str, num_str + (groups[2] or ""))
            if strike_val is not None:
                return ThresholdSpec(
                    underlying=_normalize_underlying(raw_underlying),
                    direction="below",
                    strike=strike_val,
                    date_key=date_key,
                    platform=platform,
                    market=market,
                    event_key=_event_key(market),
                )

    return None


# ---------------------------------------------------------------------------
# Discovery Engine
# ---------------------------------------------------------------------------

def discover_subset_pairs(
    markets: list[dict] | dict[str, dict],
    manual_rules: list[dict] | None = None,
    same_platform_only: bool = True,
    platform: str = "polymarket",
) -> list[dict]:
    """Discover pairs (sub, sup) where event sub ⊆ event sup (A implies B).

    Coherence condition: P(sub) <= P(sup).
    If P(sub) > P(sup), an arbitrage lock is possible by buying YES on sup and NO on sub.

    Returns:
        List of dicts:
            {
                "sub": market_dict,        # Event A (subset)
                "sup": market_dict,        # Event B (superset)
                "source": "strike" | "manual",
                "confidence": 1.0 | float,
                "platform": str,
            }
    """
    if isinstance(markets, dict):
        market_list = list(markets.values())
        markets_by_id = markets
    else:
        market_list = markets
        markets_by_id = {}
        for m in market_list:
            cid = m.get("condition_id") or m.get("id") or m.get("ticker")
            if cid:
                markets_by_id[str(cid)] = m

    pairs: list[dict] = []

    # 1. Parse threshold specs
    grouped: dict[tuple[str, str, str, str, str], list[ThresholdSpec]] = {}
    for mkt in market_list:
        spec = parse_threshold_market(mkt, platform=platform)
        if spec:
            if spec.platform == "polymarket" and not spec.event_key:
                continue
            key = (spec.underlying, spec.date_key, spec.direction, spec.platform, spec.event_key)
            grouped.setdefault(key, []).append(spec)

    # 2. Derive threshold implication pairs
    for (underlying, date_key, direction, mkt_platform, _event), specs in grouped.items():
        if len(specs) < 2:
            continue

        # Sort by strike ascending
        specs.sort(key=lambda s: s.strike)

        if direction == "above":
            # For "above" / >= : higher strike ⊆ lower strike
            # Example: BTC >= $100k implies BTC >= $90k
            for i in range(len(specs)):
                for j in range(i + 1, len(specs)):
                    sup_spec = specs[i]  # Lower strike -> easier -> Superset (B)
                    sub_spec = specs[j]  # Higher strike -> harder -> Subset (A)
                    if sub_spec.strike > sup_spec.strike:
                        pairs.append({
                            "sub": sub_spec.market,
                            "sup": sup_spec.market,
                            "source": "strike",
                            "confidence": 1.0,
                            "platform": mkt_platform,
                            "_sub_strike": sub_spec.strike,
                            "_sup_strike": sup_spec.strike,
                            "_direction": direction,
                        })
        elif direction == "below":
            # For "below" / <= : lower strike ⊆ higher strike
            # Example: Inflation <= 2% implies Inflation <= 3%
            for i in range(len(specs)):
                for j in range(i + 1, len(specs)):
                    sub_spec = specs[i]  # Lower strike -> harder -> Subset (A)
                    sup_spec = specs[j]  # Higher strike -> easier -> Superset (B)
                    if sup_spec.strike > sub_spec.strike:
                        pairs.append({
                            "sub": sub_spec.market,
                            "sup": sup_spec.market,
                            "source": "strike",
                            "confidence": 1.0,
                            "platform": mkt_platform,
                            "_sub_strike": sub_spec.strike,
                            "_sup_strike": sup_spec.strike,
                            "_direction": direction,
                        })

    # 3. Process curated manual rules
    if manual_rules:
        for rule in manual_rules:
            sub_id = str(rule.get("subset") or rule.get("sub") or rule.get("if_id") or "")
            sup_id = str(rule.get("superset") or rule.get("sup") or rule.get("then_id") or "")
            confidence = float(rule.get("confidence", 0.95))

            sub_mkt = markets_by_id.get(sub_id)
            sup_mkt = markets_by_id.get(sup_id)
            if sub_mkt and sup_mkt:
                sub_plat = sub_mkt.get("platform", platform)
                sup_plat = sup_mkt.get("platform", platform)
                if same_platform_only and sub_plat != sup_plat:
                    continue
                pairs.append({
                    "sub": sub_mkt,
                    "sup": sup_mkt,
                    "source": "manual",
                    "confidence": confidence,
                    "platform": sub_plat,
                })

    return pairs
