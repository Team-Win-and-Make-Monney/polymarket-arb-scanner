"""Pre-flight validation script for Railway deployment health.

Checks that the deployed scanner service is reachable, healthy,
and exposing expected endpoints (healthz, status, metrics).
"""

import argparse
import base64
import ipaddress
import os
import json
import socket
import sys
import urllib.error
import urllib.request
from urllib.parse import urlparse


def _validate_base_url(value: str) -> str:
    """Validate a dashboard URL and reject internal-network SSRF targets."""
    parsed = urlparse(value)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise ValueError("must be an http(s) URL with a hostname")
    hostname = parsed.hostname
    if hostname == "localhost":
        return value
    try:
        default_port = 443 if parsed.scheme == "https" else 80
        addresses = {item[4][0] for item in socket.getaddrinfo(hostname, parsed.port or default_port)}
    except socket.gaierror as exc:
        raise ValueError(f"hostname does not resolve: {exc}") from exc
    for address in addresses:
        ip = ipaddress.ip_address(address)
        if not ip.is_global:
            raise ValueError("must not resolve to a private, loopback, or link-local address")
    return value


def _make_auth_header(user: str | None, password: str | None) -> dict:
    """Build HTTP Basic Auth header if credentials provided."""
    if user and password:
        creds = base64.b64encode(f"{user}:{password}".encode()).decode()
        return {"Authorization": f"Basic {creds}"}
    return {}


# ---------------------------------------------------------------------------
# Color helpers (no deps)
# ---------------------------------------------------------------------------

def _green(text: str) -> str:
    return f"\033[92m{text}\033[0m"


def _red(text: str) -> str:
    return f"\033[91m{text}\033[0m"


def _bold(text: str) -> str:
    return f"\033[1m{text}\033[0m"


# ---------------------------------------------------------------------------
# Individual checks
# ---------------------------------------------------------------------------

def check_healthz(base_url: str, auth: dict | None = None) -> bool:
    """GET /healthz returns 200."""
    url = f"{base_url.rstrip('/')}/healthz"
    try:
        req = urllib.request.Request(url, method="GET", headers=auth or {})
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status == 200
    except (urllib.error.URLError, OSError):
        return False


def check_status(base_url: str, auth: dict | None = None) -> bool:
    """GET /status returns valid JSON with expected fields."""
    url = f"{base_url.rstrip('/')}/status"
    try:
        req = urllib.request.Request(url, method="GET", headers=auth or {})
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode())
            # Accept any valid JSON dict with scan-related fields
            return isinstance(data, dict) and ("uptime" in data or "scan_count" in data)
    except (urllib.error.URLError, OSError, json.JSONDecodeError):
        return False


def check_metrics(base_url: str, auth: dict | None = None) -> bool:
    """GET /metrics returns Prometheus text with key metric names."""
    url = f"{base_url.rstrip('/')}/metrics"
    try:
        req = urllib.request.Request(url, method="GET", headers=auth or {})
        with urllib.request.urlopen(req, timeout=10) as resp:
            body = resp.read().decode()
            return "trade" in body.lower() or "arb" in body.lower() or "execution" in body.lower()
    except (urllib.error.URLError, OSError):
        return False


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Pre-flight check for Railway deployment")
    parser.add_argument("--url", default="http://localhost:8080",
                        help="Base URL of the deployed scanner (default: http://localhost:8080)")
    parser.add_argument("--user", default=None, help="HTTP Basic Auth username")
    args = parser.parse_args()

    try:
        base_url = _validate_base_url(args.url)
    except ValueError as exc:
        parser.error(f"--url {exc}")
    auth = _make_auth_header(args.user, os.getenv("DASHBOARD_PASS"))
    print(_bold(f"\nPre-flight check: {base_url}\n"))

    checks = [
        ("Health endpoint (/healthz)", check_healthz),
        ("Status endpoint (/status)", check_status),
        ("Metrics endpoint (/metrics)", check_metrics),
    ]

    all_pass = True
    for name, check_fn in checks:
        ok = check_fn(base_url, auth)
        status = _green("PASS") if ok else _red("FAIL")
        print(f"  {status}  {name}")
        if not ok:
            all_pass = False

    print()
    if all_pass:
        print(_green("All checks passed."))
    else:
        print(_red("Some checks failed. Review above."))
        sys.exit(1)


if __name__ == "__main__":
    main()
