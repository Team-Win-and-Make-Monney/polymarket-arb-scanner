"""Offline evidence controls; all records here are explicitly synthetic."""
import copy
import hashlib
import json
from pathlib import Path
import subprocess
import sys

import pytest

from research_evidence import EvidenceError, evaluate, validate_record

CUTOFF = "2026-09-30T00:00:00Z"


def fixture():
    return {"schema_version": 1, "synthetic": True, "execution_authorized": False,
            "record_id": "fixture-1", "event_id": "fixture-event-1", "contract_id": "fixture-contract",
            "strategy_id": "fixture-strategy", "observed_at": "2026-09-29T10:00:00Z",
            "received_at": "2026-09-29T10:00:00Z", "decision_at": "2026-09-29T10:00:01Z",
            "fee_known_at": "2026-09-29T09:00:00Z",
            "fee_effective_from": "2026-09-29T09:00:00Z", "fee_effective_until": "2026-09-30T00:00:00Z", "recheck_observed_at": "2026-09-29T10:00:06Z",
            "recheck_received_at": "2026-09-29T10:00:07Z", "resolved_at": "2026-09-29T12:00:00Z",
            "resolution_received_at": "2026-09-29T12:01:00Z", "book_sha256": "a" * 64,
            "recheck_book_sha256": "b" * 64, "fee_source_sha256": "c" * 64,
            "resolution_source_sha256": "d" * 64, "fee_source_url": "https://example.org/fixture-fee",
            "resolution_source_url": "https://example.org/fixture-resolution", "fee_verified": True,
            "resolution_verified": True, "initial_ask": "0.40", "initial_taker_fee_usd": "0.02",
            "recheck_ask": "0.42", "taker_fee_usd": "0.02", "initial_depth": "2",
            "recheck_depth": "1", "side": "yes", "resolved_yes": 1, "decision_probability": "0.60"}


class TestResearchEvidence:
    def test_known_arithmetic_and_stress(self):
        result = evaluate([fixture()], CUTOFF, True)
        assert result["mean_outcome_usd_by_adverse_cents"] == {"0": .56, "1": .55, "3": .53}
        assert result["status"] == "insufficient_evidence"
        assert result["hypothesis_supported"] is None
        assert result["execution_authorized"] is False

    def test_synthetic_is_not_real_evidence(self):
        result = evaluate([fixture()], CUTOFF)
        assert result["eligible_independent_events"] == 0
        assert result["exclusions"] == {"synthetic_or_unclassified": 1}

    @pytest.mark.parametrize("field,value,reason", [
        ("recheck_observed_at", "2026-09-29T10:00:03Z", "recheck_delay_outside_protocol"),
        ("received_at", "2026-09-29T10:00:02Z", "chronology_invalid"),
        ("fee_known_at", "2026-09-29T10:00:02Z", "fee_lookahead"),
        ("observed_at", "2026-09-29T09:00:00Z", "stale_initial_book"),
        ("recheck_depth", "0.5", "number_out_of_range"),
        ("taker_fee_usd", "NaN", "number_out_of_range"),
        ("initial_ask", True, "number_invalid"),
        ("resolved_yes", True, "outcome_invalid"),
        ("resolution_verified", False, "source_not_verified"),
        ("book_sha256", "not-a-hash", "source_digest_missing"),
        ("decision_at", "2026-09-29T10:00:01", "timestamp_naive"),
        ("resolution_received_at", "2026-10-01T00:00:00Z", "chronology_invalid"),
        ("decision_probability", "0.30", "no_initial_edge"),
        ("execution_authorized", True, "execution_boundary_missing"),
    ])
    def test_fail_closed(self, field, value, reason):
        row = fixture()
        row[field] = value
        with pytest.raises(EvidenceError, match=reason):
            validate_record(row, CUTOFF, True)

    def test_duplicate_identity_excludes_all_copies(self):
        result = evaluate([fixture(), fixture()], CUTOFF, True)
        assert result["eligible_independent_events"] == 0
        assert result["exclusions"] == {"duplicate_record_id": 1, "repeated_event": 1}

    def test_repeated_event_is_not_independent(self):
        row = fixture()
        row["record_id"] = "fixture-2"
        row["resolved_yes"] = 0
        result = evaluate([fixture(), row], CUTOFF, True)
        assert result["eligible_independent_events"] == 1
        assert result["exclusions"] == {"repeated_event": 1}

    def test_bootstrap_is_reproducible_and_fixture_cannot_support_hypothesis(self):
        rows = []
        for i in range(30):
            row = fixture()
            row.update(record_id=f"fixture-{i}", event_id=f"fixture-event-{i}", resolved_yes=i % 2)
            rows.append(row)
        first = evaluate(rows, CUTOFF, True)
        assert first == evaluate(copy.deepcopy(rows), CUTOFF, True)
        assert first["status"] == "evaluated"
        assert first["hypothesis_supported"] is None
        assert first["profitability_established"] is False

    def test_cannot_pool_strategies(self):
        row = fixture()
        row.update(record_id="fixture-2", event_id="fixture-event-2", strategy_id="different")
        with pytest.raises(EvidenceError, match="mixed_strategies"):
            evaluate([fixture(), row], CUTOFF, True)

    def test_legacy_log_is_excluded(self):
        result = evaluate([{"ts": 1000000, "decision": "execute", "reason": "dry_run"}], CUTOFF)
        assert result["status"] == "insufficient_evidence"
        assert result["exclusions"] == {"unsupported_schema": 1}

    def test_cli_hash_mismatch_creates_no_report(self, tmp_path):
        source = tmp_path / "input.jsonl"
        source.write_text(json.dumps(fixture()) + "\n")
        target = tmp_path / "report.json"
        script = Path(__file__).resolve().parents[1] / "research_evidence.py"
        command = [sys.executable, str(script), str(source), "--sha256", "0" * 64,
                   "--cutoff", CUTOFF, "--output", str(target)]
        process = subprocess.run(command, capture_output=True, text=True)
        assert process.returncode != 0
        assert not target.exists()
        command[4] = hashlib.sha256(source.read_bytes()).hexdigest()
        process = subprocess.run(command + ["--synthetic"], capture_output=True, text=True)
        assert process.returncode == 0, process.stderr
        assert json.loads(target.read_text())["synthetic"] is True
        assert subprocess.run(command, capture_output=True).returncode != 0

    def test_incomplete_first_event_does_not_promote_later_outcome(self):
        first = fixture()
        first["resolution_verified"] = False
        later = fixture()
        later["record_id"] = "fixture-later"
        later["decision_at"] = "2026-09-29T10:00:02Z"
        result = evaluate([later, first], CUTOFF, True)
        assert result["eligible_independent_events"] == 0
        assert result["exclusions"] == {"source_not_verified": 1, "repeated_event": 1}

    def test_insufficient_real_evidence_is_inconclusive(self):
        assert evaluate([], CUTOFF)["hypothesis_supported"] is None

    def test_missing_ids_are_not_counted_as_duplicate_real_records(self):
        result = evaluate([{"ts": 1}, {"ts": 2}], CUTOFF)
        assert result["exclusions"] == {"unsupported_schema": 2}

    def test_fee_change_between_decision_and_recheck_is_rejected(self):
        row = fixture()
        row["fee_effective_until"] = "2026-09-29T10:00:04Z"
        with pytest.raises(EvidenceError, match="fee_not_applicable_through_recheck"):
            validate_record(row, CUTOFF, True)

    def test_duplicated_first_observation_does_not_promote_later_record(self):
        first = fixture()
        later = fixture()
        later.update(record_id="later", decision_at="2026-09-29T10:00:02Z")
        result = evaluate([later, first, copy.deepcopy(first)], CUTOFF, True)
        assert result["eligible_independent_events"] == 0
        assert result["exclusions"] == {"duplicate_record_id": 1, "repeated_event": 2}
