"""Settlement-divergence veto gate — deterministic pre-trade VETO (PM-lane safety).

A cross-venue arb (e.g. Polymarket binary + Kalshi binary) is only safe if BOTH
legs settle on the SAME real-world outcome under the SAME rules. If the venues
word their settlement criteria differently (different source, cutoff time, or
edge-case handling), the "arb" can settle one leg YES and the other NO — a
guaranteed loss, not a locked profit. This gate blocks any cross-venue pair whose
settlement rules are not CONFIRMED matching.

Operating-rule compliant: **no LLM in the hot path.** The rule-text comparison is
an OFFLINE pre-compute — an LLM reads the two settlement-rule texts and emits
MATCH / DIVERGE / UNCERTAIN, stored keyed by the market pair. This gate is PURE
and DETERMINISTIC: it only READS the stored verdict and vetoes fail-closed —

    allow ONLY if a fresh MATCH verdict is on file for the pair;
    veto on DIVERGE, UNCERTAIN, a MISSING verdict, or a STALE MATCH.

Phase 1 (this file): the deterministic veto + the offline-precompute plumbing +
tests. Phase 2 (separate PR): call the veto in `executor.py` before a Cross pair
fires, and populate the store from a scheduled rule-comparison job.
"""
from __future__ import annotations

import logging
import math
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable, Protocol

logger = logging.getLogger(__name__)

# A stored MATCH older than this is treated as STALE and vetoed. Finite by design:
# there is NO "disabled" setting, because an unbounded freshness window would make
# the gate fail-OPEN. Callers may pass a tighter window; this is the safe default.
DEFAULT_VERDICT_MAX_AGE_S = 86_400.0  # 24h


class SettlementVerdict(str, Enum):
    MATCH = "match"          # rules settle on the same outcome — safe to pair
    DIVERGE = "diverge"      # rules can settle differently — never pair
    UNCERTAIN = "uncertain"  # comparison inconclusive — treat as unsafe


# A verdict may legitimately arrive as the enum OR as a raw string (an LLM wrapper
# emitting "match", or a row deserialized from the DB); ``_coerce_verdict`` is the
# validating boundary that turns either into a trusted enum or rejects it.
VerdictValue = SettlementVerdict | str


def _coerce_verdict(value: object) -> SettlementVerdict | None:
    """Best-effort coerce a stored/produced verdict to the enum, else ``None``.

    A verdict can arrive as a plain string — an LLM wrapper returning ``"match"``,
    or a row deserialized from the DB. It must not crash the gate on ``.value`` or
    slip through unchecked; unrecognized values return ``None`` so the caller fails
    closed rather than trusting malformed data.

    Args:
        value: A ``SettlementVerdict`` or any value to coerce (typically a string).

    Returns:
        The matching ``SettlementVerdict``, or ``None`` if unrecognized.
    """
    if isinstance(value, SettlementVerdict):
        return value
    try:
        return SettlementVerdict(str(value).strip().lower())
    except ValueError:
        return None


def _finite_float(value: object) -> float | None:
    """Coerce a timing value to a finite ``float``, or ``None`` when malformed.

    A ``NaN`` compares False against every bound (so it would slip past both the
    future-dated and stale checks), and ``inf`` would disable the freshness window
    — both must be rejected so the gate stays fail-closed on bad config or rows.

    Args:
        value: A value expected to be a finite number of seconds.

    Returns:
        The value as a finite ``float``, or ``None`` if non-numeric or non-finite.
    """
    try:
        result = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


@dataclass(frozen=True)
class RuleComparison:
    """The stored result of one offline settlement-rule comparison."""
    pair_key: str
    verdict: VerdictValue
    rationale: str
    compared_at: float       # unix seconds when the comparison was made


def pair_key(venue_a: str, id_a: str, venue_b: str, id_b: str) -> str:
    """Canonical, order-independent key for a cross-venue market pair.

    Args:
        venue_a: First venue slug (e.g. ``"polymarket"``).
        id_a: First venue's market id.
        venue_b: Second venue slug (e.g. ``"kalshi"``).
        id_b: Second venue's market id.

    Returns:
        A stable ``"lo|hi"`` key, identical regardless of argument order.
    """
    a = f"{venue_a.strip().lower()}:{id_a.strip()}"
    b = f"{venue_b.strip().lower()}:{id_b.strip()}"
    lo, hi = sorted((a, b))
    return f"{lo}|{hi}"


class VerdictStore(Protocol):
    """Read side the gate needs; a real store also persists (Supabase/db)."""

    def get(self, key: str) -> RuleComparison | None: ...


class WritableVerdictStore(VerdictStore, Protocol):
    """Read + write side the offline pre-compute needs (in-memory now, DB later)."""

    def put(self, comparison: RuleComparison) -> None: ...


@dataclass
class InMemoryVerdictStore:
    """Dict-backed store. The gate uses ``get``; the offline job uses ``put``.

    Persisting to the shared DB / Supabase is a follow-up; the interface is the
    same so the gate code does not change.
    """
    _by_key: dict[str, RuleComparison] = field(default_factory=dict)

    def get(self, key: str) -> RuleComparison | None:
        return self._by_key.get(key)

    def put(self, comparison: RuleComparison) -> None:
        self._by_key[comparison.pair_key] = comparison


def settlement_divergence_veto(
    key: str,
    store: VerdictStore,
    max_age_s: float = DEFAULT_VERDICT_MAX_AGE_S,
    now: float | None = None,
) -> tuple[bool, str]:
    """Deterministic fail-closed veto for one cross-venue pair.

    The pair may fire ONLY when a fresh MATCH verdict is on file; everything else
    vetoes — a missing verdict, an unrecognized value, DIVERGE / UNCERTAIN, a
    non-finite or future-dated timestamp, or a MATCH older than ``max_age_s``.
    ``max_age_s`` is always enforced — there is no "disabled" setting, so a config
    or clock mistake can never turn the gate fail-open.

    Args:
        key: The canonical pair key (see :func:`pair_key`).
        store: The verdict store to read from.
        max_age_s: Max age in seconds for a MATCH to count as fresh. Must be a
            finite, non-negative number; a malformed value vetoes.
        now: Current unix time, injectable for tests; defaults to ``time.time()``.

    Returns:
        ``(vetoed, reason)`` — ``(False, "")`` only on a fresh MATCH, otherwise
        ``(True, <human-readable reason>)``.
    """
    comparison = store.get(key)
    if comparison is None:
        return True, f"no settlement comparison on file for {key} — veto (fail-closed)"

    verdict = _coerce_verdict(comparison.verdict)
    if verdict is None:
        return True, f"unrecognized settlement verdict {comparison.verdict!r} for {key} — veto (fail-closed)"
    if verdict is not SettlementVerdict.MATCH:
        return True, f"settlement {verdict.value} for {key}: {comparison.rationale}"

    # Validate timing is finite before comparing — a NaN slips past every numeric
    # bound and inf would disable the window, so either must veto (fail-closed).
    max_age = _finite_float(max_age_s)
    if max_age is None or max_age < 0:
        return True, f"invalid settlement max_age_s {max_age_s!r} for {key} — veto (fail-closed)"
    compared_at = _finite_float(comparison.compared_at)
    current_time = _finite_float(now if now is not None else time.time())
    if compared_at is None or current_time is None:
        return True, f"invalid settlement timestamp for {key} — veto (fail-closed)"

    age = current_time - compared_at
    if age < 0:
        return True, f"settlement MATCH for {key} is future-dated ({age:.0f}s) — veto (clock/config error)"
    if age > max_age:
        return True, f"settlement MATCH for {key} is stale ({age:.0f}s > {max_age:.0f}s) — re-compare"
    return False, ""


# ---------------------------------------------------------------------------
# Offline pre-compute — the ONLY place the (LLM) comparator runs. Never the gate.
# ---------------------------------------------------------------------------

# Compares two settlement-rule texts → (verdict, rationale). Backed by an LLM in
# production; injected so the deterministic plumbing is tested without one. The
# verdict may be the enum or a raw string — precompute coerces it before storing.
RuleComparator = Callable[[str, str], "tuple[VerdictValue, str]"]


def precompute_comparison(
    key: str,
    rules_a: str,
    rules_b: str,
    comparator: RuleComparator,
    store: WritableVerdictStore,
    now: float | None = None,
) -> RuleComparison:
    """Run the comparator on two rule texts OFFLINE and store the verdict.

    This is a scheduled pre-trade job, never the execution path — the gate later
    reads the stored verdict deterministically (and never holds a comparator). The
    comparator's verdict is normalized to the enum so a string-returning LLM
    wrapper can't persist an invalid type that would later break the gate.

    Args:
        key: The canonical pair key (see :func:`pair_key`).
        rules_a: Settlement-rule text for the first venue.
        rules_b: Settlement-rule text for the second venue.
        comparator: Callable returning ``(verdict, rationale)``; the verdict may be
            the enum or a string.
        store: A writable store to persist the result into.
        now: Current unix time, injectable for tests; defaults to ``time.time()``.

    Returns:
        The stored :class:`RuleComparison`.

    Raises:
        ValueError: If the comparator returns an unrecognized verdict.
    """
    verdict_raw, rationale = comparator(rules_a, rules_b)
    verdict = _coerce_verdict(verdict_raw)
    if verdict is None:
        raise ValueError(f"comparator returned an unrecognized verdict: {verdict_raw!r}")
    comparison = RuleComparison(
        pair_key=key,
        verdict=verdict,
        rationale=rationale,
        compared_at=now if now is not None else time.time(),
    )
    store.put(comparison)
    return comparison
