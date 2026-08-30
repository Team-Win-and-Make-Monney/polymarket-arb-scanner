"""Tests for bracket-set completeness checks."""

from scans.bracket import _brackets_are_complete


def _market(lower: float, upper: float) -> dict:
    return {"_bracket_info": {"lower_bound": lower, "upper_bound": upper}}


class TestBracketCompleteness:
    def test_complete_set_covers_both_tails(self):
        brackets = [
            _market(float("-inf"), 10.0),
            _market(10.0, 20.0),
            _market(20.0, float("inf")),
        ]
        assert _brackets_are_complete(brackets) is True

    def test_leading_gap_is_incomplete(self):
        brackets = [
            _market(10.0, 20.0),
            _market(20.0, float("inf")),
        ]
        assert _brackets_are_complete(brackets) is False
