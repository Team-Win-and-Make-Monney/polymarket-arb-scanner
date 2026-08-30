"""Kalshi reward-MM pilot safety layer (plan 10 — docs/plans/10-mm-pilot-prep.md).

Everything needed to *quote* already existed (QuoteEngine, trackers, hedger);
this module is everything needed to *survive being filled*:

- ``FillPoller``       — REST polling of ``KalshiClient.get_fills`` (section 3)
- ``HedgeController``  — auto-hedge on real fills, fail closed (section 4)
- ``authorize_order``  — the single choke point in front of ``place_order``
                         enforcing hard inventory caps (section 5)
- gate chain G1-G12    — deterministic pre-quote gates in the hot path (section 6)
- ``ControlsPoller``   — Supabase ``bot_controls.mm_pilot_enabled`` kill switch,
                         fail closed on control-plane loss (section 7)
- canary state machine — tiny-size start, auto-halt on any deviation (section 8)

Deterministic code only — no LLM anywhere, hot path or otherwise. Venue is
Kalshi ONLY; the platform literal is hard-checked at the choke point on top of
the ``ENABLED_EXECUTION_PLATFORMS`` allowlist. Every gate FAILS CLOSED: missing
or stale data, unreachable control plane, unknown orders, or hedge failure stop
quoting and pull resting orders — never proceed.
"""

from __future__ import annotations

import json
import logging
import math
import os
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from url_guard import assert_public_url

logger = logging.getLogger(__name__)

PLATFORM = "kalshi"  # hardcoded venue — the pilot never routes anywhere else
KALSHI_TICK = 0.01


def _data_path(env_name: str, default_name: str) -> str:
    """Resolve an operator-configured pilot file inside ``DATA_DIR``."""
    data_dir = Path(os.getenv("DATA_DIR", ".")).expanduser().resolve()
    raw = os.getenv(env_name)
    candidate = (
        Path(raw).expanduser() if raw else data_dir / default_name
    ).resolve()
    try:
        candidate.relative_to(data_dir)
    except ValueError as exc:
        raise ValueError(f"{env_name} must resolve inside DATA_DIR") from exc
    return str(candidate)


DECISIONS_LOG_PATH = _data_path("MM_DECISIONS_LOG_PATH", "decisions.jsonl")
STATE_PATH = _data_path("MM_STATE_PATH", "mm_pilot_state.json")

# Startup reconciliation (finding #4): when there is no persisted checkpoint
# to anchor the fill lookback, use a wide fixed window rather than "now" —
# "safely early" beats guessing at how long the process was down.
RECONCILE_FALLBACK_LOOKBACK_SECONDS = 24 * 3600


# ---------------------------------------------------------------------------
# Value types
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class FillEvent:
    """One accepted pilot fill (spec section 3)."""

    fill_id: str
    order_id: str
    ticker: str
    side: str            # "yes" | "no"
    action: str          # "buy" | "sell"
    count: int           # contracts
    price: float         # yes-price in dollars 0.01-0.99
    is_taker: bool       # True should never happen for resting quotes
    created_ts: float
    mid_at_detect: float  # book mid when we detected the fill (for toxicity)


@dataclass(frozen=True)
class GateResult:
    """Verdict from ``authorize_order`` / a pre-quote gate."""

    allowed: bool
    reason: str


def _parse_created_ts(fill: dict, fallback: float) -> float:
    """Parse a Kalshi fill's created_time into Unix seconds (fail-soft)."""
    raw = fill.get("created_time")
    if raw is None:
        return fallback
    if isinstance(raw, (int, float)):
        return float(raw)
    try:
        return datetime.fromisoformat(str(raw).replace("Z", "+00:00")).timestamp()
    except (ValueError, TypeError):
        return fallback


# ---------------------------------------------------------------------------
# PilotInventory — signed YES-equivalent exposure with avg-cost accounting
# ---------------------------------------------------------------------------

class PilotInventory:
    """Per-market net inventory in YES-equivalent terms. Thread-safe.

    Long YES exposure (buy yes / sell no) is positive; long NO exposure is
    negative. Dollars-at-cost uses the direction's contract price (yes price
    when long YES, ``1 - yes_price`` when long NO). Realized P&L accrues when
    a fill reduces the position.
    """

    def __init__(self):
        self._net: dict[str, int] = {}       # ticker -> signed contracts
        self._avg: dict[str, float] = {}     # ticker -> avg cost/contract (direction terms)
        self._realized: dict[str, float] = {}
        self._lock = threading.Lock()

    @staticmethod
    def signed_contracts(side: str, action: str, count: int) -> int:
        """YES-equivalent signed contract delta for a fill."""
        positive = (side == "yes") == (action == "buy")
        return count if positive else -count

    def apply_fill(self, ticker: str, side: str, action: str, count: int,
                   yes_price: float) -> float:
        """Apply a fill; returns realized P&L delta (0.0 when accumulating)."""
        delta = self.signed_contracts(side, action, count)
        with self._lock:
            net = self._net.get(ticker, 0)
            avg = self._avg.get(ticker, 0.0)
            realized = 0.0
            remaining = delta
            if net != 0 and (net > 0) != (remaining > 0):
                # Reducing (possibly through zero)
                reduce_ct = min(abs(remaining), abs(net))
                exit_price = yes_price if net > 0 else (1.0 - yes_price)
                realized = (exit_price - avg) * reduce_ct
                net += reduce_ct if remaining > 0 else -reduce_ct
                remaining += reduce_ct if remaining < 0 else -reduce_ct
                if net == 0:
                    avg = 0.0
            if remaining != 0:
                # Accumulating in the direction of `remaining`
                dir_price = yes_price if remaining > 0 else (1.0 - yes_price)
                total_ct = abs(net) + abs(remaining)
                avg = ((abs(net) * avg + abs(remaining) * dir_price) / total_ct
                       if total_ct else 0.0)
                net += remaining
            self._net[ticker] = net
            self._avg[ticker] = avg
            self._realized[ticker] = self._realized.get(ticker, 0.0) + realized
            return realized

    def net_contracts(self, ticker: str) -> int:
        with self._lock:
            return self._net.get(ticker, 0)

    def net_usd(self, ticker: str) -> float:
        """Signed net inventory in dollars at cost."""
        with self._lock:
            return self._net.get(ticker, 0) * self._avg.get(ticker, 0.0)

    def avg_cost(self, ticker: str) -> float:
        with self._lock:
            return self._avg.get(ticker, 0.0)

    def total_net_usd(self) -> float:
        """Sum of absolute net inventory (dollars at cost) across markets."""
        with self._lock:
            return sum(abs(n) * self._avg.get(t, 0.0)
                       for t, n in self._net.items())

    def realized_pnl_total(self) -> float:
        with self._lock:
            return sum(self._realized.values())

    def tickers_with_inventory(self) -> list[str]:
        with self._lock:
            return [t for t, n in self._net.items() if n != 0]

    def snapshot(self) -> dict:
        """Serializable snapshot for restart persistence."""
        with self._lock:
            return {
                "net": dict(self._net),
                "avg": dict(self._avg),
                "realized": dict(self._realized),
            }

    def restore(self, snap: dict) -> None:
        """Restore a snapshot() payload (restart reconciliation)."""
        with self._lock:
            self._net = {t: int(v) for t, v in (snap.get("net") or {}).items()}
            self._avg = {t: float(v) for t, v in (snap.get("avg") or {}).items()}
            self._realized = {t: float(v)
                              for t, v in (snap.get("realized") or {}).items()}


# ---------------------------------------------------------------------------
# PilotStateStore — minimal restart persistence (fail-closed reconciliation)
# ---------------------------------------------------------------------------

class PilotStateStore:
    """Atomic JSON persistence for the pilot's minimal restart state.

    Persists last_fill_ts, the open-order registry, the inventory snapshot,
    and recent seen fill ids so a restart can reconcile against the venue
    instead of assuming zero inventory and a 60s fill lookback.
    """

    def __init__(self, path: str):
        self.path = path
        self._lock = threading.Lock()

    def save(self, state: dict) -> None:
        tmp = f"{self.path}.tmp"
        with self._lock:
            try:
                with open(tmp, "w", encoding="utf-8") as fh:
                    json.dump(state, fh)
                os.replace(tmp, self.path)
            except Exception:
                logger.exception("MM pilot state persist failed (%s)", self.path)

    def load(self) -> dict | None:
        with self._lock:
            try:
                with open(self.path, encoding="utf-8") as fh:
                    return json.load(fh)
            except FileNotFoundError:
                return None
            except Exception:
                logger.exception("MM pilot state load failed (%s) — treating "
                                 "as unrecoverable, reconciliation must fail",
                                 self.path)
                raise


# ---------------------------------------------------------------------------
# ControlsPoller — Supabase bot_controls kill switch (spec section 7)
# ---------------------------------------------------------------------------

class SupabaseControlsClient:
    """Minimal read-only REST client for the pilot kill switch."""

    def __init__(self, url: str, key: str, session=None, timeout: float = 5.0):
        import requests

        self._url = url.rstrip("/")
        self._key = key
        self._session = session or requests.Session()
        self._timeout = timeout

    def fetch_control(self, key: str) -> bool | None:
        """Return one control value, or None when the row is absent."""
        response = self._session.get(
            f"{self._url}/rest/v1/bot_controls",
            params={"key": f"eq.{key}", "select": "value", "limit": "1"},
            headers={
                "apikey": self._key,
                "Authorization": f"Bearer {self._key}",
            },
            timeout=self._timeout,
        )
        response.raise_for_status()
        rows = response.json()
        if not isinstance(rows, list):
            raise RuntimeError("Supabase bot_controls response must be a list")
        if not rows:
            return None
        return rows[0].get("value") is True


def build_controls_client_from_env() -> SupabaseControlsClient:
    """Build the read-only controls client without the supabase-py SDK."""
    url = os.getenv("SUPABASE_URL", "").strip()
    key = (os.getenv("SUPABASE_SERVICE_KEY")
           or os.getenv("SUPABASE_SERVICE_ROLE_KEY")
           or os.getenv("SUPABASE_KEY") or "").strip()
    if not url or not key:
        raise RuntimeError(
            "SUPABASE_URL and SUPABASE_SERVICE_KEY (or SUPABASE_KEY) must be set"
        )
    url = assert_public_url(url, env_name="SUPABASE_URL", allow_http=False)
    return SupabaseControlsClient(url, key)

class ControlsPoller:
    """Timestamped local cache of ``bot_controls.mm_pilot_enabled``.

    Fail closed: no cache, a stale cache (control plane unreachable), or an
    explicit false all read as OFF. Unknown operator intent = off.
    """

    CONTROL_KEY = "mm_pilot_enabled"

    def __init__(self, supabase_client=None, time_fn=time.time):
        self._client = supabase_client
        self._time_fn = time_fn
        self._value: bool | None = None
        self._fetched_at: float = 0.0
        self._lock = threading.Lock()

    def poll(self) -> None:
        """Refresh the cached value. Errors leave the cache stale (fail closed)."""
        if self._client is None:
            return
        try:
            if hasattr(self._client, "fetch_control"):
                value = self._client.fetch_control(self.CONTROL_KEY)
                rows = [] if value is None else [{"value": value}]
            else:
                resp = (self._client.table("bot_controls")
                        .select("value").eq("key", self.CONTROL_KEY)
                        .limit(1).execute())
                rows = getattr(resp, "data", None) or []
            if not rows:
                logger.warning("bot_controls has no %s row — treating as OFF",
                               self.CONTROL_KEY)
                with self._lock:
                    self._value = False
                    self._fetched_at = self._time_fn()
                return
            with self._lock:
                self._value = bool(rows[0].get("value"))
                self._fetched_at = self._time_fn()
        except Exception as exc:
            logger.warning("ControlsPoller poll failed (%s) — cache goes stale, "
                           "pilot fails closed after %.0fs", exc,
                           self._max_stale())

    @staticmethod
    def _max_stale() -> float:
        from config import MM_CONTROLS_MAX_STALE_SECONDS
        return MM_CONTROLS_MAX_STALE_SECONDS

    def set_cached(self, value: bool, fetched_at: float | None = None) -> None:
        """Directly seed the cache (tests / local override)."""
        with self._lock:
            self._value = value
            self._fetched_at = (self._time_fn() if fetched_at is None
                                else fetched_at)

    def is_enabled(self) -> bool:
        """True only if the cached value is fresh AND true."""
        with self._lock:
            value, fetched_at = self._value, self._fetched_at
        if value is not True:
            return False
        return (self._time_fn() - fetched_at) <= self._max_stale()


# ---------------------------------------------------------------------------
# Kalshi client proxy — routes hedge placements through the choke point
# ---------------------------------------------------------------------------

class _PilotKalshiProxy:
    """Client facade handed to the pilot-owned ``PartialFillHedger``.

    ``PartialFillHedger._hedge_kalshi`` stays the hedge placement primitive
    (touch-price + max-loss logic unmodified), but its ``place_order`` call is
    routed back through the pilot's ``authorize_order`` choke point so there
    is provably no second code path to ``kalshi_client.place_order`` and the
    hedge order id lands in the pilot's own registry.
    """

    def __init__(self, pilot: "KalshiMMPilot"):
        self._pilot = pilot

    def fetch_order_book(self, ticker: str) -> dict | None:
        return self._pilot.get_raw_book(ticker)

    def place_order(self, ticker: str, side: str, action: str, count: int,
                    price_dollars: float, time_in_force: str = "ioc") -> dict | None:
        order_id = self._pilot.place_pilot_order(
            ticker=ticker, side=side, action=action, count=count,
            price=price_dollars, purpose="hedge", reducing=True,
        )
        if order_id is None:
            return None
        return {"order": {"order_id": order_id}}


# ---------------------------------------------------------------------------
# KalshiMMPilot — orchestrator
# ---------------------------------------------------------------------------

class KalshiMMPilot:
    """Orchestrates the Kalshi reward-MM pilot inside continuous mode.

    All external effects (orders, cancels, alerts, audit rows) flow through
    injectable collaborators so every safety property is unit-testable with a
    scripted fake client. ``DRY_RUN`` short-circuits before any client call:
    dry-run sessions place zero real orders and synthesize fills from book
    crosses (rollout phase D0).
    """

    def __init__(
        self,
        kalshi_client=None,
        db=None,
        alert_manager=None,
        controls: ControlsPoller | None = None,
        quote_engine=None,
        toxic_detector=None,
        volatility_tracker=None,
        hedger_factory=None,
        decision_writer=None,
        dry_run: bool | None = None,
        time_fn=time.time,
        mono_fn=time.monotonic,
        state_path: str | None = STATE_PATH,
    ):
        import config
        from market_maker import (QuoteEngine, get_toxic_flow_detector,
                                  get_volatility_tracker)

        self._client = kalshi_client
        self._db = db
        self._alerts = alert_manager
        self._controls = controls or ControlsPoller(time_fn=time_fn)
        self._quote_engine = quote_engine or QuoteEngine(
            min_spread=config.MM_MIN_SPREAD)
        self._toxic = toxic_detector or get_toxic_flow_detector()
        self._vol = volatility_tracker or get_volatility_tracker()
        self._time_fn = time_fn
        # Latency/aging measurements use a monotonic clock — wall-clock steps
        # (NTP, DST, frozen clocks) must not defeat or false-trigger ceilings.
        self._mono_fn = mono_fn
        self.dry_run = config.DRY_RUN if dry_run is None else dry_run

        # Pilot-owned hedger over the choke-point proxy (spec section 4).
        from hedger import PartialFillHedger
        factory = hedger_factory or (
            lambda proxy: PartialFillHedger(kalshi_client=proxy, db=db))
        self._hedger = factory(_PilotKalshiProxy(self))

        self.inventory = PilotInventory()
        self._lock = threading.RLock()

        # Resting-order registry: order_id -> order info. The ONLY source of
        # truth for what the pilot has on the book.
        self._orders: dict[str, dict] = {}
        self._order_seq = 0
        self.place_order_calls = 0  # test/runtime assertion counter

        # Book cache per ticker: parsed levels + raw + freshness.
        self._books: dict[str, dict] = {}

        # Selection snapshot (PR #43's select_lip_markets output). None until
        # the first snapshot arrives — G4 fails closed without one.
        self._selected: set[str] | None = None

        # Halt state
        self.halted = False           # whole-pilot halt (manual restart)
        self.halt_reason = ""
        self._market_halted: dict[str, str] = {}     # ticker -> reason
        self._market_halt_times: dict[str, list[float]] = {}
        self._hedge_failures: dict[str, int] = {}    # consecutive, per ticker

        # Fill dedupe (bounded to last 1,000 fill ids)
        self._seen_fill_ids: dict[str, None] = {}
        self._last_fill_ts: float = self._time_fn()

        # Fill-poll blindness (fail closed: never quote without fill sight)
        self._fill_poll_failures = 0
        self._fills_blind = False

        # State persistence + startup reconciliation. Live quoting is refused
        # until reconcile() succeeds; dry-run needs no venue reconciliation.
        self._state_store = PilotStateStore(state_path) if state_path else None
        self._reconciled = bool(self.dry_run)

        # Canary state (spec section 8)
        self.canary_graduated = False
        self.canary_clean_fills = 0
        self._live_started_ts = self._time_fn()
        self._daily_pnl_date = self._utc_date()
        self._daily_pnl_opening_total = self.inventory.realized_pnl_total()

        self._decision_writer = decision_writer
        self._decision_fh = None
        self._decision_lock = threading.Lock()
        self._loop_error_streak = 0

    # -- audit -------------------------------------------------------------

    def _write_decision(self, gate: str, ticker: str, allowed: bool,
                        reason: str, **extra) -> None:
        """Append one gate/order decision to the decisions audit trail."""
        entry = {
            "ts": self._time_fn(),
            "strategy": "KalshiMMPilot",
            "tax_bucket": "ordinary",
            "gate": gate,
            "market": ticker,
            "decision": "pass" if allowed else "fail",
            "reason": reason,
        }
        entry.update(extra)
        try:
            if self._decision_writer is not None:
                self._decision_writer(entry)
                return
            with self._decision_lock:
                if self._decision_fh is None:
                    self._decision_fh = open(DECISIONS_LOG_PATH, "a",
                                             encoding="utf-8")
                self._decision_fh.write(json.dumps(entry) + "\n")
                self._decision_fh.flush()
        except Exception:
            logger.exception("MM pilot decision audit write failed")

    def _alert(self, alert_type: str, severity: str, message: str,
               details: dict | None = None) -> None:
        log_level = {
            "INFO": logging.INFO,
            "WARNING": logging.WARNING,
            "CRITICAL": logging.CRITICAL,
        }.get(str(severity).upper(), logging.WARNING)
        logger.log(log_level, "MM pilot %s: %s", alert_type, message)
        if self._alerts is not None:
            try:
                self._alerts.alert(alert_type, severity, message, details)
            except Exception:
                logger.exception("MM pilot alert dispatch failed")

    # -- selection / book state --------------------------------------------

    def update_selection(self, tickers: list[str]) -> None:
        """Install the latest ``select_lip_markets`` output (gate G4 input)."""
        with self._lock:
            self._selected = {t for t in tickers if t}

    def pilot_tickers(self) -> list[str]:
        """Markets the pilot currently owns: selected + carrying state."""
        with self._lock:
            tickers = set(self._selected or ())
            tickers.update(info["ticker"] for info in self._orders.values())
        tickers.update(self.inventory.tickers_with_inventory())
        return sorted(tickers)

    def update_book(self, ticker: str, raw_book: dict | None) -> None:
        """Cache a fresh order book snapshot for a pilot market."""
        from kalshi_api import (parse_orderbook, best_yes_bid, best_no_bid,
                                best_yes_ask)
        if not raw_book:
            return
        parsed = parse_orderbook(raw_book)
        yes_bid = best_yes_bid(parsed)
        no_bid = best_no_bid(parsed)
        yes_ask = best_yes_ask(parsed)  # derived from best NO bid
        mid = None
        if yes_bid and yes_ask:
            mid = (yes_bid[0] + yes_ask[0]) / 2.0
        with self._lock:
            now = self._time_fn()
            self._books[ticker] = {
                "raw": raw_book,
                "mid": mid,
                "yes_bid": yes_bid,      # (price, qty) | None
                "no_bid": no_bid,        # (price, qty) | None
                "yes_ask": yes_ask,      # (price, qty) | None
                "updated_at": now,
                # Distinct from `updated_at`: only a REST book fetch (this
                # method) refreshes actual resting-order levels. WS mid
                # ticks (on_ws_price) refresh `updated_at` far more often
                # without carrying any level data — G11/G12 must never
                # judge levels fresh just because the price looked fresh.
                "levels_updated_at": now,
            }
        if mid is not None:
            try:
                self._vol.record_price(ticker, mid)
            except Exception as exc:
                logger.debug("MM pilot vol record failed (book) for %s "
                             "mid=%.4f: %s", ticker, mid, exc)

    def on_ws_price(self, ticker: str, yes_price: float) -> None:
        """Mid update from the orderbook_delta WS channel (freshness only).

        Deliberately does NOT touch ``levels_updated_at`` — a WS price tick
        carries no book-depth information, only a mid. Gates that consume
        actual levels (G11 depth sizing, G12 crossing guard) must keep
        judging staleness off the last REST book fetch, not this.
        """
        with self._lock:
            book = self._books.get(ticker)
            if book is not None:
                book["mid"] = float(yes_price)
                book["updated_at"] = self._time_fn()
        try:
            self._vol.record_price(ticker, float(yes_price))
        except Exception as exc:
            logger.debug("MM pilot vol record failed (WS) for %s price=%s: %s",
                         ticker, yes_price, exc)

    def get_raw_book(self, ticker: str) -> dict | None:
        with self._lock:
            book = self._books.get(ticker)
            return book["raw"] if book else None

    def _book(self, ticker: str) -> dict | None:
        with self._lock:
            return self._books.get(ticker)

    def _would_cross(self, ticker: str, side: str, action: str,
                     price: float) -> bool:
        """Fresh top-of-book crossing check, used immediately before a live
        quote submission (finding #2's TOCTOU guard — see the call site in
        ``place_pilot_order`` for the full rationale).

        Fetches the CURRENT book (not the cached one gates already
        evaluated against) and asks: would an order at ``price`` on
        ``side``/``action`` execute immediately as a taker right now?
        Fails closed — any inability to establish a fresh, confident
        answer (fetch error, empty book, unknown depth on the relevant
        side) returns True (treat as crossing, abort the placement)
        rather than assume it's safe to proceed.
        """
        if self._client is None:
            return True
        try:
            raw_book = self._client.fetch_order_book(ticker)
        except Exception:
            logger.exception("MM pilot pre-submit book re-check failed for "
                             "%s", ticker)
            return True
        if not raw_book:
            return True
        from kalshi_api import (parse_orderbook, best_yes_ask, best_no_ask,
                                best_yes_bid, best_no_bid)
        parsed = parse_orderbook(raw_book)
        if action == "buy":
            touch = best_yes_ask(parsed) if side == "yes" else best_no_ask(parsed)
            if touch is None:
                return True
            return price >= touch[0]
        else:
            touch = best_yes_bid(parsed) if side == "yes" else best_no_bid(parsed)
            if touch is None:
                return True
            return price <= touch[0]

    # -- registry helpers ----------------------------------------------------

    def resting_orders(self, ticker: str = "") -> list[dict]:
        with self._lock:
            return [
                {"order_id": oid, **info}
                for oid, info in self._orders.items()
                if not ticker or info["ticker"] == ticker
            ]

    def _resting_notional(self, ticker: str) -> float:
        with self._lock:
            return sum(info["count"] * info["price"]
                       for info in self._orders.values()
                       if info["ticker"] == ticker)

    def _total_resting_notional(self) -> float:
        """Return aggregate notional reserved by every resting pilot order."""
        with self._lock:
            return sum(
                info["count"] * info["price"] for info in self._orders.values()
            )

    def _utc_date(self) -> str:
        """Return the current UTC date using the injectable wall clock."""
        return datetime.fromtimestamp(self._time_fn(), tz=timezone.utc).date().isoformat()

    def _daily_realized_pnl(self) -> float:
        """Return realized P&L since the persisted opening total for this UTC day."""
        day = self._utc_date()
        total = self.inventory.realized_pnl_total()
        with self._lock:
            if self._daily_pnl_date != day:
                self._daily_pnl_date = day
                self._daily_pnl_opening_total = total
            return total - self._daily_pnl_opening_total

    @staticmethod
    def _dashboard_paused() -> bool:
        """Fail closed when the dashboard's operator pause is engaged."""
        try:
            from dashboard import is_paused
        except ImportError:
            return False
        return bool(is_paused())

    # -- restart persistence / startup reconciliation (finding #4) -----------

    def _persist_state(self) -> None:
        """Best-effort snapshot of restart-recovery state.

        Non-blocking on failure: a write failure here must never interrupt
        live trading. This file is a diagnostic aid and a fallback seed for
        the fill lookback window on the next restart — it is NOT the
        authoritative safety mechanism. ``reconcile()`` querying the live
        venue at startup is; this snapshot only narrows the fill lookback
        and gives ``reconcile()`` something to fall back on if a
        position-query endpoint is ever unavailable.
        """
        if self._state_store is None:
            return
        with self._lock:
            orders_snapshot = {oid: dict(info)
                              for oid, info in self._orders.items()}
        state = {
            "last_fill_ts": self._last_fill_ts,
            "orders": orders_snapshot,
            "inventory": self.inventory.snapshot(),
            "seen_fill_ids": list(self._seen_fill_ids.keys())[-200:],
            "daily_pnl_date": self._daily_pnl_date,
            "daily_pnl_opening_total": self._daily_pnl_opening_total,
            "saved_at": self._time_fn(),
        }
        self._state_store.save(state)

    def reconcile(self) -> bool:
        """Startup reconciliation against LIVE venue state (finding #4).

        ``_seen_fill_ids``, ``_last_fill_ts``, and ``_orders`` are in-memory
        only. On a naive restart ``_last_fill_ts`` reseeds to "now" and the
        fill poll only looks back 60s (``poll_fills``), so any fill during
        the crash-to-restart downtime is silently lost and caps/inventory
        tracking restarts from zero while real inventory may be nonzero.

        This is the required minimum fix: before ANY quoting, pull the
        venue's own view of positions and fills, and cancel every resting
        order the venue reports (rather than guess at a stale order's
        purpose/pricing and adopt it — a fresh quote next cycle at current
        prices is strictly safer than trusting an unknown-vintage order).
        ``authorize_order`` fails closed on every order while
        ``self._reconciled`` is False, so nothing above this gate can place
        or reduce until it returns True.

        Dry-run carries no real venue state and reconciles trivially (set
        True in ``__init__`` already; this still no-ops safely if called).

        Returns:
            True on success. False on any failure — ``self._reconciled``
            stays/becomes False (fail closed) and the caller (``run_loop``)
            is expected to retry on a cadence.
        """
        if self.dry_run:
            self._reconciled = True
            return True
        if self._client is None:
            logger.error("MM pilot reconcile: no live client configured — "
                         "fail closed, quoting stays disabled")
            self._reconciled = False
            return False

        persisted = None
        if self._state_store is not None:
            try:
                persisted = self._state_store.load()
            except Exception:
                logger.exception("MM pilot reconcile: persisted state file "
                                 "unreadable — continuing with venue-only "
                                 "reconciliation (not fatal by itself)")
                persisted = None

        since_ts = int((persisted or {}).get(
            "last_fill_ts", self._time_fn() - RECONCILE_FALLBACK_LOOKBACK_SECONDS))
        try:
            open_orders = self._client.get_open_orders()
        except Exception:
            logger.exception("MM pilot reconcile: venue query failed — "
                             "fail closed, quoting stays disabled")
            self._reconciled = False
            return False

        cancel_failures = 0
        for order in open_orders or []:
            oid = str(order.get("order_id") or order.get("id") or "")
            if not oid:
                cancel_failures += 1
                logger.error("MM pilot reconcile: startup order has no id — "
                             "cannot confirm cancellation")
                continue
            try:
                ok = bool(self._client.cancel_order(oid))
            except Exception:
                ok = False
            if not ok:
                cancel_failures += 1
                logger.error("MM pilot reconcile: could not cancel stale "
                             "resting order %s found on startup", oid)
        if cancel_failures:
            self._alert("MM_PILOT_RECONCILE_FAILED", "CRITICAL",
                        f"MM pilot startup reconciliation could not cancel "
                        f"{cancel_failures} pre-existing resting order(s) — "
                        f"fail closed, quoting stays disabled until this is "
                        f"resolved manually on the Kalshi UI",
                        {"cancel_failures": cancel_failures})
            self._reconciled = False
            return False

        # A successful cancel response is not enough: establish a stable,
        # order-free point at the venue before snapshotting positions/fills.
        # Otherwise a late fill can appear in the fills response while being
        # absent from a position snapshot fetched before cancellation.
        try:
            remaining_orders = self._client.get_open_orders()
        except Exception:
            logger.exception("MM pilot reconcile: post-cancel confirmation "
                             "failed — fail closed")
            self._reconciled = False
            return False
        if remaining_orders:
            self._alert(
                "MM_PILOT_RECONCILE_FAILED", "CRITICAL",
                f"MM pilot startup reconciliation still sees "
                f"{len(remaining_orders)} resting order(s) after cancellation "
                f"— quoting stays disabled",
                {"remaining_orders": len(remaining_orders)},
            )
            self._reconciled = False
            return False

        try:
            positions = self._client.get_positions(raise_on_error=True)
            fills = self._client.get_fills(min_ts=since_ts, raise_on_error=True)
        except Exception:
            logger.exception("MM pilot reconcile: stable venue snapshot failed "
                             "— fail closed, quoting stays disabled")
            self._reconciled = False
            return False

        net_map: dict[str, int] = {}
        avg_map: dict[str, float] = {}
        for pos in positions or []:
            ticker = pos.get("ticker", "")
            if not ticker:
                continue
            # Kalshi's documented MarketPosition schema (docs.kalshi.com/
            # api-reference/portfolio/get-positions) has no plain "position"
            # or "net_contracts" key — the signed net-contracts field is
            # "position_fp" (a STRING; negative = NO contracts, positive =
            # YES contracts). Guessing at the wrong key here would silently
            # read every real position as flat (net=0), which is worse than
            # the bug this reconciliation exists to fix: it would report
            # "reconciled successfully" while still seeding from a wrong
            # zero baseline.
            try:
                net = int(float(pos.get("position_fp", 0) or 0))
            except (TypeError, ValueError):
                logger.error("MM pilot reconcile: unparsable position_fp %r "
                             "for %s — fail closed",
                             pos.get("position_fp"), ticker)
                self._reconciled = False
                return False
            if net == 0:
                continue
            net_map[ticker] = net
            # No average-cost field exists on market_positions at all (not
            # average_price_dollars, not avg_price) — derive a positive
            # per-contract basis from total dollar exposure over the signed
            # contract count (CodeRabbit round-3: the signed CONTRACT count
            # above is correct regardless, but net_usd() = net * avg, so an
            # avg of 0.0 would make USD-denominated caps read this ticker
            # as $0 exposure no matter how many contracts it holds —
            # silently bypassing MM_MAX_INVENTORY_USD /
            # MM_MAX_TOTAL_INVENTORY_USD / the gross cap for exactly the
            # ticker reconciliation just discovered real inventory on).
            # Fail closed toward OVER-estimating risk instead: when the
            # exposure field is missing or unparsable, assume the
            # worst-case $1.00/contract (the maximum possible price for a
            # binary contract) so a bad read trips caps EARLY rather than
            # masking real exposure as zero.
            raw_exposure = pos.get("market_exposure_dollars")
            if raw_exposure is None:
                avg_map[ticker] = 1.0
            else:
                try:
                    avg_map[ticker] = abs(float(raw_exposure)) / abs(net)
                except (TypeError, ValueError, ZeroDivisionError):
                    avg_map[ticker] = 1.0
        # restore() replaces the inventory wholesale under its own lock —
        # this IS the "seed from venue truth, not an assumed zero" fix.
        # Realized P&L has no live-venue source in scope here (that would
        # be get_settlements()/account history, beyond this minimum fix),
        # so carry forward the last persisted total rather than reset the
        # canary's cumulative-loss counter to zero on every restart — a
        # canary near its loss ceiling must stay near it across a crash.
        # No persisted file (fresh state_path, or first-ever run) means no
        # history to carry forward; the counter legitimately starts at 0.0.
        realized_map: dict = {}
        if persisted and persisted.get("inventory"):
            realized_map = dict(persisted["inventory"].get("realized") or {})
        self.inventory.restore({"net": net_map, "avg": avg_map,
                               "realized": realized_map})

        current_total = self.inventory.realized_pnl_total()
        current_day = self._utc_date()
        persisted_day = (persisted or {}).get("daily_pnl_date")
        persisted_opening = (persisted or {}).get("daily_pnl_opening_total")
        if persisted_day == current_day:
            try:
                opening_total = float(persisted_opening)
            except (TypeError, ValueError):
                opening_total = current_total
            if not math.isfinite(opening_total):
                opening_total = current_total
        else:
            opening_total = current_total
        with self._lock:
            self._daily_pnl_date = current_day
            self._daily_pnl_opening_total = opening_total

        with self._lock:
            self._orders = {}
            for fill in fills or []:
                fid = self._fill_id(fill)
                if fid:
                    self._mark_seen(fid)
            if fills:
                self._last_fill_ts = max(
                    _parse_created_ts(f, self._last_fill_ts) for f in fills)
            elif persisted and persisted.get("last_fill_ts"):
                self._last_fill_ts = max(self._last_fill_ts,
                                         float(persisted["last_fill_ts"]))

        self._reconciled = True
        logger.info("MM pilot reconciled at startup: %d ticker(s) with "
                    "inventory, %d prior resting order(s) cancelled, fills "
                    "checked since %s", len(self.inventory.tickers_with_inventory()),
                    len(open_orders or []), since_ts)
        self._persist_state()
        return True

    # -- choke point (spec section 5) ----------------------------------------

    def authorize_order(self, ticker: str, side: str, action: str,
                        count: int, price: float,
                        reducing: bool = False) -> GateResult:
        """The ONLY gate to ``place_order`` for the pilot. Deterministic,
        thread-safe.

        Checks, in order: kill-switch state, platform allowlist ("kalshi" in
        ENABLED_EXECUTION_PLATFORMS and platform hardcoded "kalshi"), halted
        flags, per-market caps (USD and contracts), total cap, gross cap, and
        per-order size. Reducing orders (orders that strictly decrease
        ``|net_inventory|``) bypass the inventory checks only — caps must
        never block the exit — but still pass kill-switch, platform, and size
        checks. Every rejection is logged and written to the decisions audit
        trail.
        """
        import config

        result = self._authorize(ticker, side, action, count, price, reducing,
                                 config)
        if not result.allowed:
            logger.warning("MM pilot order REJECTED (%s): %s %s %s x%d @ %.2f",
                           result.reason, ticker, action, side, count, price)
        self._write_decision("authorize_order", ticker, result.allowed,
                             result.reason, side=side, action=action,
                             count=count, price=price, reducing=reducing)
        return result

    def _authorize(self, ticker: str, side: str, action: str, count: int,
                   price: float, reducing: bool, config) -> GateResult:
        # 0. Startup reconciliation gate (finding #4). Refuse EVERY order —
        # including reducing orders — until reconcile() has confirmed live
        # venue state at least once this process lifetime. An unreconciled
        # restart has stale in-memory inventory and a fill cursor that can
        # silently skip fills from crash-to-restart downtime; a "reduce"
        # order computed off that stale state is not trustworthy either.
        if not self._reconciled:
            return GateResult(False, "not_reconciled")
        # 1. Kill switch (env flag + fresh control-plane cache)
        if not config.MM_KALSHI_PILOT_ENABLED:
            return GateResult(False, "kill_switch_env")
        if not self._controls.is_enabled():
            return GateResult(False, "kill_switch")
        if self._dashboard_paused():
            return GateResult(False, "dashboard_paused")
        # 2. Platform allowlist + hardcoded venue
        if PLATFORM not in config.ENABLED_EXECUTION_PLATFORMS:
            return GateResult(False, "platform_not_allowlisted")
        if self._fills_blind:
            return GateResult(False, "fills_blind")
        # 3. Per-order size and inventory-derived reduction status.
        if count < 1:
            return GateResult(False, "order_size")
        if price <= 0 or price >= 1:
            return GateResult(False, "order_price")
        notional = count * price
        if notional > config.MM_MAX_GROSS_PER_MARKET_USD:
            return GateResult(False, "order_size")
        signed = PilotInventory.signed_contracts(side, action, count)
        net_ct = self.inventory.net_contracts(ticker)
        derived_reducing = (
            net_ct != 0
            and (signed > 0) != (net_ct > 0)
            and count <= abs(net_ct)
        )
        if (not derived_reducing
                and self._daily_realized_pnl() <= -config.MM_MAX_DAILY_LOSS_USD):
            return GateResult(False, "daily_loss_limit")
        from kalshi_policy import live_kalshi_submit_allowed
        if not live_kalshi_submit_allowed(
            ticker, reducing=reducing or derived_reducing
        ):
            return GateResult(False, "policy_blocked")
        if not config.DRY_RUN:
            envelope = getattr(config, "LIVE_ENVELOPE", None)
            if not envelope:
                return GateResult(False, "live_envelope_missing")
            if envelope.get("venue") != "kalshi-d0":
                return GateResult(False, "envelope_venue")
            from live_envelope import pair_allowed
            if not (reducing or derived_reducing) and not pair_allowed(
                ticker, envelope["pair"]
            ):
                return GateResult(False, "envelope_pair")
        # 4. Halted flags. Caller-provided ``reducing=True`` never bypasses
        # a market halt unless direction and size prove it reduces inventory.
        if self.halted:
            return GateResult(False, "pilot_halted")
        if ticker in self._market_halted and not derived_reducing:
            return GateResult(False, "market_halted")
        if reducing:
            if not derived_reducing:
                reason = ("reducing_oversize" if net_ct != 0
                          and (signed > 0) != (net_ct > 0)
                          else "not_reducing")
                return GateResult(False, reason)
            # Inventory checks are bypassed only after reduction is derived
            # from current holdings; caps must never block a valid exit.
            return GateResult(True, "ok_reducing")
        # 5. Per-market inventory caps — both units, most restrictive wins.
        # An order whose fill moves |net| toward zero is reducing-direction
        # (the one-sided quote the section-5 table keeps alive at cap): it
        # passes the inventory checks; accumulating orders are capped.
        net_usd = abs(self.inventory.net_usd(ticker))
        projected_ct = net_ct + signed
        accumulating = abs(projected_ct) > abs(net_ct)
        if accumulating:
            if abs(projected_ct) > config.MM_MAX_INVENTORY_CONTRACTS:
                return GateResult(False, "per_market_contract_cap")
            if net_usd + notional > config.MM_MAX_INVENTORY_USD:
                return GateResult(False, "per_market_inventory_cap")
            # 6. Total cap
            if (self.inventory.total_net_usd() + notional
                    > config.MM_MAX_TOTAL_INVENTORY_USD):
                return GateResult(False, "total_inventory_cap")
            aggregate = (
                self.inventory.total_net_usd()
                + self._total_resting_notional()
                + notional
            )
            if aggregate > config.MM_MAX_TOTAL_INVENTORY_USD:
                return GateResult(False, "aggregate_notional_cap")
        # 7. Gross cap: inventory at cost + resting quote notional + this order
        gross = net_usd + self._resting_notional(ticker) + notional
        if gross > config.MM_MAX_GROSS_PER_MARKET_USD:
            return GateResult(False, "gross_cap")
        return GateResult(True, "ok")

    def place_pilot_order(self, ticker: str, side: str, action: str,
                          count: int, price: float, purpose: str,
                          reducing: bool = False) -> str | None:
        """Place one pilot order through the choke point.

        Returns the order id (synthetic in dry-run), or None on rejection or
        placement failure. This method contains the pilot's single reference
        to ``kalshi_client.place_order``.

        CodeRabbit round-3: the live venue round-trip runs OUTSIDE
        ``self._lock`` so a slow/blocked network call cannot hold up
        ``on_ws_price``/``update_book`` (a different thread — the WS feed
        handler — touching disjoint state, ``self._books``). The lock is
        held only for the authorization decision and, separately, to
        record the result into ``self._orders``. In the CURRENT
        architecture (one dedicated ``run_loop`` thread drives all
        quoting/hedging/fill-processing sequentially) nothing else calls
        this method concurrently, so releasing the lock between "authorized"
        and "recorded" is safe today; a future concurrent caller would
        reopen the check-then-act race the original full-method lock
        prevented, and would need its own de-duplication if introduced.
        """
        with self._lock:
            net_ct = self.inventory.net_contracts(ticker)
            if reducing:
                # Clamp at the held position before authorization and before
                # the venue call. A caller cannot turn ``reducing=True`` into
                # a position flip by asking to sell more than is held.
                unit_delta = PilotInventory.signed_contracts(
                    side, action, 1)
                if net_ct != 0 and (unit_delta > 0) != (net_ct > 0):
                    count = min(count, abs(net_ct))
            signed = PilotInventory.signed_contracts(side, action, count)
            derived_reducing = (
                net_ct != 0
                and (signed > 0) != (net_ct > 0)
                and count <= abs(net_ct)
            )
            # Preserve one reduction decision across authorization and the
            # client boundary. Otherwise an inventory-derived exit can pass
            # policy here but be rejected when KalshiClient re-checks policy.
            effective_reducing = reducing or derived_reducing
            verdict = self.authorize_order(ticker, side, action, count, price,
                                           reducing=effective_reducing)
            if not verdict.allowed:
                return None
            if self.dry_run:
                self._order_seq += 1
                order_id = f"dry_mmpilot_{ticker}_{purpose}_{self._order_seq}"
            elif self._client is None:
                # Fail closed: live mode without a client places nothing.
                logger.error("MM pilot live placement with no Kalshi client — "
                             "rejected (fail closed)")
                self._write_decision("place_order", ticker, False, "no_client")
                return None
            else:
                order_id = None  # placed live, below, outside the lock
                self.place_order_calls += 1

        if order_id is None:
            # TOCTOU guard (finding #2): G12's crossing check ran against a
            # CACHED book at gate-evaluation time; by the time this quote
            # reaches the exchange the live top-of-book may have moved
            # enough that the "resting" order we intend to place would
            # instead execute immediately as an unintended TAKER fill.
            # Kalshi's place_order endpoint used here has no confirmed
            # post-only/maker-only flag (research found `post_only` on
            # Kalshi's newer v2 events/orders endpoint, but could not
            # confirm it on the legacy endpoint this wrapper actually
            # calls — migrating endpoints is out of scope for this fix and
            # too risky to guess at for live order placement), so the
            # next-best guard is a fresh top-of-book fetch immediately
            # before submission, aborting if price would now cross. This
            # shrinks the race window from "one full refresh cycle" to
            # "one network round trip" — hedge orders are IOC/fill_or_kill
            # and are DELIBERATELY marketable, so they are exempt.
            if purpose != "hedge" and self._would_cross(ticker, side, action,
                                                         price):
                logger.warning(
                    "MM pilot quote placement aborted (TOCTOU guard): %s "
                    "%s %s @ %.2f would now cross the live book", ticker,
                    action, side, price)
                self._write_decision("place_order", ticker, False,
                                     "would_cross_toctou")
                return None
            # Quotes rest GTC; hedges are IOC-style fill_or_kill at touch
            # (unfilled hedges are caught by _check_pending_hedges).
            tif = "fill_or_kill" if purpose == "hedge" else "gtc"
            try:
                resp = self._client.place_order(
                    ticker=ticker, side=side, action=action, count=count,
                    price_dollars=price, time_in_force=tif,
                    reducing=effective_reducing,
                )
            except Exception:
                logger.exception("MM pilot place_order outcome indeterminate "
                                 "on %s", ticker)
                self._require_reconciliation(
                    f"indeterminate order placement on {ticker}")
                return None
            if resp is None:
                logger.error("MM pilot place_order returned no response on %s "
                             "— acceptance is indeterminate", ticker)
                self._require_reconciliation(
                    f"indeterminate order placement on {ticker}")
                return None
            order = resp.get("order", resp) if isinstance(resp, dict) else {}
            order_id = order.get("order_id") or order.get("id")
            if not order_id:
                logger.error("MM pilot place_order returned no id on %s — "
                             "acceptance is indeterminate", ticker)
                self._require_reconciliation(
                    f"indeterminate order placement on {ticker}")
                return None

        with self._lock:
            self._orders[order_id] = {
                "ticker": ticker,
                "side": side,
                "action": action,
                "count": count,
                "price": price,
                "purpose": purpose,
                "placed_at": self._time_fn(),
                # Monotonic placement time — _check_pending_hedges ages
                # hedge orders off this, not wall-clock `placed_at`, so NTP
                # steps / DST / a frozen clock can't defeat or false-trigger
                # the latency ceiling.
                "placed_mono": self._mono_fn(),
            }
        self._persist_state()
        return order_id

    MAX_CANCEL_ATTEMPTS = 3

    def _cancel_order(self, order_id: str) -> bool:
        """Cancel one resting pilot order — exchange FIRST, registry second.

        The registry entry is popped only after the exchange confirms the
        cancel; a failed cancel stays in the registry (flagged
        ``pending_cancel``) so it remains visible to fill attribution, gross
        accounting, and retries. After MAX_CANCEL_ATTEMPTS failures the pilot
        halts with a persistent CRITICAL alert — a live GTC order we cannot
        cancel is an unbounded exposure.
        """
        with self._lock:
            info = self._orders.get(order_id)
        if info is None:
            return True
        if self.dry_run or order_id.startswith("dry_"):
            with self._lock:
                self._orders.pop(order_id, None)
            self._persist_state()
            return True
        ok = False
        try:
            ok = bool(self._client.cancel_order(order_id))
        except Exception:
            logger.exception("MM pilot cancel_order raised for %s", order_id)
        if ok:
            with self._lock:
                self._orders.pop(order_id, None)
            self._persist_state()
            return True
        with self._lock:
            info["pending_cancel"] = True
            info["cancel_attempts"] = info.get("cancel_attempts", 0) + 1
            attempts = info["cancel_attempts"]
            # Exponential backoff before the next retry (1s, 2s, 4s, ...,
            # capped at 30s) so a stuck cancel doesn't hammer the exchange
            # every run-loop pass while still recovering promptly once the
            # venue is responsive again.
            backoff = min(2 ** (attempts - 1), 30)
            info["next_retry_at"] = self._time_fn() + backoff
        logger.warning("MM pilot cancel FAILED for %s (attempt %d/%d) — "
                       "order stays in registry, retry in %ds",
                       order_id, attempts, self.MAX_CANCEL_ATTEMPTS, backoff)
        if attempts >= self.MAX_CANCEL_ATTEMPTS:
            self._alert("MM_PILOT_CANCEL_STUCK", "CRITICAL",
                        f"MM pilot could not cancel live order {order_id} "
                        f"after {attempts} attempts — order may still be "
                        f"resting on Kalshi; manual intervention required",
                        {"order_id": order_id, "ticker": info.get("ticker")})
            self.halt_all(f"uncancellable live order {order_id}")
        return False

    def _retry_pending_cancels(self) -> None:
        """Retry exchange cancels that previously failed, respecting each
        order's backoff window. Intended to run once per run-loop pass so a
        cancel that failed transiently (network blip, momentary 5xx) gets
        confirmed and popped from the registry without waiting for the next
        cancel/replace cycle on that specific market."""
        now = self._time_fn()
        with self._lock:
            pending = [oid for oid, info in self._orders.items()
                       if info.get("pending_cancel")
                       and now >= info.get("next_retry_at", 0.0)]
        for oid in pending:
            self._cancel_order(oid)

    def pull_market(self, ticker: str, reason: str) -> int:
        """Cancel every resting pilot order in a market (fail closed)."""
        order_ids = [o["order_id"] for o in self.resting_orders(ticker)]
        for oid in order_ids:
            self._cancel_order(oid)
        if order_ids:
            logger.info("MM pilot pulled %d quotes on %s (%s)",
                        len(order_ids), ticker, reason)
        return len(order_ids)

    def pull_all(self, reason: str) -> int:
        order_ids = [o["order_id"] for o in self.resting_orders()]
        for oid in order_ids:
            self._cancel_order(oid)
        if order_ids:
            logger.warning("MM pilot pulled ALL %d resting orders (%s)",
                           len(order_ids), reason)
        return len(order_ids)

    # -- halt machinery ------------------------------------------------------

    def halt_market(self, ticker: str, reason: str) -> None:
        """Halt one market: pull quotes, refuse to re-quote until manual reset."""
        import config
        self.pull_market(ticker, reason)
        with self._lock:
            self._market_halted[ticker] = reason
            times = self._market_halt_times.setdefault(ticker, [])
            now = self._time_fn()
            times.append(now)
            window = config.MM_HALT_WINDOW_SECONDS
            recent = [t for t in times if now - t <= window]
            self._market_halt_times[ticker] = recent
        self._alert("MM_PILOT_MARKET_HALT", "WARNING",
                    f"MM pilot market {ticker} halted: {reason}",
                    {"ticker": ticker, "reason": reason})
        self._write_decision("halt_market", ticker, False, reason)
        if len(recent) >= 2:
            self.halt_all(f"market {ticker} halted twice within "
                          f"{int(window)}s ({reason})")

    def halt_all(self, reason: str) -> None:
        """HALT the whole pilot: cancel everything; manual restart required."""
        with self._lock:
            if self.halted:
                return
            self.halted = True
            self.halt_reason = reason
        self.pull_all(reason)
        self._alert("MM_PILOT_HALT", "CRITICAL",
                    f"MM pilot HALTED: {reason} — manual restart required",
                    {"reason": reason})
        self._write_decision("halt_all", "*", False, reason)

    def _require_reconciliation(self, reason: str) -> None:
        """Halt and invalidate venue truth after an indeterminate event."""
        with self._lock:
            self._reconciled = False
        self.halt_all(reason)

    # -- gate chain (spec section 6) ------------------------------------------

    def _evaluate_gates(self, ticker: str) -> dict:
        """Run gates G1-G12 (pre-placement subset) in fixed order.

        Returns ``{"action": "quote"|"skip"|"pull"|"halted", "reason": str,
        "one_side": None|"bid_only"|"ask_only", "no_new_quotes": bool}``.
        First failure short-circuits. No network calls in here — inputs are
        pre-fetched state.
        """
        import config

        def gate(name: str, ok: bool, reason: str) -> bool:
            self._write_decision(name, ticker, ok, reason)
            return ok

        # G0 startup reconciliation (finding #4). Not part of the spec's
        # numbered G1-G12 chain; this is a pre-check so an unreconciled
        # pilot gets a clear audit-trail reason here rather than only
        # discovering the block later at authorize_order (the actual hard
        # choke point — this is belt-and-suspenders, not the enforcement).
        if not self._reconciled:
            gate("G0_reconciled", False, "not_reconciled")
            return {"action": "pull", "reason": "not_reconciled"}
        if self._fills_blind:
            gate("G0b_fill_sight", False, "fills_blind")
            return {"action": "pull", "reason": "fills_blind"}
        # G1 kill switch
        g1 = (
            config.MM_KALSHI_PILOT_ENABLED
            and self._controls.is_enabled()
            and not self._dashboard_paused()
        )
        if not gate("G1_kill_switch", g1, "ok" if g1 else "kill_switch"):
            self.halt_all("kill switch off or control plane stale")
            return {"action": "halted", "reason": "kill_switch"}
        # G2 venue allowlist
        g2 = PLATFORM in config.ENABLED_EXECUTION_PLATFORMS
        if not gate("G2_venue_allowlist", g2,
                    "ok" if g2 else "platform_not_allowlisted"):
            self.halt_all("kalshi missing from ENABLED_EXECUTION_PLATFORMS "
                          "(config error)")
            return {"action": "halted", "reason": "platform_not_allowlisted"}
        # G3 halted flags
        g3 = not self.halted and ticker not in self._market_halted
        if not gate("G3_market_halted", g3, "ok" if g3 else "market_halted"):
            return {"action": "skip", "reason": "market_halted"}
        # G4 still selected (fail closed when no snapshot has ever arrived)
        with self._lock:
            selected = self._selected
        g4 = selected is not None and ticker in selected
        if not gate("G4_still_selected", g4, "ok" if g4 else "deselected"):
            return {"action": "pull", "reason": "deselected"}
        # G5 price band + G6 book staleness
        book = self._book(ticker)
        mid = book.get("mid") if book else None
        if book is None or mid is None:
            gate("G6_book_staleness", False, "book_missing")
            return {"action": "pull", "reason": "book_missing"}
        g5 = config.LIP_PRICE_BAND_LOW <= mid <= config.LIP_PRICE_BAND_HIGH
        if not gate("G5_price_band", g5, "ok" if g5 else "price_band"):
            return {"action": "pull", "reason": "price_band"}
        age = self._time_fn() - book["updated_at"]
        g6 = age <= config.MM_BOOK_MAX_STALE_SECONDS
        if not gate("G6_book_staleness", g6, "ok" if g6 else "book_stale"):
            return {"action": "pull", "reason": "book_stale"}
        # G6b book LEVELS staleness — distinct from mid/price freshness
        # above. WS mid ticks refresh `updated_at` far more often than REST
        # book fetches refresh actual resting-order levels; G11 depth
        # sizing and G12 crossing guard (both later in refresh_market)
        # consume levels (yes_bid/no_bid/yes_ask), so they must never run
        # on levels older than MM_BOOK_MAX_STALE_SECONDS even while price
        # looks fresh from WS. Failing here (before refresh_market reaches
        # G11/G12) is what makes that guarantee airtight.
        levels_age = self._time_fn() - book.get("levels_updated_at", 0.0)
        g6b = levels_age <= config.MM_BOOK_MAX_STALE_SECONDS
        if not gate("G6b_book_levels_staleness", g6b,
                    "ok" if g6b else "book_levels_stale"):
            return {"action": "pull", "reason": "book_levels_stale"}
        # G7 toxic-flow pause
        g7 = not self._toxic.should_pause(ticker)
        if not gate("G7_toxic_flow", g7, "ok" if g7 else "toxic_flow_pause"):
            return {"action": "pull", "reason": "toxic_flow_pause"}
        # G8 volatility ceiling (G9 widening is applied inside QuoteEngine).
        # Insufficient samples must fail closed — get_volatility() returns
        # 0.0 (the calmest possible reading) both when a market is truly
        # calm and when there simply isn't enough data yet; treating warm-up
        # as "calm" would let a fast mover quote at base spread before there
        # is any real data to judge it by.
        has_samples = self._vol.has_min_samples(ticker)
        multiplier = self._vol.get_spread_multiplier(ticker)
        g8 = has_samples and multiplier < config.MM_VOL_PULL_MULTIPLIER
        if not gate("G8_volatility_ceiling", g8,
                    "ok" if g8 else
                    ("insufficient_samples" if not has_samples
                     else "volatility_ceiling")):
            return {"action": "pull", "reason": "volatility_ceiling"}
        gate("G9_volatility_widening", True,
             f"multiplier={multiplier:.2f}")
        # G10 inventory caps — stop / one-side, not clamp-and-continue
        one_side = None
        net_ct = self.inventory.net_contracts(ticker)
        net_usd = self.inventory.net_usd(ticker)
        over_per_market = (abs(net_ct) >= config.MM_MAX_INVENTORY_CONTRACTS or
                           abs(net_usd) >= config.MM_MAX_INVENTORY_USD)
        if over_per_market:
            one_side = "ask_only" if net_ct > 0 else "bid_only"
        total = self.inventory.total_net_usd()
        if total >= config.MM_MAX_TOTAL_INVENTORY_USD:
            # Stop the accumulating side in EVERY market; a flat market has
            # no inventory-reducing quote, so it places nothing.
            if net_ct > 0:
                one_side = "ask_only"
            elif net_ct < 0:
                one_side = "bid_only"
            else:
                gate("G10_inventory_caps", False, "total_inventory_cap")
                return {"action": "pull", "reason": "total_inventory_cap"}
        gross = abs(net_usd) + self._resting_notional(ticker)
        no_new_quotes = False
        if gross > config.MM_MAX_GROSS_PER_MARKET_USD:
            # Cancel newest resting orders until gross < cap; no new quotes.
            orders = sorted(self.resting_orders(ticker),
                            key=lambda o: o["placed_at"], reverse=True)
            for order in orders:
                if gross <= config.MM_MAX_GROSS_PER_MARKET_USD:
                    break
                self._cancel_order(order["order_id"])
                gross -= order["count"] * order["price"]
            no_new_quotes = True
            gate("G10_inventory_caps", False, "gross_cap")
        else:
            gate("G10_inventory_caps", True,
                 f"one_side={one_side or 'none'}")
        if no_new_quotes:
            return {"action": "skip", "reason": "gross_cap"}
        return {"action": "quote", "reason": "ok", "one_side": one_side}

    # -- quoting ---------------------------------------------------------------

    def _quote_size_usd(self) -> float:
        """Canary size until graduation (spec section 8); pilot size after."""
        import config
        if self.canary_graduated:
            return config.MM_QUOTE_SIZE_USD
        return config.MM_CANARY_QUOTE_SIZE_USD

    @staticmethod
    def _round_tick(price: float) -> float:
        return round(round(price / KALSHI_TICK) * KALSHI_TICK, 2)

    def refresh_market(self, ticker: str) -> list[str]:
        """Gate chain -> quotes -> choke point -> cancel/replace GTC orders.

        Returns the list of order ids placed this cycle (possibly empty).
        """
        import config

        if self._client is not None:
            try:
                self.update_book(ticker, self._client.fetch_order_book(ticker))
            except Exception:
                logger.exception("MM pilot book fetch failed for %s", ticker)

        plan = self._evaluate_gates(ticker)
        if plan["action"] in ("pull", "halted"):
            if plan["action"] == "pull":
                self.pull_market(ticker, plan["reason"])
            return []
        if plan["action"] == "skip":
            return []

        book = self._book(ticker)
        mid = book["mid"]
        quotes = self._quote_engine.calculate_quotes(
            mid,
            inventory=self.inventory.net_usd(ticker),
            max_inventory=config.MM_MAX_INVENTORY_USD,
            market_key=ticker,  # G9: volatility widening hook
        )
        bid = self._round_tick(quotes["bid"])
        ask = self._round_tick(quotes["ask"])

        # G12 crossing guard (post-only semantics): never a marketable quote.
        best_yes_ask = book.get("yes_ask")
        best_yes_bid = book.get("yes_bid")
        skip_bid = skip_ask = False
        if best_yes_ask is not None and bid >= best_yes_ask[0]:
            bid = self._round_tick(best_yes_ask[0] - KALSHI_TICK)
            if bid < KALSHI_TICK:
                skip_bid = True
        if best_yes_bid is not None and ask <= best_yes_bid[0]:
            ask = self._round_tick(best_yes_bid[0] + KALSHI_TICK)
            if ask > 1.0 - KALSHI_TICK:
                skip_ask = True
        self._write_decision("G12_crossing_guard", ticker,
                             not (skip_bid and skip_ask),
                             f"bid={bid:.2f} ask={ask:.2f} "
                             f"skip_bid={skip_bid} skip_ask={skip_ask}")

        one_side = plan.get("one_side")
        if one_side == "ask_only":
            skip_bid = True
        elif one_side == "bid_only":
            skip_ask = True

        size_usd = self._quote_size_usd()

        # G11 depth sizing: quote count <= fraction of same-side best size.
        # Fail closed when depth is unknown: a side with no resting size to
        # measure against must be skipped, never fall through to full
        # notional sizing (that would defeat the entire depth cap).
        def _sized_count(price: float, best: tuple | None) -> int:
            if price <= 0:
                return 0
            if best is None:
                return 0
            count = int(size_usd / price)
            depth_cap = int(config.MM_MAX_BOOK_DEPTH_FRACTION * best[1])
            return min(count, depth_cap)

        # Our bid = buy YES at `bid`; same side of the book = resting YES bids.
        bid_count = 0 if skip_bid else _sized_count(bid, book.get("yes_bid"))
        # Our ask = buy NO at (1 - ask); same side = resting NO bids.
        no_price = self._round_tick(1.0 - ask)
        ask_count = 0 if skip_ask else _sized_count(no_price, book.get("no_bid"))
        self._write_decision("G11_depth_sizing", ticker,
                             bid_count >= 1 or ask_count >= 1,
                             f"bid_count={bid_count} ask_count={ask_count}")

        # Cancel/replace: pull existing quote orders, then place fresh GTC.
        for order in self.resting_orders(ticker):
            if order["purpose"] in ("quote_bid", "quote_ask"):
                if not self._cancel_order(order["order_id"]):
                    logger.warning("MM pilot quote refresh aborted on %s: "
                                   "existing order %s is still live",
                                   ticker, order["order_id"])
                    return []

        placed: list[str] = []
        if bid_count >= 1:
            oid = self.place_pilot_order(ticker, side="yes", action="buy",
                                         count=bid_count, price=bid,
                                         purpose="quote_bid")
            if oid:
                placed.append(oid)
        if ask_count >= 1 and 0 < no_price < 1:
            oid = self.place_pilot_order(ticker, side="no", action="buy",
                                         count=ask_count, price=no_price,
                                         purpose="quote_ask")
            if oid:
                placed.append(oid)
        return placed

    def refresh_all(self) -> list[str]:
        if self._fills_blind:
            return []
        placed: list[str] = []
        for ticker in self.pilot_tickers():
            if self.halted:
                break
            placed.extend(self.refresh_market(ticker))
        return placed

    # -- fill detection (spec section 3) ----------------------------------------

    def _fill_id(self, fill: dict) -> str | None:
        """Unique fill id, or None when the record has no trade_id.

        No fallback key: (order_id, created_time, count) collapses distinct
        partial fills that share a second and a size. The caller halts on
        None — fail closed beats undercounting inventory.
        """
        fid = fill.get("trade_id")
        return str(fid) if fid else None

    def _mark_seen(self, fid: str) -> None:
        self._seen_fill_ids[fid] = None
        while len(self._seen_fill_ids) > 1000:
            self._seen_fill_ids.pop(next(iter(self._seen_fill_ids)))

    def poll_fills(self) -> list[FillEvent]:
        """One fill-poll cycle: fetch, dedupe, attribute, process."""
        if self.dry_run:
            raw_fills = self._simulate_dry_fills()
        else:
            if self._client is None:
                return []
            min_ts = int(self._last_fill_ts - 60)  # 60s overlap window
            try:
                raw_fills = self._client.get_fills(min_ts=min_ts,
                                                   raise_on_error=True)
            except Exception:
                logger.exception("MM pilot get_fills failed")
                self._on_fill_poll_failure()
                return []
        # A successful poll restores fill sight.
        if self._fill_poll_failures or self._fills_blind:
            logger.info("MM pilot fill polling recovered after %d failures",
                        self._fill_poll_failures)
        self._fill_poll_failures = 0
        self._fills_blind = False
        events: list[FillEvent] = []
        pilot_markets = set(self.pilot_tickers())
        for fill in reversed(raw_fills):  # oldest first
            fid = self._fill_id(fill)
            if fid is None:
                # No trade_id: any fallback key risks collapsing distinct
                # partial fills into one (silent inventory undercount).
                # Fail closed rather than guess.
                self.halt_all("fill without trade_id — dedupe unsafe, "
                              "inventory accounting cannot be trusted")
                return events
            if fid in self._seen_fill_ids:
                continue
            ticker = fill.get("ticker", "")
            order_id = str(fill.get("order_id", ""))
            with self._lock:
                known = order_id in self._orders
                info = self._orders.get(order_id)
            if not known:
                if ticker in pilot_markets:
                    # A fill on an unknown order_id in a pilot market is a
                    # deviation: something else is trading our markets on
                    # this account (spec sections 3 and 8).
                    self._mark_seen(fid)
                    self.halt_all(f"fill on unknown order_id {order_id} in "
                                  f"pilot market {ticker}")
                    return events
                continue  # someone else's market — not ours to account
            detect_wall = self._time_fn()
            detect_mono = self._mono_fn()
            event = self._build_event(fid, fill, info)
            if event is None:
                self._require_reconciliation(
                    f"unparseable fill {fid} on known order {order_id}")
                return events
            self._mark_seen(fid)
            self._last_fill_ts = max(self._last_fill_ts, event.created_ts)
            events.append(event)
            self._process_fill(event, info, detect_wall=detect_wall,
                               detect_mono=detect_mono)
        if not self.halted:
            self._check_pending_hedges()
        self._persist_state()
        return events

    FILL_POLL_FAILURE_LIMIT = 3

    def _on_fill_poll_failure(self) -> None:
        """Fail closed on fill blindness: quoting without fill sight means
        inventory, caps, and toxicity are all running on stale truth."""
        self._fill_poll_failures += 1
        if (self._fill_poll_failures >= self.FILL_POLL_FAILURE_LIMIT
                and not self._fills_blind):
            self._fills_blind = True
            self.pull_all("fill polling blind")
            self._alert(
                "MM_PILOT_FILLS_BLIND", "CRITICAL",
                f"MM pilot fill polling failed "
                f"{self._fill_poll_failures}x consecutively — quotes pulled, "
                f"no quoting until polling recovers",
                {"failures": self._fill_poll_failures})
            self._write_decision("fill_poll", "*", False, "fills_blind")

    def _check_pending_hedges(self) -> None:
        """Cancel-remainder path: a hedge order still resting past the latency
        ceiling means the position was NOT flattened — hedge failure, fail
        closed (spec section 4). Ages on the monotonic clock."""
        import config
        now_mono = self._mono_fn()
        for order in self.resting_orders():
            if order["purpose"] != "hedge":
                continue
            age = now_mono - order.get("placed_mono", now_mono)
            if age <= config.MM_HEDGE_MAX_LATENCY_SECONDS:
                continue
            self._cancel_order(order["order_id"])  # cancel remainder
            ticker = order["ticker"]
            if not self.canary_graduated:
                self.halt_all(f"hedge order unfilled past latency ceiling on "
                              f"{ticker}")
                return
            self.halt_market(ticker, "hedge order unfilled past latency ceiling")

    def _build_event(self, fid: str, fill: dict, info: dict) -> FillEvent | None:
        from kalshi_vip import fill_price_dollars
        price = fill_price_dollars(fill)
        if price is None:
            logger.warning("MM pilot fill %s has no price — ignored", fid)
            return None
        book = self._book(info["ticker"])
        mid = (book or {}).get("mid") or price
        try:
            count = int(fill.get("count", 0))
        except (TypeError, ValueError):
            count = 0
        if count <= 0:
            return None
        return FillEvent(
            fill_id=fid,
            order_id=str(fill.get("order_id", "")),
            ticker=info["ticker"],
            side=str(fill.get("side", info["side"])),
            action=str(fill.get("action", info["action"])),
            count=count,
            price=price,
            is_taker=bool(fill.get("is_taker", False)),
            created_ts=_parse_created_ts(fill, self._time_fn()),
            mid_at_detect=float(mid),
        )

    # -- fill processing / canary (spec sections 4 and 8) ------------------------

    def _process_fill(self, event: FillEvent, order_info: dict,
                      detect_wall: float | None = None,
                      detect_mono: float | None = None) -> None:
        import config

        if detect_wall is None:
            detect_wall = self._time_fn()
        if detect_mono is None:
            detect_mono = self._mono_fn()
        purpose = order_info.get("purpose", "")
        is_hedge = purpose == "hedge"

        # Fill accounting (registry, inventory, log) runs UNCONDITIONALLY,
        # before ANY halt decision below — including the taker-fill
        # deviation check that immediately follows. Codex round-3: this
        # check used to halt_all() and `return` BEFORE any accounting
        # ran, leaving the local inventory view wrong at exactly the
        # moment — going into a halt for investigation — it most needs
        # to be right, with the fill itself unrecorded and effectively
        # invisible. A fill already happened at the exchange; halting
        # must stop FUTURE quoting/hedging, it must never cause the
        # system to forget what already occurred (mirrors the
        # cancel-then-pop pattern used elsewhere in this branch: never
        # discard/skip state before it's durably recorded).

        # Registry maintenance: shrink/remove the filled order.
        with self._lock:
            live = self._orders.get(event.order_id)
            if live is not None:
                live["count"] -= event.count
                if live["count"] <= 0:
                    self._orders.pop(event.order_id, None)

        # 1. Inventory update (signed dollar delta / avg-cost accounting).
        self.inventory.apply_fill(event.ticker, event.side, event.action,
                                  event.count, event.price)

        # Trade log: strategy-tagged from trade one (operating rule 5).
        self._log_fill(event)
        side_price = event.price if event.side == "yes" else 1.0 - event.price
        notional = event.count * side_price

        # Durably persist the corrected registry/inventory NOW, before
        # the taker-fill halt check below — a crash immediately after
        # halting must still be able to reconcile from the right numbers
        # on restart, not from whatever was last saved before this fill.
        self._persist_state()

        # A halt suppresses future quoting and hedging, but not accounting.
        # poll_fills continues to feed every known-order fill through the
        # unconditional registry/inventory/log block above.
        if self.halted:
            return

        # Canary deviation: a resting quote should never be the taker.
        # Fill accounting above already ran and was persisted, so halting
        # here only stops FUTURE activity (quoting, hedging, toxicity/
        # canary bookkeeping for this event) — it never discards or
        # delays recording what already happened.
        if event.is_taker and not is_hedge:
            self.halt_all(f"taker fill on resting quote {event.order_id}")
            return

        # 2. Toxicity feed (quote fills only — hedges are deliberate takers).
        toxicity_failed = False
        if not is_hedge:
            quote_side = "bid" if purpose == "quote_bid" else "ask"
            try:
                self._toxic.record_fill(event.ticker, quote_side, event.price,
                                        notional, event.mid_at_detect)
                if (self._toxic.get_toxicity(event.ticker)
                        >= config.MM_TOXIC_FLOW_THRESHOLD):
                    self._toxic.trigger_pause(event.ticker)
            except Exception:
                logger.exception("MM pilot toxicity record failed for %s",
                                 event.ticker)
                self.halt_market(event.ticker, "toxicity accounting failed")
                toxicity_failed = True
                if self.halted:
                    return

        # 3. Always-on UTC-day loss accounting. The operator envelope is a
        # permanent ceiling, not a canary-only rollout threshold.
        daily_realized = self._daily_realized_pnl()
        if daily_realized <= -config.MM_MAX_DAILY_LOSS_USD:
            label = "canary realized P&L" if not self.canary_graduated else "daily realized P&L"
            self.halt_all(
                f"{label} ${daily_realized:.2f} at or below daily ceiling "
                f"-${config.MM_MAX_DAILY_LOSS_USD:.2f}"
            )
            return

        # Canary accounting (deviation checks halt the whole pilot).
        # The canary keeps its own tighter lifetime ceiling until graduation.
        if not self.canary_graduated:
            realized = self.inventory.realized_pnl_total()
            if realized < -config.MM_CANARY_MAX_LOSS_USD:
                self.halt_all(f"canary realized P&L ${realized:.2f} below "
                              f"-${config.MM_CANARY_MAX_LOSS_USD:.2f}")
                return
        if not is_hedge and not self._check_canary(event, notional):
            return

        # Post-trade cap observation: caps are pre-trade; a breach observed
        # here means the choke point leaked — the worst possible bug.
        if self._observed_cap_breach(event.ticker):
            self.halt_all(f"post-trade inventory above cap on {event.ticker} "
                          "(choke point leak)")
            return

        # Multi-market toxicity deviation.
        if not toxicity_failed and self._multi_market_toxicity():
            self.halt_all("toxicity over threshold on >=2 pilot markets")
            return

        # 4. Hedge decision (skip re-hedging on the hedge's own fill).
        if not is_hedge:
            self._hedge_on_fill(event, detect_wall=detect_wall,
                                detect_mono=detect_mono)

        # Graduation check rides on fill processing and the run loop.
        self._maybe_graduate()
        self._persist_state()

    def _log_fill(self, event: FillEvent) -> None:
        if self._db is None:
            return
        try:
            with self._lock:
                opp_ids = getattr(self, "_opp_ids", None)
                if opp_ids is None:
                    opp_ids = self._opp_ids = {}
                opp_id = opp_ids.get(event.ticker)
            if opp_id is None:
                opp_id = self._db.log_opportunity(
                    opp_type="KalshiMMPilot", market=event.ticker,
                    prices="", total_cost=0.0, net_profit=0.0, net_roi=0.0,
                    depth=0.0, action="quote",
                ) or 0
                with self._lock:
                    self._opp_ids[event.ticker] = opp_id
            side_price = event.price if event.side == "yes" else 1.0 - event.price
            self._db.log_trade(
                opportunity_id=opp_id, platform=PLATFORM,
                side=f"{event.action}_{event.side}", price=side_price,
                size=event.count * side_price, status="filled",
                fill_price=side_price, order_id=event.order_id,
            )
        except Exception:
            logger.exception("MM pilot fill DB log failed")

    def _check_canary(self, event: FillEvent, notional: float) -> bool:
        """Canary bookkeeping. Returns False when a deviation halted the pilot."""
        import config
        if self.canary_graduated:
            return True
        if notional > config.MM_CANARY_QUOTE_SIZE_USD + 0.01:
            self.halt_all(f"canary fill ${notional:.2f} exceeds canary size "
                          f"${config.MM_CANARY_QUOTE_SIZE_USD:.2f} on "
                          f"{event.ticker}")
            return False
        self.canary_clean_fills += 1
        return True

    def _maybe_graduate(self) -> None:
        import config
        if self.canary_graduated or self.halted or self.dry_run:
            return
        runtime_h = (self._time_fn() - self._live_started_ts) / 3600.0
        if (self.canary_clean_fills >= config.MM_CANARY_FILLS
                and runtime_h >= config.MM_CANARY_MIN_HOURS):
            self.canary_graduated = True
            self._write_decision("canary", "*", True, "CANARY PASSED",
                                 clean_fills=self.canary_clean_fills,
                                 runtime_hours=round(runtime_h, 2))
            self._alert("MM_PILOT_CANARY_PASSED", "INFO",
                        f"MM pilot canary passed after "
                        f"{self.canary_clean_fills} clean fills / "
                        f"{runtime_h:.1f}h — graduating to pilot size")

    def _observed_cap_breach(self, ticker: str) -> bool:
        import config
        return (abs(self.inventory.net_contracts(ticker))
                > config.MM_MAX_INVENTORY_CONTRACTS
                or abs(self.inventory.net_usd(ticker))
                > config.MM_MAX_INVENTORY_USD + 0.01
                or self.inventory.total_net_usd()
                > config.MM_MAX_TOTAL_INVENTORY_USD + 0.01)

    def _multi_market_toxicity(self) -> bool:
        import config
        toxic = 0
        for ticker in self.pilot_tickers():
            try:
                if self._toxic.get_toxicity(ticker) >= config.MM_TOXIC_FLOW_THRESHOLD:
                    toxic += 1
            except Exception as exc:
                logger.error("MM pilot toxicity check failed for %s: %s — "
                             "halting market", ticker, exc)
                self.halt_market(ticker, "toxicity state unavailable")
                continue
        return toxic >= 2

    # -- auto-hedge (spec section 4) -----------------------------------------------

    def _hedge_on_fill(self, event: FillEvent, detect_wall: float | None = None,
                      detect_mono: float | None = None) -> None:
        """Hedge decision per fill: deadband rebalance or reducing order.

        ``detect_mono`` is the monotonic timestamp captured when the fill
        was detected (poll_fills); hedge latency is measured against it with
        ``self._mono_fn()``, never wall-clock deltas — see finding #10
        (mm_pilot.py hedge-latency monotonic-clock fix). ``detect_wall`` is
        carried through only for human-readable audit-trail context.
        """
        import config

        if detect_mono is None:
            detect_mono = self._mono_fn()
        if detect_wall is None:
            detect_wall = self._time_fn()

        net_usd = self.inventory.net_usd(event.ticker)
        excess = abs(net_usd) - config.MM_INVENTORY_TARGET_USD
        if excess <= config.MM_HEDGE_DEADBAND_USD:
            # Rebalance arm: inventory skew in QuoteEngine works it off.
            self._write_decision("hedge", event.ticker, True, "hedge_deadband",
                                 net_usd=round(net_usd, 2))
            return

        net_ct = self.inventory.net_contracts(event.ticker)
        # Long YES exposure -> sell yes at the bid; long NO -> sell no.
        hold_side = "yes" if net_ct > 0 else "no"
        avg = self.inventory.avg_cost(event.ticker)

        success = False
        error: str = ""
        for attempt in (1, 2):  # retry once on failure (spec section 4 table)
            try:
                success = self._hedger.hedge_inventory(
                    market_key=event.ticker,
                    platform=PLATFORM,
                    side=hold_side,
                    fill_price=avg,
                    size=excess,
                    token_id=event.ticker,
                    reduce_action="sell",
                    # Hard ceiling from OUR OWN inventory tracker (never
                    # recomputed from dollars/price inside the hedger) — a
                    # reduce order must never be sized past the position it
                    # is reducing, regardless of how far price has moved
                    # since entry (finding #5).
                    max_contracts=abs(net_ct),
                )
            except Exception as exc:
                success = False
                error = str(exc)
                logger.exception("MM pilot hedge attempt %d raised on %s",
                                 attempt, event.ticker)
            if success:
                break

        # Latency = (wall-clock staleness of the fill AT THE MOMENT we
        # detected it) + (monotonic elapsed time from detection to hedge
        # completion, which may span one or two hedge attempts).
        #
        # The first term is unavoidably wall-clock — event.created_ts is a
        # venue-reported timestamp with no monotonic equivalent on our side
        # — but it is computed ONCE, right at detection (detect_wall was
        # captured in poll_fills before any hedge attempt ran). The second
        # term is what the ORIGINAL bug got wrong: it re-read the wall
        # clock again AFTER a possibly-slow hedge attempt
        # (self._time_fn() - event.created_ts, computed post-attempt), so an
        # NTP step, DST transition, or frozen/mocked clock during that
        # window could silently defeat or false-trigger the ceiling.
        # Measuring the processing window on the monotonic clock instead
        # closes that gap while preserving the original "how stale is this
        # fill overall" semantics.
        detection_lag = max(0.0, detect_wall - event.created_ts)
        reaction_latency = self._mono_fn() - detect_mono
        latency = detection_lag + reaction_latency
        latency_exceeded = latency > config.MM_HEDGE_MAX_LATENCY_SECONDS
        self._write_decision("hedge", event.ticker, success and not latency_exceeded,
                             "hedged" if success else (error or "hedge_failed"),
                             excess=round(excess, 2),
                             latency_s=round(latency, 2),
                             detected_at=round(detect_wall, 3))

        if latency_exceeded:
            # Canary deviation 4: latency ceiling exceeded even if the hedge
            # eventually landed.
            if not self.canary_graduated:
                self.halt_all(f"hedge latency {latency:.1f}s exceeded "
                              f"{config.MM_HEDGE_MAX_LATENCY_SECONDS:.0f}s "
                              f"ceiling on {event.ticker}")
                return
            if success:
                self.halt_market(event.ticker,
                                 f"hedge latency {latency:.1f}s over ceiling")
                return
            success = False

        if not success:
            # Fail closed: a market carrying inventory it cannot hedge never
            # keeps live quotes. Any hedge failure is a canary deviation.
            if not self.canary_graduated:
                self.halt_all(f"hedge failure on {event.ticker}")
            else:
                self.halt_market(event.ticker, "hedge failure")

    # -- dry-run fill simulation (rollout phase D0) ------------------------------

    def _simulate_dry_fills(self) -> list[dict]:
        """Synthesize FillEvents from book crosses against dry-run orders."""
        fills: list[dict] = []
        now = self._time_fn()
        for order in self.resting_orders():
            ticker = order["ticker"]
            book = self._book(ticker)
            if not book or book.get("mid") is None:
                continue
            mid = book["mid"]
            crossed = False
            if order["purpose"] == "hedge":
                crossed = True  # IOC at touch: assume immediate execution
            elif order["purpose"] == "quote_bid" and mid <= order["price"]:
                crossed = True
            elif order["purpose"] == "quote_ask" and mid >= 1.0 - order["price"]:
                crossed = True
            if not crossed:
                continue
            self._order_seq += 1
            yes_price = (order["price"] if order["side"] == "yes"
                         else 1.0 - order["price"])
            fills.append({
                "trade_id": f"dryfill_{self._order_seq}",
                "order_id": order["order_id"],
                "ticker": ticker,
                "side": order["side"],
                "action": order["action"],
                "count": order["count"],
                "yes_price": int(round(yes_price * 100)),
                "is_taker": order["purpose"] == "hedge",
                "created_time": now,
            })
        return fills

    # -- lifecycle ------------------------------------------------------------------

    def run_loop(self, stop_event: threading.Event,
                 selection_provider=None) -> None:
        """Drive controls / fill / refresh cadences until stopped.

        Intended as a daemon-thread target from continuous mode. Every stage
        is exception-guarded; three consecutive loop errors halt the pilot
        (fail closed) rather than spinning blind. The very first thing this
        loop does is reconcile against live venue state (finding #4) — no
        fill polling or quoting happens until that succeeds; a transient
        venue failure at boot retries on the controls-poll cadence rather
        than requiring a full process restart.
        """
        import config

        last_controls = last_fills = last_refresh = last_selection = 0.0
        last_reconcile_attempt = self._time_fn()
        if not self._reconciled:
            self._reconciled = self.reconcile()
        while not stop_event.is_set():
            now = self._time_fn()
            try:
                if not self._reconciled:
                    if now - last_reconcile_attempt >= config.MM_CONTROLS_POLL_SECONDS:
                        last_reconcile_attempt = now
                        self._reconciled = self.reconcile()
                    stop_event.wait(0.5)
                    continue
                # Retry any exchange cancels that previously failed — see
                # _cancel_order / _retry_pending_cancels (finding #2).
                self._retry_pending_cancels()
                if now - last_controls >= config.MM_CONTROLS_POLL_SECONDS:
                    last_controls = now
                    self._controls.poll()
                if selection_provider is not None and now - last_selection >= 3600:
                    last_selection = now
                    tickers = selection_provider()
                    if tickers is not None:
                        self.update_selection(list(tickers))
                if now - last_fills >= config.MM_FILL_POLL_SECONDS:
                    last_fills = now
                    self.poll_fills()
                if (not self._fills_blind
                        and now - last_refresh >= config.MM_REFRESH_INTERVAL):
                    last_refresh = now
                    self.refresh_all()
                self._loop_error_streak = 0
            except Exception:
                logger.exception("MM pilot loop iteration failed")
                self._loop_error_streak += 1
                if self._loop_error_streak >= 3:
                    self.halt_all("3 consecutive pilot loop errors")
            stop_event.wait(0.5)
        self.stop()

    def stop(self) -> None:
        """Cancel and verify resting orders with bounded shutdown retries."""
        cancelled = 0
        confirmed = False
        remaining_count = len(self.resting_orders())

        for attempt in range(1, self.MAX_CANCEL_ATTEMPTS + 1):
            cancelled += self.pull_all("pilot stop")
            if self.dry_run:
                remaining_count = len(self.resting_orders())
                confirmed = remaining_count == 0
            elif self._client is None:
                remaining_count = len(self.resting_orders())
                confirmed = remaining_count == 0
            else:
                try:
                    live_orders = list(self._client.get_open_orders() or [])
                except Exception:
                    logger.exception("MM pilot shutdown verification failed "
                                     "on attempt %d", attempt)
                    live_orders = []
                    remaining_count = max(1, len(self.resting_orders()))
                else:
                    remaining_count = len(live_orders)
                    confirmed = remaining_count == 0
                    # Venue-only orders can exist after an indeterminate
                    # placement response and therefore have no registry row.
                    # Cancel them directly, then re-query on the next bounded
                    # attempt before declaring shutdown complete.
                    for order in live_orders:
                        oid = str(order.get("order_id") or order.get("id") or "")
                        if not oid:
                            continue
                        with self._lock:
                            tracked = oid in self._orders
                        if tracked:
                            self._cancel_order(oid)
                        else:
                            try:
                                self._client.cancel_order(oid)
                            except Exception:
                                logger.exception("MM pilot shutdown cancel "
                                                 "raised for venue order %s", oid)
            if confirmed:
                break
            logger.warning("MM pilot shutdown cancellation not confirmed "
                           "(attempt %d/%d, remaining=%d)",
                           attempt, self.MAX_CANCEL_ATTEMPTS, remaining_count)

        if not confirmed:
            self._alert(
                "MM_PILOT_SHUTDOWN_UNCONFIRMED", "CRITICAL",
                f"MM pilot shutdown exhausted {self.MAX_CANCEL_ATTEMPTS} "
                f"cancellation attempts with {remaining_count} order(s) "
                f"possibly live; manual venue verification required",
                {"remaining_orders": remaining_count},
            )
        logger.info("MM pilot stopped: cancellation_confirmed=%s, "
                    "cancel_attempts=%d", confirmed, cancelled)
        with self._decision_lock:
            if self._decision_fh is not None and not self._decision_fh.closed:
                self._decision_fh.close()
                self._decision_fh = None

    def get_status(self) -> dict:
        """Status snapshot for dashboards / digests."""
        return {
            "halted": self.halted,
            "halt_reason": self.halt_reason,
            "markets_halted": dict(self._market_halted),
            "canary_graduated": self.canary_graduated,
            "canary_clean_fills": self.canary_clean_fills,
            "resting_orders": len(self._orders),
            "total_inventory_usd": self.inventory.total_net_usd(),
            "realized_pnl": self.inventory.realized_pnl_total(),
            "dry_run": self.dry_run,
        }
