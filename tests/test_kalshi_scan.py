"""Tests for scans/kalshi.py — Kalshi binary and multi-outcome scan logic."""

import pytest
from unittest.mock import MagicMock, patch
from typing import ClassVar
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


@pytest.fixture(autouse=True)
def mock_external_modules():
    """Mock kalshi_api if not installed."""
    mocked = {}
    if "kalshi_api" not in sys.modules:
        mocked["kalshi_api"] = MagicMock()
        sys.modules["kalshi_api"] = mocked["kalshi_api"]
    for key in list(sys.modules):
        if key == "scans.kalshi":
            del sys.modules[key]
    yield
    for mod_name in mocked:
        if mod_name in sys.modules:
            del sys.modules[mod_name]


# ---------------------------------------------------------------------------
# _fetch_kalshi_data
# ---------------------------------------------------------------------------

class TestFetchKalshiData:
    def _reset_cache(self):
        import scans.kalshi as sk
        with sk._kalshi_data_cache_lock:
            sk._kalshi_data_cache["ts"] = 0.0
            sk._kalshi_data_cache["value"] = None

    def test_returns_empty_without_client(self):
        self._reset_cache()
        from scans.kalshi import _fetch_kalshi_data
        events, by_event, titles = _fetch_kalshi_data(None)
        assert events == []
        assert by_event == {}
        assert titles == {}

    def test_returns_empty_when_no_events(self):
        self._reset_cache()
        from scans.kalshi import _fetch_kalshi_data
        client = MagicMock()
        client.fetch_all_events.return_value = []
        events, by_event, titles = _fetch_kalshi_data(client)
        assert events == []

    def test_uses_nested_markets_when_present(self):
        """Events with nested ``markets`` arrays must NOT trigger _parallel_fetch_kalshi."""
        self._reset_cache()
        from scans.kalshi import _fetch_kalshi_data

        client = MagicMock()
        client.fetch_all_events.return_value = [
            {"event_ticker": "E1", "title": "Event 1",
             "markets": [{"ticker": "M1A"}, {"ticker": "M1B"}]},
            {"event_ticker": "E2", "title": "Event 2",
             "markets": [{"ticker": "M2A"}]},
        ]

        with patch("scans.kalshi._parallel_fetch_kalshi") as mock_parallel:
            events, by_event, titles = _fetch_kalshi_data(client)

        assert mock_parallel.call_count == 0, "must not fall back to per-event REST"
        assert by_event == {
            "E1": [{"ticker": "M1A"}, {"ticker": "M1B"}],
            "E2": [{"ticker": "M2A"}],
        }
        assert titles == {"E1": "Event 1", "E2": "Event 2"}

    def test_falls_back_to_parallel_fetch_when_no_nested_markets(self):
        """Events lacking ``markets`` field still work via legacy REST path."""
        self._reset_cache()
        from scans.kalshi import _fetch_kalshi_data

        client = MagicMock()
        client.fetch_all_events.return_value = [
            {"event_ticker": "E1", "title": "Event 1"},  # no markets field
        ]
        with patch("scans.kalshi._parallel_fetch_kalshi", return_value={"E1": [{"ticker": "MX"}]}) as mock_parallel:
            events, by_event, titles = _fetch_kalshi_data(client)

        assert mock_parallel.call_count == 1
        assert by_event == {"E1": [{"ticker": "MX"}]}

    def test_cache_hit_skips_api_within_ttl(self):
        """Second call within TTL returns cached value without re-calling fetch_all_events."""
        self._reset_cache()
        import scans.kalshi as sk
        from scans.kalshi import _fetch_kalshi_data

        client = MagicMock()
        client.fetch_all_events.return_value = [
            {"event_ticker": "E1", "title": "T", "markets": [{"ticker": "MA"}]},
        ]
        # First call populates cache
        _fetch_kalshi_data(client)
        first_calls = client.fetch_all_events.call_count
        # Force long TTL so the second call hits cache
        original_ttl = sk._KALSHI_DATA_CACHE_TTL
        sk._KALSHI_DATA_CACHE_TTL = 3600
        try:
            _fetch_kalshi_data(client)
        finally:
            sk._KALSHI_DATA_CACHE_TTL = original_ttl
        assert client.fetch_all_events.call_count == first_calls, "cache hit must not re-call API"

    def test_cache_expires_after_ttl(self):
        """After TTL elapses, cache is bypassed and fetch_all_events runs again."""
        self._reset_cache()
        import scans.kalshi as sk
        from scans.kalshi import _fetch_kalshi_data

        client = MagicMock()
        client.fetch_all_events.return_value = [
            {"event_ticker": "E1", "title": "T", "markets": []},
        ]
        original_ttl = sk._KALSHI_DATA_CACHE_TTL
        sk._KALSHI_DATA_CACHE_TTL = 0  # treat any age as expired
        try:
            _fetch_kalshi_data(client)
            _fetch_kalshi_data(client)
        finally:
            sk._KALSHI_DATA_CACHE_TTL = original_ttl
        assert client.fetch_all_events.call_count == 2


# ---------------------------------------------------------------------------
# scan_kalshi_binary
# ---------------------------------------------------------------------------

class TestScanKalshiBinary:
    def test_returns_empty_without_client(self):
        from scans.kalshi import scan_kalshi_binary
        result = scan_kalshi_binary(None, 0.01)
        assert result == []

    def test_returns_empty_with_no_markets(self):
        from scans.kalshi import scan_kalshi_binary
        client = MagicMock()
        result = scan_kalshi_binary(client, 0.01, kalshi_data=([], {}, {}))
        assert result == []

    def test_finds_binary_arb(self):
        from scans.kalshi import scan_kalshi_binary

        client = MagicMock()
        client.get_market_price.return_value = (0.45, 0.45)
        client.get_order_book_depth.return_value = {"yes_ask_size": 100, "no_ask_size": 100}

        market = {
            "ticker": "KXTICKER",
            "title": "Will X happen?",
            "close_time": "2030-01-01T00:00:00Z",
            "expiration_time": "2030-01-01T00:00:00Z",
        }
        kalshi_data = (
            [{"event_ticker": "EV1", "title": "Event 1"}],
            {"EV1": [market]},
            {"EV1": "Event 1"},
        )

        with patch("scans.kalshi._within_resolution_window", return_value=True), \
             patch("scans.kalshi.filter_dust", side_effect=lambda x: x), \
             patch("scans.kalshi.net_profit_kalshi_binary", return_value={
                 "gross_spread": 0.10, "fees": 0.02, "net_profit": 0.08,
             }):
            result = scan_kalshi_binary(client, 0.01, kalshi_data=kalshi_data)

        assert len(result) == 1
        opp = result[0]
        assert opp["type"] == "KalshiBinary"
        assert opp["_kalshi_ticker"] == "KXTICKER"
        assert opp["_kalshi_yes"] == 0.45
        assert opp["_kalshi_no"] == 0.45
        assert opp["net_profit"] == 0.08

    def test_skips_dust_prices(self):
        from scans.kalshi import scan_kalshi_binary

        client = MagicMock()
        client.get_market_price.return_value = (0.001, 0.999)

        market = {"ticker": "K-DUST", "title": "Dust market",
                  "close_time": "2030-01-01T00:00:00Z"}
        kalshi_data = ([{"event_ticker": "EV1"}], {"EV1": [market]}, {"EV1": "Event"})

        with patch("scans.kalshi._within_resolution_window", return_value=True), \
             patch("scans.kalshi.filter_dust", side_effect=lambda x: x):
            result = scan_kalshi_binary(client, 0.01, kalshi_data=kalshi_data)

        assert result == []

    def test_skips_resolved_markets(self):
        from scans.kalshi import scan_kalshi_binary

        client = MagicMock()
        client.get_market_price.return_value = (0.45, 0.45)

        market = {"ticker": "K-OLD", "title": "Old market",
                  "close_time": "2020-01-01T00:00:00Z"}
        kalshi_data = ([{"event_ticker": "EV1"}], {"EV1": [market]}, {"EV1": "Event"})

        with patch("scans.kalshi._within_resolution_window", return_value=False), \
             patch("scans.kalshi.filter_dust", side_effect=lambda x: x):
            result = scan_kalshi_binary(client, 0.01, kalshi_data=kalshi_data)

        assert result == []


# ---------------------------------------------------------------------------
# scan_kalshi_multi
# ---------------------------------------------------------------------------

class TestScanKalshiMulti:
    def test_returns_empty_without_client(self):
        from scans.kalshi import scan_kalshi_multi
        result = scan_kalshi_multi(None, 0.01)
        assert result == []

    def test_skips_single_market_events(self):
        from scans.kalshi import scan_kalshi_multi

        client = MagicMock()
        kalshi_data = (
            [{"event_ticker": "EV1"}],
            {"EV1": [{"ticker": "K-1", "title": "Only one"}]},
            {"EV1": "Single event"},
        )
        result = scan_kalshi_multi(client, 0.01, kalshi_data=kalshi_data)
        assert result == []

    def test_finds_multi_arb(self):
        from scans.kalshi import scan_kalshi_multi

        client = MagicMock()
        # YES asks sum to 0.98 — plausible for a complete single-winner market.
        client.get_market_price.side_effect = [
            (0.35, 0.65), (0.33, 0.67), (0.30, 0.70),
        ]
        client.get_order_book_depth.return_value = {"yes_ask_size": 50}

        markets = [
            {"ticker": "K-A", "title": "A", "close_time": "2030-01-01T00:00:00Z",
             "expiration_time": "2030-01-01T00:00:00Z"},
            {"ticker": "K-B", "title": "B", "close_time": "2030-01-01T00:00:00Z",
             "expiration_time": "2030-01-01T00:00:00Z"},
            {"ticker": "K-C", "title": "C", "close_time": "2030-01-01T00:00:00Z",
             "expiration_time": "2030-01-01T00:00:00Z"},
        ]
        kalshi_data = (
            [{"event_ticker": "EV1", "title": "Multi Event", "mutually_exclusive": True}],
            {"EV1": markets},
            {"EV1": "Multi Event"},
        )

        with patch("scans.kalshi._within_resolution_window", return_value=True), \
             patch("scans.kalshi.filter_dust", side_effect=lambda x: x), \
             patch("scans.kalshi.net_profit_kalshi_multi", return_value={
                 "gross_spread": 0.15, "fees": 0.03, "net_profit": 0.12,
             }):
            result = scan_kalshi_multi(client, 0.01, kalshi_data=kalshi_data)

        assert len(result) >= 1
        opp = result[0]
        assert opp["type"].startswith("KalshiMulti")
        assert opp["net_profit"] > 0
        assert "_kalshi_tickers" in opp
        assert "_kalshi_prices" in opp

    def _gate_fixture(self, mutually_exclusive):
        client = MagicMock()
        # Sum = 0.90: a real arb (< 1.0) that clears the completeness floor (>= 0.85).
        client.get_market_price.side_effect = [(0.35, 0.65), (0.30, 0.70), (0.25, 0.75)]
        client.get_order_book_depth.return_value = {"yes_ask_size": 50}
        markets = [
            {"ticker": f"K-{x}", "title": x, "close_time": "2030-01-01T00:00:00Z",
             "expiration_time": "2030-01-01T00:00:00Z"}
            for x in ("A", "B", "C")
        ]
        event = {"event_ticker": "EV1", "title": "Multi Event"}
        if mutually_exclusive is not None:
            event["mutually_exclusive"] = mutually_exclusive
        return client, ([event], {"EV1": markets}, {"EV1": "Multi Event"})

    def _run_gate(self, mutually_exclusive):
        from scans.kalshi import scan_kalshi_multi
        client, kalshi_data = self._gate_fixture(mutually_exclusive)
        with patch("scans.kalshi._within_resolution_window", return_value=True), \
             patch("scans.kalshi.filter_dust", side_effect=lambda x: x), \
             patch("scans.kalshi.net_profit_kalshi_multi", return_value={
                 "gross_spread": 0.15, "fees": 0.03, "net_profit": 0.12,
             }):
            return scan_kalshi_multi(client, 0.01, kalshi_data=kalshi_data)

    def test_non_mutually_exclusive_event_skipped(self):
        # Non-exclusive market ladders (e.g. soccer "Spreads") are NOT
        # complete sets — buying YES on each does not guarantee a $1 payout.
        # This was the source of ~296K phantom detections in production.
        assert self._run_gate(False) == []

    def test_missing_mutually_exclusive_flag_skipped(self):
        # Unknown exclusivity is treated as not-a-complete-set (strict gate).
        assert self._run_gate(None) == []

    def test_mutually_exclusive_event_passes_gate(self):
        result = self._run_gate(True)
        assert len(result) >= 1
        assert result[0]["type"].startswith("KalshiMulti")

    def test_skips_mutually_exclusive_with_implausible_sum(self):
        """MECE event whose YES asks sum far below 1.0 → missing/stale legs, not arb."""
        from scans.kalshi import scan_kalshi_multi

        client = MagicMock()
        # 3 legs summing to 0.30 — implausible for a complete single-winner market.
        client.get_market_price.side_effect = [(0.10, 0.90), (0.10, 0.90), (0.10, 0.90)]

        markets = [
            {"ticker": "K-A", "title": "A", "close_time": "2030-01-01T00:00:00Z"},
            {"ticker": "K-B", "title": "B", "close_time": "2030-01-01T00:00:00Z"},
            {"ticker": "K-C", "title": "C", "close_time": "2030-01-01T00:00:00Z"},
        ]
        kalshi_data = (
            [{"event_ticker": "EV1", "title": "Sparse", "mutually_exclusive": True}],
            {"EV1": markets},
            {"EV1": "Sparse"},
        )

        with patch("scans.kalshi._within_resolution_window", return_value=True), \
             patch("scans.kalshi.filter_dust", side_effect=lambda x: x):
            result = scan_kalshi_multi(client, 0.01, kalshi_data=kalshi_data)

        assert result == []


# ---------------------------------------------------------------------------
# scan_kalshi_multi — exhaustiveness gate (2026-07-21 false-positive post-mortem)
# ---------------------------------------------------------------------------


class TestScanKalshiMultiExhaustiveness:
    """mutually_exclusive=True means at most one outcome pays — NOT that the
    listed outcomes cover the space. KXTRUMPPHOTO-26JUL26 (buckets exactly
    4/5/6/7 days, no '3 or fewer' tail) priced at 0.86 was reported as an
    8% riskless arb; if the value lands 0-3 every leg loses. A scalar strike
    ladder is only exhaustive when it has open-ended tail buckets."""

    CLOSE: ClassVar[dict[str, str]] = {"close_time": "2030-01-01T00:00:00Z", "expiration_time": "2030-01-01T00:00:00Z"}

    def _scan(self, markets, prices):
        from scans.kalshi import scan_kalshi_multi
        client = MagicMock()
        client.get_market_price.side_effect = prices
        client.get_order_book_depth.return_value = {"yes_ask_size": 50}
        kalshi_data = (
            [{"event_ticker": "EV1", "title": "Ladder Event", "mutually_exclusive": True}],
            {"EV1": markets},
            {"EV1": "Ladder Event"},
        )
        with patch("scans.kalshi._within_resolution_window", return_value=True), \
             patch("scans.kalshi.filter_dust", side_effect=lambda x: x):
            return scan_kalshi_multi(client, 0.01, kalshi_data=kalshi_data)

    def test_exact_bucket_ladder_without_tails_is_rejected(self):
        # The live KXTRUMPPHOTO shape: exact-value buckets, no open tails,
        # asks sum 0.86 — looks like an 8%+ arb but the set is not exhaustive.
        markets = [
            {"ticker": f"K-{s}", "title": str(s), "floor_strike": s, "cap_strike": s, **self.CLOSE}
            for s in (4, 5, 6, 7)
        ]
        prices = [(0.23, 0.78), (0.37, 0.64), (0.25, 0.76), (0.01, 0.99)]
        assert self._scan(markets, prices) == []

    def test_ladder_with_open_tails_is_kept(self):
        # Bottom tail (<=3), interior exact buckets, top tail (>=6): exhaustive.
        markets = [
            {"ticker": "K-LOW", "title": "3 or fewer", "floor_strike": None, "cap_strike": 3, **self.CLOSE},
            {"ticker": "K-4", "title": "4", "floor_strike": 4, "cap_strike": 4, **self.CLOSE},
            {"ticker": "K-5", "title": "5", "floor_strike": 5, "cap_strike": 5, **self.CLOSE},
            {"ticker": "K-HIGH", "title": "6 or more", "floor_strike": 6, "cap_strike": None, **self.CLOSE},
        ]
        prices = [(0.18, 0.83), (0.28, 0.73), (0.26, 0.75), (0.18, 0.83)]  # asks sum 0.90
        result = self._scan(markets, prices)
        assert len(result) == 1
        assert result[0]["type"] == "KalshiMulti(4)"

    def test_ladder_with_interior_gap_is_rejected(self):
        # Tails present but bucket 4 missing (<=3, 5, >=6): a 4 loses every leg.
        markets = [
            {"ticker": "K-LOW", "title": "3 or fewer", "floor_strike": None, "cap_strike": 3, **self.CLOSE},
            {"ticker": "K-5", "title": "5", "floor_strike": 5, "cap_strike": 5, **self.CLOSE},
            {"ticker": "K-HIGH", "title": "6 or more", "floor_strike": 6, "cap_strike": None, **self.CLOSE},
        ]
        prices = [(0.28, 0.73), (0.30, 0.71), (0.28, 0.73)]  # asks sum 0.86
        assert self._scan(markets, prices) == []

    def test_ladder_with_between_buckets_is_kept(self):
        # <=3, between 4-5, >=6: contiguous coverage with a range bucket.
        markets = [
            {"ticker": "K-LOW", "title": "3 or fewer", "floor_strike": None, "cap_strike": 3, **self.CLOSE},
            {"ticker": "K-MID", "title": "4 to 5", "floor_strike": 4, "cap_strike": 5, **self.CLOSE},
            {"ticker": "K-HIGH", "title": "6 or more", "floor_strike": 6, "cap_strike": None, **self.CLOSE},
        ]
        prices = [(0.28, 0.73), (0.32, 0.69), (0.28, 0.73)]  # asks sum 0.88
        result = self._scan(markets, prices)
        assert len(result) == 1

    def test_categorical_event_without_strikes_unchanged(self):
        # No strike fields at all -> no structural signal; existing behavior kept.
        markets = [
            {"ticker": "K-A", "title": "A", **self.CLOSE},
            {"ticker": "K-B", "title": "B", **self.CLOSE},
            {"ticker": "K-C", "title": "C", **self.CLOSE},
        ]
        prices = [(0.35, 0.65), (0.30, 0.70), (0.25, 0.75)]  # asks sum 0.90
        result = self._scan(markets, prices)
        assert len(result) == 1
