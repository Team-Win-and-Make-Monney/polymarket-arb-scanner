"""Tests for fail-closed live envelope."""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from live_envelope import EnvelopeError, load_envelope, pair_allowed


def _write(path: Path, payload: dict) -> Path:
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


class TestLoadEnvelope:
    def test_missing_file(self, tmp_path):
        with pytest.raises(EnvelopeError, match="missing"):
            load_envelope(tmp_path / "nope.json")

    def test_missing_fields(self, tmp_path):
        path = _write(tmp_path / "e.json", {"venue": "kalshi-d0"})
        with pytest.raises(EnvelopeError, match="missing fields"):
            load_envelope(path)

    def test_forbidden_venue(self, tmp_path):
        path = _write(tmp_path / "e.json", {
            "venue": "polymarket-intl",
            "pair": "BTCUSD",
            "max_notional_usd": 100,
            "max_daily_loss_usd": 25,
            "kill_switch": "pause",
        })
        with pytest.raises(EnvelopeError, match="forbidden"):
            load_envelope(path)

    def test_wildcard_pair_rejected(self, tmp_path):
        path = _write(tmp_path / "e.json", {
            "venue": "kalshi-d0",
            "pair": "whatever looks good",
            "max_notional_usd": 100,
            "max_daily_loss_usd": 25,
            "kill_switch": "pause",
        })
        with pytest.raises(EnvelopeError, match="exact ticker"):
            load_envelope(path)

    def test_zero_notional_rejected(self, tmp_path):
        path = _write(tmp_path / "e.json", {
            "venue": "kalshi-d0",
            "pair": "KXFED",
            "max_notional_usd": 0,
            "max_daily_loss_usd": 25,
            "kill_switch": "pause",
        })
        with pytest.raises(EnvelopeError, match="max_notional_usd"):
            load_envelope(path)

    def test_non_finite_notional_rejected(self, tmp_path):
        path = tmp_path / "e.json"
        path.write_text(
            '{"venue":"kalshi-d0","pair":"KXFED","max_notional_usd":NaN,'
            '"max_daily_loss_usd":25,"kill_switch":"pause"}',
            encoding="utf-8",
        )
        with pytest.raises(EnvelopeError, match="max_notional_usd"):
            load_envelope(path)

    def test_valid_kalshi_d0(self, tmp_path):
        path = _write(tmp_path / "e.json", {
            "venue": "kalshi-d0",
            "pair": "KXFED",
            "max_notional_usd": 150,
            "max_daily_loss_usd": 25,
            "kill_switch": "screen -S mm-d0-live -X quit",
        })
        loaded = load_envelope(path)
        assert loaded["venue"] == "kalshi-d0"
        assert loaded["dry_run"] is False
        assert loaded["max_notional_usd"] == 150


class TestPairAllowed:
    def test_exact_ticker(self):
        assert pair_allowed("KXFED-26SEP", "KXFED-26SEP")

    def test_series_prefix(self):
        assert pair_allowed("KXFED-26SEP-HIKE", "KXFED")

    def test_other_series_rejected(self):
        assert not pair_allowed("KXWTI15M-26AUG172330-30", "KXFED")
