"""Daily portfolio P&L digest cron — the live runner for ``digest.py``.

Queries the shared cross-engine ``pnl`` table in Supabase (the data-layer
integration point for all engines), rolls it up via ``pnl_ledger.aggregate_pnl``,
renders the digest with ``digest.format_pnl_digest``, and posts it to Telegram.
Deterministic, no LLM in the path (15-KPI-DASHBOARD-SPEC build note).

Reads go through PostgREST with the SERVICE-role key: the ``pnl`` table is
deny-by-default RLS (migration 0003), so the anon key cannot read it. Only
``requests`` is needed — no supabase-py dependency.

Cron-safe at every missing-config boundary: no Supabase creds or a failed query
logs and exits 0; no Telegram creds prints the digest instead of sending.

Usage:
    python scripts/run_pnl_digest.py [--dry-run] [--capital USD]
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
from datetime import datetime, timezone

import requests

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from digest import _month_start, format_pnl_digest  # noqa: E402
from pnl_ledger import PnlEntry  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
logger = logging.getLogger(__name__)

_PNL_SELECT = "engine,lane,tax_bucket,amount_usd,trade_date"


def send_telegram(token: str, chat_id: str, text: str) -> bool:
    """Send a Telegram message; never log the tokenized URL on failure."""
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    try:
        resp = requests.post(
            url,
            json={"chat_id": chat_id, "text": text, "parse_mode": "Markdown",
                  "disable_web_page_preview": True},
            timeout=15,
        )
        resp.raise_for_status()
        return True
    except requests.HTTPError as exc:
        resp = exc.response
        detail = ""
        if resp is not None:
            try:
                detail = resp.json().get("description", "")
            except ValueError:
                detail = resp.text[:200]
        status = getattr(resp, "status_code", "?")
        logger.warning("Telegram send failed: HTTP %s — %s", status, detail)
        return False
    except requests.RequestException as exc:
        logger.warning("Telegram send failed: %s", type(exc).__name__)
        return False


def fetch_pnl_rows(session, base_url: str, service_key: str, since_iso: str):
    """Fetch pnl rows with trade_date >= since_iso via PostgREST. None on error."""
    url = f"{base_url.rstrip('/')}/rest/v1/pnl"
    params = {"select": _PNL_SELECT, "trade_date": f"gte.{since_iso}",
              "order": "trade_date.asc"}
    headers = {"apikey": service_key, "Authorization": f"Bearer {service_key}",
               "Accept": "application/json"}
    try:
        resp = session.get(url, params=params, headers=headers, timeout=20)
        resp.raise_for_status()
        return resp.json()
    except (requests.RequestException, ValueError) as exc:
        logger.warning("Supabase pnl query failed: %s", type(exc).__name__)
        return None


def rows_to_entries(rows) -> list[PnlEntry]:
    """Build PnlEntry rows, skipping any that fail validation (bad bucket/date).

    Row fields are passed to PnlEntry as-is (only amount_usd is coerced to float)
    so PnlEntry's own validation rejects None / non-string values — coercing them
    to ``str()`` first would turn ``None`` into the literal ``"None"`` and let a
    corrupt row through into the rollups.
    """
    entries: list[PnlEntry] = []
    for row in rows or []:
        try:
            entries.append(PnlEntry(
                engine=row["engine"],
                lane=row["lane"],
                tax_bucket=row["tax_bucket"],
                amount_usd=float(row["amount_usd"]),
                trade_date=row["trade_date"],
            ))
        except (KeyError, ValueError, TypeError) as exc:
            logger.warning("Skipping malformed pnl row: %s", exc)
    return entries


def main() -> None:
    parser = argparse.ArgumentParser(description="Daily portfolio P&L digest")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print the digest instead of sending to Telegram")
    parser.add_argument("--capital", type=float, default=None,
                        help="Deployed capital USD for the LOC-floor hurdle line")
    args = parser.parse_args()

    base_url = os.getenv("SUPABASE_URL")
    service_key = os.getenv("SUPABASE_SERVICE_KEY") or os.getenv("SUPABASE_KEY")
    if not base_url or not service_key:
        logger.warning(
            "SUPABASE_URL / SUPABASE_SERVICE_KEY not set — cannot read the pnl "
            "table (RLS needs the service key). Cron-safe exit."
        )
        return

    asof = datetime.now(timezone.utc).date()
    since_iso = _month_start(asof).isoformat()

    rows = fetch_pnl_rows(requests.Session(), base_url, service_key, since_iso)
    if rows is None:
        logger.warning("No pnl data returned (query failed). Cron-safe exit.")
        return

    entries = rows_to_entries(rows)
    capital = args.capital
    raw_capital = os.getenv("DEPLOYED_CAPITAL_USD")
    if capital is None and raw_capital:
        try:
            capital = float(raw_capital)
        except ValueError:
            logger.warning(
                "DEPLOYED_CAPITAL_USD=%r is not a valid float; skipping the hurdle verdict.",
                raw_capital,
            )
            capital = None
    text = format_pnl_digest(entries, asof=asof, deployed_capital_usd=capital)
    logger.info("Built digest from %d pnl rows (MTD since %s)", len(entries), since_iso)

    token = os.getenv("TELEGRAM_BOT_TOKEN")
    chat_id = os.getenv("TELEGRAM_CHAT_ID")
    if args.dry_run or not token or not chat_id:
        if not token or not chat_id:
            logger.warning("TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID not set — printing only.")
        print(text)
        return

    logger.info("Telegram digest %s", "sent" if send_telegram(token, chat_id, text) else "FAILED")


if __name__ == "__main__":
    main()
