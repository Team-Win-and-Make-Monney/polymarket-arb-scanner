"""Offline forecast scoring. No fitting, orders, position sizing or P&L claims."""

from __future__ import annotations

import hashlib
import math
from urllib.parse import urlsplit

from research_jev import validation as v


def evaluate_paper_forecasts(payload: dict, client=None) -> dict:
    """Score independently recorded probabilities against later, held-out outcomes.

    Args:
        payload: Feature records, independent forecasts, outcomes and a fixed time split.
        client: Unused; this function never calls a model.

    Returns:
        Brier/log losses versus the supplied baseline, or an explicit rejected dataset.
    """
    try:
        return _evaluate(payload)
    except (ValueError, TypeError, KeyError, OverflowError, RecursionError):
        return {"workflow": "paper_evaluation", "schema_version": 1, "status": "insufficient_evidence",
                "reason": "invalid_or_leaking_evaluation_data", "paper_only": True, "execution_enabled": False}


def _evaluate(payload: dict) -> dict:
    row = v.bounded(payload, {"evaluation_at", "split_at", "train_event_groups", "feature_records", "forecasts", "outcomes"})
    evaluation_at, split_at = v.instant(row["evaluation_at"]), v.instant(row["split_at"])
    training_groups = set(v.ids(row["train_event_groups"], empty=True, limit=2000))
    if split_at >= evaluation_at:
        raise ValueError("invalid_time_split")
    tables = {}
    for name, key in (("feature_records", "record_id"), ("forecasts", "record_id"), ("outcomes", "target_id")):
        entries = row[name]
        if not isinstance(entries, list) or not 1 <= len(entries) <= 20:
            raise ValueError("missing_evaluation_rows")
        tables[name] = {v.identifier(entry[key]): entry for entry in entries}
        if len(tables[name]) != len(entries):
            raise ValueError("duplicate_evaluation_ids")
    if set(tables["feature_records"]) != set(tables["forecasts"]):
        raise ValueError("missing_feature_record")
    cases, groups, target_ids = [], set(), set()
    for record_id, forecast in tables["forecasts"].items():
        v.mapping(forecast, {"record_id", "model_id", "predicted_at", "training_end", "probability", "baseline_probability"})
        v.identifier(forecast["model_id"])
        record = tables["feature_records"][record_id]
        if (record.get("workflow") != "paper_features" or record.get("status") != "ok"
                or record.get("paper_only") is not True or record.get("execution_enabled") is not False
                or record.get("probabilities_are_market_forecasts") is not False):
            raise ValueError("unavailable_features")
        group = v.identifier(record["event_group"])
        if group in training_groups or group in groups:
            raise ValueError("event_group_leakage")
        groups.add(group)
        target = v.mapping(record["target"], {"id", "proposition", "entity_ids", "horizon_end"})
        target_id = v.identifier(target["id"])
        if target_id in target_ids:
            raise ValueError("duplicate_target")
        target_ids.add(target_id)
        outcome = tables["outcomes"][target_id]
        v.mapping(outcome, {"target_id", "value", "occurred_at", "known_at", "source_url"})
        outcome_url = urlsplit(v.text(outcome["source_url"], 2000))
        if outcome_url.scheme != "https" or not outcome_url.hostname or outcome_url.username or outcome_url.password:
            raise ValueError("invalid_outcome_source")
        # Outcome evidence is supplied separately and is never passed to TypeSafe.
        if isinstance(outcome["value"], bool) or outcome["value"] not in {0, 1}:
            raise ValueError("invalid_binary_outcome")
        observed = v.instant(outcome["occurred_at"])
        known = v.instant(outcome["known_at"])
        prediction_at = v.instant(forecast["predicted_at"])
        as_of, horizon = v.instant(record["as_of"]), v.instant(target["horizon_end"])
        training_end = v.instant(forecast["training_end"])
        if not training_end < split_at <= as_of <= prediction_at < horizon <= observed <= known <= evaluation_at:
            raise ValueError("forecast_time_leakage")
        evidence = record.get("evidence")
        if not isinstance(evidence, list) or not evidence:
            raise ValueError("missing_feature_evidence")
        if len(evidence) > v.MAX_SOURCES:
            raise ValueError("too_many_feature_evidence")
        for source in evidence:
            if not isinstance(source, dict):
                raise ValueError("invalid_source")
            v.identifier(source.get("source_id"))
            text = v.text(source.get("text"))
            url = urlsplit(v.text(source.get("url"), 2000))
            if url.scheme != "https" or not url.hostname or url.username or url.password:
                raise ValueError("invalid_source_url")
            expected_sha = hashlib.sha256(text.encode("utf-8")).hexdigest()
            if source.get("sha256") != expected_sha:
                raise ValueError("invalid_source_digest")
            if not (v.instant(source["published_at"]) <= v.instant(source["available_at"])
                    <= v.instant(source["captured_at"]) <= as_of):
                raise ValueError("feature_time_leakage")
        features = v.mapping(record.get("features"),
                             {"supporting_claim_present", "opposing_claim_present", "uncertainty_present"})
        for value in features.values():
            v.probability(value)
        prediction = v.probability(forecast["probability"])
        baseline = v.probability(forecast["baseline_probability"])
        y = outcome["value"]
        cases.append({"record_id": record_id, "event_group": group, "target_id": target_id,
                      "model_id": forecast["model_id"], "predicted_at": forecast["predicted_at"],
                      "probability": prediction, "baseline_probability": baseline, "outcome": y,
                      "brier": (prediction - y) ** 2, "baseline_brier": (baseline - y) ** 2,
                      "log_loss": _log_loss(prediction, y), "baseline_log_loss": _log_loss(baseline, y)})
    if target_ids != set(tables["outcomes"]):
        raise ValueError("unused_outcome_rows")
    means = {name: sum(case[name] for case in cases) / len(cases)
             for name in ("brier", "baseline_brier", "log_loss", "baseline_log_loss")}
    return {"workflow": "paper_evaluation", "schema_version": 1, "status": "ok", "input_hash": v.digest(row),
            "paper_only": True, "execution_enabled": False, "evaluation_at": row["evaluation_at"],
            "heldout_cases": len(cases), "heldout_event_groups": len(groups), "metrics": means,
            "brier_change_vs_baseline": means["brier"] - means["baseline_brier"], "cases": cases,
            "market_probability_source": "independently_supplied_forecasts",
            "calibration_established": False, "profitability_established": False,
            "log_loss_clip_epsilon": 1e-15}


def _log_loss(probability: float, outcome: int) -> float:
    clipped = max(1e-15, min(1 - 1e-15, probability))
    return -math.log(clipped if outcome else 1 - clipped)
