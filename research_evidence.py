"""Offline evidence gate. No imports from scanner, configuration or venue clients."""

import argparse
from collections import Counter
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import json
from pathlib import Path
import random


class EvidenceError(ValueError):
    """A record cannot establish the specified paper outcome."""


def timestamp(value):
    """Parse an explicitly timezone-aware timestamp."""
    if not isinstance(value, str):
        raise EvidenceError("timestamp_missing")
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise EvidenceError("timestamp_invalid") from exc
    if result.utcoffset() is None:
        raise EvidenceError("timestamp_naive")
    return result


def number(value, low, high):
    """Reject bools, non-finite and out-of-envelope quantities."""
    if isinstance(value, bool):
        raise EvidenceError("number_invalid")
    try:
        result = Decimal(str(value))
    except InvalidOperation as exc:
        raise EvidenceError("number_invalid") from exc
    if not result.is_finite() or not Decimal(str(low)) <= result <= Decimal(str(high)):
        raise EvidenceError("number_out_of_range")
    return result


def validate_record(row, cutoff, allow_synthetic=False):
    """Validate one frozen, one-contract paper observation and its later outcome.

    Attestations and source references preserve provenance; they do not prove
    source truth or actual fills. Inputs must be independently reviewed.
    """
    if not isinstance(row, dict) or row.get("schema_version") != 1:
        raise EvidenceError("unsupported_schema")
    if row.get("synthetic") is not False and not (allow_synthetic and row.get("synthetic") is True):
        raise EvidenceError("synthetic_or_unclassified")
    for field in ("record_id", "event_id", "contract_id", "strategy_id"):
        if not isinstance(row.get(field), str) or not row[field].strip():
            raise EvidenceError("identity_missing")
    if row.get("execution_authorized") is not False:
        raise EvidenceError("execution_boundary_missing")
    times = {key: timestamp(row.get(key)) for key in (
        "observed_at", "received_at", "decision_at", "fee_known_at",
        "recheck_observed_at", "recheck_received_at", "resolved_at", "resolution_received_at"
    )}
    if not (times["observed_at"] <= times["received_at"] <= times["decision_at"]
            < times["recheck_observed_at"] <= times["recheck_received_at"]
            < times["resolved_at"] <= times["resolution_received_at"] <= timestamp(cutoff)):
        raise EvidenceError("chronology_invalid")
    if times["fee_known_at"] > times["decision_at"]:
        raise EvidenceError("fee_lookahead")
    if (times["decision_at"] - times["observed_at"]).total_seconds() > 2:
        raise EvidenceError("stale_initial_book")
    delay = (times["recheck_observed_at"] - times["decision_at"]).total_seconds()
    if not 5 <= delay <= 30:
        raise EvidenceError("recheck_delay_outside_protocol")
    if (times["recheck_received_at"] - times["recheck_observed_at"]).total_seconds() > 2:
        raise EvidenceError("stale_recheck_book")
    for field in ("book_sha256", "recheck_book_sha256", "fee_source_sha256", "resolution_source_sha256"):
        value = row.get(field)
        if not isinstance(value, str) or len(value) != 64 or any(c not in "0123456789abcdef" for c in value):
            raise EvidenceError("source_digest_missing")
    for field in ("fee_source_url", "resolution_source_url"):
        if not isinstance(row.get(field), str) or not row[field].startswith("https://"):
            raise EvidenceError("source_url_missing")
    if row.get("resolution_verified") is not True or row.get("fee_verified") is not True:
        raise EvidenceError("source_not_verified")
    initial_ask = number(row.get("initial_ask"), 0, 1)
    recheck_ask = number(row.get("recheck_ask"), 0, 1)
    fee = number(row.get("taker_fee_usd"), 0, 1)
    number(row.get("initial_depth"), 1, 1_000_000_000)
    number(row.get("recheck_depth"), 1, 1_000_000_000)
    if row.get("side") not in ("yes", "no") or row.get("resolved_yes") not in (0, 1):
        raise EvidenceError("outcome_invalid")
    if isinstance(row.get("resolved_yes"), bool):
        raise EvidenceError("outcome_invalid")
    probability = number(row.get("decision_probability"), 0, 1)
    initial_fee = number(row.get("initial_taker_fee_usd"), 0, 1)
    if probability - initial_ask - initial_fee <= 0:
        raise EvidenceError("no_initial_edge")
    payoff = Decimal(row["resolved_yes"] if row["side"] == "yes" else 1 - row["resolved_yes"])
    return {"record_id": row["record_id"], "event_id": row["event_id"],
            "strategy_id": row["strategy_id"], "decision_at": row["decision_at"],
            "cost_adjusted_outcome_usd": payoff - recheck_ask - fee}


def evaluate(rows, cutoff, allow_synthetic=False):
    """Report exclusions, one observation per event and fixed slippage stresses."""
    timestamp(cutoff)
    rejected = Counter()
    eligible = []
    identities = Counter(row["record_id"] for row in rows if isinstance(row, dict)
                         and isinstance(row.get("record_id"), str) and row["record_id"])
    # Freeze the first observation for each declared event BEFORE checking outcomes
    # or completeness. An incomplete first observation never promotes a later one.
    def order_key(item):
        index, row = item
        try:
            at = timestamp(row.get("decision_at"))
        except (EvidenceError, AttributeError):
            at = datetime.min.replace(tzinfo=timezone.utc)
        return at, index

    ordered = sorted(enumerate(rows), key=order_key)
    seen = set()
    for _, row in ordered:
        if (isinstance(row, dict) and isinstance(row.get("record_id"), str)
                and row["record_id"] and identities[row["record_id"]] > 1):
            rejected["duplicate_record_id"] += 1
            continue
        event_id = row.get("event_id") if isinstance(row, dict) else None
        if isinstance(event_id, str) and event_id:
            if event_id in seen:
                rejected["repeated_event"] += 1
                continue
            seen.add(event_id)
        try:
            value = validate_record(row, cutoff, allow_synthetic)
            if identities[value["record_id"]] != 1:
                raise EvidenceError("duplicate_record_id")
            eligible.append(value)
        except EvidenceError as exc:
            rejected[str(exc)] += 1
    unique = eligible
    strategies = {row["strategy_id"] for row in unique}
    if len(strategies) > 1:
        raise EvidenceError("mixed_strategies_require_separate_protocols")
    outcomes = [float(row["cost_adjusted_outcome_usd"]) for row in unique]
    enough = len(outcomes) >= 30
    interval = None
    if enough:
        rng = random.Random(20260929)
        samples = sorted(sum(rng.choices(outcomes, k=len(outcomes))) / len(outcomes) - .01
                         for _ in range(2000))
        interval = [samples[49], samples[1949]]
    return {"schema_version": 1, "mode": "offline-paper-evaluation", "execution_authorized": False,
            "synthetic": allow_synthetic, "input_rows": len(rows), "eligible_independent_events": len(unique),
            "exclusions": dict(sorted(rejected.items())), "cutoff": cutoff,
            "status": "evaluated" if enough else "insufficient_evidence",
            "hypothesis_supported": bool(interval[0] > 0) if enough and not allow_synthetic else None,
            "profitability_established": False, "actual_fills_verified": False,
            "mean_outcome_usd_by_adverse_cents": {
                str(cents): sum(outcomes) / len(outcomes) - cents / 100 if outcomes else None
                for cents in (0, 1, 3)},
            "approx_95pct_event_bootstrap_primary_1cent": interval,
            "bootstrap_seed": 20260929, "bootstrap_resamples": 2000,
            "limitations": ["Displayed depth is not a fill guarantee.",
                            "Source digests and verification flags require external review.",
                            "Event IDs reduce repeated-contract counting; cross-event dependence can remain.",
                            "No actual orders, realized account P&L or trading authorization."]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path)
    parser.add_argument("--sha256", required=True, help="Previously frozen input digest")
    parser.add_argument("--cutoff", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--synthetic", action="store_true", help="Fixture validation only")
    args = parser.parse_args()
    payload = args.input.read_bytes()
    if hashlib.sha256(payload).hexdigest() != args.sha256:
        parser.error("Input integrity mismatch; preserve evidence and stop.")
    rows = [json.loads(line) for line in payload.decode().splitlines() if line.strip()]
    result = evaluate(rows, args.cutoff, args.synthetic)
    result["input_sha256"] = args.sha256
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("x") as target:
        target.write(json.dumps(result, indent=2, allow_nan=False) + "\n")


if __name__ == "__main__":
    main()
