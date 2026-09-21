"""Deterministic source, identity, time and numeric validation."""

from __future__ import annotations

import hashlib
import json
import math
import re
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from urllib.parse import urlsplit

MAX_SOURCES = 20
MAX_INPUT_BYTES = 80000
MAX_SOURCE_CHARS = 20000
_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:/-]{0,127}\Z")
_KINDS = {"filing", "news", "official", "transcript", "market_rules"}


def digest(value: object) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                     ensure_ascii=False, allow_nan=False).encode()).hexdigest()


def mapping(value: object, required: set[str], optional: set[str] = frozenset()) -> dict:
    if not isinstance(value, dict) or set(value) - required - optional:
        raise ValueError("invalid_fields")
    if required - set(value):
        raise ValueError("missing_fields")
    return value


def text(value: object, limit: int = MAX_SOURCE_CHARS) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("missing_text")
    if len(value) > limit:
        raise ValueError("input_too_large")
    return value


def identifier(value: object) -> str:
    if not isinstance(value, str) or not _ID.fullmatch(value):
        raise ValueError("invalid_id")
    return value


def ids(value: object, *, empty: bool = False, limit: int = MAX_SOURCES) -> list[str]:
    if not isinstance(value, list) or len(value) > limit or (not value and not empty):
        raise ValueError("missing_entity_ids")
    checked = [identifier(item) for item in value]
    if len(checked) != len(set(checked)):
        raise ValueError("duplicate_ids")
    return checked


def instant(value: object) -> datetime:
    if not isinstance(value, str):
        raise ValueError("missing_timestamp")
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise ValueError("invalid_timestamp") from None
    if result.tzinfo is None:
        raise ValueError("timezone_required")
    return result.astimezone(timezone.utc)


def probability(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError("invalid_probability")
    if not 0 <= value <= 1:
        raise ValueError("invalid_probability")
    return float(value)


def decimal_value(value: object) -> str:
    # Strings avoid losing exact input precision through a JSON float.
    if not isinstance(value, str) or len(value) > 64:
        raise ValueError("decimal_string_required")
    try:
        number = Decimal(value)
    except InvalidOperation:
        raise ValueError("invalid_decimal") from None
    if not number.is_finite() or number.copy_abs() > Decimal("1e18"):
        raise ValueError("invalid_decimal")
    if not -18 <= number.as_tuple().exponent <= 18 or len(number.as_tuple().digits) > 38:
        raise ValueError("decimal_precision_exceeded")
    # Decimal.normalize() can round under the ambient precision context.
    # Formatting these bounded values preserves every supplied digit exactly.
    fixed = format(number, "f")
    if "." in fixed:
        fixed = fixed.rstrip("0").rstrip(".")
    return "0" if number == 0 else fixed


def bounded(payload: object, required: set[str], optional: set[str] = frozenset()) -> dict:
    data = mapping(payload, required, optional)
    try:
        size = len(json.dumps(data, ensure_ascii=False, allow_nan=False).encode())
    except (TypeError, ValueError):
        raise ValueError("invalid_json_value") from None
    if size > MAX_INPUT_BYTES:
        raise ValueError("input_too_large")
    return data


def source(value: object, as_of: str) -> dict:
    raw = mapping(value, {"id", "text", "url", "published_at", "available_at", "captured_at", "entity_ids", "kind"},
                  {"speaker_id", "quality", "required_attention", "constraints"})
    result = {key: raw[key] for key in raw}
    identifier(raw["id"])
    text(raw["text"])
    ids(raw["entity_ids"])
    if raw["kind"] not in _KINDS:
        raise ValueError("invalid_source_kind")
    url = urlsplit(text(raw["url"], 2000))
    if url.scheme != "https" or not url.hostname or url.username or url.password:
        raise ValueError("invalid_source_url")
    times = [instant(raw[key]) for key in ("published_at", "available_at", "captured_at")]
    if not times[0] <= times[1] <= times[2] <= instant(as_of):
        raise ValueError("source_time_leakage")
    if "required_attention" in raw and not isinstance(raw["required_attention"], bool):
        raise ValueError("invalid_required_attention")
    if "speaker_id" in raw:
        identifier(raw["speaker_id"])
    if "quality" in raw and raw["quality"] not in {"reviewed", "unreviewed", "incomplete"}:
        raise ValueError("invalid_quality")
    if "constraints" in raw:
        result["constraints"] = constraints(raw["constraints"])
    return result


def sources(values: object, as_of: str, *, empty: bool = False) -> list[dict]:
    if not isinstance(values, list) or len(values) > MAX_SOURCES or (not values and not empty):
        raise ValueError("missing_sources")
    result = [source(item, as_of) for item in values]
    if len({item["id"] for item in result}) != len(result):
        raise ValueError("duplicate_source_ids")
    return result


def evidence(values: list[dict]) -> list[dict]:
    return [{"source_id": item["id"], "text": item["text"], "url": item["url"],
             "sha256": hashlib.sha256(item["text"].encode()).hexdigest(),
             **{key: item[key] for key in ("published_at", "available_at", "captured_at")}}
            for item in values]


def trusted(item: dict, hosts: object) -> None:
    if not isinstance(hosts, list) or not hosts or len(hosts) > MAX_SOURCES:
        raise ValueError("missing_trusted_hosts")
    if any(not isinstance(host, str) or not re.fullmatch(r"[a-z0-9.-]+", host) for host in hosts):
        raise ValueError("invalid_trusted_hosts")
    if urlsplit(item["url"]).hostname not in hosts:
        raise ValueError("untrusted_source_host")


def threshold(value: object) -> dict | None:
    if value is None:
        return None
    row = mapping(value, {"metric", "operator", "value", "unit"})
    if row["operator"] not in {"gt", "gte", "lt", "lte", "eq"}:
        raise ValueError("invalid_operator")
    return {"metric": identifier(row["metric"]), "operator": row["operator"],
            "value": decimal_value(row["value"]), "unit": identifier(row["unit"])}


def constraints(value: object) -> dict:
    row = mapping(value, set(), {"event_id", "window_start", "window_end", "threshold", "direction"})
    result = dict(row)
    if "event_id" in row:
        identifier(row["event_id"])
    if ("window_start" in row) != ("window_end" in row):
        raise ValueError("missing_event_window")
    if "window_start" in row:
        start, end = instant(row["window_start"]), instant(row["window_end"])
        if start > end:
            raise ValueError("invalid_event_window")
        result.update(window_start=start.isoformat(), window_end=end.isoformat())
    if "threshold" in row:
        result["threshold"] = threshold(row["threshold"])
    if "direction" in row and row["direction"] not in {"yes", "no"}:
        raise ValueError("invalid_direction")
    return result


def contract(value: object, as_of: str) -> dict:
    row = mapping(value, {"id", "title", "entity_ids", "rules", "rules_complete", "resolution_source_id", "terms"})
    identifier(row["id"])
    text(row["title"], 2000)
    ids(row["entity_ids"])
    identifier(row["resolution_source_id"])
    if row["rules_complete"] is not True:
        raise ValueError("missing_complete_rules")
    rules = source(row["rules"], as_of)
    if rules["kind"] != "market_rules" or not set(row["entity_ids"]) <= set(rules["entity_ids"]):
        raise ValueError("rules_identity_mismatch")
    terms = constraints(row["terms"])
    if set(terms) != {"event_id", "window_start", "window_end", "threshold", "direction"}:
        raise ValueError("missing_contract_terms")
    return {**row, "rules": rules, "terms": terms}


def mismatches(left: dict, right: dict) -> list[str]:
    """Compare only independently supplied, normalized fields; never parse prose."""
    return [key for key in sorted(set(left) & set(right)) if left[key] != right[key]]


def chronological_pair(previous: dict, current: dict) -> None:
    if previous["id"] == current["id"] or instant(previous["captured_at"]) > instant(current["available_at"]):
        raise ValueError("comparison_time_leakage")
