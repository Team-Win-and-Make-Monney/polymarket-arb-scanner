"""Earnings-mention NO-harvest out-of-sample (OOS) logger — detection only.

Re-runs the round-2 earnings-mention pilot out-of-sample on newly-settled Kalshi
company-KPI / earnings-mention markets to confirm the in-sample finding that
11-50c YES contracts are systematically ~10pts too rich at T-24h (R2-1: +10.3pts,
z=2.41, n=240). This module is a deterministic statistical logger: it finds
markets that have ALREADY settled, reconstructs each one's YES price at T-24h
before close from historical candlesticks, joins that to the realized
settlement, and computes a richness z-score. It places NO orders and involves
NO LLM in any path. See docs/plans/08-earnings-mention-oos.md.

Method note (redesign, 2026-07-05): v1 tried to snapshot markets LIVE while
they sat inside their open [close-24h, close-6h] window. That cannot reliably
catch a market whose entire lifetime falls between two weekly cron runs (e.g.
one that opens Tuesday and settles before the next Sunday run never gets
snapshotted at all) — a real coverage gap, not just a timing nuance. The
in-sample pilot never had this problem because it worked backwards from
settled markets via candlestick reconstruction (T1-pm-dispersion-novelty.md
§a: "T-24h/T-6h candle reconstruction"), which needs nothing to happen while
the market is still open. This module now does the same: look at what has
SETTLED since the last watermark, then fetch each one's T-24h price
after the fact. There is no "pending" snapshot state as a result — a market
is only ever touched once, after it has already resolved.

The client is duck-typed (any object exposing ``fetch_candlesticks``) so the
core logic is unit-testable without network, keys, or the live KalshiClient.
``fetch_settled_markets`` (the discovery step) is called directly by the
runner (scripts/run_earnings_mention_oos.py), which also owns the
watermark/seen-ticker bookkeeping — this module stays limited to the parts
that are pure or that need network only to answer one question (what was the
YES price at T-24h for this specific, already-settled market).
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from statistics import fmean, stdev
from typing import Iterable, Protocol

logger = logging.getLogger(__name__)

# --- pre-registered constants (do not move without re-registering the gate) ---
BAND_LO = 0.11          # YES price band lower bound (R2-1: 11-50c)
BAND_HI = 0.50          # YES price band upper bound
PURSUE_GAP_PTS = 9.0    # OOS richness gap (points) to PURSUE
KILL_GAP_PTS = 4.5      # OOS richness gap below which to KILL
MIN_N = 100             # min OOS contracts before a terminal verdict
PURSUE_Z = 2.0          # min z-score to PURSUE

# Candlestick sample window for T-24h reconstruction: a 3h window at hourly
# resolution around the T-24h mark (not a single instant), same as the
# validated command-center pull (scripts/longshot_fade_pull.py phase3) —
# guards against a gap in the candle series landing exactly on T-24h.
CANDLE_WINDOW_START_H = 26.0   # window opens at close-26h
CANDLE_WINDOW_END_H = 23.0     # window closes at close-23h
CANDLE_PERIOD_MIN = 60         # hourly candles

# Series-ticker prefixes for the company-KPI / earnings-mention family this
# pilot targets. KXEARNINGSMENTION confirmed directly against both the T1
# brief ("145 earnings-call mention series: KXEARNINGSMENTION{TSLA,NVDA,
# META,COST,PLTR,...}") and the in-sample pilot's actual market list
# (t1-kalshi-company-pilot.json's earnings_mention_series: KXEARNINGSMENTIONLOW,
# KXEARNINGSMENTIONBA, KXEARNINGSMENTIONKKR, KXEARNINGSMENTIONDPZ, ...).
#
# The broader KPI-bracket family the spec also mentions (e.g. KXTESLAPROD)
# is deliberately NOT included: enumerating it correctly needs the full
# in-sample pilot market list (1,144 markets swept), which lives in the
# command-center repo this pipeline does not read from, and a wrong or
# incomplete guess risks exactly the contamination this scoping exists to
# prevent. A narrower, verified-clean sample beats a broader, possibly-wrong
# one for a statistic that IS the 8/3 decision input. Extending
# SERIES_PREFIXES to the KPI-bracket family is a follow-up once that list is
# available to whatever process maintains this file.
SERIES_PREFIXES: tuple[str, ...] = ("KXEARNINGSMENTION",)


class _MarketClient(Protocol):
    def fetch_candlesticks(self, series_ticker: str, ticker: str, start_ts: int, end_ts: int,
                           *a, **k) -> list[dict] | None: ...


@dataclass(frozen=True)
class Resolved:
    """A snapshot joined to its realized binary settlement outcome."""
    ticker: str
    yes_price: float
    outcome: float  # 1.0 if settled YES, 0.0 if settled NO
    series: str


@dataclass(frozen=True)
class OosStats:
    """Out-of-sample richness statistics over the 11-50c YES band."""
    n: int
    mean_richness_pts: float          # mean(yes_price - outcome) * 100, in points
    z: float
    by_category: dict[str, tuple[int, float]] = field(default_factory=dict)


# --------------------------------------------------------------------------- #
# Pure helpers
# --------------------------------------------------------------------------- #
def _parse_ts(value: str | None) -> datetime | None:
    """Parse a Kalshi ISO-8601 timestamp to a tz-aware UTC datetime, or None."""
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        logger.debug("Failed to parse timestamp %r", value)
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _series_of(market: dict) -> str:
    return str(
        market.get("series_ticker")
        or market.get("event_ticker")
        or market.get("ticker", "")
    )


def classify_market(market: dict) -> bool:
    """True if *market* belongs to the company-KPI / earnings-mention family.

    Matches ONLY the confirmed series-ticker prefix(es) in SERIES_PREFIXES —
    NOT a title/text pattern. An earlier version also OR'd in a loose title
    regex (bare words like "say"/"mention" matched against ANY market's
    title), which let unrelated markets like "Will Trump say recession?"
    pass and contaminate the OOS sample — a false positive here directly
    corrupts the statistic this whole pipeline exists to compute, which is
    worse than a false negative (that only costs sample size). Series-ticker
    matching is precise by construction: it's Kalshi's own naming, not prose.
    """
    series = _series_of(market)
    return series.upper().startswith(tuple(p.upper() for p in SERIES_PREFIXES))


def _close_time(market: dict) -> datetime | None:
    return _parse_ts(market.get("close_time") or market.get("expiration_time"))


def has_valid_result(market: dict) -> bool:
    """True if *market* carries a definitive yes/no settlement result.

    A settled-but-voided/undecided market (result is empty or some other
    sentinel) will never carry a scoreable outcome — the caller should still
    mark such a ticker as seen (it will never resolve differently later) but
    must not feed it into compute_oos_stats.
    """
    return str(market.get("result", "")).lower() in ("yes", "no")


def _series_ticker_for_candles(market: dict) -> str:
    """Series ticker for the /series/{s}/markets/{t}/candlesticks path.

    Prefers the market's own ``series_ticker`` field; falls back to the
    ``event_ticker``'s prefix before the first hyphen (series tickers never
    contain one — e.g. ``KXEARNINGSMENTIONBA-26Q2ER`` -> ``KXEARNINGSMENTIONBA``,
    the same derivation scripts/longshot_fade_pull.py uses), since passing
    the wrong string 404s the candlestick endpoint outright.
    """
    explicit = market.get("series_ticker")
    if explicit:
        return str(explicit)
    event_ticker = str(market.get("event_ticker", ""))
    return event_ticker.split("-")[0] if event_ticker else ""


def _candle_dollars(block: dict | None, *keys: str) -> float | None:
    """Read the first present ``*_dollars`` string field as a float.

    Candlestick price fields are dollar strings (e.g. ``close_dollars=
    "0.2200"``), NOT bare cents — confirmed gotcha from the in-sample pilot
    (T1-pm-dispersion-novelty.md methodology note).
    """
    for key in keys:
        value = (block or {}).get(key)
        if value is None:
            continue
        try:
            return float(value)
        except (TypeError, ValueError):
            continue
    return None


def _candle_yes_price(candle: dict) -> float | None:
    """Extract a YES price (in $0-1 terms) from one candlestick.

    Prefers the trade-price block's close/mean; falls back to the midpoint
    of the yes_bid/yes_ask close if no trades occurred in the candle.
    """
    price = _candle_dollars(candle.get("price"), "close_dollars", "mean_dollars")
    if price is not None:
        return price
    bid = _candle_dollars(candle.get("yes_bid"), "close_dollars")
    ask = _candle_dollars(candle.get("yes_ask"), "close_dollars")
    if bid is not None and ask is not None:
        return (bid + ask) / 2.0
    return None


# --------------------------------------------------------------------------- #
# T-24h price reconstruction (replaces the old live-snapshot window)
# --------------------------------------------------------------------------- #
def price_at_t24h(client: _MarketClient, market: dict) -> float | None:
    """Reconstruct *market*'s YES price at T-24h before close via candlesticks.

    Samples the [close-26h, close-23h] window at hourly resolution and reads
    the last candle in it (closest to T-24h) — the same method the in-sample
    pilot used, so this needs nothing to have happened while the market was
    still open.

    Raises:
        RuntimeError: if fetch_candlesticks signals the request itself
            failed (returns None) — transient, caller should retry.

    Returns:
        The reconstructed YES price, or None if the request succeeded (or
        wasn't attempted because close_time/ticker/series couldn't be
        determined) but there is definitively no usable price — e.g. a
        market whose entire lifetime was shorter than the T-24h lookback
        window (a genuinely empty candle list from a successful request) or
        an unusable candle. This is PERMANENT: retrying will never produce
        different data, unlike the transient RuntimeError path.
    """
    close = _close_time(market)
    ticker = str(market.get("ticker", ""))
    series = _series_ticker_for_candles(market)
    if close is None or not ticker or not series:
        return None  # permanent -- can never be fetched without these fields
    start_ts = int((close - timedelta(hours=CANDLE_WINDOW_START_H)).timestamp())
    end_ts = int((close - timedelta(hours=CANDLE_WINDOW_END_H)).timestamp())
    candles = client.fetch_candlesticks(series, ticker, start_ts, end_ts, CANDLE_PERIOD_MIN)
    if candles is None:
        raise RuntimeError(f"candlestick fetch failed for {ticker}")
    if not candles:
        return None  # permanent -- request succeeded, genuinely no data in window
    return _candle_yes_price(candles[-1])


def build_resolved(market: dict, yes_price_t24h: float) -> Resolved:
    """Build a Resolved record from an already-settled market plus its
    reconstructed T-24h YES price. Caller must have already confirmed
    has_valid_result(market) is True."""
    return Resolved(
        ticker=str(market.get("ticker", "")),
        yes_price=yes_price_t24h,
        outcome=1.0 if str(market.get("result", "")).lower() == "yes" else 0.0,
        series=_series_of(market),
    )


# --------------------------------------------------------------------------- #
# Stage 3 — OOS richness statistics + verdict
# --------------------------------------------------------------------------- #
def _zscore(richness: list[float]) -> float:
    n = len(richness)
    if n < 2:
        return 0.0
    sd = stdev(richness)
    if sd == 0:
        return 0.0
    return fmean(richness) / (sd / math.sqrt(n))


def compute_oos_stats(resolved: Iterable[Resolved]) -> OosStats:
    """Mean YES richness and z-score over the 11-50c band, plus by-category."""
    band = [r for r in resolved if BAND_LO <= r.yes_price <= BAND_HI]
    richness = [r.yes_price - r.outcome for r in band]
    n = len(richness)
    mean_pts = (fmean(richness) * 100.0) if n else 0.0
    z = _zscore(richness)

    by_category: dict[str, list[float]] = {}
    for r in band:
        by_category.setdefault(r.series, []).append(r.yes_price - r.outcome)
    cat_stats = {
        series: (len(vals), round(fmean(vals) * 100.0, 2))
        for series, vals in by_category.items()
    }
    return OosStats(
        n=n,
        mean_richness_pts=mean_pts,
        z=z,
        by_category=cat_stats,
    )


def verdict(stats: OosStats) -> str:
    """Pre-registered gate toward the 8/3 decision review (no discretionary override).

    'pursue' if mean_richness_pts >= PURSUE_GAP_PTS (9.0) and z >= PURSUE_Z (2.0),
    provided n >= MIN_N (100). 'kill' if mean_richness_pts < KILL_GAP_PTS (4.5).
    Otherwise 'continue' — including whenever n < MIN_N, which is never terminal.
    """
    if stats.n < MIN_N:
        return "continue"
    if stats.mean_richness_pts >= PURSUE_GAP_PTS and stats.z >= PURSUE_Z:
        return "pursue"
    if stats.mean_richness_pts < KILL_GAP_PTS:
        return "kill"
    return "continue"
