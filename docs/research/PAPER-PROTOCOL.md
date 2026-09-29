# Fixed paper evaluation: candidate economics after latency and costs

Status: protocol and offline evaluator ready; no collector or trading process is activated.
Version: 2026-09-29. This is a new study, not an extension of any earlier pilot.

## Hypothesis and unit

For one independently specified directional, binary-contract signal, the mean
one-contract paper outcome remains positive after a 5–30 second delayed best-ask
recheck, applicable taker fees and a one-cent-per-contract adverse price stress.
The primary success criterion is a positive lower endpoint of an approximate
95% event-level bootstrap interval, with at least 30 independent event observations.
Zero- and three-cent stresses are sensitivity reports, never replacement success criteria.
Do not pool different strategies, choose an outcome after settlement, or infer maker
fills from resting quotations. This contract deliberately does not score a market-making
strategy from inventory snapshots or quote-placement logs.

Before collection, freeze the signal version, approved public market universe,
cohort start and end times, and the intended settlement horizon in an immutable
manifest. The signal and data source must be selected before observing outcomes.
A seven-day collection window is a proposed operating duration, not a statistical
power calculation or assurance of 30 independent settlements. Pending outcomes
remain pending; the collection deadline is never extended to obtain significance.

## Evidence contract

`research_evidence.py` accepts one JSON object per line. See
`tests/fixtures/research-evidence.jsonl` for a **synthetic schema example only**.

Each real record requires stable record, event, contract and strategy identifiers;
explicitly timezone-aware observation, receipt, decision, fee-knowledge, recheck,
resolution and resolution-receipt timestamps; source digests and source URLs;
verified fee and settlement evidence; one-contract initial/recheck ask and displayed
depth; the selected side's decision-time probability; and binary YES resolution.
`decision_probability` is the probability of the selected side, not always YES.
The probability's source and original decision journal must be retained separately;
typing a probability into a row is not proof it existed at the decision time.

Initial quotes must be no more than two seconds old at decision time. Recheck
observation must be 5–30 seconds later and received within two seconds. Both books
must show at least one contract of depth. Applicable initial and delayed taker fee
amounts must be verified, including current series fees, event overrides, effective
times and member-specific rounding. Never substitute a generic fee when unknown.
The schema requires `fee_effective_from` (inclusive) and `fee_effective_until`
(exclusive), verified from the retained fee evidence. The same verified fee model
must cover both decision and delayed recheck; exclude a row when a fee change falls
between them. Never invent an applicability end time when the evidence is missing. No maker rebates or incentive estimates count.

Outcomes require independent venue resolution evidence received by the evaluation
cutoff. There is no conversion from a log's `execute` label to a verified fill.
One earliest observation per event is selected before completeness/outcome checks;
incomplete first observations are not replaced by later observations. Duplicate
record IDs are excluded. Report all exclusions and unresolved observations.
Event grouping reduces duplication but cannot guarantee cross-event independence.

## Freeze and evaluate

1. Retain raw sources locally; confirm storage and redistribution rights before upload.
2. Freeze the input JSONL, source digest manifest and protocol commit **before** running
   the held-out evaluation. Keep a separate exploratory period. Record first-run results.
3. Run the standard-library evaluator with the frozen digest and a fixed UTC cutoff:

```sh
python research_evidence.py /absolute/path/frozen.jsonl \
  --sha256 PREVIOUSLY_RECORDED_SHA256 --cutoff FIXED_UTC_TIMESTAMP \
  --output /absolute/path/new-report.json
```

The output path must not exist. A hash mismatch stops the run. `--synthetic` is only
for fixtures and can never report a supported economic hypothesis. CI uploads only
that synthetic fixture report; no private trading logs are uploaded automatically.
Fewer than 30 eligible independent events yields `insufficient_evidence`, not failure
of the economic hypothesis. The bootstrap is approximate and cannot establish
profitability under selection bias, regime changes or shared-event dependence.

## Prospective handoff

A separate observation adapter must write this evidence contract without importing
execution clients or modifying a live scanner. Public REST orderbook responses can
lack an exchange observation timestamp; local receipt time is not a substitute. Such
records cannot pass the strict historical-availability gate. Select an authoritative
feed that supplies usable timestamps before activating a longitudinal collector.
No collector is hidden inside tests or CI, and no scheduled workflow is added here.

The remaining operator decisions are the exact signal/cohort, collection window,
timestamp-capable feed and applicable fee model. No live launch, trading permission,
order, credentials or risk limits follow from a passing research report.
