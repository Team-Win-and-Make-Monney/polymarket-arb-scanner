"""Snapshot adapters for existing discovery records and external signal responses.

These functions never mutate discovery caches or SignalAggregator. An incumbent
record and a proposed remove-only screen are returned together for evaluation.
"""

from __future__ import annotations

from research_jev import validation as v
from research_jev.workflows import compare_settlement_language, source_to_market_relevance


def screen_discovery_candidates(payload: dict, client=None) -> dict:
    """Accept discovery-cache records plus full contract snapshots keyed by venue/id."""
    try:
        row = v.bounded(payload, {"as_of", "candidates", "contracts", "trusted_hosts"})
        candidates = row["candidates"]
        if not isinstance(candidates, list) or len(candidates) > 20 or not isinstance(row["contracts"], dict):
            raise ValueError("invalid_candidates")
        diagnostics = []
        for index, candidate in enumerate(candidates):
            if not isinstance(candidate, dict):
                raise ValueError("invalid_candidate")
            required = {"venue_a", "market_a_id", "question_a", "venue_b", "market_b_id", "question_b"}
            if not required <= candidate.keys():
                raise ValueError("invalid_candidate")
            keys = [f"{v.identifier(candidate[f'venue_{side}'])}/{v.identifier(candidate[f'market_{side}_id'])}"
                    for side in ("a", "b")]
            contracts = [row["contracts"].get(key) for key in keys]
            for side, contract in zip(("a", "b"), contracts):
                if contract is not None and (contract.get("id") != candidate[f"market_{side}_id"]
                                             or contract.get("title") != candidate[f"question_{side}"]):
                    raise ValueError("contract_binding_mismatch")
            result = compare_settlement_language({"as_of": row["as_of"], "contract_a": contracts[0],
                        "contract_b": contracts[1], "trusted_hosts": row["trusted_hosts"]}, client)
            diagnostics.append({"index": index, "screen": result})
        proposed = [candidate for index, candidate in enumerate(candidates)
                    if not diagnostics[index]["screen"].get("would_remove", False)]
        return {"workflow": "discovery_screen", "status": "ok", "execution_enabled": False,
                "incumbent_candidates": candidates, "candidates": candidates,
                "proposed_candidates": proposed, "diagnostics": diagnostics,
                "equivalence_approved": False, "live_state_changed": False}
    except (ValueError, TypeError, KeyError, AttributeError):
        return {"workflow": "discovery_screen", "status": "insufficient_evidence",
                "reason": "invalid_or_missing_input", "retain_candidates": True,
                "equivalence_approved": False, "execution_enabled": False}


def screen_signal_candidates(payload: dict, client=None) -> dict:
    """Evaluate Manifold/Metaculus candidate responses without changing their probabilities."""
    try:
        row = v.bounded(payload, {"as_of", "provider", "candidates", "snapshots", "contract", "trusted_hosts"})
        provider = row["provider"]
        if provider not in {"manifold", "metaculus"} or not isinstance(row["snapshots"], dict):
            raise ValueError("invalid_provider")
        if not isinstance(row["candidates"], list) or len(row["candidates"]) > 20:
            raise ValueError("invalid_candidates")
        candidates, diagnostics = [], []
        for candidate in row["candidates"]:
            if not isinstance(candidate, dict):
                raise ValueError("invalid_candidate")
            candidate_id = v.identifier(str(candidate["id"]))
            title = candidate.get("question") if provider == "manifold" else candidate.get("title")
            v.text(title, 2000)
            if provider == "manifold":
                if not isinstance(candidate.get("isResolved", False), bool):
                    raise ValueError("invalid_resolved_flag")
                if candidate.get("isResolved", False):
                    continue
                probability = candidate.get("probability")
            else:
                probability = candidate.get("community_prediction", {}).get("full", {}).get("q2")
            if probability is None:
                continue
            v.probability(probability)
            snapshot = row["snapshots"].get(candidate_id)
            if snapshot is not None and (snapshot.get("id") != candidate_id or snapshot.get("text") != title):
                raise ValueError("snapshot_binding_mismatch")
            result = source_to_market_relevance({"as_of": row["as_of"], "source": snapshot,
                     "contract": row["contract"], "trusted_hosts": row["trusted_hosts"]}, client)
            candidates.append({"id": candidate_id, "probability": probability})
            diagnostics.append({"id": candidate_id, "screen": result})
        if len({item["id"] for item in candidates}) != len(candidates):
            raise ValueError("duplicate_candidate_id")
        eligible = [candidate for candidate, diagnostic in zip(candidates, diagnostics)
                    if not diagnostic["screen"].get("would_remove", False)]
        return {"workflow": "signal_screen", "status": "ok", "provider": provider,
                "incumbent": candidates[0] if candidates else None,
                "selected": candidates[0] if candidates else None,
                "proposed": eligible[0] if eligible else None, "diagnostics": diagnostics,
                "probabilities_modified": False, "execution_enabled": False, "live_state_changed": False}
    except (ValueError, TypeError, KeyError, AttributeError):
        return {"workflow": "signal_screen", "status": "insufficient_evidence",
                "reason": "invalid_or_missing_input", "retain_incumbent": True, "execution_enabled": False}
