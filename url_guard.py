"""SSRF guard for env-var-derived request URLs (audit findings S01-S04).

Several integrations read their endpoint from an environment variable —
``POLYGON_RPC_URL`` (gas_monitor), ``GEMINI_BASE_URL`` (gemini_api),
``WEBHOOK_URL`` (notifier), ``GJO_API_URL`` / ``INFER_API_URL``
(superforecaster_api). An attacker who can set env vars (e.g. a compromised
Railway config) could redirect authenticated, HMAC-signed, or payload-bearing
requests to an internal host — a classic SSRF vector.

This module validates such a URL at client construction:

  - scheme must be http/https (no ``file://``, ``gopher://`` …),
  - no embedded credentials (``user:pass@host``),
  - the host must not be an internal address — a private / loopback /
    link-local / reserved / multicast / unspecified IP literal (including the
    cloud metadata IP ``169.254.169.254`` and decimal-encoded forms such as
    ``2130706433`` == ``127.0.0.1``), or an obviously-internal hostname
    (``localhost``, ``*.internal``, ``*.local``, ``metadata.google.internal`` …).

The check is intentionally DNS-free so it is hermetic and fast at construction
time; it catches the env-injection vectors in the threat model (internal IP /
hostname literals) without a network round-trip. The residual DNS-rebinding case
(a public name that resolves to a private IP) is out of scope for env injection.

Fail-closed by default. An operator who deliberately points an endpoint at a
private host (e.g. a self-hosted Polygon node) can opt out with
``ALLOW_PRIVATE_INTERNAL_URLS=true``.
"""
from __future__ import annotations

import ipaddress
import logging
import os
import socket
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

_ALLOWED_SCHEMES = frozenset({"http", "https"})

# Hostname suffixes/names that are never public.
_INTERNAL_HOST_SUFFIXES = (".internal", ".local", ".lan", ".localdomain", ".localhost")
_INTERNAL_HOST_NAMES = frozenset({"localhost", "metadata.google.internal"})


def _private_urls_allowed() -> bool:
    return os.getenv("ALLOW_PRIVATE_INTERNAL_URLS", "").strip().lower() in ("1", "true", "yes")


def _is_internal_ip(ip) -> bool:
    # `not is_global` is the catch-all — it covers CGNAT (100.64.0.0/10) and
    # other non-global ranges the specific flags miss; the explicit flags stay
    # for clarity and belt-and-suspenders.
    return bool(
        not ip.is_global
        or ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_reserved
        or ip.is_multicast
        or ip.is_unspecified
    )


def _as_ip(host: str):
    """Return the IP for an IP-literal host, else None.

    Covers canonical dotted-quad, decimal/integer (2130706433 == 127.0.0.1), and
    the resolver-accepted legacy IPv4 forms (127.1, 0x7f000001, 0177.0.0.1) that
    ``ipaddress`` rejects but ``inet_aton`` and ``requests`` both accept.
    """
    try:
        return ipaddress.ip_address(host)
    except ValueError:
        pass
    try:
        return ipaddress.ip_address(int(host))
    except (ValueError, OverflowError):
        pass
    try:
        return ipaddress.IPv4Address(socket.inet_aton(host))
    except OSError:
        return None


def assert_public_url(url: str, *, env_name: str = "", allow_http: bool = True) -> str:
    """Return ``url`` unchanged if it is SSRF-safe, else raise ``ValueError``.

    Args:
        url: The candidate URL (typically read from an environment variable).
        env_name: The env-var name, used only to make error messages actionable.
        allow_http: When False, only ``https`` is accepted (use for endpoints
            that are always TLS, e.g. exchange APIs).

    Returns:
        The same ``url`` string, so call sites can assign the validated value.

    Raises:
        ValueError: On a bad scheme, embedded credentials, or an internal host.
    """
    label = env_name or "url"
    if not isinstance(url, str) or not url.strip():
        raise ValueError(f"{label} is empty or not a string")
    url = url.strip()  # tolerate copy/paste whitespace in env vars

    parsed = urlparse(url)
    allowed = _ALLOWED_SCHEMES if allow_http else frozenset({"https"})
    if parsed.scheme not in allowed:
        raise ValueError(f"{label} scheme {parsed.scheme!r} not allowed (need one of {sorted(allowed)})")
    if parsed.username or parsed.password:
        raise ValueError(f"{label} must not embed credentials")
    host = parsed.hostname
    if not host:
        raise ValueError(f"{label} has no host")

    if _private_urls_allowed():
        return url

    ip = _as_ip(host)
    if ip is not None:
        if _is_internal_ip(ip):
            raise ValueError(
                f"{label} host {host!r} is a non-public address {ip} "
                f"(set ALLOW_PRIVATE_INTERNAL_URLS=true to override)"
            )
        return url

    lowered = host.lower().rstrip(".")
    if lowered in _INTERNAL_HOST_NAMES or lowered.endswith(_INTERNAL_HOST_SUFFIXES):
        raise ValueError(
            f"{label} host {host!r} is an internal hostname "
            f"(set ALLOW_PRIVATE_INTERNAL_URLS=true to override)"
        )
    return url
