# JEV financial research workflows

`research_jev` implements eight bounded research workflows over supplied,
timestamped snapshots. The package imports no scanner, executor, broker,
production configuration, provider client, or notification module. It does not
fetch source material, change live state, create orders, or authorize execution.

## Run locally

The package requires Python 3.10+ and the standard library only. Python 3.12 is
used by the repository checks. Default mode is **off**, including when an API key
already exists in the environment.

```sh
python -m research_jev event --input examples/jev-research/event.json
python -m research_jev settlement --input examples/jev-research/settlement.json
python -m research_jev paper-evaluate --input examples/jev-research/paper-evaluate.json
```

`--output /path/to/new.json` writes a new local file and refuses to overwrite an
existing file. `--mode shadow` records model judgments and proposed ordering but
retains the incumbent. `--mode advisory` may apply a proposed order to the local
research queue. Neither mode changes live discovery or signal selection. An
enabled mode uses `TYPESAFE_API_KEY` with the direct TypeSafe endpoint; source
transmission must be appropriate for the dataset's privacy requirements.

| Report use | Function | CLI command | Main output |
|---|---|---|---|
| Filing/news event classification | `classify_event` | `event` | Event type or abstention; exact issuer evidence |
| Novelty/material changes | `screen_novelty` | `novelty` | Duplicate/update/contradiction/unrelated; originals retained |
| Source-to-market relevance | `source_to_market_relevance` | `relevance` | Same-event/direction judgments and a remove-only proposal |
| Settlement-language comparison | `compare_settlement_language` | `settlement` | Exact terms mismatches and semantic discrepancy; never equivalence approval |
| Research attention queue | `rank_research_attention` | `attention` | Baseline and proposed source order; mandatory attention retained |
| Incentive/program changes | `detect_incentive_changes` | `incentives` | Exact condition changes, prose category and affected workflow candidates |
| Speech/transcript analysis | `analyze_transcript` | `transcript` | Independent themes, uncertainty and prior-language comparison |
| Deferred price prediction | `record_paper_features`, `evaluate_paper_forecasts` | `paper-features`, `paper-evaluate` | Paper-only features and scoring of independently supplied forecasts |

Functions are exported from `research_jev`. An integration can inject any
synchronous client implementing `evaluate(state, questions) -> dict` and the
portable runtime result contract. All complete ordered requests are hashed;
replayed or injected responses must match those hashes and the pinned model.
An injected client must declare its mode explicitly; a missing mode stays off.

## Snapshot contract

Each source has `id`, `text`, `url`, `kind`, `entity_ids`, `published_at`,
`available_at`, and `captured_at`. Kinds are `filing`, `news`, `official`,
`transcript`, and `market_rules`. Source identifiers must be stable and unique;
reusing one ID with different text/context is rejected. Evidence output copies
the exact supplied text and records its SHA-256. URLs must be HTTPS and contain
no user/password component. The application does not verify their contents by
fetching them.

Times must be explicit timezone-aware ISO 8601 values satisfying
`published_at <= available_at <= captured_at <= as_of`. This strict rule requires
snapshots captured at the historical decision time. A later re-download of an
old publication does not establish that the historical pipeline could see its
current text. Prior/current comparisons additionally require that the prior
snapshot was captured before the current source became available.

Inputs are capped at 80,000 UTF-8 JSON bytes, 20 sources, and 20,000 characters
per source. They are rejected instead of truncated. Collections have stable
IDs and duplicate detection. Exact fields are checked before inference; no
future outcome or extra label field is admitted into a paper feature request.

Complete example inputs exist in [examples/jev-research](../examples/jev-research/README.md).
Every example uses synthetic `example.org` sources. The corresponding
`*-insufficient.json` files exercise missing evidence, and
`mocked-results.json` records explicitly mocked outputs. These are software
fixtures, not research findings or measured model performance.

## Contract and signal constraints

A contract contains `id`, `title`, `entity_ids`, a full `rules` source snapshot,
`rules_complete: true`, `resolution_source_id`, and `terms`. Terms contain
`event_id`, `window_start`, `window_end`, `direction` (`yes`/`no`), and `threshold`
(null for a qualitative event or `{metric, operator, value, unit}`). Supported
operators are `gt`, `gte`, `lt`, `lte`, and `eq`; threshold values are exact
decimal strings with at most 18 fractional places. Normalization never rounds.

Both settlement contracts must have complete rules. Code compares normalized
dates, entities, thresholds, direction and named resolution sources before
semantic review. A match still returns `equivalence_approved: false` and requires
review. A positive model label is not proof of payoff equivalence. Rules
completeness and extracted terms are supplied by the caller, not inferred here.

A relevance source may carry optional `constraints` describing its proposition.
A known different event/entity is a removal proposal. Differing numeric or date
propositions cause abstention: a claim about >50 can still bear on a >25 contract.
The model never performs that arithmetic or rewrites a market probability.
Source and rule hosts must appear exactly in the caller's trusted-host list;
subdomains are not implicitly trusted.

Novelty screening checks optional event IDs before exact-text deduplication or
semantic comparison. Shared entities with differing event IDs, or an event ID
supplied on only one side, require review. Repeated wording across different
reporting periods cannot be treated as a duplicate. Original sources remain in
the result even when their scopes conflict.

Two concrete adapters consume the existing pipeline formats:

- `screen_discovery_candidates` / `discovery-screen` accepts discovery-cache
  records with `venue_a`, `market_a_id`, `question_a`, `venue_b`, `market_b_id`,
  and `question_b`, plus contracts keyed by `venue/market_id`. ID and exact title
  bindings are checked. Original candidates and proposed survivors are separate.
- `screen_signal_candidates` / `signal-screen` accepts Manifold
  `id/question/probability/isResolved` rows or Metaculus
  `id/title/community_prediction.full.q2` rows. Snapshots are keyed by candidate
  ID and bound to its exact title. Resolved Manifold candidates are excluded as
  in the incumbent path. Probabilities pass through unchanged; complements are
  never manufactured. Missing snapshots, uncertainty and outages retain the
  incumbent candidate.

These adapters carry forward the older discovery experiment's remove-only
screen and the relevance experiment's separate same-event/same-direction
questions. They do not merge those older branches or modify `market_discovery.py`,
`signal_aggregator.py`, `jev_client.py`, discovery caches, or production flags.
Existing Firecrawl evidence ingestion (PR #142) is a separate scope.

## Programs, transcripts and research priority

Program comparison requires two official snapshots bound to the same program
ID and trusted hosts. Caller-supplied exact conditions include start/end times,
reward amount/unit, minimum volume and eligible regions. Negative numeric values
are rejected. Any exact prose change or structured condition change requires
review, including when the model is off, unavailable, or labels it "cosmetic."
Only identical prose and conditions take the deterministic unchanged path.
Outputs identify research workflows potentially affected; account
eligibility is always `not_evaluated`.

Transcript input must identify the same speaker and have caller-reviewed
quality. Without a prior transcript the theme tags still work, but the change
comparison explicitly reports insufficient evidence. The quality flag is an
input prerequisite, not proof produced by the model.

Attention is ranked against explicit, entity-linked watchlist theses. All
sources stay in the queue. Required-attention items precede other items;
uncertain items stay visible for review. Shadow mode keeps original order.

## Paper evaluation

`paper-features` produces three reusable textual features: supporting claim
present, opposing claim present, and uncertainty present. Their probabilities
describe the text, **not the market outcome**. The target horizon must follow
the feature cutoff. The feature path cannot receive outcome labels or future
prices.

`paper-evaluate` is entirely deterministic and makes no model call. Inputs are
saved feature records, independently frozen forecasts, later source-backed
binary outcomes, `split_at`, `evaluation_at`, and training event groups. It
requires `training_end < split_at <= feature_as_of <= predicted_at < horizon_end
<= outcome_occurred_at <= outcome_known_at <= evaluation_at`. It rejects shared
train/test event groups, repeated test groups, duplicate targets, missing rows,
out-of-range probabilities, and future feature snapshots. It reports Brier and
clipped log loss against the supplied baseline. It does not train a predictor,
establish calibration, simulate orders, or claim profitability.

## Runtime, uncertainty and verification

Vendored runtime: `research_jev/runtime.py`, portable runtime version `1.0.0`,
SHA-256 `1d652c67847f2eb743e5b286e8d5de8f74f2ea0501b293ae4ccfae60d8d31cd6`.
Pinned model: `jev-1.13.0`. Requests use the fixed direct endpoint, no redirects,
no retries, and bounded request/response bodies and deadlines. Keys and raw
provider errors never enter output. Unknown usage remains null.

Question packs have explicit versions in `rubrics.py`. Choice acceptance uses
both a 0.80 selected probability and 0.80 confidence; Nouls use 0.80/0.20 with an
abstention band. These are experimental thresholds, not measured calibration.
The [current API](https://docs.typesafe.ai/api),
[Choice primitive](https://docs.typesafe.ai/primitives/choice),
[reranking cookbook](https://docs.typesafe.ai/cookbooks/rerank_typesafe), and
[feature-discovery cookbook](https://docs.typesafe.ai/cookbooks/autoresearch_feature_discovery)
were checked during implementation on 2026-09-20.

Validation is documented in [the implementation spec](plans/11-jev-research-workflows.md).
Use `.venv/bin/python -m pytest tests/test_research_jev.py -q` for the focused
behavior suite. Full correctness gates remain repository policy. Real deployment
requires approved source data, independent event/time-split labels, incumbent
and deterministic comparisons, missed-event tests, measured end-to-end costs,
and review of false removals. No private source corpus or live model traffic was
used to claim those gates passed.
