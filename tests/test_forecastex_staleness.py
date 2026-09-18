"""Tests for the ForecastEx-vs-Kalshi staleness watcher core."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from forecastex_staleness import (  # noqa: E402
    MatchedPair,
    VenueQuote,
    detect_staleness,
    format_staleness_alert,
)

NOW = 10_000.0


def _pair(label="BTC>100k Dec", k_price=0.40, k_age=10.0, fx_price=0.55, fx_age=10.0):
    return MatchedPair(
        label=label,
        kalshi=VenueQuote("kalshi", "K1", k_price, NOW - k_age),
        forecastex=VenueQuote("forecastex", "F1", fx_price, NOW - fx_age),
    )


class TestForecastexStaleness:
    def test_alerts_when_kalshi_fresh_forecastex_stale_and_diverging(self):
        # ForecastEx 1200s old (>900), Kalshi 10s old, gap 0.15 (>=0.05) → alert.
        alerts = detect_staleness([_pair(fx_age=1_200.0)], NOW)
        assert len(alerts) == 1
        assert alerts[0].forecastex_age_s == 1_200.0
        assert alerts[0].price_gap == pytest.approx(0.15)

    def test_no_alert_when_forecastex_fresh(self):
        assert detect_staleness([_pair(fx_age=60.0)], NOW) == []

    def test_no_alert_when_stale_but_agreeing(self):
        # Stale ForecastEx but only 0.02 off Kalshi → below the 0.05 threshold.
        assert detect_staleness([_pair(fx_price=0.42, fx_age=1_200.0)], NOW) == []

    def test_no_alert_when_kalshi_also_stale(self):
        # Both stale → Kalshi isn't a fresh reference, so the comparison is meaningless.
        assert detect_staleness([_pair(k_age=2_000.0, fx_age=1_200.0)], NOW) == []

    def test_age_boundary_just_over_max_alerts(self):
        # ForecastEx age just over max_age (901 > 900), gap clearly above threshold.
        p = _pair(k_price=0.40, fx_price=0.50, fx_age=901.0)  # gap 0.10
        assert len(detect_staleness([p], NOW, max_age_s=900.0, min_divergence=0.05)) == 1

    def test_batch_filters_mixed(self):
        pairs = [
            _pair(label="A", fx_age=1_200.0),                 # alert
            _pair(label="B", fx_age=60.0),                    # fresh → no
            _pair(label="C", fx_price=0.41, fx_age=1_200.0),  # agrees → no
        ]
        alerts = detect_staleness(pairs, NOW)
        assert [a.label for a in alerts] == ["A"]

    def test_format_shows_direction_and_human_review(self):
        alert = detect_staleness([_pair(fx_price=0.55, fx_age=1_200.0)], NOW)[0]
        msg = format_staleness_alert(alert)
        assert "rich" in msg               # ForecastEx 0.55 > Kalshi 0.40
        assert "review" in msg
