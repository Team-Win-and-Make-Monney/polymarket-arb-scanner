"""UMA / Polymarket resolution-state monitor (dispute-risk gate data source).

Classifies Gamma market metadata to detect UMA proposal and dispute windows:
- Proposed / disputed UMA resolution status
- Closed markets that remain unresolved
- Markets not accepting orders
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_BLOCKING_UMA_STATUSES = frozenset({"proposed", "disputed"})


# ---------------------------------------------------------------------------
# Classification & Extraction
# ---------------------------------------------------------------------------

def classify_dispute_state(market: dict) -> dict:
    """Return dispute classification dict for a Gamma market dict.

    Args:
        market: Dictionary representing a Polymarket market (from Gamma API).

    Returns:
        Dict with keys: condition_id, state, blocked (bool), reason.
    """
    cid = market.get("conditionId") or market.get("condition_id") or ""
    status = (market.get("umaResolutionStatus") or "").lower()
    closed = bool(market.get("closed"))
    resolved = bool(market.get("resolved") or market.get("umaResolutionStatus") == "resolved")
    accepting = market.get("acceptingOrders", True)

    blocked, reason = False, "clear"
    if status in _BLOCKING_UMA_STATUSES:
        blocked, reason = True, f"uma_{status}"
    elif closed and not resolved:
        blocked, reason = True, "closed_unresolved"
    elif accepting is False:
        blocked, reason = True, "not_accepting_orders"

    state_label = status or ("closed" if closed else "open")
    return {
        "condition_id": cid,
        "state": state_label,
        "blocked": blocked,
        "reason": reason,
    }


def fetch_dispute_states(markets: list[dict]) -> dict[str, dict]:
    """Map condition_id -> dispute classification for all markets carrying one.

    Handles flat market lists as well as event objects containing nested
    'markets' arrays.

    Args:
        markets: List of market or event dictionaries.

    Returns:
        Dict mapping condition_id to classification dict.
    """
    out: dict[str, dict] = {}
    if not markets:
        return out

    for m in markets:
        if not isinstance(m, dict):
            continue

        c = classify_dispute_state(m)
        if c["condition_id"]:
            out[c["condition_id"]] = c

        # Support event dicts with nested markets array
        nested = m.get("markets")
        if isinstance(nested, list):
            for sub_m in nested:
                if isinstance(sub_m, dict):
                    sub_c = classify_dispute_state(sub_m)
                    if sub_c["condition_id"]:
                        out[sub_c["condition_id"]] = sub_c

    return out
