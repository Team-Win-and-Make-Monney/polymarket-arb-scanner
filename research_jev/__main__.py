"""Local JSON-in/JSON-out research CLI. Off unless the operator explicitly selects a mode."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from research_jev.adapters import screen_discovery_candidates, screen_signal_candidates
from research_jev.paper import evaluate_paper_forecasts
from research_jev.runtime import JevClient, parse_json
from research_jev.workflows import (
    analyze_transcript, classify_event, compare_settlement_language, detect_incentive_changes,
    rank_research_attention, record_paper_features, screen_novelty, source_to_market_relevance,
)

WORKFLOWS = {
    "event": classify_event, "novelty": screen_novelty, "relevance": source_to_market_relevance,
    "settlement": compare_settlement_language, "attention": rank_research_attention,
    "incentives": detect_incentive_changes, "transcript": analyze_transcript,
    "paper-features": record_paper_features, "paper-evaluate": evaluate_paper_forecasts,
    "discovery-screen": screen_discovery_candidates, "signal-screen": screen_signal_candidates,
}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("workflow", choices=WORKFLOWS)
    parser.add_argument("--input", required=True, type=Path, help="Supplied timestamped JSON snapshot; never fetched")
    parser.add_argument("--output", type=Path, help="New local JSON file; existing files are never overwritten")
    parser.add_argument("--mode", choices=("off", "shadow", "advisory"), default="off")
    args = parser.parse_args(argv)
    try:
        with args.input.open("rb") as stream:
            raw = stream.read(80001)
        if len(raw) > 80000:
            raise ValueError("input_too_large")
        payload = parse_json(raw)
        result = WORKFLOWS[args.workflow](payload, JevClient(mode=args.mode))
        rendered = json.dumps(result, ensure_ascii=False, allow_nan=False, indent=2) + "\n"
        if args.output:
            with args.output.open("x", encoding="utf-8") as stream:
                stream.write(rendered)
        else:
            sys.stdout.write(rendered)
        return 2 if result["status"] == "insufficient_evidence" else 0
    except Exception:
        # Do not expose input paths, source text, credentials or raw provider errors.
        print(json.dumps({"status": "invalid", "reason": "input_or_output_error", "execution_enabled": False}),
              file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
