"""Live-legal Kalshi selection policy. Stdlib only.

Michigan sports stay closed. Mention books are under CFTC review as of 2026-08-17.
``F1`` is not a ticker token here — it false-positives ``KXCONF1``.
"""

from __future__ import annotations

import os

EXCLUDED_CATEGORIES = tuple(
    c.strip() for c in os.getenv("LIP_EXCLUDED_CATEGORIES", "Sports").split(",") if c.strip()
)

BLOCKED_TICKER_TOKENS = (
    "NFL", "NBA", "MLB", "NHL", "WNBA", "NCAA", "UFC", "SOCCER", "EPL", "MLS",
    "GOLF", "PGA", "TENNIS", "NASCAR", "CFB", "CBB", "ATP", "WTA",
    "CRICKET", "RUGBY", "LEAGUESCUP", "MENTION",
)


def event_blocked(event: dict) -> bool:
    """True when a Kalshi event is out of policy for this book."""
    category = (event.get("category") or "").strip()
    if category and any(category.lower() == excluded.lower() for excluded in EXCLUDED_CATEGORIES):
        return True
    ticker = (event.get("event_ticker") or "").upper()
    return any(token in ticker for token in BLOCKED_TICKER_TOKENS)
