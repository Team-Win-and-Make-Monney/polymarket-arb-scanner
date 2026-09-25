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
            {"ticker": "A", "yes_ask": 0.40, "floor_strike": None, "cap_strike": 4},
            {"ticker": "B", "yes_ask": 0.40, "floor_strike": 5, "cap_strike": None},
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


TOP_AI_MODELS = [
    ("claude-fable-5.1-max", 0.74), ("claude-opus-5-max", 0.06), ("claude-opus-5-high", 0.05),
    ("claude-opus-4-6-high", 0.02), ("claude-opus-4-6", 0.02), ("gemini-3.8-flash-high", 0.01),
]


def _top_ai_model_event(legs):
    """KXTOPMODEL-26SEP28 "Top AI model this week": categorical, no strike fields."""
    return {
        "event_ticker": "KXTOPMODEL-26SEP28",
        "title": "Top AI model this week",
        "category": "Science and Technology",
        "mutually_exclusive": True,
        "markets": [
            {
                "ticker": f"KXTOPMODEL-26SEP28-{i}",
                "yes_sub_title": label,
                "yes_ask": ask,
                "rules_primary": f"If {label} is the top-ranked AI model on Sep 28, 2026 at 10:00 AM ET, "
                                 "then the market resolves to Yes.",
            }
            for i, (label, ask) in enumerate(legs)
        ],
    }


def test_categorical_without_catch_all_is_not_complete_set():
    # Six named models (asks sum to 0.90) and no catch-all leg: an unlisted
    # model finishing first resolves every leg NO.
    kinds = {row["kind"] for row in detect_event(_top_ai_model_event(TOP_AI_MODELS))}
    assert "complete_set_underprice" not in kinds


def test_categorical_with_catch_all_is_complete_set():
    event = _top_ai_model_event([*TOP_AI_MODELS, ("Other", 0.03)])
    flags = [row for row in detect_event(event) if row["kind"] == "complete_set_underprice"]
    assert len(flags) == 1
    assert flags[0]["n_outcomes"] == 7
    assert flags[0]["yes_ask_sum"] == 0.93


def test_categorical_catch_all_with_no_winner_rule_is_not_complete_set():
    # KXMODELHIGH lists "Other" but pays only if a model hits 1550 before a
    # deadline; if none does, every leg resolves NO.
    event = _top_ai_model_event([*TOP_AI_MODELS, ("Other", 0.03)])
    for market in event["markets"]:
        market["rules_primary"] = (
            f"If a model by {market['yes_sub_title']} is the first to hit 1550 on Text Arena "
            "before Jan 1, 2027, then the market resolves to Yes."
        )
    kinds = {row["kind"] for row in detect_event(event)}
    assert "complete_set_underprice" not in kinds


def test_categorical_catch_all_with_tie_no_winner_rule_is_not_complete_set():
    # If a tie occurs, no market resolves to Yes.
    event = _top_ai_model_event([*TOP_AI_MODELS, ("Other", 0.03)])
    for market in event["markets"]:
        market["rules_secondary"] = "If a tie occurs, no market resolves to Yes."
    kinds = {row["kind"] for row in detect_event(event)}
    assert "complete_set_underprice" not in kinds


def test_categorical_catch_all_with_positive_catch_all_rule_is_complete_set():
    # "If no other market wins, this market resolves to Yes."
    event = _top_ai_model_event([*TOP_AI_MODELS, ("Other", 0.03)])
    event["markets"][-1]["rules_primary"] = "If no other market wins, this market resolves to Yes."
    flags = [row for row in detect_event(event) if row["kind"] == "complete_set_underprice"]
    assert len(flags) == 1
    assert flags[0]["n_outcomes"] == 7
    assert flags[0]["yes_ask_sum"] == 0.93


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
