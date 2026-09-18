"""Tests for the REQUIRE_KALSHI fail-fast gate (cli.require_kalshi_or_exit)."""
import pytest

from cli import require_kalshi_or_exit


class _FakeClient:
    pass


class TestRequireKalshi:
    @pytest.mark.parametrize("value", ["1", "true", "yes", "TRUE", " true ", "Yes\n"])
    def test_exits_when_required_and_client_missing(self, monkeypatch, value):
        monkeypatch.setenv("REQUIRE_KALSHI", value)
        with pytest.raises(SystemExit) as exc:
            require_kalshi_or_exit(None)
        assert exc.value.code == 1

    @pytest.mark.parametrize("value", ["1", "true", "yes"])
    def test_no_exit_when_required_and_client_present(self, monkeypatch, value):
        monkeypatch.setenv("REQUIRE_KALSHI", value)
        require_kalshi_or_exit(_FakeClient())

    @pytest.mark.parametrize("value", ["", "0", "false", "no", "banana"])
    def test_no_exit_when_not_required(self, monkeypatch, value):
        monkeypatch.setenv("REQUIRE_KALSHI", value)
        require_kalshi_or_exit(None)

    def test_no_exit_when_unset(self, monkeypatch):
        monkeypatch.delenv("REQUIRE_KALSHI", raising=False)
        require_kalshi_or_exit(None)
