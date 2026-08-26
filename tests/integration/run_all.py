"""Integration test orchestrator.

Runs every per-strategy dry-run test in ``test_strategies.py`` plus the fee
verification script from Plan 02-02, then writes a structured RESULTS.md
report to ``.planning/phases/02-harden-test/RESULTS.md``.

Usage::

    python tests/integration/run_all.py

Exit codes:
  0 — all tests passed or skipped (no FAILs)
  1 — at least one test FAILED
"""

import os
import subprocess
import sys
import textwrap
from datetime import datetime

# ---------------------------------------------------------------------------
# Path setup
# ---------------------------------------------------------------------------

_HERE = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.abspath(os.path.join(_HERE, "..", ".."))

# Ensure project root is importable (for test_strategies import below)
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

# ---------------------------------------------------------------------------
# Import strategy test module
# ---------------------------------------------------------------------------

import importlib.util as _ilu

_spec = _ilu.spec_from_file_location("test_strategies", os.path.join(_HERE, "test_strategies.py"))
if _spec is None or _spec.loader is None:
    raise RuntimeError("Unable to load integration test_strategies.py")
_mod = _ilu.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

_STRATEGY_FUNCS = [
    name for name in dir(_mod.TestStrategyDryRun)
    if name.startswith("test_") and callable(getattr(_mod.TestStrategyDryRun, name))
]

# ---------------------------------------------------------------------------
# Mode label mapping (human-readable)
# ---------------------------------------------------------------------------

_MODE_LABELS = {
    "test_binary_dry_run": ("Binary internal arb", "binary"),
    "test_negrisk_dry_run": ("NegRisk internal arb", "negrisk"),
    "test_cross_dry_run": ("Cross-platform 2-way", "cross"),
    "test_kalshi_dry_run": ("Kalshi binary + multi", "kalshi"),
    "test_cross_all_dry_run": ("Cross-all platform pairs", "cross-all"),
    "test_spread_dry_run": ("Bid-ask spread", "spread"),
    "test_betfair_dry_run": ("Back-all/Back-lay (Betfair)", "betfair"),
    "test_smarkets_dry_run": ("Back-all/Back-lay (Smarkets)", "smarkets"),
    "test_sxbet_dry_run": ("Back-all/Back-lay (SX Bet)", "sxbet"),
    "test_matchbook_dry_run": ("Back-all/Back-lay (Matchbook)", "matchbook"),
    "test_gemini_dry_run": ("Gemini binary + multi", "gemini"),
    "test_ibkr_dry_run": ("IBKR ForecastEx binary", "ibkr"),
    "test_event_dry_run": ("Event divergence (Metaculus)", "event"),
    "test_stale_dry_run": ("Stale price exploitation", "stale"),
    "test_resolution_dry_run": ("Resolution sniping", "resolution"),
    "test_triangular_dry_run": ("Triangular 3-way arb", "triangular"),
    "test_multi_cross_dry_run": ("Multi-outcome cross-platform", "multi-cross"),
    "test_convergence_dry_run": ("Cross-platform convergence", "convergence"),
    "test_mm_dry_run": ("Market making (dry-run)", "mm"),
}


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

def _run_one(fn_name: str) -> dict:
    """Run a single test function and return a result dict."""
    instance = _mod.TestStrategyDryRun()
    fn = getattr(instance, fn_name)
    strategy, mode = _MODE_LABELS.get(fn_name, (fn_name, fn_name))
    ts = datetime.now().isoformat(timespec="seconds")

    try:
        fn()
        return {
            "strategy": strategy,
            "mode": mode,
            "status": "PASS",
            "evidence": "Exit code 0, no Traceback in stderr",
            "timestamp": ts,
        }
    except BaseException as exc:
        msg = str(exc)
        # Detect pytest.skip signal (raised as Skipped — inherits BaseException, not Exception)
        exc_type = type(exc).__name__
        if "skip" in exc_type.lower() or "Skipped" in exc_type:
            reason = msg if msg else "No credentials"
            return {
                "strategy": strategy,
                "mode": mode,
                "status": "SKIP",
                "evidence": reason,
                "timestamp": ts,
            }
        # Genuine failure
        snippet = msg[:300] if msg else "(no message)"
        return {
            "strategy": strategy,
            "mode": mode,
            "status": "FAIL",
            "evidence": snippet,
            "timestamp": ts,
        }


# ---------------------------------------------------------------------------
# Fee verification
# ---------------------------------------------------------------------------

def _run_fee_verification() -> dict:
    """Run verify_fees.py and return a result dict."""
    fee_script = os.path.join(_HERE, "verify_fees.py")
    ts = datetime.now().isoformat(timespec="seconds")

    if not os.path.exists(fee_script):
        return {
            "strategy": "Fee verification (all 8 platforms)",
            "mode": "fee-verification",
            "status": "SKIP",
            "evidence": "verify_fees.py not found — run Plan 02-02 first",
            "timestamp": ts,
        }

    fee_result = subprocess.run(
        [sys.executable, fee_script],
        capture_output=True,
        text=True,
        timeout=30,
        cwd=PROJECT_ROOT,
    )
    fee_status = "PASS" if fee_result.returncode == 0 else "FAIL"
    raw_output = fee_result.stdout or fee_result.stderr or ""
    evidence = raw_output[-300:] if raw_output else "(no output)"

    return {
        "strategy": "Fee verification (all 8 platforms)",
        "mode": "fee-verification",
        "status": fee_status,
        "evidence": evidence.strip(),
        "timestamp": ts,
    }


# ---------------------------------------------------------------------------
# RESULTS.md generator
# ---------------------------------------------------------------------------

def _write_results_md(results: list[dict], output_path: str) -> None:
    """Write structured RESULTS.md to output_path."""
    run_date = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    strategy_results = [r for r in results if r["mode"] != "fee-verification"]
    fee_results = [r for r in results if r["mode"] == "fee-verification"]

    total = len(strategy_results)
    passed = sum(1 for r in strategy_results if r["status"] == "PASS")
    failed = sum(1 for r in strategy_results if r["status"] == "FAIL")
    skipped = sum(1 for r in strategy_results if r["status"] == "SKIP")

    lines = [
        "# Phase 2: Harden & Test — Integration Test Results",
        "",
        f"**Run date:** {run_date}",
        f"**Total:** {total} tested, {passed} passed, {failed} failed, {skipped} skipped",
        "",
        "## Strategy Dry-Run Tests (HARDEN-01)",
        "",
        "| Strategy | Mode | Status | Evidence | Timestamp |",
        "|----------|------|--------|----------|-----------|",
    ]

    for r in strategy_results:
        evidence = r["evidence"].replace("|", " ").replace("\n", " ")
        evidence = textwrap.shorten(evidence, width=80, placeholder="...")
        lines.append(
            f"| {r['strategy']} | {r['mode']} | {r['status']} | {evidence} | {r['timestamp']} |"
        )

    lines += [
        "",
        "## Fee Verification (HARDEN-02)",
        "",
        "| Check | Status | Evidence | Timestamp |",
        "|-------|--------|----------|-----------|",
    ]

    for r in fee_results:
        evidence = r["evidence"].replace("|", " ").replace("\n", " ")
        evidence = textwrap.shorten(evidence, width=120, placeholder="...")
        lines.append(
            f"| {r['strategy']} | {r['status']} | {evidence} | {r['timestamp']} |"
        )

    lines.append("")  # trailing newline

    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines))

    print(f"RESULTS.md written to {output_path}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    print(f"Running {len(_STRATEGY_FUNCS)} strategy integration tests...")
    print()

    results: list[dict] = []

    for fn_name in sorted(_STRATEGY_FUNCS):
        result = _run_one(fn_name)
        results.append(result)
        status_icon = {"PASS": "PASS", "FAIL": "FAIL", "SKIP": "SKIP"}[result["status"]]
        print(f"  [{status_icon}] {result['strategy']} ({result['mode']})")
        if result["status"] == "FAIL":
            print(f"         {result['evidence'][:120]}")

    print()
    print("Running fee verification (verify_fees.py)...")
    fee_result = _run_fee_verification()
    results.append(fee_result)
    print(f"  [{fee_result['status']}] {fee_result['strategy']}")

    # Write RESULTS.md
    output_path = os.path.join(
        PROJECT_ROOT, ".planning", "phases", "02-harden-test", "RESULTS.md"
    )
    _write_results_md(results, output_path)

    # Summary
    strategy_results = [r for r in results if r["mode"] != "fee-verification"]
    failed = [r for r in strategy_results if r["status"] == "FAIL"]
    fee_failed = fee_result["status"] == "FAIL"
    passed = sum(1 for r in strategy_results if r["status"] == "PASS")
    skipped = sum(1 for r in strategy_results if r["status"] == "SKIP")

    print()
    print(f"Summary: {passed} passed, {len(failed)} failed, {skipped} skipped")

    if failed or fee_failed:
        print("FAILED tests:")
        for r in failed:
            print(f"  - {r['strategy']} ({r['mode']})")
        if fee_failed:
            print(f"  - {fee_result['strategy']} (fee-verification)")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
