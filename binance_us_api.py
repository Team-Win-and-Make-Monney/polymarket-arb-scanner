"""Opt-in Binance.US balance reporting; this module exposes only two GET routes.

This account reader is independent of scanner configuration and order execution.
It does not load dotenv files or infer promotion eligibility from a balance.
"""

import argparse
from datetime import datetime, timezone
from decimal import Decimal
import getpass
import hashlib
import hmac
import json
import logging
import os
import re
import subprocess
import sys
from urllib.parse import urlencode

import requests

logger = logging.getLogger(__name__)

_BASE_URL = "https://api.binance.us"
_READ_PATHS = frozenset({"/api/v3/time", "/api/v3/account"})
_KEYCHAIN_SERVICES = ("codex-binance-us-api-key", "codex-binance-us-secret-key")
_ENV_NAMES = ("BINANCE_US_API_KEY", "BINANCE_US_API_SECRET")


class BinanceUSReadError(RuntimeError):
    """A sanitized failure that does not include authentication material."""


def load_auth(source: str) -> tuple[str, str]:
    """Load a complete key/secret pair from one explicitly selected source.

    Args:
        source: Either ``keychain`` (macOS login Keychain) or ``env``.

    Returns:
        API key and matching HMAC secret, held only in memory.
    """
    if source == "env":
        values = tuple(os.environ.get(name, "") for name in _ENV_NAMES)
    elif source == "keychain":
        values = []
        for service in _KEYCHAIN_SERVICES:
            try:
                result = subprocess.run(
                    ["/usr/bin/security", "find-generic-password", "-a", getpass.getuser(), "-s", service, "-w"],
                    capture_output=True, timeout=15,
                )
            except (OSError, subprocess.TimeoutExpired):
                raise BinanceUSReadError("Keychain lookup failed; no account request sent.") from None
            if result.returncode != 0:
                raise BinanceUSReadError("Keychain key/secret pair is incomplete or unavailable.")
            try:
                values.append(result.stdout.rstrip(b"\r\n").decode("ascii"))
            except UnicodeDecodeError:
                raise BinanceUSReadError("Invalid Keychain authentication material.") from None
    else:
        raise BinanceUSReadError("Unknown authentication source.")
    if not all(isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9]{64}", value) for value in values):
        raise BinanceUSReadError("A complete 64-character Binance.US HMAC key/secret pair is required.")
    return values[0], values[1]


def _get_json(path: str, *, params: str = "", api_key: str = "") -> dict:
    """Read an allowlisted endpoint without following redirects or retrying."""
    if path not in _READ_PATHS:
        raise BinanceUSReadError("Endpoint is outside the read-only allowlist.")
    headers = {"X-MBX-APIKEY": api_key} if api_key else {}
    try:
        response = requests.get(
            _BASE_URL + path, params=params, headers=headers,
            timeout=(5, 20), allow_redirects=False,
        )
    except requests.RequestException:
        # requests exceptions can contain the signed URL; never interpolate them.
        raise BinanceUSReadError("Binance.US read failed at the network layer.") from None
    try:
        if 300 <= response.status_code < 400:
            raise BinanceUSReadError("Binance.US redirected the request; redirect refused.")
        try:
            data = response.json()
        except ValueError:
            raise BinanceUSReadError("Binance.US returned an unreadable response.") from None
        code = data.get("code") if isinstance(data, dict) else None
        if response.status_code != 200 or (type(code) is int and code < 0):
            suffix = f", API code {code}" if type(code) is int else ""
            raise BinanceUSReadError(f"Binance.US rejected the read (HTTP {response.status_code}{suffix}).")
        if not isinstance(data, dict):
            raise BinanceUSReadError("Binance.US returned an unexpected response shape.")
        return data
    finally:
        response.close()


def server_time() -> int:
    """Return exchange time in milliseconds without sending authentication."""
    value = _get_json("/api/v3/time").get("serverTime")
    if type(value) is not int or value <= 0:
        raise BinanceUSReadError("Binance.US server time is missing or invalid.")
    return value


def _balances(data: dict) -> list[dict]:
    """Validate decimal quantities and return nonzero assets without valuation."""
    rows = data.get("balances")
    if not isinstance(rows, list):
        raise BinanceUSReadError("Binance.US account response has no valid balance list.")
    result = []
    seen = set()
    for row in rows:
        if not isinstance(row, dict):
            raise BinanceUSReadError("Binance.US returned an invalid balance row.")
        asset = row.get("asset")
        amounts = [row.get("free"), row.get("locked")]
        if (not isinstance(asset, str) or not re.fullmatch(r"[A-Z0-9]{1,30}", asset)
                or asset in seen or not all(
                    isinstance(value, str) and re.fullmatch(r"[0-9]{1,32}(?:\.[0-9]{1,32})?", value)
                    for value in amounts
                )):
            raise BinanceUSReadError("Binance.US returned an invalid balance row.")
        seen.add(asset)
        free, locked = map(Decimal, amounts)
        if free or locked:
            result.append({"asset": asset, "free": amounts[0], "locked": amounts[1]})
    return result


def account_snapshot(api_key: str, api_secret: str) -> dict:
    """Fetch a signed balance snapshot without order, transfer or staking routes.

    Args:
        api_key: Binance.US exchange API key.
        api_secret: The matching HMAC secret.

    Returns:
        Nonzero balances and account flags. Flags are not API-key permissions
        or operator approval; the response does not establish bonus eligibility.
    """
    if not all(isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9]{64}", value)
               for value in (api_key, api_secret)):
        raise BinanceUSReadError("A complete Binance.US HMAC key/secret pair is required.")
    timestamp = server_time()
    query = urlencode({"timestamp": timestamp, "recvWindow": 5000})
    signature = hmac.new(api_secret.encode("ascii"), query.encode("ascii"), hashlib.sha256).hexdigest()
    data = _get_json("/api/v3/account", params=f"{query}&signature={signature}", api_key=api_key)
    return {
        "venue": "binance_us",
        "mode": "read_only",
        "observed_at": datetime.now(timezone.utc).isoformat(),
        "server_time_ms": timestamp,
        "balances": _balances(data),
        "account_flags": {
            name: data.get(name) if type(data.get(name)) is bool else None
            for name in ("canTrade", "canWithdraw", "canDeposit")
        },
        "promotion_eligibility": "not_available_from_account_endpoint",
    }


def main(argv: list[str] | None = None) -> int:
    """Print a read-only snapshot or an unauthenticated connectivity result."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", choices=("keychain", "env"), default="keychain")
    parser.add_argument("--public-check", action="store_true", help="Read server time without loading secrets")
    args = parser.parse_args(argv)
    try:
        if args.public_check:
            result = {"venue": "binance_us", "server_time_ms": server_time(), "authenticated": False}
        else:
            result = account_snapshot(*load_auth(args.source))
    except BinanceUSReadError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
