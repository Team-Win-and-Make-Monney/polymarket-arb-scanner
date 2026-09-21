"""Concrete, bounded financial research workflows with explicit abstention."""

from __future__ import annotations

import functools
import logging
from decimal import Decimal

from research_jev import rubrics as r
from research_jev import validation as v
from research_jev.runtime import JevClient, content_hash, validate_response

logger = logging.getLogger(__name__)


def guarded(name):
    def decorate(function):
        @functools.wraps(function)
        def invoke(payload, client=None):
            try:
                return function(payload, client)
            except (ValueError, TypeError, KeyError, OverflowError, RecursionError):
                # Input content and exception strings are deliberately not logged.
                logger.debug("JEV research input did not satisfy %s schema", name)
                return {"workflow": name, "rubric_version": r.VERSIONS[name], "schema_version": 1,
                        "status": "insufficient_evidence", "reason": "invalid_or_missing_input",
                        "retain_candidate": True, "equivalence_approved": False, "execution_enabled": False,
                        "evidence": [], "evaluation": None}
        return invoke
    return decorate


def _base(name: str, state: dict, items: list[dict]) -> dict:
    by_id = {}
    for item in items:
        if item["id"] in by_id and by_id[item["id"]] != item:
            raise ValueError("conflicting_source_ids")
        by_id[item["id"]] = item
    return {"workflow": name, "rubric_version": r.VERSIONS[name], "schema_version": 1,
            "input_hash": v.digest(state), "as_of": state["as_of"], "status": "ok", "reason": None,
            "evidence": v.evidence(list(by_id.values())), "execution_enabled": False, "equivalence_approved": False,
            "retain_candidate": True, "evaluation": None}


def _evaluate(result: dict, state: dict, questions: dict, client) -> dict | None:
    evaluator = client if client is not None else JevClient()
    mode = getattr(evaluator, "mode", "off")
    metadata = {"status": "off" if mode == "off" else "unavailable", "mode": mode,
                "model": r.MODEL, "usage": None, "elapsed_ms": None,
                "state_hash": content_hash(state), "question_hash": content_hash(questions), "error_code": None}
    result["evaluation"] = metadata
    if mode == "off":
        result.update(status="off", reason="model_disabled")
        return None
    try:
        got = evaluator.evaluate(state, questions)
        if not isinstance(got, dict) or got.get("mode") != mode or mode not in {"shadow", "advisory"}:
            raise ValueError("invalid_evaluation")
        if got.get("status") != "ok":
            code = got.get("error_code")
            metadata["error_code"] = code if isinstance(code, str) and len(code) <= 64 else None
            result.update(status="unavailable", reason="model_unavailable")
            return None
        answers, usage = validate_response({"model": got.get("model"), "answers": got.get("answers"),
                                            "usage": got.get("usage")}, questions, r.MODEL)
        # A replay or injected client must correspond to this exact ordered request.
        if (got.get("state_hash") != content_hash(state)
                or got.get("question_hash") != content_hash(questions)):
            raise ValueError("request_hash_mismatch")
        metadata.update(status="ok", usage=usage, elapsed_ms=got.get("elapsed_ms"))
        result["judgments"] = answers
        return answers
    except Exception:
        logger.debug("JEV research evaluation was rejected")
        result.update(status="unavailable", reason="invalid_or_failed_evaluation")
        metadata.update(status="invalid", error_code="invalid_or_failed_evaluation")
        return None


def _selected(result: dict, answers: dict | None, key: str) -> str | None:
    if answers is None:
        return None
    answer = answers[key]
    selected = answer["choice"]
    if (selected == "uncertain" or answer["confidence"] < r.MIN_CONFIDENCE
            or answer["probabilities"][selected] < r.MIN_PROBABILITY):
        result.update(status="abstain", reason="uncertain_judgment")
        return None
    return selected


def _strong(value: float) -> bool | None:
    if value >= r.MIN_PROBABILITY:
        return True
    if value <= r.MAX_NEGATIVE_PROBABILITY:
        return False
    return None


@guarded("event")
def classify_event(payload: dict, client=None) -> dict:
    """Label a supplied issuer filing or news event without inferring financial numbers."""
    row = v.bounded(payload, {"as_of", "issuer_id", "source"})
    item = v.source(row["source"], row["as_of"])
    if v.identifier(row["issuer_id"]) not in item["entity_ids"] or item["kind"] not in {"filing", "news", "official"}:
        raise ValueError("issuer_or_kind_mismatch")
    state = {**row, "source": item}
    result = _base("event", state, [item])
    answers = _evaluate(result, state, r.event_questions(), client)
    result["event_type"] = _selected(result, answers, "event_type")
    result["requires_review"] = result["event_type"] in {None, "multiple"}
    return result


@guarded("novelty")
def screen_novelty(payload: dict, client=None) -> dict:
    """Compare current evidence with evidence already available; never delete originals."""
    row = v.bounded(payload, {"as_of", "current", "previous"})
    current = v.source(row["current"], row["as_of"])
    previous = v.sources(row["previous"], row["as_of"])
    for item in previous:
        v.chronological_pair(item, current)
    state = {**row, "current": current, "previous": previous}
    result = _base("novelty", state, previous + [current])
    result.update(relationship=None, retain_originals=True, requires_review=True)
    current_event = current.get("constraints", {}).get("event_id")
    event_conflicts = [item["id"] for item in previous
                       if set(current["entity_ids"]) & set(item["entity_ids"])
                       and current_event != item.get("constraints", {}).get("event_id")]
    if event_conflicts:
        # Repeated wording can describe different event periods. A missing ID on
        # one side also cannot establish equality with the other side's known ID.
        result.update(status="abstain", reason="event_identity_not_proven", event_conflicts=event_conflicts)
        return result
    if any(current["text"] == item["text"] and current["entity_ids"] == item["entity_ids"] for item in previous):
        result.update(relationship="duplicate", reason="exact_text_and_entity_match", requires_review=False)
        return result
    if not any(set(current["entity_ids"]) & set(item["entity_ids"]) for item in previous):
        result.update(relationship="unrelated", reason="disjoint_entity_ids", requires_review=False)
        return result
    answers = _evaluate(result, state, r.novelty_questions(), client)
    result["relationship"] = _selected(result, answers, "relationship")
    result["requires_review"] = result["relationship"] in {None, "update", "contradiction"}
    return result


@guarded("relevance")
def source_to_market_relevance(payload: dict, client=None) -> dict:
    """Advise whether a supplied source addresses the exact contract proposition."""
    row = v.bounded(payload, {"as_of", "source", "contract", "trusted_hosts"})
    item = v.source(row["source"], row["as_of"])
    contract = v.contract(row["contract"], row["as_of"])
    v.trusted(item, row["trusted_hosts"])
    v.trusted(contract["rules"], row["trusted_hosts"])
    state = {**row, "source": item, "contract": contract}
    result = _base("relevance", state, [item, contract["rules"]])
    source_terms = item.get("constraints", {})
    mismatches = v.mismatches(source_terms, contract["terms"])
    if not set(item["entity_ids"]) & set(contract["entity_ids"]):
        mismatches.append("entity_ids")
    result.update(contract_id=contract["id"], deterministic_mismatches=mismatches, would_remove=False,
                  relevance=None, semantic_direction=None)
    if "entity_ids" in mismatches or "event_id" in mismatches:
        result.update(relevance="unrelated", reason="deterministic_identity_mismatch", would_remove=True)
        return result
    if mismatches:
        # A differing threshold can still be evidence (e.g. >30 bears on >25).
        # It needs semantic/arithmetic review; it is not a proved irrelevant source.
        result.update(status="abstain", reason="source_proposition_terms_differ")
        return result
    answers = _evaluate(result, state, r.relevance_questions(), client)
    if answers is None:
        return result
    event, direction = (_strong(answers[key]["noul"]) for key in ("same_event", "same_direction"))
    result["semantic_direction"] = direction
    if event is False or direction is False:
        result.update(relevance="unrelated", would_remove=True)
    elif event is True and direction is True:
        result["relevance"] = "relevant"
    else:
        result.update(status="abstain", reason="uncertain_judgment")
    # Advisory proposal only: live and incumbent candidates are always retained.
    return result


@guarded("settlement")
def compare_settlement_language(payload: dict, client=None) -> dict:
    """Flag discrepancies; even an apparent agreement never approves equivalence."""
    row = v.bounded(payload, {"as_of", "contract_a", "contract_b", "trusted_hosts"})
    left, right = (v.contract(row[key], row["as_of"]) for key in ("contract_a", "contract_b"))
    for market in (left, right):
        v.trusted(market["rules"], row["trusted_hosts"])
    if left["id"] == right["id"]:
        raise ValueError("duplicate_contract_id")
    state = {**row, "contract_a": left, "contract_b": right}
    result = _base("settlement", state, [left["rules"], right["rules"]])
    mismatches = v.mismatches(left["terms"], right["terms"])
    if set(left["entity_ids"]) != set(right["entity_ids"]):
        mismatches.append("entity_ids")
    if left["resolution_source_id"] != right["resolution_source_id"]:
        mismatches.append("resolution_source_id")
    result.update(deterministic_mismatches=mismatches, language=None, would_remove=bool(mismatches),
                  requires_review=True)
    if mismatches:
        result.update(language="disagreement", reason="deterministic_mismatch")
        return result
    answers = _evaluate(result, state, r.settlement_questions(), client)
    result["language"] = _selected(result, answers, "language")
    result["would_remove"] = result["language"] == "disagreement"
    return result


@guarded("attention")
def rank_research_attention(payload: dict, client=None) -> dict:
    """Rank a watchlist queue without removing sources or overriding required attention."""
    row = v.bounded(payload, {"as_of", "sources", "watchlist"})
    items = v.sources(row["sources"], row["as_of"])
    watchlist = row["watchlist"]
    if not isinstance(watchlist, list) or not 1 <= len(watchlist) <= 20:
        raise ValueError("missing_watchlist")
    for thesis in watchlist:
        v.mapping(thesis, {"id", "entity_ids", "thesis"})
        v.identifier(thesis["id"])
        v.ids(thesis["entity_ids"])
        v.text(thesis["thesis"], 2000)
    if len({thesis["id"] for thesis in watchlist}) != len(watchlist):
        raise ValueError("duplicate_watchlist_id")
    candidates = [{"source": item, "matching_theses": [thesis for thesis in watchlist
                   if set(thesis["entity_ids"]) & set(item["entity_ids"])]} for item in items]
    state = {"as_of": row["as_of"], "candidates": candidates}
    result = _base("attention", state, items)
    baseline = [item["id"] for item in items]
    result.update(baseline_order=baseline, order=baseline, proposed_order=baseline, attention=[])
    answers = _evaluate(result, state, r.attention_questions(candidates), client)
    if answers is None:
        return result
    scored = []
    for index, candidate in enumerate(candidates):
        score = answers[f"s{index}_attention"]["noul"] if candidate["matching_theses"] else 0.0
        item = candidate["source"]
        scored.append({"source_id": item["id"], "attention_judgment": score,
                       "required_attention": item.get("required_attention", False),
                       "uncertain": _strong(score) is None, "baseline_index": index})
    proposed = sorted(scored, key=lambda item: (not item["required_attention"],
                      not item["uncertain"], -item["attention_judgment"], item["baseline_index"]))
    result["attention"] = scored
    result["proposed_order"] = [item["source_id"] for item in proposed]
    if result["evaluation"]["mode"] == "advisory":
        result["order"] = result["proposed_order"]
    result["probabilities_are_market_forecasts"] = False
    return result


def _conditions(value: object) -> dict:
    raw = v.mapping(value, {"start_at", "end_at", "reward_amount", "reward_unit", "minimum_volume", "eligible_regions"})
    if v.instant(raw["start_at"]) > v.instant(raw["end_at"]):
        raise ValueError("invalid_program_window")
    if any(Decimal(v.decimal_value(raw[key])) < 0 for key in ("reward_amount", "minimum_volume")):
        raise ValueError("negative_program_value")
    return {"start_at": v.instant(raw["start_at"]).isoformat(), "end_at": v.instant(raw["end_at"]).isoformat(),
            "reward_amount": v.decimal_value(raw["reward_amount"]), "reward_unit": v.identifier(raw["reward_unit"]),
            "minimum_volume": v.decimal_value(raw["minimum_volume"]),
            "eligible_regions": sorted(v.ids(raw["eligible_regions"], empty=True))}


@guarded("incentives")
def detect_incentive_changes(payload: dict, client=None) -> dict:
    """Compare official program snapshots and supplied exact conditions, without determining eligibility."""
    row = v.bounded(payload, {"as_of", "program_id", "previous", "current", "previous_conditions",
                             "current_conditions", "workflows", "trusted_hosts"})
    program = v.identifier(row["program_id"])
    previous, current = (v.source(row[key], row["as_of"]) for key in ("previous", "current"))
    for item in (previous, current):
        if item["kind"] != "official" or program not in item["entity_ids"]:
            raise ValueError("program_identity_mismatch")
        v.trusted(item, row["trusted_hosts"])
    v.chronological_pair(previous, current)
    workflows = row["workflows"]
    if not isinstance(workflows, list) or not 1 <= len(workflows) <= 20:
        raise ValueError("missing_workflows")
    for workflow in workflows:
        v.mapping(workflow, {"id", "description"})
        v.identifier(workflow["id"])
        v.text(workflow["description"], 2000)
    if len({workflow["id"] for workflow in workflows}) != len(workflows):
        raise ValueError("duplicate_workflow_id")
    old, new = (_conditions(row[key]) for key in ("previous_conditions", "current_conditions"))
    state = {**row, "previous": previous, "current": current, "previous_conditions": old, "current_conditions": new}
    result = _base("incentives", state, [previous, current])
    changes = {key: {"previous": old[key], "current": new[key]} for key in v.mismatches(old, new)}
    prose_changed = previous["text"] != current["text"]
    result.update(deterministic_changes=changes, change=None, affected_workflows=[],
                  unresolved_workflows=[workflow["id"] for workflow in workflows],
                  account_eligibility="not_evaluated", requires_review=bool(changes) or prose_changed)
    if not prose_changed and not changes:
        result.update(change="unchanged", reason="exact_text_and_conditions_match", unresolved_workflows=[])
        return result
    answers = _evaluate(result, state, r.incentive_questions(workflows), client)
    if answers is None:
        return result
    result["change"] = _selected(result, answers, "change")
    result["affected_workflows"] = [workflow["id"] for index, workflow in enumerate(workflows)
                                    if _strong(answers[f"w{index}_affected"]["noul"]) is True]
    result["unresolved_workflows"] = [workflow["id"] for index, workflow in enumerate(workflows)
                                      if _strong(answers[f"w{index}_affected"]["noul"]) is None]
    result["requires_review"] = (bool(changes) or prose_changed or result["change"] != "cosmetic"
                                 or bool(result["unresolved_workflows"]))
    return result


@guarded("transcript")
def analyze_transcript(payload: dict, client=None) -> dict:
    """Tag reviewed speaker text and compare prior language; never infer price moves."""
    row = v.bounded(payload, {"as_of", "speaker_id", "current"}, {"previous"})
    current = v.source(row["current"], row["as_of"])
    previous = v.source(row["previous"], row["as_of"]) if row.get("previous") is not None else None
    for item in [current] + ([previous] if previous else []):
        if (item["kind"] != "transcript" or item.get("speaker_id") != v.identifier(row["speaker_id"])
                or item.get("quality") != "reviewed"):
            raise ValueError("unverified_transcript")
    if previous:
        v.chronological_pair(previous, current)
    state = {"as_of": row["as_of"], "speaker_id": row["speaker_id"], "current": current, "previous": previous}
    result = _base("transcript", state, ([previous] if previous else []) + [current])
    result.update(themes=[], unresolved_themes=[], language_change=None,
                  comparison_status="pending" if previous else "insufficient_evidence")
    questions = r.transcript_questions(previous is not None)
    answers = _evaluate(result, state, questions, client)
    if answers is None:
        return result
    result["themes"] = [key for key in questions if questions[key]["type"] == "noul"
                        and _strong(answers[key]["noul"]) is True]
    result["unresolved_themes"] = [key for key in questions if questions[key]["type"] == "noul"
                                   and _strong(answers[key]["noul"]) is None]
    if previous:
        result["language_change"] = _selected(result, answers, "language_change")
        result["comparison_status"] = "ok" if result["language_change"] else "abstain"
    return result


@guarded("paper_features")
def record_paper_features(payload: dict, client=None) -> dict:
    """Record text-derived features as of a historical cutoff; outcomes are forbidden input fields."""
    row = v.bounded(payload, {"as_of", "record_id", "event_group", "target", "sources"})
    v.identifier(row["record_id"])
    v.identifier(row["event_group"])
    target = v.mapping(row["target"], {"id", "proposition", "entity_ids", "horizon_end"})
    v.identifier(target["id"])
    v.text(target["proposition"], 2000)
    v.ids(target["entity_ids"])
    if v.instant(target["horizon_end"]) <= v.instant(row["as_of"]):
        raise ValueError("outcome_horizon_not_future")
    items = v.sources(row["sources"], row["as_of"])
    if any(not set(item["entity_ids"]) & set(target["entity_ids"]) for item in items):
        raise ValueError("target_entity_mismatch")
    state = {**row, "sources": items}
    result = _base("paper_features", state, items)
    result.update(record_id=row["record_id"], event_group=row["event_group"], target=target,
                  features=None, paper_only=True, probabilities_are_market_forecasts=False)
    answers = _evaluate(result, state, r.paper_questions(), client)
    if answers is not None:
        result["features"] = {key: answer["noul"] for key, answer in answers.items()}
    return result
