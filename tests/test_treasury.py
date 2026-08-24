"""Tests for Strategy #18: treasury / auto-rebalancing.

Covers:
- TreasuryManager rejects unsupported corridors
- TreasuryManager rejects when feature flag disabled
- TreasuryManager rejects amounts below MIN_TRANSFER_AMOUNT
- TreasuryManager rejects when daily limit exceeded
- DRY_RUN path writes audit row but does not call gemini withdraw
- Live path calls gemini.withdraw_usdc and updates row to 'succeeded'
- Idempotent replay returns existing transfer id without re-executing
- gemini_api.withdraw_usdc rejects empty address / non-positive amount
"""

import os
import sys
import pytest
from unittest.mock import MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from db import TradeDB


@pytest.fixture
def db():
    trade_db = TradeDB(":memory:")
    yield trade_db
    trade_db.close()


@pytest.fixture(autouse=True)
def _reset_config(monkeypatch):
    """Each test gets a fresh config snapshot.

    Crucially, after the test completes we pop config + treasury again so
    later tests in the suite re-import them with their original (un-monkey-
    patched) env state. Otherwise any test that imports `config` at module
    load time and binds names from it can end up with stale references.
    """
    sys.modules.pop("config", None)
    sys.modules.pop("treasury", None)
    monkeypatch.setenv("AUTO_REBALANCE_ENABLED", "true")
    monkeypatch.setenv("MIN_TRANSFER_AMOUNT", "50.0")
    monkeypatch.setenv("MAX_AUTO_TRANSFER_PER_DAY", "500.0")
    monkeypatch.setenv("POLYMARKET_DEPOSIT_ADDRESS", "0xPMproxy123")
    yield
    # Teardown: drop the env-polluted modules so subsequent test files
    # get a fresh import with the restored environment.
    sys.modules.pop("config", None)
    sys.modules.pop("treasury", None)
    sys.modules.pop("gemini_api", None)


def _import_treasury():
    if "treasury" in sys.modules:
        del sys.modules["treasury"]
    from treasury import TreasuryManager, TransferResult, SUPPORTED_CORRIDORS
    return TreasuryManager, TransferResult, SUPPORTED_CORRIDORS


# ---------------------------------------------------------------------------
# Corridor + flag rejections
# ---------------------------------------------------------------------------

class TestCorridorRejection:
    def test_unsupported_corridor(self, db):
        TreasuryManager, _, _ = _import_treasury()
        tm = TreasuryManager(db=db, dry_run=True)
        result = tm.execute_transfer("kalshi", "polymarket", 100.0)
        assert result.ok is False
        assert "Unsupported corridor" in (result.error or "")

    def test_disabled_feature_flag(self, db, monkeypatch):
        monkeypatch.setenv("AUTO_REBALANCE_ENABLED", "false")
        sys.modules.pop("config", None)
        TreasuryManager, _, _ = _import_treasury()
        tm = TreasuryManager(db=db, dry_run=True)
        result = tm.execute_transfer("gemini", "polymarket", 100.0)
        assert result.ok is False
        assert "AUTO_REBALANCE_ENABLED" in (result.error or "")


# ---------------------------------------------------------------------------
# Amount + daily limit gates
# ---------------------------------------------------------------------------

class TestRiskGates:
    def test_below_min_amount(self, db):
        TreasuryManager, _, _ = _import_treasury()
        tm = TreasuryManager(db=db, dry_run=True)
        result = tm.execute_transfer("gemini", "polymarket", 5.0)
        assert result.ok is False
        assert "MIN_TRANSFER_AMOUNT" in (result.error or "")

    def test_kill_switch_engaged(self, db):
        TreasuryManager, _, _ = _import_treasury()
        tm = TreasuryManager(db=db, dry_run=True, kill_switch=lambda: True)
        result = tm.execute_transfer("gemini", "polymarket", 100.0)
        assert result.ok is False
        assert "kill switch" in (result.error or "")

    def test_daily_limit_blocks_overflow(self, db):
        TreasuryManager, _, _ = _import_treasury()
        tm = TreasuryManager(db=db, dry_run=True)
        # Two transfers totalling exactly the daily cap should both succeed
        r1 = tm.execute_transfer("gemini", "polymarket", 250.0)
        r2 = tm.execute_transfer("gemini", "polymarket", 250.0,
                                 idempotency_key="explicit_2")
        assert r1.ok is True and r2.ok is True
        # A third one breaches the cap
        r3 = tm.execute_transfer("gemini", "polymarket", 100.0,
                                 idempotency_key="explicit_3")
        assert r3.ok is False
        assert "Daily limit" in (r3.error or "")


# ---------------------------------------------------------------------------
# DRY_RUN behavior
# ---------------------------------------------------------------------------

class TestDryRun:
    def test_dry_run_writes_audit_row_without_calling_gemini(self, db):
        TreasuryManager, _, _ = _import_treasury()
        gemini = MagicMock()
        tm = TreasuryManager(db=db, gemini_client=gemini, dry_run=True)
        result = tm.execute_transfer("gemini", "polymarket", 100.0)
        assert result.ok is True
        assert result.dry_run is True
        gemini.withdraw_usdc.assert_not_called()
        rows = db.get_transfers_today()
        assert len(rows) == 1
        assert rows[0]["status"] == "dry_run"
        assert rows[0]["amount_usd"] == 100.0


# ---------------------------------------------------------------------------
# Live path
# ---------------------------------------------------------------------------

class TestLivePath:
    def test_live_path_calls_gemini_and_updates_status(self, db):
        TreasuryManager, _, _ = _import_treasury()
        gemini = MagicMock()
        gemini.withdraw_usdc.return_value = {"txHash": "0xabc123"}
        tm = TreasuryManager(db=db, gemini_client=gemini, dry_run=False)
        result = tm.execute_transfer("gemini", "polymarket", 100.0)
        assert result.ok is True
        assert result.dry_run is False
        assert result.tx_hash == "0xabc123"
        gemini.withdraw_usdc.assert_called_once_with(
            address="0xPMproxy123", amount=100.0,
        )
        rows = db.get_transfers_today()
        assert len(rows) == 1
        assert rows[0]["status"] == "succeeded"
        assert rows[0]["tx_hash"] == "0xabc123"

    def test_live_path_records_failure_on_exception(self, db):
        TreasuryManager, _, _ = _import_treasury()
        gemini = MagicMock()
        gemini.withdraw_usdc.side_effect = RuntimeError("upstream 503")
        tm = TreasuryManager(db=db, gemini_client=gemini, dry_run=False)
        result = tm.execute_transfer("gemini", "polymarket", 100.0)
        assert result.ok is False
        assert "upstream 503" in (result.error or "")
        rows = db.get_transfers_today()
        assert rows[0]["status"] == "failed"
        assert "upstream 503" in (rows[0]["error"] or "")


# ---------------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------------

class TestIdempotency:
    def test_replay_returns_existing_id_without_double_execution(self, db):
        TreasuryManager, _, _ = _import_treasury()
        gemini = MagicMock()
        gemini.withdraw_usdc.return_value = {"txHash": "0xfeed"}
        tm = TreasuryManager(db=db, gemini_client=gemini, dry_run=False)
        key = "deterministic_key"
        r1 = tm.execute_transfer("gemini", "polymarket", 100.0,
                                 idempotency_key=key)
        r2 = tm.execute_transfer("gemini", "polymarket", 100.0,
                                 idempotency_key=key)
        # Both calls surface the same transfer id and the venue is called once.
        assert r1.transfer_id == r2.transfer_id
        gemini.withdraw_usdc.assert_called_once_with(
            address="0xPMproxy123", amount=100.0,
        )
        rows = db.get_transfers_today()
        assert len(rows) == 1

    def test_replay_returns_prior_result_before_daily_limit_gate(self, db, monkeypatch):
        TreasuryManager, _, _ = _import_treasury()
        tm = TreasuryManager(db=db, dry_run=True)
        key = "replay_after_limit"
        first = tm.execute_transfer("gemini", "polymarket", 100.0, idempotency_key=key)
        import config
        monkeypatch.setattr(config, "MAX_AUTO_TRANSFER_PER_DAY", 0.0)

        replay = tm.execute_transfer("gemini", "polymarket", 100.0, idempotency_key=key)

        assert replay.ok is True
        assert replay.transfer_id == first.transfer_id
        assert replay.dry_run is True

    def test_reused_key_with_different_payload_is_rejected(self, db):
        TreasuryManager, _, _ = _import_treasury()
        tm = TreasuryManager(db=db, dry_run=True)
        key = "payload_bound_key"
        first = tm.execute_transfer("gemini", "polymarket", 100.0, idempotency_key=key)

        mismatch = tm.execute_transfer("gemini", "polymarket", 125.0, idempotency_key=key)

        assert first.ok is True
        assert mismatch.ok is False
        assert mismatch.transfer_id == first.transfer_id
        assert "payload mismatch" in (mismatch.error or "")


# ---------------------------------------------------------------------------
# Gemini withdraw_usdc input validation
# ---------------------------------------------------------------------------

class TestGeminiWithdrawValidation:
    @pytest.fixture(autouse=True)
    def _stub_gemini_module(self, monkeypatch):
        # Drop the module so we get a fresh import after env vars are set
        sys.modules.pop("gemini_api", None)
        # Provide minimum credentials so __init__ doesn't bail
        monkeypatch.setenv("GEMINI_API_KEY", "key")
        monkeypatch.setenv("GEMINI_API_SECRET", "secret")
        yield
        sys.modules.pop("gemini_api", None)

    def test_empty_address_returns_none(self, monkeypatch):
        from gemini_api import GeminiClient
        client = GeminiClient()
        # Patch _private_request so we'd notice if validation slipped
        client._private_request = MagicMock(return_value={"txHash": "x"})
        assert client.withdraw_usdc("", 100.0) is None
        client._private_request.assert_not_called()

    def test_zero_amount_returns_none(self):
        from gemini_api import GeminiClient
        client = GeminiClient()
        client._private_request = MagicMock(return_value={"txHash": "x"})
        assert client.withdraw_usdc("0xabc", 0.0) is None
        client._private_request.assert_not_called()

    def test_valid_call_dispatches(self):
        from gemini_api import GeminiClient
        client = GeminiClient()
        client._private_request = MagicMock(return_value={"txHash": "0xabc"})
        result = client.withdraw_usdc("0xabc", 100.0)
        assert result == {"txHash": "0xabc"}
        client._private_request.assert_called_once()
        args, kwargs = client._private_request.call_args
        assert args[0] == "/v1/withdraw/usdc"
        assert kwargs["payload_data"]["address"] == "0xabc"
