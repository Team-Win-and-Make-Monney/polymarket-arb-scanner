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
        "mutually_exclusive": True,
        "markets": [
            {"ticker": "A", "yes_ask": 0.40},
            {"ticker": "B", "yes_ask": 0.40},
        ],
    }
    kinds = {row["kind"] for row in detect_event(event)}
    assert "complete_set_underprice" in kinds


def test_non_exclusive_ladder_is_not_complete_set():
    event = {
        "event_ticker": "KXSPREAD",
        "category": "Economics",
        "mutually_exclusive": False,
        "markets": [
            {"ticker": "A", "yes_ask": 0.40},
            {"ticker": "B", "yes_ask": 0.40},
        ],
    }
    kinds = {row["kind"] for row in detect_event(event)}
    assert "complete_set_underprice" not in kinds


def test_non_exhaustive_strike_ladder_is_not_complete_set():
    event = {
        "event_ticker": "KXPHOTO",
        "category": "Economics",
        "mutually_exclusive": True,
        "markets": [
            {"ticker": "A", "yes_ask": 0.40, "floor_strike": 4, "cap_strike": 4},
            {"ticker": "B", "yes_ask": 0.40, "floor_strike": 5, "cap_strike": 5},
        ],
    }
    kinds = {row["kind"] for row in detect_event(event)}
    assert "complete_set_underprice" not in kinds


def test_closed_market_is_not_late_window():
    event = {
        "event_ticker": "KXOLD",
        "category": "Economics",
        "markets": [
            {
                "ticker": "A",
                "yes_ask": 0.40,
                "close_time": "2020-01-01T00:00:00Z",
            },
        ],
    }
    kinds = {row["kind"] for row in detect_event(event)}
    assert "late_window_unpinned" not in kinds


def test_sports_event_skipped():
    event = {
        "event_ticker": "KXNFLGAME",
        "category": "Sports",
        "markets": [{"ticker": "A", "yes_ask": 0.40}, {"ticker": "B", "yes_ask": 0.40}],
    }
    assert detect_event(event) == []
