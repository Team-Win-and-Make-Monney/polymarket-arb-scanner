"""Firecrawl resolution-evidence discovery with no trading authority.

This module is deliberately disconnected from scanners, prices, order books,
execution, and risk controls. It prepares a bounded evidence artifact from the
credit-capped Firecrawl Agent API; direct venue APIs remain authoritative for all
market state.
"""

from __future__ import annotations

import hashlib
import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import httpx

logger = logging.getLogger(__name__)

MAX_CREDITS = 25
MAX_EVIDENCE_ITEMS = 8

EVIDENCE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["evidence"],
    "properties": {
        "evidence": {
            "type": "array",
            "maxItems": MAX_EVIDENCE_ITEMS,
            "items": {
                "type": "object",
                "required": ["title", "url", "summary"],
                "properties": {
                    "title": {"type": "string"},
                    "url": {"type": "string"},
                    "summary": {"type": "string"},
                    "published_at": {"type": ["string", "null"]},
                },
            },
        }
    },
}


def _response_object(response: httpx.Response, context: str) -> dict[str, Any]:
    """Decode one provider response and reject malformed or non-object JSON."""
    try:
        payload = response.json()
    except ValueError as exc:
        raise RuntimeError(f"Firecrawl returned invalid JSON for {context}.") from exc
    if not isinstance(payload, dict):
        raise TypeError(f"Firecrawl returned non-object JSON for {context}.")
    return payload


# ---------------------------------------------------------------------------

class FirecrawlEvidenceClient:
    """Synchronous, credit-capped Firecrawl Agent client for evidence only."""

    def __init__(
        self,
        api_key: str,
        *,
        max_credits: int = 10,
        timeout_seconds: float = 120.0,
        poll_interval_seconds: float = 2.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        if not api_key:
            raise ValueError("FIRECRAWL_API_KEY is required.")
        if not 1 <= max_credits <= MAX_CREDITS:
            raise ValueError(f"max_credits must be between 1 and {MAX_CREDITS}.")
        self._api_key = api_key
        self._max_credits = max_credits
        self._timeout_seconds = timeout_seconds
        self._poll_interval_seconds = poll_interval_seconds
        self._transport = transport

    def discover(self, question: str, *, max_age_hours: int = 72) -> dict[str, Any]:
        """Return a normalized evidence artifact for a resolution question."""
        question = question.strip()
        if not question:
            raise ValueError("A non-empty resolution question is required.")
        if not 1 <= max_age_hours <= 24 * 365:
            raise ValueError("max_age_hours must be between 1 and 8760.")

        prompt = (
            "Find current, primary-source news or official evidence relevant to this prediction-"
            f"market resolution question: {question}. Return at most {MAX_EVIDENCE_ITEMS} items. "
            "Exclude market prices, probabilities, order books, positions, trading advice, and "
            "execution instructions. Use null published_at when the source does not state a date."
        )
        headers = {"Authorization": f"Bearer {self._api_key}", "Content-Type": "application/json"}
        deadline = time.monotonic() + self._timeout_seconds
        job_id: str | None = None
        terminal = False

        with httpx.Client(transport=self._transport) as client:
            try:
                response = client.post(
                    "https://api.firecrawl.dev/v2/agent",
                    headers=headers,
                    json={
                        "prompt": prompt,
                        "schema": EVIDENCE_SCHEMA,
                        "maxCredits": self._max_credits,
                        "model": "spark-1-mini",
                    },
                    timeout=_remaining_seconds(deadline),
                )
                response.raise_for_status()
                job_id_value = _response_object(response, "agent creation").get("id")
                if not isinstance(job_id_value, str) or not job_id_value:
                    raise RuntimeError("Firecrawl did not return an agent job id.")
                job_id = job_id_value

                final_poll = time.monotonic() >= deadline
                while True:
                    status_response = client.get(
                        f"https://api.firecrawl.dev/v2/agent/{job_id}",
                        headers=headers,
                        # Permit one bounded readback after the discovery deadline. A paid
                        # job may finish during the final sleep, and skipping that readback
                        # would incorrectly report a completed run as a timeout.
                        timeout=5.0 if final_poll else _remaining_seconds(deadline),
                    )
                    status_response.raise_for_status()
                    payload = _response_object(status_response, "agent status")
                    status = payload.get("status")
                    if status == "completed":
                        terminal = True
                        credits_used = payload.get("creditsUsed")
                        if (
                            isinstance(credits_used, (int, float))
                            and credits_used > self._max_credits
                        ):
                            raise RuntimeError(
                                "Firecrawl reported usage above the requested credit cap."
                            )
                        data = payload.get("data")
                        if not isinstance(data, dict) or not isinstance(
                            data.get("evidence"), list
                        ):
                            raise RuntimeError(
                                "Firecrawl completed without a structured evidence array."
                            )
                        return normalize_evidence(
                            question,
                            data,
                            max_age_hours=max_age_hours,
                            max_credits=self._max_credits,
                            credits_used=credits_used,
                        )
                    if status in {"failed", "cancelled"}:
                        terminal = True
                        raise RuntimeError(f"Firecrawl evidence discovery {status}.")
                    if status != "processing":
                        raise RuntimeError("Firecrawl returned an unknown agent status.")
                    if final_poll:
                        raise TimeoutError("Firecrawl evidence discovery timed out.")
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        final_poll = True
                        continue
                    if self._poll_interval_seconds > 0:
                        sleep_seconds = min(self._poll_interval_seconds, remaining)
                        time.sleep(sleep_seconds)
                        if sleep_seconds >= remaining:
                            final_poll = True
            finally:
                if job_id and not terminal:
                    _cancel_non_terminal_job(client, job_id, headers)


# ---------------------------------------------------------------------------

def normalize_evidence(
    question: str,
    data: Any,
    *,
    max_age_hours: int,
    max_credits: int,
    credits_used: Any = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Normalize evidence without turning missing dates into false freshness."""
    retrieved_at = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    raw_items = data.get("evidence", []) if isinstance(data, dict) else []
    if not isinstance(raw_items, list):
        raise TypeError("Firecrawl evidence response did not contain an evidence array.")
    if len(raw_items) > MAX_EVIDENCE_ITEMS:
        raise RuntimeError("Firecrawl evidence response exceeded the item ceiling.")

    seen_urls: set[str] = set()
    normalized: list[dict[str, Any]] = []
    for item in raw_items:
        if not isinstance(item, dict):
            continue
        url = _safe_source_url(item.get("url"))
        if not url or url in seen_urls:
            continue
        seen_urls.add(url)
        title = str(item.get("title") or "").strip()
        summary = str(item.get("summary") or "").strip()
        if not title or not summary:
            continue
        published_at = _parse_published_at(item.get("published_at"))
        freshness = _freshness(published_at, retrieved_at, max_age_hours)
        digest_input = f"{url}\n{title}\n{summary}".encode()
        normalized.append(
            {
                "title": title,
                "url": url,
                "summary": summary,
                "published_at": published_at.isoformat() if published_at else None,
                "retrieved_at": retrieved_at.isoformat(),
                "freshness": freshness,
                "content_sha256": hashlib.sha256(digest_input).hexdigest(),
            }
        )

    return {
        "schema_version": 1,
        "scope": "resolution_evidence_only",
        "question": question,
        "authorizes_trading": False,
        "market_api_remains_authoritative": True,
        "max_credits": max_credits,
        "credits_used": credits_used if isinstance(credits_used, (int, float)) else None,
        "evidence": normalized,
    }


def _safe_source_url(value: Any) -> str | None:
    try:
        parsed = urlsplit(str(value))
    except (TypeError, ValueError):
        return None
    if parsed.scheme != "https" or not parsed.hostname:
        return None
    host = parsed.hostname.lower()
    try:
        parsed_port = parsed.port
    except ValueError:
        return None
    port = f":{parsed_port}" if parsed_port and parsed_port != 443 else ""
    return urlunsplit(("https", f"{host}{port}", parsed.path or "/", "", ""))


def _parse_published_at(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _freshness(published_at: datetime | None, now: datetime, max_age_hours: int) -> str:
    if published_at is None:
        return "unknown"
    if published_at > now + timedelta(minutes=5):
        return "future_invalid"
    if published_at < now - timedelta(hours=max_age_hours):
        return "stale"
    return "current"


def _remaining_seconds(deadline: float) -> float:
    """Return a positive per-request timeout within the discovery deadline."""
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError("Firecrawl evidence discovery timed out.")
    return min(30.0, remaining)


def _cancel_non_terminal_job(
    client: httpx.Client,
    job_id: str,
    headers: dict[str, str],
) -> None:
    """Best-effort cleanup that never replaces the discovery failure."""
    try:
        cancel_response = client.delete(
            f"https://api.firecrawl.dev/v2/agent/{job_id}",
            headers=headers,
            timeout=5.0,
        )
        cancel_response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 409:
            logger.debug("Firecrawl job %s was already terminal during cleanup", job_id)
        else:
            logger.warning(
                "Firecrawl job %s cleanup returned HTTP %d",
                job_id,
                exc.response.status_code,
            )
    except httpx.HTTPError as exc:
        logger.warning("Firecrawl job %s cleanup failed: %s", job_id, exc)
