"""Run one explicitly approved, credit-capped resolution-evidence query."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from firecrawl_evidence import FirecrawlEvidenceClient


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("question")
    parser.add_argument("--max-credits", type=int, default=10)
    parser.add_argument("--max-age-hours", type=int, default=72)
    args = parser.parse_args()

    if os.getenv("FIRECRAWL_EVIDENCE_ALLOW_EXTERNAL_DISPATCH") != "1":
        print(
            "Refusing external dispatch: set FIRECRAWL_EVIDENCE_ALLOW_EXTERNAL_DISPATCH=1 "
            "for the separately approved run.",
            file=sys.stderr,
        )
        return 2

    client = FirecrawlEvidenceClient(
        os.getenv("FIRECRAWL_API_KEY", ""), max_credits=args.max_credits
    )
    artifact = client.discover(args.question, max_age_hours=args.max_age_hours)
    print(json.dumps(artifact, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
