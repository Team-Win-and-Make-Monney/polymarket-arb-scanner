"""The deterministic validation rulebook (docs/plans/09-policy-broker.md §4).

Every rule re-validates against LIVE sources (injected callables — never
stale state). Any exception raised by a live source fails that check closed.
ALL applicable checks must pass or the intent is rejected.
"""

import logging
import math
from dataclasses import dataclass
from typing import Callable

from .policy import PolicyConfig, compute_gate_hash
from .queue import HALT_SCOPE_CAPITAL, Intent, IntentQueue

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Live sources — the broker never trusts a cached number
# ---------------------------------------------------------------------------


@dataclass
class LiveSources:
    """Callables the broker uses to read live state at validation time."""

    portfolio_value_usd: Callable[[], float]
    realized_pnl_usd: Callable[[], float]
    ledger_balances: Callable[[], dict[str, float]]
    venue_balances: Callable[[], dict[str, float]]
    gate_config: Callable[[str], dict]
    input_ages_seconds: Callable[[], dict[str, float]]
    heartbeat_ages_seconds: Callable[[], dict[str, float]]
    seconds_since_last_flip: Callable[[], float]
    kill_switch_dry_run: Callable[[], bool]
    # Live per-market book depth in USD, read by the broker ITSELF — never
    # trusted from the proposer's payload (the proposer is the very thing the
    # broker exists to constrain, so a gamed intent could claim any depth).
    market_book_depth_usd: Callable[[str], float]


@dataclass(frozen=True)
class CheckResult:
    name: str
    ok: bool
    reason: str = ""
    # A confirmed reconciliation break sets this so the BROKER records the
    # ALL-capital halt in its control flow — where a record_halt failure is
    # escalated, not swallowed by this check's fail-closed wrapper.
    halt_capital: bool = False


# ---------------------------------------------------------------------------
# Validator
# ---------------------------------------------------------------------------


class BrokerValidator:
    """Runs the fail-closed rulebook for one intent."""

    def __init__(self, policy: PolicyConfig, sources: LiveSources, queue: IntentQueue):
        self.policy = policy
        self.sources = sources
        self.queue = queue

    # -- public entry ---------------------------------------------------------

    def validate(self, intent: Intent) -> list[CheckResult]:
        checks: list[Callable[[Intent], CheckResult]] = [
            self._check_allowlist,
            self._check_gate_hashes,
            self._check_kill_state,
            self._check_freshness,
        ]
        if intent.intent_type == "move_capital":
            checks += [self._check_capital_halt, self._check_caps,
                       self._check_reconciliation]
        if intent.intent_type == "flip_lane":
            checks.append(self._check_cooldown)
            if intent.payload.get("action") == "enable":
                checks += [self._check_kill_switch_dry_run, self._check_micro_entry]
        return [self._run(check, intent) for check in checks]

    @staticmethod
    def passed(results: list[CheckResult]) -> bool:
        return all(r.ok for r in results)

    @staticmethod
    def failures(results: list[CheckResult]) -> str:
        return "; ".join(f"{r.name}: {r.reason}" for r in results if not r.ok)

    def reconcile_all(self) -> CheckResult:
        """Reconcile every reported venue without trusting an intent payload."""
        try:
            ledger = self.sources.ledger_balances()
            live = self.sources.venue_balances()
            if not ledger or not live:
                return CheckResult(
                    "reconciliation", False,
                    "no live balance evidence (ledger or venue balances empty)",
                )
            tolerance = self.policy.recon_tolerance_usd
            for venue in sorted(set(ledger) | set(live)):
                ledger_bal = self._finite(
                    ledger.get(venue, 0.0), f"ledger balance '{venue}'"
                )
                live_bal = self._finite(
                    live.get(venue, 0.0), f"live balance '{venue}'"
                )
                diff = abs(ledger_bal - live_bal)
                if diff > tolerance:
                    return CheckResult(
                        "reconciliation", False,
                        f"reconciliation break on '{venue}': ledger "
                        f"${ledger_bal:,.2f} vs live ${live_bal:,.2f} "
                        f"(diff ${diff:,.2f} > ${tolerance:,.2f})",
                        halt_capital=True,
                    )
            return CheckResult("reconciliation", True)
        except Exception as exc:
            logger.error("Reconciliation preflight failed closed: %s", exc)
            return CheckResult(
                "reconciliation", False,
                f"fail-closed: live source unreadable ({exc})",
            )

    # -- fail-closed wrapper ----------------------------------------------------

    def _run(self, check: Callable[[Intent], CheckResult], intent: Intent) -> CheckResult:
        name = check.__name__.removeprefix("_check_")
        try:
            return check(intent)
        except Exception as exc:  # any live-source error ⇒ fail closed
            logger.error("Check %s fail-closed: %s", name, exc)
            return CheckResult(name, False, f"fail-closed: live source unreadable ({exc})")

    # -- individual rules ---------------------------------------------------------

    @staticmethod
    def _finite(value, what: str) -> float:
        """Live numbers must be finite — NaN makes every threshold comparison
        False (fail-open). Booleans are rejected too: bool is an int subclass,
        so True/False would silently coerce a broken live source to 1.0/0.0.
        Raising here lands in the _run fail-closed path."""
        if isinstance(value, bool):
            raise ValueError(f"{what} must be a number, got {value!r}")
        num = float(value)
        if not math.isfinite(num):
            raise ValueError(f"{what} is non-finite ({value!r})")
        return num

    def _intent_venues(self, intent: Intent) -> list[str]:
        p = intent.payload
        keys = ("venue", "from_venue", "to_venue")
        # strip().lower() matches PolicyConfig canonicalization — a padded venue
        # ("draftkings ") must not dodge the sportsbook/allowlist comparison.
        return [str(p[k]).strip().lower() for k in keys if p.get(k)]

    def _check_allowlist(self, intent: Intent) -> CheckResult:
        venues = self._intent_venues(intent)
        if not venues:
            return CheckResult("allowlist", False, "intent names no venue")
        for venue in venues:
            if venue in self.policy.sportsbook_venues:
                return CheckResult(
                    "allowlist", False,
                    f"'{venue}' is a sportsbook — auto-placement is never permitted",
                )
            if venue not in self.policy.venue_allowlist:
                return CheckResult("allowlist", False, f"'{venue}' not on venue allowlist")
        return CheckResult("allowlist", True)

    def _check_gate_hashes(self, intent: Intent) -> CheckResult:
        for gate, registered in self.policy.gate_hashes.items():
            live = compute_gate_hash(self.sources.gate_config(gate))
            if live != registered:
                return CheckResult(
                    "gate_hashes", False,
                    f"gate '{gate}' config hash mismatch (registered {registered[:12]}…, "
                    f"live {live[:12]}…) — possible merged threshold edit",
                )
        return CheckResult("gate_hashes", True)

    def _check_kill_state(self, intent: Intent) -> CheckResult:
        if self.policy.kill_global:
            return CheckResult("kill_state", False, "global kill switch is set")
        if self.policy.any_lane_halted():
            halted = sorted(
                lane for lane, is_halted in self.policy.kill_lanes.items() if is_halted
            )
            return CheckResult("kill_state", False, f"lanes in kill halt: {halted}")
        return CheckResult("kill_state", True)

    def _check_freshness(self, intent: Intent) -> CheckResult:
        ttl = self.policy.freshness_ttl_seconds
        inputs = self.sources.input_ages_seconds()
        heartbeats = self.sources.heartbeat_ages_seconds()
        if not inputs or not heartbeats:
            return CheckResult("freshness", False,
                               "no gate inputs/heartbeats reported — cannot verify freshness")
        # Iterate the two maps independently — a merged dict would let a fresh
        # heartbeat silently overwrite (and mask) a stale input of the same name.
        for name, age in list(inputs.items()) + list(heartbeats.items()):
            # NaN would make `age > ttl` False, and a bool/str age must not
            # coerce — parse through _finite so the failure is targeted and
            # explicit rather than a generic "live source unreadable".
            try:
                age_num = self._finite(age, f"'{name}' age")
            except (TypeError, ValueError):
                return CheckResult("freshness", False,
                                   f"'{name}' reported a non-finite age ({age!r})")
            if age_num > ttl:
                return CheckResult("freshness", False,
                                   f"'{name}' is stale ({age_num:.0f}s > TTL {ttl:.0f}s)")
        return CheckResult("freshness", True)

    def _check_capital_halt(self, intent: Intent) -> CheckResult:
        if self.queue.halt_active(HALT_SCOPE_CAPITAL):
            return CheckResult("capital_halt", False,
                               "capital moves are halted (operator clear required)")
        return CheckResult("capital_halt", True)

    def _check_caps(self, intent: Intent) -> CheckResult:
        p = intent.payload
        amount = self._finite(p.get("amount_usd", 0), "amount_usd")
        if amount <= 0:
            return CheckResult("caps", False, "amount_usd must be > 0")
        # Case-insensitive: "Principal"/"PRINCIPAL" must not slip past the gate.
        if str(p.get("source", "")).strip().lower() == "principal":
            return CheckResult("caps", False,
                               "new principal is operator-gated — broker only moves earned gains")
        ceiling = self.policy.principal_cap_usd + self._finite(
            self.sources.realized_pnl_usd(), "realized_pnl_usd")
        portfolio = self._finite(
            self.sources.portfolio_value_usd(), "portfolio_value_usd")
        if portfolio > ceiling:
            return CheckResult(
                "caps", False,
                f"portfolio ${portfolio:,.2f} exceeds working ceiling ${ceiling:,.2f} "
                f"({self.policy.tranche} principal + realized P&L)",
            )
        # POST-ACTION ceiling: current portfolio plus this move must stay within
        # the working ceiling — not each independently (spec: "post-action
        # portfolio ≤ working ceiling").
        if portfolio + amount > ceiling:
            return CheckResult(
                "caps", False,
                f"post-action portfolio ${portfolio + amount:,.2f} would exceed working "
                f"ceiling ${ceiling:,.2f}",
            )
        # Every capital move is market-scoped: the per-market cap and the
        # book-depth cap CANNOT be verified without a named market, so a move
        # that omits one is rejected — never silently exempt from the $/market
        # cap by leaving 'market' out.
        market = p.get("market")
        if not market:
            return CheckResult(
                "caps", False,
                "move_capital must name a 'market' — the per-market cap and book-depth "
                "cap cannot be verified without it",
            )
        if amount > self.policy.per_market_cap_usd:
            return CheckResult(
                "caps", False,
                f"amount ${amount:,.2f} exceeds per-market cap "
                f"${self.policy.per_market_cap_usd:,.2f} for '{market}'",
            )
        # Book depth is read LIVE by the broker, never trusted from the
        # proposer's payload — a gamed intent could otherwise claim any depth.
        depth = self._finite(
            self.sources.market_book_depth_usd(str(market)),
            f"live book depth for '{market}'",
        )
        if depth <= 0:
            return CheckResult("caps", False,
                               f"live book depth for '{market}' is ${depth:,.2f} — cannot size a move")
        if amount > depth:
            return CheckResult(
                "caps", False,
                f"amount ${amount:,.2f} exceeds live book depth ${depth:,.2f} for '{market}'",
            )
        return CheckResult("caps", True)

    def _check_reconciliation(self, intent: Intent) -> CheckResult:
        ledger = self.sources.ledger_balances()
        live = self.sources.venue_balances()
        # Empty balance maps are NOT a pass — a capital move needs live evidence
        # on both sides. Fail closed rather than reconcile nothing against nothing.
        if not ledger or not live:
            return CheckResult(
                "reconciliation", False,
                "no live balance evidence (ledger or venue balances empty) — "
                "cannot reconcile a capital move",
            )
        # The move's own venues MUST appear on both sides; an absent venue means
        # there is no reconciliation evidence for the funds being moved.
        for venue in self._intent_venues(intent):
            if venue not in ledger or venue not in live:
                return CheckResult(
                    "reconciliation", False,
                    f"venue '{venue}' missing from ledger or live balances — "
                    "no reconciliation evidence for this move",
                )
        tolerance = self.policy.recon_tolerance_usd
        for venue in sorted(set(ledger) | set(live)):
            ledger_bal = self._finite(ledger.get(venue, 0.0), f"ledger balance '{venue}'")
            live_bal = self._finite(live.get(venue, 0.0), f"live balance '{venue}'")
            diff = abs(ledger_bal - live_bal)
            if diff > tolerance:
                reason = (
                    f"reconciliation break on '{venue}': ledger "
                    f"${ledger_bal:,.2f} vs live ${live_bal:,.2f} "
                    f"(diff ${diff:,.2f} > ${tolerance:,.2f})"
                )
                # Flag the confirmed break so the broker records the ALL-capital
                # halt in its control flow (a record_halt failure there is
                # escalated, never silently swallowed by the _run wrapper).
                return CheckResult("reconciliation", False, reason, halt_capital=True)
        return CheckResult("reconciliation", True)

    def _check_cooldown(self, intent: Intent) -> CheckResult:
        # NaN would make `elapsed < cooldown` False (fail-open, no violation) —
        # route through _finite so a non-finite live source fails closed.
        elapsed = self._finite(
            self.sources.seconds_since_last_flip(), "seconds_since_last_flip")
        if elapsed < self.policy.cooldown_seconds:
            return CheckResult(
                "cooldown", False,
                f"last lane flip {elapsed:.0f}s ago < cooldown "
                f"{self.policy.cooldown_seconds:.0f}s",
            )
        return CheckResult("cooldown", True)

    def _check_kill_switch_dry_run(self, intent: Intent) -> CheckResult:
        # Strict True only: a non-bool truthy value (e.g. the string "false" or
        # any object) from a broken live source must NOT read as a passing
        # dry-run and let an order be placed.
        if self.sources.kill_switch_dry_run() is not True:
            return CheckResult("kill_switch_dry_run", False,
                               "kill-switch dry-run did not return True — no order may be placed")
        return CheckResult("kill_switch_dry_run", True)

    def _check_micro_entry(self, intent: Intent) -> CheckResult:
        micro = self.policy.micro_entry
        try:
            if float(micro["max_first_order_usd"]) <= 0:
                return CheckResult("micro_entry", False, "max_first_order_usd must be > 0")
            if int(micro["first_n_fills"]) < 1:
                return CheckResult("micro_entry", False, "first_n_fills must be >= 1")
            if float(micro["max_fill_deviation_pct"]) <= 0:
                return CheckResult("micro_entry", False, "max_fill_deviation_pct must be > 0")
        except (KeyError, TypeError, ValueError) as exc:
            return CheckResult("micro_entry", False, f"invalid micro-entry config: {exc}")
        return CheckResult("micro_entry", True)
