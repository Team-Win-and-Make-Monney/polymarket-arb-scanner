"""Per-strategy dry-run integration tests.

Each test invokes ``scanner.py --mode <mode> --dry-run`` as a subprocess
against real platform APIs and asserts:
  - Exit code is 0 (no crash)
  - No ``Traceback`` appears in stderr

Tests skip gracefully when the required platform credentials are absent so the
suite can run in CI without secrets.
"""

import os
import subprocess
import sys
import time

import pytest

# ---------------------------------------------------------------------------
# Path setup
# ---------------------------------------------------------------------------

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))


# ---------------------------------------------------------------------------
# Runner helper
# ---------------------------------------------------------------------------

def _run_scanner(mode: str, timeout: int = 120) -> tuple[int, str, str]:
    """Run ``scanner.py --mode <mode> --dry-run`` and return (returncode, stdout, stderr)."""
    result = subprocess.run(
        [sys.executable, os.path.join(PROJECT_ROOT, "scanner.py"),
         "--mode", mode, "--dry-run"],
        capture_output=True,
        text=True,
        timeout=timeout,
        cwd=PROJECT_ROOT,
    )
    return result.returncode, result.stdout, result.stderr


# ---------------------------------------------------------------------------
# Credential helpers
# ---------------------------------------------------------------------------

def _has_polymarket_creds() -> bool:
    return bool(os.getenv("POLYMARKET_PRIVATE_KEY"))


def _has_kalshi_creds() -> bool:
    return bool(os.getenv("KALSHI_API_KEY_ID"))


def _has_betfair_creds() -> bool:
    return all(os.getenv(k) for k in ("BETFAIR_APP_KEY", "BETFAIR_USERNAME", "BETFAIR_PASSWORD"))


def _has_smarkets_creds() -> bool:
    return bool(os.getenv("SMARKETS_API_KEY"))


def _has_sxbet_creds() -> bool:
    return bool(os.getenv("SXBET_API_KEY"))


def _has_matchbook_creds() -> bool:
    return all(os.getenv(k) for k in ("MATCHBOOK_USERNAME", "MATCHBOOK_PASSWORD"))


def _has_gemini_creds() -> bool:
    return all(os.getenv(k) for k in ("GEMINI_API_KEY", "GEMINI_API_SECRET"))


def _has_ibkr_creds() -> bool:
    return bool(os.getenv("IBKR_HOST"))


# ---------------------------------------------------------------------------
# Integration test class
# ---------------------------------------------------------------------------

class TestStrategyDryRun:
    """Dry-run integration tests — one test per scanner --mode value."""

    # Class-level results store (populated during runs for reporting)
    _results: list[dict] = []

    # -- Layer 1: Pure Arbitrage -----------------------------------------------

    def test_binary_dry_run(self):
        """Binary internal arb scan (Polymarket)."""
        if not _has_polymarket_creds():
            pytest.skip("No Polymarket credentials (POLYMARKET_PRIVATE_KEY)")
        code, stdout, stderr = _run_scanner("binary")
        assert code == 0, f"binary mode exited {code}\n--- stderr ---\n{stderr}"
        assert "Traceback" not in stderr, f"Traceback in stderr:\n{stderr}"

    def test_negrisk_dry_run(self):
        """NegRisk internal arb scan (Polymarket)."""
        if not _has_polymarket_creds():
            pytest.skip("No Polymarket credentials (POLYMARKET_PRIVATE_KEY)")
        code, stdout, stderr = _run_scanner("negrisk")
        assert code == 0, f"negrisk mode exited {code}\n--- stderr ---\n{stderr}"
        assert "Traceback" not in stderr, f"Traceback in stderr:\n{stderr}"

    def test_cross_dry_run(self):
        """Cross-platform 2-way arb scan (Polymarket + Kalshi)."""
        if not _has_polymarket_creds() or not _has_kalshi_creds():
            pytest.skip("Requires Polymarket + Kalshi credentials")
        code, stdout, stderr = _run_scanner("cross")
        assert code == 0, f"cross mode exited {code}\n--- stderr ---\n{stderr}"
        assert "Traceback" not in stderr, f"Traceback in stderr:\n{stderr}"

    def test_kalshi_dry_run(self):
        """Kalshi binary + multi-outcome arb scan."""
        if not _has_kalshi_creds():
            pytest.skip("No Kalshi credentials (KALSHI_API_KEY_ID)")
        code, stdout, stderr = _run_scanner("kalshi")
        assert code == 0, f"kalshi mode exited {code}\n--- stderr ---\n{stderr}"
        assert "Traceback" not in stderr, f"Traceback in stderr:\n{stderr}"

    def test_cross_all_dry_run(self):
        """Cross-all scan across all available platform pairs."""
        if not _has_polymarket_creds() or not _has_kalshi_creds():
            pytest.skip("Requires Polymarket + Kalshi credentials (minimum pair)")
        code, stdout, stderr = _run_scanner("cross-all")
        assert code == 0, f"cross-all mode exited {code}\n--- stderr ---\n{stderr}"
        assert "Traceback" not in stderr, f"Traceback in stderr:\n{stderr}"

    def test_spread_dry_run(self):
        """Bid-ask spread detection (Polymarket + Kalshi)."""
        if not _has_polymarket_creds() or not _has_kalshi_creds():
            pytest.skip("Requires Polymarket + Kalshi credentials")
        code, stdout, stderr = _run_scanner("spread")
        assert code == 0, f"spread mode exited {code}\n--- stderr ---\n{stderr}"
        assert "Traceback" not in stderr, f"Traceback in stderr:\n{stderr}"

    def test_betfair_dry_run(self):
        """Back-all / Back-lay arb scan (Betfair)."""
        if not _has_betfair_creds():
            pytest.skip("No Betfair credentials (BETFAIR_APP_KEY / BETFAIR_USERNAME / BETFAIR_PASSWORD)")
        code, stdout, stderr = _run_scanner("betfair")
        assert code == 0, f"betfair mode exited {code}\n--- stderr ---\n{stderr}"
        assert "Traceback" not in stderr, f"Traceback in stderr:\n{stderr}"

    def test_smarkets_dry_run(self):
        """Back-all / Back-lay arb scan (Smarkets)."""
        if not _has_smarkets_creds():
            pytest.skip("No Smarkets credentials (SMARKETS_API_KEY)")
        code, stdout, stderr = _run_scanner("smarkets")
        assert code == 0, f"smarkets mode exited {code}\n--- stderr ---\n{stderr}"
        assert "Traceback" not in stderr, f"Traceback in stderr:\n{stderr}"

    def test_sxbet_dry_run(self):
        """Back-all / Back-lay arb scan (SX Bet)."""
        if not _has_sxbet_creds():
            pytest.skip("No SX Bet credentials (SXBET_API_KEY)")
        code, stdout, stderr = _run_scanner("sxbet")
        assert code == 0, f"sxbet mode exited {code}\n--- stderr ---\n{stderr}"
        assert "Traceback" not in stderr, f"Traceback in stderr:\n{stderr}"

    def test_matchbook_dry_run(self):
        """Back-all / Back-lay arb scan (Matchbook)."""
        if not _has_matchbook_creds():
            pytest.skip("No Matchbook credentials (MATCHBOOK_USERNAME / MATCHBOOK_PASSWORD)")
        code, stdout, stderr = _run_scanner("matchbook")
        assert code == 0, f"matchbook mode exited {code}\n--- stderr ---\n{stderr}"
        assert "Traceback" not in stderr, f"Traceback in stderr:\n{stderr}"

    def test_gemini_dry_run(self):
        """Binary + multi-outcome scan (Gemini Predictions)."""
        if not _has_gemini_creds():
            pytest.skip("No Gemini credentials (GEMINI_API_KEY / GEMINI_API_SECRET)")
        code, stdout, stderr = _run_scanner("gemini")
        assert code == 0, f"gemini mode exited {code}\n--- stderr ---\n{stderr}"
        assert "Traceback" not in stderr, f"Traceback in stderr:\n{stderr}"

    def test_ibkr_dry_run(self):
        """Binary scan (IBKR ForecastEx via IB Gateway)."""
        if not _has_ibkr_creds():
            pytest.skip("No IBKR credentials (IBKR_HOST)")
        code, stdout, stderr = _run_scanner("ibkr")
        assert code == 0, f"ibkr mode exited {code}\n--- stderr ---\n{stderr}"
        assert "Traceback" not in stderr, f"Traceback in stderr:\n{stderr}"

    # -- Layer 2: Near-Arbitrage -----------------------------------------------

    def test_event_dry_run(self):
        """Event divergence scan using synthetic Metaculus-shaped data."""
        # This integration test is local-only; the live API requires approved access.
        code, stdout, stderr = _run_scanner("event")
        assert code == 0, f"event mode exited {code}\n--- stderr ---\n{stderr}"
        assert "Traceback" not in stderr, f"Traceback in stderr:\n{stderr}"

    def test_stale_dry_run(self):
        """Stale price exploitation scan (one-shot is a no-op — requires --continuous).

        In one-shot mode, stale scan produces no results because it needs historical
        WebSocket price data. We assert only that the exit code is 0 and there is no
        crash — NOT that any opportunities are found.
        """
        if not _has_polymarket_creds():
            pytest.skip("No Polymarket credentials (POLYMARKET_PRIVATE_KEY)")
        code, stdout, stderr = _run_scanner("stale")
        assert code == 0, f"stale mode exited {code}\n--- stderr ---\n{stderr}"
        assert "Traceback" not in stderr, f"Traceback in stderr:\n{stderr}"

    def test_resolution_dry_run(self):
        """Resolution sniping scan (Polymarket + Kalshi)."""
        if not _has_polymarket_creds() or not _has_kalshi_creds():
            pytest.skip("Requires Polymarket + Kalshi credentials")
        code, stdout, stderr = _run_scanner("resolution")
        assert code == 0, f"resolution mode exited {code}\n--- stderr ---\n{stderr}"
        assert "Traceback" not in stderr, f"Traceback in stderr:\n{stderr}"

    # -- Layer 1 (Advanced): Triangular & Multi-Cross ---------------------------

    def test_triangular_dry_run(self):
        """3-way cross-platform triangular arb scan."""
        if not _has_polymarket_creds() or not _has_kalshi_creds():
            pytest.skip("Requires Polymarket + Kalshi credentials")
        code, stdout, stderr = _run_scanner("triangular")
        assert code == 0, f"triangular mode exited {code}\n--- stderr ---\n{stderr}"
        assert "Traceback" not in stderr, f"Traceback in stderr:\n{stderr}"

    def test_multi_cross_dry_run(self):
        """Multi-outcome cross-platform scan (cheapest YES per outcome)."""
        if not _has_polymarket_creds() or not _has_kalshi_creds():
            pytest.skip("Requires Polymarket + Kalshi credentials")
        code, stdout, stderr = _run_scanner("multi-cross")
        assert code == 0, f"multi-cross mode exited {code}\n--- stderr ---\n{stderr}"
        assert "Traceback" not in stderr, f"Traceback in stderr:\n{stderr}"

    # -- Layer 4: Informed Trading ----------------------------------------------

    def test_convergence_dry_run(self):
        """Cross-platform convergence scan (outlier → median)."""
        if not _has_polymarket_creds() or not _has_kalshi_creds():
            pytest.skip("Requires Polymarket + Kalshi credentials")
        code, stdout, stderr = _run_scanner("convergence")
        assert code == 0, f"convergence mode exited {code}\n--- stderr ---\n{stderr}"
        assert "Traceback" not in stderr, f"Traceback in stderr:\n{stderr}"

    # -- Layer 3: Market Making -------------------------------------------------

    def test_mm_dry_run(self):
        """Market making engine dry-run (gated by MM_ENABLED flag)."""
        if not _has_polymarket_creds():
            pytest.skip("No Polymarket credentials (POLYMARKET_PRIVATE_KEY)")
        code, stdout, stderr = _run_scanner("mm")
        assert code == 0, f"mm mode exited {code}\n--- stderr ---\n{stderr}"
        assert "Traceback" not in stderr, f"Traceback in stderr:\n{stderr}"
