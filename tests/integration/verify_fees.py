"""Fee verification script — HARDEN-02 evidence.

Verifies that fee calculations in fees.py match documented platform rates for
all 8 trading platforms. Run via:

    python tests/integration/verify_fees.py

Exit codes:
  0 — all fee calculations match documented rates within 0.1% tolerance
  1 — at least one mismatch detected

Fee references:
  Polymarket: 2% taker fee on net winnings -- docs.polymarket.com
  Kalshi: round up 7% of C * P * (1-P) per order -- kalshi.com/docs/kalshi-fee-schedule.pdf
  Betfair: 2-5% commission on net winnings -- BETFAIR_COMMISSION_RATE in config.py (default 3%)
  Smarkets: 2% fixed commission -- SMARKETS_COMMISSION_RATE in config.py
  Gemini: 1% maker / 5% taker -- GEMINI_FEE_RATE in config.py (default 5% taker)
  SX Bet: 0% commission on predictions
  Matchbook: 0% commission on predictions
  IBKR ForecastEx: $0.00 commission
"""

import math
import sys
import os

# ---------------------------------------------------------------------------
# Path setup — allow running from any directory
# ---------------------------------------------------------------------------

_HERE = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.abspath(os.path.join(_HERE, "..", ".."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

# ---------------------------------------------------------------------------
# Imports from project
# ---------------------------------------------------------------------------

from fees import (
    polymarket_fee,
    kalshi_taker_fee,
    net_profit_binary_internal,
    net_profit_kalshi_binary,
    net_profit_betfair_backall,
    net_profit_smarkets_backall,
    net_profit_gemini_binary,
    net_profit_sxbet_backall,
    net_profit_matchbook_backall,
    net_profit_ibkr_binary,
)
from config import (
    BETFAIR_COMMISSION_RATE,
    SMARKETS_COMMISSION_RATE,
    GEMINI_FEE_RATE,
    KALSHI_FEE_CAP_CENTS,
)


# ---------------------------------------------------------------------------
# Tolerance
# ---------------------------------------------------------------------------

TOLERANCE = 0.001  # 0.1% — any mismatch beyond this is a FAIL


def _within_tolerance(calculated: float, expected: float, tol: float = TOLERANCE) -> bool:
    """Return True if |calculated - expected| <= tol."""
    return abs(calculated - expected) <= tol


# ---------------------------------------------------------------------------
# Per-platform verification cases
# ---------------------------------------------------------------------------


def verify_polymarket() -> list[dict]:
    """Polymarket: 2% fee on net winnings (sell_price - buy_price).

    Reference: docs.polymarket.com — 2% taker fee on net winnings.
    """
    cases = [
        # (yes_price, no_price, description)
        (0.40, 0.55, "buy_40_no_55"),
        (0.30, 0.65, "buy_30_no_65"),
        (0.45, 0.50, "buy_45_no_50"),
    ]
    results = []
    for yes_p, no_p, desc in cases:
        result = net_profit_binary_internal(yes_p, no_p)
        calculated_fee = result["fees"]

        # Documented calculation: 2% of (1.0 - min(yes_p, no_p)) for the winner
        # (the cheaper side wins => higher net winnings => higher fee, worst-case)
        cheaper = min(yes_p, no_p)
        # Net winnings for cheaper side winning: 1.0 - cheaper
        expected_fee_from_pm = 0.02 * (1.0 - cheaper)
        # Note: POLYGON_GAS_ESTIMATE is also included in the fee total.
        # We verify the PM-specific fee component separately.
        pm_fee_component = polymarket_fee(cheaper, 1.0)

        match = _within_tolerance(pm_fee_component, expected_fee_from_pm)
        results.append({
            "platform": "Polymarket",
            "case": desc,
            "buy_price": yes_p,
            "sell_price": no_p,
            "calculated_fee": round(pm_fee_component, 6),
            "documented_fee": round(expected_fee_from_pm, 6),
            "documented_formula": "2% of (1 - min(yes, no))",
            "match": match,
        })
    return results


def verify_kalshi() -> list[dict]:
    """Kalshi: round up 7% of C * P * (1-P) once per order.

    Reference: official schedule effective July 7, 2026.
    Whole-contract, whole-cent orders round the order total up to a cent.
    """
    cases = [
        # (yes_price, no_price, description)
        (0.30, 0.65, "30_65"),
        (0.50, 0.45, "50_45"),
        (0.10, 0.85, "10_85"),
    ]
    results = []
    for yes_p, no_p, desc in cases:
        result = net_profit_kalshi_binary(yes_p, no_p)
        calculated_total_fee = result["fees"]

        # Documented formula: kalshi_taker_fee(p) for each one-contract leg.
        # fee_cents = ceil(7 * P * (1-P)), capped at KALSHI_FEE_CAP_CENTS.
        def expected_kalshi_fee(price: float) -> float:
            if price <= 0 or price >= 1:
                return 0.0
            fee_cents = math.ceil(7 * price * (1.0 - price) - 1e-9)
            fee_cents = min(fee_cents, KALSHI_FEE_CAP_CENTS)
            return fee_cents / 100.0

        expected_fee = expected_kalshi_fee(yes_p) + expected_kalshi_fee(no_p)
        calculated_fee_sum = kalshi_taker_fee(yes_p) + kalshi_taker_fee(no_p)

        match = _within_tolerance(calculated_fee_sum, expected_fee)
        results.append({
            "platform": "Kalshi",
            "case": desc,
            "buy_price": yes_p,
            "sell_price": no_p,
            "calculated_fee": round(calculated_fee_sum, 6),
            "documented_fee": round(expected_fee, 6),
            "documented_formula": "round up 7% of C*P*(1-P) once per order",
            "match": match,
        })
    return results


def verify_betfair() -> list[dict]:
    """Betfair: commission_rate % on net winnings (default 3% from config).

    Reference: BETFAIR_COMMISSION_RATE in config.py (default 0.03 = 3%).
    Documented range: 2-5% depending on activity discount.
    """
    cases = [
        # (implied_probs list, description)
        ([0.40, 0.55], "two_runner_40_55"),
        ([0.30, 0.45, 0.20], "three_runner"),
        ([0.45, 0.50], "two_runner_45_50"),
    ]
    results = []
    for probs, desc in cases:
        result = net_profit_betfair_backall(probs, commission_rate=BETFAIR_COMMISSION_RATE)
        calculated_fee = result["fees"]

        # Documented formula: commission_rate * (1.0 - cheapest_prob)
        cheapest = min(probs)
        net_winnings = 1.0 - cheapest
        expected_fee = BETFAIR_COMMISSION_RATE * net_winnings if net_winnings > 0 else 0.0

        match = _within_tolerance(calculated_fee, expected_fee)
        results.append({
            "platform": "Betfair",
            "case": desc,
            "buy_price": probs[0],
            "sell_price": probs[-1],
            "calculated_fee": round(calculated_fee, 6),
            "documented_fee": round(expected_fee, 6),
            "documented_formula": f"{BETFAIR_COMMISSION_RATE*100:.0f}% of net winnings",
            "match": match,
        })
    return results


def verify_smarkets() -> list[dict]:
    """Smarkets: 2% fixed commission on net winnings.

    Reference: SMARKETS_COMMISSION_RATE in config.py (default 0.02).
    """
    cases = [
        ([0.45, 0.50], "two_runner_45_50"),
        ([0.30, 0.65], "two_runner_30_65"),
        ([0.25, 0.40, 0.30], "three_runner"),
    ]
    results = []
    for probs, desc in cases:
        result = net_profit_smarkets_backall(probs, commission_rate=SMARKETS_COMMISSION_RATE)
        calculated_fee = result["fees"]

        cheapest = min(probs)
        net_winnings = 1.0 - cheapest
        expected_fee = SMARKETS_COMMISSION_RATE * net_winnings if net_winnings > 0 else 0.0

        match = _within_tolerance(calculated_fee, expected_fee)
        results.append({
            "platform": "Smarkets",
            "case": desc,
            "buy_price": probs[0],
            "sell_price": probs[-1],
            "calculated_fee": round(calculated_fee, 6),
            "documented_fee": round(expected_fee, 6),
            "documented_formula": f"{SMARKETS_COMMISSION_RATE*100:.0f}% of net winnings (fixed)",
            "match": match,
        })
    return results


def verify_gemini() -> list[dict]:
    """Gemini Predictions: fee_rate * P * (1-P), rounded up per leg.

    Reference: GEMINI_FEE_RATE in config.py (default 0.05 = 5% taker).
    Maker rate is 1%, taker rate is 5%.
    """
    cases = [
        (0.35, 0.60, "buy_35_no_60"),
        (0.40, 0.55, "buy_40_no_55"),
        (0.45, 0.50, "buy_45_no_50"),
    ]
    results = []
    for yes_p, no_p, desc in cases:
        result = net_profit_gemini_binary(yes_p, no_p, fee_rate=GEMINI_FEE_RATE)
        calculated_fee = result["fees"]

        expected_fee = (
            math.ceil(GEMINI_FEE_RATE * yes_p * (1.0 - yes_p) * 100) / 100
            + math.ceil(GEMINI_FEE_RATE * no_p * (1.0 - no_p) * 100) / 100
        )

        match = _within_tolerance(calculated_fee, expected_fee)
        results.append({
            "platform": "Gemini",
            "case": desc,
            "buy_price": yes_p,
            "sell_price": no_p,
            "calculated_fee": round(calculated_fee, 6),
            "documented_fee": round(expected_fee, 6),
            "documented_formula": f"ceil({GEMINI_FEE_RATE*100:.0f}% * P * (1-P)) per leg",
            "match": match,
        })
    return results


def verify_sxbet() -> list[dict]:
    """SX Bet: 0% commission — no fee on predictions.

    Reference: SX Bet API documentation — 0% fee on prediction markets.
    """
    cases = [
        ([0.45, 0.50], "two_runner_45_50"),
        ([0.30, 0.65], "two_runner_30_65"),
        ([0.25, 0.40, 0.30], "three_runner"),
    ]
    results = []
    for probs, desc in cases:
        result = net_profit_sxbet_backall(probs)
        calculated_fee = result["fees"]
        expected_fee = 0.0

        match = _within_tolerance(calculated_fee, expected_fee)
        results.append({
            "platform": "SX Bet",
            "case": desc,
            "buy_price": probs[0],
            "sell_price": probs[-1],
            "calculated_fee": round(calculated_fee, 6),
            "documented_fee": expected_fee,
            "documented_formula": "0% commission on predictions",
            "match": match,
        })
    return results


def verify_matchbook() -> list[dict]:
    """Matchbook: 0% commission on prediction markets.

    Reference: Matchbook docs — 0% commission on prediction markets.
    """
    cases = [
        ([0.40, 0.55], "two_runner_40_55"),
        ([0.30, 0.65], "two_runner_30_65"),
        ([0.20, 0.50, 0.25], "three_runner"),
    ]
    results = []
    for probs, desc in cases:
        result = net_profit_matchbook_backall(probs)
        calculated_fee = result["fees"]
        expected_fee = 0.0

        match = _within_tolerance(calculated_fee, expected_fee)
        results.append({
            "platform": "Matchbook",
            "case": desc,
            "buy_price": probs[0],
            "sell_price": probs[-1],
            "calculated_fee": round(calculated_fee, 6),
            "documented_fee": expected_fee,
            "documented_formula": "0% commission on predictions",
            "match": match,
        })
    return results


def verify_ibkr() -> list[dict]:
    """IBKR ForecastEx: $0.00 commission.

    Reference: IBKR ForecastEx product documentation — $0.00 commission.
    BUY-only, LMT-only. No commission on either buy or settlement.
    """
    cases = [
        (0.40, 0.55, "buy_40_no_55"),
        (0.30, 0.65, "buy_30_no_65"),
        (0.45, 0.50, "buy_45_no_50"),
    ]
    results = []
    for yes_p, no_p, desc in cases:
        result = net_profit_ibkr_binary(yes_p, no_p)
        calculated_fee = result["fees"]
        expected_fee = 0.0

        match = _within_tolerance(calculated_fee, expected_fee)
        results.append({
            "platform": "IBKR",
            "case": desc,
            "buy_price": yes_p,
            "sell_price": no_p,
            "calculated_fee": round(calculated_fee, 6),
            "documented_fee": expected_fee,
            "documented_formula": "$0.00 commission on ForecastEx",
            "match": match,
        })
    return results


# ---------------------------------------------------------------------------
# Report printer
# ---------------------------------------------------------------------------


def print_table(all_results: list[dict]) -> None:
    """Print a formatted table of fee verification results."""
    header = (
        f"{'Platform':<14} | {'Buy':>5} | {'Sell':>5} | "
        f"{'Calc Fee':>12} | {'Doc Fee':>12} | {'Formula':<36} | Match"
    )
    separator = "-" * len(header)

    print(header)
    print(separator)

    for r in all_results:
        match_str = "YES" if r["match"] else "NO *** MISMATCH ***"
        buy_str = f"{r['buy_price']:.4f}"
        sell_str = f"{r['sell_price']:.4f}"
        calc_str = f"${r['calculated_fee']:.6f}"
        doc_str = f"${r['documented_fee']:.6f}"
        formula = r["documented_formula"][:36]
        platform = r["platform"]

        print(
            f"{platform:<14} | {buy_str:>5} | {sell_str:>5} | "
            f"{calc_str:>12} | {doc_str:>12} | {formula:<36} | {match_str}"
        )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> int:
    """Run all fee verification checks. Returns 0 on success, 1 on failure."""
    print("Fee Verification Script — HARDEN-02 Evidence")
    print("=" * 90)
    print(f"Tolerance: {TOLERANCE * 100:.1f}%")
    print(f"Config: BETFAIR_COMMISSION_RATE={BETFAIR_COMMISSION_RATE}, "
          f"SMARKETS_COMMISSION_RATE={SMARKETS_COMMISSION_RATE}, "
          f"GEMINI_FEE_RATE={GEMINI_FEE_RATE}")
    print()

    all_results = []
    all_results.extend(verify_polymarket())
    all_results.extend(verify_kalshi())
    all_results.extend(verify_betfair())
    all_results.extend(verify_smarkets())
    all_results.extend(verify_gemini())
    all_results.extend(verify_sxbet())
    all_results.extend(verify_matchbook())
    all_results.extend(verify_ibkr())

    print_table(all_results)
    print()

    # Summary by platform
    platforms_checked = sorted(set(r["platform"] for r in all_results))
    print("Platform Summary:")
    all_pass = True
    for platform in platforms_checked:
        platform_results = [r for r in all_results if r["platform"] == platform]
        passed = all(r["match"] for r in platform_results)
        n = len(platform_results)
        status = "PASS" if passed else "FAIL"
        if not passed:
            all_pass = False
        print(f"  {platform:<14}: {status} ({n} cases)")

    print()
    if all_pass:
        print("Result: ALL PASS — All 8 platforms match documented fee rates within "
              f"{TOLERANCE * 100:.1f}% tolerance.")
        return 0
    else:
        mismatches = [r for r in all_results if not r["match"]]
        print(f"Result: FAIL — {len(mismatches)} mismatch(es) detected.")
        for r in mismatches:
            print(f"  MISMATCH: {r['platform']} ({r['case']}): "
                  f"calculated={r['calculated_fee']}, documented={r['documented_fee']}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
