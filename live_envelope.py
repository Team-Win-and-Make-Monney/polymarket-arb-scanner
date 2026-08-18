"""Fail-closed live-trading envelope.

Live Kalshi / Kraken / Coinbase / Phantom execution starts only when all five
fields are present in one operator envelope file. Agents do not invent limits.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

ALLOWED_VENUES = frozenset({
    "kalshi-d0",
    "kraken-live",
    "coinbase-agent",
    "phantom-mainnet",
})
FORBIDDEN_VENUES = frozenset({
    "polymarket-intl",
    "hyperliquid",
    "michigan-sports",
    "kraken-paper",
})
REQUIRED_FIELDS = (
    "venue",
    "pair",
    "max_notional_usd",
    "max_daily_loss_usd",
    "kill_switch",
)


class EnvelopeError(RuntimeError):
    """Live envelope missing, malformed, or out of policy."""


def envelope_path() -> Path:
    raw = os.getenv("LIVE_ENVELOPE_PATH", "artifacts/live-envelope.json")
    return Path(raw)


def _positive_number(name: str, value: object) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise EnvelopeError(f"envelope.{name} must be a number") from exc
    if number <= 0:
        raise EnvelopeError(f"envelope.{name} must be > 0, got {number}")
    return number


def load_envelope(path: Path | None = None) -> dict:
    """Load and validate the five-item live envelope. Never fills defaults."""
    target = path or envelope_path()
    if not target.is_file():
        raise EnvelopeError(
            f"Live envelope missing at {target}. Write all five fields "
            "(venue, pair, max_notional_usd, max_daily_loss_usd, kill_switch) "
            "from one operator message. Template: artifacts/live-envelope.example.json"
        )
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise EnvelopeError(f"Live envelope is not valid JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise EnvelopeError("Live envelope must be a JSON object")

    missing = [field for field in REQUIRED_FIELDS if not payload.get(field)]
    if missing:
        raise EnvelopeError(f"Live envelope missing fields: {', '.join(missing)}")

    venue = str(payload["venue"]).strip().lower()
    if venue in FORBIDDEN_VENUES:
        raise EnvelopeError(f"Venue {venue} is forbidden for live execution")
    if venue not in ALLOWED_VENUES:
        raise EnvelopeError(
            f"Venue {venue!r} is not an allowed live venue. "
            f"Allowed: {sorted(ALLOWED_VENUES)}"
        )

    pair = str(payload["pair"]).strip()
    if pair.lower() in {"any", "whatever looks good", "*"}:
        raise EnvelopeError("pair must be an exact ticker or series, not a wildcard")

    return {
        "venue": venue,
        "pair": pair,
        "max_notional_usd": _positive_number("max_notional_usd", payload["max_notional_usd"]),
        "max_daily_loss_usd": _positive_number("max_daily_loss_usd", payload["max_daily_loss_usd"]),
        "kill_switch": str(payload["kill_switch"]).strip(),
        "dry_run": False,
    }


def pair_allowed(ticker: str, pair: str) -> bool:
    """True when a Kalshi ticker is inside the operator-named pair/series."""
    t = ticker.strip().upper()
    p = pair.strip().upper()
    if not t or not p:
        return False
    if t == p:
        return True
    return t.startswith(p + "-")


def require_live_envelope() -> dict:
    """Import-time gate used when DRY_RUN is false."""
    return load_envelope()
