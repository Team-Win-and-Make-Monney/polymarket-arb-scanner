"""Shared factories for the policy-broker test suite (tests/test_broker_*)."""

import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from broker.policy import PolicyConfig, compute_gate_hash
from broker.validator import LiveSources

# The "live" gate config the pre-registered hash is computed from. A test that
# wants a mismatch (simulated merged threshold edit) returns different values.
GATE_CONFIG = {"min_net_roi": 0.02, "max_trade_size": 10.0}


def policy_data(**overrides) -> dict:
    data = {
        "tranche": "T1",
        "principal_cap_usd": 8000.0,
        "per_market_cap_usd": 300.0,
        "venue_allowlist": ["kalshi", "polymarket", "gemini"],
        "sportsbook_venues": ["draftkings", "fanduel"],
        "gate_hashes": {"pre_trade": compute_gate_hash(GATE_CONFIG)},
        "kill_state": {"global": False, "lanes": {}},
        "cooldown_seconds": 3600.0,
        "freshness_ttl_seconds": 300.0,
        "recon_tolerance_usd": 1.0,
        "micro_entry": {
            "max_first_order_usd": 10.0,
            "first_n_fills": 5,
            "max_fill_deviation_pct": 0.05,
        },
    }
    data.update(overrides)
    return data


def make_policy(**overrides) -> PolicyConfig:
    return PolicyConfig(policy_data(**overrides), Path("/outside/repo/policy.json"))


def healthy_sources(**overrides) -> LiveSources:
    defaults = dict(
        portfolio_value_usd=lambda: 5000.0,
        realized_pnl_usd=lambda: 200.0,
        ledger_balances=lambda: {"kalshi": 3000.0, "polymarket": 2000.0},
        venue_balances=lambda: {"kalshi": 3000.0, "polymarket": 2000.0},
        gate_config=lambda name: dict(GATE_CONFIG),
        input_ages_seconds=lambda: {"prices": 10.0, "balances": 20.0},
        heartbeat_ages_seconds=lambda: {"ws_feed": 5.0},
        seconds_since_last_flip=lambda: 10 ** 9,
        kill_switch_dry_run=lambda: True,
        market_book_depth_usd=lambda market: 5000.0,
    )
    defaults.update(overrides)
    return LiveSources(**defaults)
