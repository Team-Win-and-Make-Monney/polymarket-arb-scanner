"""Unit tests for Kalshi structured ticker parsing and temporal pair discovery."""

import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from kalshi_ticker import (
    find_temporal_pairs,
    is_cumulative,
    parse_kalshi_ticker,
)


class TestParseKalshiTicker:
    """Test parse_kalshi_ticker parsing accuracy across multiple ticker variations."""

    def test_parse_standard_daily_touch_ticker(self) -> None:
        ticker = "KXBTC-26FEB07-T101999.99"
        res = parse_kalshi_ticker(ticker)
        assert res is not None
        assert res["series"] == "KXBTC"
        assert res["asset"] == "BTC"
        assert res["datetime"] == datetime(2026, 2, 7, 0, 0, tzinfo=timezone.utc)
        assert res["strike"] == 101999.99
        assert res["direction"] == "above"
        assert res["type_code"] == "T"
        assert res["ticker"] == ticker

    def test_parse_hourly_resolution_ticker(self) -> None:
        ticker = "KXBTC-26APR2717-T87749.99"
        res = parse_kalshi_ticker(ticker)
        assert res is not None
        assert res["series"] == "KXBTC"
        assert res["asset"] == "BTC"
        assert res["datetime"] == datetime(2026, 4, 27, 17, 0, tzinfo=timezone.utc)
        assert res["strike"] == 87749.99
        assert res["direction"] == "above"
        assert res["type_code"] == "T"

    def test_parse_below_strike_ticker(self) -> None:
        ticker = "KXETH-26DEC31-B3000"
        res = parse_kalshi_ticker(ticker)
        assert res is not None
        assert res["series"] == "KXETH"
        assert res["asset"] == "ETH"
        assert res["datetime"] == datetime(2026, 12, 31, 0, 0, tzinfo=timezone.utc)
        assert res["strike"] == 3000.0
        assert res["direction"] == "below"
        assert res["type_code"] == "B"

    def test_parse_non_kx_prefix_ticker(self) -> None:
        ticker = "INX-26DEC15-T5500"
        res = parse_kalshi_ticker(ticker)
        assert res is not None
        assert res["series"] == "INX"
        assert res["asset"] == "INX"
        assert res["strike"] == 5500.0

    def test_parse_invalid_tickers(self) -> None:
        assert parse_kalshi_ticker("") is None
        assert parse_kalshi_ticker("INVALID-TICKER") is None
        assert parse_kalshi_ticker("KXBTC-26FOO07-T100000") is None  # Invalid month
        assert parse_kalshi_ticker("KXBTC-26FEB99-T100000") is None  # Invalid day


class TestIsCumulative:
    """Test cumulative classification heuristics."""

    def test_positive_cumulative_by_deadline(self) -> None:
        market = {
            "title": "Will Bitcoin reach $100,000 by December 31, 2026?",
            "subtitle": "Bitcoin price",
        }
        assert is_cumulative(market) is True

    def test_positive_cumulative_before_date(self) -> None:
        market = {
            "title": "Will Ethereum hit $4,000 before July 2026?",
        }
        assert is_cumulative(market) is True

    def test_positive_cumulative_touch(self) -> None:
        market = {
            "title": "Bitcoin touch $90k",
            "subtitle": "Anytime before expiration",
        }
        assert is_cumulative(market) is True

    def test_negative_range_market_excluded(self) -> None:
        market = {
            "title": "Bitcoin price between $90,000 and $100,000",
            "subtitle": "At expiration",
        }
        assert is_cumulative(market) is False

    def test_negative_point_in_time_close_excluded(self) -> None:
        market = {
            "title": "Bitcoin price at 5pm on December 31, 2026",
            "subtitle": "Settle at close",
        }
        assert is_cumulative(market) is False

    def test_negative_unrelated_event_excluded(self) -> None:
        market = {
            "title": "Fed interest rate decision in December",
        }
        assert is_cumulative(market) is False


class TestFindTemporalPairs:
    """Test temporal pair discovery and date ordering."""

    def test_pairs_discovered_and_ordered_by_date(self) -> None:
        m1 = {
            "ticker": "KXBTC-26MAR31-T100000",
            "title": "Will Bitcoin reach $100k by March 31, 2026?",
            "yes_bid": 0.40,
            "yes_ask": 0.45,
        }
        m2 = {
            "ticker": "KXBTC-26JUN30-T100000",
            "title": "Will Bitcoin reach $100k by June 30, 2026?",
            "yes_bid": 0.50,
            "yes_ask": 0.55,
        }
        m3 = {
            "ticker": "KXBTC-26DEC31-T100000",
            "title": "Will Bitcoin reach $100k by December 31, 2026?",
            "yes_bid": 0.60,
            "yes_ask": 0.65,
        }

        pairs = find_temporal_pairs([m3, m1, m2])  # Passed out of order
        assert len(pairs) == 3

        # Pair 1: Mar 31 (sub) and Jun 30 (sup)
        p1 = pairs[0]
        assert p1["early_ticker"] == "KXBTC-26MAR31-T100000"
        assert p1["late_ticker"] == "KXBTC-26JUN30-T100000"
        assert p1["dt_early"] < p1["dt_late"]

        # Pair 2: Mar 31 (sub) and Dec 31 (sup)
        p2 = pairs[1]
        assert p2["early_ticker"] == "KXBTC-26MAR31-T100000"
        assert p2["late_ticker"] == "KXBTC-26DEC31-T100000"

        # Pair 3: Jun 30 (sub) and Dec 31 (sup)
        p3 = pairs[2]
        assert p3["early_ticker"] == "KXBTC-26JUN30-T100000"
        assert p3["late_ticker"] == "KXBTC-26DEC31-T100000"

    def test_different_strikes_not_paired(self) -> None:
        m1 = {
            "ticker": "KXBTC-26MAR31-T90000",
            "title": "Will Bitcoin reach $90k by March 31, 2026?",
        }
        m2 = {
            "ticker": "KXBTC-26JUN30-T100000",
            "title": "Will Bitcoin reach $100k by June 30, 2026?",
        }
        pairs = find_temporal_pairs([m1, m2])
        assert len(pairs) == 0

    def test_non_cumulative_markets_ignored(self) -> None:
        m1 = {
            "ticker": "KXBTC-26MAR31-T100000",
            "title": "Bitcoin price at 5pm on March 31, 2026",
        }
        m2 = {
            "ticker": "KXBTC-26JUN30-T100000",
            "title": "Will Bitcoin reach $100k by June 30, 2026?",
        }
        pairs = find_temporal_pairs([m1, m2])
        assert len(pairs) == 0
