"""Tests for the settlement-divergence veto — deterministic, fail-closed PM-lane gate."""
from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from settlement_divergence import (
    DEFAULT_VERDICT_MAX_AGE_S,
    InMemoryVerdictStore,
    RuleComparison,
    SettlementVerdict,
    pair_key,
    precompute_comparison,
    settlement_divergence_veto,
)


def _store(verdict=None, rationale="", at=1000.0, key="k"):
    s = InMemoryVerdictStore()
    if verdict is not None:
        s.put(RuleComparison(key, verdict, rationale, at))
    return s


class TestSettlementDivergence:
    # ---------------------------------------------------------------------------
    # Pair key
    def test_pair_key_is_order_independent(self):
        assert pair_key("polymarket", "0xABC", "kalshi", "KXBTC-Y") == pair_key(
            "kalshi", "KXBTC-Y", "polymarket", "0xABC"
        )

    # ---------------------------------------------------------------------------
    # Fail-closed veto
    def test_veto_when_no_verdict_on_file(self):
        vetoed, reason = settlement_divergence_veto("k", _store())
        assert vetoed is True
        assert "no settlement comparison" in reason

    def test_veto_on_diverge(self):
        vetoed, reason = settlement_divergence_veto("k", _store(SettlementVerdict.DIVERGE, "diff source"))
        assert vetoed is True
        assert "diverge" in reason

    def test_veto_on_uncertain(self):
        assert settlement_divergence_veto("k", _store(SettlementVerdict.UNCERTAIN, "ambiguous"))[0] is True

    def test_allows_only_on_match(self):
        # Fresh MATCH (compared_at == now) → allowed.
        vetoed, reason = settlement_divergence_veto(
            "k", _store(SettlementVerdict.MATCH, "identical", at=1000.0), now=1000.0
        )
        assert vetoed is False
        assert reason == ""

    def test_unrecognized_verdict_is_vetoed(self):
        # A bogus verdict string (e.g. a bad DB row) must veto, not crash on `.value`.
        s = InMemoryVerdictStore()
        s.put(RuleComparison("k", "totally-bogus", "manual row", 1000.0))
        vetoed, reason = settlement_divergence_veto("k", s, now=1000.0)
        assert vetoed is True
        assert "unrecognized" in reason

    def test_string_match_verdict_is_honored(self):
        # A MATCH stored as a plain string (LLM wrapper) is coerced, not rejected.
        s = InMemoryVerdictStore()
        s.put(RuleComparison("k", "match", "stringy", 1000.0))
        assert settlement_divergence_veto("k", s, now=1000.0)[0] is False

    # ---------------------------------------------------------------------------
    # Staleness and non-finite timing (settlement rules can change)
    def test_stale_match_is_vetoed(self):
        s = _store(SettlementVerdict.MATCH, "ok", at=0.0)
        vetoed, reason = settlement_divergence_veto("k", s, max_age_s=3600.0, now=10_000.0)
        assert vetoed is True
        assert "stale" in reason

    def test_fresh_match_within_max_age_allowed(self):
        s = _store(SettlementVerdict.MATCH, "ok", at=9_000.0)
        assert settlement_divergence_veto("k", s, max_age_s=3600.0, now=10_000.0)[0] is False

    def test_default_max_age_is_finite_not_disabled(self):
        # No max_age passed → the default window is still enforced (never fail-open).
        old = 10_000.0
        s = _store(SettlementVerdict.MATCH, "ok", at=old)
        now = old + DEFAULT_VERDICT_MAX_AGE_S + 1.0
        vetoed, reason = settlement_divergence_veto("k", s, now=now)
        assert vetoed is True
        assert "stale" in reason

    def test_future_dated_match_is_vetoed(self):
        # compared_at ahead of now → negative age → veto (clock/config error).
        s = _store(SettlementVerdict.MATCH, "ok", at=10_000.0)
        vetoed, reason = settlement_divergence_veto("k", s, now=5_000.0)
        assert vetoed is True
        assert "future-dated" in reason

    def test_nan_now_is_vetoed(self):
        # A NaN timestamp slips past every numeric bound — must veto fail-closed.
        s = _store(SettlementVerdict.MATCH, "ok", at=1000.0)
        vetoed, reason = settlement_divergence_veto("k", s, now=float("nan"))
        assert vetoed is True
        assert "invalid settlement timestamp" in reason

    def test_inf_max_age_is_vetoed(self):
        # An infinite window would disable freshness — must veto fail-closed.
        s = _store(SettlementVerdict.MATCH, "ok", at=1000.0)
        vetoed, reason = settlement_divergence_veto("k", s, max_age_s=float("inf"), now=1000.0)
        assert vetoed is True
        assert "invalid settlement max_age_s" in reason

    def test_nan_compared_at_is_vetoed(self):
        s = _store(SettlementVerdict.MATCH, "ok", at=float("nan"))
        vetoed, reason = settlement_divergence_veto("k", s, now=1000.0)
        assert vetoed is True
        assert "invalid settlement timestamp" in reason

    # ---------------------------------------------------------------------------
    # Offline pre-compute populates the store the gate reads
    def test_precompute_stores_verdict_and_gate_then_allows(self):
        store = InMemoryVerdictStore()

        def fake_llm(a, b):
            return (SettlementVerdict.MATCH if a == b else SettlementVerdict.DIVERGE), "stub"

        c = precompute_comparison("k", "rules X", "rules X", fake_llm, store, now=1234.0)
        assert c.verdict is SettlementVerdict.MATCH
        assert store.get("k").verdict is SettlementVerdict.MATCH
        assert settlement_divergence_veto("k", store, now=1234.0)[0] is False

    def test_precompute_diverge_keeps_gate_vetoing(self):
        store = InMemoryVerdictStore()
        precompute_comparison("k", "a", "b", lambda a, b: (SettlementVerdict.DIVERGE, "x"), store)
        assert settlement_divergence_veto("k", store)[0] is True

    def test_precompute_normalizes_string_verdict(self):
        # An LLM wrapper returning a plain string is coerced to the enum, stored valid.
        store = InMemoryVerdictStore()
        c = precompute_comparison("k", "x", "x", lambda a, b: ("match", "stringy"), store, now=1.0)
        assert c.verdict is SettlementVerdict.MATCH

    def test_precompute_rejects_unrecognized_verdict(self):
        store = InMemoryVerdictStore()
        with pytest.raises(ValueError, match="unrecognized verdict"):
            precompute_comparison("k", "x", "y", lambda a, b: ("maybe", "?"), store)
