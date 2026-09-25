"""Kalshi categorical complete-set gate. Stdlib only.

Shared by ``scans/kalshi.py`` (``scan_kalshi_multi``) and
``scripts/kalshi_structural_detectors.py`` (the D0 MM pilot selection feed) so
both apply one rule to strike-less mutually exclusive events.
"""

from __future__ import annotations

import re

# Leg labels (``yes_sub_title``) that cover every outcome not listed elsewhere
# in the event. Matched against the whole label, never a substring: live
# labels such as "No other person" (KXNEXTSTATE's no-appointee leg), "Other
# renewables" (one energy category) and album titles containing "Other" are
# named outcomes. Qualified forms like "Any other person (excluding Jeanie
# Buss)" can carve out an unlisted outcome, so they fail closed too.
CATCH_ALL_LABELS = frozenset({
    "other", "others", "any other", "all other", "all others",
    "anyone else", "anybody else", "someone else", "somebody else",
    "field", "the field", "none of the above",
})

# Rules text that lets every leg resolve NO. A catch-all covers unlisted
# winners, not "no winner": KXMODELHIGH lists "Other" but pays only if a model
# hits 1550 before Jan 1, 2027, and KXNBANEXTGOVERNOR's "Any other person"
# leg sits under "If no new team governor ... all markets resolve to No".
# "All other markets resolve to No" only restates exclusivity, so it is exempt.
NO_WINNER_RULE = re.compile(
    r"\bbefore\b"
    r"|\bif (?:no|none|nobody)\b"
    r"|\b(?:all|every|each)\s+(?!other\b)(?:\w+\s+){0,2}?(?:markets?|contracts?|strikes?)\b[^.]*?"
    r"\b(?:resolved?|resolves)\s+(?:to\s+)?\W?no\b",
    re.IGNORECASE,
)


def is_catch_all_label(label: str | None) -> bool:
    """Whether a leg label is a bare catch-all such as "Other" or "Someone else"."""
    if not isinstance(label, str):
        return False
    return " ".join(label.lower().split()).strip(".,;:!?\"'") in CATCH_ALL_LABELS


def is_exhaustive_categorical(event_markets: list[dict]) -> bool:
    """Whether a categorical (strike-less) Kalshi event covers every outcome.

    ``mutually_exclusive`` only guarantees at most one YES, and Kalshi publishes
    no exhaustiveness field (``collateral_return_type`` is MECNET exactly when
    ``mutually_exclusive`` is true). KXTOPMODEL "Top AI model this week" listed
    six models and no catch-all, so any unlisted model finishing first resolves
    every leg NO. Fail closed: demand an explicit catch-all leg, and reject any
    event whose rules describe a way for no leg to win.

    Args:
        event_markets: The event's nested market dicts.

    Returns:
        True only when some leg's ``yes_sub_title`` is a catch-all label and no
        leg's rules text matches ``NO_WINNER_RULE``.
    """
    if not any(is_catch_all_label(m.get("yes_sub_title")) for m in event_markets):
        return False
    return not any(
        NO_WINNER_RULE.search(f"{m.get('rules_primary') or ''} {m.get('rules_secondary') or ''}")
        for m in event_markets
    )
