"""Tests for the EDGAR corporate-action detection core."""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from edgar_events import (
    EdgarEventType,
    Filing,
    classify_filing,
    format_edgar_alert,
    scan_filings,
)


def _filing(form_type, filer="Acme LLC", subject="Target Corp", body=""):
    return Filing(
        form_type=form_type,
        filer=filer,
        subject=subject,
        filed_at="2026-06-13",
        accession="0001",
        url="https://sec.gov/x",
        body_excerpt=body,
    )


class TestEdgarEvents:
    # ---------------------------------------------------------------------------
    # Classification
    def test_odd_lot_tender_detected_only_with_provision(self):
        with_provision = _filing("SC TO-I", body="Holders of odd lots (fewer than 100 shares) ...")
        assert classify_filing(with_provision).type is EdgarEventType.ODD_LOT_TENDER
        # A tender offer WITHOUT an odd-lot provision is not our signal.
        assert classify_filing(_filing("SC TO-I", body="standard pro-rata tender")) is None

    def test_odd_lot_hyphenated_phrasing_detected(self):
        # Filings often hyphenate "odd-lot" — hyphen normalization must still match.
        f = _filing("SC TO-I", body="Odd-Lot Tender: holders of fewer than 100 shares ...")
        assert classify_filing(f).type is EdgarEventType.ODD_LOT_TENDER

    def test_going_private_13e3(self):
        assert classify_filing(_filing("SC 13E3")).type is EdgarEventType.GOING_PRIVATE

    def test_merger_s4(self):
        assert classify_filing(_filing("S-4")).type is EdgarEventType.MERGER_S4

    def test_activist_13d_only_for_watched_filers(self):
        saba = _filing("SC 13D", filer="Saba Capital Management, L.P.")
        assert classify_filing(saba).type is EdgarEventType.ACTIVIST_13D
        # A 13D from a non-watched filer is ignored.
        assert classify_filing(_filing("SC 13D", filer="Random Holdings Inc")) is None

    def test_amendment_forms_classified_as_base(self):
        amended_13d = _filing("SC 13D/A", filer="Bulldog Investors")
        assert classify_filing(amended_13d).type is EdgarEventType.ACTIVIST_13D
        assert classify_filing(_filing("S-4/A")).type is EdgarEventType.MERGER_S4

    def test_unwatched_form_returns_none(self):
        assert classify_filing(_filing("10-K")) is None
        assert classify_filing(_filing("8-K")) is None

    # ---------------------------------------------------------------------------
    # Formatting + batch
    def test_format_alert_has_header_and_human_only_reminder(self):
        event = classify_filing(_filing("SC 13E3", subject="Discount Fund Inc"))
        msg = format_edgar_alert(event)
        assert "Going-private" in msg
        assert "Discount Fund Inc" in msg
        assert "no auto-trade" in msg

    def test_scan_filings_filters_batch(self):
        filings = [
            _filing("SC 13E3"),
            _filing("10-K"),                                   # ignored
            _filing("SC 13D", filer="Karpus Investment Mgmt"),  # activist
            _filing("SC 13D", filer="Nobody Capital"),          # ignored
            _filing("SC TO-I", body="odd lot priority"),        # odd-lot
        ]
        events = scan_filings(filings)
        assert len(events) == 3
        types = {e.type for e in events}
        assert types == {
            EdgarEventType.GOING_PRIVATE,
            EdgarEventType.ACTIVIST_13D,
            EdgarEventType.ODD_LOT_TENDER,
        }
