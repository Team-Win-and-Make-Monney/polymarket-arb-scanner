"""Tests for the earnings-mention OOS runner (scripts/run_earnings_mention_oos.py).

The deterministic richness/verdict math and candlestick reconstruction live in
earnings_mention.py (tested separately); this pins the runner's own logic:
JSON state roundtrip, the state-anomaly guard, and the
discover-settled -> reconstruct-T24h -> accumulate -> verdict wiring in
run_cycle, including the fail-closed retry behavior on a partial-fetch
failure. All network is faked via a duck-typed client — no live Kalshi calls.
"""
from __future__ import annotations

import os
import json
import sys
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from earnings_mention import OosStats
from scripts.run_earnings_mention_oos import (
    _check_state_anomaly,
    _format_failure_notice,
    _should_alert,
    format_message,
    load_state,
    run_cycle,
    save_state,
)


# --------------------------------------------------------------------------- #
# Fake client — a settled-markets list + a per-ticker candlestick table.
#
# fetch_candlesticks contract (matches kalshi_api.KalshiClient after the
# CodeRabbit/Codex fixes): a ticker mapped to None simulates a TRANSIENT
# request failure (price_at_t24h raises RuntimeError); a ticker mapped
# to [] (or simply absent from the table) simulates a successful request
# that found no candles in the window -- PERMANENT, price_at_t24h returns
# None without raising. These are NOT interchangeable.
# --------------------------------------------------------------------------- #
class FakeClient:
    def __init__(self, settled=None, candles=None, raise_pagination_error=False):
        self._settled = settled if settled is not None else []
        self._candles = candles or {}
        self.candlestick_tickers: list[str] = []
        self._raise_pagination_error = raise_pagination_error

    def fetch_settled_markets(self, min_close_ts, *a, **k):
        if self._raise_pagination_error:
            raise RuntimeError("simulated incomplete pagination")
        return self._settled

    def fetch_candlesticks(self, series_ticker, ticker, start_ts, end_ts, period_interval=60):
        self.candlestick_tickers.append(ticker)
        return self._candles.get(ticker, [])


def _candle(close_dollars: str) -> dict:
    return {"price": {"close_dollars": close_dollars}}


def _market(ticker: str, close: str, result: str = "yes", title: str = "Will Foo mention Bar?",
            series_ticker: str = "KXEARNINGSMENTIONBA", **extra) -> dict:
    m = {"ticker": ticker, "title": title, "close_time": close, "result": result, "series_ticker": series_ticker}
    m.update(extra)
    return m


def _fresh_state() -> dict:
    return {"watermark_ts": None, "seen": [], "resolved": [], "last_verdict": "continue", "first_seen_ts": None}


def _persistable(result: dict) -> dict:
    """Strip the caller-facing underscore-prefixed extras run_cycle returns,
    leaving exactly what a caller would pass into the NEXT run_cycle call."""
    return {k: v for k, v in result.items() if not k.startswith("_")}


NOW = datetime(2026, 7, 5, 12, 0, tzinfo=timezone.utc)
CLOSE_DT = datetime(2026, 7, 6, 0, 0, tzinfo=timezone.utc)
CLOSE_ISO = "2026-07-06T00:00:00Z"
CLOSE_TS = int(CLOSE_DT.timestamp())


# --------------------------------------------------------------------------- #
# load_state / save_state
# --------------------------------------------------------------------------- #
class TestState:
    def test_load_state_missing_file_returns_empty(self, tmp_path):
        assert load_state(tmp_path / "does-not-exist.json") == _fresh_state()

    def test_load_state_none_path_returns_empty(self):
        assert load_state(None) == _fresh_state()

    def test_load_state_corrupt_json_aborts(self, tmp_path):
        path = tmp_path / "state.json"
        path.write_text("{not valid json")
        with pytest.raises(ValueError):
            load_state(path)

    def test_load_state_non_dict_json_aborts(self, tmp_path):
        path = tmp_path / "state.json"
        path.write_text("[1, 2, 3]")
        with pytest.raises(ValueError, match="JSON object"):
            load_state(path)

    @pytest.mark.parametrize("key,value", [
        ("seen", None),
        ("seen", {"M1": True}),
        ("resolved", {}),
        ("watermark_ts", True),
        ("last_verdict", "maybe"),
    ])
    def test_load_state_schema_invalid_aborts(self, tmp_path, key, value):
        path = tmp_path / "state.json"
        state = _fresh_state()
        state[key] = value
        path.write_text(json.dumps(state))
        with pytest.raises(ValueError):
            load_state(path)

    def test_save_and_load_roundtrip(self, tmp_path):
        path = tmp_path / "state.json"
        state = {
            "watermark_ts": 1735000000,
            "seen": ["M1", "M2"],
            "resolved": [{"ticker": "M1", "yes_price": 0.3, "outcome": 1.0, "series": "S",
                          "resolved_ts": NOW.isoformat()}],
            "last_verdict": "continue",
            "first_seen_ts": NOW.isoformat(),
        }
        save_state(path, state)
        assert load_state(path) == state

    def test_load_state_rejects_duplicate_resolved_tickers(self, tmp_path):
        path = tmp_path / "state.json"
        row = {
            "ticker": "M1", "yes_price": 0.3, "outcome": 1.0,
            "series": "S", "resolved_ts": NOW.isoformat(),
        }
        state = {**_fresh_state(), "seen": ["M1"], "resolved": [row, row]}
        path.write_text(json.dumps(state))
        with pytest.raises(ValueError, match="duplicate resolved"):
            load_state(path)

    def test_load_state_requires_resolved_ticker_in_seen(self, tmp_path):
        path = tmp_path / "state.json"
        state = {
            **_fresh_state(),
            "resolved": [{
                "ticker": "M1", "yes_price": 0.3, "outcome": 1.0,
                "series": "S", "resolved_ts": NOW.isoformat(),
            }],
        }
        path.write_text(json.dumps(state))
        with pytest.raises(ValueError, match="also be present in seen"):
            load_state(path)

    def test_save_state_noop_when_path_none(self):
        save_state(None, _fresh_state())  # must not raise

    def test_save_state_atomic_write_leaves_no_tmp_file(self, tmp_path):
        path = tmp_path / "state.json"
        save_state(path, _fresh_state())
        assert path.exists()
        assert not (tmp_path / "state.json.tmp").exists()

    def test_save_failure_preserves_existing_state(self, tmp_path, monkeypatch):
        path = tmp_path / "state.json"
        path.write_text('{"original": true}')
        monkeypatch.setattr("scripts.run_earnings_mention_oos.os.replace", MagicMock(
            side_effect=OSError("disk full")
        ))
        with pytest.raises(OSError, match="disk full"):
            save_state(path, _fresh_state())
        assert path.read_text() == '{"original": true}'
        assert not (tmp_path / "state.json.tmp").exists()


# --------------------------------------------------------------------------- #
# _check_state_anomaly
# --------------------------------------------------------------------------- #
class TestCheckStateAnomaly:
    def test_no_anomaly_on_genuine_first_run(self):
        assert _check_state_anomaly({"first_seen_ts": None, "resolved": []}, NOW) is None

    def test_no_anomaly_when_young(self):
        state = {"first_seen_ts": NOW.isoformat(), "resolved": []}
        assert _check_state_anomaly(state, NOW + timedelta(days=3)) is None

    def test_no_anomaly_within_a_generous_zero_settlement_gap(self):
        """A 20-day gap with zero settlements is plausible on its own
        (mention-market settlements are lumpy, tied to quarterly earnings)
        -- must not fire below the 30-day threshold."""
        state = {"first_seen_ts": (NOW - timedelta(days=20)).isoformat(), "resolved": []}
        assert _check_state_anomaly(state, NOW) is None

    def test_no_anomaly_when_resolved_present_even_if_old(self):
        state = {"first_seen_ts": (NOW - timedelta(days=45)).isoformat(), "resolved": [{"ticker": "X"}]}
        assert _check_state_anomaly(state, NOW) is None

    def test_anomaly_when_old_and_empty(self):
        state = {"first_seen_ts": (NOW - timedelta(days=45)).isoformat(), "resolved": []}
        msg = _check_state_anomaly(state, NOW)
        assert msg is not None
        assert "eviction" in msg or "reset" in msg
        assert "MAY be" in msg  # softened language -- acknowledges the genuine-gap possibility

    def test_no_anomaly_with_unparseable_first_seen(self):
        assert _check_state_anomaly({"first_seen_ts": "not-a-date", "resolved": []}, NOW) is None


# --------------------------------------------------------------------------- #
# run_cycle — discover settled -> reconstruct T-24h -> accumulate -> verdict
# --------------------------------------------------------------------------- #
class TestRunCycle:
    def test_first_run_seeds_watermark_from_now_when_nothing_settled(self):
        result = run_cycle(FakeClient(settled=[]), NOW, _fresh_state())
        assert result["watermark_ts"] == int(NOW.timestamp())
        assert result["resolved"] == []
        assert result["seen"] == []
        assert result["first_seen_ts"] == NOW.isoformat()
        assert result["_new_resolved"] == 0
        assert result["_failed_tickers"] == []

    def test_settled_market_with_valid_candle_is_resolved(self):
        market = _market("M1", CLOSE_ISO, result="yes")
        client = FakeClient(settled=[market], candles={"M1": [_candle("0.30")]})
        state = {**_fresh_state(), "watermark_ts": 0}
        result = run_cycle(client, NOW, state)
        assert [r["ticker"] for r in result["resolved"]] == ["M1"]
        assert result["resolved"][0]["outcome"] == 1.0
        assert result["resolved"][0]["yes_price"] == pytest.approx(0.30)
        assert result["seen"] == ["M1"]
        assert result["_new_resolved"] == 1
        assert result["watermark_ts"] == CLOSE_TS + 1

    def test_duplicate_discovery_row_is_counted_once(self):
        market = _market("M1", CLOSE_ISO, result="yes")
        client = FakeClient(
            settled=[market, dict(market)], candles={"M1": [_candle("0.30")]}
        )
        result = run_cycle(client, NOW, {**_fresh_state(), "watermark_ts": 0})
        assert [row["ticker"] for row in result["resolved"]] == ["M1"]
        assert result["_stats"].n == 1
        assert client.candlestick_tickers == ["M1"]

    def test_non_mention_market_advances_watermark_but_is_not_tracked_seen(self):
        market = _market("BTC1", CLOSE_ISO, result="yes", title="Will BTC hit 100k?", series_ticker="KXBTC")
        client = FakeClient(settled=[market])
        state = {**_fresh_state(), "watermark_ts": 0}
        result = run_cycle(client, NOW, state)
        assert result["resolved"] == []
        assert result["seen"] == []  # never tracked -- watermark advancement is enough
        assert result["watermark_ts"] == CLOSE_TS + 1
        assert client.candlestick_tickers == []  # never even attempted a candle fetch

    def test_voided_market_marked_seen_but_not_resolved(self):
        market = _market("M1", CLOSE_ISO, result="")  # settled but voided / no result
        client = FakeClient(settled=[market])
        state = {**_fresh_state(), "watermark_ts": 0}
        result = run_cycle(client, NOW, state)
        assert result["resolved"] == []
        assert result["seen"] == ["M1"]  # tracked -- it will never resolve differently
        assert result["watermark_ts"] == CLOSE_TS + 1

    def test_already_seen_ticker_is_skipped_entirely(self):
        market = _market("M1", CLOSE_ISO, result="yes")
        client = FakeClient(settled=[market], candles={"M1": [_candle("0.30")]})
        state = {**_fresh_state(), "watermark_ts": 0, "seen": ["M1"]}
        result = run_cycle(client, NOW, state)
        assert result["resolved"] == []
        assert client.candlestick_tickers == []  # never even attempted
        assert result["watermark_ts"] == CLOSE_TS + 1

    def test_seen_later_market_advances_watermark_during_rollback_catchup(self):
        earlier = _market("M1", "2026-07-05T00:00:00Z", result="yes")
        later = _market("M2", CLOSE_ISO, result="yes")
        client = FakeClient(settled=[earlier, later], candles={"M1": [_candle("0.30")]})
        state = {**_fresh_state(), "watermark_ts": 0, "seen": ["M2"]}
        result = run_cycle(client, NOW, state)
        assert result["watermark_ts"] == CLOSE_TS + 1

    def test_candlestick_request_failure_is_not_marked_seen_and_rolls_back_watermark(self):
        """candles={"M1": None} simulates a TRANSIENT request failure
        (price_at_t24h raises RuntimeError) -- must be retried, not
        excluded."""
        market = _market("M1", CLOSE_ISO, result="yes")
        client = FakeClient(settled=[market], candles={"M1": None})
        state = {**_fresh_state(), "watermark_ts": 0}
        result = run_cycle(client, NOW, state)
        assert result["resolved"] == []
        assert result["seen"] == []
        assert result["_failed_tickers"] == ["M1"]
        assert result["watermark_ts"] == CLOSE_TS - 1  # rolled back so it's retried next cycle

    def test_no_candle_data_is_permanent_not_a_retryable_failure(self):
        """candles={} (or the ticker simply absent) simulates a SUCCESSFUL
        request that genuinely found zero candles in the window -- e.g. the
        market's whole lifetime was shorter than the T-24h lookback. This
        is PERMANENT: must be marked seen and excluded, NOT added to
        _failed_tickers (which would retry forever for data that will never
        exist), and must NOT roll back the watermark."""
        market = _market("M1", CLOSE_ISO, result="yes")
        client = FakeClient(settled=[market], candles={})
        state = {**_fresh_state(), "watermark_ts": 0}
        result = run_cycle(client, NOW, state)
        assert result["resolved"] == []
        assert result["seen"] == ["M1"]          # excluded for good
        assert result["_failed_tickers"] == []   # NOT a retryable failure
        assert result["watermark_ts"] == CLOSE_TS + 1  # advances normally, no rollback

    def test_failed_ticker_is_retried_and_succeeds_next_cycle(self):
        market = _market("M1", CLOSE_ISO, result="yes")
        client1 = FakeClient(settled=[market], candles={"M1": None})  # transient failure this cycle
        result1 = run_cycle(client1, NOW, {**_fresh_state(), "watermark_ts": 0})
        assert result1["_failed_tickers"] == ["M1"]
        state1 = _persistable(result1)

        later = NOW + timedelta(days=7)
        client2 = FakeClient(settled=[market], candles={"M1": [_candle("0.30")]})  # succeeds now
        result2 = run_cycle(client2, later, state1)
        assert [r["ticker"] for r in result2["resolved"]] == ["M1"]
        assert result2["_new_resolved"] == 1
        assert result2["_failed_tickers"] == []

    def test_pagination_incomplete_error_propagates_uncaught(self):
        """run_cycle does not swallow a discovery-level pagination failure
        -- the caller (main()) is responsible for the whole-cycle,
        cron-safe abort. Silently continuing past an incomplete settled-
        markets fetch risks a biased verdict or a watermark advanced past
        markets never actually seen."""
        client = FakeClient(raise_pagination_error=True)
        with pytest.raises(RuntimeError):
            run_cycle(client, NOW, _fresh_state())

    def test_failed_cycle_preserves_previous_verdict(self):
        market = _market("M1", CLOSE_ISO, result="yes")
        client = FakeClient(settled=[market], candles={"M1": None})
        state = {**_fresh_state(), "watermark_ts": 0, "last_verdict": "pursue"}
        result = run_cycle(client, NOW, state)
        assert result["_failed_tickers"] == ["M1"]
        assert result["last_verdict"] == "pursue"

    def test_resolved_history_accumulates_across_cycles(self):
        market1 = _market("M1", CLOSE_ISO, result="yes")
        client1 = FakeClient(settled=[market1], candles={"M1": [_candle("0.30")]})
        result1 = run_cycle(client1, NOW, {**_fresh_state(), "watermark_ts": 0})
        state1 = _persistable(result1)

        close2_iso = "2026-07-13T00:00:00Z"
        market2 = _market("M2", close2_iso, result="no")
        client2 = FakeClient(settled=[market2], candles={"M2": [_candle("0.20")]})
        later = NOW + timedelta(days=7)
        result2 = run_cycle(client2, later, state1)
        assert sorted(r["ticker"] for r in result2["resolved"]) == ["M1", "M2"]
        assert result2["_stats"].n == 2

    def test_verdict_flip_is_visible_via_prev_verdict(self):
        state = {**_fresh_state(), "watermark_ts": 0, "last_verdict": "pursue"}
        result = run_cycle(FakeClient(settled=[]), NOW, state)
        assert result["_prev_verdict"] == "pursue"
        assert result["last_verdict"] == "continue"  # n=0 -> always continue

    def test_first_seen_ts_set_once_and_preserved(self):
        result1 = run_cycle(FakeClient(settled=[]), NOW, _fresh_state())
        assert result1["first_seen_ts"] == NOW.isoformat()
        state1 = _persistable(result1)

        later = NOW + timedelta(days=7)
        result2 = run_cycle(FakeClient(settled=[]), later, state1)
        assert result2["first_seen_ts"] == NOW.isoformat()  # unchanged, not overwritten


# --------------------------------------------------------------------------- #
# _should_alert
# --------------------------------------------------------------------------- #
class TestShouldAlert:
    def test_alerts_on_new_resolution(self):
        assert _should_alert(1, "continue", "continue", False) is True

    def test_alerts_on_verdict_change(self):
        assert _should_alert(0, "continue", "pursue", False) is True

    def test_alerts_when_forced(self):
        assert _should_alert(0, "continue", "continue", True) is True

    def test_no_alert_when_nothing_changed(self):
        assert _should_alert(0, "continue", "continue", False) is False


# --------------------------------------------------------------------------- #
# format_message / _format_failure_notice
# --------------------------------------------------------------------------- #
class TestFormatMessage:
    def test_includes_core_fields(self):
        stats = OosStats(n=120, mean_richness_pts=10.3, z=2.41, by_category={"KXEARNINGSMENTION": (120, 10.3)})
        msg = format_message(stats, "pursue", new_resolved=5)
        assert "n=120" in msg
        assert "PURSUE" in msg
        assert "New settlements resolved this run: 5" in msg
        assert "KXEARNINGSMENTION" in msg

    def test_handles_empty_by_category(self):
        stats = OosStats(n=0, mean_richness_pts=0.0, z=0.0, by_category={})
        msg = format_message(stats, "continue", new_resolved=0)
        assert "CONTINUE" in msg
        assert "n=0" in msg


class TestFormatFailureNotice:
    def test_includes_count_and_tickers(self):
        msg = _format_failure_notice(["M1", "M2"])
        assert "2 market(s)" in msg
        assert "M1" in msg and "M2" in msg
        assert "retry" in msg.lower()

    def test_truncates_long_list(self):
        tickers = [f"M{i}" for i in range(15)]
        msg = _format_failure_notice(tickers)
        assert "+5 more" in msg
