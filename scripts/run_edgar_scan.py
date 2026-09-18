"""SEC EDGAR corporate-action watcher — the live runner for ``edgar_events``.

Polls the SEC EDGAR ``getcurrent`` Atom feeds for the four watched form types,
classifies each filing through the deterministic ``edgar_events`` core, and
sends a Telegram ticket for every *new* hit (deduped by accession number across
runs via a JSON state file). Detection only — IBKR is the human tender rail, no
order ever places here.

Two of the four signals need a field the feed doesn't carry, so they get a
best-effort secondary fetch of the filing index (graceful on failure — a miss
just means no alert for that one, never a crash or a false positive):
  * SC 13D   -> the activist filer name (feed entry only names the subject)
  * SC TO-I  -> the filing body, scanned for the "odd lot" provision

Data source is zero-auth and free; SEC only asks for a declaring User-Agent
(set ``SEC_USER_AGENT`` with contact info). Cron-safe: the scan always runs
(SEC is public); if Telegram creds are absent it logs what it would have sent
and exits 0.

Usage:
    python scripts/run_edgar_scan.py [--always-alert] [--state-file PATH] [--count N]
"""
from __future__ import annotations

import argparse
import html as html_lib
import json
import logging
import os
import re
import sys
import time
from pathlib import Path

import defusedxml.ElementTree as ET
import requests
from defusedxml.common import DefusedXmlException

# Allow `from edgar_events import ...` / `from notifier import ...` when run as
# a script from the repo root (scripts/ would otherwise be sys.path[0]).
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from edgar_events import Filing, format_edgar_alert, scan_filings  # noqa: E402
from notifier import WebhookNotifier  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
logger = logging.getLogger(__name__)

# The form types worth a ticket (see edgar_events module docstring).
WATCHED_FORMS = ["SC TO-I", "SC 13E3", "S-4", "SC 13D"]

# SEC's fair-access policy 403s any client without a declaring User-Agent that
# carries contact info, so SEC_USER_AGENT is required (see main()). No contact
# is hard-coded here — that would bake a personal email into the repo.
_GETCURRENT = "https://www.sec.gov/cgi-bin/browse-edgar"
_REQUEST_DELAY_S = 0.3   # polite spacing; SEC tolerates up to 10 req/s
_MAX_STATE = 5_000       # cap remembered accessions so the state file can't grow unbounded


# ---------------------------------------------------------------------------
# Pure parsing helpers (no network — unit-tested directly)
# ---------------------------------------------------------------------------

def build_getcurrent_url(form_type: str, count: int) -> str:
    """Build the EDGAR getcurrent Atom URL for a form type."""
    from urllib.parse import urlencode
    query = urlencode({
        "action": "getcurrent",
        "type": form_type,
        "company": "",
        "dateb": "",
        "owner": "include",
        "count": count,
        "output": "atom",
    })
    return f"{_GETCURRENT}?{query}"


def _local(tag: str) -> str:
    """Strip the XML namespace from a tag (``{ns}entry`` -> ``entry``)."""
    return tag.rsplit("}", 1)[-1]


def parse_accession(id_text: str) -> str:
    """Pull the accession number out of an Atom entry ``<id>`` urn."""
    marker = "accession-number="
    if marker in (id_text or ""):
        return id_text.split(marker, 1)[1].strip()
    return ""


def parse_filed_date(summary: str) -> str:
    """Extract the ISO filed date from an entry ``<summary>`` ("Filed: …")."""
    match = re.search(r"\d{4}-\d{2}-\d{2}", summary or "")
    return match.group(0) if match else ""


def parse_entry_company(title: str) -> str:
    """Extract the company name from an entry title ``FORM - NAME (CIK) (Role)``."""
    name = title or ""
    if " - " in name:
        name = name.split(" - ", 1)[1]
    # Drop a trailing "(123456) (Subject)" / "(Filer)" tail.
    name = re.sub(r"\s*\(\d{6,}\)\s*\([^()]*\)\s*$", "", name)
    name = re.sub(r"\s*\([^()]*\)\s*$", "", name)
    return html_lib.unescape(name).strip()


def parse_atom(xml_text: str) -> list[dict]:
    """Parse a getcurrent Atom feed into a list of raw entry dicts.

    Namespace-agnostic so it survives the default Atom namespace. Each dict has
    ``form_type``, ``company``, ``accession``, ``filed_at`` and ``url``. Parses
    with defusedxml: the feed is network-sourced, so malformed XML or a hostile
    entity-expansion / XXE payload degrades to an empty feed rather than raising.
    """
    try:
        root = ET.fromstring(xml_text)
    except (ET.ParseError, DefusedXmlException) as exc:
        logger.warning("EDGAR feed parse failed: %s", exc)
        return []

    entries: list[dict] = []
    for entry in (el for el in root.iter() if _local(el.tag) == "entry"):
        title = form = accession = filed = url = ""
        for child in entry:
            name = _local(child.tag)
            if name == "title":
                title = (child.text or "").strip()
            elif name == "category":
                form = (child.get("term") or form).strip()
            elif name == "id":
                accession = parse_accession(child.text or "")
            elif name == "summary":
                filed = parse_filed_date("".join(child.itertext()))
            elif name == "link" and child.get("rel") in (None, "alternate"):
                url = child.get("href") or url
        entries.append({
            "form_type": form,
            "company": parse_entry_company(title),
            "accession": accession,
            "filed_at": filed,
            "url": url,
        })
    return entries


def extract_filer_from_index(index_html: str) -> str | None:
    """Extract the "(Filed by)" entity from a filing index page, or None."""
    match = re.search(
        r'companyName">\s*(.*?)\s*\(Filed by\)', index_html or "", re.I | re.S
    )
    if match:
        return html_lib.unescape(match.group(1)).strip() or None
    return None


def index_json_url(index_htm_url: str) -> str:
    """Map a ``…/<accession>-index.htm`` URL to the directory's ``index.json``."""
    return index_htm_url.rsplit("/", 1)[0] + "/index.json"


def pick_primary_doc(index_json: dict) -> str | None:
    """Pick the largest non-index document name from an EDGAR ``index.json``."""
    items = (index_json.get("directory") or {}).get("item") or []
    docs = [
        it for it in items
        if str(it.get("name", "")).lower().endswith((".htm", ".html", ".txt"))
        and "index" not in str(it.get("name", "")).lower()
    ]
    if not docs:
        return None
    docs.sort(key=lambda it: int(it.get("size") or 0), reverse=True)
    return str(docs[0]["name"])


# ---------------------------------------------------------------------------
# State (dedup across runs)
# ---------------------------------------------------------------------------

def load_seen(path: Path | None) -> set[str]:
    """Load the set of already-alerted accession numbers."""
    if not path or not path.exists():
        return set()
    try:
        return set(json.loads(path.read_text()).get("seen", []))
    except (ValueError, OSError) as exc:
        logger.warning("EDGAR state read failed (%s) — treating as empty", exc)
        return set()


def save_seen(path: Path | None, seen: set[str]) -> None:
    """Persist seen accessions, capped to the most recent ``_MAX_STATE``."""
    if not path:
        return
    trimmed = sorted(seen)[-_MAX_STATE:]
    try:
        path.write_text(json.dumps({"seen": trimmed}))
    except OSError as exc:
        logger.warning("EDGAR state write failed: %s", exc)


# ---------------------------------------------------------------------------
# Network glue (thin; parsing above is what's tested hard)
# ---------------------------------------------------------------------------

def _get(session: requests.Session, url: str, ua: str, timeout: int = 20) -> str | None:
    """GET text from SEC, returning None on any error (never raises)."""
    try:
        resp = session.get(url, headers={"User-Agent": ua}, timeout=timeout)
        resp.raise_for_status()
        return resp.text
    except requests.RequestException as exc:
        logger.warning("EDGAR fetch failed for %s: %s", url, exc)
        return None


def enrich_filing(entry: dict, session: requests.Session, ua: str) -> Filing:
    """Build a Filing, doing a best-effort secondary fetch where the core needs it.

    SC 13D -> resolve the activist filer from the index page.
    SC TO-I -> pull the primary document text for the odd-lot keyword check.
    Any fetch/parse failure degrades silently: the field stays empty and the
    core simply won't raise that signal (no false positive).
    """
    form = (entry.get("form_type") or "").upper()
    filer = ""
    body = ""
    url = entry.get("url", "")

    if url and ("SC 13D" in form or "SC TO-I" in form):
        index_html = _get(session, url, ua)
        time.sleep(_REQUEST_DELAY_S)
        if index_html:
            if "SC 13D" in form:
                filer = extract_filer_from_index(index_html) or ""
            if "SC TO-I" in form:
                raw = _get(session, index_json_url(url), ua)
                time.sleep(_REQUEST_DELAY_S)
                if raw:
                    try:
                        doc = pick_primary_doc(json.loads(raw))
                    except ValueError:
                        doc = None
                    if doc:
                        doc_url = url.rsplit("/", 1)[0] + "/" + doc
                        body = (_get(session, doc_url, ua) or "")[:20_000]
                        time.sleep(_REQUEST_DELAY_S)

    return Filing(
        form_type=entry.get("form_type", ""),
        filer=filer,
        subject=entry.get("company", ""),
        filed_at=entry.get("filed_at", ""),
        accession=entry.get("accession", ""),
        url=url,
        body_excerpt=body,
    )


def collect_events(session: requests.Session, ua: str, count: int):
    """Poll every watched form feed and return (events, n_scanned)."""
    events = []
    scanned = 0
    for form in WATCHED_FORMS:
        xml_text = _get(session, build_getcurrent_url(form, count), ua)
        time.sleep(_REQUEST_DELAY_S)
        if not xml_text:
            continue
        entries = parse_atom(xml_text)
        scanned += len(entries)
        filings = [enrich_filing(e, session, ua) for e in entries]
        events.extend(scan_filings(filings))
    return events, scanned


def select_new_events(events, seen: set[str]):
    """Filter matched events down to genuinely new, dedupable ones.

    An event is new when it has a non-empty accession that is neither already in
    ``seen`` (prior runs) nor repeated earlier in this same batch (feeds can
    echo an entry). Events with no accession are dropped — they can't be deduped,
    so alerting them would re-fire on every run.
    """
    new_events = []
    batch: set[str] = set()
    for event in events:
        acc = event.filing.accession
        if not acc or acc in seen or acc in batch:
            continue
        batch.add(acc)
        new_events.append(event)
    return new_events


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="SEC EDGAR corporate-action watcher")
    parser.add_argument("--state-file", type=Path, default=None,
                        help="JSON file of already-alerted accession numbers")
    parser.add_argument("--count", type=int, default=40,
                        help="Recent filings to pull per form type (default 40)")
    parser.add_argument("--always-alert", action="store_true",
                        help="Send a heartbeat even when there are no new events")
    args = parser.parse_args()

    ua = (os.getenv("SEC_USER_AGENT") or "").strip()
    if not ua:
        logger.warning(
            "SEC_USER_AGENT not set — SEC's fair-access policy 403s requests "
            "without a declaring contact UA (e.g. 'arbgrid you@example.com'). "
            "Skipping scan. Cron-safe exit."
        )
        return
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    chat_id = os.getenv("TELEGRAM_CHAT_ID")

    session = requests.Session()
    logger.info("Scanning EDGAR getcurrent for: %s", ", ".join(WATCHED_FORMS))
    events, scanned = collect_events(session, ua, args.count)

    seen = load_seen(args.state_file)
    new_events = select_new_events(events, seen)
    logger.info("Scanned %d filings; %d matched, %d new", scanned, len(events), len(new_events))

    alerted: set[str] = set()
    if not token or not chat_id:
        would_fire = ", ".join(
            f"{e.type.value}:{e.filing.accession}" for e in new_events
        ) or "none"
        logger.warning(
            "TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID not set — not alerting "
            "(%d new events would have fired: %s). Cron-safe exit.",
            len(new_events), would_fire,
        )
    else:
        notifier = WebhookNotifier("telegram")
        for event in new_events:
            notifier.notify_text(format_edgar_alert(event))
            alerted.add(event.filing.accession)
            logger.info("Alerted: %s %s", event.type.value, event.filing.accession)
        if args.always_alert:
            notifier.notify_text(
                f"🛰️ EDGAR watcher alive — scanned {scanned} filings across "
                f"{len(WATCHED_FORMS)} forms, {len(new_events)} new event(s)."
            )

    # Record only what we actually alerted, so a credentials outage doesn't
    # permanently suppress events once creds are restored.
    seen.update(alerted)
    save_seen(args.state_file, seen)


if __name__ == "__main__":
    main()
