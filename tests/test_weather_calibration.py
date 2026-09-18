"""Tests for weather_calibration — empirical-CDF shift derivation + PIT gate."""
from __future__ import annotations

import logging
import math
import random
import sys
from pathlib import Path

import pytest

logger = logging.getLogger(__name__)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from weather_calibration import (  # noqa: E402
    MIN_ERROR_SAMPLES,
    MIN_PIT_SAMPLES,
    PITResult,
    derive_shift,
    empirical_cdf,
    pit_uniformity,
    randomized_pit,
)
from weather_paper_rules import calibrate  # noqa: E402

FIVE = [-2.0, -1.0, 0.0, 1.0, 2.0]
# n = 30 sample with the same distinct values (6 copies each): the strict-count
# knots are 0.0, 0.2, 0.4, 0.6, 0.8 (#{e < v} / n at each distinct value), so
# interpolated values are hand-computable.
REP = FIVE * 6

# Synthetic gate verdicts for exercising derive_shift's structural PIT gate.
PASS_PIT = PITResult(statistic=0.01, threshold=0.1, n=100, passed=True, reason="synthetic pass")
FAIL_PIT = PITResult(statistic=0.5, threshold=0.1, n=100, passed=False, reason="synthetic fail")


class TestEmpiricalCdf:
    def test_left_continuous_at_data_values(self):
        # Event identity is strict: F_hat(x-) = #{e < x} / n at data values.
        n = 30
        assert empirical_cdf(REP, -2.0) == pytest.approx(0.5 / n)   # 0 strictly below, clamped
        assert empirical_cdf(REP, -1.0) == pytest.approx(6 / 30)
        assert empirical_cdf(REP, 0.0) == pytest.approx(12 / 30)    # NOT midrank 0.5
        assert empirical_cdf(REP, 1.0) == pytest.approx(18 / 30)
        assert empirical_cdf(REP, 2.0) == pytest.approx(24 / 30)

    def test_linear_interpolation_between_distinct_order_statistics(self):
        # strict-count knots: F(0) = 0.4, F(1) = 0.6, F(2) = 0.8
        assert empirical_cdf(REP, 0.5) == pytest.approx(0.5)
        assert empirical_cdf(REP, 0.25) == pytest.approx(0.45)
        assert empirical_cdf(REP, 1.5) == pytest.approx(0.7)

    def test_edges_clamped_never_zero_or_one(self):
        assert empirical_cdf(REP, -100.0) == pytest.approx(0.5 / 30)
        assert empirical_cdf(REP, 100.0) == pytest.approx(1.0 - 0.5 / 30)

    def test_tied_mass_excluded_by_event_identity(self):
        # 30 zeros: P(e < 0) is 0 by count — the midrank estimator would say
        # 0.5, wrongly crediting half the tied mass to the strict event.
        zeros = [0.0] * 30
        assert empirical_cdf(zeros, 0.0) == pytest.approx(0.5 / 30)

        tied = [0.0] * 24 + [1.0] * 6
        assert empirical_cdf(tied, 0.0) == pytest.approx(0.5 / 30)  # #{e < 0} = 0, clamped
        assert empirical_cdf(tied, 1.0) == pytest.approx(24 / 30)   # #{e < 1} = 24
        # between distinct values: strict-count knots 0.0 (at 0.0) and 0.8 (at 1.0)
        assert empirical_cdf(tied, 0.5) == pytest.approx(0.4)

    def test_short_sample_fails_closed(self):
        with pytest.raises(ValueError):
            empirical_cdf(FIVE, 0.0)
        with pytest.raises(ValueError):
            empirical_cdf([], 0.0)

    def test_min_samples_floor_cannot_be_lowered(self):
        # min_samples only raises the module floor; n = 5 stays rejected.
        with pytest.raises(ValueError):
            empirical_cdf(FIVE, 0.0, min_samples=1)
        # raising the floor above n = 30 is honored
        with pytest.raises(ValueError):
            empirical_cdf(REP, 0.0, min_samples=45)
        assert MIN_ERROR_SAMPLES == 30

    def test_non_int_min_samples_rejected(self):
        for bad in (float("nan"), float("inf"), 1.0, "30", True, 0, -3):
            with pytest.raises(ValueError):
                empirical_cdf(REP, 0.0, min_samples=bad)

    def test_extreme_magnitude_samples_no_overflow(self):
        # x_hi - x_lo overflows to inf for valid finite inputs; the rescale
        # path must still return a finite, correctly interpolated value.
        extreme = [-1e308] * 15 + [1e308] * 15  # strict-count knots 0.0 and 0.5
        assert empirical_cdf(extreme, 0.0) == pytest.approx(0.25)
        assert empirical_cdf(extreme, 0.5e308) == pytest.approx(0.375)

    def test_non_finite_inputs_rejected(self):
        with pytest.raises(ValueError):
            empirical_cdf(REP, float("nan"))
        with pytest.raises(ValueError):
            empirical_cdf([0.0] * 29 + [float("inf")], 0.0)

    def test_monotone_nondecreasing_around_every_knot(self):
        # Regression (CodeRabbit, PR #85): with midrank/Hazen knots the
        # interpolant approached 0.7 just below a tied value while the exact
        # hit returned the strict count 0.6 — increasing x LOWERED the
        # probability. Strict-count knots make the estimator monotone:
        # check immediately below / at / immediately above every knot.
        eps = 1e-9
        for sample in (REP, [0.0] * 24 + [1.0] * 6, sorted({float(i % 7) for i in range(70)} | {2.5}) * 6):
            knots = sorted(set(sample))
            probes = sorted(
                {v + d for v in knots for d in (-eps, 0.0, eps)}
                | {knots[0] - 1.0, knots[-1] + 1.0})
            values = [empirical_cdf(sample, x) for x in probes]
            for a, b in zip(values, values[1:]):
                assert b >= a, "empirical_cdf must be nondecreasing"

    def test_monotone_on_dense_grid(self):
        # Property: nondecreasing on a dense grid spanning past both edges.
        grid = [x / 50.0 for x in range(-150, 151)]
        values = [empirical_cdf(REP, x) for x in grid]
        for a, b in zip(values, values[1:]):
            assert b >= a

    def test_exact_hit_agrees_with_left_limit(self):
        # The CodeRabbit counterexample shape: the left limit at a tied value
        # must not exceed the exact-hit strict count.
        assert empirical_cdf(REP, 1.0 - 1e-9) <= empirical_cdf(REP, 1.0)
        assert empirical_cdf(REP, 1.0) == pytest.approx(18 / 30)
        assert empirical_cdf(REP, 1.0 - 1e-9) == pytest.approx(0.6, abs=1e-6)


class TestDeriveShift:
    def test_hand_computed_shift(self):
        # F(0-) = 12/30 = 0.4; raw 0.4 -> shift 0
        assert derive_shift(REP, 0.4, 0.0, PASS_PIT) == pytest.approx(0.0)
        # F(1-) = 18/30 = 0.6; raw 0.7 -> shift -0.1
        assert derive_shift(REP, 0.7, 1.0, PASS_PIT) == pytest.approx(-0.1)
        # F(0.5) = 0.5 (interpolated between strict-count knots 0.4 and 0.6);
        # raw 0.9 -> shift -0.4
        assert derive_shift(REP, 0.9, 0.5, PASS_PIT) == pytest.approx(-0.4)

    def test_event_identity_with_tied_mass(self):
        # NBM exactly right every time (all errors 0): P(actual > threshold)
        # for a forecast sitting at the threshold (d = 0) is 0 by the strict
        # identity — clamped to 0.5/n, never the midrank 0.5.
        zeros = [0.0] * 30
        shift = derive_shift(zeros, 0.5, 0.0, PASS_PIT)
        assert calibrate(0.5, shift) == pytest.approx(0.5 / 30)

    def test_hot_biased_model_shifted_down(self):
        hot = [float(i) for i in range(1, 31)]  # NBM ran hot every time
        shift = derive_shift(hot, 0.5, 0.0, PASS_PIT)
        assert calibrate(0.5, shift) == pytest.approx(0.5 / 30)
        assert shift < 0

    def test_failed_pit_gate_raises(self):
        with pytest.raises(ValueError, match="PIT gate failed"):
            derive_shift(REP, 0.5, 0.0, FAIL_PIT)
        # a real failing verdict from pit_uniformity is equally rejected
        real_fail = pit_uniformity([0.5] * 100)
        assert real_fail.passed is False
        with pytest.raises(ValueError, match="PIT gate failed"):
            derive_shift(REP, 0.5, 0.0, real_fail)

    def test_raw_prob_out_of_range_rejected(self):
        for bad in (-0.01, 1.01, float("nan")):
            with pytest.raises(ValueError):
                derive_shift(REP, bad, 0.0, PASS_PIT)

    def test_short_sample_fails_closed_even_with_min_samples_one(self):
        with pytest.raises(ValueError):
            derive_shift(FIVE, 0.5, 0.0, PASS_PIT)
        with pytest.raises(ValueError):
            derive_shift(FIVE, 0.5, 0.0, PASS_PIT, min_samples=1)

    def test_calibrate_output_stays_in_unit_interval(self):
        # Property: for any raw_prob/threshold grid, calibrate(raw, shift)
        # matches the empirical-CDF value (up to floating-point rounding)
        # and stays within [0, 1].
        errors = [(-1) ** i * (i * 0.37 % 4.0) for i in range(60)]
        for raw in (0.0, 0.1, 0.5, 0.9, 1.0):
            for dist in (-6.0, -1.5, 0.0, 0.7, 2.2, 8.0):
                shift = derive_shift(errors, raw, dist, PASS_PIT)
                out = calibrate(raw, shift)
                assert 0.0 <= out <= 1.0
                assert out == pytest.approx(empirical_cdf(errors, dist))

    def test_determinism(self):
        errors = [math.sin(i) * 3 for i in range(50)]
        a = derive_shift(list(errors), 0.42, 1.3, PASS_PIT)
        b = derive_shift(list(errors), 0.42, 1.3, PASS_PIT)
        assert a == b
        assert empirical_cdf(errors, 0.9) == empirical_cdf(errors, 0.9)


class TestRandomizedPit:
    def test_deterministic_with_seeded_rng(self):
        errors = [float(i % 7) for i in range(70)]
        seq_a = [randomized_pit(errors, float(i % 7), random.Random(42)) for i in range(10)]
        seq_b = [randomized_pit(errors, float(i % 7), random.Random(42)) for i in range(10)]
        assert seq_a == seq_b

    def test_values_within_unit_interval(self):
        errors = [float(i % 7) for i in range(70)]
        rng = random.Random(7)
        for obs in (-5.0, 0.0, 3.0, 6.0, 10.0):
            assert 0.0 <= randomized_pit(errors, obs, rng) <= 1.0

    def test_spreads_across_tied_block_mass(self):
        # u = F(x-) + V * (F(x) - F(x-)): for 30 zeros in a 30-sample archive,
        # u must land inside [0, 1] spanning the whole tied block, not at 0.5.
        zeros = [0.0] * 30
        rng = random.Random(1)
        us = [randomized_pit(zeros, 0.0, rng) for _ in range(50)]
        assert all(0.0 <= u <= 1.0 for u in us)
        assert max(us) - min(us) > 0.5  # spread, not an atom

    def test_fail_closed_edges(self):
        with pytest.raises(ValueError):
            randomized_pit([0.0] * 5, 0.0, random.Random(0))
        with pytest.raises(ValueError):
            randomized_pit([0.0] * 30, float("nan"), random.Random(0))
        with pytest.raises(ValueError):
            randomized_pit([0.0] * 30, 0.0, random.Random(0), min_samples=1.5)

    def test_discrete_sample_randomized_passes_midrank_fails(self):
        # Rounded observations: 7 discrete values. Raw midrank PITs have
        # atoms -> continuous-null KS rejects; randomized PITs are uniform
        # under the null -> KS passes. Same data, same test.
        archive = [float(v) for v in range(-3, 4) for _ in range(30)]  # n = 210
        holdout = [float(v) for v in range(-3, 4) for _ in range(100)]  # n = 700
        n_arch = len(archive)

        midrank = []
        for obs in holdout:
            less = sum(1 for e in archive if e < obs)
            equal = sum(1 for e in archive if e == obs)
            midrank.append((less + 0.5 * equal) / n_arch)
        assert pit_uniformity(midrank).passed is False

        rng = random.Random(42)
        randomized = [randomized_pit(archive, obs, rng) for obs in holdout]
        assert pit_uniformity(randomized).passed is True


class TestPitUniformity:
    def test_uniform_grid_passes(self):
        n = 100
        pit = [(i + 0.5) / n for i in range(n)]
        res = pit_uniformity(pit)
        assert isinstance(res, PITResult)
        assert res.passed is True
        assert res.n == n
        # exact D for the mid-grid is 0.5 / n
        assert res.statistic == pytest.approx(0.5 / n)
        assert res.statistic < res.threshold

    def test_clustered_values_fail(self):
        res = pit_uniformity([0.5] * 100)
        assert res.passed is False
        assert res.statistic == pytest.approx(0.5)

    def test_skewed_values_fail(self):
        # everything piled in the bottom quartile
        res = pit_uniformity([(i + 0.5) / 400 for i in range(100)])
        assert res.passed is False

    def test_tiny_n_fails_closed(self):
        res = pit_uniformity([0.1, 0.5, 0.9])
        assert res.passed is False
        assert res.n == 3
        assert math.isnan(res.statistic)
        assert "fail-closed" in res.reason
        assert MIN_PIT_SAMPLES == 30

    def test_min_samples_floor_cannot_be_lowered(self):
        # even with min_samples=1, n = 10 stays below the module floor
        res = pit_uniformity([(i + 0.5) / 10 for i in range(10)], min_samples=1)
        assert res.passed is False
        assert "fail-closed" in res.reason

    def test_non_int_min_samples_rejected(self):
        for bad in (0, -1, float("nan"), float("inf"), 30.0, True):
            with pytest.raises(ValueError):
                pit_uniformity([0.5] * 30, min_samples=bad)

    def test_out_of_range_values_rejected(self):
        with pytest.raises(ValueError):
            pit_uniformity([0.5] * 29 + [1.5])
        with pytest.raises(ValueError):
            pit_uniformity([float("nan")] * 30)

    def test_threshold_is_asymptotic_formula(self):
        res = pit_uniformity([(i + 0.5) / 50 for i in range(50)])
        assert res.threshold == pytest.approx(
            math.sqrt(-0.5 * math.log(0.025)) / math.sqrt(50))

    def test_determinism(self):
        pit = [((i * 7919) % 100 + 0.5) / 100 for i in range(100)]
        assert pit_uniformity(list(pit)) == pit_uniformity(list(pit))


class TestPitTypeValidation:
    def test_none_pit_raises_type_error(self):
        errors = [float(i) for i in range(30)]
        with pytest.raises(TypeError):
            derive_shift(errors, 0.5, 10.0, None)

    def test_duck_typed_fake_pit_rejected(self):
        class FakePit:
            passed = True
            reason = "fake"
        errors = [float(i) for i in range(30)]
        with pytest.raises(TypeError):
            derive_shift(errors, 0.5, 10.0, FakePit())
