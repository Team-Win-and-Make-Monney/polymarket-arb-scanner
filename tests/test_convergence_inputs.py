"""Convergence input assembly (2026-07-21 scan-performance post-mortem).

The inline block in continuous.py re-ran a full fuzzy match of every
Polymarket market against ~69k flat Kalshi markets on every scan cycle
(~166s, 66% of scan time) — while CONVERGENCE_MIN_PLATFORMS=3 with only
2 platform sources meant the output could never produce an opportunity.
"""
import sys
import os
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from scans.convergence_inputs import ConvergenceMatchCache, build_convergence_matched

# Inside the MAX_RESOLUTION_DAYS window so fixtures survive the window filter.
_SOON = (datetime.now(timezone.utc) + timedelta(days=2)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _pm(cid, title, yes):
    return {
        "condition_id": cid,
        "question": title,
        "tokens": [{"outcome": "Yes", "price": yes}],
    }


def _kalshi_event(ticker, title, yes_ask):
    return {
        "event_ticker": f"EV-{ticker}",
        "markets": [{
            "ticker": ticker, "title": title, "yes_ask": yes_ask,
            "close_time": _SOON, "expiration_time": _SOON,
        }],
    }


def _matcher_for(pairs):
    """Return a matcher fn mapping pm condition_id -> kalshi ticker."""
    def matcher(pm_markets, k_markets, _a, _b, threshold, min_confidence):
        k_by_ticker = {m.get("ticker"): m for m in k_markets}
        out = []
        for pm in pm_markets:
            tick = pairs.get(pm.get("condition_id"))
            if tick and tick in k_by_ticker:
                out.append({"market_a": pm, "market_b": k_by_ticker[tick]})
        return out
    return MagicMock(side_effect=matcher)


class TestShortCircuit:
    def test_skips_matching_when_sources_below_min_platforms(self):
        # Only 2 price sources exist; min_platforms=3 -> nothing can ever
        # qualify, so the expensive fuzzy match must not run at all.
        matcher = _matcher_for({"c1": "K-1"})
        cache = ConvergenceMatchCache(refresh_interval=1800.0)
        result = build_convergence_matched(
            [_pm("c1", "Will X happen?", 0.5)],
            [_kalshi_event("K-1", "Will X happen?", 0.55)],
            cache, matcher_fn=matcher, min_confidence="medium",
            min_platforms=3, now=1000.0,
        )
        assert result == []
        matcher.assert_not_called()


class TestBuildsMatchedMarkets:
    def test_builds_two_platform_prices(self):
        matcher = _matcher_for({"c1": "K-1"})
        cache = ConvergenceMatchCache(refresh_interval=1800.0)
        result = build_convergence_matched(
            [_pm("c1", "Will X happen?", 0.50)],
            [_kalshi_event("K-1", "Will X happen?", 0.60)],
            cache, matcher_fn=matcher, min_confidence="medium",
            min_platforms=2, now=1000.0,
        )
        assert len(result) == 1
        pp = result[0]["platform_prices"]
        assert pp["polymarket"]["yes"] == 0.50
        assert pp["kalshi"]["yes"] == 0.60
        assert matcher.call_count == 1

    def test_cent_denominated_kalshi_price_normalised(self):
        matcher = _matcher_for({"c1": "K-1"})
        cache = ConvergenceMatchCache(refresh_interval=1800.0)
        result = build_convergence_matched(
            [_pm("c1", "Will X happen?", 0.50)],
            [_kalshi_event("K-1", "Will X happen?", 60)],
            cache, matcher_fn=matcher, min_confidence="medium",
            min_platforms=2, now=1000.0,
        )
        assert result[0]["platform_prices"]["kalshi"]["yes"] == 0.60


class TestZeroPrices:
    def test_zero_prices_are_not_dropped_by_truthiness(self):
        # A 0.0 price is data, not absence — scan_convergence's own 0<p<1
        # bound is the right place to exclude it, not a truthiness check here.
        matcher = _matcher_for({"c1": "K-1"})
        cache = ConvergenceMatchCache(refresh_interval=1800.0)
        result = build_convergence_matched(
            [_pm("c1", "Will X happen?", 0.0)],
            [_kalshi_event("K-1", "Will X happen?", 0.60)],
            cache, matcher_fn=matcher, min_confidence="medium",
            min_platforms=2, now=1000.0,
        )
        assert len(result) == 1
        assert result[0]["platform_prices"]["polymarket"]["yes"] == 0.0


class TestMatchCache:
    def test_second_cycle_skips_matching_but_refreshes_prices(self):
        matcher = _matcher_for({"c1": "K-1"})
        cache = ConvergenceMatchCache(refresh_interval=1800.0)
        pm = [_pm("c1", "Will X happen?", 0.50)]
        build_convergence_matched(
            pm, [_kalshi_event("K-1", "Will X happen?", 0.60)],
            cache, matcher_fn=matcher, min_confidence="medium",
            min_platforms=2, now=1000.0,
        )
        # Next cycle: same markets, moved Kalshi price, no re-match.
        result = build_convergence_matched(
            pm, [_kalshi_event("K-1", "Will X happen?", 0.70)],
            cache, matcher_fn=matcher, min_confidence="medium",
            min_platforms=2, now=1060.0,
        )
        assert matcher.call_count == 1
        assert result[0]["platform_prices"]["kalshi"]["yes"] == 0.70

    def test_only_new_markets_are_matched_incrementally(self):
        matcher = _matcher_for({"c1": "K-1", "c2": "K-2"})
        cache = ConvergenceMatchCache(refresh_interval=1800.0)
        events = [
            _kalshi_event("K-1", "Will X happen?", 0.60),
            _kalshi_event("K-2", "Will Y happen?", 0.40),
        ]
        build_convergence_matched(
            [_pm("c1", "Will X happen?", 0.50)], events,
            cache, matcher_fn=matcher, min_confidence="medium",
            min_platforms=2, now=1000.0,
        )
        result = build_convergence_matched(
            [_pm("c1", "Will X happen?", 0.50), _pm("c2", "Will Y happen?", 0.45)],
            events, cache, matcher_fn=matcher, min_confidence="medium",
            min_platforms=2, now=1060.0,
        )
        assert matcher.call_count == 2
        # Second matcher call only received the new market.
        second_call_pm = matcher.call_args_list[1][0][0]
        assert [m["condition_id"] for m in second_call_pm] == ["c2"]
        assert len(result) == 2

    def test_full_rematch_after_refresh_interval(self):
        matcher = _matcher_for({"c1": "K-1"})
        cache = ConvergenceMatchCache(refresh_interval=1800.0)
        pm = [_pm("c1", "Will X happen?", 0.50)]
        events = [_kalshi_event("K-1", "Will X happen?", 0.60)]
        kwargs = dict(cache=cache, matcher_fn=matcher, min_confidence="medium",
                      min_platforms=2)
        build_convergence_matched(pm, events, now=1000.0, **kwargs)
        build_convergence_matched(pm, events, now=3000.0, **kwargs)
        assert matcher.call_count == 2

    def test_unmatched_market_cached_as_negative(self):
        # A market with no Kalshi counterpart must not be re-matched each cycle.
        matcher = _matcher_for({})
        cache = ConvergenceMatchCache(refresh_interval=1800.0)
        pm = [_pm("c1", "Will X happen?", 0.50)]
        events = [_kalshi_event("K-1", "Unrelated", 0.60)]
        kwargs = dict(cache=cache, matcher_fn=matcher, min_confidence="medium",
                      min_platforms=2)
        assert build_convergence_matched(pm, events, now=1000.0, **kwargs) == []
        assert build_convergence_matched(pm, events, now=1060.0, **kwargs) == []
        assert matcher.call_count == 1
