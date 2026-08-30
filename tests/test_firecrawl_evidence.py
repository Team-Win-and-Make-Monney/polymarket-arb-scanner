from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import Mock

import httpx
import pytest

from firecrawl_evidence import FirecrawlEvidenceClient, normalize_evidence
from scripts.firecrawl_resolution_evidence import main


class TestFirecrawlEvidence:
    def test_agent_request_is_credit_capped_and_evidence_only(self):
        requests: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            if request.method == "POST":
                return httpx.Response(200, json={"success": True, "id": "job-1"})
            return httpx.Response(
                200,
                json={
                    "success": True,
                    "status": "completed",
                    "creditsUsed": 6,
                    "data": {
                        "evidence": [
                            {
                                "title": "Official result",
                                "url": "https://agency.example.gov/result?token=remove#section",
                                "summary": "The agency published the final result.",
                                "published_at": "2026-08-29T12:00:00Z",
                                "price": 0.99,
                            }
                        ]
                    },
                },
            )

        client = FirecrawlEvidenceClient(
            "fixture-key",
            max_credits=10,
            poll_interval_seconds=0,
            transport=httpx.MockTransport(handler),
        )
        artifact = client.discover("Did the agency publish the result?", max_age_hours=72)

        assert artifact["scope"] == "resolution_evidence_only"
        assert artifact["authorizes_trading"] is False
        assert artifact["market_api_remains_authoritative"] is True
        assert artifact["max_credits"] == 10
        assert artifact["credits_used"] == 6
        assert artifact["evidence"][0]["url"] == "https://agency.example.gov/result"
        assert "price" not in artifact["evidence"][0]
        body = json.loads(requests[0].content)
        assert body["maxCredits"] == 10
        assert body["model"] == "spark-1-mini"
        assert "prices" in body["prompt"]
        assert "fixture-key" not in requests[0].content.decode()
        assert [request.url.path for request in requests] == ["/v2/agent", "/v2/agent/job-1"]

    def test_missing_dates_are_unknown_not_fresh_and_urls_dedupe(self):
        artifact = normalize_evidence(
            "Question",
            {
                "evidence": [
                    {
                        "title": "Undated source",
                        "url": "https://example.gov/update?a=1",
                        "summary": "No publication date is present.",
                        "published_at": None,
                    },
                    {
                        "title": "Duplicate",
                        "url": "https://example.gov/update?a=2#x",
                        "summary": "Same canonical URL.",
                        "published_at": "2026-08-29T10:00:00Z",
                    },
                    {
                        "title": "Insecure",
                        "url": "http://example.gov/update",
                        "summary": "Rejected.",
                    },
                ]
            },
            max_age_hours=72,
            max_credits=10,
            now=datetime(2026, 8, 29, 12, tzinfo=timezone.utc),
        )
        assert len(artifact["evidence"]) == 1
        assert artifact["evidence"][0]["freshness"] == "unknown"
        assert artifact["evidence"][0]["published_at"] is None

    def test_credit_bounds_fail_closed(self):
        with pytest.raises(ValueError, match="max_credits"):
            FirecrawlEvidenceClient("fixture", max_credits=26)

    def test_cli_refuses_dispatch_by_default(self):
        env = {"PATH": os.environ.get("PATH", "")}
        result = subprocess.run(
            [sys.executable, "scripts/firecrawl_resolution_evidence.py", "Test question"],
            cwd=Path.cwd(),
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == 2
        assert "Refusing external dispatch" in result.stderr

    def test_non_terminal_job_is_cancelled_at_deadline(self):
        requests: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            if request.method == "POST":
                return httpx.Response(200, json={"success": True, "id": "job-timeout"})
            if request.method == "DELETE":
                return httpx.Response(200, json={"success": True})
            return httpx.Response(200, json={"success": True, "status": "processing"})

        client = FirecrawlEvidenceClient(
            "fixture-key",
            timeout_seconds=0.01,
            poll_interval_seconds=0.02,
            transport=httpx.MockTransport(handler),
        )

        with pytest.raises(TimeoutError, match="timed out"):
            client.discover("Did the official result publish?")

        assert [(request.method, request.url.path) for request in requests] == [
            ("POST", "/v2/agent"),
            ("GET", "/v2/agent/job-timeout"),
            ("DELETE", "/v2/agent/job-timeout"),
        ]

    def test_cli_accepts_only_exact_gate_and_emits_complete_artifact(self, monkeypatch, capsys):
        client = Mock()
        client.discover.return_value = {"evidence": [], "authorizes_trading": False}
        factory = Mock(return_value=client)
        monkeypatch.setenv("FIRECRAWL_EVIDENCE_ALLOW_EXTERNAL_DISPATCH", "1")
        monkeypatch.setenv("FIRECRAWL_API_KEY", "fixture-key")

        assert main(["Approved question"], client_factory=factory) == 0
        assert json.loads(capsys.readouterr().out)["authorizes_trading"] is False
        client.discover.assert_called_once_with("Approved question", max_age_hours=72)

        monkeypatch.setenv("FIRECRAWL_EVIDENCE_ALLOW_EXTERNAL_DISPATCH", "0")
        stale_factory = Mock()
        assert main(["Stale question"], client_factory=stale_factory) == 2
        stale_factory.assert_not_called()

    def test_cli_failure_emits_no_partial_artifact(self, monkeypatch, capsys):
        client = Mock()
        client.discover.side_effect = RuntimeError("provider failed")
        monkeypatch.setenv("FIRECRAWL_EVIDENCE_ALLOW_EXTERNAL_DISPATCH", "1")
        monkeypatch.setenv("FIRECRAWL_API_KEY", "fixture-key")

        assert main(["Approved question"], client_factory=Mock(return_value=client)) == 1
        captured = capsys.readouterr()
        assert captured.out == ""
        assert "no artifact emitted" in captured.err

    def test_module_does_not_import_trading_or_market_clients(self):
        source = Path("firecrawl_evidence.py").read_text()
        forbidden = ("executor", "polymarket_api", "orderbook", "continuous", "risk_manager")
        assert all(f"import {name}" not in source for name in forbidden)
