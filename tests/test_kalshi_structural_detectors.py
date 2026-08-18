"""Tests for live-legal structural detectors (no orders)."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from kalshi_structural_detectors import detect_event


def test_complete_set_underprice():
    event = {
        "event_ticker": "KXTEST",
        "category": "Economics",
        "markets": [
            {"ticker": "A", "yes_ask": 0.40},
            {"ticker": "B", "yes_ask": 0.40},
        ],
    }
    kinds = {row["kind"] for row in detect_event(event)}
    assert "complete_set_underprice" in kinds


def test_sports_event_skipped():
    event = {
        "event_ticker": "KXNFLGAME",
        "category": "Sports",
        "markets": [{"ticker": "A", "yes_ask": 0.40}, {"ticker": "B", "yes_ask": 0.40}],
    }
    assert detect_event(event) == []
