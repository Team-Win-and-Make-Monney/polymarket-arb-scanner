"""Policy config loading + gate-hash registry for the policy broker.

The policy config MUST live outside this repo (docs/plans/09-policy-broker.md):
the loop can auto-merge this repo, so a merge must never be able to widen the
loop's own money authority. The loader fail-closes on any missing, unreadable,
malformed, or in-repo config.
"""

import hashlib
import json
import logging
import math
import os
from pathlib import Path

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

REPO_ROOT = Path(__file__).resolve().parent.parent

DEFAULT_POLICY_PATH = (
    Path.home() / "Financial Markets with AI" / "policy" / "broker-policy.json"
)

REQUIRED_KEYS = frozenset([
    "tranche",
    "principal_cap_usd",
    "per_market_cap_usd",
    "venue_allowlist",
    "sportsbook_venues",
    "gate_hashes",
    "kill_state",
    "cooldown_seconds",
    "freshness_ttl_seconds",
    "recon_tolerance_usd",
    "micro_entry",
])

REQUIRED_MICRO_ENTRY_KEYS = frozenset([
    "max_first_order_usd",
    "first_n_fills",
    "max_fill_deviation_pct",
])


class PolicyError(ValueError):
    """Policy config missing, malformed, or unsafe. Always fail-closed."""


# ---------------------------------------------------------------------------
# Gate-config hashing
# ---------------------------------------------------------------------------

def compute_gate_hash(gate_config: dict) -> str:
    """SHA-256 of the canonical-JSON form of a gate's live config values.

    Key order never affects the hash, so the registered hash only changes when
    a gate value actually changes (e.g. a merged edit to a threshold).
    Strict JSON only: a NaN/Infinity gate value has no canonical form, so it
    raises (fail-closed) instead of hashing a Python-specific extension.
    """
    try:
        canonical = json.dumps(gate_config, sort_keys=True, separators=(",", ":"),
                               allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise PolicyError(
            f"gate config is not canonical-JSON-hashable: {exc}"
        ) from exc
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Policy config
# ---------------------------------------------------------------------------

class PolicyConfig:
    """Validated, read-only view of the out-of-band policy file."""

    def __init__(self, data: dict, source_path: Path):
        self.source_path = source_path
        self.tranche: str = data["tranche"]
        self.principal_cap_usd: float = float(data["principal_cap_usd"])
        self.per_market_cap_usd: float = float(data["per_market_cap_usd"])
        # strip().lower() canonicalization everywhere a venue/lane is compared —
        # otherwise "draftkings " (trailing space) would miss the sportsbook set
        # while still matching a whitespace-padded allowlist entry.
        self.venue_allowlist: frozenset[str] = frozenset(
            str(v).strip().lower() for v in data["venue_allowlist"]
        )
        self.sportsbook_venues: frozenset[str] = frozenset(
            str(v).strip().lower() for v in data["sportsbook_venues"]
        )
        self.gate_hashes: dict[str, str] = dict(data["gate_hashes"])
        kill_state = data["kill_state"]
        self.kill_global: bool = bool(kill_state.get("global", False))
        self.kill_lanes: dict[str, bool] = {
            str(k).strip().lower(): bool(v) for k, v in kill_state.get("lanes", {}).items()
        }
        self.cooldown_seconds: float = float(data["cooldown_seconds"])
        self.freshness_ttl_seconds: float = float(data["freshness_ttl_seconds"])
        self.recon_tolerance_usd: float = float(data["recon_tolerance_usd"])
        self.micro_entry: dict = dict(data["micro_entry"])

    def lane_halted(self, lane: str) -> bool:
        return self.kill_lanes.get(lane.strip().lower(), False)

    def any_lane_halted(self) -> bool:
        return any(self.kill_lanes.values())


def _require_finite(value, key: str) -> float:
    """JSON accepts NaN/Infinity; a NaN cap would make every comparison
    fail-open (NaN > ceiling is always False). Reject non-finite numbers,
    and booleans (bool is an int subclass — float(True) == 1.0)."""
    if isinstance(value, bool):
        raise PolicyError(f"{key} must be a number, got {value!r}")
    try:
        num = float(value)
    except (TypeError, ValueError) as exc:
        raise PolicyError(f"{key} must be a number, got {value!r}") from exc
    if not math.isfinite(num):
        raise PolicyError(f"{key} must be finite, got {value!r}")
    return num


def _validate(data: dict, path: Path) -> None:
    missing = REQUIRED_KEYS - data.keys()
    if missing:
        raise PolicyError(
            f"policy config {path} missing required keys: {sorted(missing)}"
        )
    if not isinstance(data["venue_allowlist"], list) or not data["venue_allowlist"]:
        raise PolicyError("venue_allowlist must be a non-empty list")
    if not isinstance(data["sportsbook_venues"], list):
        raise PolicyError("sportsbook_venues must be a list")
    if not isinstance(data["gate_hashes"], dict) or not data["gate_hashes"]:
        raise PolicyError("gate_hashes must be a non-empty dict (pre-registered)")
    kill_state = data["kill_state"]
    if not isinstance(kill_state, dict):
        raise PolicyError("kill_state must be a dict")
    # Strict bools only: bool("false") is True — coercion would un-kill a lane.
    if not isinstance(kill_state.get("global", False), bool):
        raise PolicyError("kill_state.global must be a boolean")
    lanes = kill_state.get("lanes", {})
    if not isinstance(lanes, dict):
        raise PolicyError("kill_state.lanes must be a dict")
    for lane, halted in lanes.items():
        if not isinstance(halted, bool):
            raise PolicyError(f"kill_state.lanes[{lane!r}] must be a boolean")
    micro = data["micro_entry"]
    if not isinstance(micro, dict):
        raise PolicyError("micro_entry must be a dict")
    micro_missing = REQUIRED_MICRO_ENTRY_KEYS - micro.keys()
    if micro_missing:
        raise PolicyError(f"micro_entry missing keys: {sorted(micro_missing)}")
    for key in ("principal_cap_usd", "per_market_cap_usd"):
        if _require_finite(data[key], key) <= 0:
            raise PolicyError(f"{key} must be > 0")
    for key in ("cooldown_seconds", "freshness_ttl_seconds", "recon_tolerance_usd"):
        if _require_finite(data[key], key) < 0:
            raise PolicyError(f"{key} must be >= 0")
    if _require_finite(micro["max_first_order_usd"],
                       "micro_entry.max_first_order_usd") <= 0:
        raise PolicyError("micro_entry.max_first_order_usd must be > 0")
    fills = _require_finite(micro["first_n_fills"], "micro_entry.first_n_fills")
    if fills != int(fills):
        raise PolicyError(f"micro_entry.first_n_fills must be an integer, got {fills!r}")
    if int(fills) < 1:
        raise PolicyError("micro_entry.first_n_fills must be >= 1")
    if _require_finite(micro["max_fill_deviation_pct"],
                       "micro_entry.max_fill_deviation_pct") <= 0:
        raise PolicyError("micro_entry.max_fill_deviation_pct must be > 0")


def load_policy(path: str | os.PathLike | None = None) -> PolicyConfig:
    """Load and validate the policy config. Fail-closed on every error path.

    Refuses any config located inside this repo: a merge to the loop-mergeable
    repo must never be able to alter (or smuggle in) the broker's policy.
    """
    if path is None:
        path = os.getenv("BROKER_POLICY_PATH", str(DEFAULT_POLICY_PATH))
    given = Path(path).expanduser()
    resolved = given.resolve()
    # BOTH the path AS GIVEN (the symlink node, with `..` collapsed but symlinks
    # NOT followed) AND its resolved target must live outside the repo. Checking
    # only the resolved target would let an in-repo symlink point outside — then
    # a merge to the loop-mergeable repo could repoint it and control the policy.
    literal = Path(os.path.normpath(
        str(given if given.is_absolute() else Path.cwd() / given)))
    for candidate, what in ((literal, "path"), (resolved, "resolved target")):
        try:
            inside_repo = candidate.is_relative_to(REPO_ROOT)
        except ValueError:
            inside_repo = False
        if inside_repo:
            raise PolicyError(
                f"policy config {what} {candidate} is INSIDE the loop-mergeable repo "
                f"({REPO_ROOT}) — refusing (config isolation is non-negotiable)"
            )
    try:
        raw = resolved.read_text(encoding="utf-8")
    except OSError as exc:
        raise PolicyError(f"policy config unreadable at {resolved}: {exc}") from exc
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise PolicyError(f"policy config {resolved} is not valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise PolicyError(f"policy config {resolved} must be a JSON object")

    _validate(data, resolved)
    logger.info("Loaded broker policy from %s (tranche=%s)", resolved, data["tranche"])
    return PolicyConfig(data, resolved)
