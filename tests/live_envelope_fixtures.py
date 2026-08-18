"""Shared test envelope — dummy limits for validate_config, not live authority."""

from __future__ import annotations

import json
from pathlib import Path


def write_test_envelope(tmp_path: Path, **overrides) -> Path:
    payload = {
        "venue": "kalshi-d0",
        "pair": "KXFED",
        "max_notional_usd": 150,
        "max_daily_loss_usd": 25,
        "kill_switch": "quit",
    }
    payload.update(overrides)
    path = tmp_path / "live-envelope.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path
