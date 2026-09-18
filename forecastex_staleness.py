"""ForecastEx-vs-Kalshi staleness watcher — detection core (PM-lane watcher).

ForecastEx (IBKR's CFTC event-contract venue) is much thinner than Kalshi, so its
quotes can go stale while the matched Kalshi contract reprices. This flags matched
pairs where, against a FRESH Kalshi reference, the ForecastEx quote is BOTH stale
(no update within ``max_age_s``) AND diverges by >= ``min_divergence`` — a
candidate stale-quote on the thin venue for a human / the executor to check
(ForecastEx via IBKR is allowlisted; this is detection only — the live fetch +
contract pairing is a Phase-2 runner).

The three conditions matter together:
  * Kalshi fresh  — without a live reference there's nothing to compare against;
  * ForecastEx stale — a fresh divergence is just a normal cross-venue spread
    (the arb scanner's job), not staleness;
  * divergence >= threshold — a stale quote that still agrees with Kalshi isn't
    actionable.

Pure + deterministic; tested with synthetic snapshots.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class VenueQuote:
    venue: str           # "kalshi" | "forecastex"
    contract_id: str
    price: float         # implied probability in [0, 1]
    updated_at: float    # unix seconds of the last quote update


@dataclass(frozen=True)
class MatchedPair:
    label: str           # human label for the matched event/contract
    kalshi: VenueQuote
    forecastex: VenueQuote


@dataclass(frozen=True)
class StalenessAlert:
    label: str
    forecastex_age_s: float
    price_gap: float
    kalshi_price: float
    forecastex_price: float
    reason: str


def detect_staleness(
    pairs: list[MatchedPair],
    now: float,
    max_age_s: float = 900.0,
    min_divergence: float = 0.05,
) -> list[StalenessAlert]:
    """Flag pairs where Kalshi is fresh, ForecastEx is stale, and they diverge."""
    alerts: list[StalenessAlert] = []
    for p in pairs:
        fx_age = now - p.forecastex.updated_at
        kalshi_age = now - p.kalshi.updated_at
        gap = abs(p.kalshi.price - p.forecastex.price)
        if kalshi_age <= max_age_s and fx_age > max_age_s and gap >= min_divergence:
            alerts.append(
                StalenessAlert(
                    label=p.label,
                    forecastex_age_s=fx_age,
                    price_gap=gap,
                    kalshi_price=p.kalshi.price,
                    forecastex_price=p.forecastex.price,
                    reason=f"ForecastEx stale {fx_age:.0f}s and {gap:.2f} off a fresh Kalshi",
                )
            )
    return alerts


def format_staleness_alert(alert: StalenessAlert) -> str:
    direction = "rich" if alert.forecastex_price > alert.kalshi_price else "cheap"
    return "\n".join(
        [
            f"⏱️ Stale ForecastEx quote — {alert.label}",
            f"Kalshi {alert.kalshi_price:.2f} vs ForecastEx {alert.forecastex_price:.2f} "
            f"(ForecastEx {direction} by {alert.price_gap:.2f})",
            f"ForecastEx last updated {alert.forecastex_age_s:.0f}s ago",
            "Action: review — possible stale-quote on ForecastEx (IBKR/ForecastEx is allowlisted).",
        ]
    )
