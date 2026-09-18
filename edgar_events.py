"""SEC EDGAR corporate-action event scanner — detection core (BUILD-LITE).

Flags four filing types that create tradeable/arb situations for a HUMAN to
action (IBKR is the tender rail — no automated trading here, detection only):

  * SC TO-I with an odd-lot provision  -> small-holder tender (odd lots get
    priority / no proration; a sub-100-share holder can tender at a premium)
  * SC 13E3                            -> going-private transaction
  * S-4                                -> stock/merger M&A registration
  * SC 13D by a watched activist filer -> activist 5%+ stake (Saba / Karpus /
    Bulldog — closed-end-fund discount activists)

Pure + deterministic: ``classify_filing`` maps a Filing record to an EdgarEvent
(or None), and ``format_edgar_alert`` renders a Telegram ticket. The live poll
(sec-edgar MCP) + Telegram delivery is a thin Phase-2 runner; this core is tested
with synthetic filings. Amendments (``/A``) are treated as their base form.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import Enum

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Public types and constants
# ---------------------------------------------------------------------------

class EdgarEventType(str, Enum):
    ODD_LOT_TENDER = "odd_lot_tender"
    GOING_PRIVATE = "going_private"
    MERGER_S4 = "merger_s4"
    ACTIVIST_13D = "activist_13d"


# Activist 13D filers worth a ticket (closed-end-fund / discount activists).
ACTIVIST_FILERS = frozenset({
    "saba capital",
    "karpus",
    "bulldog investors",
})


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Filing:
    form_type: str          # e.g. "SC TO-I", "SC 13E3", "S-4", "SC 13D", "SC 13D/A"
    filer: str              # filing person/entity
    subject: str            # subject company
    filed_at: str           # ISO date
    accession: str = ""
    url: str = ""
    body_excerpt: str = ""  # optional filing text (used for odd-lot detection)


@dataclass(frozen=True)
class EdgarEvent:
    type: EdgarEventType
    filing: Filing
    note: str


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------

def _canonical_form(form_type: str) -> str:
    """Normalize a form type for matching (drop ``/A``, spaces, hyphens; uppercase).

    Args:
        form_type: Raw SEC form type (e.g. ``"SC TO-I/A"``, ``"SC 13E3"``).

    Returns:
        The canonical form: ``"SC TO-I/A" -> "SCTOI"``, ``"S-4" -> "S4"``,
        ``"SC 13D/A" -> "SC13D"``.
    """
    base = (form_type or "").upper().split("/")[0]
    return base.replace(" ", "").replace("-", "").strip()


def classify_filing(filing: Filing, activist_filers=ACTIVIST_FILERS) -> EdgarEvent | None:
    """Map a filing to an EdgarEvent, or ``None`` if it isn't a watched signal.

    Args:
        filing: The SEC filing to classify.
        activist_filers: Watched activist filer substrings for SC 13D matching.

    Returns:
        An ``EdgarEvent`` for a watched signal, otherwise ``None``.
    """
    form = _canonical_form(filing.form_type)

    if form == "SCTOI":  # issuer tender offer — only of interest with an odd-lot provision
        # Normalize hyphens so "odd-lot" (common filing phrasing) matches too.
        body = (filing.body_excerpt or "").lower().replace("-", " ")
        if "odd lot" in body:
            return EdgarEvent(
                EdgarEventType.ODD_LOT_TENDER,
                filing,
                "odd-lot provision — small holders may tender without proration",
            )
        return None

    if form == "SC13E3":
        return EdgarEvent(EdgarEventType.GOING_PRIVATE, filing, "going-private transaction")

    if form == "S4":
        return EdgarEvent(EdgarEventType.MERGER_S4, filing, "stock/merger M&A registration")

    if form == "SC13D":
        filer = (filing.filer or "").lower()
        if any(a in filer for a in activist_filers):
            return EdgarEvent(
                EdgarEventType.ACTIVIST_13D, filing, f"activist 5%+ stake by {filing.filer}"
            )
        return None

    return None


# ---------------------------------------------------------------------------
# Alert formatting + batch scan
# ---------------------------------------------------------------------------

_HEADERS = {
    EdgarEventType.ODD_LOT_TENDER: "🎯 Odd-lot tender",
    EdgarEventType.GOING_PRIVATE: "🏷️ Going-private (13E-3)",
    EdgarEventType.MERGER_S4: "🤝 M&A (S-4)",
    EdgarEventType.ACTIVIST_13D: "📈 Activist 13D",
}


def format_edgar_alert(event: EdgarEvent) -> str:
    """Render a Telegram ticket for an event, ending with the human-only reminder.

    Args:
        event: The classified EDGAR event to format.

    Returns:
        A multi-line Telegram-ready alert string.
    """
    f = event.filing
    lines = [
        f"{_HEADERS[event.type]} — {f.subject}",
        f"Filer: {f.filer}",
        f"Form {f.form_type} · filed {f.filed_at}",
        event.note,
    ]
    if f.url:
        lines.append(f.url)
    lines.append("Action: review manually — IBKR is the tender rail, no auto-trade.")
    return "\n".join(lines)


def scan_filings(filings, activist_filers=ACTIVIST_FILERS) -> list[EdgarEvent]:
    """Classify a batch of filings, returning only the matched events.

    Args:
        filings: Iterable of ``Filing`` records to classify.
        activist_filers: Watched activist filer substrings for SC 13D matching.

    Returns:
        The list of ``EdgarEvent`` for filings that matched a watched signal.
    """
    out: list[EdgarEvent] = []
    for f in filings:
        event = classify_filing(f, activist_filers)
        if event is not None:
            out.append(event)
    return out
