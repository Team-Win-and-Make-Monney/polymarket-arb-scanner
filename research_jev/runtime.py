"""Portable TypeSafe client with bounded I/O and validated typed judgments.

Version 1.0.0. No action authority or automatic retries. The byte budget is an
application limit, not an exact TypeSafe token count. Source order is retained in
hashes because changing Choice option order can change a model's answer.
"""

from __future__ import annotations

import hashlib
import http.client
import json
import math
import os
import re
import socket
import time
from collections.abc import Callable
from typing import Any

RUNTIME_VERSION = "1.0.0"
DEFAULT_MODEL = "jev-1.13.0"
_MODES = {"off", "shadow", "advisory"}
_MODEL = re.compile(r"jev-[0-9]+\.[0-9]+\.[0-9]+\Z")
_MAX_SAFE_INTEGER = 9_007_199_254_740_991
Transport = Callable[[bytes, str, float, int], bytes]


class _Failure(Exception):
    """A fixed, non-sensitive failure code."""


def _json_value(value: Any, depth: int = 0, active: set[int] | None = None) -> None:
    if depth > 32:
        raise _Failure("invalid_json")
    if value is None or isinstance(value, (str, bool)):
        return
    if isinstance(value, (int, float)):
        if abs(value) > _MAX_SAFE_INTEGER or not math.isfinite(value):
            raise _Failure("invalid_json")
        return
    active = set() if active is None else active
    if not isinstance(value, (dict, list)) or id(value) in active:
        raise _Failure("invalid_json")
    active.add(id(value))
    try:
        if isinstance(value, dict):
            if any(not isinstance(key, str) for key in value):
                raise _Failure("invalid_json")
            for item in value.values():
                _json_value(item, depth + 1, active)
        else:
            for item in value:
                _json_value(item, depth + 1, active)
    finally:
        active.remove(id(value))


def _encode(value: Any) -> bytes:
    _json_value(value)
    try:
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"),
                          allow_nan=False).encode("utf-8")
    except (ValueError, UnicodeError, OverflowError, TypeError, RecursionError):
        raise _Failure("invalid_json") from None


def content_hash(value: Any) -> str | None:
    """Hash deterministic JSON retaining key order; invalid input has no hash."""
    try:
        return hashlib.sha256(_encode(value)).hexdigest()
    except _Failure:
        return None


def _object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise _Failure("duplicate_json_key")
        result[key] = value
    return result


def _reject_constant(_: str) -> Any:
    raise _Failure("invalid_json")


def parse_json(data: bytes | str) -> Any:
    """Decode JSON without accepting duplicate keys or non-finite constants."""
    try:
        result = json.loads(data, object_pairs_hook=_object, parse_constant=_reject_constant)
        _json_value(result)
        return result
    except _Failure:
        raise
    except (ValueError, UnicodeError, TypeError, RecursionError):
        raise _Failure("invalid_json") from None


def _description(value: Any, *, nullable: bool = False) -> bool:
    return (nullable and value is None) or isinstance(value, (str, dict, list))


def validate_questions(questions: Any) -> None:
    if not isinstance(questions, dict) or not 1 <= len(questions) <= 128:
        raise _Failure("invalid_questions")
    for key, question in questions.items():
        if not key or len(key) > 128 or not isinstance(question, dict):
            raise _Failure("invalid_questions")
        if set(question) - {"type", "instructions", "criteria"}:
            raise _Failure("invalid_questions")
        kind = question.get("type")
        instructions = question.get("instructions")
        if not _description(instructions) or not instructions:
            raise _Failure("invalid_questions")
        criteria = question.get("criteria")
        if kind == "noul":
            if criteria is not None and (
                not isinstance(criteria, dict) or set(criteria) - {"true", "false"}
                or any(not _description(v) for v in criteria.values())
            ):
                raise _Failure("invalid_questions")
        elif kind == "choice":
            if not isinstance(criteria, dict) or not 1 <= len(criteria) <= 255:
                raise _Failure("invalid_questions")
            if any(not k or not _description(v, nullable=True) for k, v in criteria.items()):
                raise _Failure("invalid_questions")
        elif kind == "score":
            if not isinstance(criteria, list) or not 2 <= len(criteria) <= 10:
                raise _Failure("invalid_questions")
            if any(not _description(v) for v in criteria):
                raise _Failure("invalid_questions")
        else:
            raise _Failure("invalid_questions")


def _number(value: Any, low: float, high: float) -> bool:
    return (isinstance(value, (float, int)) and not isinstance(value, bool)
            and low <= value <= high and math.isfinite(value))


def validate_response(response: Any, questions: dict[str, Any], model: str) -> tuple[dict, dict | None]:
    if not isinstance(response, dict) or response.get("model") != model:
        raise _Failure("model_mismatch")
    answers = response.get("answers")
    if not isinstance(answers, dict) or set(answers) != set(questions):
        raise _Failure("invalid_answers")
    for key, question in questions.items():
        answer = answers[key]
        kind = question["type"]
        if not isinstance(answer, dict) or answer.get("type") != kind:
            raise _Failure("invalid_answers")
        expected_fields = {"type", "noul"} if kind == "noul" else (
            {"type", "choice", "probabilities", "confidence"} if kind == "choice"
            else {"type", "score", "legend", "probabilities", "confidence"}
        )
        if set(answer) != expected_fields:
            raise _Failure("invalid_answers")
        if kind == "noul":
            if not _number(answer["noul"], 0, 1):
                raise _Failure("invalid_probability")
            continue
        probabilities = answer["probabilities"]
        criteria = question["criteria"]
        expected = set(criteria) if kind == "choice" else {str(i) for i in range(len(criteria))}
        if not isinstance(probabilities, dict) or set(probabilities) != expected:
            raise _Failure("invalid_probabilities")
        if any(not _number(v, 0, 1) for v in probabilities.values()):
            raise _Failure("invalid_probability")
        if abs(sum(probabilities.values()) - 1) > 0.02:
            raise _Failure("invalid_distribution")
        if not _number(answer["confidence"], 0, 1):
            raise _Failure("invalid_confidence")
        if kind == "choice":
            choice = answer["choice"]
            if not isinstance(choice, str) or choice not in expected:
                raise _Failure("invalid_choice")
            if probabilities[choice] + 1e-9 < max(probabilities.values()):
                raise _Failure("invalid_choice")
        else:
            legend = answer["legend"]
            if not isinstance(legend, dict) or set(legend) != expected:
                raise _Failure("invalid_legend")
            if any(legend[str(i)] != criterion for i, criterion in enumerate(criteria)):
                raise _Failure("invalid_legend")
            score = answer["score"]
            weighted = sum(int(k) * p for k, p in probabilities.items())
            if not _number(score, 0, len(criteria) - 1) or abs(score - weighted) > 0.05:
                raise _Failure("invalid_score")
    usage = response.get("usage")
    if usage is not None:
        if not isinstance(usage, dict):
            raise _Failure("invalid_usage")
        for key in ("input_tokens", "output_tokens"):
            if not isinstance(usage.get(key), int) or isinstance(usage[key], bool) or usage[key] < 0:
                raise _Failure("invalid_usage")
        usage = {k: usage[k] for k in ("input_tokens", "output_tokens")}
    return answers, usage


def _http_transport(body: bytes, api_key: str, timeout: float, max_bytes: int) -> bytes:
    deadline = time.monotonic() + timeout
    connection = http.client.HTTPSConnection("api.typesafe.ai", timeout=timeout)

    def remaining() -> float:
        budget = deadline - time.monotonic()
        if budget <= 0:
            raise _Failure("timeout")
        if connection.sock is not None:
            connection.sock.settimeout(budget)
        return budget

    try:
        connection.request("POST", "/v1/systemone", body=body, headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json", "Accept": "application/json",
            "User-Agent": f"jev-portfolio/{RUNTIME_VERSION}",
        })
        remaining()
        response = connection.getresponse()
        remaining()
        if response.status != 200:
            code = "http_redirect" if 300 <= response.status < 400 else f"http_{response.status}"
            raise _Failure(code)
        content_length = response.getheader("Content-Length")
        if content_length is not None:
            try:
                length = int(content_length)
            except ValueError:
                raise _Failure("invalid_response") from None
            if length < 0 or length > max_bytes:
                raise _Failure("response_too_large")
        chunks: list[bytes] = []
        total = 0
        while True:
            remaining()
            chunk = response.read1(min(65536, max_bytes + 1 - total))
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
            if total > max_bytes:
                raise _Failure("response_too_large")
        remaining()
        return b"".join(chunks)
    except _Failure:
        raise
    except (TimeoutError, socket.timeout):
        raise _Failure("timeout") from None
    except (OSError, http.client.HTTPException, ValueError):
        raise _Failure("network_error") from None
    finally:
        connection.close()


class JevClient:
    """Validated, opt-in TypeSafe transport. Results never authorize an action."""

    def __init__(self, api_key: str | None = None, model: str = DEFAULT_MODEL,
                 mode: str = "off", timeout_seconds: float = 3.0,
                 max_request_bytes: int = 100000, max_response_bytes: int = 1000000,
                 transport: Transport | None = None):
        self._key = api_key if api_key is not None else os.environ.get("TYPESAFE_API_KEY")
        self.model = model
        self.mode = mode
        self.timeout_seconds = timeout_seconds
        self.max_request_bytes = max_request_bytes
        self.max_response_bytes = max_response_bytes
        self._transport = transport or _http_transport

    def evaluate(self, state: Any, questions: Any) -> dict[str, Any]:
        start = time.monotonic()
        model_valid = isinstance(self.model, str) and _MODEL.fullmatch(self.model) is not None
        mode_valid = isinstance(self.mode, str) and self.mode in _MODES
        result: dict[str, Any] = {
            "status": "invalid", "mode": self.mode if mode_valid else "off",
            "model": self.model if model_valid else None, "answers": {}, "usage": None,
            "elapsed_ms": 0.0, "state_hash": content_hash(state),
            "question_hash": content_hash(questions), "error_code": None,
        }
        try:
            if not mode_valid:
                raise _Failure("invalid_mode")
            if not model_valid:
                raise _Failure("invalid_model")
            if not _number(self.timeout_seconds, 0.01, 60):
                raise _Failure("invalid_timeout")
            for size in (self.max_request_bytes, self.max_response_bytes):
                if isinstance(size, bool) or not isinstance(size, int) or not 1 <= size <= 10_000_000:
                    raise _Failure("invalid_budget")
            if not isinstance(state, (str, dict, list)):
                raise _Failure("invalid_state")
            body = _encode({"model": self.model, "state": state, "questions": questions})
            validate_questions(questions)
            if len(body) > self.max_request_bytes:
                raise _Failure("request_too_large")
            if self.mode == "off":
                result["status"] = "off"
                return result
            if not isinstance(self._key, str) or not self._key.strip():
                result.update(status="unavailable", error_code="missing_api_key")
                return result
            if not self._key.isascii() or any(c.isspace() for c in self._key):
                raise _Failure("invalid_api_key")
            try:
                raw = self._transport(body, self._key, self.timeout_seconds, self.max_response_bytes)
            except _Failure as exc:
                result.update(status="unavailable", error_code=str(exc))
                return result
            except (TimeoutError, socket.timeout):
                result.update(status="unavailable", error_code="timeout")
                return result
            except Exception:
                result.update(status="unavailable", error_code="network_error")
                return result
            if time.monotonic() - start > self.timeout_seconds:
                result.update(status="unavailable", error_code="timeout")
                return result
            if not isinstance(raw, bytes):
                raise _Failure("invalid_response")
            if len(raw) > self.max_response_bytes:
                raise _Failure("response_too_large")
            answers, usage = validate_response(parse_json(raw), questions, self.model)
            result.update(status="ok", answers=answers, usage=usage)
            return result
        except _Failure as exc:
            result["error_code"] = str(exc)
            return result
        finally:
            result["elapsed_ms"] = round((time.monotonic() - start) * 1000, 3)


def confident_choice(evaluation: dict[str, Any], question_id: str,
                     minimum: float = 0.8, no_match: tuple[str, ...] = ("unknown", "none")) -> str | None:
    """Read a valid candidate label; the threshold is experimental, not accuracy."""
    if evaluation.get("status") != "ok":
        return None
    answer = evaluation.get("answers", {}).get(question_id, {})
    if answer.get("type") != "choice" or not _number(answer.get("confidence"), minimum, 1):
        return None
    choice = answer.get("choice")
    return choice if isinstance(choice, str) and choice not in no_match else None
