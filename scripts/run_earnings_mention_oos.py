"""Earnings-mention NO-harvest OOS logger — the live weekly runner.

Weekly entry point for ``earnings_mention.py``: finds Kalshi company-KPI /
earnings-mention markets that have SETTLED since the last watermark,
reconstructs each one's YES price at T-24h before close via candlesticks,
accumulates the resolved history in a local JSON state file across runs,
recomputes the OOS richness stats + pre-registered verdict, and
Telegram-tickets the running verdict. Detection/logging only — this script
never places an order, never touches capital, and has no LLM in its path.
See docs/plans/08-earnings-mention-oos.md.

Redesign (2026-07-05): v1 tried to snapshot markets LIVE while they sat
inside their open [close-24h, close-6h] window, tracked as "pending" state
resolved on a later run. That approach cannot reliably catch a market whose
entire lifetime (open -> settle) falls between two weekly cron runs — a
real coverage gap. This version instead looks backwards from what has
ALREADY settled (client.fetch_settled_markets, watermark-bounded) and
reconstructs each one's T-24h price after the fact via candlesticks
(earnings_mention.price_at_t24h) — the same method the in-sample pilot used
(T1-pm-dispersion-novelty.md §a). There is no "pending" state as a result:
state is just a time watermark + a set of already-processed tickers + the
accumulated resolved history.

Fail-closed on partial failures, at two levels:
  - Per-ticker: a market whose candlestick fetch itself FAILS (network/HTTP
    error — signaled by a RuntimeError) is neither marked seen nor
    counted, and the watermark rolls back so it's retried next cycle. This
    is distinct from a market that has NO usable candle because it
    genuinely never traded in the T-24h window (e.g. its whole lifetime was
    shorter than the lookback) — earnings_mention.price_at_t24h returns
    None for that, a PERMANENT condition, so it's marked seen and excluded
    immediately rather than retried forever (both cases used to look
    identical — "returns None either way" — which permanently stuck a
    market that could never resolve in the retry queue).
  - Whole-cycle: kalshi_api.fetch_settled_markets raises
    RuntimeError rather than returning a silently-partial
    list if a page fails or the page budget is exhausted with more data
    available. main() treats that as a total cycle failure: no state
    change, no alert, try again next scheduled run.
Any per-ticker failure this cycle suppresses the normal verdict alert (a
partial-cycle verdict could be biased if the failures aren't random) in
favor of a distinct failure notice; state changes are still saved so
progress from tickers that DID resolve cleanly this cycle isn't lost.

State: v1 persists locally as JSON, restored across scheduled runs via
``actions/cache`` — the same convention scripts/run_edgar_scan.py uses for
its dedup state file. This intentionally does NOT write to the shared
Supabase ``pnl`` table: that table is realized P&L (see
scripts/run_pnl_digest.py), and this logger produces $0 statistical rows
that would corrupt its rollups. Spec step 6 gates "add Supabase secrets ->
activate weekly cron" as an explicit operator follow-up once a dedicated
table exists — not assumed here. actions/cache is this repo's only existing
cross-run state convention (no workflow here commits state back to git);
introducing an auto-push-to-master capability for a rolling state blob was
judged a materially bigger, riskier change than the problem warranted for a
~4-week campaign window, so instead: the workflow also uploads the state
file as a 90-day build artifact (human-recoverable backup, not part of the
automated restore path), and _check_state_anomaly gives a best-effort,
self-referential guard against a silent cache reset (see its docstring for
the one case it structurally cannot detect: a *total* cache wipe erases its
own reference point too).

Cron-safe at every missing-config boundary: no Kalshi creds, a failed
auth, or missing Telegram creds all log a warning and exit 0 rather than
raising.

Usage:
    python scripts/run_earnings_mention_oos.py [--state-file PATH] [--always-alert]
"""
from __future__ import annotations

import argparse
import json
import logging
import math
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

# Allow `from earnings_mention import ...` / `from kalshi_api import ...` when
# run as a script from the repo root (scripts/ would otherwise be sys.path[0]).
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from earnings_mention import (  # noqa: E402
    OosStats,
    Resolved,
    _close_time,
    build_resolved,
    classify_market,
    compute_oos_stats,
    has_valid_result,
    price_at_t24h,
    verdict,
)
from kalshi_api import KalshiClient  # noqa: E402
from notifier import WebhookNotifier  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
logger = logging.getLogger(__name__)

# A loaded state older than this with zero resolved records is suspicious
# enough to log/alert loudly rather than silently proceed (see
# _check_state_anomaly). 30d (not a shorter window) because mention-market
# settlements are lumpy -- tied to each company's quarterly earnings call --
# so a multi-week gap with genuinely zero settlements is plausible on its
# own and shouldn't page anyone; this can't fully tell the two apart (see
# _check_state_anomaly's docstring), so it trades some detection latency
# for fewer false alarms.
ANOMALY_MIN_AGE_DAYS = 30.0


def _empty_state() -> dict:
    """A fresh state — never a shared instance.

    Returning a module-level constant dict here would alias its ``seen``/
    ``resolved`` list objects across every caller; a future in-place
    mutation on one caller's "empty" state would silently corrupt it for
    every other caller in the same process. ``watermark_ts``/``first_seen_ts``
    are None (not 0/epoch) so run_cycle knows to seed them from "now" on a
    genuine first run rather than attempting to page Kalshi's entire
    settlement history.
    """
    return {"watermark_ts": None, "seen": [], "resolved": [], "last_verdict": "continue", "first_seen_ts": None}


# --------------------------------------------------------------------------- #
# JSON (de)serialization for the Resolved dataclass
# --------------------------------------------------------------------------- #
def _resolved_to_dict(r: Resolved, resolved_ts: str) -> dict:
    return {
        "ticker": r.ticker,
        "yes_price": r.yes_price,
        "outcome": r.outcome,
        "series": r.series,
        "resolved_ts": resolved_ts,
    }


def _resolved_from_dict(d: dict) -> Resolved:
    return Resolved(ticker=d["ticker"], yes_price=d["yes_price"], outcome=d["outcome"], series=d["series"])


def _close_ts(market: dict) -> int | None:
    """Unix-seconds close_time for watermark bookkeeping, or None."""
    dt = _close_time(market)
    return int(dt.timestamp()) if dt is not None else None


# --------------------------------------------------------------------------- #
# State (accumulate OOS resolutions across weekly runs)
# --------------------------------------------------------------------------- #
def load_state(path: Path | None) -> dict:
    """Load {watermark_ts, seen, resolved, last_verdict, first_seen_ts}.

    Only a missing file is fresh state. Corrupt or schema-invalid state raises
    so the scheduled job cannot silently discard accumulated OOS evidence.
    """
    if not path or not path.exists():
        return _empty_state()
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("OOS state must be a JSON object")
    state = {
        "watermark_ts": data.get("watermark_ts"),
        "seen": data.get("seen", []),
        "resolved": data.get("resolved", []),
        "last_verdict": data.get("last_verdict", "continue"),
        "first_seen_ts": data.get("first_seen_ts"),
    }
    watermark = state["watermark_ts"]
    if watermark is not None and (
        isinstance(watermark, bool) or not isinstance(watermark, int) or watermark < 0
    ):
        raise ValueError("OOS state watermark_ts must be null or a non-negative integer")
    if not isinstance(state["seen"], list) or not all(
        isinstance(ticker, str) and ticker for ticker in state["seen"]
    ):
        raise ValueError("OOS state seen must be a list of non-empty ticker strings")
    if len(state["seen"]) != len(set(state["seen"])):
        raise ValueError("OOS state seen contains duplicate tickers")
    if not isinstance(state["resolved"], list):
        raise ValueError("OOS state resolved must be a list")
    resolved_tickers: set[str] = set()
    for item in state["resolved"]:
        if not isinstance(item, dict) or not {
            "ticker", "yes_price", "outcome", "series", "resolved_ts",
        }.issubset(item):
            raise ValueError("OOS state contains a malformed resolved row")
        for name in ("ticker", "series", "resolved_ts"):
            if not isinstance(item[name], str) or not item[name]:
                raise ValueError(f"OOS resolved row {name} must be a non-empty string")
        if item["ticker"] in resolved_tickers:
            raise ValueError("OOS state contains duplicate resolved tickers")
        resolved_tickers.add(item["ticker"])
        for name in ("yes_price", "outcome"):
            value = item[name]
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
                raise ValueError(f"OOS resolved row {name} must be a finite number")
        if not 0.0 <= float(item["yes_price"]) <= 1.0 or float(item["outcome"]) not in (0.0, 1.0):
            raise ValueError("OOS resolved row has an invalid price or outcome")
        _resolved_from_dict(item)
        if not isinstance(item["resolved_ts"], str):
            raise ValueError("OOS resolved_ts must be a string")
    if not resolved_tickers.issubset(set(state["seen"])):
        raise ValueError("OOS resolved tickers must also be present in seen")
    if state["last_verdict"] not in {"continue", "pursue", "kill"}:
        raise ValueError("OOS state last_verdict is invalid")
    first_seen = state["first_seen_ts"]
    if first_seen is not None:
        parsed = datetime.fromisoformat(first_seen)
        if parsed.tzinfo is None:
            raise ValueError("OOS state first_seen_ts must include a timezone")
    return state


def save_state(path: Path | None, state: dict) -> None:
    """Persist state to the JSON state file.

    Writes atomically (temp file + os.replace) so a crash or kill mid-write
    can never leave a truncated/corrupt file behind — unlike a dedup cache,
    this file *is* the accumulated OOS sample the 8/3 verdict is built on.
    """
    if not path:
        return
    tmp = path.with_name(path.name + ".tmp")
    try:
        tmp.write_text(
            json.dumps(state, sort_keys=True, allow_nan=False), encoding="utf-8"
        )
        os.replace(tmp, path)
    except Exception:
        tmp.unlink(missing_ok=True)
        raise


def _check_state_anomaly(state: dict, now: datetime, min_age_days: float = ANOMALY_MIN_AGE_DAYS) -> str | None:
    """Best-effort check: does the freshly-loaded state look like it silently
    reset (e.g. an actions/cache eviction) rather than being a genuine first
    run?

    Two acknowledged, undetectable-from-here limitations (documented rather
    than "fixed" — neither has a clean fix without a second, independently
    durable signal this pipeline doesn't have, see the module docstring):
      - Self-referential: if the ENTIRE cache is wiped, first_seen_ts is
        wiped along with it, so a total wipe can't be detected here.
      - Ambiguous with a genuine zero-settlement period: mention-market
        settlements are lumpy (quarterly per company), so an old
        first_seen_ts with zero resolved records could mean either "cache
        reset" or "nothing has settled yet, and that's normal." This
        function cannot tell those apart; it only knows the gap has gone on
        long enough (ANOMALY_MIN_AGE_DAYS) to be worth a human glance at the
        run history either way.
    """
    first_seen = state.get("first_seen_ts")
    if not first_seen:
        return None  # genuinely the first run -- nothing to compare against
    try:
        age_days = (now - datetime.fromisoformat(first_seen)).total_seconds() / 86400.0
    except (ValueError, TypeError):
        return None
    if age_days >= min_age_days and not state.get("resolved"):
        return (
            f"state has first_seen_ts={first_seen} ({age_days:.1f}d ago) but 0 "
            "resolved records loaded. This MAY be a genuine zero-settlement "
            "gap (mention-market settlements are lumpy, tied to quarterly "
            "earnings) rather than a cache reset -- but it's gone on long "
            "enough to be worth a human glance at the run history."
        )
    return None


# --------------------------------------------------------------------------- #
# One weekly cycle — deterministic given (client, now, state) beyond the
# client's own network calls. Fully testable with a fake client, no network.
# --------------------------------------------------------------------------- #
def run_cycle(client, now: datetime, state: dict) -> dict:
    """Find newly-settled mention/KPI markets, reconstruct each one's T-24h
    price, and accumulate the running OOS verdict. No "pending" snapshot
    state: a market is only ever looked at once, after it has already
    settled.

    Fail-closed: a market whose candlestick fetch fails this cycle is
    neither marked seen nor counted, and the watermark is rolled back to
    guarantee it's re-included in the next fetch_settled_markets call — it
    is retried, never silently dropped.

    Returns the updated persistable state ({watermark_ts, seen, resolved,
    last_verdict, first_seen_ts}) plus caller-facing extras the caller pops
    before saving: ``_stats`` (OosStats), ``_prev_verdict`` (str),
    ``_new_resolved`` (int), ``_failed_tickers`` (list[str]).
    """
    seen: set[str] = set(state.get("seen", []))
    resolved_dicts = list(state.get("resolved", []))
    resolved_objs = [_resolved_from_dict(d) for d in resolved_dicts]

    watermark = state.get("watermark_ts")
    watermark = int(now.timestamp()) if watermark is None else int(watermark)

    raw_markets = client.fetch_settled_markets(watermark) or []

    newly_resolved: list[Resolved] = []
    newly_seen: set[str] = set()
    failed_tickers: list[str] = []
    processed_tickers: set[str] = set()
    max_close_ts = watermark
    min_failed_close_ts: int | None = None

    for market in raw_markets:
        ticker = str(market.get("ticker", ""))
        close_ts = _close_ts(market)
        if close_ts is not None:
            max_close_ts = max(max_close_ts, close_ts + 1)
        if not ticker or ticker in seen or ticker in processed_tickers:
            continue
        processed_tickers.add(ticker)

        if not classify_market(market):
            continue  # not mention/KPI -- watermark advancement above is enough

        if not has_valid_result(market):
            newly_seen.add(ticker)  # voided/undecided -- will never resolve differently
            continue

        try:
            price = price_at_t24h(client, market)
        except RuntimeError as exc:
            # Transient: the candlestick request itself failed. Not seen,
            # not counted -- retried next cycle via the watermark rollback
            # below.
            logger.debug("Candlestick fetch failed for %s: %s", ticker, exc)
            failed_tickers.append(ticker)
            if close_ts is not None:
                min_failed_close_ts = close_ts if min_failed_close_ts is None else min(min_failed_close_ts, close_ts)
            continue

        if price is None:
            # Permanent: the request succeeded but there is definitively no
            # usable candle (e.g. the market's whole lifetime was shorter
            # than the T-24h lookback window). Mark seen so it's excluded
            # for good -- retrying can never produce different data, and
            # treating this the same as a transient failure would
            # permanently jam the watermark on an unfixable ticker.
            newly_seen.add(ticker)
            continue

        newly_resolved.append(build_resolved(market, price))
        newly_seen.add(ticker)

    seen |= newly_seen
    resolved_ts = now.isoformat()
    resolved_dicts = resolved_dicts + [_resolved_to_dict(r, resolved_ts) for r in newly_resolved]
    resolved_objs = resolved_objs + newly_resolved

    # A failure rolls the watermark back to just before the earliest failed
    # ticker's close_time, so the NEXT fetch_settled_markets call is
    # guaranteed to include it again regardless of inclusive/exclusive
    # boundary semantics. Tickers that already succeeded in this same batch
    # get re-fetched too on that retry, but `seen` skips them without a
    # redundant candlestick call -- idempotent by construction.
    new_watermark = (min_failed_close_ts - 1) if min_failed_close_ts is not None else max_close_ts

    stats = compute_oos_stats(resolved_objs)
    prev_verdict = state.get("last_verdict", "continue")
    verdict_str = verdict(stats)
    persisted_verdict = prev_verdict if failed_tickers else verdict_str

    return {
        "watermark_ts": new_watermark,
        "seen": sorted(seen),
        "resolved": resolved_dicts,
        "last_verdict": persisted_verdict,
        "first_seen_ts": state.get("first_seen_ts") or now.isoformat(),
        "_stats": stats,
        "_prev_verdict": prev_verdict,
        "_new_resolved": len(newly_resolved),
        "_failed_tickers": failed_tickers,
    }


def _should_alert(new_resolved: int, prev_verdict: str, verdict_str: str, always_alert: bool) -> bool:
    """Alert on real signal (a new resolution or a verdict flip), or if forced.

    Keeps a weekly cron from spamming the same "continue, n=0" message every
    run while still surfacing the one thing that actually matters: the OOS
    verdict changing, e.g. continue -> pursue.
    """
    return new_resolved > 0 or verdict_str != prev_verdict or always_alert


def format_message(stats: OosStats, verdict_str: str, new_resolved: int) -> str:
    """Render the running-verdict Telegram ticket text."""
    lines = [
        "\U0001F4CA Earnings-Mention OOS Logger — weekly run",
        f"New settlements resolved this run: {new_resolved}",
        f"OOS sample (11-50c band): n={stats.n}, mean richness={stats.mean_richness_pts:+.2f}pts, z={stats.z:.2f}",
        f"Verdict: {verdict_str.upper()}",
    ]
    if stats.by_category:
        top = sorted(stats.by_category.items(), key=lambda kv: -kv[1][0])[:5]
        lines.append(
            "By category (n/mean pts): "
            + ", ".join(f"{series}={n}/{mean:+.1f}" for series, (n, mean) in top)
        )
    return "\n".join(lines)


def _format_failure_notice(failed_tickers: list[str]) -> str:
    """Render the fail-closed failure-notice Telegram ticket text."""
    shown = ", ".join(failed_tickers[:10])
    more = f" (+{len(failed_tickers) - 10} more)" if len(failed_tickers) > 10 else ""
    return (
        "⚠️ Earnings-Mention OOS Logger — "
        f"{len(failed_tickers)} market(s) could not be resolved this cycle "
        f"(candlestick fetch failed): {shown}{more}. Will retry next run; "
        "verdict not recomputed/alerted this cycle (fail-closed)."
    )


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #
def main() -> None:
    parser = argparse.ArgumentParser(description="Earnings-mention NO-harvest OOS logger (weekly)")
    parser.add_argument("--state-file", type=Path, default=None,
                        help="JSON file accumulating the watermark/seen/resolved state across runs")
    parser.add_argument("--always-alert", action="store_true",
                        help="Telegram-ticket the running verdict even with no new signal this run")
    args = parser.parse_args()

    api_key_id = os.getenv("KALSHI_API_KEY_ID")
    private_key_path = os.getenv("KALSHI_PRIVATE_KEY_PATH")
    private_key_b64 = os.getenv("KALSHI_PRIVATE_KEY_BASE64")
    if not api_key_id or not (private_key_path or private_key_b64):
        logger.warning(
            "KALSHI_API_KEY_ID / KALSHI_PRIVATE_KEY_PATH (or _BASE64) not set — "
            "cannot read Kalshi markets. Cron-safe exit."
        )
        return

    client = KalshiClient()
    if private_key_b64:
        authed = client.login_with_api_key(api_key_id, private_key_base64=private_key_b64)
    else:
        authed = client.login_with_api_key(api_key_id, private_key_path=os.path.expanduser(private_key_path))
    if not authed:
        logger.warning("Kalshi auth failed. Cron-safe exit.")
        return

    now = datetime.now(timezone.utc)
    state = load_state(args.state_file)

    token = os.getenv("TELEGRAM_BOT_TOKEN")
    chat_id = os.getenv("TELEGRAM_CHAT_ID")

    anomaly = _check_state_anomaly(state, now)
    if anomaly:
        logger.error("OOS STATE ANOMALY: %s", anomaly)
        if token and chat_id:
            WebhookNotifier("telegram").notify_text(f"⚠️ Earnings-Mention OOS Logger anomaly: {anomaly}")

    try:
        result = run_cycle(client, now, state)
    except RuntimeError as exc:
        # The discovery step itself couldn't retrieve a complete result set
        # this cycle (a page failed, or the page budget ran out with more
        # data available). Treating a partial list as complete risks a
        # biased verdict or a watermark advanced past markets never
        # actually seen -- so this is a whole-cycle abort, not a per-ticker
        # retry: no state change, no alert, try again next scheduled run.
        logger.error("OOS cycle aborted: settled-markets discovery incomplete: %s. Cron-safe exit, no state change.", exc)
        return

    stats: OosStats = result.pop("_stats")
    prev_verdict: str = result.pop("_prev_verdict")
    new_resolved: int = result.pop("_new_resolved")
    failed_tickers: list[str] = result.pop("_failed_tickers")
    verdict_str: str = result["last_verdict"]

    logger.info(
        "OOS cycle: seen=%d resolved_total=%d new_resolved=%d failed=%d n=%d mean=%.2fpts z=%.2f verdict=%s watermark_ts=%d",
        len(result["seen"]), len(result["resolved"]), new_resolved, len(failed_tickers),
        stats.n, stats.mean_richness_pts, stats.z, verdict_str, result["watermark_ts"],
    )
    save_state(args.state_file, result)

    if failed_tickers:
        logger.warning(
            "OOS cycle had %d fetch failure(s); skipping the verdict alert this cycle, will retry next run: %s",
            len(failed_tickers), ", ".join(failed_tickers),
        )
        if token and chat_id:
            WebhookNotifier("telegram").notify_text(_format_failure_notice(failed_tickers))
        return  # fail-closed: never alert a verdict computed from a cycle with fetch failures

    if not _should_alert(new_resolved, prev_verdict, verdict_str, args.always_alert):
        logger.info("No new resolutions and verdict unchanged (%s) — skipping Telegram.", verdict_str)
        return

    message = format_message(stats, verdict_str, new_resolved)
    if not token or not chat_id:
        logger.warning("TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID not set — printing instead:\n%s", message)
        return
    WebhookNotifier("telegram").notify_text(message)
    logger.info("Telegram ticket sent (verdict=%s, new_resolved=%d).", verdict_str, new_resolved)


if __name__ == "__main__":
    main()
