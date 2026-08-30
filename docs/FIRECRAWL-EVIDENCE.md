# Firecrawl resolution evidence boundary

Firecrawl may discover current primary-source news or official resolution evidence.
It is not a market-data provider and must never supply prices, probabilities, order
books, positions, sizing, trading advice, venue eligibility, or execution authority.
Direct venue APIs remain authoritative for market state.

The standalone command is fail-closed and not imported by scanners or execution code:

```sh
FIRECRAWL_EVIDENCE_ALLOW_EXTERNAL_DISPATCH=1 \
  python scripts/firecrawl_resolution_evidence.py \
  --max-credits 10 \
  "Did the named official agency publish the final result?"
```

Do not run it without separate approval for the exact query and Firecrawl credit cap.
The maximum accepted cap is 25 credits. Output strips URL credentials, queries, and
fragments; ignores price-like provider fields; hashes retained evidence; and labels
missing publication dates `unknown` rather than treating retrieval time as freshness.

Acceptance for use in analysis requires an HTTPS source URL, a reproducible primary
source, a stated publication date for any `current` label, and human confirmation that
the evidence answers the venue's resolution criteria. Evidence can inform analysis but
cannot trigger an order, change `DRY_RUN`, or authorize a venue.
