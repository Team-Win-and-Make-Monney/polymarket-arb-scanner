"""Tests for broker/validator.py — every rulebook rule: one passing + one
failing (fail-closed) test. DoD items 2, 3, 4, 6."""

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))

from broker.queue import HALT_SCOPE_ALL, HALT_SCOPE_CAPITAL, Intent, IntentQueue
from broker.validator import BrokerValidator
from broker_helpers import GATE_CONFIG, healthy_sources, make_policy


def flip_enable(lane="kalshi-lip", venue="kalshi", key="f1"):
    return Intent("flip_lane", {"lane": lane, "venue": venue, "action": "enable"}, key)


def flip_disable(key="f2"):
    return Intent("flip_lane", {"lane": "kalshi-lip", "venue": "kalshi",
                                "action": "disable"}, key)


def move(amount=100.0, key="m1", **extra):
    # move_capital is market-scoped; the per-market + book-depth caps cannot be
    # verified without a named market, so a default is supplied here. Book depth
    # is read from the LIVE source (healthy_sources), never from this payload.
    payload = {"amount_usd": amount, "from_venue": "kalshi", "to_venue": "polymarket",
               "market": "KXTEST"}
    payload.update(extra)
    return Intent("move_capital", payload, key)


def make_validator(policy=None, sources=None, queue=None):
    queue = queue or IntentQueue(":memory:")
    return BrokerValidator(policy or make_policy(), sources or healthy_sources(), queue)


# ---------------------------------------------------------------------------
# Caps (T1 principal + realized P&L; $300/market; book depth)
# ---------------------------------------------------------------------------

class TestCaps:
    def test_pass_within_all_caps(self):
        v = make_validator()
        assert v._check_caps(move(100.0)).ok

    def test_fail_zero_amount(self):
        assert not make_validator()._check_caps(move(0.0)).ok

    def test_fail_new_principal_operator_gated(self):
        result = make_validator()._check_caps(move(100.0, source="principal"))
        assert not result.ok
        assert "operator-gated" in result.reason

    def test_fail_new_principal_case_insensitive(self):
        # "Principal"/"PRINCIPAL" must not slip past the gate (Codex R2 #3).
        for variant in ("Principal", "PRINCIPAL", " principal "):
            result = make_validator()._check_caps(move(100.0, source=variant))
            assert not result.ok, variant
            assert "operator-gated" in result.reason

    def test_fail_post_action_over_ceiling(self):
        # ceiling 8200; portfolio 8100 (under) but 8100 + 200 = 8300 over the
        # working ceiling — must fail on POST-ACTION exposure (Codex R2 #2).
        v = make_validator(sources=healthy_sources(portfolio_value_usd=lambda: 8100.0))
        result = v._check_caps(move(200.0))
        assert not result.ok
        assert "post-action" in result.reason

    def test_fail_portfolio_over_ceiling(self):
        # ceiling = 8000 + 200 realized; portfolio 9000 breaches it
        v = make_validator(sources=healthy_sources(portfolio_value_usd=lambda: 9000.0))
        result = v._check_caps(move(100.0))
        assert not result.ok
        assert "ceiling" in result.reason

    def test_negative_pnl_shrinks_ceiling(self):
        # ceiling = 8000 - 7950 = 50; portfolio 40 is fine but amount 100 is not
        v = make_validator(sources=healthy_sources(
            realized_pnl_usd=lambda: -7950.0, portfolio_value_usd=lambda: 40.0))
        assert not v._check_caps(move(100.0)).ok

    def test_pass_market_scoped_within_cap_and_depth(self):
        # Live depth 5000 (healthy source); 200 <= 300 cap and <= 5000 depth.
        v = make_validator()
        assert v._check_caps(move(200.0, market="BTC-DEC")).ok

    def test_fail_per_market_cap(self):
        v = make_validator()
        result = v._check_caps(move(400.0, market="BTC-DEC"))
        assert not result.ok
        assert "per-market cap" in result.reason

    def test_fail_move_without_market(self):
        # A move that omits 'market' must NOT be silently exempt from the
        # per-market/depth caps — it is rejected (Codex finding #4).
        no_market = Intent("move_capital",
                           {"amount_usd": 500.0, "from_venue": "kalshi",
                            "to_venue": "polymarket"}, "nm1")
        result = make_validator()._check_caps(no_market)
        assert not result.ok
        assert "must name a 'market'" in result.reason

    def test_fail_amount_over_live_depth(self):
        # Depth comes from the LIVE source, not the payload: a move within the
        # $300 cap still fails when live book depth is thinner than the amount.
        v = make_validator(sources=healthy_sources(
            market_book_depth_usd=lambda market: 150.0))
        result = v._check_caps(move(200.0, market="BTC-DEC"))
        assert not result.ok
        assert "live book depth" in result.reason

    def test_payload_book_depth_cannot_override_live(self):
        # A gamed intent claiming huge depth in its payload must be ignored;
        # the broker trusts only the live source (Codex finding #5).
        v = make_validator(sources=healthy_sources(
            market_book_depth_usd=lambda market: 50.0))
        result = v._check_caps(move(200.0, market="BTC-DEC", book_depth_usd=10000.0))
        assert not result.ok
        assert "live book depth" in result.reason

    def test_fail_closed_on_nan_live_depth(self):
        v = make_validator(sources=healthy_sources(
            market_book_depth_usd=lambda market: float("nan")))
        results = v.validate(move(200.0, market="BTC-DEC"))
        caps = next(r for r in results if r.name == "caps")
        assert not caps.ok
        assert "non-finite" in caps.reason

    def test_fail_closed_on_nan_portfolio(self):
        # NaN > ceiling is always False — must not pass the cap check.
        v = make_validator(sources=healthy_sources(
            portfolio_value_usd=lambda: float("nan")))
        results = v.validate(move())
        caps = next(r for r in results if r.name == "caps")
        assert not caps.ok
        assert "non-finite" in caps.reason

    def test_fail_closed_on_boolean_portfolio(self):
        # bool is an int subclass — a live source returning True must not be
        # silently coerced to a plausible $1.00 portfolio value.
        v = make_validator(sources=healthy_sources(
            portfolio_value_usd=lambda: True))
        results = v.validate(move())
        caps = next(r for r in results if r.name == "caps")
        assert not caps.ok
        assert "must be a number" in caps.reason

    def test_fail_closed_when_portfolio_source_raises(self):
        def boom():
            raise ConnectionError("venue API down")
        v = make_validator(sources=healthy_sources(portfolio_value_usd=boom))
        results = v.validate(move())
        caps = next(r for r in results if r.name == "caps")
        assert not caps.ok
        assert "fail-closed" in caps.reason


# ---------------------------------------------------------------------------
# Allowlist (sportsbooks never)
# ---------------------------------------------------------------------------

class TestAllowlist:
    def test_pass_allowlisted_venue(self):
        assert make_validator()._check_allowlist(flip_enable()).ok

    def test_fail_unlisted_venue(self):
        result = make_validator()._check_allowlist(flip_enable(venue="hyperliquid"))
        assert not result.ok
        assert "not on venue allowlist" in result.reason

    def test_fail_sportsbook_always(self):
        # Even if someone puts a sportsbook ON the allowlist, it still fails.
        policy = make_policy(venue_allowlist=["kalshi", "draftkings"])
        result = make_validator(policy=policy)._check_allowlist(
            flip_enable(venue="draftkings"))
        assert not result.ok
        assert "never permitted" in result.reason

    def test_fail_no_venue_named(self):
        intent = Intent("rotate_secret", {"secret_name": "X"}, "r1")
        assert not make_validator()._check_allowlist(intent).ok

    def test_move_checks_both_venues(self):
        v = make_validator()
        bad = Intent("move_capital", {"amount_usd": 10.0, "from_venue": "kalshi",
                                      "to_venue": "bovada"}, "m9")
        assert not v._check_allowlist(bad).ok

    def test_sportsbook_whitespace_alias_still_blocked(self):
        # A padded venue ("draftkings ") must not dodge the sportsbook set via
        # lowercase-only comparison (Codex R3 finding #4).
        policy = make_policy(venue_allowlist=["kalshi", "draftkings"],
                             sportsbook_venues=["draftkings"])
        result = make_validator(policy=policy)._check_allowlist(
            flip_enable(venue="draftkings "))
        assert not result.ok
        assert "never permitted" in result.reason

    def test_whitespace_padded_allowlisted_venue_matches(self):
        # The flip side: a padded allowlisted venue still resolves to allowed.
        assert make_validator()._check_allowlist(flip_enable(venue=" kalshi ")).ok


# ---------------------------------------------------------------------------
# Gate-config hash (DoD item 4 — simulated merged threshold edit)
# ---------------------------------------------------------------------------

class TestGateHashes:
    def test_pass_when_live_matches_registered(self):
        assert make_validator()._check_gate_hashes(flip_enable()).ok

    def test_fail_on_merged_threshold_edit(self):
        edited = dict(GATE_CONFIG, min_net_roi=0.001)  # loosened via a merge
        v = make_validator(sources=healthy_sources(gate_config=lambda name: edited))
        result = v._check_gate_hashes(flip_enable())
        assert not result.ok
        assert "hash mismatch" in result.reason

    def test_fail_closed_when_gate_unreadable(self):
        def boom(name):
            raise KeyError(name)
        v = make_validator(sources=healthy_sources(gate_config=boom))
        results = v.validate(flip_enable())
        gate = next(r for r in results if r.name == "gate_hashes")
        assert not gate.ok
        assert "fail-closed" in gate.reason


# ---------------------------------------------------------------------------
# Kill-state
# ---------------------------------------------------------------------------

class TestKillState:
    def test_pass_when_clear(self):
        assert make_validator()._check_kill_state(flip_enable()).ok

    def test_fail_global_kill(self):
        policy = make_policy(kill_state={"global": True, "lanes": {}})
        assert not make_validator(policy=policy)._check_kill_state(flip_enable()).ok

    def test_fail_any_lane_halted(self):
        policy = make_policy(kill_state={"global": False,
                                         "lanes": {"perp-carry": True}})
        result = make_validator(policy=policy)._check_kill_state(move())
        assert not result.ok
        assert "perp-carry" in result.reason


# ---------------------------------------------------------------------------
# Live reconciliation (DoD item 3 — break halts ALL capital moves)
# ---------------------------------------------------------------------------

class TestReconciliation:
    def test_pass_within_tolerance(self):
        queue = IntentQueue(":memory:")
        v = make_validator(queue=queue)
        assert v._check_reconciliation(move()).ok
        assert queue.halt_active(HALT_SCOPE_CAPITAL) is False

    def test_break_flags_halt_capital_without_recording_here(self):
        # The check FLAGS a confirmed break (halt_capital) but does NOT record
        # the halt itself — the broker records it in its control flow so a
        # record_halt failure is escalated, not swallowed (Codex R2 finding #4).
        queue = IntentQueue(":memory:")
        v = make_validator(
            queue=queue,
            sources=healthy_sources(
                venue_balances=lambda: {"kalshi": 2500.0, "polymarket": 2000.0}),
        )
        result = v._check_reconciliation(move())
        assert not result.ok
        assert "reconciliation break" in result.reason
        assert result.halt_capital is True
        # Recording is the broker's job now — the validator does not do it.
        assert queue.halt_active(HALT_SCOPE_CAPITAL) is False

    def test_fail_closed_on_nan_balance_without_halt(self):
        # A NaN balance is unverifiable → fail closed; only a CONFIRMED break
        # records the capital-moves halt.
        queue = IntentQueue(":memory:")
        v = make_validator(queue=queue, sources=healthy_sources(
            venue_balances=lambda: {"kalshi": float("nan"), "polymarket": 2000.0}))
        results = v.validate(move())
        recon = next(r for r in results if r.name == "reconciliation")
        assert not recon.ok
        assert "non-finite" in recon.reason
        assert queue.halt_active(HALT_SCOPE_CAPITAL) is False

    def test_fail_closed_when_venue_balances_unreadable(self):
        def boom():
            raise TimeoutError("venue API timeout")
        v = make_validator(sources=healthy_sources(venue_balances=boom))
        results = v.validate(move())
        recon = next(r for r in results if r.name == "reconciliation")
        assert not recon.ok
        assert "fail-closed" in recon.reason

    def test_fail_closed_on_empty_balances(self):
        # Empty ledger/venue maps are NOT a pass — a capital move with no live
        # balance evidence must fail closed (Codex finding #3).
        v = make_validator(sources=healthy_sources(
            ledger_balances=lambda: {}, venue_balances=lambda: {}))
        result = v._check_reconciliation(move())
        assert not result.ok
        assert "no live balance evidence" in result.reason

    def test_break_reason_handles_numeric_string_balance(self):
        # A numeric-string balance must not crash the ',.2f' reason formatting;
        # the reason is built from the _finite-normalized floats (CR CLI #4).
        v = make_validator(sources=healthy_sources(
            ledger_balances=lambda: {"kalshi": "3000", "polymarket": 2000.0},
            venue_balances=lambda: {"kalshi": 1.0, "polymarket": 2000.0}))
        result = v._check_reconciliation(move())
        assert not result.ok
        assert result.halt_capital is True
        assert "$3,000.00" in result.reason and "$1.00" in result.reason

    def test_fail_when_move_venue_absent_from_balances(self):
        # The move's own venues must appear on both sides; an absent venue means
        # there is no reconciliation evidence for the funds being moved.
        v = make_validator(sources=healthy_sources(
            ledger_balances=lambda: {"kalshi": 3000.0},
            venue_balances=lambda: {"kalshi": 3000.0}))
        result = v._check_reconciliation(move())  # move touches polymarket too
        assert not result.ok
        assert "no reconciliation evidence" in result.reason


# ---------------------------------------------------------------------------
# Capital-moves halt gate
# ---------------------------------------------------------------------------

class TestCapitalHalt:
    def test_pass_when_no_halt(self):
        assert make_validator()._check_capital_halt(move()).ok

    def test_fail_when_capital_halted(self):
        queue = IntentQueue(":memory:")
        queue.record_halt(HALT_SCOPE_CAPITAL, "earlier recon break")
        assert not make_validator(queue=queue)._check_capital_halt(move()).ok

    def test_fail_when_all_scope_halted(self):
        queue = IntentQueue(":memory:")
        queue.record_halt(HALT_SCOPE_ALL, "operator stop")
        assert not make_validator(queue=queue)._check_capital_halt(move()).ok


# ---------------------------------------------------------------------------
# Freshness
# ---------------------------------------------------------------------------

class TestFreshness:
    def test_pass_when_fresh(self):
        assert make_validator()._check_freshness(flip_enable()).ok

    def test_fail_stale_input(self):
        v = make_validator(sources=healthy_sources(
            input_ages_seconds=lambda: {"prices": 9999.0}))
        result = v._check_freshness(flip_enable())
        assert not result.ok
        assert "stale" in result.reason

    def test_fail_dead_heartbeat(self):
        v = make_validator(sources=healthy_sources(
            heartbeat_ages_seconds=lambda: {"ws_feed": 100000.0}))
        assert not v._check_freshness(flip_enable()).ok

    def test_fail_closed_when_nothing_reported(self):
        v = make_validator(sources=healthy_sources(input_ages_seconds=lambda: {}))
        result = v._check_freshness(flip_enable())
        assert not result.ok
        assert "cannot verify" in result.reason

    def test_fail_closed_on_nan_age(self):
        # NaN > ttl is always False — must not pass as "fresh".
        v = make_validator(sources=healthy_sources(
            input_ages_seconds=lambda: {"prices": float("nan")}))
        result = v._check_freshness(flip_enable())
        assert not result.ok
        assert "non-finite" in result.reason

    def test_fail_closed_on_boolean_age(self):
        # bool is an int subclass — True must not pass as a 1-second age.
        v = make_validator(sources=healthy_sources(
            input_ages_seconds=lambda: {"prices": True}))
        result = v._check_freshness(flip_enable())
        assert not result.ok
        assert "non-finite" in result.reason

    def test_stale_input_not_masked_by_fresh_heartbeat_same_name(self):
        # A merged {**inputs, **heartbeats} would let a fresh heartbeat overwrite
        # a stale input of the same name; both must be checked independently.
        v = make_validator(sources=healthy_sources(
            input_ages_seconds=lambda: {"prices": 9999.0},
            heartbeat_ages_seconds=lambda: {"prices": 5.0}))
        result = v._check_freshness(flip_enable())
        assert not result.ok
        assert "stale" in result.reason

    def test_non_numeric_age_is_a_targeted_freshness_failure(self):
        # A string age must produce an explicit freshness failure, not the
        # generic "live source unreadable" from the fail-closed wrapper.
        v = make_validator(sources=healthy_sources(
            heartbeat_ages_seconds=lambda: {"ws_feed": "yesterday"}))
        results = v.validate(flip_enable())
        fresh = next(r for r in results if r.name == "freshness")
        assert not fresh.ok
        assert "non-finite" in fresh.reason
        assert "unreadable" not in fresh.reason


# ---------------------------------------------------------------------------
# Cooldown
# ---------------------------------------------------------------------------

class TestCooldown:
    def test_pass_outside_window(self):
        assert make_validator()._check_cooldown(flip_enable()).ok

    def test_fail_within_window(self):
        v = make_validator(sources=healthy_sources(
            seconds_since_last_flip=lambda: 60.0))
        result = v._check_cooldown(flip_enable())
        assert not result.ok
        assert "cooldown" in result.reason

    def test_fail_closed_on_nan_elapsed(self):
        # NaN < cooldown is always False (fail-open, no violation); a non-finite
        # cooldown source must fail closed instead (Codex finding #2).
        v = make_validator(sources=healthy_sources(
            seconds_since_last_flip=lambda: float("nan")))
        results = v.validate(flip_enable())
        cooldown = next(r for r in results if r.name == "cooldown")
        assert not cooldown.ok
        assert "non-finite" in cooldown.reason


# ---------------------------------------------------------------------------
# Kill-switch dry-run (DoD item 6 — BEFORE the first order)
# ---------------------------------------------------------------------------

class TestKillSwitchDryRun:
    def test_pass_when_dry_run_halts(self):
        assert make_validator()._check_kill_switch_dry_run(flip_enable()).ok

    def test_fail_when_dry_run_fails(self):
        v = make_validator(sources=healthy_sources(kill_switch_dry_run=lambda: False))
        result = v._check_kill_switch_dry_run(flip_enable())
        assert not result.ok
        assert "no order may be placed" in result.reason

    def test_fail_on_nonbool_truthy_dry_run(self):
        # A broken source returning the string "false" (truthy) must NOT read as
        # a passing dry-run — strict True only (Codex finding #1).
        v = make_validator(sources=healthy_sources(kill_switch_dry_run=lambda: "false"))
        result = v._check_kill_switch_dry_run(flip_enable())
        assert not result.ok
        assert "did not return True" in result.reason

    def test_dry_run_runs_for_enable_not_disable(self):
        v = make_validator()
        enable_names = [r.name for r in v.validate(flip_enable())]
        disable_names = [r.name for r in v.validate(flip_disable())]
        assert "kill_switch_dry_run" in enable_names
        assert "kill_switch_dry_run" not in disable_names


# ---------------------------------------------------------------------------
# Micro-entry
# ---------------------------------------------------------------------------

class TestMicroEntry:
    def test_pass_valid_config(self):
        assert make_validator()._check_micro_entry(flip_enable()).ok

    def test_fail_invalid_config(self):
        policy = make_policy()
        policy.micro_entry["max_first_order_usd"] = 0
        assert not make_validator(policy=policy)._check_micro_entry(flip_enable()).ok


# ---------------------------------------------------------------------------
# validate() composition
# ---------------------------------------------------------------------------

class TestValidateComposition:
    def test_all_pass_for_healthy_flip(self):
        v = make_validator()
        results = v.validate(flip_enable())
        assert v.passed(results)

    def test_move_runs_capital_rules_flip_does_not(self):
        v = make_validator()
        move_names = {r.name for r in v.validate(move())}
        flip_names = {r.name for r in v.validate(flip_enable())}
        assert {"caps", "reconciliation", "capital_halt"} <= move_names
        assert not {"caps", "reconciliation"} & flip_names

    def test_failures_reported_together(self):
        v = make_validator(sources=healthy_sources(
            kill_switch_dry_run=lambda: False,
            seconds_since_last_flip=lambda: 1.0,
        ))
        results = v.validate(flip_enable())
        assert not v.passed(results)
        failures = v.failures(results)
        assert "cooldown" in failures and "kill_switch_dry_run" in failures
