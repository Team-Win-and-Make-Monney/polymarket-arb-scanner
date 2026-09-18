"""Weather calibration math — empirical-CDF shift derivation + PIT gate.

Fills the ``derive_shift`` seam flagged in the Plan 11 draft spec (Open
Question #1): ``weather_paper_rules.calibrate(raw_prob, shift)`` expects a
scalar shift in PROBABILITY space, but the NBM error archive is naturally in
DEGREE space (forecast MaxT - actual MaxT). This module performs the
conversion via empirical-CDF percentile interpolation, and provides the
PIT-uniformity diagnostic that structurally gates whether the calibration
may be used at all.

Semantics
---------
With signed errors ``e = forecast_max_f - actual_max_f`` (positive = NBM ran
hot) and ``d = forecast_max_f - bracket_threshold_f`` (the threshold
distance, positive = forecast above the bracket threshold):

    actual > threshold  <=>  e < d          (STRICT inequality)

so the calibrated probability that the actual MaxT clears the threshold is
the LEFT limit of the empirical CDF of historical errors at ``d`` —
``F_hat(d-) ~= P(e < d)`` — not the midrank value, which would wrongly count
half of any tied mass at ``d`` toward the event. ``derive_shift`` returns
``F_hat(d-) - raw_prob`` so that ``calibrate(raw_prob, shift)`` equals
``F_hat(d-)`` up to floating-point rounding (the value is already within
[0, 1], so calibrate()'s clamp only absorbs rounding).

Empirical CDF construction: knots at DISTINCT order statistics carrying
their strict-count probabilities ``#{e < v} / n``, linear interpolation
between knots — a tied block's jump is spread over the gap to the NEXT
distinct value, so the estimator is monotone nondecreasing and interpolation
never counts tied mass at a knot toward the strict event (midrank/Hazen
knots would sit above the strict count inside tied blocks and decrease at
exact hits). At a point EQUAL to a data value the left-continuous,
strict-count value ``#{e < x} / n`` is returned — identical to the knot.
Everywhere the result is clamped to ``[0.5 / n, 1 - 0.5 / n]`` — never
exactly 0 or 1, so no finite sample can claim certainty.

Fail-closed edges: fewer than the module-floor number of errors, non-finite
inputs, or ``raw_prob`` outside [0, 1] raise ``ValueError``. The
``min_samples`` parameters can only RAISE the hard floors
``MIN_ERROR_SAMPLES`` / ``MIN_PIT_SAMPLES``, never lower them, and must be
ints. Callers must not fall back to an uncalibrated path silently.

PIT gate (structural): ``pit_uniformity`` runs a one-sample
Kolmogorov-Smirnov test of probability-integral-transform values against
Uniform(0, 1), using the asymptotic critical value
``D_crit = 1.3581 / sqrt(n)`` at alpha = 0.05 (Smirnov's asymptotic formula
— adequate for the n >= 30 samples this gate requires; exact small-sample
tables are NOT used, which is why small n fails closed instead). The KS
null assumes a CONTINUOUS distribution: PIT values from rounded/discrete
observations have atoms and must be produced with ``randomized_pit`` (or
come from genuinely continuous data) or the test is not distribution-free.
``derive_shift`` REQUIRES a ``PITResult`` and raises
``ValueError`` when it did not pass — a failed PIT gate makes
the shift unusable by construction, not by caller discipline.

Pure + deterministic (``randomized_pit`` takes a caller-seeded
``random.Random``), stdlib only.
"""
from __future__ import annotations

import bisect
import logging
import math
import random
from dataclasses import dataclass

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Hard module floor on the error-archive size before a shift may be derived.
# Below this the empirical CDF is too coarse to interpolate meaningfully —
# fail closed. Callers can only raise this via min_samples, never lower it.
MIN_ERROR_SAMPLES = 30

# Hard module floor on the PIT sample size for the asymptotic KS critical
# value to be a fair approximation. Below it the gate fails closed.
MIN_PIT_SAMPLES = 30

# Smirnov asymptotic critical-value coefficient at alpha = 0.05:
# D_crit = c(alpha) / sqrt(n), c(0.05) = sqrt(-0.5 * ln(0.05 / 2)) ~= 1.3581.
KS_ALPHA = 0.05
_KS_COEFF_05 = math.sqrt(-0.5 * math.log(KS_ALPHA / 2.0))


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PITResult:
    """Outcome of the PIT-uniformity gate."""

    statistic: float   # KS D statistic vs Uniform(0, 1); NaN when n too small
    threshold: float   # critical value the statistic is compared against
    n: int             # number of PIT values tested
    passed: bool       # hard gate: False => reject the calibration
    reason: str        # human-readable explanation of the verdict


# ---------------------------------------------------------------------------
# Internal validation helpers
# ---------------------------------------------------------------------------


def _effective_min(min_samples: int, floor: int, func_name: str) -> int:
    """Validate ``min_samples`` and apply the hard module floor.

    Callers can only RAISE the floor, never lower it. Non-int values
    (including bool, NaN, inf) are rejected outright.
    """
    if isinstance(min_samples, bool) or not isinstance(min_samples, int):
        raise ValueError(
            "%s: min_samples must be an int, got %r" % (func_name, min_samples))
    if min_samples < 1:
        raise ValueError(
            "%s: min_samples must be >= 1, got %d" % (func_name, min_samples))
    return max(min_samples, floor)


def _validated_errors(errors_degrees: list[float], effective_min: int,
                      func_name: str) -> list[float]:
    """Finiteness + floor checks; returns the sorted sample."""
    if len(errors_degrees) < effective_min:
        raise ValueError(
            "%s: %d error samples < required minimum %d (fail-closed)"
            % (func_name, len(errors_degrees), effective_min))
    for e in errors_degrees:
        if not math.isfinite(e):
            raise ValueError("%s: non-finite error sample %r" % (func_name, e))
    return sorted(errors_degrees)


# ---------------------------------------------------------------------------
# Empirical CDF (left-continuous at data values, interpolated between)
# ---------------------------------------------------------------------------


def empirical_cdf(errors_degrees: list[float], x: float,
                  min_samples: int = MIN_ERROR_SAMPLES) -> float:
    """Strict-event empirical CDF estimate ``F_hat(x-) ~= P(e < x)``.

    At a point equal to a data value the left-continuous, strict-count value
    ``#{e < x} / n`` is returned (no half tied mass — the event identity
    ``actual > threshold <=> e < d`` is strict). Between distinct order
    statistics, linear interpolation between strict-count knots is used —
    each distinct value carries ``#{e < v} / n``, matching the exact-hit
    value, so the estimator is monotone nondecreasing everywhere.
    The result is clamped to [0.5/n, 1 - 0.5/n] everywhere.

    Args:
        errors_degrees: Historical error sample in degree space
            (forecast MaxT - actual MaxT).
        x: Point at which to evaluate the estimator (degree space).
        min_samples: Minimum sample size, an int >= 1. Only raises the hard
            module floor ``MIN_ERROR_SAMPLES``; it can never lower it.

    Returns:
        The estimate in (0, 1) — never exactly 0 or 1 for a finite sample.

    Raises:
        ValueError: If ``min_samples`` is not an int or < 1, the sample is
            shorter than the effective minimum, or ``x`` / any sample value
            is non-finite (fail-closed).
    """
    eff_min = _effective_min(min_samples, MIN_ERROR_SAMPLES, "empirical_cdf")
    if not math.isfinite(x):
        raise ValueError("empirical_cdf: x must be finite, got %r" % (x,))
    ordered = _validated_errors(errors_degrees, eff_min, "empirical_cdf")
    n = len(ordered)
    lo_clamp = 0.5 / n
    hi_clamp = 1.0 - 0.5 / n

    lt = bisect.bisect_left(ordered, x)
    le = bisect.bisect_right(ordered, x)
    if le > lt:
        # x coincides with a data value: left-continuous strict count.
        return min(max(lt / n, lo_clamp), hi_clamp)

    if x < ordered[0]:
        return lo_clamp
    if x > ordered[-1]:
        return hi_clamp

    # Knots: distinct values carrying their strict-count probability
    # #{e < v} / n — the same value the exact-hit branch returns, so the
    # interpolant is monotone (midrank/Hazen knots sit above the strict
    # count inside tied blocks and would decrease at exact hits).
    xs: list[float] = []
    ps: list[float] = []
    i = 0
    while i < n:
        j = i
        while j + 1 < n and ordered[j + 1] == ordered[i]:
            j += 1
        xs.append(ordered[i])
        ps.append(i / n)
        i = j + 1

    k = bisect.bisect_right(xs, x)
    x_lo, x_hi = xs[k - 1], xs[k]
    p_lo, p_hi = ps[k - 1], ps[k]
    span = x_hi - x_lo
    if math.isinf(span):
        # Finite endpoints whose difference overflows float range: rescale by
        # the largest magnitude so the interpolation fraction stays finite.
        scale = max(abs(x), abs(x_lo), abs(x_hi))
        logger.debug("empirical_cdf: rescaling interpolation span by %g to avoid overflow", scale)
        x, x_lo, x_hi = x / scale, x_lo / scale, x_hi / scale
        span = x_hi - x_lo
    value = p_lo + (p_hi - p_lo) * (x - x_lo) / span
    return min(max(value, lo_clamp), hi_clamp)


# ---------------------------------------------------------------------------
# Shift derivation (degree space -> probability space), PIT-gated
# ---------------------------------------------------------------------------


def derive_shift(errors_degrees: list[float], raw_prob: float,
                 threshold_distance_degrees: float, pit: PITResult,
                 min_samples: int = MIN_ERROR_SAMPLES) -> float:
    """Probability-space shift for ``weather_paper_rules.calibrate``.

    The PIT gate is structural: a ``PITResult`` from ``pit_uniformity`` is
    REQUIRED, and a failed gate raises instead of returning a number —
    calibration whose PIT diagnostic failed must never be used.

    Args:
        errors_degrees: Historical NBM errors in degree space
            (forecast MaxT - actual MaxT).
        raw_prob: Uncalibrated model probability of the YES outcome
            (actual MaxT strictly above the bracket threshold), in [0, 1].
        threshold_distance_degrees: Forecast MaxT - bracket threshold.
        pit: The PIT-uniformity verdict for this error archive (run
            ``pit_uniformity`` on held-out randomized PITs first).
        min_samples: Minimum error-archive size, an int >= 1. Only raises
            the hard module floor ``MIN_ERROR_SAMPLES``.

    Returns:
        ``shift`` such that ``calibrate(raw_prob, shift)`` equals the
        left-continuous empirical-CDF probability F_hat(d-) up to
        floating-point rounding.

    Raises:
        TypeError: If ``pit`` is not a ``PITResult`` from ``pit_uniformity``
            (duck-typed fakes and ``None`` are rejected, not trusted).
        ValueError: If ``pit.passed`` is False, ``raw_prob`` is outside
            [0, 1], the sample is too short, ``min_samples`` is invalid, or
            any input is non-finite — callers must treat this as "no
            calibration available", not shift = 0 (fail-closed).
    """
    if not isinstance(pit, PITResult):
        raise TypeError(
            "derive_shift: pit must be a PITResult from pit_uniformity, got %r"
            % (type(pit).__name__,))
    if not pit.passed:
        raise ValueError(
            "derive_shift: PIT gate failed (%s) — calibration rejected" % pit.reason)
    if not math.isfinite(raw_prob) or not 0.0 <= raw_prob <= 1.0:
        raise ValueError("derive_shift: raw_prob must be in [0, 1], got %r" % (raw_prob,))
    calibrated = empirical_cdf(errors_degrees, threshold_distance_degrees,
                               min_samples=min_samples)
    return calibrated - raw_prob


# ---------------------------------------------------------------------------
# PIT construction + uniformity gate
# ---------------------------------------------------------------------------


def randomized_pit(errors_degrees: list[float], observed: float,
                   rng: random.Random,
                   min_samples: int = MIN_ERROR_SAMPLES) -> float:
    """Randomized PIT ``u = F_hat(x-) + V * (F_hat(x) - F_hat(x-))``.

    For discrete/rounded observations the plain (midrank) PIT has atoms, so
    the continuous-null KS test in ``pit_uniformity`` is not
    distribution-free. Randomizing uniformly across each tied block's
    probability mass (V ~ Uniform(0, 1) from the caller-seeded ``rng``)
    restores uniformity under the null. Here ``F_hat`` is the raw step
    ECDF: ``F_hat(x-) = #{e < x} / n``, ``F_hat(x) = #{e <= x} / n``.

    Args:
        errors_degrees: The historical error sample defining the CDF.
        observed: The held-out observed error to transform.
        rng: Caller-seeded ``random.Random`` — seeding makes runs
            deterministic.
        min_samples: Minimum sample size, an int >= 1. Only raises the hard
            module floor ``MIN_ERROR_SAMPLES``.

    Returns:
        A PIT value in [0, 1].

    Raises:
        ValueError: If ``min_samples`` is invalid, the sample is too short,
            or any input is non-finite (fail-closed).
    """
    eff_min = _effective_min(min_samples, MIN_ERROR_SAMPLES, "randomized_pit")
    if not math.isfinite(observed):
        raise ValueError("randomized_pit: observed must be finite, got %r" % (observed,))
    ordered = _validated_errors(errors_degrees, eff_min, "randomized_pit")
    n = len(ordered)
    lt = bisect.bisect_left(ordered, observed)
    eq = bisect.bisect_right(ordered, observed) - lt
    return (lt + rng.random() * eq) / n


def pit_uniformity(pit_values: list[float],
                   min_samples: int = MIN_PIT_SAMPLES) -> PITResult:
    """Kolmogorov-Smirnov test of PIT values against Uniform(0, 1).

    ``pit_values`` are PITs over held-out (forecast, outcome) pairs; a
    well-calibrated CDF makes them ~Uniform(0, 1). The KS null assumes a
    continuous distribution: inputs must be randomized PITs (see
    ``randomized_pit``) or come from genuinely continuous observations —
    raw midrank PITs of rounded data have atoms and bias the test.

    Args:
        pit_values: PIT values, each in [0, 1].
        min_samples: Minimum sample size, an int >= 1. Only raises the hard
            module floor ``MIN_PIT_SAMPLES``. Below the effective minimum
            the gate fails closed (passed=False).

    Returns:
        A ``PITResult``; ``passed`` is True iff n meets the effective
        minimum and D < 1.3581 / sqrt(n) (asymptotic alpha = 0.05 critical
        value). Anything else — including tiny samples — fails closed.

    Raises:
        ValueError: If ``min_samples`` is not an int or < 1, or any PIT
            value is outside [0, 1] or non-finite.
    """
    eff_min = _effective_min(min_samples, MIN_PIT_SAMPLES, "pit_uniformity")
    n = len(pit_values)
    for v in pit_values:
        if not math.isfinite(v) or not 0.0 <= v <= 1.0:
            raise ValueError("pit_uniformity: PIT value out of [0, 1]: %r" % (v,))

    if n < eff_min:
        return PITResult(
            statistic=float("nan"), threshold=float("nan"), n=n, passed=False,
            reason="fail-closed: %d PIT values < required minimum %d" % (n, eff_min))

    ordered = sorted(pit_values)
    d_stat = 0.0
    for i, u in enumerate(ordered):
        d_plus = (i + 1) / n - u
        d_minus = u - i / n
        d_stat = max(d_stat, d_plus, d_minus)

    threshold = _KS_COEFF_05 / math.sqrt(n)
    passed = d_stat < threshold
    reason = ("KS D=%.4f %s critical %.4f (asymptotic, alpha=%.2f, n=%d)"
              % (d_stat, "<" if passed else ">=", threshold, KS_ALPHA, n))
    return PITResult(statistic=d_stat, threshold=threshold, n=n,
                     passed=passed, reason=reason)
