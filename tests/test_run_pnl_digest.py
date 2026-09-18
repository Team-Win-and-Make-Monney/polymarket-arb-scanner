"""Tests for the P&L digest runner (scripts/run_pnl_digest.py).

Covers row->PnlEntry building (with graceful skip of malformed rows, including
None fields), the PostgREST fetch (success + error -> None), and that a failed
Telegram send never leaks the bot token into logs. All network is faked; the
runner imports only ``requests`` (a hard dep) plus the pure ``digest`` /
``pnl_ledger`` modules, so no external-SDK stubbing is needed.
"""
from __future__ import annotations

import os
import sys
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from scripts.run_pnl_digest import (
    fetch_pnl_rows,
    rows_to_entries,
    send_telegram,
)
import scripts.run_pnl_digest as runner


class _Resp:
    def __init__(self, payload=None, raise_exc=None, status=200, text=""):
        self._payload = payload
        self._raise = raise_exc
        self.status_code = status
        self.text = text

    def raise_for_status(self):
        if self._raise:
            raise self._raise

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


class _Session:
    def __init__(self, resp):
        self._resp = resp
        self.last_url = None
        self.last_params = None
        self.last_headers = None

    def get(self, url, params=None, headers=None, timeout=None):
        self.last_url, self.last_params, self.last_headers = url, params, headers
        return self._resp


class TestRunPnlDigest:
    def test_rows_to_entries_builds_and_skips_malformed(self):
        rows = [
            {"engine": "quant", "lane": "perp_carry", "tax_bucket": "possible_1256",
             "amount_usd": "12.5", "trade_date": "2026-06-18"},
            {"engine": "arbgrid", "lane": "pm", "tax_bucket": "weird",  # bad bucket
             "amount_usd": "1", "trade_date": "2026-06-18"},
            {"engine": "arbgrid", "lane": "pm", "amount_usd": "1",       # missing key
             "trade_date": "2026-06-18"},
            {"engine": None, "lane": "pm", "tax_bucket": "ordinary",     # None field
             "amount_usd": "1", "trade_date": "2026-06-18"},
        ]
        entries = rows_to_entries(rows)
        assert len(entries) == 1                 # only the clean row survives
        assert entries[0].amount_usd == 12.5
        assert rows_to_entries(None) == []

    def test_fetch_pnl_rows_success_and_query_shape(self):
        payload = [{"engine": "quant", "lane": "perp_carry", "tax_bucket": "possible_1256",
                    "amount_usd": 5.0, "trade_date": "2026-06-18"}]
        session = _Session(_Resp(payload=payload))
        rows = fetch_pnl_rows(session, "https://proj.supabase.co/", "svc-key", "2026-06-01")
        assert rows == payload
        assert session.last_url == "https://proj.supabase.co/rest/v1/pnl"
        assert session.last_params["trade_date"] == "gte.2026-06-01"
        # Service key must ride both the apikey and bearer headers (RLS bypass).
        assert session.last_headers["apikey"] == "svc-key"
        assert session.last_headers["Authorization"] == "Bearer svc-key"

    def test_fetch_pnl_rows_error_returns_none(self):
        import requests
        session = _Session(_Resp(raise_exc=requests.HTTPError("500")))
        assert fetch_pnl_rows(session, "https://proj.supabase.co", "k", "2026-06-01") is None

    def test_send_telegram_failure_does_not_leak_token(self, caplog):
        import requests
        leak_canary = "do-not-log-token"
        err = requests.HTTPError(
            f"400 ... https://api.telegram.org/bot{leak_canary}/sendMessage"
        )
        err.response = _Resp(payload={"description": "Bad Request: chat not found"},
                             status=400)

        with patch.object(runner.requests, "post", side_effect=err):
            with caplog.at_level("WARNING"):
                ok = send_telegram(leak_canary, "999", "hi")

        assert ok is False
        assert "chat not found" in caplog.text
        assert leak_canary not in caplog.text

    def test_send_telegram_success(self):
        with patch.object(runner.requests, "post", return_value=_Resp(payload={"ok": True})):
            assert send_telegram("tok", "chat", "hello") is True
