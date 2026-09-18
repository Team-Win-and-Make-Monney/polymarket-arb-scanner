"""Unit tests for the SSRF URL guard (audit S01-S04). Hermetic — no DNS."""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from url_guard import assert_public_url


class TestUrlGuard:
    # -----------------------------------------------------------------------
    # Accepts public URLs (the real defaults) without touching the network
    # -----------------------------------------------------------------------

    def test_public_https_hostname_passes(self):
        assert assert_public_url("https://api.gemini.com", env_name="GEMINI_BASE_URL") == (
            "https://api.gemini.com"
        )

    def test_public_http_hostname_passes_when_allowed(self):
        assert assert_public_url("http://polygon-rpc.com", env_name="POLYGON_RPC_URL")

    def test_public_ip_literal_passes(self):
        assert assert_public_url("https://93.184.216.34", env_name="WEBHOOK_URL")

    def test_leading_trailing_whitespace_stripped(self):
        assert assert_public_url("  https://api.gemini.com\n", env_name="GEMINI_BASE_URL") == (
            "https://api.gemini.com"
        )

    def test_uppercase_scheme_internal_rejected(self):
        with pytest.raises(ValueError, match="non-public"):
            assert_public_url("HTTP://127.0.0.1", env_name="WEBHOOK_URL")

    # -----------------------------------------------------------------------
    # Rejects the SSRF vectors (internal IP literals)
    # -----------------------------------------------------------------------

    def test_private_ip_literal_rejected(self):
        with pytest.raises(ValueError, match="non-public"):
            assert_public_url("http://10.0.0.5:8545", env_name="POLYGON_RPC_URL")

    def test_loopback_rejected(self):
        with pytest.raises(ValueError, match="non-public"):
            assert_public_url("http://127.0.0.1", env_name="WEBHOOK_URL")

    def test_cloud_metadata_ip_rejected(self):
        # 169.254.169.254 is link-local — the AWS/GCP metadata IP.
        with pytest.raises(ValueError, match="non-public"):
            assert_public_url("http://169.254.169.254/latest/meta-data/", env_name="GJO_API_URL")

    def test_ipv6_loopback_rejected(self):
        with pytest.raises(ValueError, match="non-public"):
            assert_public_url("http://[::1]:9000", env_name="WEBHOOK_URL")

    def test_decimal_encoded_ip_rejected(self):
        # http://2130706433 == 127.0.0.1 — a classic guard-bypass form.
        with pytest.raises(ValueError, match="non-public"):
            assert_public_url("http://2130706433", env_name="POLYGON_RPC_URL")

    @pytest.mark.parametrize("url", [
        "http://127.1",            # a.b shorthand → 127.0.0.1
        "http://0x7f000001",       # hex → 127.0.0.1
        "http://0177.0.0.1",       # octal → 127.0.0.1
        "http://100.64.0.1",       # CGNAT 100.64.0.0/10 — non-global, not is_private
    ])
    def test_legacy_and_non_global_ip_forms_rejected(self, url):
        with pytest.raises(ValueError, match="non-public"):
            assert_public_url(url, env_name="POLYGON_RPC_URL")

    # -----------------------------------------------------------------------
    # Rejects internal hostnames
    # -----------------------------------------------------------------------

    def test_localhost_name_rejected(self):
        with pytest.raises(ValueError, match="internal hostname"):
            assert_public_url("http://localhost:8080", env_name="WEBHOOK_URL")

    def test_internal_suffix_rejected(self):
        with pytest.raises(ValueError, match="internal hostname"):
            assert_public_url("https://redis.internal", env_name="GEMINI_BASE_URL")

    def test_gcp_metadata_hostname_rejected(self):
        with pytest.raises(ValueError, match="internal hostname"):
            assert_public_url("http://metadata.google.internal/", env_name="GJO_API_URL")

    # -----------------------------------------------------------------------
    # Rejects malformed / dangerous shapes
    # -----------------------------------------------------------------------

    def test_non_http_scheme_rejected(self):
        with pytest.raises(ValueError, match="scheme"):
            assert_public_url("file:///etc/passwd", env_name="WEBHOOK_URL")

    def test_https_only_mode_rejects_http(self):
        with pytest.raises(ValueError, match="scheme"):
            assert_public_url("http://api.gemini.com", env_name="GEMINI_BASE_URL", allow_http=False)

    def test_embedded_credentials_rejected(self):
        with pytest.raises(ValueError, match="credentials"):
            assert_public_url("https://user:pass@api.gemini.com", env_name="GEMINI_BASE_URL")

    def test_empty_rejected(self):
        with pytest.raises(ValueError, match="empty"):
            assert_public_url("", env_name="WEBHOOK_URL")

    # -----------------------------------------------------------------------
    # Operator opt-out
    # -----------------------------------------------------------------------

    def test_private_allowed_with_opt_out(self):
        with patch.dict("os.environ", {"ALLOW_PRIVATE_INTERNAL_URLS": "true"}):
            assert assert_public_url("http://10.0.0.5:8545", env_name="POLYGON_RPC_URL")
