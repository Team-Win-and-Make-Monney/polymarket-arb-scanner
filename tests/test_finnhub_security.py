"""Finnhub API-key handling test (audit S11 — key in header, not URL query)."""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from finnhub_api import FinnhubNewsClient


class TestFinnhubKeyHandling:
    def test_api_key_sent_in_header(self):
        client = FinnhubNewsClient(api_key="secret-key")
        assert client._session.headers["X-Finnhub-Token"] == "secret-key"

    def test_fetch_does_not_put_token_in_query_params(self):
        client = FinnhubNewsClient(api_key="secret-key")
        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = []
        resp.raise_for_status.return_value = None
        resp.text = "[]"
        client._session.get = MagicMock(return_value=resp)

        client.fetch_company_news("AAPL", "2026-01-01", "2026-01-02")

        params = client._session.get.call_args.kwargs.get("params", {})
        assert "token" not in params              # key no longer leaks into the URL
        assert client._session.headers["X-Finnhub-Token"] == "secret-key"
