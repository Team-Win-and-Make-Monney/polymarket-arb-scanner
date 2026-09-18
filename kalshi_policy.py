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
    ticker = event.get("event_ticker") or event.get("ticker") or ""
    return ticker_blocked(ticker)


def ticker_blocked(ticker: str) -> bool:
    """Return whether a Kalshi ticker contains a forbidden token."""
    normalized = (ticker or "").upper()
    return bool(normalized) and any(token in normalized for token in BLOCKED_TICKER_TOKENS)


def live_kalshi_submit_allowed(ticker: str, *, reducing: bool = False) -> bool:
    """Guard live order paths while allowing risk-reducing flattening."""
    return reducing or not ticker_blocked(ticker)
