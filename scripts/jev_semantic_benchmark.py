#!/usr/bin/env python3
"""Bounded offline/live semantic benchmark. No execution, account, or trading imports."""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from jev_client import JevClient, JevError
from jev_semantics import PROMPT_VERSION, equivalence_questions, news_question


def questions_for(task: str) -> dict:
    if task == "news":
        return {"outcome_resolution": news_question()}
    if task == "equivalence":
        return equivalence_questions()
    raise ValueError("Unsupported task")


def baseline(row: dict) -> str:
    """Frozen simple baseline; used to measure incremental semantic value."""
    state = row["state"]
    if row["task"] == "equivalence":
        a, b = state.get("rules_a", "").strip(), state.get("rules_b", "").strip()
        if not a or not b:
            return "uncertain"
        return "identical" if a == b else "divergent"
    text = (state.get("headline", "") + " " + state.get("summary", "")).lower()
    if any(w in text for w in ("approved", "completed", "confirmed", "launched")):
        return "resolves_yes"
    if any(w in text for w in ("rejected", "cancelled", "blocked", "denied")):
        return "resolves_no"
    return "neutral_unclear"


def evaluate(rows: list[dict], client=None) -> dict:
    records = []
    for row in rows:
        questions = questions_for(row["task"])
        payload = {"state": row["state"], "questions": questions}
        record = {"id": row["id"], "task": row["task"], "expected": row.get("expected"),
                  "baseline": baseline(row), "observed_at": datetime.now(timezone.utc).isoformat(),
                  "input_sha256": hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest(),
                  "prompt_version": PROMPT_VERSION, "status": "offline", "auto_accepted": False}
        if client is not None:
            start = time.monotonic()
            try:
                response = client.query_decisions(**payload)
                key = "outcome_resolution" if row["task"] == "news" else "is_equivalent"
                answer = response["answers"][key]
                record.update(status="ok", predicted=answer["choice"], confidence=answer["confidence"],
                              answers=response["answers"], model=response.get("model"), usage=response.get("usage", {}))
                conf = answer["confidence"]
                accepted = conf >= 0.90 and answer["choice"] not in {"uncertain", "neutral_unclear"}
                if row["task"] == "news":
                    accepted = accepted and bool(row["state"].get("settlement_rules", "").strip())
                if row["task"] == "equivalence":
                    # Match the operational guard, including mandatory full rules.
                    complete = bool(row["state"].get("rules_a", "").strip() and row["state"].get("rules_b", "").strip())
                    accepted = accepted and complete
                    if answer["choice"] == "identical":
                        accepted = accepted and response["answers"]["probability"]["noul"] >= 0.85
                record["auto_accepted"] = accepted
            except JevError as exc:
                record.update(status="error", error=str(exc))
            record["latency_ms"] = round((time.monotonic() - start) * 1000, 2)
        records.append(record)
    return {"generated_at": datetime.now(timezone.utc).isoformat(), "mode": "live" if client else "offline",
            "scope": "semantic smoke benchmark; no trading performance or independent holdout claim",
            "summary": summarize(records), "records": records}


def summarize(records: list[dict]) -> dict:
    complete = [r for r in records if r["status"] == "ok"]
    labeled = [r for r in complete if r.get("expected") is not None]
    accepted = [r for r in labeled if r["auto_accepted"]]
    latencies = sorted(r["latency_ms"] for r in complete)
    confusion = Counter(f"{r['task']}:{r['expected']}->{r['predicted']}" for r in labeled)
    return {
        "total": len(records), "completed": len(complete), "errors": sum(r["status"] == "error" for r in records),
        "labeled": len(labeled),
        "accuracy": sum(r["predicted"] == r["expected"] for r in labeled) / len(labeled) if labeled else None,
        "baseline_accuracy": sum(r["baseline"] == r["expected"] for r in labeled) / len(labeled) if labeled else None,
        "accepted_count": len(accepted),
        "accepted_accuracy": sum(r["predicted"] == r["expected"] for r in accepted) / len(accepted) if accepted else None,
        "accepted_coverage": len(accepted) / len(labeled) if labeled else None,
        "false_equivalence": sum(r["task"] == "equivalence" and r["predicted"] == "identical"
                                 and r["expected"] != "identical" for r in labeled),
        "false_resolution": sum(r["task"] == "news" and r["predicted"] in {"resolves_yes", "resolves_no"}
                                and r["predicted"] != r["expected"] for r in labeled),
        "confusion": dict(confusion),
        "latency_p95_ms": latencies[min(len(latencies) - 1, int(len(latencies) * 0.95))] if latencies else None,
        "input_tokens": sum(r.get("usage", {}).get("input_tokens", 0) for r in complete),
        "output_tokens": sum(r.get("usage", {}).get("output_tokens", 0) for r in complete),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, default=Path(__file__).resolve().parents[1] / "research/jev/semantic-cases.jsonl")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--live", action="store_true", help="Explicitly make bounded, paid TypeSafe requests")
    parser.add_argument("--max-cases", type=int, default=24)
    args = parser.parse_args()
    if not 1 <= args.max_cases <= 200:
        parser.error("max-cases must be between 1 and 200")
    raw = args.input.read_bytes()
    rows = [json.loads(line) for line in raw.splitlines() if line.strip()][:args.max_cases]
    if not rows or len({r["id"] for r in rows}) != len(rows):
        parser.error("Provide nonempty cases with unique ids")
    report = evaluate(rows, JevClient() if args.live else None)
    report["dataset_sha256"] = hashlib.sha256(raw).hexdigest()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2, allow_nan=False) + "\n")
    print(json.dumps(report["summary"], indent=2))
    return 1 if report["summary"]["errors"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
