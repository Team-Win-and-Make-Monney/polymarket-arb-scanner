"""Tests for the EDGAR watcher runner (scripts/run_edgar_scan.py).

The deterministic classification lives in edgar_events (tested separately); this
pins the runner's own logic: Atom parsing of the real getcurrent feed shape, the
field extractors, the best-effort filer/primary-doc resolution, cross-run +
in-batch dedup, and the enrich->classify->format path. All network is faked — no
live SEC calls.
"""
from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from scripts.run_edgar_scan import (
    build_getcurrent_url,
    collect_events,
    enrich_filing,
    extract_filer_from_index,
    index_json_url,
    load_seen,
    parse_accession,
    parse_atom,
    parse_entry_company,
    parse_filed_date,
    pick_primary_doc,
    save_seen,
    select_new_events,
)
from edgar_events import EdgarEventType, Filing, classify_filing, format_edgar_alert, scan_filings


# A real getcurrent entry (SC 13D/A) captured live, plus a synthetic 13E-3 and
# S-4 so the feed-only signals are exercised without a secondary fetch.
SAMPLE_ATOM = """<?xml version="1.0" encoding="ISO-8859-1" ?>
<feed xmlns="http://www.w3.org/2005/Atom">
<title>Latest Filings</title>
<entry>
<title>SC 13E3 - TARGET CO INC (0000111111) (Subject)</title>
<link rel="alternate" type="text/html" href="https://www.sec.gov/Archives/edgar/data/111111/000000000026000001/0000000000-26-000001-index.htm"/>
<summary type="html"> &lt;b&gt;Filed:&lt;/b&gt; 2026-06-18 &lt;b&gt;AccNo:&lt;/b&gt; 0000000000-26-000001 </summary>
<category scheme="https://www.sec.gov/" label="form type" term="SC 13E3"/>
<id>urn:tag:sec.gov,2008:accession-number=0000000000-26-000001</id>
</entry>
<entry>
<title>S-4 - ACQUIRER CORP (0000222222) (Filer)</title>
<link rel="alternate" type="text/html" href="https://www.sec.gov/Archives/edgar/data/222222/000000000026000002/0000000000-26-000002-index.htm"/>
<summary type="html"> &lt;b&gt;Filed:&lt;/b&gt; 2026-06-19 &lt;b&gt;AccNo:&lt;/b&gt; 0000000000-26-000002 </summary>
<category scheme="https://www.sec.gov/" label="form type" term="S-4"/>
<id>urn:tag:sec.gov,2008:accession-number=0000000000-26-000002</id>
</entry>
<entry>
<title>SC 13D/A - GENCO SHIPPING &amp; TRADING LTD (0001326200) (Subject)</title>
<link rel="alternate" type="text/html" href="https://www.sec.gov/Archives/edgar/data/1326200/000110465926075210/0001104659-26-075210-index.htm"/>
<summary type="html"> &lt;b&gt;Filed:&lt;/b&gt; 2026-06-17 &lt;b&gt;AccNo:&lt;/b&gt; 0001104659-26-075210 </summary>
<category scheme="https://www.sec.gov/" label="form type" term="SC 13D/A"/>
<id>urn:tag:sec.gov,2008:accession-number=0001104659-26-075210</id>
</entry>
</feed>"""


class _FakeResp:
    def __init__(self, text):
        self.text = text

    def raise_for_status(self):
        return None


class _FakeSession:
    """Routes GETs by URL substring; records call count."""

    def __init__(self, routes):
        self.routes = routes
        self.calls = 0

    def get(self, url, headers=None, timeout=None):
        self.calls += 1
        for needle, text in self.routes.items():
            if needle in url:
                return _FakeResp(text)
        return _FakeResp("")


class TestRunEdgarScan:
    # --- field parsers -----------------------------------------------------

    def test_parse_accession(self):
        assert parse_accession(
            "urn:tag:sec.gov,2008:accession-number=0001104659-26-075210"
        ) == "0001104659-26-075210"
        assert parse_accession("garbage") == ""

    def test_parse_filed_date(self):
        assert parse_filed_date("<b>Filed:</b> 2026-06-17 <b>AccNo:</b> x") == "2026-06-17"
        assert parse_filed_date("no date here") == ""

    def test_parse_entry_company_strips_cik_and_role(self):
        assert parse_entry_company(
            "SC 13D/A - GENCO SHIPPING & TRADING LTD (0001326200) (Subject)"
        ) == "GENCO SHIPPING & TRADING LTD"
        assert parse_entry_company("S-4 - ACQUIRER CORP (0000222222) (Filer)") == "ACQUIRER CORP"

    # --- Atom feed parse ---------------------------------------------------

    def test_parse_atom_extracts_all_fields(self):
        entries = parse_atom(SAMPLE_ATOM)
        assert len(entries) == 3
        e13d = entries[2]
        assert e13d["form_type"] == "SC 13D/A"
        assert e13d["company"] == "GENCO SHIPPING & TRADING LTD"
        assert e13d["accession"] == "0001104659-26-075210"
        assert e13d["filed_at"] == "2026-06-17"
        assert e13d["url"].endswith("-index.htm")

    def test_parse_atom_bad_xml_is_safe(self):
        assert parse_atom("<not-valid") == []

    # --- secondary-fetch helpers ------------------------------------------

    def test_extract_filer_from_index(self):
        # HTML literals keep single quotes: they contain double quotes, so Ruff's
        # avoid-escape convention prefers single over escaped double quotes.
        html = '<span class="companyName">SABA CAPITAL MANAGEMENT (Filed by)</span>'
        assert extract_filer_from_index(html) == "SABA CAPITAL MANAGEMENT"
        assert extract_filer_from_index("<span>nothing</span>") is None

    def test_index_json_url(self):
        assert index_json_url(
            "https://www.sec.gov/Archives/edgar/data/1/000/0001-26-1-index.htm"
        ) == "https://www.sec.gov/Archives/edgar/data/1/000/index.json"

    def test_pick_primary_doc_prefers_largest_non_index(self):
        blob = {"directory": {"item": [
            {"name": "0001-26-1-index.htm", "size": "5000"},
            {"name": "ex99.htm", "size": "2000"},
            {"name": "tender.htm", "size": "90000"},
        ]}}
        assert pick_primary_doc(blob) == "tender.htm"
        assert pick_primary_doc({"directory": {"item": []}}) is None

    # --- state / dedup -----------------------------------------------------

    def test_state_roundtrip_and_dedup(self, tmp_path):
        path = tmp_path / "state.json"
        assert load_seen(path) == set()
        save_seen(path, {"acc-1", "acc-2"})
        assert load_seen(path) == {"acc-1", "acc-2"}

    def test_save_seen_caps_size(self, tmp_path):
        path = tmp_path / "state.json"
        save_seen(path, {f"acc-{i:05d}" for i in range(6000)})
        assert len(load_seen(path)) == 5000

    def test_select_new_events_skips_seen_dups_and_blank_accession(self):
        def ev(form, acc):
            return classify_filing(Filing(
                form_type=form, filer="", subject="X", filed_at="2026-06-18",
                accession=acc, url="",
            ))
        a = ev("SC 13E3", "acc-1")
        a_dup = ev("SC 13E3", "acc-1")          # repeated within this batch
        b = ev("S-4", "acc-2")
        seen_before = ev("SC 13E3", "acc-old")  # already alerted in a prior run
        blank = ev("S-4", "")                   # no accession -> undedupable
        new = select_new_events([a, a_dup, b, seen_before, blank], seen={"acc-old"})
        accs = [e.filing.accession for e in new]
        assert accs == ["acc-1", "acc-2"]       # dup, seen, and blank all dropped

    # --- enrich + classify integration ------------------------------------

    def test_enrich_13e3_needs_no_secondary_fetch(self):
        session = _FakeSession({})
        entry = parse_atom(SAMPLE_ATOM)[0]  # the SC 13E3
        filing = enrich_filing(entry, session, "ua")
        assert session.calls == 0  # 13E-3 classifies from metadata alone
        assert scan_filings([filing])[0].type is EdgarEventType.GOING_PRIVATE

    def test_enrich_13d_resolves_activist_filer_and_classifies(self):
        session = _FakeSession({
            "-index.htm": '<span class="companyName">SABA CAPITAL MANAGEMENT (Filed by)</span>',
        })
        entry = parse_atom(SAMPLE_ATOM)[2]  # the SC 13D/A
        filing = enrich_filing(entry, session, "ua")
        assert filing.filer == "SABA CAPITAL MANAGEMENT"
        event = scan_filings([filing])[0]
        assert event.type is EdgarEventType.ACTIVIST_13D
        assert "SABA CAPITAL MANAGEMENT" in format_edgar_alert(event)

    def test_collect_events_scans_all_forms_and_classifies(self):
        # Every form feed returns the same 3-entry sample; the SC 13D index resolves
        # to a non-watched filer, so only the 13E-3 + S-4 raise events per feed.
        session = _FakeSession({
            "action=getcurrent": SAMPLE_ATOM,
            "-index.htm": '<span class="companyName">RANDOM HOLDER LLC (Filed by)</span>',
        })
        events, scanned = collect_events(session, "ua", count=40)
        kinds = {e.type for e in events}
        assert EdgarEventType.GOING_PRIVATE in kinds
        assert EdgarEventType.MERGER_S4 in kinds
        assert EdgarEventType.ACTIVIST_13D not in kinds  # filer not on the watchlist
        assert scanned == 12  # 3 entries x 4 form feeds

    def test_build_getcurrent_url(self):
        url = build_getcurrent_url("SC 13D", 40)
        assert "action=getcurrent" in url and "output=atom" in url and "count=40" in url
