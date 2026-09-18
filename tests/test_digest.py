"""Tests for the P&L digest formatter (digest.py).

Pins the day/WTD/MTD window filtering and the rendered digest sections, so a
formatting or window-boundary regression is caught before the digest goes out
daily. Note PnlEntry validates trade_date at construction, so a malformed date
can only reach entries_in_window via a non-PnlEntry object — exercised with a
stub to prove the defensive skip is live.
"""
from __future__ import annotations

import os
import sys
from datetime import date
from types import SimpleNamespace

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from digest import _week_start, entries_in_window, format_pnl_digest
from pnl_ledger import PnlEntry

ASOF = date(2026, 6, 18)
WK = _week_start(ASOF)  # Monday of ASOF's week (mid-June, same month)


def _e(amount, trade_date, lane="perp_carry", bucket="possible_1256", engine="quant"):
    return PnlEntry(engine=engine, lane=lane, tax_bucket=bucket,
                    amount_usd=amount, trade_date=trade_date)


def _entries():
    return [
        _e(10.0, ASOF.isoformat()),                  # today      -> day, wtd, mtd
        _e(20.0, WK.isoformat()),                    # mon        -> wtd, mtd
        _e(30.0, date(2026, 6, 2).isoformat()),      # early June -> mtd only
        _e(99.0, date(2026, 5, 20).isoformat()),     # last month -> none
    ]


class TestDigest:
    def test_window_day_is_only_today(self):
        got = entries_in_window(_entries(), ASOF, "day")
        assert [e.amount_usd for e in got] == [10.0]

    def test_window_wtd_excludes_earlier_month(self):
        got = sorted(e.amount_usd for e in entries_in_window(_entries(), ASOF, "wtd"))
        assert got == [10.0, 20.0]

    def test_window_mtd_includes_june_excludes_may(self):
        got = sorted(e.amount_usd for e in entries_in_window(_entries(), ASOF, "mtd"))
        assert got == [10.0, 20.0, 30.0]

    def test_window_skips_unparseable_date(self):
        stub = SimpleNamespace(trade_date="not-a-date", amount_usd=1.0)
        assert entries_in_window([stub], ASOF, "mtd") == []

    def test_unknown_window_raises(self):
        try:
            entries_in_window([], ASOF, "ytd")
        except ValueError:
            return
        raise AssertionError("expected ValueError for unknown window")

    def test_format_digest_sections_and_totals(self):
        text = format_pnl_digest(_entries(), asof=ASOF)
        assert "Portfolio P&L digest* — 2026-06-18" in text
        # MTD total = 10 + 20 + 30 = 60 (May excluded)
        assert "MTD $60.00" in text
        assert "day $10.00" in text
        assert "perp_carry: $60.00" in text          # by-lane rollup
        assert "possible_1256: $60.00" in text       # by-tax-bucket rollup
        assert "Not yet wired" in text               # honest minimal-scope footer
        assert "4.70% (LOC)" in text                 # floor reference (no capital given)

    def test_format_digest_empty_is_safe(self):
        text = format_pnl_digest([], asof=ASOF)
        assert "MTD $0.00" in text
        assert "(none)" in text

    def test_format_digest_hurdle_line_with_capital(self):
        # MTD 60 on 10k since June 1 (18 days) — below the 4.70% floor.
        text = format_pnl_digest(_entries(), asof=ASOF, deployed_capital_usd=10_000.0)
        assert "LOC floor" in text
        assert ("below" in text or "beats" in text)
