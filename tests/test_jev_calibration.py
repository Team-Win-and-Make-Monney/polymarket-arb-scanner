"""Comprehensive unit and statistical tests for the Jev Empirical Calibration & Brier Score engine.

Covers:
- Brier Score calculation and edge cases.
- Brier Skill Score benchmarking against market pricing.
- Reliability bins partitioning and Expected Calibration Error (ECE).
- Trade edge realization and PnL simulation.
- Database resolution tracking and query methods.
- Resolution syncer with mocked Polymarket Gamma API.
- Report generation and ASCII diagram rendering.
"""

from __future__ import annotations

import os
import sys
import tempfile
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from db import TradeDB
from jev_calibration import (
    calculate_brier_score,
    calculate_brier_skill_score,
    calculate_calibration_bins,
    calculate_edge_realization,
    calculate_expected_calibration_error,
    generate_calibration_report,
    render_ascii_reliability_diagram,
)
from scripts.sync_jev_resolutions import parse_resolution_outcome, sync_resolutions


class TestBrierMath:
    """Test the statistical accuracy of Brier score calculations."""

    def test_perfect_predictions(self):
        forecasts = [1.0, 0.0, 1.0, 0.0]
        outcomes = [1.0, 0.0, 1.0, 0.0]
        assert calculate_brier_score(forecasts, outcomes) == 0.0

    def test_total_inversion(self):
        forecasts = [0.0, 1.0]
        outcomes = [1.0, 0.0]
        assert calculate_brier_score(forecasts, outcomes) == 1.0

    def test_uninformative_guess_is_quarter(self):
        forecasts = [0.5, 0.5, 0.5, 0.5]
        outcomes = [1.0, 0.0, 1.0, 0.0]
        # (0.5 - 1)^2 = 0.25, (0.5 - 0)^2 = 0.25 -> mean is 0.25
        assert calculate_brier_score(forecasts, outcomes) == 0.25

    def test_empty_or_mismatched_inputs_safe(self):
        assert calculate_brier_score([], []) == 0.0
        assert calculate_brier_score([0.5], [1.0, 0.0]) == 0.0
        assert calculate_brier_score([0.5], []) == 0.0

    def test_brier_skill_score_positive_when_model_better(self):
        # Model predicted 0.90 for YES (outcome 1.0)
        # Market was priced at 0.60 for YES
        model_f = [0.90]
        mkt_f = [0.60]
        outcomes = [1.0]

        bs_model = (0.90 - 1.0) ** 2  # 0.01
        bs_mkt = (0.60 - 1.0) ** 2    # 0.16
        expected_bss = 1.0 - (bs_model / bs_mkt)  # 1.0 - (0.01 / 0.16) = +0.9375

        bss = calculate_brier_skill_score(model_f, mkt_f, outcomes)
        assert bss is not None
        assert pytest.approx(bss, rel=1e-4) == expected_bss
        assert bss > 0

    def test_brier_skill_score_negative_when_market_better(self):
        model_f = [0.60]
        mkt_f = [0.90]
        outcomes = [1.0]

        bss = calculate_brier_skill_score(model_f, mkt_f, outcomes)
        assert bss is not None
        assert bss < 0


class TestCalibrationBinsAndECE:
    """Test probability binning and Expected Calibration Error."""

    def test_calibration_bins_partition(self):
        forecasts = [0.05, 0.15, 0.25, 0.95]
        outcomes = [0.0, 0.0, 1.0, 1.0]
        bins = calculate_calibration_bins(forecasts, outcomes, num_bins=10)

        assert len(bins) == 10
        assert bins[0]["count"] == 1  # [0.0, 0.1)
        assert bins[1]["count"] == 1  # [0.1, 0.2)
        assert bins[2]["count"] == 1  # [0.2, 0.3)
        assert bins[9]["count"] == 1  # [0.9, 1.0]

        assert bins[0]["observed_frequency"] == 0.0
        assert bins[9]["observed_frequency"] == 1.0

    def test_perfect_calibration_ece_is_zero(self):
        # Predictions perfectly matching observed outcome frequency in each bin
        forecasts = [0.2, 0.2, 0.2, 0.2, 0.2]  # mean 0.2
        outcomes = [1.0, 0.0, 0.0, 0.0, 0.0]   # freq 1/5 = 0.2
        bins = calculate_calibration_bins(forecasts, outcomes, num_bins=5)
        ece = calculate_expected_calibration_error(bins, total_samples=5)
        assert pytest.approx(ece, abs=1e-4) == 0.0

    def test_uncalibrated_ece(self):
        forecasts = [0.9, 0.9, 0.9, 0.9]
        outcomes = [0.0, 0.0, 0.0, 0.0]  # Completely wrong
        bins = calculate_calibration_bins(forecasts, outcomes, num_bins=10)
        ece = calculate_expected_calibration_error(bins, total_samples=4)
        assert pytest.approx(ece, rel=1e-4) == 0.90


class TestEdgeRealization:
    """Test simulated trade performance attribution."""

    def test_buy_yes_winning(self):
        decisions = [{
            "action": "buy_yes",
            "market_prob": 0.60,
            "resolved_outcome": 1.0,
        }]
        res = calculate_edge_realization(decisions, standard_stake=50.0)
        assert res["total_recommended"] == 1
        assert res["wins"] == 1
        assert res["win_rate"] == 1.0
        # Cost = 50 * 0.60 = $30. Payout = $50. PnL = +$20
        assert pytest.approx(res["total_pnl"], abs=1e-2) == 20.0
        assert pytest.approx(res["total_cost"], abs=1e-2) == 30.0
        assert pytest.approx(res["roi"], abs=1e-2) == (20.0 / 30.0)

    def test_buy_no_winning(self):
        decisions = [{
            "action": "buy_no",
            "market_prob": 0.70,  # YES is 0.70 -> NO cost is 0.30
            "resolved_outcome": 0.0,  # NO won!
        }]
        res = calculate_edge_realization(decisions, standard_stake=50.0)
        assert res["total_recommended"] == 1
        assert res["wins"] == 1
        assert res["win_rate"] == 1.0
        # Cost = 50 * 0.30 = $15. Payout = $50. PnL = +$35
        assert pytest.approx(res["total_pnl"], abs=1e-2) == 35.0

    def test_mixed_trades_attribution(self):
        decisions = [
            {"action": "buy_yes", "market_prob": 0.50, "resolved_outcome": 1.0},  # Win: +$25
            {"action": "buy_yes", "market_prob": 0.50, "resolved_outcome": 0.0},  # Loss: -$25
            {"action": "pass_fair", "market_prob": 0.50, "resolved_outcome": 1.0}, # Ignored
        ]
        res = calculate_edge_realization(decisions, standard_stake=50.0)
        assert res["total_recommended"] == 2
        assert res["wins"] == 1
        assert res["losses"] == 1
        assert res["win_rate"] == 0.5
        assert pytest.approx(res["total_pnl"], abs=1e-2) == 0.0


class TestDbResolutionMethods:
    """Test SQLite database migration and resolution recording methods."""

    @pytest.fixture
    def test_db(self):
        tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        tmp.close()
        db = TradeDB(tmp.name)
        yield db
        db.close()
        if os.path.exists(tmp.name):
            os.unlink(tmp.name)

    def test_update_single_resolution(self, test_db):
        dec_id = test_db.record_jev_decision(
            asset="BTC",
            strike=100000.0,
            spot=85000.0,
            action="buy_yes",
            market_prob=0.40,
            jev_prob=0.65,
        )
        assert dec_id > 0

        # Initially unresolved
        data = test_db.get_jev_calibration_data(only_resolved=True)
        assert len(data) == 0

        # Update resolution
        ok = test_db.update_jev_resolution(dec_id, outcome=1.0)
        assert ok is True

        data = test_db.get_jev_calibration_data(only_resolved=True)
        assert len(data) == 1
        assert data[0]["resolved_outcome"] == 1.0
        assert data[0]["resolved_at"] is not None

    def test_update_resolutions_by_market_identifier(self, test_db):
        test_db.record_jev_decision(
            asset="ETH",
            strike=3000.0,
            spot=2500.0,
            action="buy_no",
            market_prob=0.80,
            jev_prob=0.35,
            details={"question": "Will Ethereum reach $3,000 by May?"},
        )
        test_db.record_jev_decision(
            asset="ETH",
            strike=3000.0,
            spot=2600.0,
            action="buy_no",
            market_prob=0.82,
            jev_prob=0.32,
            details={"question": "Will Ethereum reach $3,000 by May?"},
        )

        updated_count = test_db.update_jev_resolutions_by_market(
            identifier="Ethereum reach $3,000",
            outcome=0.0,
        )
        assert updated_count == 2

        calib_data = test_db.get_jev_calibration_data(asset="ETH", only_resolved=True)
        assert len(calib_data) == 2
        assert all(r["resolved_outcome"] == 0.0 for r in calib_data)


class TestResolutionOutcomeParser:
    """Test parsing Polymarket Gamma API market dictionaries."""

    def test_parse_resolved_outcome_string(self):
        assert parse_resolution_outcome({"closed": True, "resolvedOutcome": "Yes"}) == 1.0
        assert parse_resolution_outcome({"closed": True, "resolvedOutcome": "No"}) == 0.0
        assert parse_resolution_outcome({"closed": True, "resolvedOutcome": "1"}) == 1.0
        assert parse_resolution_outcome({"closed": True, "resolvedOutcome": "0"}) == 0.0

    def test_parse_outcome_prices(self):
        mkt_yes = {"closed": True, "outcomePrices": '["1", "0"]'}
        mkt_no = {"closed": True, "outcomePrices": '["0", "1"]'}
        assert parse_resolution_outcome(mkt_yes) == 1.0
        assert parse_resolution_outcome(mkt_no) == 0.0

    def test_parse_unresolved_returns_none(self):
        mkt_open = {"closed": False, "outcomePrices": '["0.65", "0.35"]'}
        assert parse_resolution_outcome(mkt_open) is None


class TestReportAndAsciiDiagram:
    """Test full calibration report generation and ASCII diagram formatting."""

    @pytest.fixture
    def populated_db(self):
        tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        tmp.close()
        db = TradeDB(tmp.name)

        # Populate a calibrated sample of decisions
        samples = [
            ("BTC", 0.70, 0.55, "buy_yes", 1.0),
            ("BTC", 0.80, 0.60, "buy_yes", 1.0),
            ("ETH", 0.30, 0.45, "buy_no", 0.0),
            ("SOL", 0.90, 0.75, "buy_yes", 1.0),
            ("SOL", 0.20, 0.40, "buy_no", 0.0),
        ]
        for asset, j_prob, m_prob, action, outcome in samples:
            dec_id = db.record_jev_decision(
                asset=asset,
                strike=100.0,
                spot=90.0,
                action=action,
                market_prob=m_prob,
                jev_prob=j_prob,
            )
            db.update_jev_resolution(dec_id, outcome=outcome)

        yield db
        db.close()
        if os.path.exists(tmp.name):
            os.unlink(tmp.name)

    def test_generate_calibration_report_populated(self, populated_db):
        report = generate_calibration_report(populated_db, asset="all")
        assert report["status"] == "success"
        assert report["total_logged"] == 5
        assert report["total_resolved"] == 5
        assert report["jev_brier_score"] < 0.10  # Well calibrated
        assert report["brier_skill_score"] is not None
        assert report["brier_skill_score"] > 0  # Jev outperformed market
        assert "BTC" in report["per_asset"]
        assert "ETH" in report["per_asset"]
        assert "SOL" in report["per_asset"]
        assert len(report["bins"]) == 10
        assert report["edge_realization"]["win_rate"] == 1.0

    def test_render_ascii_reliability_diagram(self):
        bins = [
            {"count": 5, "mean_forecast": 0.25, "observed_frequency": 0.20},
            {"count": 8, "mean_forecast": 0.80, "observed_frequency": 0.85},
        ]
        diagram = render_ascii_reliability_diagram(bins, width=30, height=8)
        assert "Reliability Diagram" in diagram
        assert "1.0 |" in diagram
        assert "0.0 |" in diagram
        assert "Ideal Calibration" in diagram
        assert "*" in diagram
