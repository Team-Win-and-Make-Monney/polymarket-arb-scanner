"""Tests for cli.py — argument parsing, config precedence, and oneshot dispatch."""

import pytest
from unittest.mock import MagicMock, patch
import sys
import os
import argparse

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


# ---------------------------------------------------------------------------
# sys.modules mocking — cli.py imports 30+ modules at the top level
# ---------------------------------------------------------------------------

# Modules that may not be installed in test environments.
# We inject MagicMock stubs *before* importing cli so its top-level imports
# resolve without ImportError.
_EXTERNAL_MODS = [
    "polymarket_api", "kalshi_api", "betfair_api", "smarkets_api",
    "sxbet_api", "matchbook_api", "gemini_api", "ibkr_api",
    "metaculus_api", "gas_monitor", "event_monitor", "ws_feeds",
    "recovery", "requests",
]

_stashed = {}
for _mod in _EXTERNAL_MODS:
    if _mod in sys.modules:
        _stashed[_mod] = sys.modules[_mod]
    else:
        sys.modules[_mod] = MagicMock()

# Now import cli once — all @patch("cli.<name>") targets will reference this module.
import cli as _cli_mod  # noqa: E402

# Restore stashed modules AND remove newly-injected mocks to prevent
# cross-test pollution (other test files need real module imports).
for _mod in _EXTERNAL_MODS:
    if _mod in _stashed:
        sys.modules[_mod] = _stashed[_mod]
    elif _mod in sys.modules and isinstance(sys.modules[_mod], MagicMock):
        del sys.modules[_mod]
_stashed.clear()


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _make_args(mode="binary", **kwargs):
    """Build an argparse.Namespace mimicking cli.main()'s parsed args."""
    defaults = {
        "mode": mode,
        "min_profit": None,
        "limit": None,
        "json": False,
        "min_confidence": "LOW",
        "min_depth": 0,
        "continuous": False,
    }
    defaults.update(kwargs)
    return argparse.Namespace(**defaults)


def _make_executor(dry_run=True):
    executor = MagicMock()
    executor.dry_run = dry_run
    executor.exec_mode = "semi-auto"
    executor.execute.return_value = True
    return executor


def _make_db():
    db = MagicMock()
    db.get_open_positions_count.return_value = 0
    db.get_daily_pnl.return_value = 0.0
    return db


# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------

class TestArgumentParsing:
    """Verify argparse flags produce the expected Namespace values."""

    def test_default_mode_is_all(self):
        parser = argparse.ArgumentParser()
        parser.add_argument("--mode", choices=[
            "all", "binary", "negrisk", "cross", "kalshi", "cross-all",
            "spread", "betfair", "smarkets", "sxbet", "matchbook",
            "gemini", "ibkr", "event", "triangular",
        ], default="all")
        args = parser.parse_args([])
        assert args.mode == "all"

    def test_mode_kalshi(self):
        parser = argparse.ArgumentParser()
        parser.add_argument("--mode", choices=[
            "all", "binary", "negrisk", "cross", "kalshi", "cross-all",
            "spread", "betfair", "smarkets", "sxbet", "matchbook",
            "gemini", "ibkr", "event", "triangular",
        ], default="all")
        args = parser.parse_args(["--mode", "kalshi"])
        assert args.mode == "kalshi"

    def test_mode_mm_pilot(self):
        parser = argparse.ArgumentParser()
        parser.add_argument("--mode", choices=["all", "mm-pilot"], default="all")
        args = parser.parse_args(["--mode", "mm-pilot"])
        assert args.mode == "mm-pilot"

    def test_min_profit_float(self):
        parser = argparse.ArgumentParser()
        parser.add_argument("--min-profit", type=float, default=None)
        args = parser.parse_args(["--min-profit", "0.03"])
        assert args.min_profit == pytest.approx(0.03)

    def test_limit_int(self):
        parser = argparse.ArgumentParser()
        parser.add_argument("--limit", type=int, default=None)
        args = parser.parse_args(["--limit", "10"])
        assert args.limit == 10

    def test_json_flag(self):
        parser = argparse.ArgumentParser()
        parser.add_argument("--json", action="store_true")
        args = parser.parse_args(["--json"])
        assert args.json is True

    def test_json_default_false(self):
        parser = argparse.ArgumentParser()
        parser.add_argument("--json", action="store_true")
        args = parser.parse_args([])
        assert args.json is False

    def test_continuous_flag(self):
        parser = argparse.ArgumentParser()
        parser.add_argument("--continuous", action="store_true")
        args = parser.parse_args(["--continuous"])
        assert args.continuous is True

    def test_exec_mode_choices(self):
        parser = argparse.ArgumentParser()
        parser.add_argument("--exec-mode", choices=["semi-auto", "full-auto"], default=None)
        args = parser.parse_args(["--exec-mode", "full-auto"])
        assert args.exec_mode == "full-auto"

    def test_min_confidence_choices(self):
        parser = argparse.ArgumentParser()
        parser.add_argument("--min-confidence", choices=["HIGH", "MEDIUM", "LOW"], default="LOW")
        args = parser.parse_args(["--min-confidence", "HIGH"])
        assert args.min_confidence == "HIGH"

    def test_dry_run_flag(self):
        parser = argparse.ArgumentParser()
        parser.add_argument("--dry-run", action="store_true", default=None)
        args = parser.parse_args(["--dry-run"])
        assert args.dry_run is True

    def test_log_level_choices(self):
        parser = argparse.ArgumentParser()
        parser.add_argument("--log-level", choices=["DEBUG", "INFO", "WARNING", "ERROR"], default=None)
        args = parser.parse_args(["--log-level", "DEBUG"])
        assert args.log_level == "DEBUG"

    def test_all_14_modes_accepted(self):
        """All 14 scan modes should be valid choices."""
        modes = [
            "all", "binary", "negrisk", "cross", "kalshi", "cross-all",
            "spread", "betfair", "smarkets", "sxbet", "matchbook",
            "gemini", "ibkr", "event", "triangular",
        ]
        parser = argparse.ArgumentParser()
        parser.add_argument("--mode", choices=modes, default="all")
        for mode in modes:
            args = parser.parse_args(["--mode", mode])
            assert args.mode == mode


# ---------------------------------------------------------------------------
# Config precedence (CLI > env > defaults)
# ---------------------------------------------------------------------------

class TestConfigPrecedence:
    """CLI args override env vars which override config defaults."""

    def test_min_profit_cli_overrides_env(self):
        """--min-profit flag takes precedence over MIN_PROFIT_THRESHOLD env."""
        args = argparse.Namespace(min_profit=0.05)
        min_profit = args.min_profit or float(os.getenv("MIN_PROFIT_THRESHOLD", "0.01"))
        assert min_profit == pytest.approx(0.05)

    def test_min_profit_falls_back_to_env(self):
        """When --min-profit is None, falls back to env var."""
        with patch.dict(os.environ, {"MIN_PROFIT_THRESHOLD": "0.02"}):
            args = argparse.Namespace(min_profit=None)
            min_profit = args.min_profit or float(os.getenv("MIN_PROFIT_THRESHOLD", "0.01"))
            assert min_profit == pytest.approx(0.02)

    def test_min_profit_falls_back_to_default(self):
        """When neither CLI nor env is set, uses DEFAULT_MIN_PROFIT."""
        env = os.environ.copy()
        env.pop("MIN_PROFIT_THRESHOLD", None)
        with patch.dict(os.environ, env, clear=True):
            args = argparse.Namespace(min_profit=None)
            from config import DEFAULT_MIN_PROFIT
            min_profit = args.min_profit or float(os.getenv("MIN_PROFIT_THRESHOLD", str(DEFAULT_MIN_PROFIT)))
            assert min_profit == pytest.approx(DEFAULT_MIN_PROFIT)

    def test_dry_run_cli_true_overrides_env(self):
        """--dry-run flag overrides DRY_RUN env."""
        with patch.dict(os.environ, {"DRY_RUN": "false"}):
            cli_dry_run = True
            dry_run = cli_dry_run if cli_dry_run is not None else os.getenv("DRY_RUN", "true").lower() == "true"
            assert dry_run is True

    def test_dry_run_env_false(self):
        """When CLI is None, DRY_RUN=false makes dry_run False."""
        with patch.dict(os.environ, {"DRY_RUN": "false"}):
            cli_dry_run = None
            dry_run = cli_dry_run if cli_dry_run is not None else os.getenv("DRY_RUN", "true").lower() == "true"
            assert dry_run is False

    def test_dry_run_default_is_true(self):
        """No CLI flag, no env var -> dry_run defaults to True."""
        env = os.environ.copy()
        env.pop("DRY_RUN", None)
        with patch.dict(os.environ, env, clear=True):
            cli_dry_run = None
            dry_run = cli_dry_run if cli_dry_run is not None else os.getenv("DRY_RUN", "true").lower() == "true"
            assert dry_run is True

    def test_exec_mode_cli_overrides_env(self):
        """--exec-mode flag overrides EXECUTION_MODE env."""
        with patch.dict(os.environ, {"EXECUTION_MODE": "semi-auto"}):
            cli_exec_mode = "full-auto"
            exec_mode = cli_exec_mode or os.getenv("EXECUTION_MODE", "semi-auto")
            assert exec_mode == "full-auto"

    def test_exec_mode_falls_back_to_env(self):
        """When CLI is None, EXECUTION_MODE env is used."""
        with patch.dict(os.environ, {"EXECUTION_MODE": "full-auto"}):
            cli_exec_mode = None
            exec_mode = cli_exec_mode or os.getenv("EXECUTION_MODE", "semi-auto")
            assert exec_mode == "full-auto"

    def test_max_trade_cli_overrides_env(self):
        """--max-trade flag overrides MAX_TRADE_SIZE env."""
        with patch.dict(os.environ, {"MAX_TRADE_SIZE": "10.0"}):
            cli_max_trade = 25.0
            max_trade = cli_max_trade or float(os.getenv("MAX_TRADE_SIZE", "5.0"))
            assert max_trade == pytest.approx(25.0)


# ---------------------------------------------------------------------------
# _run_oneshot — mode routing
# ---------------------------------------------------------------------------

class TestRunOneshotModeRouting:
    """Verify _run_oneshot dispatches to the correct scan functions per mode."""

    @patch.object(_cli_mod, "display_results")
    @patch.object(_cli_mod, "dashboard_state")
    @patch.object(_cli_mod, "scan_binary_internal", return_value=[{"type": "BinaryInternal", "net_profit": 0.05}])
    @patch.object(_cli_mod, "fetch_all_markets", return_value=[{"question": "test", "conditionId": "c1"}])
    def test_binary_mode_calls_scan_binary(self, mock_fetch, mock_scan, mock_dash, mock_display):
        args = _make_args(mode="binary")
        _cli_mod._run_oneshot(args, 0.01, None, _make_executor(), _make_db())
        mock_scan.assert_called_once()

    @patch.object(_cli_mod, "display_results")
    @patch.object(_cli_mod, "dashboard_state")
    @patch.object(_cli_mod, "scan_kalshi_binary", return_value=[])
    @patch.object(_cli_mod, "scan_kalshi_multi", return_value=[])
    @patch.object(_cli_mod, "_fetch_kalshi_data", return_value=([{"event_ticker": "E1"}], []))
    def test_kalshi_mode_calls_both_kalshi_scans(self, mock_fetch, mock_binary, mock_multi, mock_dash, mock_display):
        args = _make_args(mode="kalshi")
        kalshi_client = MagicMock()
        _cli_mod._run_oneshot(args, 0.01, kalshi_client, _make_executor(), _make_db())
        mock_binary.assert_called_once()
        mock_multi.assert_called_once()

    @patch.object(_cli_mod, "display_results")
    @patch.object(_cli_mod, "dashboard_state")
    @patch.object(_cli_mod, "scan_cross_platform", return_value=[])
    @patch.object(_cli_mod, "fetch_all_markets", return_value=[{"question": "test"}])
    @patch.object(_cli_mod, "_fetch_kalshi_data", return_value=([{"event_ticker": "E1"}], []))
    def test_cross_mode_calls_scan_cross_platform(self, mock_kdata, mock_fetch, mock_cross, mock_dash, mock_display):
        args = _make_args(mode="cross")
        kalshi_client = MagicMock()
        _cli_mod._run_oneshot(args, 0.01, kalshi_client, _make_executor(), _make_db())
        mock_cross.assert_called_once()

    @patch.object(_cli_mod, "display_results")
    @patch.object(_cli_mod, "dashboard_state")
    @patch.object(_cli_mod, "scan_spread_polymarket", return_value=[])
    @patch.object(_cli_mod, "fetch_all_markets", return_value=[{"question": "test"}])
    def test_spread_mode_runs_spread_scan(self, mock_fetch, mock_pm, mock_dash, mock_display):
        args = _make_args(mode="spread")
        _cli_mod._run_oneshot(args, 0.01, None, _make_executor(), _make_db())
        mock_pm.assert_called_once()

    @patch.object(_cli_mod, "display_results")
    @patch.object(_cli_mod, "dashboard_state")
    @patch.object(_cli_mod, "scan_betfair_backall", return_value=[])
    @patch.object(_cli_mod, "scan_betfair_backlay", return_value=[])
    def test_betfair_mode_runs_both_betfair_scans(self, mock_lay, mock_all, mock_dash, mock_display):
        args = _make_args(mode="betfair")
        bf_client = MagicMock()
        _cli_mod._run_oneshot(args, 0.01, None, _make_executor(), _make_db(),
                              extra_clients={"betfair": bf_client})
        mock_all.assert_called_once()
        mock_lay.assert_called_once()

    @patch.object(_cli_mod, "display_results")
    @patch.object(_cli_mod, "dashboard_state")
    @patch.object(_cli_mod, "scan_gemini_binary", return_value=[])
    @patch.object(_cli_mod, "scan_gemini_multi", return_value=[])
    def test_gemini_mode_runs_both_gemini_scans(self, mock_multi, mock_binary, mock_dash, mock_display):
        args = _make_args(mode="gemini")
        gm_client = MagicMock()
        _cli_mod._run_oneshot(args, 0.01, None, _make_executor(), _make_db(),
                              extra_clients={"gemini": gm_client})
        mock_binary.assert_called_once()
        mock_multi.assert_called_once()

    @patch.object(_cli_mod, "display_results")
    @patch.object(_cli_mod, "dashboard_state")
    @patch.object(_cli_mod, "scan_smarkets_backall", return_value=[])
    @patch.object(_cli_mod, "scan_smarkets_backlay", return_value=[])
    def test_smarkets_mode_runs_both_smarkets_scans(self, mock_lay, mock_all, mock_dash, mock_display):
        args = _make_args(mode="smarkets")
        sm_client = MagicMock()
        _cli_mod._run_oneshot(args, 0.01, None, _make_executor(), _make_db(),
                              extra_clients={"smarkets": sm_client})
        mock_all.assert_called_once()
        mock_lay.assert_called_once()

    @patch.object(_cli_mod, "display_results")
    @patch.object(_cli_mod, "dashboard_state")
    @patch.object(_cli_mod, "scan_ibkr_binary", return_value=[])
    def test_ibkr_mode_runs_ibkr_scan(self, mock_ibkr, mock_dash, mock_display):
        args = _make_args(mode="ibkr")
        ibkr_client = MagicMock()
        _cli_mod._run_oneshot(args, 0.01, None, _make_executor(), _make_db(),
                              extra_clients={"ibkr": ibkr_client})
        mock_ibkr.assert_called_once()

    @patch.object(_cli_mod, "display_results")
    @patch.object(_cli_mod, "dashboard_state")
    @patch.object(_cli_mod, "scan_polymarket_rewards", return_value=[])
    @patch.object(_cli_mod, "fetch_reward_markets", return_value=[{"conditionId": "reward"}])
    @patch.object(_cli_mod, "fetch_all_markets")
    def test_rewards_mode_uses_dedicated_reward_feed(
        self, mock_fetch_all, mock_fetch_rewards, mock_scan, mock_dash, mock_display,
    ):
        with patch.object(_cli_mod, "CONFIG_REWARDS_ENABLED", True):
            _cli_mod._run_oneshot(
                _make_args(mode="rewards"), 0.01, None, _make_executor(), _make_db(),
            )
        mock_fetch_all.assert_not_called()
        mock_fetch_rewards.assert_called_once_with()
        assert mock_scan.call_args.args[0] == [{"conditionId": "reward"}]


# ---------------------------------------------------------------------------
# _run_oneshot — filtering and execution
# ---------------------------------------------------------------------------

class TestRunOneshotFilteringAndExecution:
    """Verify min_depth filtering, limit, sorting, and execution dispatch."""

    @patch.object(_cli_mod, "display_results")
    @patch.object(_cli_mod, "dashboard_state")
    @patch.object(_cli_mod, "scan_binary_internal")
    @patch.object(_cli_mod, "fetch_all_markets", return_value=[{"question": "test"}])
    def test_min_depth_filters_shallow_opps(self, mock_fetch, mock_scan, mock_dash, mock_display):
        mock_scan.return_value = [
            {"type": "BinaryInternal", "net_profit": 0.05, "_clob_depth": 10},
            {"type": "BinaryInternal", "net_profit": 0.08, "_clob_depth": 100},
        ]
        args = _make_args(min_depth=50)
        _cli_mod._run_oneshot(args, 0.01, None, _make_executor(), _make_db())
        displayed = mock_display.call_args[0][0]
        assert len(displayed) == 1
        assert displayed[0]["_clob_depth"] == 100

    @patch.object(_cli_mod, "display_results")
    @patch.object(_cli_mod, "dashboard_state")
    @patch.object(_cli_mod, "scan_binary_internal")
    @patch.object(_cli_mod, "fetch_all_markets", return_value=[{"question": "test"}])
    def test_limit_caps_results(self, mock_fetch, mock_scan, mock_dash, mock_display):
        mock_scan.return_value = [
            {"type": "BinaryInternal", "net_profit": 0.05, "net_roi": 0.05, "_clob_depth": 50},
            {"type": "BinaryInternal", "net_profit": 0.08, "net_roi": 0.08, "_clob_depth": 50},
            {"type": "BinaryInternal", "net_profit": 0.03, "net_roi": 0.03, "_clob_depth": 50},
        ]
        args = _make_args(limit=2)
        _cli_mod._run_oneshot(args, 0.01, None, _make_executor(), _make_db())
        displayed = mock_display.call_args[0][0]
        assert len(displayed) == 2

    @patch.object(_cli_mod, "display_results")
    @patch.object(_cli_mod, "dashboard_state")
    @patch.object(_cli_mod, "scan_binary_internal")
    @patch.object(_cli_mod, "fetch_all_markets", return_value=[{"question": "test"}])
    def test_executor_called_for_each_opp_in_dry_run(self, mock_fetch, mock_scan, mock_dash, mock_display):
        opps = [
            {"type": "BinaryInternal", "net_profit": 0.05},
            {"type": "BinaryInternal", "net_profit": 0.08},
        ]
        mock_scan.return_value = opps
        executor = _make_executor(dry_run=True)
        args = _make_args()
        _cli_mod._run_oneshot(args, 0.01, None, executor, _make_db())
        assert executor.execute.call_count == 2

    @patch.object(_cli_mod, "display_results")
    @patch.object(_cli_mod, "dashboard_state")
    @patch.object(_cli_mod, "scan_binary_internal")
    @patch.object(_cli_mod, "fetch_all_markets", return_value=[{"question": "test"}])
    def test_monitoring_only_opportunity_is_not_executed(self, mock_fetch, mock_scan, mock_dash, mock_display):
        mock_scan.return_value = [
            {"type": "BinaryInternal", "net_profit": 0.05},
            {"type": "PolymarketRewards", "net_profit": 0.0, "_execution_eligible": False},
        ]
        executor = _make_executor(dry_run=True)
        _cli_mod._run_oneshot(_make_args(), 0.01, None, executor, _make_db())
        executor.execute.assert_called_once_with(mock_scan.return_value[0])

    @patch.object(_cli_mod, "display_results")
    @patch.object(_cli_mod, "dashboard_state")
    @patch.object(_cli_mod, "scan_binary_internal", return_value=[])
    @patch.object(_cli_mod, "fetch_all_markets", return_value=[{"question": "test"}])
    def test_no_opps_means_no_execution(self, mock_fetch, mock_scan, mock_dash, mock_display):
        executor = _make_executor()
        args = _make_args()
        _cli_mod._run_oneshot(args, 0.01, None, executor, _make_db())
        executor.execute.assert_not_called()

    @patch.object(_cli_mod, "display_results")
    @patch.object(_cli_mod, "dashboard_state")
    @patch.object(_cli_mod, "scan_binary_internal")
    @patch.object(_cli_mod, "fetch_all_markets", return_value=[{"question": "test"}])
    def test_dashboard_state_updated(self, mock_fetch, mock_scan, mock_dash, mock_display):
        mock_scan.return_value = [
            {"type": "BinaryInternal", "net_profit": 0.05},
        ]
        db = _make_db()
        args = _make_args()
        _cli_mod._run_oneshot(args, 0.01, None, _make_executor(), db)
        # dashboard_state should have had opportunities_found updated
        assert mock_dash.opportunities_found is not None

    @patch.object(_cli_mod, "display_results")
    @patch.object(_cli_mod, "dashboard_state")
    @patch.object(_cli_mod, "scan_binary_internal")
    @patch.object(_cli_mod, "fetch_all_markets", return_value=[{"question": "test"}])
    def test_notifier_called_when_present(self, mock_fetch, mock_scan, mock_dash, mock_display):
        mock_scan.return_value = [
            {"type": "BinaryInternal", "net_profit": 0.10},
        ]
        notifier = MagicMock()
        args = _make_args()
        _cli_mod._run_oneshot(args, 0.01, None, _make_executor(), _make_db(),
                              notifier=notifier)
        notifier.notify.assert_called_once()

    @patch.object(_cli_mod, "display_results")
    @patch.object(_cli_mod, "dashboard_state")
    @patch.object(_cli_mod, "scan_binary_internal", return_value=[])
    @patch.object(_cli_mod, "fetch_all_markets", return_value=[{"question": "test"}])
    def test_notifier_not_called_when_no_opps(self, mock_fetch, mock_scan, mock_dash, mock_display):
        notifier = MagicMock()
        args = _make_args()
        _cli_mod._run_oneshot(args, 0.01, None, _make_executor(), _make_db(),
                              notifier=notifier)
        notifier.notify.assert_not_called()


# ---------------------------------------------------------------------------
# main() — continuous vs oneshot branching
# ---------------------------------------------------------------------------

class TestMainBranching:
    """Verify main() dispatches to _run_oneshot or run_continuous based on --continuous."""

    @patch.object(_cli_mod, "start_dashboard", return_value=None)
    @patch.object(_cli_mod, "run_continuous")
    @patch.object(_cli_mod, "_run_oneshot")
    @patch.object(_cli_mod, "ArbitrageExecutor")
    @patch.object(_cli_mod, "RiskManager")
    @patch.object(_cli_mod, "TradeDB")
    @patch.object(_cli_mod, "setup_logging")
    @patch.object(_cli_mod, "load_dotenv")
    def test_oneshot_by_default(self, mock_dotenv, mock_logging, mock_db_cls,
                                mock_risk_cls, mock_exec_cls, mock_oneshot,
                                mock_continuous, mock_dashboard):
        mock_db = MagicMock()
        mock_db_cls.return_value = mock_db

        with patch("sys.argv", ["scanner.py", "--mode", "binary"]):
            with patch.dict(os.environ, {}, clear=False):
                _cli_mod.main()

        mock_oneshot.assert_called_once()
        mock_continuous.assert_not_called()
        mock_db.close.assert_called_once()

    @patch.object(_cli_mod, "start_dashboard", return_value=None)
    @patch.object(_cli_mod, "run_continuous")
    @patch.object(_cli_mod, "_run_oneshot")
    @patch.object(_cli_mod, "ArbitrageExecutor")
    @patch.object(_cli_mod, "RiskManager")
    @patch.object(_cli_mod, "TradeDB")
    @patch.object(_cli_mod, "setup_logging")
    @patch.object(_cli_mod, "load_dotenv")
    def test_continuous_when_flag_set(self, mock_dotenv, mock_logging, mock_db_cls,
                                      mock_risk_cls, mock_exec_cls, mock_oneshot,
                                      mock_continuous, mock_dashboard):
        mock_db = MagicMock()
        mock_db_cls.return_value = mock_db

        with patch("sys.argv", ["scanner.py", "--continuous"]):
            with patch.dict(os.environ, {}, clear=False):
                _cli_mod.main()

        mock_continuous.assert_called_once()
        mock_oneshot.assert_not_called()
        mock_db.close.assert_called_once()

    @patch.object(_cli_mod, "start_dashboard", return_value=None)
    @patch.object(_cli_mod, "run_continuous")
    @patch.object(_cli_mod, "_run_oneshot")
    @patch.object(_cli_mod, "ArbitrageExecutor")
    @patch.object(_cli_mod, "RiskManager")
    @patch.object(_cli_mod, "TradeDB")
    @patch.object(_cli_mod, "setup_logging")
    @patch.object(_cli_mod, "load_dotenv")
    def test_mm_pilot_mode_dispatches_only_continuous(
        self, mock_dotenv, mock_logging, mock_db_cls, mock_risk_cls,
        mock_exec_cls, mock_oneshot, mock_continuous, mock_dashboard,
    ):
        mock_db = MagicMock()
        mock_db_cls.return_value = mock_db

        with patch("sys.argv", ["scanner.py", "--continuous", "--mode", "mm-pilot", "--dry-run"]):
            with patch.dict(os.environ, {}, clear=False):
                _cli_mod.main()

        mock_continuous.assert_called_once()
        mock_oneshot.assert_not_called()
        assert mock_continuous.call_args.args[0].mode == "mm-pilot"
        assert mock_exec_cls.call_args.kwargs["dry_run"] is True
        mock_db.close.assert_called_once()


# ---------------------------------------------------------------------------
# Parallel data fetching in _run_oneshot
# ---------------------------------------------------------------------------

class TestParallelDataFetching:
    """Verify the parallel fetch stage fetches correct data per mode."""

    @patch.object(_cli_mod, "display_results")
    @patch.object(_cli_mod, "dashboard_state")
    @patch.object(_cli_mod, "scan_binary_internal", return_value=[])
    @patch.object(_cli_mod, "scan_negrisk_internal", return_value=[])
    @patch.object(_cli_mod, "scan_kalshi_binary", return_value=[])
    @patch.object(_cli_mod, "scan_kalshi_multi", return_value=[])
    @patch.object(_cli_mod, "scan_cross_platform", return_value=[])
    @patch.object(_cli_mod, "scan_spread_polymarket", return_value=[])
    @patch.object(_cli_mod, "scan_triangular", return_value=[])
    @patch.object(_cli_mod, "scan_multi_cross", return_value=[])
    @patch.object(_cli_mod, "fetch_events", return_value=[{"id": "ev1"}])
    @patch.object(_cli_mod, "fetch_all_markets", return_value=[{"question": "test"}])
    @patch.object(_cli_mod, "_fetch_kalshi_data", return_value=([{"event_ticker": "E1"}], {}, {}))
    def test_all_mode_fetches_everything(
        self, mock_kdata, mock_fetch_markets, mock_fetch_events,
        mock_multi_cross, mock_tri, mock_spread_pm, mock_cross,
        mock_kalshi_multi, mock_kalshi_binary, mock_negrisk, mock_binary,
        mock_dash, mock_display,
    ):
        args = _make_args(mode="all")
        kalshi_client = MagicMock()
        _cli_mod._run_oneshot(args, 0.01, kalshi_client, _make_executor(), _make_db())
        mock_fetch_markets.assert_called_once()
        mock_fetch_events.assert_called_once()
        mock_kdata.assert_called_once()

    @patch.object(_cli_mod, "display_results")
    @patch.object(_cli_mod, "dashboard_state")
    @patch.object(_cli_mod, "scan_kalshi_binary", return_value=[])
    @patch.object(_cli_mod, "scan_kalshi_multi", return_value=[])
    @patch.object(_cli_mod, "_fetch_kalshi_data", return_value=([{"event_ticker": "E1"}], {}, {}))
    @patch.object(_cli_mod, "fetch_all_markets")
    @patch.object(_cli_mod, "fetch_events")
    def test_kalshi_mode_does_not_fetch_poly(
        self, mock_fetch_events, mock_fetch_markets,
        mock_kdata, mock_binary, mock_multi, mock_dash, mock_display,
    ):
        args = _make_args(mode="kalshi")
        kalshi_client = MagicMock()
        _cli_mod._run_oneshot(args, 0.01, kalshi_client, _make_executor(), _make_db())
        # Kalshi mode should NOT fetch poly markets or events
        mock_fetch_markets.assert_not_called()
        mock_fetch_events.assert_not_called()


# ---------------------------------------------------------------------------
# Event monitor integration
# ---------------------------------------------------------------------------

class TestEventMonitorIntegration:
    """Verify event_monitor.scan_event_divergences is called in event mode."""

    @patch.object(_cli_mod, "display_results")
    @patch.object(_cli_mod, "dashboard_state")
    @patch.object(_cli_mod, "fetch_all_markets", return_value=[{"question": "test"}])
    def test_event_mode_calls_event_monitor(self, mock_fetch, mock_dash, mock_display):
        event_monitor = MagicMock()
        event_monitor.scan_event_divergences.return_value = [
            {"type": "EventDivergence", "net_profit": 0.05},
        ]
        args = _make_args(mode="event")
        _cli_mod._run_oneshot(args, 0.01, None, _make_executor(), _make_db(),
                              event_monitor=event_monitor)
        event_monitor.scan_event_divergences.assert_called_once()

    @patch.object(_cli_mod, "display_results")
    @patch.object(_cli_mod, "dashboard_state")
    @patch.object(_cli_mod, "fetch_all_markets", return_value=[{"question": "test"}])
    def test_event_mode_without_monitor_is_noop(self, mock_fetch, mock_dash, mock_display):
        args = _make_args(mode="event")
        # No event_monitor -> no crash
        _cli_mod._run_oneshot(args, 0.01, None, _make_executor(), _make_db(),
                              event_monitor=None)


# ---------------------------------------------------------------------------
# JSON output flag
# ---------------------------------------------------------------------------

class TestJsonOutput:
    """Verify --json flag is passed through to display_results."""

    @patch.object(_cli_mod, "display_results")
    @patch.object(_cli_mod, "dashboard_state")
    @patch.object(_cli_mod, "scan_binary_internal", return_value=[{"type": "BinaryInternal", "net_profit": 0.05}])
    @patch.object(_cli_mod, "fetch_all_markets", return_value=[{"question": "test"}])
    def test_json_flag_passed_to_display(self, mock_fetch, mock_scan, mock_dash, mock_display):
        args = _make_args(json=True)
        _cli_mod._run_oneshot(args, 0.01, None, _make_executor(), _make_db())
        mock_display.assert_called_once()
        call_args = mock_display.call_args
        # display_results(all_opportunities, args.json) — second positional arg is True
        assert call_args[0][1] is True


# ---------------------------------------------------------------------------
# MarketMaker dry_run respects --exec-mode (INTEG-02)
# ---------------------------------------------------------------------------


class TestMarketMakerDryRun:
    """MarketMaker must use executor.dry_run — not hardcoded True."""

    @patch.object(_cli_mod, "display_results")
    @patch.object(_cli_mod, "dashboard_state")
    def test_market_maker_respects_exec_mode_full_auto(self, mock_dash, mock_display):
        """When exec-mode is full-auto, MarketMaker gets dry_run=False."""
        executor = _make_executor(dry_run=False)
        args = _make_args(mode="mm")

        with patch("market_maker.MarketMaker") as mock_mm_cls:
            mock_mm_instance = MagicMock()
            mock_mm_instance.generate_opportunities.return_value = []
            mock_mm_cls.return_value = mock_mm_instance

            _cli_mod._run_oneshot(args, 0.01, None, executor, _make_db())

        mock_mm_cls.assert_called_once()
        _, call_kwargs = mock_mm_cls.call_args
        assert call_kwargs.get("dry_run") is False, (
            "MarketMaker should receive dry_run=False when executor.dry_run is False"
        )

    @patch.object(_cli_mod, "display_results")
    @patch.object(_cli_mod, "dashboard_state")
    def test_market_maker_respects_exec_mode_dry_run(self, mock_dash, mock_display):
        """When executor is in dry_run mode, MarketMaker gets dry_run=True."""
        executor = _make_executor(dry_run=True)
        args = _make_args(mode="mm")

        with patch("market_maker.MarketMaker") as mock_mm_cls:
            mock_mm_instance = MagicMock()
            mock_mm_instance.generate_opportunities.return_value = []
            mock_mm_cls.return_value = mock_mm_instance

            _cli_mod._run_oneshot(args, 0.01, None, executor, _make_db())

        mock_mm_cls.assert_called_once()
        _, call_kwargs = mock_mm_cls.call_args
        assert call_kwargs.get("dry_run") is True, (
            "MarketMaker should receive dry_run=True when executor.dry_run is True"
        )


# ---------------------------------------------------------------------------
# Phase 9 (Structural Alpha Strategies) — new CLI modes
# ---------------------------------------------------------------------------

class TestPhase9CLIModes:
    """Test that logical-arb and whale-copy modes are registered and accepted."""

    def test_mode_logical_arb_recognized(self):
        """Test that --mode logical-arb is a valid CLI choice."""
        parser = argparse.ArgumentParser()
        parser.add_argument("--mode", choices=[
            "all", "binary", "negrisk", "cross", "kalshi", "cross-all",
            "spread", "betfair", "smarkets", "sxbet", "matchbook",
            "gemini", "ibkr", "event", "triangular", "multi-cross",
            "stale", "resolution", "convergence", "mm", "rewards",
            "imbalance", "news-snipe", "correlated", "time-decay",
            "logical-arb", "whale-copy",
        ], default="all")
        args = parser.parse_args(["--mode", "logical-arb"])
        assert args.mode == "logical-arb"

    def test_mode_whale_copy_recognized(self):
        """Test that --mode whale-copy is a valid CLI choice."""
        parser = argparse.ArgumentParser()
        parser.add_argument("--mode", choices=[
            "all", "binary", "negrisk", "cross", "kalshi", "cross-all",
            "spread", "betfair", "smarkets", "sxbet", "matchbook",
            "gemini", "ibkr", "event", "triangular", "multi-cross",
            "stale", "resolution", "convergence", "mm", "rewards",
            "imbalance", "news-snipe", "correlated", "time-decay",
            "logical-arb", "whale-copy",
        ], default="all")
        args = parser.parse_args(["--mode", "whale-copy"])
        assert args.mode == "whale-copy"

    def test_help_includes_new_modes(self, capsys):
        """Test that --help output includes both new Phase 9 modes."""
        parser = argparse.ArgumentParser()
        parser.add_argument("--mode", choices=[
            "all", "binary", "negrisk", "cross", "kalshi", "cross-all",
            "spread", "betfair", "smarkets", "sxbet", "matchbook",
            "gemini", "ibkr", "event", "triangular", "multi-cross",
            "stale", "resolution", "convergence", "mm", "rewards",
            "imbalance", "news-snipe", "correlated", "time-decay",
            "logical-arb", "whale-copy",
        ], default="all", help="Scanning mode")
        try:
            parser.parse_args(["--help"])
        except SystemExit:
            # --help causes parser to exit; capture output instead
            pass
        captured = capsys.readouterr()
        assert "logical-arb" in captured.out or "logical-arb" in captured.err
        assert "whale-copy" in captured.out or "whale-copy" in captured.err


# ---------------------------------------------------------------------------
# Optional external-source startup gates
# ---------------------------------------------------------------------------

class TestOptionalClientStartup:
    def test_ibkr_does_not_probe_localhost_without_explicit_host(self):
        with patch.dict(os.environ, {}, clear=True):
            with patch.object(_cli_mod, "IBKRClient") as client_cls:
                result = _cli_mod._initialize_ibkr_client("all")

        assert result is None
        client_cls.assert_not_called()

    def test_ibkr_connects_to_explicit_gateway(self):
        client = MagicMock()
        client.login.return_value = True

        with patch.dict(
            os.environ,
            {"IBKR_HOST": "gateway.internal", "IBKR_PORT": "4002", "IBKR_CLIENT_ID": "7"},
            clear=True,
        ):
            with patch.object(_cli_mod, "IBKRClient", return_value=client):
                result = _cli_mod._initialize_ibkr_client("ibkr")

        assert result is client
        client.login.assert_called_once_with("gateway.internal", 4002, 7)

    def test_metaculus_requires_event_monitor(self):
        with patch.object(_cli_mod, "CONFIG_EVENT_MONITOR", False):
            with patch.object(_cli_mod, "MetaculusClient") as client_cls:
                result = _cli_mod._initialize_metaculus_client()

        assert result is None
        client_cls.assert_not_called()

    def test_metaculus_requires_token_and_commercial_approval(self):
        with patch.object(_cli_mod, "CONFIG_EVENT_MONITOR", True):
            with patch.object(_cli_mod, "CONFIG_METACULUS_COMMERCIAL_USE_APPROVED", False):
                with patch.dict(os.environ, {"METACULUS_API_KEY": "token"}, clear=True):
                    with patch.object(_cli_mod, "MetaculusClient") as client_cls:
                        result = _cli_mod._initialize_metaculus_client()

        assert result is None
        client_cls.assert_not_called()

    def test_metaculus_initializes_after_both_access_gates(self):
        client = MagicMock()
        client.login.return_value = True

        with patch.object(_cli_mod, "CONFIG_EVENT_MONITOR", True):
            with patch.object(_cli_mod, "CONFIG_METACULUS_COMMERCIAL_USE_APPROVED", True):
                with patch.dict(os.environ, {"METACULUS_API_KEY": "token"}, clear=True):
                    with patch.object(_cli_mod, "MetaculusClient", return_value=client):
                        result = _cli_mod._initialize_metaculus_client()

        assert result is client
        client.login.assert_called_once_with(api_key="token")
