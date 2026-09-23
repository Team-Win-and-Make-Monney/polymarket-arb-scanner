"""Integration tests for the Layer-4 strategy wiring in continuous.py.

The four Layer-4 scans (imbalance, news-snipe, correlated, time-decay) were
silently disabled in continuous mode: the inline loop blocks called the scans
with stale kwargs (``poly_markets=`` / ``kalshi_data=`` / ``min_profit=``) and
wrong config names, and a broad ``except`` swallowed the resulting
TypeError / ImportError. Flipping the feature flag therefore did nothing.

These tests assert that flipping each flag actually invokes the corresponding
scan — patched with ``autospec=True`` so a stale signature raises instead of
silently passing — and that the scan's result propagates back (proving the call
is not being swallowed). A flag-off case asserts the scan is not called.
"""

import os
import sys
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# Mock heavy SDK-dependent modules before importing continuous (mirrors the
# established pattern in tests/test_continuous_sprint3_wiring.py).
_modules_to_mock = ["kalshi_api", "polymarket_api", "dashboard", "display", "recovery"]
_saved_modules = {name: sys.modules[name] for name in _modules_to_mock if name in sys.modules}
for _mod_name in _modules_to_mock:
    sys.modules[_mod_name] = MagicMock()
sys.modules["dashboard"].state = MagicMock()

import continuous  # noqa: E402
import config as config_mod  # noqa: E402

# Restore for any sibling test files
for _mod_name in _modules_to_mock:
    if _mod_name in _saved_modules:
        sys.modules[_mod_name] = _saved_modules[_mod_name]
    elif _mod_name in sys.modules:
        del sys.modules[_mod_name]


# A minimal Polymarket market dict so _build_poly_markets_by_key yields a
# non-empty markets_by_key (keyed "polymarket-cond-1").
_POLY_MARKET = {
    "condition_id": "cond-1",
    "question": "Will X happen?",
    "clobTokenIds": ["token-yes", "token-no"],
}


class TestContinuousImportsLayer4Scans:
    """Regression guard — the four scans must be bound as continuous module
    attributes (required both for the wiring to exist and to be patchable)."""

    @pytest.mark.parametrize(
        "name",
        ["scan_imbalance", "scan_news_snipe", "scan_correlated", "scan_time_decay"],
    )
    def test_scan_imported(self, name):
        assert hasattr(continuous, name)

    @pytest.mark.parametrize(
        "name",
        [
            "_scan_imbalance_layer4",
            "_scan_news_snipe_layer4",
            "_scan_correlated_layer4",
            "_scan_time_decay_layer4",
        ],
    )
    def test_helper_defined(self, name):
        assert callable(getattr(continuous, name))


class TestBuildPolyMarketsByKey:
    def test_indexes_by_condition_id(self):
        out = continuous._build_poly_markets_by_key([_POLY_MARKET])
        assert out == {"polymarket-cond-1": _POLY_MARKET}

    def test_supports_camelcase_condition_id(self):
        out = continuous._build_poly_markets_by_key([{"conditionId": "abc"}])
        assert "polymarket-abc" in out

    def test_skips_markets_without_condition_id(self):
        out = continuous._build_poly_markets_by_key([{"question": "no id"}])
        assert out == {}

    def test_none_input_is_safe(self):
        assert continuous._build_poly_markets_by_key(None) == {}


class TestParseCorrelatedPairs:
    def test_parses_json_string(self):
        pairs = continuous._parse_correlated_pairs('[["a", "b"], ["c", "d"]]')
        assert pairs == [("a", "b"), ("c", "d")]

    def test_accepts_already_parsed_list(self):
        pairs = continuous._parse_correlated_pairs([["a", "b"]])
        assert pairs == [("a", "b")]

    def test_empty_default_returns_empty(self):
        assert continuous._parse_correlated_pairs("[]") == []

    def test_malformed_json_returns_empty(self):
        assert continuous._parse_correlated_pairs("not json") == []

    def test_drops_wrong_arity_pairs(self):
        assert continuous._parse_correlated_pairs('[["a"], ["b", "c", "d"]]') == []


class TestImbalanceWiring:
    def test_invoked_when_flag_on(self, monkeypatch):
        monkeypatch.setattr(config_mod, "IMBALANCE_ENABLED", True)
        monkeypatch.setattr(config_mod, "IMBALANCE_RATIO", 3.0)
        with patch.object(continuous, "scan_imbalance", autospec=True) as m:
            m.return_value = [{"type": "Imbalance"}]
            out = continuous._scan_imbalance_layer4([_POLY_MARKET], {}, "all")
        m.assert_called_once()
        call = m.call_args
        assert "polymarket-cond-1" in call.args[0]
        assert call.kwargs["min_ratio"] == 3.0
        assert out == [{"type": "Imbalance"}]  # result propagated, not swallowed

    def test_skipped_when_flag_off(self, monkeypatch):
        monkeypatch.setattr(config_mod, "IMBALANCE_ENABLED", False)
        with patch.object(continuous, "scan_imbalance", autospec=True) as m:
            out = continuous._scan_imbalance_layer4([_POLY_MARKET], {}, "all")
        m.assert_not_called()
        assert out == []

    def test_skipped_when_mode_excludes(self, monkeypatch):
        monkeypatch.setattr(config_mod, "IMBALANCE_ENABLED", True)
        with patch.object(continuous, "scan_imbalance", autospec=True) as m:
            out = continuous._scan_imbalance_layer4([_POLY_MARKET], {}, "binary")
        m.assert_not_called()
        assert out == []


class TestNewsSnipeWiring:
    def test_invoked_when_flag_on(self, monkeypatch):
        monkeypatch.setattr(config_mod, "NEWS_SNIPE_ENABLED", True)
        monkeypatch.setattr(config_mod, "FINNHUB_API_KEY", "test-key")
        monkeypatch.setattr(config_mod, "FUZZY_MATCH_THRESHOLD", 72)
        fake_finnhub = MagicMock()
        monkeypatch.setitem(sys.modules, "finnhub_api", fake_finnhub)
        with patch.object(continuous, "scan_news_snipe", autospec=True) as m:
            m.return_value = [{"type": "NewsSnipe"}]
            out = continuous._scan_news_snipe_layer4([_POLY_MARKET], "all")
        m.assert_called_once()
        call = m.call_args
        assert "polymarket-cond-1" in call.args[0]
        assert call.kwargs["fuzzy_threshold"] == 72
        assert out == [{"type": "NewsSnipe"}]
        # The Finnhub client is constructed from the configured key.
        fake_finnhub.FinnhubNewsClient.assert_called_once_with(api_key="test-key")

    def test_skipped_without_api_key(self, monkeypatch):
        monkeypatch.setattr(config_mod, "NEWS_SNIPE_ENABLED", True)
        monkeypatch.setattr(config_mod, "FINNHUB_API_KEY", "")
        with patch.object(continuous, "scan_news_snipe", autospec=True) as m:
            out = continuous._scan_news_snipe_layer4([_POLY_MARKET], "all")
        m.assert_not_called()
        assert out == []

    def test_skipped_when_flag_off(self, monkeypatch):
        monkeypatch.setattr(config_mod, "NEWS_SNIPE_ENABLED", False)
        monkeypatch.setattr(config_mod, "FIRECRAWL_NEWS_ENABLED", False)
        with patch.object(continuous, "scan_news_snipe", autospec=True) as m:
            out = continuous._scan_news_snipe_layer4([_POLY_MARKET], "all")
        m.assert_not_called()
        assert out == []

    def test_firecrawl_source_invoked_when_flag_on(self, monkeypatch):
        # Finnhub off, Firecrawl on -> scan runs once with the Firecrawl client.
        monkeypatch.setattr(config_mod, "NEWS_SNIPE_ENABLED", False)
        monkeypatch.setattr(config_mod, "FINNHUB_API_KEY", "")
        monkeypatch.setattr(config_mod, "FIRECRAWL_NEWS_ENABLED", True)
        monkeypatch.setattr(config_mod, "FIRECRAWL_API_KEY", "fc-test-key")
        monkeypatch.setattr(config_mod, "FUZZY_MATCH_THRESHOLD", 72)
        fake_fc = MagicMock()
        monkeypatch.setitem(sys.modules, "firecrawl_news_client", fake_fc)
        with patch.object(continuous, "scan_news_snipe", autospec=True) as m:
            m.return_value = [{"type": "NewsSnipe", "src": "firecrawl"}]
            out = continuous._scan_news_snipe_layer4([_POLY_MARKET], "all")
        m.assert_called_once()
        assert out == [{"type": "NewsSnipe", "src": "firecrawl"}]
        fake_fc.FirecrawlNewsClient.assert_called_once_with(api_key="fc-test-key")

    def test_both_sources_merge(self, monkeypatch):
        # Both gates on -> scan runs once per client and results merge.
        monkeypatch.setattr(config_mod, "NEWS_SNIPE_ENABLED", True)
        monkeypatch.setattr(config_mod, "FINNHUB_API_KEY", "finnhub-key")
        monkeypatch.setattr(config_mod, "FIRECRAWL_NEWS_ENABLED", True)
        monkeypatch.setattr(config_mod, "FIRECRAWL_API_KEY", "fc-key")
        monkeypatch.setattr(config_mod, "FUZZY_MATCH_THRESHOLD", 72)
        monkeypatch.setitem(sys.modules, "finnhub_api", MagicMock())
        monkeypatch.setitem(sys.modules, "firecrawl_news_client", MagicMock())
        with patch.object(continuous, "scan_news_snipe", autospec=True) as m:
            m.side_effect = [
                [{"type": "NewsSnipe", "src": "finnhub"}],
                [{"type": "NewsSnipe", "src": "firecrawl"}],
            ]
            out = continuous._scan_news_snipe_layer4([_POLY_MARKET], "all")
        assert m.call_count == 2
        srcs = sorted(o["src"] for o in out)
        assert srcs == ["finnhub", "firecrawl"]

    def test_one_client_failure_does_not_kill_other(self, monkeypatch):
        # Finnhub client raises during scan; Firecrawl still returns its opps.
        monkeypatch.setattr(config_mod, "NEWS_SNIPE_ENABLED", True)
        monkeypatch.setattr(config_mod, "FINNHUB_API_KEY", "finnhub-key")
        monkeypatch.setattr(config_mod, "FIRECRAWL_NEWS_ENABLED", True)
        monkeypatch.setattr(config_mod, "FIRECRAWL_API_KEY", "fc-key")
        monkeypatch.setattr(config_mod, "FUZZY_MATCH_THRESHOLD", 72)
        monkeypatch.setitem(sys.modules, "finnhub_api", MagicMock())
        monkeypatch.setitem(sys.modules, "firecrawl_news_client", MagicMock())
        with patch.object(continuous, "scan_news_snipe", autospec=True) as m:
            m.side_effect = [
                RuntimeError("finnhub boom"),
                [{"type": "NewsSnipe", "src": "firecrawl"}],
            ]
            out = continuous._scan_news_snipe_layer4([_POLY_MARKET], "all")
        assert m.call_count == 2
        assert out == [{"type": "NewsSnipe", "src": "firecrawl"}]


class TestCorrelatedWiring:
    def test_invoked_when_flag_on(self, monkeypatch):
        monkeypatch.setattr(config_mod, "CORRELATED_ENABLED", True)
        monkeypatch.setattr(config_mod, "CORRELATED_PAIRS", '[["cond-1", "cond-2"]]')
        monkeypatch.setattr(config_mod, "CORRELATION_DIVERGENCE_THRESHOLD", 0.10)
        with patch.object(continuous, "scan_correlated", autospec=True) as m:
            m.return_value = [{"type": "Correlated"}]
            out = continuous._scan_correlated_layer4([_POLY_MARKET], {}, "all")
        m.assert_called_once()
        call = m.call_args
        assert "polymarket-cond-1" in call.args[0]
        assert call.args[1] == [("cond-1", "cond-2")]  # parsed JSON pairs
        assert call.kwargs["min_spread"] == 0.10
        assert out == [{"type": "Correlated"}]

    def test_skipped_when_no_pairs_configured(self, monkeypatch):
        monkeypatch.setattr(config_mod, "CORRELATED_ENABLED", True)
        monkeypatch.setattr(config_mod, "CORRELATED_PAIRS", "[]")
        with patch.object(continuous, "scan_correlated", autospec=True) as m:
            out = continuous._scan_correlated_layer4([_POLY_MARKET], {}, "all")
        m.assert_not_called()
        assert out == []

    def test_skipped_when_flag_off(self, monkeypatch):
        monkeypatch.setattr(config_mod, "CORRELATED_ENABLED", False)
        with patch.object(continuous, "scan_correlated", autospec=True) as m:
            out = continuous._scan_correlated_layer4([_POLY_MARKET], {}, "all")
        m.assert_not_called()
        assert out == []


class TestTimeDecayWiring:
    def test_invoked_when_flag_on(self, monkeypatch):
        monkeypatch.setattr(config_mod, "TIME_DECAY_ENABLED", True)
        monkeypatch.setattr(config_mod, "TIME_DECAY_MIN_HOURS_EXPIRY", 48)
        monkeypatch.setattr(config_mod, "TIME_DECAY_MIN_CONSENSUS", 0.90)
        monkeypatch.setattr(config_mod, "TIME_DECAY_BUY_BELOW_PRICE", 0.95)
        aggregator = MagicMock()
        with patch.object(continuous, "scan_time_decay", autospec=True) as m:
            m.return_value = [{"type": "TimeDecay"}]
            out = continuous._scan_time_decay_layer4(
                [_POLY_MARKET], {}, "all", signal_aggregator=aggregator
            )
        m.assert_called_once()
        call = m.call_args
        assert "polymarket-cond-1" in call.args[0]
        assert call.args[1] is aggregator
        assert call.kwargs["min_hours_to_expiry"] == 48
        assert call.kwargs["min_consensus"] == 0.90
        assert call.kwargs["buy_below_price"] == 0.95
        assert out == [{"type": "TimeDecay"}]

    def test_skipped_when_flag_off(self, monkeypatch):
        monkeypatch.setattr(config_mod, "TIME_DECAY_ENABLED", False)
        with patch.object(continuous, "scan_time_decay", autospec=True) as m:
            out = continuous._scan_time_decay_layer4(
                [_POLY_MARKET], {}, "all", signal_aggregator=MagicMock()
            )
        m.assert_not_called()
        assert out == []
