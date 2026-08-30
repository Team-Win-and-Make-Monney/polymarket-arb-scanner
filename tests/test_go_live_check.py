"""Tests for the deployment pre-flight URL safety boundary."""

from unittest.mock import patch

import pytest

from scripts.go_live_check import _validate_base_url


class TestValidateBaseUrl:
    def test_allows_localhost_for_local_preflight(self):
        assert _validate_base_url("http://localhost:8080") == "http://localhost:8080"

    @pytest.mark.parametrize("value", ["file:///etc/passwd", "ftp://example.com/x", "not-a-url"])
    def test_rejects_non_http_urls(self, value):
        with pytest.raises(ValueError):
            _validate_base_url(value)

    def test_rejects_link_local_resolution(self):
        answer = [(2, 1, 6, "", ("169.254.169.254", 80))]
        with patch("scripts.go_live_check.socket.getaddrinfo", return_value=answer):
            with pytest.raises(ValueError, match="link-local"):
                _validate_base_url("http://metadata.example")

    def test_allows_public_resolution(self):
        answer = [(2, 1, 6, "", ("93.184.216.34", 443))]
        with patch("scripts.go_live_check.socket.getaddrinfo", return_value=answer):
            assert _validate_base_url("https://example.com") == "https://example.com"
