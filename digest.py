"""Daily portfolio P&L digest formatter — the deterministic renderer referenced

by ``pnl_ledger.py``. Pure + no I/O + no LLM: takes the cross-engine P&L entries
and renders the Telegram digest text (command-center 15-KPI-DASHBOARD-SPEC §1).

Minimal scope by design (spec: "start minimal … add the rest as lanes go live"):
only the P&L section is backed by real data today (the ``pnl`` table), so the
remaining digest sections are named honestly as not-yet-wired rather than faked.
"""
from __future__ import annotations

import logging
from datetime import date, timedelta

from pnl_ledger import PnlSummary, aggregate_pnl, clears_hurdle

logger = logging.getLogger(__name__)

# Capital-policy floors (command-center capital policy, set 2026-06-12).
LOC_FLOOR_ANNUAL = 0.047
VOO_PACE_ANNUAL = 0.14

# Digest sections that have no data source yet (no lane is live); surfaced so the
# digest never silently implies these are covered.
_NOT_WIRED = (
    "open positions", "cash/margin", "gate status", "rewards",
    "watcher states", "funding logger", "ops alerts",
)


def _week_start(d: date) -> date:
    """Monday of the week containing ``d``."""
    return d - timedelta(days=d.weekday())


def _month_start(d: date) -> date:
    """First day of ``d``'s month."""
    return d.replace(day=1)


def entries_in_window(entries, asof: date, window: str) -> list:
    """Filter P&L entries to a window: 'day', 'wtd' (Mon-to-date), or 'mtd'.

    Entries with an unparseable ``trade_date`` are skipped rather than raising —
    one bad row must not sink the daily digest.
    """
    if window == "day":
        start = asof
    elif window == "wtd":
        start = _week_start(asof)
    elif window == "mtd":
        start = _month_start(asof)
    else:
        raise ValueError(f"unknown window {window!r}")

    out = []
    for e in entries:
        try:
            td = date.fromisoformat(e.trade_date)
        except (ValueError, TypeError):
            continue
        if start <= td <= asof:
            out.append(e)
    return out


def _fmt_usd(amount: float) -> str:
    """Format a signed USD amount, sign before the dollar (e.g. -$3.00)."""
    sign = "-" if amount < 0 else ""
    return f"{sign}${abs(amount):,.2f}"


def _kv_lines(mapping: dict[str, float]) -> list[str]:
    """Render a {key: amount} map as indented digest lines."""
    if not mapping:
        return ["  (none)"]
    return [f"  {k}: {_fmt_usd(v)}" for k, v in sorted(mapping.items())]


def _hurdle_line(mtd: PnlSummary, asof: date, deployed_capital_usd: float | None) -> str:
    """One-line LOC-floor verdict (if capital given) or the floor/pace reference."""
    if deployed_capital_usd and deployed_capital_usd > 0:
        days_held = (asof - _month_start(asof)).days + 1
        cleared, hurdle_usd = clears_hurdle(
            mtd.total_usd, LOC_FLOOR_ANNUAL, deployed_capital_usd, days_held
        )
        verdict = "beats" if cleared else "below"
        return (f"Hurdle: MTD {verdict} the {LOC_FLOOR_ANNUAL:.2%} LOC floor "
                f"({_fmt_usd(hurdle_usd)} over {days_held}d on "
                f"{_fmt_usd(deployed_capital_usd)} deployed).")
    return f"Hurdle floor {LOC_FLOOR_ANNUAL:.2%} (LOC) · pace ~{VOO_PACE_ANNUAL:.0%} (VOO)."


def format_pnl_digest(entries, *, asof: date, deployed_capital_usd: float | None = None) -> str:
    """Render the daily P&L digest (spec §1) from cross-engine ``PnlEntry`` rows.

    Args:
        entries: PnlEntry rows (typically the month-to-date pull).
        asof: The digest date; day/WTD/MTD windows are computed relative to it.
        deployed_capital_usd: If given (>0), adds a LOC-floor hurdle verdict for
            the MTD total; otherwise just the floor/pace reference line.
    """
    day = aggregate_pnl(entries_in_window(entries, asof, "day"))
    wtd = aggregate_pnl(entries_in_window(entries, asof, "wtd"))
    mtd: PnlSummary = aggregate_pnl(entries_in_window(entries, asof, "mtd"))

    # The data block is wrapped in a ``` code fence: lane / tax-bucket keys like
    # "perp_carry" / "possible_1256" carry underscores, which Telegram Markdown
    # would read as italics delimiters and reject the whole send ("can't parse
    # entities"). Inside a fence the text is literal, so no fragile per-key
    # escaping — only the static header/footer use Markdown.
    body = [
        (f"Net P&L   day {_fmt_usd(day.total_usd)} · "
         f"WTD {_fmt_usd(wtd.total_usd)} · MTD {_fmt_usd(mtd.total_usd)}"),
        "",
        "By lane (MTD):",
        *_kv_lines(mtd.by_lane),
        "",
        "By tax bucket (MTD):",
        *_kv_lines(mtd.by_tax_bucket),
        "",
        _hurdle_line(mtd, asof, deployed_capital_usd),
    ]

    return "\n".join([
        f"📊 *Portfolio P&L digest* — {asof.isoformat()}",
        "```",
        *body,
        "```",
        "_Not yet wired (add as lanes go live): " + ", ".join(_NOT_WIRED) + "._",
    ])
