"""Arbitrage trade execution engine."""

import hashlib
import json
import logging
import math
import os
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed

from config import (
    FILL_POLL_INTERVAL, FILL_POLL_TIMEOUT, HEDGE_ENABLED,
    CONCURRENT_EXECUTION, BALANCE_CACHE_TTL,
    ENABLED_EXECUTION_PLATFORMS, PLATFORM_MIN_ORDER_SIZE,
    ORDER_TIME_IN_FORCE, GTC_ORDER_TIMEOUT,
    FAILED_TRADE_COOLDOWN, WS_CACHE_MAX_AGE_REVALIDATION,
    REVALIDATION_MIN_FLOOR as CONFIG_REVALIDATION_MIN_FLOOR,
    REVAL_FLOORS, get_layer,
    NEWS_SNIPE_CONFIDENCE_THRESHOLD, TIME_DECAY_MIN_CONSENSUS,
    MIN_ENTRY_PRICE, EXIT_LIQUIDITY_GATE_ENABLED, MIN_EXIT_BID_DEPTH,
)


class _RevalidationAPIError(Exception):
    """Raised when revalidation fails due to an API/network error, not price movement."""
    pass


def _cached_probability(entry: dict | None, *keys: str) -> float | None:
    """Return the first scalar 0..1 probability from a cache entry.

    WebSocket payloads retain raw string fields and, for Kalshi snapshots,
    raw price ladders. Revalidation must consume normalized executable asks,
    never concatenate a raw string with a float or treat a ladder as a price.
    """
    if not entry:
        return None
    for key in keys:
        value = entry.get(key)
        if value is None or isinstance(value, (dict, list, tuple)):
            continue
        try:
            probability = float(value)
        except (TypeError, ValueError):
            continue
        if 0.0 <= probability <= 1.0:
            return probability
    return None

# Conditional metrics import — never breaks if metrics.py is missing
try:
    from config import METRICS_ENABLED as _METRICS_ENABLED
    if _METRICS_ENABLED:
        from metrics import metrics as _metrics
    else:
        _metrics = None
except Exception:
    _metrics = None

# Conditional alerting import — MON-03: per-strategy loss streak tracking
try:
    from alerting import alert_manager as _alert_manager
except Exception:
    _alert_manager = None

from db import TradeDB
from risk_manager import RiskManager
from polymarket_api import get_clob_prices, fetch_order_book, get_best_bid_ask, PolymarketTrader
from kalshi_api import KalshiClient
from betfair_api import BetfairClient
from smarkets_api import SmarketsClient
from sxbet_api import SXBetClient
from matchbook_api import MatchbookClient
from gemini_api import GeminiClient
from ibkr_api import IBKRClient
from fees import (
    net_profit_binary_internal,
    net_profit_negrisk_internal,
    net_profit_negrisk_no_side,
    net_profit_cross_platform,
    net_profit_kalshi_binary,
    net_profit_kalshi_multi,
    net_profit_triangular,
    net_profit_gemini_binary,
    net_profit_ibkr_binary,
    net_profit_multi_cross,
    net_profit_logical_arb,
    find_lowest_fee_path,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# HARDEN-05: Idempotency key generation
# ---------------------------------------------------------------------------

def _make_idempotency_key(market_id: str, side: str, price: float, extra: str = "") -> str:
    """Return a 16-char hex key stable within a 60-second window (HARDEN-05).

    The key is derived from: market_id, side, price (4 decimal places),
    the current minute bucket (Unix time // 60), and an optional extra
    discriminator. This ensures the same logical order attempt within a
    single minute maps to the same key, allowing platforms and our own DB
    to detect and reject duplicate submissions.
    """
    minute_bucket = int(time.time()) // 60
    raw = f"{market_id}:{side}:{price:.4f}:{minute_bucket}:{extra}"
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def _derive_position_platform(legs: list[dict]) -> str:
    """Pick the platform label for a settled position from its legs.

    If every leg targets the same exchange, store that exchange so settlement
    checks dispatch to the right API. If legs span multiple exchanges, store
    "cross" — settlement code already special-cases that. Falls back to
    "unknown" only when legs is empty (should not happen post-execution).
    """
    platforms = {leg.get("platform") for leg in legs if leg.get("platform")}
    if not platforms:
        return "unknown"
    if len(platforms) == 1:
        return next(iter(platforms))
    return "cross"


def _derive_market_ticker(opportunity: dict, legs: list[dict]) -> str | None:
    """Pick the platform-native id used for settlement lookups.

    Settlement queries the platform API by ticker (e.g. a Kalshi ticker like
    KXEPLSPREAD-...), but ``market_identifier`` holds the human-readable title.
    Storing the ticker lets check_settlements look the market up correctly.
    Returns None when no platform-native id is available, in which case
    settlement falls back to ``market_identifier`` (prior behavior).
    """
    ticker = opportunity.get("_kalshi_ticker")
    if not ticker:
        for leg in legs:
            ticker = leg.get("_kalshi_ticker") or leg.get("_ticker")
            if ticker:
                break
    return ticker or None


def _check_min_entry_price(opportunity: dict, legs: list[dict]) -> tuple[bool, str]:
    """Entry-discipline gate: refuse taker/arb entries below MIN_ENTRY_PRICE.

    Production evidence (June 2026): $0.01 longshot fills had no resting bids
    to exit into — 97 of 104 hedge attempts failed because the market would
    not buy the position back at any acceptable price. A hedger cannot save
    a position the market will not buy back, so the fix is to not enter.

    MarketMake opportunities are exempt: resting cheap two-sided quotes is
    how liquidity rewards are farmed, and MM exits via cancel/replace rather
    than market sells. Only probability-priced buy legs (price in (0, 1)) are
    gated — exchange back/lay legs price in decimal odds and are skipped.
    """
    if MIN_ENTRY_PRICE <= 0:
        return True, ""
    if opportunity.get("type", "").startswith("MarketMake"):
        return True, ""
    for leg in legs:
        side = str(leg.get("side", "")).upper()
        action = str(leg.get("action", "buy")).lower()
        is_buy = side == "BUY" or (side in ("YES", "NO") and action == "buy")
        if not is_buy:
            continue
        price = leg.get("price")
        if not isinstance(price, (int, float)) or not (0 < price < 1):
            continue
        if price < MIN_ENTRY_PRICE:
            return False, (
                f"entry price ${price:.3f} below MIN_ENTRY_PRICE ${MIN_ENTRY_PRICE:.2f} "
                f"({leg.get('platform', '?')} leg — no exit liquidity at penny prices)"
            )
    return True, ""


class ArbitrageExecutor:
    """Executes arbitrage trades with risk controls, dry-run, and semi/full-auto modes."""

    def __init__(
        self,
        pm_trader: PolymarketTrader | None,
        kalshi_client: KalshiClient | None,
        db: TradeDB,
        risk_manager: RiskManager,
        dry_run: bool = True,
        exec_mode: str = "semi-auto",
        max_trade_size: float = 5.0,
        price_cache: dict | None = None,
        betfair_client: BetfairClient | None = None,
        smarkets_client: SmarketsClient | None = None,
        sxbet_client: SXBetClient | None = None,
        matchbook_client: MatchbookClient | None = None,
        gemini_client: GeminiClient | None = None,
        ibkr_client: IBKRClient | None = None,
        gas_monitor=None,
        revalidation_adaptive: bool = True,
        revalidation_min_floor: float = 0.003,
        dynamic_sizing: bool = False,
        sizing_aggressiveness: float = 0.5,
        concurrent_execution: bool = False,
        notifier=None,
        position_sizer=None,
    ):
        self.pm_trader = pm_trader
        self.kalshi_client = kalshi_client
        self.betfair_client = betfair_client
        self.smarkets_client = smarkets_client
        self.sxbet_client = sxbet_client
        self.matchbook_client = matchbook_client
        self.gemini_client = gemini_client
        self.ibkr_client = ibkr_client
        self.gas_monitor = gas_monitor
        self.notifier = notifier
        self.db = db
        self.risk = risk_manager
        self.dry_run = dry_run
        self.exec_mode = exec_mode
        self.max_trade_size = max_trade_size
        self.price_cache = price_cache
        self.revalidation_adaptive = revalidation_adaptive
        self.revalidation_min_floor = revalidation_min_floor
        self.dynamic_sizing = dynamic_sizing
        self.sizing_aggressiveness = sizing_aggressiveness
        self.concurrent_execution = concurrent_execution
        self.position_sizer = position_sizer
        # Balance cache: avoids redundant API calls within a scan cycle
        self._balance_cache: dict = {}
        self._balance_cache_ts: float = 0.0
        self._balance_cache_type: str = ""
        self._balance_cache_generation = 0
        self._balance_cache_lock = threading.Lock()
        # Failed-trade cooldown: ticker/market -> earliest retry time.
        # Prevents catastrophic loops when the same bogus opportunity keeps
        # failing (e.g. insufficient liquidity) and is re-presented.
        self._failed_cooldowns: dict[str, float] = {}
        self._kalshi_reconciliation_required = False
        self._FAILED_COOLDOWN_SECS = FAILED_TRADE_COOLDOWN
        # HARDEN-03: structured decision log (JSONL)
        _data_dir = os.getenv("DATA_DIR", ".")
        self._decision_log_path = os.path.join(_data_dir, "decisions.jsonl")
        self._decision_log_lock = threading.Lock()
        self._decision_fh = open(self._decision_log_path, "a", encoding="utf-8", buffering=1)
        # Whale copy position tracking
        self._whale_copy_position_count = 0

    def _get_cached_balances(self, opp_type: str) -> dict | None:
        """Return cached balances if fresh, otherwise fetch and cache.

        Caches balance results for BALANCE_CACHE_TTL seconds to avoid
        redundant API calls between the risk gate and preflight check,
        and across rapid WS-triggered executions.

        Args:
            opp_type: Opportunity type string for platform determination.

        Returns:
            Dict of {platform: balance} or None if no balances available.
        """
        now = time.time()
        with self._balance_cache_lock:
            if (now - self._balance_cache_ts < BALANCE_CACHE_TTL
                    and self._balance_cache_type == opp_type
                    and self._balance_cache):
                return dict(self._balance_cache)
            generation = self._balance_cache_generation
        balances = self._fetch_balances(opp_type)
        if balances:
            with self._balance_cache_lock:
                if generation == self._balance_cache_generation:
                    self._balance_cache = dict(balances)
                    self._balance_cache_ts = now
                    self._balance_cache_type = opp_type
        return balances

    def invalidate_balance_cache(self):
        """Clear the balance cache after a trade fills or position changes."""
        with self._balance_cache_lock:
            self._balance_cache_generation += 1
            self._balance_cache = {}
            self._balance_cache_ts = 0.0
            self._balance_cache_type = ""

    def execute(self, opportunity: dict, market_data: dict | None = None) -> bool:
        """Execute an arbitrage opportunity through the full pipeline.

        Args:
            opportunity: Opportunity dict from scanner
            market_data: Optional pre-fetched market data for re-validation

        Returns:
            True if trade was executed (or logged as dry_run), False if skipped.
        """
        opp_type = opportunity.get("type", "")
        market = opportunity.get("market", "Unknown")

        # 0. Kill switch — abort all execution when dashboard pause is engaged
        try:
            from dashboard import is_paused
            if is_paused():
                logger.info(f"[PAUSED] Kill switch active — skipping: {market} ({opp_type})")
                self._log_skipped(opportunity, "kill_switch")
                return False
        except ImportError:
            pass  # Dashboard not available (e.g. tests)

        if not self.dry_run and self._kalshi_reconciliation_required:
            logger.error(
                "Kalshi execution is halted pending explicit venue reconciliation"
            )
            self._log_skipped(opportunity, "kalshi_reconciliation_required")
            return False

        # 0b. Failed-trade cooldown — prevent re-execution loops
        cooldown_key = opportunity.get("_kalshi_ticker") or market
        cooldown_until = self._failed_cooldowns.get(cooldown_key, 0)
        if time.time() < cooldown_until:
            remaining = int(cooldown_until - time.time())
            logger.debug(f"Cooldown active for {cooldown_key} ({remaining}s remaining). Skipping.")
            self._log_skipped(opportunity, "failed_cooldown")
            return False

        # 0c. HARDEN-05: Idempotency — reject duplicate within 60s window
        if self.db.has_recent_trade(market, window_secs=60.0):
            logger.debug(f"Duplicate trade for {market} within 60s window. Skipping.")
            self._log_skipped(opportunity, "duplicate_trade")
            return False

        # 0d. STRAT-05: Whale copy position limit (max 5 concurrent)
        if opp_type == "WhaleCopy":
            from config import WHALE_COPY_MAX_POSITIONS
            if self._whale_copy_position_count >= WHALE_COPY_MAX_POSITIONS:
                logger.warning(
                    "Whale copy position limit reached (%d/%d), skipping: %s",
                    self._whale_copy_position_count, WHALE_COPY_MAX_POSITIONS, market,
                )
                self._log_skipped(opportunity, "whale_position_limit")
                return False

        # 0e. Plan 10 / MM pilot single-choke-point guard. The legacy
        # KalshiRewards path (this method's opp_type == "KalshiRewards"
        # branch below, and _build_legs -> _execute_single_leg's kalshi arm)
        # calls kalshi_client.place_order directly and is pre-existing on
        # master — it does NOT run through KalshiMMPilot.authorize_order's
        # kill switch, caps, or gates. That is fine when nothing else is
        # quoting Kalshi, but once the MM pilot owns Kalshi quoting
        # (MM_KALSHI_PILOT_ENABLED), two independent systems placing orders
        # on the same venue at once is exactly the failure mode the pilot's
        # single-choke-point design exists to prevent. Fail closed: disable
        # this path entirely while the pilot is enabled, regardless of
        # DRY_RUN/REWARDS_ENABLED. When the flag is false, behavior here is
        # unchanged from master.
        if opp_type == "KalshiRewards":
            from config import MM_KALSHI_PILOT_ENABLED
            if MM_KALSHI_PILOT_ENABLED:
                logger.warning(
                    "[MM_PILOT] Legacy KalshiRewards execution path disabled "
                    "while MM_KALSHI_PILOT_ENABLED=true — %s skipped (the "
                    "pilot owns Kalshi quoting; see "
                    "docs/plans/10-mm-pilot-prep.md)", market,
                )
                self._log_skipped(opportunity, "mm_pilot_owns_kalshi")
                return False

        prefix = "[DRY RUN] " if self.dry_run else ""

        logger.info(f"{prefix}--- Evaluating: {market} ({opp_type}) ---")

        _exec_start = time.time()

        # 1. Re-validate prices (always run for REVAL| calibration logging)
        _reval_result = self._revalidate(opportunity, self.price_cache)
        if not _reval_result and not self.dry_run:
            self._log_skipped(opportunity, "stale_prices")
            if _metrics:
                _metrics.inc("revalidation_failures", {"type": opp_type, "reason": "price_degraded"})
            return False
        if not _reval_result and self.dry_run:
            logger.info("%sRevalidation would reject (calibration only)", prefix)
            if _metrics:
                _metrics.inc("revalidation_failures", {"type": opp_type, "reason": "price_degraded_dryrun"})
            # Continue in dry-run — log the rejection but don't skip

        # 2. Risk gate (uses cached balances to avoid redundant API calls)
        balances = self._get_cached_balances(opp_type)
        if not self.dry_run and not balances:
            logger.warning("Live execution blocked: required venue balances are unknown")
            self._log_skipped(opportunity, "balance_unavailable")
            return False
        allowed, reason = self.risk.check(opportunity, self.db, balances)
        if not allowed:
            logger.info(f"{prefix}Risk blocked: {reason}")
            self._log_skipped(opportunity, f"risk:{reason}")
            if _metrics:
                _metrics.inc("risk_rejections", {"strategy": opp_type, "reason": reason[:50]})
            return False

        # 2b. Dynamic fee check (GasMonitor)
        if self.gas_monitor and not self.gas_monitor.should_execute(opportunity):
            logger.info(f"{prefix}Dynamic fee check: profit below gas-aware threshold")
            self._log_skipped(opportunity, "gas_threshold")
            return False

        # 3. Size calculation
        depth = opportunity.get("_clob_depth", 0)
        per_leg_budget = self._per_leg_budget(opp_type, opportunity, balances)
        if self.position_sizer:
            desired_size = self.position_sizer.size_for_opportunity(opportunity)
        elif self.dynamic_sizing:
            desired_size = self.risk.calculate_dynamic_size(opportunity, self.sizing_aggressiveness)
        else:
            desired_size = self.max_trade_size
        size = self.risk.clamp_size(desired_size, depth, per_leg_budget)
        if size <= 0:
            logger.info(f"{prefix}Size 0 after constraints. Skipping.")
            self._log_skipped(opportunity, "zero_size")
            return False

        # 4. Build execution plan
        legs = self._build_legs(opportunity, size)
        if not legs:
            logger.info(f"{prefix}Could not build execution legs. Skipping.")
            self._log_skipped(opportunity, "no_legs")
            return False
        if not self.dry_run:
            for platform in {leg["platform"] for leg in legs}:
                balance = (balances or {}).get(platform)
                if (not isinstance(balance, (int, float))
                        or not math.isfinite(balance) or balance < 0):
                    logger.warning(
                        "Live execution blocked: %s balance is unknown or invalid",
                        platform,
                    )
                    self._log_skipped(opportunity, f"balance_unavailable:{platform}")
                    return False

        # 4b. Entry-discipline gate: never enter positions the market won't
        # buy back (penny longshots have no exit liquidity; see hedger.py).
        ok_entry, entry_reason = _check_min_entry_price(opportunity, legs)
        if not ok_entry:
            logger.info(f"{prefix}Entry blocked: {entry_reason}")
            self._log_skipped(opportunity, "min_entry_price")
            return False

        # 4c. Entry-discipline gate #2: verify a live exit bid exists under
        # every buy leg (live order-book check; fails closed on fetch error).
        ok_exit, exit_reason = self._check_exit_liquidity(opportunity, legs)
        if not ok_exit:
            logger.info(f"{prefix}Entry blocked: {exit_reason}")
            self._log_skipped(opportunity, "exit_liquidity")
            return False

        # 5. Display plan
        self._print_plan(opportunity, legs, size, prefix)

        # 6. Approval gate (semi-auto)
        if not self.dry_run and self.exec_mode == "semi-auto":
            try:
                answer = input("  Execute? [y/N] ").strip().lower()
            except (EOFError, KeyboardInterrupt):
                answer = "n"
            if answer != "y":
                logger.info("Skipped by user.")
                self._log_skipped(opportunity, "user_declined")
                return False

        # 7. Execute or dry-run log
        if self.dry_run:
            result = self._dry_run_log(opportunity, legs, size)
            if _metrics and result:
                _metrics.inc("trades_executed", {"strategy": opp_type, "status": "dry_run"})
            return result
        else:
            # Use concurrent execution when enabled and all legs support cancellation
            if self.concurrent_execution and self._supports_concurrent(legs):
                result = self._execute_legs_concurrent(opportunity, legs, size)
            else:
                result = self._execute_legs(opportunity, legs, size)
            if _metrics:
                latency = time.time() - _exec_start
                _metrics.observe("execution_latency_seconds", {"strategy": opp_type}, latency)
                if result:
                    _metrics.inc("trades_executed", {"strategy": opp_type, "status": "filled"})
                else:
                    _metrics.inc("trades_failed", {"strategy": opp_type, "reason": "execution"})
            # HARDEN-03: log live execution decision
            if result:
                self._write_decision(opportunity, "execute", "filled")
            else:
                self._write_decision(opportunity, "reject", "execution_failed")
            # On failure, set a cooldown so the same opportunity is not
            # re-attempted immediately (prevents catastrophic retry loops).
            if not result:
                self._failed_cooldowns[cooldown_key] = time.time() + self._FAILED_COOLDOWN_SECS
                logger.info(f"Cooldown set for {cooldown_key} ({self._FAILED_COOLDOWN_SECS}s)")
            return result

    def _check_exit_liquidity(self, opportunity: dict, legs: list[dict]) -> tuple[bool, str]:
        """Entry-discipline gate #2: require a live exit bid under every buy leg.

        MIN_ENTRY_PRICE blocks penny longshots; this gate blocks one-sided
        books at any price. Production evidence (June 2026): partial fills
        could not be hedged because the entered markets had no resting bids —
        the fix is to never enter a market the hedger could not exit. Fails
        CLOSED when an order book cannot be fetched (no verification → no
        entry).

        Only Kalshi and Polymarket legs are checked (the venues with live
        execution paths today). MarketMake is exempt: it exits via
        cancel/replace, and resting quotes on thin books is how liquidity
        rewards are farmed.
        """
        if not EXIT_LIQUIDITY_GATE_ENABLED:
            return True, ""
        if self.dry_run:
            # Dry-run places no orders and creates no positions to hedge;
            # skipping avoids per-candidate live book fetches in paper mode.
            return True, ""
        if opportunity.get("type", "").startswith("MarketMake"):
            return True, ""

        book_cache: dict = {}
        for leg in legs:
            side = str(leg.get("side", "")).upper()
            action = str(leg.get("action", "buy")).lower()
            is_buy = side == "BUY" or (side in ("YES", "NO") and action == "buy")
            if not is_buy:
                continue
            price = leg.get("price")
            if not isinstance(price, (int, float)) or not (0 < price < 1):
                continue

            platform = leg.get("platform", "")
            if platform == "kalshi":
                if not self.kalshi_client:
                    # No client means no way to verify exit liquidity — and no
                    # way to place the order later. Fail closed, don't skip.
                    return False, "kalshi leg present but no Kalshi client (fail closed)"
                ticker = leg.get("_ticker") or opportunity.get("_kalshi_ticker") or ""
                if not ticker:
                    continue
                from kalshi_api import parse_orderbook, best_yes_bid, best_no_bid
                if ticker in book_cache:
                    parsed = book_cache[ticker]
                else:
                    try:
                        book = self.kalshi_client.fetch_order_book(ticker)
                        parsed = parse_orderbook(book) if book else None
                    except Exception as exc:
                        logger.warning(f"Exit-liquidity book fetch raised for {ticker}: {exc}")
                        parsed = None
                    if not parsed:
                        return False, f"could not fetch order book for {ticker} (fail closed)"
                    book_cache[ticker] = parsed
                kalshi_side = str(leg.get("side", "")).lower()
                exit_bid = best_yes_bid(parsed) if kalshi_side == "yes" else best_no_bid(parsed)
                if not exit_bid or exit_bid[0] <= 0:
                    return False, f"no resting {kalshi_side} bid on {ticker} — one-sided book"
                if exit_bid[1] < MIN_EXIT_BID_DEPTH:
                    return False, (
                        f"exit bid depth {exit_bid[1]:.0f} < MIN_EXIT_BID_DEPTH "
                        f"{MIN_EXIT_BID_DEPTH} on {ticker}"
                    )
            elif platform == "polymarket":
                token_id = leg.get("_token_id") or ""
                if not token_id:
                    continue
                if token_id in book_cache:
                    best = book_cache[token_id]
                else:
                    try:
                        book = fetch_order_book(token_id)
                        best = get_best_bid_ask(book) if book else None
                    except Exception as exc:
                        logger.warning(f"Exit-liquidity CLOB fetch raised for {token_id[:16]}: {exc}")
                        best = None
                    if not best:
                        return False, f"could not fetch CLOB book for token {token_id[:16]} (fail closed)"
                    book_cache[token_id] = best
                bid = best.get("bid")
                bid_size = best.get("bid_size") or 0
                if not bid or bid <= 0:
                    return False, f"no resting bid on token {token_id[:16]} — one-sided book"
                if bid_size < MIN_EXIT_BID_DEPTH:
                    return False, (
                        f"exit bid depth {bid_size:.0f} < MIN_EXIT_BID_DEPTH "
                        f"{MIN_EXIT_BID_DEPTH} on token {token_id[:16]}"
                    )
            # Other platforms: no wired exit-side book check; MIN_ENTRY_PRICE
            # and the per-platform risk gates still apply.
        return True, ""

    def _revalidate(self, opportunity: dict, price_cache: dict | None = None) -> bool:
        """Re-fetch current prices and verify the opportunity still exists.

        Returns True if the opportunity is still profitable (>= threshold of original).
        Returns False only when prices have genuinely degraded below threshold.

        API/network failures fail closed because an unverified price cannot
        authorize a consequential order.

        Emits a structured REVAL| calibration log line for every decision (per D-01).
        """
        start_ms = int(time.time() * 1000)
        opp_type = opportunity.get("type", "")
        original_profit = opportunity.get("net_profit", 0)
        if original_profit <= 0:
            return False

        # Compute scan-time ROI for the calibration log
        total_cost_raw = opportunity.get("total_cost", "$0")
        total_cost = (
            float(total_cost_raw.replace("$", ""))
            if isinstance(total_cost_raw, str)
            else float(total_cost_raw)
        )
        scan_roi = original_profit / total_cost if total_cost > 0 else 0

        # Determine layer — prefer explicit _layer tag, fall back to config lookup
        layer = opportunity.get("_layer")
        if layer is None:
            layer = get_layer(opp_type)
            if layer == 0:
                layer = 1  # Default to L1 for unknown types
        floor = REVAL_FLOORS.get(layer, REVAL_FLOORS.get(1, 0.02))

        passed = True
        reval_profit = original_profit
        reason = "live_orderbook"

        try:
            if opp_type == "Binary":
                passed, reval_profit, reason = self._revalidate_binary(
                    opportunity, original_profit, price_cache)
            elif opp_type.startswith("NegRiskNO"):
                passed, reval_profit, reason = self._revalidate_negrisk_no(
                    opportunity, original_profit, price_cache)
            elif opp_type.startswith("NegRisk"):
                passed, reval_profit, reason = self._revalidate_negrisk(
                    opportunity, original_profit, price_cache)
            elif opp_type.startswith("Cross"):
                passed, reval_profit, reason = self._revalidate_cross(
                    opportunity, original_profit, price_cache)
            elif opp_type == "KalshiBinary":
                passed, reval_profit, reason = self._revalidate_kalshi_binary(
                    opportunity, original_profit)
            elif opp_type.startswith("KalshiMulti"):
                passed, reval_profit, reason = self._revalidate_kalshi_multi(
                    opportunity, original_profit)
            elif opp_type.startswith("Spread"):
                reason = "live_orderbook"  # Spread prices are live — no mid-price staleness
            elif opp_type in ("BetfairBackAll", "BetfairBackLay"):
                reason = "live_orderbook"  # Betfair prices are live order book
            elif opp_type in ("SmarketsBackAll", "SmarketsBackLay"):
                reason = "live_orderbook"  # Smarkets prices are live order book
            elif opp_type in ("SXBetBackAll", "SXBetBackLay"):
                reason = "live_orderbook"  # SX Bet prices are live order book
            elif opp_type in ("MatchbookBackAll", "MatchbookBackLay"):
                reason = "live_orderbook"  # Matchbook prices are live order book
            elif opp_type in ("GeminiBinary", "GeminiMulti"):
                reason = "live_orderbook"  # Gemini prices are from order book
            elif opp_type == "IBKRBinary":
                reason = "live_snapshot"  # IBKR prices are from snapshot
            elif opp_type.startswith("MultiCross"):
                passed, reval_profit, reason = self._revalidate_multi_cross(
                    opportunity, original_profit, price_cache)
            elif opp_type == "TriangularCross":
                passed, reval_profit, reason = self._revalidate_triangular(
                    opportunity, original_profit, price_cache)
            elif opp_type == "EventDivergence":
                reason = "signal_based"  # Signal-based — no stale mid-price to revalidate
            elif opp_type in ("StalePriceOpp", "ResolutionSnipeOpp", "ConvergenceOpp"):
                reason = "signal_based"  # Signal/time-based — directional, no mid-price revalidation
            elif opp_type == "FeePromo":
                # Strategy #9: scan_fee_promo already re-scored using current
                # fee globals at emit time. No further mid-price revalidation.
                reason = "fee_reload"
            elif opp_type == "CrossPlatformMM":
                # Strategy #11: paired bid/ask quotes — refreshed by the MM
                # engine, not subject to stale-mid revalidation.
                reason = "mm_refreshed"
            elif opp_type.startswith("NWayArb"):
                # Sprint 4: N-way cross-platform arb. Picks the cheapest YES
                # and cheapest NO across 4+ platforms. Reuse the Cross
                # revalidator when both platforms are poly+kalshi; otherwise
                # accept on the signal alone (the scan's freshly-fetched mid
                # prices are still recent).
                pa = opportunity.get("_platform_a", "")
                pb = opportunity.get("_platform_b", "")
                if {pa, pb} <= {"polymarket", "kalshi"}:
                    passed, reval_profit, reason = self._revalidate_cross(
                        opportunity, original_profit, price_cache)
                else:
                    reason = "nway_signal"
            elif opp_type == "LeadLagMM":
                # Sprint 4: lag-based directional quote. Ask the LeadLagMM
                # singleton whether the lag still exists; if the lagger has
                # caught up, the convergence trade is stale.
                try:
                    from market_maker import get_lead_lag_mm
                    detector = get_lead_lag_mm()
                    market_key = opportunity.get("_market_key", "")
                    lagger = opportunity.get("_lagger", "")
                    if market_key and lagger and detector.should_quote(market_key, lagger):
                        reason = "lag_confirmed"
                    else:
                        passed = False
                        reason = "lag_collapsed"
                except Exception:
                    reason = "lag_confirmed"
            elif opp_type in ("ToxicFlowPause", "VolatilityAdjustedMM"):
                # Sprint 4: observability-only opps. Block execution.
                passed = False
                reason = "defensive_observability"
            elif opp_type == "MarketMake":
                reason = "mm_refreshed"  # MM quotes are continuously refreshed by the MM engine
            elif opp_type == "PolymarketRewards":
                reason = "reward_refreshed"  # Reward quotes are refreshed during reward scan
            elif opp_type == "KalshiRewards":
                reason = "reward_refreshed"  # Reward quotes are refreshed during reward scan
            elif opp_type == "Imbalance":
                # STRAT-01: Imbalance revalidation — check ratio hasn't collapsed
                current_ratio = abs(opportunity.get("_imbalance_ratio", 0.0))
                if current_ratio > 0:
                    original_ratio = abs(opportunity.get("_original_imbalance_ratio", current_ratio))
                    min_ratio_to_revalidate = original_ratio * 0.7  # Allow 30% collapse
                    if current_ratio < min_ratio_to_revalidate:
                        logger.info(
                            "Imbalance collapsed %.1f%% -> rejected",
                            (1 - current_ratio / original_ratio) * 100 if original_ratio > 0 else 0
                        )
                        passed = False
                        reason = "Imbalance collapsed"
                    else:
                        reason = "imbalance_stable"
                else:
                    reason = "imbalance_stable"
            elif opp_type == "NewsSnipe":
                # STRAT-02: News snipe revalidation — check confidence threshold
                confidence = opportunity.get("_confidence", 0.0)
                if confidence < NEWS_SNIPE_CONFIDENCE_THRESHOLD:
                    logger.info("News snipe confidence too low: %.1f", confidence)
                    passed = False
                    reason = f"Confidence {confidence:.2f} below threshold {NEWS_SNIPE_CONFIDENCE_THRESHOLD}"
                else:
                    reason = "confidence_verified"
            elif opp_type == "Correlated":
                # STRAT-06: Correlated revalidation — check spread hasn't collapsed
                current_spread = opportunity.get("_spread", 0.0)
                original_spread = opportunity.get("_original_spread", current_spread)
                if original_spread > 0:
                    min_spread_to_revalidate = original_spread * 0.8  # Allow 20% collapse
                    if current_spread < min_spread_to_revalidate:
                        logger.info(
                            "Correlated spread collapsed %.1f%% -> rejected",
                            (1 - current_spread / original_spread) * 100
                        )
                        passed = False
                        reason = "Spread collapsed"
                    else:
                        reason = "spread_verified"
                else:
                    reason = "spread_verified"
            elif opp_type == "TimeDecay":
                # STRAT-07: Time decay revalidation — check expiry and consensus
                hours_left = opportunity.get("_hours_to_expiry", 0.0)
                if hours_left < 1.0:
                    logger.info("Time decay: market expired before execution")
                    passed = False
                    reason = "Market expired"
                else:
                    consensus = opportunity.get("_consensus_prob", 0.0)
                    if max(consensus, 1.0 - consensus) < TIME_DECAY_MIN_CONSENSUS:
                        logger.info("Time decay: consensus dropped below threshold: %.2f", consensus)
                        passed = False
                        reason = f"Consensus {consensus:.2f} dropped below {TIME_DECAY_MIN_CONSENSUS}"
                    else:
                        reason = "time_decay_verified"
            elif opp_type == "LogicalArb":
                # STRAT-04: Logical arb revalidation — check both market prices haven't moved >10%
                passed, reval_profit, reason = self._revalidate_logical_arb(
                    opportunity, original_profit)
            elif opp_type == "WhaleCopy":
                # STRAT-05: Whale copy revalidation — check <30s latency and price stability
                passed, reval_profit, reason = self._revalidate_whale_copy(
                    opportunity, original_profit)
            # Unknown type — proceed cautiously (passed=True from init)

        except _RevalidationAPIError as e:
            logger.info("Revalidation API error for %s; rejecting: %s", opp_type, e)
            passed = False
            reval_profit = 0.0
            reason = "api_error_rejected"
        except Exception as e:
            logger.warning("Revalidation unexpected error: %s", e)
            passed = False
            reval_profit = 0.0
            reason = "unexpected_error"

        # Emit structured calibration log line (per D-01)
        reval_roi = reval_profit / total_cost if total_cost > 0 else 0
        elapsed_ms = int(time.time() * 1000) - start_ms
        logger.info(
            "REVAL|layer=%d|type=%s|scan_roi=%.4f|reval_roi=%.4f|delta=%.4f|"
            "passed=%s|reason=%s|elapsed_ms=%d|floor=%.4f",
            layer, opp_type, scan_roi, reval_roi, scan_roi - reval_roi,
            passed, reason, elapsed_ms, floor,
        )

        return passed

    def _check_ws_cache(self, price_cache: dict | None, platform: str, key: str) -> dict | None:
        """Check WebSocket price cache for fresh data (< WS_CACHE_MAX_AGE_REVALIDATION seconds)."""
        if not price_cache:
            return None
        entry = price_cache.get((platform, key))
        if entry and time.time() - entry.get("_ts", 0) < WS_CACHE_MAX_AGE_REVALIDATION:
            return entry
        return None

    def _get_revalidation_threshold(self, original_profit: float, opp: dict) -> float:
        """Calculate the minimum acceptable profit during revalidation.

        When adaptive revalidation is enabled:
        - ROI >= 5%: strict 90% of original profit
        - ROI 2-5%: moderate 80% of original profit
        - ROI < 2%: lenient — accept if profit > absolute floor
        Partial-CLOB opportunities use 80% max instead of 90%.

        Multi-outcome opportunities (3+ legs) get a per-leg tolerance
        bonus because each leg's re-fetch introduces independent noise.
        When disabled: always use strict 90% threshold.
        """
        is_partial = opp.get("_partial_clob", False)
        opp_type = opp.get("type", "")

        # Multi-outcome leg count scaling — each leg adds re-fetch noise
        leg_count = 1
        if "(" in opp_type:
            try:
                leg_count = int(opp_type.split("(")[1].rstrip(")"))
            except (ValueError, IndexError):
                leg_count = 1
        # Each additional leg beyond 2 loosens threshold by 5% (capped at 30% bonus)
        multi_bonus = min(0.30, max(0, (leg_count - 2)) * 0.05)

        if not self.revalidation_adaptive:
            base = 0.8 if is_partial else 0.9
            return original_profit * max(0.5, base - multi_bonus)

        total_cost_str = opp.get("total_cost", "$0")
        total_cost = float(total_cost_str.replace("$", "")) if isinstance(total_cost_str, str) else float(total_cost_str)
        roi = original_profit / total_cost if total_cost > 0 else 0

        if roi >= 0.05:
            base = 0.8 if is_partial else 0.9
            return original_profit * max(0.5, base - multi_bonus)
        elif roi >= 0.02:
            base = 0.7 if is_partial else 0.8
            return original_profit * max(0.4, base - multi_bonus)
        else:
            # Low-ROI: use layer-specific floor instead of global minimum
            layer = opp.get("_layer")
            if layer is None:
                opp_type_str = opp.get("type", "")
                layer = get_layer(opp_type_str)
                if layer == 0:
                    logger.warning(
                        "Revalidation: unknown layer for type=%s, using L1 floor", opp_type_str
                    )
                    layer = 1
            return REVAL_FLOORS.get(layer, REVAL_FLOORS.get(1, 0.02))

    def _revalidate_binary(
        self, opp: dict, original_profit: float, price_cache: dict | None
    ) -> tuple[bool, float, str]:
        """Revalidate a Polymarket binary opportunity.

        Returns:
            (passed, reval_profit, reason)
        """
        token_ids = opp.get("_token_ids", [])
        if len(token_ids) < 2:
            logger.warning("Revalidation: missing token IDs for binary")
            raise _RevalidationAPIError("missing token IDs for binary")

        # Try WS cache first, then API
        yes_ask = no_ask = None
        cached_yes = self._check_ws_cache(price_cache, "polymarket", token_ids[0])
        cached_no = self._check_ws_cache(price_cache, "polymarket", token_ids[1])

        # Check for stale feeds (no message in 30s) — skip stale prices
        if cached_yes and cached_yes.get("_stale", False):
            logger.info("Skipping revalidation: polymarket YES stale for >30s")
            return False, 0.0, "feed_stale"
        if cached_no and cached_no.get("_stale", False):
            logger.info("Skipping revalidation: polymarket NO stale for >30s")
            return False, 0.0, "feed_stale"

        if cached_yes and cached_no:
            yes_ask = _cached_probability(cached_yes, "best_ask", "ask", "price")
            no_ask = _cached_probability(cached_no, "best_ask", "ask", "price")

        if yes_ask is None or no_ask is None:
            yes_book = fetch_order_book(token_ids[0])
            no_book = fetch_order_book(token_ids[1])
            if not yes_book or not no_book:
                raise _RevalidationAPIError("failed to fetch order book for binary")
            yes_data = get_best_bid_ask(yes_book)
            no_data = get_best_bid_ask(no_book)
            yes_ask = yes_data["ask"]
            no_ask = no_data["ask"]

        if yes_ask is None or no_ask is None:
            raise _RevalidationAPIError("no ask price in order book for binary")

        result = net_profit_binary_internal(yes_ask, no_ask)
        reval_profit = result["net_profit"]
        threshold = self._get_revalidation_threshold(original_profit, opp)
        if reval_profit < threshold:
            logger.info(
                "Revalidation: binary profit degraded %.4f -> %.4f (threshold=%.4f)",
                original_profit, reval_profit, threshold,
            )
            return False, reval_profit, "profit_below_floor"
        # Update opportunity with fresh prices
        opp["prices"] = f"Y={yes_ask:.3f} N={no_ask:.3f}"
        opp["net_profit"] = reval_profit
        return True, reval_profit, "passed"

    def _revalidate_negrisk(
        self, opp: dict, original_profit: float, price_cache: dict | None
    ) -> tuple[bool, float, str]:
        """Revalidate a Polymarket NegRisk opportunity.

        Returns:
            (passed, reval_profit, reason)
        """
        token_ids = opp.get("_token_ids", [])
        if not token_ids:
            raise _RevalidationAPIError("no token IDs for negrisk")

        yes_asks = []
        for tid in token_ids:
            if not tid:
                raise _RevalidationAPIError("empty token ID in negrisk")
            cached = self._check_ws_cache(price_cache, "polymarket", tid)

            # Check for stale feed
            if cached and cached.get("_stale", False):
                logger.info("Skipping revalidation: polymarket token %s stale for >30s", tid)
                return False, 0.0, "feed_stale"

            cached_ask = _cached_probability(cached, "best_ask", "ask", "price")
            if cached_ask is not None:
                yes_asks.append(cached_ask)
            else:
                book = fetch_order_book(tid)
                if not book:
                    raise _RevalidationAPIError(f"failed to fetch order book for negrisk token {tid}")
                data = get_best_bid_ask(book)
                if data["ask"] is None:
                    raise _RevalidationAPIError(f"no ask price for negrisk token {tid}")
                yes_asks.append(data["ask"])

        result = net_profit_negrisk_internal(yes_asks)
        reval_profit = result["net_profit"]
        threshold = self._get_revalidation_threshold(original_profit, opp)
        if reval_profit < threshold:
            logger.info(
                "Revalidation: negrisk profit degraded %.4f -> %.4f (threshold=%.4f)",
                original_profit, reval_profit, threshold,
            )
            return False, reval_profit, "profit_below_floor"
        opp["net_profit"] = reval_profit
        return True, reval_profit, "passed"

    def _revalidate_negrisk_no(
        self, opp: dict, original_profit: float, price_cache: dict | None
    ) -> tuple[bool, float, str]:
        """Revalidate a Polymarket NegRisk NO-side opportunity.

        The opp's _token_ids are the NO tokens, so the fetched asks are NO asks.

        Returns:
            (passed, reval_profit, reason)
        """
        token_ids = opp.get("_token_ids", [])
        if not token_ids:
            raise _RevalidationAPIError("no token IDs for negrisk_no")

        no_asks = []
        for tid in token_ids:
            if not tid:
                raise _RevalidationAPIError("empty token ID in negrisk_no")
            cached = self._check_ws_cache(price_cache, "polymarket", tid)

            # Check for stale feed
            if cached and cached.get("_stale", False):
                logger.info("Skipping revalidation: polymarket token %s stale for >30s", tid)
                return False, 0.0, "feed_stale"

            cached_ask = _cached_probability(cached, "best_ask", "ask", "price")
            if cached_ask is not None:
                no_asks.append(cached_ask)
            else:
                book = fetch_order_book(tid)
                if not book:
                    raise _RevalidationAPIError(f"failed to fetch order book for negrisk_no token {tid}")
                data = get_best_bid_ask(book)
                if data["ask"] is None:
                    raise _RevalidationAPIError(f"no ask price for negrisk_no token {tid}")
                no_asks.append(data["ask"])

        result = net_profit_negrisk_no_side(no_asks)
        reval_profit = result["net_profit"]
        threshold = self._get_revalidation_threshold(original_profit, opp)
        if reval_profit < threshold:
            logger.info(
                "Revalidation: negrisk_no profit degraded %.4f -> %.4f (threshold=%.4f)",
                original_profit, reval_profit, threshold,
            )
            return False, reval_profit, "profit_below_floor"
        opp["net_profit"] = reval_profit
        # Propagate refreshed asks so _build_legs prices orders from the
        # revalidated book, not the stale scan-time snapshot.
        opp["_no_prices"] = no_asks
        return True, reval_profit, "passed"

    def _revalidate_cross(
        self, opp: dict, original_profit: float, price_cache: dict | None
    ) -> tuple[bool, float, str]:
        """Revalidate a cross-platform opportunity.

        Returns:
            (passed, reval_profit, reason)
        """
        token_ids = opp.get("_token_ids", [])
        kalshi_ticker = opp.get("_kalshi_ticker", "")

        # Re-fetch PM prices
        pm_yes = pm_no = None
        if len(token_ids) >= 2:
            for i, tid in enumerate(token_ids[:2]):
                cached = self._check_ws_cache(price_cache, "polymarket", tid)

                # Check for stale feed
                if cached and cached.get("_stale", False):
                    logger.info("Skipping revalidation: polymarket token %s stale for >30s", tid)
                    return False, 0.0, "feed_stale"

                cached_ask = _cached_probability(cached, "best_ask", "ask", "price")
                if cached_ask is not None:
                    if i == 0:
                        pm_yes = cached_ask
                    else:
                        pm_no = cached_ask
            if pm_yes is None or pm_no is None:
                yes_book = fetch_order_book(token_ids[0])
                no_book = fetch_order_book(token_ids[1])
                if not yes_book or not no_book:
                    raise _RevalidationAPIError("failed to fetch PM order book for cross")
                yes_data = get_best_bid_ask(yes_book)
                no_data = get_best_bid_ask(no_book)
                pm_yes = yes_data["ask"]
                pm_no = no_data["ask"]

        # Re-fetch Kalshi prices
        k_yes = k_no = None
        if kalshi_ticker and self.kalshi_client:
            cached_k = self._check_ws_cache(price_cache, "kalshi", kalshi_ticker)

            # Check for stale feed
            if cached_k and cached_k.get("_stale", False):
                logger.info("Skipping revalidation: kalshi %s stale for >30s", kalshi_ticker)
                return False, 0.0, "feed_stale"

            if cached_k:
                k_yes = _cached_probability(cached_k, "yes_ask", "yes_price", "yes")
                k_no = _cached_probability(cached_k, "no_ask", "no_price", "no")
            if k_yes is None or k_no is None:
                book = self.kalshi_client.fetch_order_book(kalshi_ticker)
                if not book:
                    raise _RevalidationAPIError("failed to fetch Kalshi order book for cross")
                from kalshi_api import parse_orderbook, best_yes_ask, best_no_ask, _audit_raw_orderbook
                _audit_raw_orderbook(kalshi_ticker, book)
                parsed = parse_orderbook(book)
                # For arb: use the best ASK on each side (what we'd pay to buy).
                yes_ask = best_yes_ask(parsed)
                no_ask = best_no_ask(parsed)
                if yes_ask is not None:
                    k_yes = yes_ask[0]
                if no_ask is not None:
                    k_no = no_ask[0]

        if pm_yes is None or pm_no is None or k_yes is None or k_no is None:
            raise _RevalidationAPIError("incomplete prices after re-fetch for cross")

        # An inverted matched pair maps logical Kalshi YES/NO to the opposite
        # physical contract. Preserve that mapping through live revalidation;
        # otherwise the revalidator scores one pair of contracts while the
        # executor submits another.
        pair_inverted = bool(opp.get("_pair_inverted", False))
        logical_k_yes, logical_k_no = (
            (k_no, k_yes) if pair_inverted else (k_yes, k_no)
        )

        result1 = net_profit_cross_platform(pm_yes, logical_k_no, "yes", "no")
        result2 = net_profit_cross_platform(pm_no, logical_k_yes, "no", "yes")

        if result1["net_profit"] >= result2["net_profit"]:
            best = result1["net_profit"]
            total_cost = pm_yes + logical_k_no
            kalshi_side = "yes" if pair_inverted else "no"
            fresh_legs = [
                {
                    "platform": "polymarket",
                    "side": "BUY",
                    "token": "yes",
                    "price": pm_yes,
                    "_token_id": token_ids[0],
                },
                {
                    "platform": "kalshi",
                    "side": kalshi_side,
                    "action": "buy",
                    "price": logical_k_no,
                    "_ticker": kalshi_ticker,
                },
            ]
            opp["prices"] = f"PM_Y={pm_yes:.3f} K_{kalshi_side[0].upper()}={logical_k_no:.3f}"
        else:
            best = result2["net_profit"]
            total_cost = pm_no + logical_k_yes
            kalshi_side = "no" if pair_inverted else "yes"
            fresh_legs = [
                {
                    "platform": "polymarket",
                    "side": "BUY",
                    "token": "no",
                    "price": pm_no,
                    "_token_id": token_ids[1],
                },
                {
                    "platform": "kalshi",
                    "side": kalshi_side,
                    "action": "buy",
                    "price": logical_k_yes,
                    "_ticker": kalshi_ticker,
                },
            ]
            opp["prices"] = f"PM_N={pm_no:.3f} K_{kalshi_side[0].upper()}={logical_k_yes:.3f}"

        threshold = self._get_revalidation_threshold(original_profit, opp)
        if best < threshold:
            logger.info(
                "Revalidation: cross profit degraded %.4f -> %.4f (threshold=%.4f)",
                original_profit, best, threshold,
            )
            return False, best, "profit_below_floor"
        opp["net_profit"] = best
        opp["total_cost"] = f"${total_cost:.4f}"
        opp["net_roi"] = f"{best / total_cost * 100:.2f}%" if total_cost > 0 else "0%"
        # Every Polymarket/Kalshi cross that passes action-time revalidation
        # must execute the exact plan that was just scored.  Legacy one-shot
        # opportunities do not arrive with ``_cross_legs`` pre-populated, so
        # making this conditional would discard the fresh strategy and fall
        # back to stale scan-time routing in ``_build_legs``.
        opp["_cross_legs"] = fresh_legs
        opp["_kalshi_yes"] = k_yes
        opp["_kalshi_no"] = k_no
        return True, best, "passed"

    def _revalidate_kalshi_binary(
        self, opp: dict, original_profit: float
    ) -> tuple[bool, float, str]:
        """Revalidate a Kalshi binary opportunity.

        Returns:
            (passed, reval_profit, reason)
        """
        ticker = opp.get("_kalshi_ticker", "")
        if not ticker or not self.kalshi_client:
            raise _RevalidationAPIError("no ticker or no Kalshi client for kalshi_binary")
        book = self.kalshi_client.fetch_order_book(ticker)
        if not book:
            raise _RevalidationAPIError(f"failed to fetch Kalshi order book for {ticker}")
        from kalshi_api import parse_orderbook, best_yes_ask, best_no_ask, _audit_raw_orderbook
        _audit_raw_orderbook(ticker, book)
        parsed = parse_orderbook(book)
        # YES ask derives from best NO bid (and vice versa); both must exist
        # to validate a binary arbitrage.
        yes_ask = best_yes_ask(parsed)
        no_ask = best_no_ask(parsed)
        if yes_ask is None or no_ask is None:
            raise _RevalidationAPIError(f"missing ask side(s) for Kalshi {ticker}")
        k_yes = yes_ask[0]
        k_no = no_ask[0]

        result = net_profit_kalshi_binary(k_yes, k_no)
        reval_profit = result["net_profit"]
        threshold = self._get_revalidation_threshold(original_profit, opp)
        if reval_profit < threshold:
            logger.info(
                "Revalidation: kalshi_binary profit degraded %.4f -> %.4f (threshold=%.4f)",
                original_profit, reval_profit, threshold,
            )
            return False, reval_profit, "profit_below_floor"
        opp["net_profit"] = reval_profit
        opp["_kalshi_yes"] = k_yes
        opp["_kalshi_no"] = k_no
        return True, reval_profit, "passed"

    def _revalidate_kalshi_multi(
        self, opp: dict, original_profit: float
    ) -> tuple[bool, float, str]:
        """Revalidate a Kalshi multi-outcome opportunity.

        Also enforces a pre-trade depth check on EACH leg to prevent the
        Fill-or-Kill partial-fill trap: if any leg has fewer than
        config.KALSHI_MULTI_MIN_DEPTH contracts at the best ask, reject the
        opportunity rather than risk an unhedgeable partial fill.

        Returns:
            (passed, reval_profit, reason)
        """
        import config as _config
        min_depth = getattr(_config, "KALSHI_MULTI_MIN_DEPTH", 10)

        tickers = opp.get("_kalshi_tickers", [])
        if not tickers or not self.kalshi_client:
            raise _RevalidationAPIError("no tickers or no Kalshi client for kalshi_multi")
        yes_prices = []
        for ticker in tickers:
            book = self.kalshi_client.fetch_order_book(ticker)
            if not book:
                raise _RevalidationAPIError(f"failed to fetch Kalshi order book for {ticker}")
            from kalshi_api import parse_orderbook, best_yes_ask, _audit_raw_orderbook
            _audit_raw_orderbook(ticker, book)
            parsed = parse_orderbook(book)
            yes_ask = best_yes_ask(parsed)
            if yes_ask is None:
                raise _RevalidationAPIError(f"no YES ask available for Kalshi {ticker}")
            price, leg_depth = yes_ask[0], int(yes_ask[1])
            # Pre-trade depth gate: refuse to trade thin legs that will
            # cause fill_or_kill_insufficient_resting_volume rejections
            # and unhedgeable partial fills.
            if leg_depth < min_depth:
                logger.info(
                    "KalshiMulti depth gate: %s leg_depth=%d < min_depth=%d, rejecting",
                    ticker, leg_depth, min_depth,
                )
                return False, original_profit, "insufficient_leg_depth"
            yes_prices.append(price)
        result = net_profit_kalshi_multi(yes_prices)
        reval_profit = result["net_profit"]
        threshold = self._get_revalidation_threshold(original_profit, opp)
        if reval_profit < threshold:
            logger.info(
                "Revalidation: kalshi_multi profit degraded %.4f -> %.4f (threshold=%.4f)",
                original_profit, reval_profit, threshold,
            )
            return False, reval_profit, "profit_below_floor"
        opp["net_profit"] = reval_profit
        opp["_kalshi_prices"] = list(yes_prices)
        return True, reval_profit, "passed"

    def _revalidate_triangular(
        self, opp: dict, original_profit: float, price_cache: dict | None
    ) -> tuple[bool, float, str]:
        """Revalidate a TriangularCross opportunity by re-fetching prices from both platforms.

        Returns:
            (passed, reval_profit, reason)
        """
        platform_a = opp.get("_platform_a", "")
        platform_b = opp.get("_platform_b", "")
        prices_str = opp.get("prices", "")

        if not platform_a or not platform_b:
            raise _RevalidationAPIError("missing platform info for triangular")

        # Parse current prices from prices string: "{platform}_Y={price} {platform}_N={price}"
        parts = prices_str.split()
        if len(parts) != 2:
            raise _RevalidationAPIError("unexpected prices format for triangular")

        yes_price = no_price = None
        for part in parts:
            if "=" not in part:
                raise _RevalidationAPIError("malformed price label in triangular")
            label, val = part.split("=", 1)
            try:
                price = float(val)
            except ValueError:
                raise _RevalidationAPIError("non-numeric price in triangular")
            if label.endswith("_Y"):
                yes_price = price
            elif label.endswith("_N"):
                no_price = price

        if yes_price is None or no_price is None:
            raise _RevalidationAPIError("could not parse YES/NO prices for triangular")

        # Re-fetch YES-side price
        fresh_yes = self._refetch_platform_price(platform_a, opp, price_cache, "yes")
        # Re-fetch NO-side price
        fresh_no = self._refetch_platform_price(platform_b, opp, price_cache, "no")

        # If we couldn't refetch, use the original prices
        if fresh_yes is not None:
            yes_price = fresh_yes
        if fresh_no is not None:
            no_price = fresh_no

        # Recalculate profit with fresh prices
        result = net_profit_triangular(yes_price, no_price, platform_a, platform_b)
        reval_profit = result["net_profit"]
        threshold = self._get_revalidation_threshold(original_profit, opp)

        if reval_profit < threshold:
            logger.info(
                "Revalidation: triangular profit degraded %.4f -> %.4f (threshold=%.4f)",
                original_profit, reval_profit, threshold,
            )
            return False, reval_profit, "profit_below_floor"

        # Update opportunity with fresh prices
        opp["prices"] = f"{platform_a}_Y={yes_price:.3f} {platform_b}_N={no_price:.3f}"
        opp["net_profit"] = reval_profit
        return True, reval_profit, "passed"

    def _revalidate_multi_cross(
        self, opp: dict, original_profit: float, price_cache: dict | None
    ) -> tuple[bool, float, str]:
        """Revalidate a MultiCross opportunity by re-checking each leg's price.

        Also enforces a per-leg depth check on EACH Kalshi leg to prevent
        the Fill-or-Kill partial-fill trap. If any Kalshi leg has fewer than
        MULTI_CROSS_MIN_DEPTH contracts at best ask, reject the opportunity.

        Returns:
            (passed, reval_profit, reason)
        """
        import config as _config
        min_depth = getattr(_config, "MULTI_CROSS_MIN_DEPTH", 10)

        outcome_legs = opp.get("_outcome_legs", [])
        if not outcome_legs:
            raise _RevalidationAPIError("no outcome legs for multi_cross")

        prices = []
        platforms = []
        for leg in outcome_legs:
            platform = leg.get("platform", "")
            price = leg.get("price", 0)

            # Depth gate for Kalshi legs — check resting volume before
            # attempting FOK order that could leave unhedgeable orphans
            if platform == "kalshi" and self.kalshi_client:
                ticker = leg.get("_kalshi_ticker", "")
                if ticker:
                    try:
                        book = self.kalshi_client.fetch_order_book(ticker)
                        if book:
                            from kalshi_api import parse_orderbook, best_yes_ask, _audit_raw_orderbook
                            _audit_raw_orderbook(ticker, book)
                            yes_ask = best_yes_ask(parse_orderbook(book))
                            leg_depth = int(yes_ask[1]) if yes_ask else 0
                            if leg_depth < min_depth:
                                logger.info(
                                    "MultiCross depth gate: %s leg_depth=%d < min=%d, rejecting",
                                    ticker, leg_depth, min_depth,
                                )
                                return False, original_profit, "insufficient_leg_depth"
                    except Exception as e:
                        logger.debug("MultiCross depth check failed for %s: %s", ticker, e)

            # Try to get a fresh price from WS cache
            cache_key = leg.get("_token_id") or leg.get("_kalshi_ticker", "")
            cached = self._check_ws_cache(price_cache, platform, cache_key) if cache_key else None

            # Check for stale feed
            if cached and cached.get("_stale", False):
                logger.info("Skipping revalidation: %s %s stale for >30s", platform, cache_key)
                return False, 0.0, "feed_stale"

            if cached:
                if platform == "polymarket":
                    fresh_price = _cached_probability(cached, "best_ask", "ask", "price")
                else:
                    fresh_price = _cached_probability(cached, "yes_ask", "yes_price", "yes")
                prices.append(fresh_price if fresh_price is not None else float(price))
            else:
                prices.append(float(price))
            platforms.append(platform)

        result = net_profit_multi_cross(prices, platforms)
        reval_profit = result["net_profit"]
        threshold = self._get_revalidation_threshold(original_profit, opp)

        if reval_profit < threshold:
            logger.info(
                "Revalidation: multi-cross profit degraded %.4f -> %.4f (threshold=%.4f)",
                original_profit, reval_profit, threshold,
            )
            return False, reval_profit, "profit_below_floor"

        opp["net_profit"] = reval_profit
        return True, reval_profit, "passed"

    def _revalidate_logical_arb(
        self, opp: dict, original_profit: float,
    ) -> tuple[bool, float, str]:
        """Revalidate a LogicalArb opportunity by re-checking both markets' prices.

        Layer 4: Checks that neither market price has moved >10% from scan time.
        Returns:
            (passed, reval_profit, reason)
        """
        token_ids = opp.get("_token_ids", [])
        if_token_ids = opp.get("_if_token_ids", [])
        if len(token_ids) < 2 or len(if_token_ids) < 2:
            raise _RevalidationAPIError("logical_arb missing token IDs")

        # Fetch live prices for the underpriced outcome (then_yes)
        try:
            then_yes_token = token_ids[0]
            then_yes_book = fetch_order_book(then_yes_token)
            if_no_book = fetch_order_book(if_token_ids[1])
            if not then_yes_book or not if_no_book:
                raise _RevalidationAPIError("logical_arb CLOB unavailable")

            then_yes_data = get_best_bid_ask(then_yes_book)
            if_no_data = get_best_bid_ask(if_no_book)
            fresh_then_price = then_yes_data.get("ask")
            fresh_if_no_price = if_no_data.get("ask")
            if fresh_then_price is None or fresh_if_no_price is None:
                raise _RevalidationAPIError("logical_arb executable asks unavailable")
            original_then_price = opp.get("_then_price", fresh_then_price)

            # Check for >10% price movement (Layer 4 threshold)
            price_delta_pct = abs(fresh_then_price - original_then_price) / max(original_then_price, 0.001)
            if price_delta_pct > 0.10:
                logger.info(
                    "Logical arb then_yes price moved %.1f%% (%.4f -> %.4f), rejecting",
                    price_delta_pct * 100, original_then_price, fresh_then_price,
                )
                return False, 0.0, "price_moved_too_much"

            reval_profit = net_profit_logical_arb(1.0 - fresh_if_no_price, fresh_then_price)
            threshold = self._get_revalidation_threshold(original_profit, opp)
            if reval_profit < threshold:
                return False, reval_profit, "profit_below_floor"

            opp["_then_price"] = fresh_then_price
            opp["_if_no_price"] = fresh_if_no_price
            opp["net_profit"] = reval_profit

        except Exception as e:
            if isinstance(e, _RevalidationAPIError):
                raise
            raise _RevalidationAPIError(f"logical_arb CLOB fetch failed: {e}") from e

        return True, reval_profit, "passed"

    def _revalidate_whale_copy(
        self, opp: dict, original_profit: float,
    ) -> tuple[bool, float, str]:
        """Revalidate a WhaleCopy opportunity — check <30s latency and price stability.

        Layer 4: Rejects if whale trade is >30s old or market price moved >10%.
        Returns:
            (passed, reval_profit, reason)
        """
        import time as _time

        # Check latency budget: whale trade must be <30s old
        whale_ts = opp.get("_whale_timestamp", 0)
        if whale_ts:
            age_seconds = _time.time() - whale_ts
            if age_seconds > 30:
                logger.info(
                    "WhaleCopy trade too old (%.1fs > 30s), rejecting: %s",
                    age_seconds, opp.get("market", ""),
                )
                return False, 0.0, "stale_whale_trade"

        # Check market price hasn't moved >10%
        token_ids = opp.get("_token_ids", [])
        if token_ids:
            try:
                book = fetch_order_book(token_ids[0])
                if book:
                    asks = book.get("asks", [])
                    if asks:
                        fresh_price = float(asks[0].get("price", 0))
                        scan_price = opp.get("_market_price", fresh_price)
                        if scan_price > 0:
                            delta = abs(fresh_price - scan_price) / scan_price
                            if delta > 0.10:
                                logger.info(
                                    "WhaleCopy price moved %.1f%% (%.4f -> %.4f), rejecting",
                                    delta * 100, scan_price, fresh_price,
                                )
                                return False, 0.0, "price_moved_too_much"
                        opp["_market_price"] = fresh_price
            except Exception as e:
                logger.debug("WhaleCopy revalidation CLOB fetch failed: %s", e)
                return True, original_profit, "clob_error_accepted"

        return True, original_profit, "passed"

    def _refetch_platform_price(
        self, platform: str, opp: dict, price_cache: dict | None, side: str,
    ) -> float | None:
        """Re-fetch a single price from a platform for revalidation.

        Returns the fresh ask price, or None if unable to fetch.
        """
        if platform == "polymarket":
            token_ids = opp.get("_token_ids", [])
            idx = 0 if side == "yes" else (1 if len(token_ids) > 1 else 0)
            if idx < len(token_ids):
                tid = token_ids[idx]
                cached = self._check_ws_cache(price_cache, "polymarket", tid)

                # Check for stale feed
                if cached and cached.get("_stale", False):
                    logger.info("Skipping price refetch: polymarket %s stale for >30s", tid)
                    return None

                cached_ask = _cached_probability(cached, "best_ask", "ask", "price")
                if cached_ask is not None:
                    return cached_ask
                book = fetch_order_book(tid)
                if book:
                    data = get_best_bid_ask(book)
                    return data.get("ask")
        elif platform == "kalshi":
            ticker = opp.get("_kalshi_ticker", "")
            if ticker and self.kalshi_client:
                cached = self._check_ws_cache(price_cache, "kalshi", ticker)

                # Check for stale feed
                if cached and cached.get("_stale", False):
                    logger.info("Skipping price refetch: kalshi %s stale for >30s", ticker)
                    return None

                cached_ask = _cached_probability(
                    cached, f"{side}_ask", f"{side}_price", side,
                )
                if cached_ask is not None:
                    return cached_ask
                book = self.kalshi_client.fetch_order_book(ticker)
                if book:
                    from kalshi_api import parse_orderbook, best_yes_ask, best_no_ask, _audit_raw_orderbook
                    _audit_raw_orderbook(ticker, book)
                    parsed = parse_orderbook(book)
                    # Caller wants the price for trading at this side — use ASK.
                    ask = best_yes_ask(parsed) if side == "yes" else best_no_ask(parsed)
                    if ask is not None:
                        return ask[0]
        elif platform == "gemini":
            event_id = opp.get("_gm_event_id", "")
            if event_id and self.gemini_client:
                symbol = opp.get("_gm_yes_symbol" if side == "yes" else "_gm_no_symbol", "")
                if symbol:
                    book = self.gemini_client.get_order_book(symbol, limit=1)
                    if book and book.get("asks"):
                        return book["asks"][0].get("price")
        elif platform == "ibkr":
            event_id = opp.get("_ibkr_event_id", "")
            if event_id and self.ibkr_client:
                # Re-fetch via get_market_price with a minimal market dict
                conid_key = "_ibkr_yes_conid" if side == "yes" else "_ibkr_no_conid"
                conid = opp.get(conid_key, "")
                if conid:
                    mini_market = {
                        "contracts": [
                            {"conid": opp.get("_ibkr_yes_conid", ""), "side": "YES"},
                            {"conid": opp.get("_ibkr_no_conid", ""), "side": "NO"},
                        ]
                    }
                    y, n = self.ibkr_client.get_market_price(mini_market)
                    return y if side == "yes" else n
        # For betfair/smarkets/sxbet/matchbook: live order book, skip refetch
        return None

    def _per_leg_budget(self, opp_type: str, opportunity: dict, balances: dict | None) -> float | None:
        """Calculate per-leg budget based on platform balance and number of legs.

        For multi-leg arbs on the same platform, divides the available balance
        by the number of legs so the total trade fits within the balance.
        """
        if not balances:
            return None

        # Determine relevant balance
        if "Kalshi" in opp_type and "Cross" not in opp_type:
            balance = balances.get("kalshi")
        elif "Cross" in opp_type or "Triangular" in opp_type:
            # Cross/Triangular arbs have 1 leg per platform — use the smaller balance
            bal_values = [v for v in balances.values() if v is not None and v > 0]
            balance = min(bal_values) if bal_values else None
        elif opp_type.startswith("Betfair"):
            balance = balances.get("betfair")
        elif opp_type.startswith("Smarkets"):
            balance = balances.get("smarkets")
        elif opp_type.startswith("SXBet"):
            balance = balances.get("sxbet")
        elif opp_type.startswith("Matchbook"):
            balance = balances.get("matchbook")
        elif opp_type.startswith("Gemini"):
            balance = balances.get("gemini")
        elif opp_type.startswith("IBKR"):
            balance = balances.get("ibkr")
        else:
            balance = balances.get("polymarket")

        if balance is None or balance <= 0:
            return 0.0

        # Estimate number of legs on the same platform
        if opp_type == "Binary" or opp_type == "KalshiBinary":
            num_legs = 2
        elif opp_type.startswith("KalshiMulti") or opp_type.startswith("NegRisk"):
            # Parse count from type like "KalshiMulti(3)" or "NegRisk(5)"
            import re
            m = re.search(r'\((\d+)\)', opp_type)
            num_legs = int(m.group(1)) if m else len(opportunity.get("_token_ids", [])) or 2
        elif "Cross" in opp_type or "Triangular" in opp_type:
            num_legs = 1  # 1 leg per platform
        elif opp_type.endswith("BackAll"):
            # Multi-leg: count from selection/runner IDs
            ids_keys = ("_bf_selection_ids", "_sm_contract_ids", "_sx_outcome_ids", "_mb_runner_ids")
            for key in ids_keys:
                ids = opportunity.get(key, [])
                if ids:
                    num_legs = len(ids)
                    break
            else:
                num_legs = 2
        elif opp_type.endswith("BackLay"):
            num_legs = 2
        else:
            num_legs = 2

        return balance / num_legs

    def _fetch_balances(self, opp_type: str) -> dict | None:
        """Fetch balances from relevant platforms concurrently.

        Builds a list of (platform_name, callable) pairs based on the
        opportunity type, then submits all balance fetches in parallel
        via ThreadPoolExecutor. This reduces cross-platform balance
        fetching from sequential (200ms-2s) to parallel (~200ms).

        Args:
            opp_type: Opportunity type string (e.g. "Cross(PM_YES + K_NO)").

        Returns:
            Dict of {platform: balance} or None if no balances fetched.
        """
        needs_kalshi = "Kalshi" in opp_type or "Cross" in opp_type or "Triangular" in opp_type
        needs_polymarket = "Kalshi" not in opp_type or "Cross" in opp_type or "Triangular" in opp_type
        needs_exchange = (
            "Cross" in opp_type or "Triangular" in opp_type
            or opp_type.startswith("Betfair") or opp_type.startswith("Smarkets")
            or opp_type.startswith("SXBet") or opp_type.startswith("Matchbook")
            or opp_type.startswith("Gemini") or opp_type.startswith("IBKR")
            or opp_type == "EventDivergence"
        )

        # Build list of (platform_name, fetch_callable) to run in parallel
        fetch_tasks: list[tuple[str, callable]] = []
        if needs_kalshi and self.kalshi_client:
            fetch_tasks.append(("kalshi", self.kalshi_client.get_balance))
        if needs_polymarket and self.pm_trader:
            fetch_tasks.append(("polymarket", self.pm_trader.get_balance))
        if needs_exchange:
            exchange_clients = [
                ("betfair", self.betfair_client),
                ("smarkets", self.smarkets_client),
                ("sxbet", self.sxbet_client),
                ("matchbook", self.matchbook_client),
                ("gemini", self.gemini_client),
                ("ibkr", self.ibkr_client),
            ]
            for name, client in exchange_clients:
                if client and hasattr(client, "get_balance"):
                    fetch_tasks.append((name, client.get_balance))

        if not fetch_tasks:
            return None

        balances = {}
        with ThreadPoolExecutor(max_workers=len(fetch_tasks)) as pool:
            futures = {
                pool.submit(fn): name for name, fn in fetch_tasks
            }
            for future in as_completed(futures):
                name = futures[future]
                try:
                    bal = future.result()
                    if bal is not None:
                        balances[name] = bal
                except Exception as e:
                    logger.warning("Failed to fetch %s balance: %s", name, e)
        return balances if balances else None

    def _build_legs(self, opportunity: dict, size: float) -> list[dict]:
        """Build execution legs based on opportunity type."""
        opp_type = opportunity.get("type", "")
        token_ids = opportunity.get("_token_ids", [])
        legs = []

        if opp_type == "KalshiBinary":
            # Buy YES + NO on same Kalshi market
            prices = [opportunity["_kalshi_yes"], opportunity["_kalshi_no"]]
            package_cost = sum(prices)
            contract_count = max(1, int(size / package_cost)) if package_cost > 0 else 0
            legs = [
                {"platform": "kalshi", "side": "yes", "action": "buy",
                 "price": opportunity["_kalshi_yes"],
                 "_ticker": opportunity["_kalshi_ticker"],
                 "_contract_count": contract_count},
                {"platform": "kalshi", "side": "no", "action": "buy",
                 "price": opportunity["_kalshi_no"],
                 "_ticker": opportunity["_kalshi_ticker"],
                 "_contract_count": contract_count},
            ]
        elif opp_type.startswith("KalshiMulti"):
            # Buy YES on every outcome in a Kalshi event
            prices = list(opportunity["_kalshi_prices"])
            package_cost = sum(prices)
            contract_count = max(1, int(size / package_cost)) if package_cost > 0 else 0
            legs = [
                {"platform": "kalshi", "side": "yes", "action": "buy",
                 "price": price, "_ticker": ticker,
                 "_contract_count": contract_count}
                for ticker, price in zip(
                    opportunity["_kalshi_tickers"],
                    prices,
                )
            ]
        elif opp_type == "Binary":
            # Buy YES + NO on Polymarket
            yes_token = token_ids[0] if len(token_ids) > 0 else ""
            no_token = token_ids[1] if len(token_ids) > 1 else ""
            yes_price = self._parse_price(opportunity, "Y=")
            no_price = self._parse_price(opportunity, "N=")
            if yes_price is None or no_price is None:
                return []
            legs = [
                {"platform": "polymarket", "side": "BUY", "token": "yes",
                 "price": yes_price, "_token_id": yes_token},
                {"platform": "polymarket", "side": "BUY", "token": "no",
                 "price": no_price, "_token_id": no_token},
            ]
        elif opp_type.startswith("NegRiskNO"):
            # Buy NO on each outcome — Σ NO < (N-1) arbitrage.
            # Read the full price list from _no_prices (not the truncated summary string).
            no_prices = opportunity.get("_no_prices", [])
            # Every leg must have a real token ID: an empty list or any empty
            # string would produce invalid order-book fetches at execution.
            if not no_prices or len(no_prices) != len(token_ids) or not all(token_ids):
                logger.warning(f"NegRiskNO invalid legs: {len(no_prices)} prices vs {len(token_ids)} token IDs "
                               f"(empty IDs: {sum(1 for t in token_ids if not t)}). Skipping.")
                return []
            for i, price in enumerate(no_prices):
                legs.append({
                    "platform": "polymarket",
                    "side": "BUY",
                    "token": f"no_{i}",
                    "price": price,
                    "_token_id": token_ids[i],
                })
        elif opp_type.startswith("NegRisk"):
            # Buy YES on each outcome — prices are comma-separated in the opp
            prices_str = opportunity.get("prices", "")
            prices = []
            for p in prices_str.split(","):
                p = p.strip()
                # Skip trailing summary like "... (N total)"
                if not p or p.startswith("(") or p == "...":
                    continue
                try:
                    prices.append(float(p))
                except ValueError:
                    continue
            # Validate price count matches token ID count
            if token_ids and len(prices) != len(token_ids):
                logger.warning(f"NegRisk price count ({len(prices)}) != token ID count ({len(token_ids)}). Skipping.")
                return []
            for i, price in enumerate(prices):
                legs.append({
                    "platform": "polymarket",
                    "side": "BUY",
                    "token": f"yes_{i}",
                    "price": price,
                    "_token_id": token_ids[i] if i < len(token_ids) else "",
                })
        elif opp_type.startswith("Spread"):
            # Spread capture: BUY then SELL on same token
            token_id = opportunity.get("_token_id", "")
            buy_price = opportunity.get("_ask_price", 0)
            sell_price = opportunity.get("_bid_price", 0)
            spread_platform = opportunity.get("_spread_platform", "polymarket")
            if spread_platform == "polymarket":
                legs = [
                    {"platform": "polymarket", "side": "BUY", "token": "yes",
                     "price": buy_price, "_token_id": token_id},
                    {"platform": "polymarket", "side": "SELL", "token": "yes",
                     "price": sell_price, "_token_id": token_id},
                ]
            elif spread_platform == "kalshi":
                ticker = opportunity.get("_kalshi_ticker", "")
                side = opportunity.get("_spread_side", "yes")
                legs = [
                    {"platform": "kalshi", "side": side, "action": "buy",
                     "price": buy_price, "_ticker": ticker},
                    {"platform": "kalshi", "side": side, "action": "sell",
                     "price": sell_price, "_ticker": ticker},
                ]
        elif opp_type == "BetfairBackAll":
            legs = [
                {"platform": "betfair", "side": "BACK",
                 "price": price, "_market_id": opportunity.get("_bf_market_id", ""),
                 "_selection_id": sel_id}
                for sel_id, price in zip(
                    opportunity.get("_bf_selection_ids", []),
                    opportunity.get("_bf_prices", []),
                )
            ]
        elif opp_type == "BetfairBackLay":
            market_id = opportunity.get("_bf_market_id", "")
            sel_id = opportunity.get("_bf_selection_id")
            legs = [
                {"platform": "betfair", "side": "BACK",
                 "price": opportunity.get("_bf_back_price", 0),
                 "_market_id": market_id, "_selection_id": sel_id},
                {"platform": "betfair", "side": "LAY",
                 "price": opportunity.get("_bf_lay_price", 0),
                 "_market_id": market_id, "_selection_id": sel_id},
            ]
        elif opp_type == "SmarketsBackAll":
            legs = [
                {"platform": "smarkets", "side": "BACK",
                 "price": price, "_market_id": opportunity.get("_sm_market_id", ""),
                 "_contract_id": cid}
                for cid, price in zip(
                    opportunity.get("_sm_contract_ids", []),
                    opportunity.get("_sm_prices", []),
                )
            ]
        elif opp_type == "SmarketsBackLay":
            legs = [
                {"platform": "smarkets", "side": "BACK",
                 "price": opportunity.get("_sm_back_price", 0),
                 "_market_id": opportunity.get("_sm_market_id", ""),
                 "_contract_id": opportunity.get("_sm_contract_id")},
                {"platform": "smarkets", "side": "LAY",
                 "price": opportunity.get("_sm_lay_price", 0),
                 "_market_id": opportunity.get("_sm_market_id", ""),
                 "_contract_id": opportunity.get("_sm_contract_id")},
            ]
        elif opp_type == "SXBetBackAll":
            legs = [
                {"platform": "sxbet", "side": "BACK",
                 "price": price, "_market_hash": opportunity.get("_sx_market_hash", ""),
                 "_outcome_id": oid}
                for oid, price in zip(
                    opportunity.get("_sx_outcome_ids", []),
                    opportunity.get("_sx_prices", []),
                )
            ]
        elif opp_type == "SXBetBackLay":
            legs = [
                {"platform": "sxbet", "side": "BACK",
                 "price": opportunity.get("_sx_back_price", 0),
                 "_market_hash": opportunity.get("_sx_market_hash", ""),
                 "_outcome_id": opportunity.get("_sx_outcome_id")},
                {"platform": "sxbet", "side": "LAY",
                 "price": opportunity.get("_sx_lay_price", 0),
                 "_market_hash": opportunity.get("_sx_market_hash", ""),
                 "_outcome_id": opportunity.get("_sx_outcome_id")},
            ]
        elif opp_type == "MatchbookBackAll":
            legs = [
                {"platform": "matchbook", "side": "BACK",
                 "price": price, "_market_id": opportunity.get("_mb_market_id", ""),
                 "_runner_id": rid}
                for rid, price in zip(
                    opportunity.get("_mb_runner_ids", []),
                    opportunity.get("_mb_prices", []),
                )
            ]
        elif opp_type == "MatchbookBackLay":
            market_id = opportunity.get("_mb_market_id", "")
            runner_id = opportunity.get("_mb_runner_id")
            legs = [
                {"platform": "matchbook", "side": "BACK",
                 "price": opportunity.get("_mb_back_price", 0),
                 "_market_id": market_id, "_runner_id": runner_id},
                {"platform": "matchbook", "side": "LAY",
                 "price": opportunity.get("_mb_lay_price", 0),
                 "_market_id": market_id, "_runner_id": runner_id},
            ]
        elif opp_type == "GeminiBinary":
            if "gemini" not in ENABLED_EXECUTION_PLATFORMS:
                logger.debug("Skipping GeminiBinary: gemini not in ENABLED_EXECUTION_PLATFORMS")
                return []
            yes_price = opportunity.get("_gm_yes_price", self._parse_price(opportunity, "Y="))
            no_price = opportunity.get("_gm_no_price", self._parse_price(opportunity, "N="))
            if yes_price is None or no_price is None:
                return []
            legs = [
                {"platform": "gemini", "symbol": opportunity.get("_gm_yes_symbol", ""),
                 "side": "buy", "outcome": "yes", "price": yes_price},
                {"platform": "gemini", "symbol": opportunity.get("_gm_no_symbol", ""),
                 "side": "buy", "outcome": "no", "price": no_price},
            ]
        elif opp_type == "GeminiMulti":
            if "gemini" not in ENABLED_EXECUTION_PLATFORMS:
                logger.debug("Skipping GeminiMulti: gemini not in ENABLED_EXECUTION_PLATFORMS")
                return []
            symbols = opportunity.get("_gm_symbols", [])
            prices = opportunity.get("_gm_prices", [])
            for symbol, price in zip(symbols, prices):
                legs.append({
                    "platform": "gemini", "symbol": symbol,
                    "side": "buy", "outcome": "yes", "price": price,
                })
        elif opp_type == "IBKRBinary":
            if "ibkr" not in ENABLED_EXECUTION_PLATFORMS:
                logger.debug("Skipping IBKRBinary: ibkr not in ENABLED_EXECUTION_PLATFORMS")
                return []
            yes_price = opportunity.get("_ibkr_yes_price", self._parse_price(opportunity, "Y="))
            no_price = opportunity.get("_ibkr_no_price", self._parse_price(opportunity, "N="))
            if yes_price is None or no_price is None:
                return []
            legs = [
                {"platform": "ibkr", "conid": opportunity.get("_ibkr_yes_conid", ""),
                 "side": "buy", "price": yes_price},
                {"platform": "ibkr", "conid": opportunity.get("_ibkr_no_conid", ""),
                 "side": "buy", "price": no_price},
            ]
        elif opp_type.startswith("MultiCross"):
            # Multi-outcome cross-platform: buy cheapest YES per outcome across platforms
            outcome_legs = opportunity.get("_outcome_legs", [])
            for leg in outcome_legs:
                plat = leg.get("platform", "")
                if plat and plat not in ENABLED_EXECUTION_PLATFORMS:
                    logger.info(
                        "MultiCross leg on '%s' blocked — not in ENABLED_EXECUTION_PLATFORMS",
                        plat,
                    )
                    return []
                if plat == "polymarket":
                    legs.append({
                        "platform": "polymarket", "side": "BUY", "token": "yes",
                        "price": leg["price"],
                        "_token_id": leg.get("_token_id", ""),
                    })
                elif plat == "kalshi":
                    legs.append({
                        "platform": "kalshi", "side": "yes", "action": "buy",
                        "price": leg["price"],
                        "_ticker": leg.get("_kalshi_ticker", ""),
                    })
        elif opp_type == "TriangularCross":
            # 3-way cross-platform: cheapest YES + cheapest NO across 3+ platforms
            legs = self._build_cross_all_legs(opportunity, size)
        elif opp_type == "EventDivergence":
            # Single-leg directional trade based on Metaculus divergence signal
            legs = self._build_event_divergence_legs(opportunity, size)
        elif opp_type in ("StalePriceOpp", "ResolutionSnipeOpp", "ConvergenceOpp"):
            # Layer 2-4: single-leg directional trades
            legs = self._build_directional_legs(opportunity, size)
        elif opp_type == "PolymarketRewards":
            # Layer 3: Polymarket liquidity rewards — resting limit orders
            optimal_bid = opportunity.get("optimal_bid", 0)
            optimal_ask = opportunity.get("optimal_ask", 0)
            reward_size = opportunity.get("size", size)

            if not optimal_bid or not optimal_ask:
                return []

            # Determine sides based on midpoint range (Polymarket reward rule)
            mid = (optimal_bid + optimal_ask) / 2
            sides = []
            if 0.10 <= mid <= 0.90:
                # Single-sided OK; post both for better reward score
                sides = [
                    {"platform": "polymarket", "side": "BUY", "price": optimal_bid,
                     "token": "yes", "_market_key": opportunity.get("_market_key", "")},
                    {"platform": "polymarket", "side": "SELL", "price": optimal_ask,
                     "token": "yes", "_market_key": opportunity.get("_market_key", "")},
                ]
            else:
                # Outside range: must post both sides anyway for reward eligibility
                sides = [
                    {"platform": "polymarket", "side": "BUY", "price": optimal_bid,
                     "token": "yes", "_market_key": opportunity.get("_market_key", "")},
                    {"platform": "polymarket", "side": "SELL", "price": optimal_ask,
                     "token": "yes", "_market_key": opportunity.get("_market_key", "")},
                ]

            legs = sides
        elif opp_type == "KalshiRewards":
            # Layer 3: Kalshi liquidity incentive program — resting limit orders
            optimal_bid = opportunity.get("optimal_bid", 0)
            optimal_ask = opportunity.get("optimal_ask", 0)
            reward_size = opportunity.get("size", size)
            market_ticker = opportunity.get("market_ticker", "")

            if not optimal_bid or not optimal_ask or not market_ticker:
                return []

            sides = [
                {"platform": "kalshi", "side": "yes", "action": "buy",
                 "price": optimal_bid, "_ticker": market_ticker},
                {"platform": "kalshi", "side": "yes", "action": "sell",
                 "price": optimal_ask, "_ticker": market_ticker},
            ]

            legs = sides
        elif opp_type == "MarketMake":
            # Layer 3: market making — bid+ask pair
            legs = self._build_mm_legs(opportunity, size)
        elif opp_type == "FeePromo":
            # Strategy #9: cached cross-platform near-miss that re-cleared
            # MIN_NET_ROI after a fee rate drop. The opp dict carries the
            # original PM_Y/K_N prices string and _platform_a/_platform_b,
            # so the generic cross-all builder handles it cleanly.
            legs = self._build_cross_all_legs(opportunity, size)
        elif opp_type == "CrossPlatformMM":
            # Strategy #11: paired bid/ask across two platforms. The scan
            # pre-builds both legs as ``_leg_a`` / ``_leg_b`` so the executor
            # just unpacks them.
            leg_a = opportunity.get("_leg_a")
            leg_b = opportunity.get("_leg_b")
            legs = [leg for leg in (leg_a, leg_b) if leg]
        elif opp_type.startswith("NWayArb"):
            # Sprint 4: N-way cross-platform arbitrage. The scan emits the
            # cheapest-YES platform as _platform_a (price _price_a) and the
            # cheapest-NO platform as _platform_b (price _price_b). Build two
            # legs and reuse _build_single_cross_leg for per-platform shape.
            platform_yes = opportunity.get("_platform_a", "")
            platform_no = opportunity.get("_platform_b", "")
            price_yes = opportunity.get("_price_a")
            price_no = opportunity.get("_price_b")
            if not platform_yes or not platform_no or price_yes is None or price_no is None:
                legs = []
            else:
                blocked = False
                for plat in (platform_yes, platform_no):
                    if plat not in ENABLED_EXECUTION_PLATFORMS:
                        logger.info(
                            "NWayArb leg on '%s' blocked — not in ENABLED_EXECUTION_PLATFORMS",
                            plat,
                        )
                        blocked = True
                        break
                if blocked:
                    legs = []
                else:
                    leg_a = self._build_single_cross_leg(
                        f"{platform_yes}_Y={price_yes}", platform_yes, platform_no,
                        opportunity, token_ids, is_first=True,
                    )
                    leg_b = self._build_single_cross_leg(
                        f"{platform_no}_N={price_no}", platform_no, platform_yes,
                        opportunity, token_ids, is_first=False,
                    )
                    legs = [leg for leg in (leg_a, leg_b) if leg]
                    if len(legs) != 2:
                        legs = []
        elif opp_type == "LeadLagMM":
            # Sprint 4: directional quote on the lagging platform anchored to
            # the leader's fair value. Single BUY-YES leg at fair_value.
            lagger = opportunity.get("_lagger", "")
            fair_value = opportunity.get("_fair_value")
            if not lagger or fair_value is None:
                legs = []
            elif lagger not in ENABLED_EXECUTION_PLATFORMS:
                logger.info(
                    "LeadLagMM leg on '%s' blocked — not in ENABLED_EXECUTION_PLATFORMS",
                    lagger,
                )
                legs = []
            else:
                leg = {
                    "platform": lagger,
                    "side": "BUY" if lagger == "polymarket" else "yes",
                    "price": float(fair_value),
                    "token": "yes",
                    "_market_key": opportunity.get("_market_key", ""),
                    "_leader": opportunity.get("_leader", ""),
                }
                if lagger == "polymarket":
                    leg["_token_id"] = token_ids[0] if token_ids else ""
                elif lagger == "kalshi":
                    leg["action"] = "buy"
                    leg["_ticker"] = opportunity.get("_market_key", "")
                legs = [leg]
        elif opp_type in ("ToxicFlowPause", "VolatilityAdjustedMM"):
            # Sprint 4: observability-only opps. ToxicFlowPause is a defensive
            # pause signal; VolatilityAdjustedMM informs MM spread-widening
            # rather than a placeable trade. Return [] so executor skips both.
            logger.info(
                "%s is observability-only — no execution legs built", opp_type,
            )
            legs = []
        elif opp_type.startswith("Cross"):
            explicit_cross_legs = opportunity.get("_cross_legs")
            if isinstance(explicit_cross_legs, list) and explicit_cross_legs:
                return [dict(leg) for leg in explicit_cross_legs]

            # Re-validate fee path if scan provided a hint (per user decision: confirm or override)
            fee_path = opportunity.get("_fee_path")
            if fee_path:
                fresh = find_lowest_fee_path(
                    [fee_path["best_yes_platform"], fee_path["best_no_platform"]],
                    {fee_path["best_yes_platform"]: fee_path["yes_price"]},
                    {fee_path["best_no_platform"]: fee_path["no_price"]},
                )
                if fresh and fresh["net_profit"] > 0:
                    logger.info(
                        f"Fee path re-validated: {fresh['best_yes_platform']} YES + "
                        f"{fresh['best_no_platform']} NO, net=${fresh['net_profit']:.4f}"
                    )
                    yes_plat = fresh["best_yes_platform"]
                    no_plat = fresh["best_no_platform"]
                    yes_price = fresh["yes_price"]
                    no_price = fresh["no_price"]

                    # Build YES leg using fee-path-optimal platform
                    yes_leg: dict = {"platform": yes_plat, "side": "BUY", "token": "yes",
                                     "price": yes_price}
                    if yes_plat == "polymarket":
                        yes_leg["_token_id"] = token_ids[0] if len(token_ids) > 0 else ""
                    elif yes_plat == "kalshi":
                        yes_leg["side"] = "yes"
                        yes_leg["action"] = "buy"
                        yes_leg["_ticker"] = opportunity.get("_kalshi_ticker", "")

                    # Build NO leg using fee-path-optimal platform
                    no_leg: dict = {"platform": no_plat, "side": "BUY", "token": "no",
                                    "price": no_price}
                    if no_plat == "polymarket":
                        no_leg["_token_id"] = token_ids[1] if len(token_ids) > 1 else ""
                    elif no_plat == "kalshi":
                        no_leg["side"] = "no"
                        no_leg["action"] = "buy"
                        no_leg["_ticker"] = opportunity.get("_kalshi_ticker", "")

                    legs = [yes_leg, no_leg]
                    return legs
                else:
                    logger.info("Fee path stale (no longer profitable), falling back to default routing")
                    # Fall through to default prices_str routing below

            # Default routing: parse prices_str (backward compatible)
            prices_str = opportunity.get("prices", "")
            kalshi_ticker = opportunity.get("_kalshi_ticker", "")
            if "PM_Y=" in prices_str and "K_N=" in prices_str:
                yes_token = token_ids[0] if len(token_ids) > 0 else ""
                pm_price = self._parse_price(opportunity, "PM_Y=")
                k_price = self._parse_price(opportunity, "K_N=")
                if pm_price is None or k_price is None:
                    return []
                legs = [
                    {"platform": "polymarket", "side": "BUY", "token": "yes",
                     "price": pm_price, "_token_id": yes_token},
                    {"platform": "kalshi", "side": "no", "action": "buy",
                     "price": k_price, "_ticker": kalshi_ticker},
                ]
            elif "PM_N=" in prices_str and "K_Y=" in prices_str:
                no_token = token_ids[1] if len(token_ids) > 1 else ""
                pm_price = self._parse_price(opportunity, "PM_N=")
                k_price = self._parse_price(opportunity, "K_Y=")
                if pm_price is None or k_price is None:
                    return []
                legs = [
                    {"platform": "polymarket", "side": "BUY", "token": "no",
                     "price": pm_price, "_token_id": no_token},
                    {"platform": "kalshi", "side": "yes", "action": "buy",
                     "price": k_price, "_ticker": kalshi_ticker},
                ]
            elif "_platform_a" in opportunity:
                # Generic cross-platform handler for cross-all opportunities
                legs = self._build_cross_all_legs(opportunity, size)
        elif opp_type == "Imbalance":
            # STRAT-01: Order Book Imbalance — buy on predicted direction
            direction = opportunity.get("_direction", "YES")
            token_ids = opportunity.get("_token_ids", [])
            if not token_ids or len(token_ids) < 2:
                raise ValueError(f"Imbalance opp missing token IDs: {opportunity}")

            yes_token = token_ids[0]
            no_token = token_ids[1]

            if direction == "YES":
                legs = [{"platform": "polymarket", "side": "BUY", "token": "yes",
                         "price": opportunity.get("_yes_price", 0), "_token_id": yes_token}]
            else:
                legs = [{"platform": "polymarket", "side": "BUY", "token": "no",
                         "price": opportunity.get("_no_price", 0), "_token_id": no_token}]
        elif opp_type == "NewsSnipe":
            # STRAT-02: News Snipe — buy sentiment side at market (taker order)
            sentiment = opportunity.get("_sentiment", "YES")
            token_ids = opportunity.get("_token_ids", [])
            if not token_ids or len(token_ids) < 2:
                raise ValueError(f"NewsSnipe opp missing token IDs: {opportunity}")

            yes_token = token_ids[0]
            no_token = token_ids[1]

            if sentiment == "YES":
                legs = [{"platform": "polymarket", "side": "BUY", "token": "yes",
                         "price": opportunity.get("_yes_price", 0), "_token_id": yes_token}]
            else:
                legs = [{"platform": "polymarket", "side": "BUY", "token": "no",
                         "price": opportunity.get("_no_price", 0), "_token_id": no_token}]
        elif opp_type == "Correlated":
            # STRAT-06: Correlated Pairs — long underpriced, short overpriced
            long_leg = opportunity.get("_long_leg", {})
            short_leg = opportunity.get("_short_leg", {})
            long_token_ids = long_leg.get("_token_ids", [])
            short_token_ids = short_leg.get("_token_ids", [])

            if not long_token_ids or not short_token_ids:
                raise ValueError(f"Correlated opp missing token IDs: {opportunity}")

            legs = [
                {"platform": "polymarket", "side": "BUY", "token": "yes",
                 "price": long_leg.get("_yes_price", 0), "_token_id": long_token_ids[0]},
                {"platform": "polymarket", "side": "SELL", "token": "yes",
                 "price": short_leg.get("_yes_price", 0), "_token_id": short_token_ids[0]},
            ]
        elif opp_type == "TimeDecay":
            # STRAT-07: Time Decay — buy high-consensus outcome at discount
            consensus_side = opportunity.get("_consensus_side", "YES")
            token_ids = opportunity.get("_token_ids", [])
            if not token_ids or len(token_ids) < 2:
                raise ValueError(f"TimeDecay opp missing token IDs: {opportunity}")

            yes_token = token_ids[0]
            no_token = token_ids[1]

            if consensus_side == "YES":
                legs = [{"platform": "polymarket", "side": "BUY", "token": "yes",
                         "price": opportunity.get("_yes_price", 0), "_token_id": yes_token}]
            else:
                legs = [{"platform": "polymarket", "side": "BUY", "token": "no",
                         "price": opportunity.get("_no_price", 0), "_token_id": no_token}]

        elif opp_type == "LogicalArb":
            # STRAT-04: Combinatorial Logical Arbitrage — buy underpriced implied outcome, sell implying
            # Example: Bitcoin >$100k (if_yes) implies Bitcoin >$90k (then_yes).
            # If P(>$90k) < P(>$100k), buy >$90k and sell >$100k for arbitrage.
            token_ids = opportunity.get("_token_ids", [])
            if_token_ids = opportunity.get("_if_token_ids", [])
            if len(token_ids) < 2 or len(if_token_ids) < 2:
                logger.warning("LogicalArb opp missing token IDs: %s", opportunity)
                return []

            then_yes_token = token_ids[0]
            if_no_token = if_token_ids[1]

            if not then_yes_token or not if_no_token:
                logger.warning("LogicalArb opp missing required token IDs for both outcomes")
                return []

            legs = [
                {"platform": "polymarket", "side": "BUY", "token": "yes",
                 "price": opportunity.get("_then_price", 0), "_token_id": then_yes_token},
                {"platform": "polymarket", "side": "BUY", "token": "no",
                 "price": opportunity.get("_if_no_price", 1.0 - opportunity.get("_if_price", 0)),
                 "_token_id": if_no_token},
            ]

        elif opp_type == "WhaleCopy":
            # STRAT-05: Whale copy trading — mirror whale trader's position
            token_ids = opportunity.get("_token_ids", [])
            if not token_ids:
                logger.warning("WhaleCopy opp missing token IDs: %s", opportunity.get("market", ""))
                return []
            legs = [
                {"platform": "polymarket", "side": "BUY", "token": "yes",
                 "price": opportunity.get("_market_price", 0),
                 "_token_id": token_ids[0]},
            ]

        # HARDEN-05: Attach a per-leg idempotency key before returning.
        # Keys are stable within a 60-second minute bucket so that retries
        # within the same minute are identifiable as duplicates by the platform.
        market_id = opportunity.get("market", "")
        for leg in legs:
            side = leg.get("side", leg.get("token", leg.get("outcome", "")))
            leg["_idempotency_key"] = _make_idempotency_key(
                market_id=market_id,
                side=str(side),
                price=float(leg.get("price", 0)),
            )

        return legs

    def _build_cross_all_legs(self, opportunity: dict, size: float) -> list[dict]:
        """Build execution legs for generic cross-platform opportunities."""
        prices_str = opportunity.get("prices", "")
        platform_a = opportunity.get("_platform_a", "")
        platform_b = opportunity.get("_platform_b", "")

        # Reject early if either platform is not whitelisted
        for plat in (platform_a, platform_b):
            if plat and plat not in ENABLED_EXECUTION_PLATFORMS:
                logger.info(
                    f"Cross-platform leg on '{plat}' blocked — "
                    f"not in ENABLED_EXECUTION_PLATFORMS"
                )
                return []
            min_size = PLATFORM_MIN_ORDER_SIZE.get(plat, 0)
            if plat and size > 0 and size / 2 < min_size:
                logger.info(
                    f"Cross-platform leg on '{plat}' blocked — "
                    f"per-leg size ${size / 2:.2f} below minimum ${min_size:.2f}"
                )
                return []
        token_ids = opportunity.get("_token_ids", [])

        # Parse prices: format is "{platform}_Y={price} {platform}_N={price}"
        parts = prices_str.split()
        if len(parts) != 2:
            return []

        leg_a = self._build_single_cross_leg(
            parts[0], platform_a, platform_b, opportunity, token_ids, is_first=True)
        leg_b = self._build_single_cross_leg(
            parts[1], platform_b, platform_a, opportunity, token_ids, is_first=False)

        if leg_a is None or leg_b is None:
            return []
        return [leg_a, leg_b]

    def _build_single_cross_leg(
        self, price_part: str, expected_platform: str, other_platform: str,
        opportunity: dict, token_ids: list, is_first: bool,
    ) -> dict | None:
        """Parse a single price part and build a leg dict."""
        # price_part format: "{platform}_Y={price}" or "{platform}_N={price}"
        if "=" not in price_part:
            return None

        label, val = price_part.split("=", 1)
        try:
            price = float(val)
        except ValueError:
            return None
        if price <= 0 or price >= 1:
            return None

        # Determine platform and side from label
        if label.endswith("_Y"):
            platform_name = label[:-2]
            side = "yes"
        elif label.endswith("_N"):
            platform_name = label[:-2]
            side = "no"
        else:
            return None

        leg = {"price": price}

        if platform_name == "polymarket":
            token_idx = 0 if side == "yes" else 1
            leg["platform"] = "polymarket"
            leg["side"] = "BUY"
            leg["token"] = side
            leg["_token_id"] = token_ids[token_idx] if token_idx < len(token_ids) else ""
        elif platform_name == "kalshi":
            leg["platform"] = "kalshi"
            leg["side"] = side
            leg["action"] = "buy"
            leg["_ticker"] = opportunity.get("_kalshi_ticker", "")
        elif platform_name == "betfair":
            leg["platform"] = "betfair"
            leg["side"] = side
            leg["_market_id"] = opportunity.get("_market_id", "")
            leg["_selection_id"] = opportunity.get("_selection_id")
        elif platform_name == "smarkets":
            leg["platform"] = "smarkets"
            leg["side"] = side
            leg["_market_id"] = opportunity.get("_sm_market_id", "")
        elif platform_name == "sxbet":
            leg["platform"] = "sxbet"
            leg["side"] = side
            leg["_market_hash"] = opportunity.get("_sx_market_hash", "")
        elif platform_name == "matchbook":
            leg["platform"] = "matchbook"
            leg["side"] = side
            leg["_market_id"] = opportunity.get("_mb_market_id", "")
            leg["_runner_id"] = opportunity.get("_mb_runner_id")
        elif platform_name == "gemini":
            leg["platform"] = "gemini"
            leg["side"] = "buy"
            leg["outcome"] = side
            leg["symbol"] = opportunity.get(
                "_gm_yes_symbol" if side == "yes" else "_gm_no_symbol", "")
        elif platform_name == "ibkr":
            leg["platform"] = "ibkr"
            leg["side"] = "buy"
            leg["conid"] = opportunity.get(
                "_ibkr_yes_conid" if side == "yes" else "_ibkr_no_conid", "")
        else:
            return None

        return leg

    def _build_event_divergence_legs(self, opportunity: dict, size: float) -> list[dict]:
        """Build execution legs for an EventDivergence opportunity.

        EventDivergence is a single-leg directional trade: buy YES or NO on
        one platform based on Metaculus divergence signal.
        """
        platform = opportunity.get("_platform", "")
        direction = opportunity.get("_direction", "")
        prices_str = opportunity.get("prices", "")

        # Extract platform price from "platform={price} metaculus={price}"
        platform_price = None
        for part in prices_str.split():
            if part.startswith("platform="):
                try:
                    platform_price = float(part.split("=", 1)[1])
                except ValueError as e:
                    logger.debug("Ignoring unparseable price part: %s", e)
                break

        if not platform or not direction or platform_price is None:
            return []

        if direction == "BUY_YES":
            side = "yes"
            price = platform_price
        elif direction == "BUY_NO":
            side = "no"
            price = 1.0 - platform_price  # NO price is complement
        else:
            return []

        leg = {"price": price, "side": side}

        if platform == "polymarket":
            token_ids = opportunity.get("_token_ids", [])
            token_idx = 0 if side == "yes" else 1
            leg["platform"] = "polymarket"
            leg["side"] = "BUY"
            leg["token"] = side
            leg["_token_id"] = token_ids[token_idx] if token_idx < len(token_ids) else ""
        elif platform == "kalshi":
            leg["platform"] = "kalshi"
            leg["action"] = "buy"
            leg["_ticker"] = opportunity.get("_kalshi_ticker", "")
        elif platform == "betfair":
            leg["platform"] = "betfair"
            leg["side"] = "BACK" if direction == "BUY_YES" else "LAY"
            leg["_market_id"] = opportunity.get("_market_id", "")
            leg["_selection_id"] = opportunity.get("_selection_id")
        elif platform == "smarkets":
            leg["platform"] = "smarkets"
            leg["side"] = "BACK" if direction == "BUY_YES" else "LAY"
            leg["_market_id"] = opportunity.get("_sm_market_id", "")
            leg["_contract_id"] = opportunity.get("_sm_contract_id", "")
        elif platform == "sxbet":
            leg["platform"] = "sxbet"
            leg["side"] = "BACK" if direction == "BUY_YES" else "LAY"
            leg["_market_hash"] = opportunity.get("_sx_market_hash", "")
            leg["_outcome_id"] = opportunity.get("_sx_outcome_id", "")
        elif platform == "matchbook":
            leg["platform"] = "matchbook"
            leg["side"] = "back" if direction == "BUY_YES" else "lay"
            leg["_market_id"] = opportunity.get("_mb_market_id", "")
            leg["_runner_id"] = opportunity.get("_mb_runner_id", "")
        elif platform == "gemini":
            leg["platform"] = "gemini"
            leg["side"] = "buy"
            leg["outcome"] = "yes" if direction == "BUY_YES" else "no"
            leg["symbol"] = opportunity.get(
                "_gm_yes_symbol" if direction == "BUY_YES" else "_gm_no_symbol", "")
        elif platform == "ibkr":
            # IBKR: BUY_YES = buy YES conid, BUY_NO = buy NO conid (both are BUY)
            leg["platform"] = "ibkr"
            leg["side"] = "buy"
            leg["conid"] = opportunity.get(
                "_ibkr_yes_conid" if direction == "BUY_YES" else "_ibkr_no_conid", "")
        else:
            return []

        return [leg]

    def _build_directional_legs(self, opportunity: dict, size: float) -> list[dict]:
        """Build execution legs for directional opportunities (Stale, Resolution, Convergence).

        These are single-leg trades similar to EventDivergence — buy YES or NO
        on a single platform based on signal.
        """
        platform = opportunity.get("_platform", "")
        direction = opportunity.get("_direction", "")
        trade_price = opportunity.get("_trade_price") or opportunity.get("_price", 0)

        if not platform or not direction or not trade_price:
            return []

        if platform not in ENABLED_EXECUTION_PLATFORMS:
            logger.info("Directional leg on '%s' blocked — not in ENABLED_EXECUTION_PLATFORMS", platform)
            return []

        side = "yes" if direction == "BUY_YES" else "no"
        leg = {"price": trade_price, "side": side}

        if platform == "polymarket":
            token_ids = opportunity.get("_token_ids", [])
            token_idx = 0 if side == "yes" else 1
            leg["platform"] = "polymarket"
            leg["side"] = "BUY"
            leg["token"] = side
            leg["_token_id"] = token_ids[token_idx] if token_idx < len(token_ids) else ""
        elif platform == "kalshi":
            leg["platform"] = "kalshi"
            leg["action"] = "buy"
            leg["_ticker"] = opportunity.get("_kalshi_ticker", "")
        elif platform == "gemini":
            leg["platform"] = "gemini"
            leg["side"] = "buy"
            leg["outcome"] = side
            leg["symbol"] = opportunity.get(
                "_gm_yes_symbol" if side == "yes" else "_gm_no_symbol", "")
        elif platform == "ibkr":
            leg["platform"] = "ibkr"
            leg["side"] = "buy"
            leg["conid"] = opportunity.get(
                "_ibkr_yes_conid" if side == "yes" else "_ibkr_no_conid", "")
        elif platform == "betfair":
            leg["platform"] = "betfair"
            leg["side"] = "BACK" if direction == "BUY_YES" else "LAY"
            leg["_market_id"] = opportunity.get("_market_id", "")
            leg["_selection_id"] = opportunity.get("_selection_id")
        elif platform == "smarkets":
            leg["platform"] = "smarkets"
            leg["side"] = "BACK" if direction == "BUY_YES" else "LAY"
            leg["_market_id"] = opportunity.get("_sm_market_id", "")
            leg["_contract_id"] = opportunity.get("_sm_contract_id", "")
        elif platform == "sxbet":
            leg["platform"] = "sxbet"
            leg["side"] = "BACK" if direction == "BUY_YES" else "LAY"
            leg["_market_hash"] = opportunity.get("_sx_market_hash", "")
            leg["_outcome_id"] = opportunity.get("_sx_outcome_id", "")
        elif platform == "matchbook":
            leg["platform"] = "matchbook"
            leg["side"] = "back" if direction == "BUY_YES" else "lay"
            leg["_market_id"] = opportunity.get("_mb_market_id", "")
            leg["_runner_id"] = opportunity.get("_mb_runner_id", "")
        else:
            return []

        return [leg]

    def _build_mm_legs(self, opportunity: dict, size: float) -> list[dict]:
        """Build execution legs for a market making opportunity.

        Market making places both a bid and an ask as resting limit orders.
        """
        platform = opportunity.get("_platform", "")
        bid_price = opportunity.get("_bid_price", 0)
        ask_price = opportunity.get("_ask_price", 0)
        market_key = opportunity.get("_market_key", "")

        if not platform or not bid_price or not ask_price:
            return []

        if platform not in ENABLED_EXECUTION_PLATFORMS:
            logger.info("MM leg on '%s' blocked — not in ENABLED_EXECUTION_PLATFORMS", platform)
            return []

        legs = [
            {"platform": platform, "side": "BUY", "price": bid_price,
             "token": "yes", "_market_key": market_key, "_mm_side": "bid"},
            {"platform": platform, "side": "SELL", "price": ask_price,
             "token": "yes", "_market_key": market_key, "_mm_side": "ask"},
        ]
        return legs

    def _parse_price(self, opportunity: dict, prefix: str) -> float | None:
        """Extract a price from the opportunity's prices string. Returns None on failure."""
        prices_str = opportunity.get("prices", "")
        for part in prices_str.split():
            if part.startswith(prefix):
                try:
                    value = float(part[len(prefix):])
                    if value <= 0 or value >= 1:
                        logger.warning(f"Price out of range for {prefix}: {value}")
                        return None
                    return value
                except ValueError:
                    logger.warning(f"Failed to parse price for {prefix}: {part}")
                    return None
        logger.warning(f"Price prefix '{prefix}' not found in: {prices_str}")
        return None

    def _print_plan(self, opportunity: dict, legs: list[dict], size: float, prefix: str):
        """Print the execution plan."""
        logger.info(f"{prefix}Trade size: ${size:.2f}")
        logger.info(f"{prefix}Net profit: ${opportunity.get('net_profit', 0):.4f}")
        logger.info(f"{prefix}ROI: {opportunity.get('net_roi', 'N/A')}")
        for i, leg in enumerate(legs):
            platform = leg["platform"]
            side = leg.get("side", "")
            price = leg.get("price", 0)
            token = leg.get("token", "")
            logger.info(f"{prefix}Leg {i+1}: {platform} {side} {token} @ ${price:.3f}")

    def _dry_run_log(self, opportunity: dict, legs: list[dict], size: float) -> bool:
        """Log a dry-run execution."""
        total_cost_str = opportunity.get("total_cost", "$0")
        total_cost = float(total_cost_str.replace("$", "")) if isinstance(total_cost_str, str) else float(total_cost_str)
        roi_str = opportunity.get("net_roi", "0%")
        roi = float(roi_str.replace("%", "")) / 100 if isinstance(roi_str, str) else 0

        opp_id = self.db.log_opportunity(
            opp_type=opportunity.get("type", ""),
            market=opportunity.get("market", ""),
            prices=opportunity.get("prices", ""),
            total_cost=total_cost,
            net_profit=opportunity.get("net_profit", 0),
            net_roi=roi,
            depth=opportunity.get("_clob_depth", 0),
            action="dry_run",
        )

        for leg in legs:
            self.db.log_trade(
                opportunity_id=opp_id,
                platform=leg["platform"],
                side=leg.get("side", leg.get("token", "")),
                price=leg.get("price", 0),
                size=size,
                status="dry_run",
                outcome=leg.get("outcome") or leg.get("token"),
            )

        logger.info(f"[DRY RUN] Logged opportunity #{opp_id} with {len(legs)} legs.")
        self._write_decision(opportunity, "execute", "dry_run")
        return True

    def _record_failed_leg(self, trade_id: int, leg: dict,
                           unknown_state: bool = False) -> None:
        """Record a failed leg, preserving reconciliation state for unconfirmed cancels.

        A leg whose GTC cancel was unconfirmed may still have a live order at
        the venue. Recording it as 'failed' would hide it from recovery.py
        (which only scans 'pending' trades with order IDs), so persist it as
        'pending' with its order_id instead.

        unknown_state: pass True from exception handlers. An exception raised
        after _execute_single_leg assigned leg['_order_id'] leaves the venue-
        side state unknown (the order may be live), so such legs are also
        preserved as 'pending' rather than dropped to 'failed' (fail closed).
        """
        if leg.get("_cancel_unconfirmed") or (unknown_state and leg.get("_order_id")):
            self.db.set_trade_order_id(trade_id, leg.get("_order_id"))
            self.db.update_trade_status(trade_id, "pending")
            reason = ("cancel unconfirmed" if leg.get("_cancel_unconfirmed")
                      else "exception after order placement — venue state unknown")
            logger.error(
                f"Trade #{trade_id} left 'pending' with order_id={leg.get('_order_id')!r} "
                f"— {reason}, awaiting recovery reconciliation"
            )
        else:
            self.db.update_trade_status(trade_id, "failed")

    def _execute_legs(self, opportunity: dict, legs: list[dict], size: float) -> bool:
        """Execute trade legs concurrently on both platforms."""
        total_cost_str = opportunity.get("total_cost", "$0")
        total_cost = float(total_cost_str.replace("$", "")) if isinstance(total_cost_str, str) else float(total_cost_str)
        roi_str = opportunity.get("net_roi", "0%")
        roi = float(roi_str.replace("%", "")) / 100 if isinstance(roi_str, str) else 0

        opp_id = self.db.log_opportunity(
            opp_type=opportunity.get("type", ""),
            market=opportunity.get("market", ""),
            prices=opportunity.get("prices", ""),
            total_cost=total_cost,
            net_profit=opportunity.get("net_profit", 0),
            net_roi=roi,
            depth=opportunity.get("_clob_depth", 0),
            action="traded",
        )

        # Determine if legs span multiple platforms (cross-platform arbs)
        platforms = set(leg["platform"] for leg in legs)
        cross_platform = len(platforms) > 1

        # Log all trades as pending
        for i, leg in enumerate(legs):
            trade_id = self.db.log_trade(
                opportunity_id=opp_id,
                platform=leg["platform"],
                side=leg.get("side", leg.get("token", "")),
                price=leg.get("price", 0),
                size=size,
                status="pending",
                outcome=leg.get("outcome") or leg.get("token"),
            )
            leg["_trade_id"] = trade_id

        # Pre-flight: verify cost per platform fits within cached balances.
        # Groups legs by platform and checks each platform's balance separately,
        # fixing the previous bug where only the first leg's platform was checked.
        opp_type = opportunity.get("type", "")
        cached_balances = self._get_cached_balances(opp_type) or {}
        cost_per_platform: dict[str, float] = {}
        for leg in legs:
            price = leg.get("price", 0)
            if price > 0:
                count = leg.get("_contract_count", int(size / price))
                plat = leg["platform"]
                cost_per_platform[plat] = cost_per_platform.get(plat, 0) + count * price
        for plat, cost in cost_per_platform.items():
            bal = cached_balances.get(plat)
            if (isinstance(bal, (int, float)) and math.isfinite(bal)
                    and bal >= 0 and cost > bal * 0.95):
                logger.warning(
                    "Pre-flight: %s cost $%.2f exceeds 95%% of balance $%.2f. Skipping.",
                    plat, cost, bal,
                )
                for leg in legs:
                    self.db.update_trade_status(leg["_trade_id"], "aborted")
                return False

        results = {}
        if cross_platform:
            # Cross-platform: execute legs concurrently (different exchanges)
            with ThreadPoolExecutor(max_workers=len(legs)) as pool:
                futures = {}
                for i, leg in enumerate(legs):
                    future = pool.submit(self._execute_single_leg, leg, size, opportunity)
                    futures[future] = (i, leg)
                for future in as_completed(futures):
                    idx, leg = futures[future]
                    try:
                        success, order_id, fill_price = future.result()
                        trade_id = leg["_trade_id"]
                        if success:
                            slippage = fill_price - leg.get("price", 0) if fill_price else 0
                            self.db.update_trade_status(trade_id, "filled", fill_price,
                                                        slippage=slippage)
                            leg["_fill_price"] = fill_price
                            results[idx] = True
                            logger.info(f"Leg {idx+1} FILLED: {leg['platform']} order={order_id}")
                        else:
                            self._record_failed_leg(trade_id, leg)
                            results[idx] = False
                            logger.error(f"Leg {idx+1} FAILED: {leg['platform']}")
                    except Exception as e:
                        trade_id = leg["_trade_id"]
                        self._record_failed_leg(trade_id, leg, unknown_state=True)
                        results[idx] = False
                        logger.error(f"Leg {idx+1} ERROR: {e}")
        else:
            # Same-platform: execute legs sequentially, abort on first failure
            for i, leg in enumerate(legs):
                try:
                    success, order_id, fill_price = self._execute_single_leg(leg, size, opportunity)
                    trade_id = leg["_trade_id"]
                    if success:
                        slippage = fill_price - leg.get("price", 0) if fill_price else 0
                        self.db.update_trade_status(trade_id, "filled", fill_price,
                                                    slippage=slippage)
                        leg["_fill_price"] = fill_price
                        results[i] = True
                        logger.info(f"Leg {i+1} FILLED: {leg['platform']} order={order_id}")
                    else:
                        self._record_failed_leg(trade_id, leg)
                        results[i] = False
                        logger.error(f"Leg {i+1} FAILED: {leg['platform']}")
                        # Abort remaining legs — no point continuing
                        for j in range(i + 1, len(legs)):
                            self.db.update_trade_status(legs[j]["_trade_id"], "aborted")
                        logger.warning("Aborting remaining legs after leg %d failure.", i + 1)
                        break
                except Exception as e:
                    trade_id = leg["_trade_id"]
                    self._record_failed_leg(trade_id, leg, unknown_state=True)
                    results[i] = False
                    logger.error(f"Leg {i+1} ERROR: {e}")
                    for j in range(i + 1, len(legs)):
                        self.db.update_trade_status(legs[j]["_trade_id"], "aborted")
                    break

        # Check if all legs succeeded
        all_filled = len(results) == len(legs) and all(results.values())
        if all_filled:
            # Create position in DB for lifecycle tracking.
            # Derive platform from legs themselves so settlement checks dispatch
            # to the right API regardless of opp_type string.
            market = opportunity.get("market", "Unknown")
            opp_type = opportunity.get("type", "")
            platform = _derive_position_platform(legs)
            self.db.create_position(
                opportunity_id=opp_id,
                market_identifier=market,
                platform=platform,
                expected_pnl=opportunity.get("net_profit", 0),
                market_ticker=_derive_market_ticker(opportunity, legs),
            )
            # Invalidate balance cache after a successful trade
            self.invalidate_balance_cache()
            # STRAT-05: Track whale copy positions
            if opp_type == "WhaleCopy":
                self._whale_copy_position_count += 1
            # Notify on successful trade
            self._notify_trade(opportunity, legs, size, success=True)

            # MON-03: Check per-strategy loss streak after logging successful trade
            if _alert_manager:
                try:
                    strategy_type = opportunity.get("type", "unknown")
                    trade_won = opportunity.get("net_profit", 0) > 0
                    _alert_manager.check_strategy_loss_streak(strategy_type, trade_won)
                    logger.debug(
                        "Logged trade for strategy %s: %s",
                        strategy_type,
                        "win" if trade_won else "loss",
                    )
                except Exception as e:
                    logger.warning("Error checking strategy loss streak: %s", str(e))
        else:
            # MON-03: Check per-strategy loss streak for failed/partial trades
            if _alert_manager:
                try:
                    strategy_type = opportunity.get("type", "unknown")
                    trade_won = False  # Partial/failed trades are always losses
                    _alert_manager.check_strategy_loss_streak(strategy_type, trade_won)
                    logger.debug("Logged failed/partial trade for strategy %s: loss", strategy_type)
                except Exception as e:
                    logger.warning("Error checking strategy loss streak: %s", str(e))

            # Partial fill detected — attempt hedging on filled legs
            logger.warning("Partial fill detected. Attempting hedge...")
            if HEDGE_ENABLED:
                from hedger import PartialFillHedger
                hedger = PartialFillHedger(
                    pm_trader=self.pm_trader,
                    kalshi_client=self.kalshi_client,
                    betfair_client=self.betfair_client,
                    smarkets_client=self.smarkets_client,
                    sxbet_client=self.sxbet_client,
                    matchbook_client=self.matchbook_client,
                    gemini_client=self.gemini_client,
                    db=self.db,
                )
                for i, leg in enumerate(legs):
                    if results.get(i):
                        # Use actual fill price from the executor; fall back to limit
                        # only if poller couldn't return one (should not happen post-fix).
                        fill_price = leg.get("_fill_price") or leg.get("price", 0)
                        hedger.queue_hedge(
                            trade_id=leg.get("_trade_id"),
                            platform=leg["platform"],
                            token_id=leg.get("_token_id", leg.get("_ticker", "")),
                            side=leg.get("side", ""),
                            fill_price=fill_price,
                            size=size,
                            opportunity_id=opp_id,
                            market_id=leg.get("_market_id"),
                            selection_id=leg.get("_selection_id"),
                            contract_id=leg.get("_contract_id"),
                            market_hash=leg.get("_market_hash"),
                            runner_id=leg.get("_runner_id"),
                            outcome_id=leg.get("_outcome_id"),
                            symbol=leg.get("symbol"),
                            max_contracts=leg.get("_max_contracts"),
                        )
                        # Attempt immediate hedge
                        hedger.process_pending_hedges()
            else:
                    # Legacy fallback: cancel + orphan
                for i, leg in enumerate(legs):
                    if results.get(i) and leg.get("_order_id"):
                        cancel_ok = self._cancel_leg(leg)
                        if not cancel_ok:
                            trade_id = leg.get("_trade_id")
                            if trade_id:
                                self.db.update_trade_status(trade_id, "orphaned")
                            logger.warning(f"Leg {i+1} cancel failed -- marked as orphaned.")
            # Notify on failed trade
            self._notify_trade(opportunity, legs, size, success=False)

        return all_filled

    # Platforms that cannot sell/cancel — concurrent execution is not safe
    _NO_CANCEL_PLATFORMS = frozenset({"ibkr"})

    def _supports_concurrent(self, legs: list[dict]) -> bool:
        """Check whether all legs are on platforms that support cancellation.

        Concurrent execution requires the ability to hedge (sell) a filled
        leg if the other leg fails.  IBKR is BUY-only and cannot be hedged,
        so any opportunity involving IBKR must fall back to sequential mode.
        """
        for leg in legs:
            platform = leg.get("platform")
            if not isinstance(platform, str) or not platform:
                return False
            if platform in self._NO_CANCEL_PLATFORMS:
                return False
        return len(legs) >= 2

    def _execute_legs_concurrent(
        self, opportunity: dict, legs: list[dict], size: float,
    ) -> bool:
        """Submit all legs simultaneously and hedge on partial failure.

        All legs are submitted via ThreadPoolExecutor at once.  After all
        futures complete:
        - All succeed → create position (normal flow).
        - Some fail → hedge filled legs using PartialFillHedger.
        """
        total_cost_str = opportunity.get("total_cost", "$0")
        total_cost = float(total_cost_str.replace("$", "")) if isinstance(total_cost_str, str) else float(total_cost_str)
        roi_str = opportunity.get("net_roi", "0%")
        roi = float(roi_str.replace("%", "")) / 100 if isinstance(roi_str, str) else 0

        opp_id = self.db.log_opportunity(
            opp_type=opportunity.get("type", ""),
            market=opportunity.get("market", ""),
            prices=opportunity.get("prices", ""),
            total_cost=total_cost,
            net_profit=opportunity.get("net_profit", 0),
            net_roi=roi,
            depth=opportunity.get("_clob_depth", 0),
            action="traded_concurrent",
        )

        # Log all trades as pending
        for leg in legs:
            trade_id = self.db.log_trade(
                opportunity_id=opp_id,
                platform=leg["platform"],
                side=leg.get("side", leg.get("token", "")),
                price=leg.get("price", 0),
                size=size,
                status="pending",
                outcome=leg.get("outcome") or leg.get("token"),
            )
            leg["_trade_id"] = trade_id

        results: dict[int, bool] = {}

        # Submit all legs concurrently
        with ThreadPoolExecutor(max_workers=len(legs)) as pool:
            futures = {}
            for i, leg in enumerate(legs):
                future = pool.submit(self._execute_single_leg, leg, size, opportunity)
                futures[future] = (i, leg)
            for future in as_completed(futures):
                idx, leg = futures[future]
                try:
                    success, order_id, fill_price = future.result()
                    trade_id = leg["_trade_id"]
                    if success:
                        slippage = fill_price - leg.get("price", 0) if fill_price else 0
                        self.db.update_trade_status(
                            trade_id, "filled", fill_price, slippage=slippage)
                        results[idx] = True
                        logger.info(
                            "Concurrent leg %d FILLED: %s order=%s",
                            idx + 1, leg["platform"], order_id,
                        )
                    else:
                        self._record_failed_leg(trade_id, leg)
                        results[idx] = False
                        logger.error(
                            "Concurrent leg %d FAILED: %s", idx + 1, leg["platform"])
                except Exception as e:
                    trade_id = leg["_trade_id"]
                    self._record_failed_leg(trade_id, leg, unknown_state=True)
                    results[idx] = False
                    logger.error("Concurrent leg %d ERROR: %s", idx + 1, e)

        all_filled = len(results) == len(legs) and all(results.values())
        if all_filled:
            market = opportunity.get("market", "Unknown")
            platform = _derive_position_platform(legs)
            self.db.create_position(
                opportunity_id=opp_id,
                market_identifier=market,
                platform=platform,
                expected_pnl=opportunity.get("net_profit", 0),
                market_ticker=_derive_market_ticker(opportunity, legs),
            )
            self._notify_trade(opportunity, legs, size, success=True)
        else:
            # Hedge any filled legs
            logger.warning("Concurrent execution: partial fill detected. Attempting hedge...")
            if HEDGE_ENABLED:
                from hedger import PartialFillHedger
                hedger = PartialFillHedger(
                    pm_trader=self.pm_trader,
                    kalshi_client=self.kalshi_client,
                    betfair_client=self.betfair_client,
                    smarkets_client=self.smarkets_client,
                    sxbet_client=self.sxbet_client,
                    matchbook_client=self.matchbook_client,
                    gemini_client=self.gemini_client,
                    db=self.db,
                )
                for i, leg in enumerate(legs):
                    if results.get(i):
                        fill_price = leg.get("_fill_price") or leg.get("price", 0)
                        hedger.queue_hedge(
                            trade_id=leg.get("_trade_id"),
                            platform=leg["platform"],
                            token_id=leg.get("_token_id", leg.get("_ticker", "")),
                            side=leg.get("side", ""),
                            fill_price=fill_price,
                            size=size,
                            opportunity_id=opp_id,
                            market_id=leg.get("_market_id"),
                            selection_id=leg.get("_selection_id"),
                            contract_id=leg.get("_contract_id"),
                            market_hash=leg.get("_market_hash"),
                            runner_id=leg.get("_runner_id"),
                            outcome_id=leg.get("_outcome_id"),
                            symbol=leg.get("symbol"),
                            max_contracts=leg.get("_max_contracts"),
                        )
                hedger.process_pending_hedges()
            self._notify_trade(opportunity, legs, size, success=False)

        return all_filled

    def _execute_single_leg(
        self, leg: dict, size: float, opportunity: dict
    ) -> tuple[bool, str | None, float | None]:
        """Execute a single trade leg. Returns (success, order_id, fill_price)."""
        platform = leg["platform"]
        price = leg.get("price", 0)

        # --- Platform whitelist guard ---
        if platform not in ENABLED_EXECUTION_PLATFORMS:
            logger.warning(
                f"Platform '{platform}' not in ENABLED_EXECUTION_PLATFORMS "
                f"({', '.join(sorted(ENABLED_EXECUTION_PLATFORMS))}). "
                f"Skipping leg."
            )
            return False, None, None

        # --- Minimum order size guard ---
        min_size = PLATFORM_MIN_ORDER_SIZE.get(platform, 0)
        if size < min_size:
            logger.warning(
                f"Order size ${size:.2f} below {platform} minimum "
                f"${min_size:.2f}. Skipping leg."
            )
            return False, None, None

        if platform == "polymarket":
            if not self.pm_trader:
                return False, None, None
            token_id = leg.get("_token_id", "")
            if not token_id:
                logger.error("Missing token_id for Polymarket leg")
                return False, None, None
            side = leg.get("side", "BUY")
            neg_risk = "NegRisk" in opportunity.get("type", "")

            # Maker routing (per D-05): use GTC limit orders when ORDER_TIME_IN_FORCE != "FOK"
            use_gtc = ORDER_TIME_IN_FORCE not in ("FOK", "fill_or_kill")
            resp = self.pm_trader.place_order(
                token_id=token_id,
                side=side,
                price=price,
                size=size,
                neg_risk=neg_risk,
                order_type="GTC" if use_gtc else "FOK",
            )
            if resp and resp.get("success"):
                order_id = resp.get("orderID", resp.get("order_id", ""))
                leg["_order_id"] = order_id
                if use_gtc:
                    # Poll for fill; cancel and skip on timeout (no taker fallback per D-05)
                    fill_price = self._confirm_fill_pm(order_id, price)
                    if fill_price is None:
                        logger.info(
                            "Polymarket GTC order %s timed out after %.1fs, "
                            "cancelling — no taker fallback per D-05",
                            order_id, GTC_ORDER_TIMEOUT,
                        )
                        cancel_confirmed = False
                        try:
                            cancel_confirmed = bool(self.pm_trader.cancel_order(order_id))
                        except Exception as cancel_err:
                            logger.warning(
                                f"Failed to cancel Polymarket GTC order {order_id}: {cancel_err}"
                            )
                        if not cancel_confirmed:
                            # Cancel unconfirmed: the order may still be live and fill
                            # later (untracked exposure). Mark the leg so recovery/
                            # reconciliation can find it via the preserved _order_id.
                            leg["_cancel_unconfirmed"] = True
                            logger.error(
                                f"Polymarket GTC order {order_id} cancel UNCONFIRMED for market "
                                f"{opportunity.get('market', opportunity.get('type', '?'))} — "
                                f"order may still be live; leg flagged for reconciliation"
                            )
                            if _alert_manager:
                                try:
                                    _alert_manager.alert(
                                        "cancel_unconfirmed",
                                        "CRITICAL",
                                        f"Polymarket GTC cancel unconfirmed for order {order_id}",
                                        details={
                                            "order_id": order_id,
                                            "market": opportunity.get("market"),
                                            "platform": "polymarket",
                                        },
                                    )
                                except Exception:
                                    logger.exception("cancel_unconfirmed alert failed")
                        return False, order_id, None
                    return True, order_id, fill_price
                else:
                    # FOK: poll for fill confirmation
                    fill_price = self._confirm_fill_pm(order_id, price)
                    if fill_price is None:
                        logger.warning("Polymarket FOK order %s not filled (cancel/expire/timeout)", order_id)
                        return False, order_id, None
                    return True, order_id, fill_price
            return False, None, None

        elif platform == "kalshi":
            if not self.kalshi_client:
                return False, None, None
            if not self.dry_run:
                logger.error(
                    "Generic live Kalshi execution is disabled; use the guarded MM pilot launcher"
                )
                return False, None, None
            ticker = leg.get("_ticker", "")
            side = leg.get("side", "yes")
            action = leg.get("action", "buy")
            # Convert dollar size to contracts (1 contract = $1 payout)
            count = leg.get("_contract_count")
            if not isinstance(count, int) or count < 1:
                count = max(1, int(size / price)) if price > 0 else 1
            leg["_max_contracts"] = count

            # Determine time-in-force based on config and leg position
            leg_index = leg.get("_leg_index", 0)
            if ORDER_TIME_IN_FORCE == "gtc":
                tif = "gtc"
            elif ORDER_TIME_IN_FORCE == "gtc_first_leg" and leg_index == 0:
                tif = "gtc"
            else:
                tif = "fill_or_kill"

            from kalshi_policy import live_kalshi_submit_allowed
            if not live_kalshi_submit_allowed(ticker):
                logger.warning("Kalshi order blocked by live policy: %s", ticker)
                return False, None, None

            client_order_id = str(uuid.uuid4())
            client_order_ref = f"client:{client_order_id}"
            leg["_order_id"] = client_order_ref
            trade_id = leg.get("_trade_id")
            if trade_id:
                self.db.set_trade_order_id(trade_id, client_order_ref)
            resp = self.kalshi_client.place_order(
                ticker=ticker,
                side=side,
                action=action,
                count=count,
                price_dollars=price,
                time_in_force=tif,
                client_order_id=client_order_id,
            )
            if resp:
                order = resp.get("order", resp)
                order_id = order.get("order_id") or order.get("id") or ""
                if not order_id:
                    from kalshi_api import KalshiOrderIndeterminate
                    raise KalshiOrderIndeterminate(
                        f"Kalshi accepted {ticker} but returned no order identity",
                        client_order_id=client_order_id,
                    )
                leg["_order_id"] = order_id
                if trade_id:
                    self.db.set_trade_order_id(trade_id, order_id)
                status = order.get("status", "")
                if status == "executed":
                    # FOK filled instantly — extract avg_price directly
                    avg_price = order.get("avg_price")
                    if avg_price is not None:
                        fill_price = float(avg_price) / 100.0
                    else:
                        fill_price = price
                    return True, order_id, fill_price
                elif status == "resting":
                    # GTC order resting — wait up to GTC_ORDER_TIMEOUT then cancel
                    timeout = GTC_ORDER_TIMEOUT if tif == "gtc" else FILL_POLL_TIMEOUT
                    fill_price = self._confirm_fill_kalshi(order_id, price)
                    if fill_price is None and tif == "gtc":
                        # Cancel unfilled GTC order
                        logger.warning("Kalshi GTC order timed out (%.0fs), cancelling: %s",
                                       timeout, order_id)
                        try:
                            self.kalshi_client.cancel_order(order_id)
                        except Exception as e:
                            logger.warning("Failed to cancel Kalshi GTC order %s: %s",
                                           order_id, e)
                        return False, order_id, None
                    if fill_price is None:
                        logger.warning("Kalshi FOK order %s rested then failed (cancel/expire/timeout)", order_id)
                        return False, order_id, None
                    return True, order_id, fill_price
                logger.warning("Kalshi order not filled: status=%s ticker=%s resp=%s",
                               status, ticker, str(resp)[:300])
            else:
                leg.pop("_order_id", None)
                if trade_id:
                    self.db.set_trade_order_id(trade_id, None)
                logger.warning("Kalshi place_order returned None for %s %s @ $%.3f (count=%d)",
                               side, ticker, price, count)
            return False, None, None

        elif platform == "betfair":
            if not self.betfair_client or not self.betfair_client.authenticated:
                return False, None, None
            market_id = leg.get("_market_id", "")
            selection_id = leg.get("_selection_id")
            if not market_id or not selection_id:
                return False, None, None
            side = "BACK" if leg.get("side", "").lower() in ("yes", "back") else "LAY"
            # Convert probability price to decimal odds
            decimal_odds = round(1.0 / price, 2) if price > 0 else 2.0
            instructions = [{
                "selectionId": selection_id,
                "side": side,
                "orderType": "LIMIT",
                "limitOrder": {
                    "size": round(size, 2),
                    "price": decimal_odds,
                    "persistenceType": "LAPSE",
                },
            }]
            resp = self.betfair_client.place_orders(market_id, instructions)
            if resp and resp.get("status") == "SUCCESS":
                results = resp.get("instructionReports", [])
                if results:
                    bet_id = results[0].get("betId", "")
                    leg["_order_id"] = bet_id
                    fill_price = self._confirm_fill_betfair(bet_id, price)
                    if fill_price is None:
                        logger.warning("Betfair bet %s not filled (cancel/expire/timeout)", bet_id)
                        return False, bet_id, None
                    return True, bet_id, fill_price
            return False, None, None

        elif platform == "smarkets":
            if not self.smarkets_client or not self.smarkets_client.authenticated:
                return False, None, None
            market_id = leg.get("_market_id", "")
            contract_id = leg.get("_contract_id", "")
            if not market_id:
                return False, None, None
            side = leg.get("side", "BACK")
            quantity = max(1, int(size / price)) if price > 0 else 1
            resp = self.smarkets_client.place_order(
                market_id=market_id, contract_id=contract_id,
                side=side, price=price, quantity=quantity,
            )
            if resp and not resp.get("error"):
                order_id = str(resp.get("id", resp.get("order_id", "")))
                leg["_order_id"] = order_id
                fill_price = self._confirm_fill_smarkets(order_id, price)
                if fill_price is None:
                    logger.warning("Smarkets order %s not filled (cancel/expire/timeout)", order_id)
                    return False, order_id, None
                return True, order_id, fill_price
            if resp and resp.get("error"):
                logger.warning("Smarkets place_order returned error: %s", resp.get("error"))
            return False, None, None

        elif platform == "sxbet":
            if not self.sxbet_client or not self.sxbet_client.authenticated:
                return False, None, None
            market_hash = leg.get("_market_hash", "")
            outcome_id = leg.get("_outcome_id", "")
            if not market_hash:
                return False, None, None
            side = leg.get("side", "BACK")
            quantity = max(1, int(size / price)) if price > 0 else 1
            resp = self.sxbet_client.place_order(
                market_hash=market_hash, outcome_id=outcome_id,
                side=side, price=price, quantity=quantity,
            )
            if resp and not resp.get("error"):
                order_id = str(resp.get("orderHash", resp.get("id", "")))
                leg["_order_id"] = order_id
                fill_price = self._confirm_fill_sxbet(order_id, price)
                if fill_price is None:
                    logger.warning("SX Bet order %s not filled (cancel/expire/timeout)", order_id)
                    return False, order_id, None
                return True, order_id, fill_price
            if resp and resp.get("error"):
                logger.warning("SX Bet place_order returned error: %s", resp.get("error"))
            return False, None, None

        elif platform == "matchbook":
            if not self.matchbook_client or not self.matchbook_client.authenticated:
                return False, None, None
            market_id = leg.get("_market_id", "")
            runner_id = leg.get("_runner_id", "")
            if not market_id or not runner_id:
                return False, None, None
            side = leg.get("side", "BACK").lower()
            # Convert probability price to decimal odds
            decimal_odds = round(1.0 / price, 2) if price > 0 else 2.0
            resp = self.matchbook_client.place_order(
                market_id=market_id, runner_id=runner_id,
                side=side, odds=decimal_odds, stake=round(size, 2),
            )
            if resp and not resp.get("error"):
                order_id = str(resp.get("id", resp.get("offer-id", "")))
                leg["_order_id"] = order_id
                fill_price = self._confirm_fill_matchbook(order_id, price)
                if fill_price is None:
                    logger.warning("Matchbook order %s not filled (cancel/expire/timeout)", order_id)
                    return False, order_id, None
                return True, order_id, fill_price
            if resp and resp.get("error"):
                logger.warning("Matchbook place_order returned error: %s", resp.get("error"))
            return False, None, None

        elif platform == "gemini":
            if not self.gemini_client or not self.gemini_client.authenticated:
                return False, None, None
            symbol = leg.get("symbol", "")
            if not symbol:
                return False, None, None
            outcome = leg.get("outcome", "yes")
            quantity = max(1, int(size / price)) if price > 0 else 1
            from config import GEMINI_ORDER_TYPE
            tif = "good-til-cancelled" if GEMINI_ORDER_TYPE == "gtc" else "immediate-or-cancel"
            resp = self.gemini_client.place_order(
                symbol=symbol, side="buy", outcome=outcome,
                quantity=quantity, price=price, time_in_force=tif,
            )
            if resp and not resp.get("error"):
                order_id = str(resp.get("orderId", resp.get("order_id", "")))
                leg["_order_id"] = order_id
                fill_price = self._confirm_fill_gemini(order_id, price)
                if fill_price is None:
                    logger.warning("Gemini order %s not filled (cancel/expire/timeout)", order_id)
                    return False, order_id, None
                return True, order_id, fill_price
            if resp and resp.get("error"):
                logger.warning("Gemini place_order returned error: %s", resp.get("error"))
            return False, None, None

        elif platform == "ibkr":
            if not self.ibkr_client or not self.ibkr_client.authenticated:
                return False, None, None
            conid = leg.get("conid", "")
            if not conid:
                return False, None, None
            quantity = max(1, int(size / price)) if price > 0 else 1
            resp = self.ibkr_client.place_order(
                conid=conid, quantity=quantity, price=price,
            )
            if resp and not resp.get("error"):
                order_id = str(resp.get("orderId", resp.get("order_id", "")))
                leg["_order_id"] = order_id
                fill_price = self._confirm_fill_ibkr(order_id, price)
                if fill_price is None:
                    logger.warning("IBKR order %s not filled (cancel/expire/timeout)", order_id)
                    return False, order_id, None
                return True, order_id, fill_price
            if resp and resp.get("error"):
                logger.warning("IBKR place_order returned error: %s", resp.get("error"))
            return False, None, None

        return False, None, None

    def _confirm_fill_gemini(self, order_id: str, expected_price: float) -> float | None:
        """Poll Gemini for fill confirmation. Returns actual fill price, or None if not filled (cancel/expire/timeout)."""
        if not self.gemini_client or not order_id:
            return None
        max_polls = int(FILL_POLL_TIMEOUT / FILL_POLL_INTERVAL)
        for _ in range(max_polls):
            status = self.gemini_client.get_order_status(order_id)
            if status:
                order_status = (status.get("status") or status.get("orderStatus") or "").lower()
                if order_status in ("filled", "closed"):
                    avg_price = status.get("avgExecutionPrice") or status.get("price")
                    if avg_price is not None:
                        return float(avg_price)
                    return expected_price
                elif order_status in ("cancelled", "canceled", "expired"):
                    return None
            time.sleep(FILL_POLL_INTERVAL)
        logger.warning("Fill poll timeout for Gemini order %s after %.1fs — status uncertain",
                        order_id, FILL_POLL_TIMEOUT)
        return None

    def _confirm_fill_ibkr(self, order_id: str, expected_price: float) -> float | None:
        """Poll IBKR for fill confirmation. Returns actual fill price, or None if not filled (cancel/expire/timeout)."""
        if not self.ibkr_client or not order_id:
            return None
        max_polls = int(FILL_POLL_TIMEOUT / FILL_POLL_INTERVAL)
        for _ in range(max_polls):
            status = self.ibkr_client.get_order_status(order_id)
            if status:
                order_status = (status.get("status") or "").lower()
                if order_status == "filled":
                    avg_price = status.get("avgFillPrice")
                    if avg_price is not None and float(avg_price) > 0:
                        return float(avg_price)
                    return expected_price
                elif order_status in ("cancelled", "expired", "inactive"):
                    return None
            time.sleep(FILL_POLL_INTERVAL)
        logger.warning("Fill poll timeout for IBKR order %s after %.1fs — status uncertain",
                        order_id, FILL_POLL_TIMEOUT)
        return None

    def _confirm_fill_pm(self, order_id: str, expected_price: float) -> float | None:
        """Poll Polymarket for fill confirmation. Returns actual fill price, or None if not filled (cancel/expire/timeout)."""
        if not self.pm_trader or not order_id:
            return None
        max_polls = int(FILL_POLL_TIMEOUT / FILL_POLL_INTERVAL)
        for _ in range(max_polls):
            status = self.pm_trader.get_order_status(order_id)
            if status:
                order_status = status.get("status", "")
                if order_status == "matched":
                    return float(status.get("price", expected_price))
                elif order_status in ("canceled", "expired"):
                    return None
            time.sleep(FILL_POLL_INTERVAL)
        logger.warning("Fill poll timeout for Polymarket order %s after %.1fs — status uncertain",
                        order_id, FILL_POLL_TIMEOUT)
        return None

    def _confirm_fill_kalshi(self, order_id: str, expected_price: float) -> float | None:
        """Poll Kalshi for fill confirmation. Returns actual fill price, or None if not filled (cancel/expire/timeout)."""
        if not self.kalshi_client or not order_id:
            return None
        max_polls = int(FILL_POLL_TIMEOUT / FILL_POLL_INTERVAL)
        for _ in range(max_polls):
            status = self.kalshi_client.get_order_status(order_id)
            if status:
                order_status = status.get("status", "")
                if order_status == "executed":
                    avg_price = status.get("avg_price")
                    if avg_price is not None:
                        return float(avg_price) / 100.0
                    return expected_price
                elif order_status in ("canceled", "expired"):
                    return None
            time.sleep(FILL_POLL_INTERVAL)
        logger.warning("Fill poll timeout for Kalshi order %s after %.1fs — status uncertain",
                        order_id, FILL_POLL_TIMEOUT)
        return None

    def _confirm_fill_betfair(self, bet_id: str, expected_price: float) -> float | None:
        """Poll Betfair for fill confirmation. Returns actual fill price, or None if not filled (cancel/expire/timeout)."""
        if not self.betfair_client or not bet_id:
            return None
        max_polls = int(FILL_POLL_TIMEOUT / FILL_POLL_INTERVAL)
        for _ in range(max_polls):
            status = self.betfair_client.get_order_status(bet_id)
            if status:
                order_status = status.get("status", "")
                if order_status == "EXECUTION_COMPLETE":
                    avg_price = status.get("averagePriceMatched") or status.get("priceMatched")
                    if avg_price is not None and float(avg_price) > 1.0:
                        return 1.0 / float(avg_price)  # decimal odds -> probability
                    return expected_price
                elif order_status in ("CANCELLED", "EXPIRED", "LAPSED"):
                    return None
            time.sleep(FILL_POLL_INTERVAL)
        logger.warning("Fill poll timeout for Betfair bet %s after %.1fs — status uncertain",
                        bet_id, FILL_POLL_TIMEOUT)
        return None

    def _confirm_fill_smarkets(self, order_id: str, expected_price: float) -> float | None:
        """Poll Smarkets for fill confirmation. Returns actual fill price, or None if not filled (cancel/expire/timeout)."""
        if not self.smarkets_client or not order_id:
            return None
        max_polls = int(FILL_POLL_TIMEOUT / FILL_POLL_INTERVAL)
        for _ in range(max_polls):
            status = self.smarkets_client.get_order_status(order_id)
            if status:
                order_status = status.get("state", status.get("status", ""))
                if order_status in ("matched", "filled", "executed"):
                    avg_price = status.get("avg_price") or status.get("price")
                    if avg_price is not None:
                        return float(avg_price) / 10000.0  # basis points -> probability
                    return expected_price
                elif order_status in ("cancelled", "expired"):
                    return None
            time.sleep(FILL_POLL_INTERVAL)
        logger.warning("Fill poll timeout for Smarkets order %s after %.1fs — status uncertain",
                        order_id, FILL_POLL_TIMEOUT)
        return None

    def _confirm_fill_sxbet(self, order_id: str, expected_price: float) -> float | None:
        """Poll SX Bet for fill confirmation. Returns actual fill price, or None if not filled (cancel/expire/timeout)."""
        if not self.sxbet_client or not order_id:
            return None
        max_polls = int(FILL_POLL_TIMEOUT / FILL_POLL_INTERVAL)
        for _ in range(max_polls):
            status = self.sxbet_client.get_order_status(order_id)
            if status:
                order_status = status.get("status", "")
                if order_status in ("FILLED", "MATCHED"):
                    avg_price = status.get("avgPrice") or status.get("price")
                    if avg_price is not None:
                        return float(avg_price)
                    return expected_price
                elif order_status in ("CANCELLED", "EXPIRED"):
                    return None
            time.sleep(FILL_POLL_INTERVAL)
        logger.warning("Fill poll timeout for SX Bet order %s after %.1fs — status uncertain",
                        order_id, FILL_POLL_TIMEOUT)
        return None

    def _confirm_fill_matchbook(self, order_id: str, expected_price: float) -> float | None:
        """Poll Matchbook for fill confirmation. Returns actual fill price, or None if not filled (cancel/expire/timeout)."""
        if not self.matchbook_client or not order_id:
            return None
        max_polls = int(FILL_POLL_TIMEOUT / FILL_POLL_INTERVAL)
        for _ in range(max_polls):
            status = self.matchbook_client.get_order_status(order_id)
            if status:
                order_status = status.get("status", "")
                if order_status in ("matched", "filled"):
                    avg_odds = status.get("matched-odds") or status.get("odds")
                    if avg_odds is not None and float(avg_odds) > 1.0:
                        return 1.0 / float(avg_odds)  # decimal odds -> probability
                    return expected_price
                elif order_status in ("cancelled", "expired"):
                    return None
            time.sleep(FILL_POLL_INTERVAL)
        logger.warning("Fill poll timeout for Matchbook order %s after %.1fs — status uncertain",
                        order_id, FILL_POLL_TIMEOUT)
        return None

    def _cancel_leg(self, leg: dict) -> bool:
        """Attempt to cancel a filled/resting order for cleanup. Returns True if successful."""
        platform = leg["platform"]
        order_id = leg.get("_order_id", "")
        if not order_id:
            return False
        if platform == "polymarket" and self.pm_trader:
            return self.pm_trader.cancel_order(order_id)
        elif platform == "kalshi" and self.kalshi_client:
            return self.kalshi_client.cancel_order(order_id)
        elif platform == "betfair" and self.betfair_client:
            return self.betfair_client.cancel_orders(leg.get("_market_id", ""), [order_id])
        elif platform == "smarkets" and self.smarkets_client:
            return self.smarkets_client.cancel_order(order_id)
        elif platform == "sxbet" and self.sxbet_client:
            return self.sxbet_client.cancel_order(order_id)
        elif platform == "matchbook" and self.matchbook_client:
            return self.matchbook_client.cancel_order(order_id)
        elif platform == "gemini" and self.gemini_client:
            return self.gemini_client.cancel_order(order_id)
        elif platform == "ibkr" and self.ibkr_client:
            return self.ibkr_client.cancel_order(order_id)
        return False

    def _notify_trade(self, opportunity: dict, legs: list[dict], size: float,
                      success: bool):
        """Send a webhook notification about a trade execution result.

        Args:
            opportunity: The opportunity dict that was executed.
            legs: List of execution leg dicts.
            size: Trade size in dollars.
            success: True if all legs filled, False if partial/failed.
        """
        if not self.notifier or not hasattr(self.notifier, "url") or not self.notifier.url:
            return

        import threading as _threading

        market = opportunity.get("market", "Unknown")
        opp_type = opportunity.get("type", "")
        profit = opportunity.get("net_profit", 0)
        platforms = ", ".join(set(leg["platform"] for leg in legs))

        if success:
            status = "FILLED"
            msg = (f"TRADE FILLED: {opp_type} | {market[:60]} | "
                   f"${size:.2f} on {platforms} | expected profit ${profit:.4f}")
        else:
            status = "FAILED"
            filled = sum(1 for leg in legs if leg.get("_trade_id"))
            msg = (f"TRADE FAILED: {opp_type} | {market[:60]} | "
                   f"${size:.2f} on {platforms} | {filled}/{len(legs)} legs filled")

        url = self.notifier.url
        if getattr(self.notifier, "_is_telegram", False):
            emoji = "\u2705" if success else "\u274c"
            payload = {"text": f"{emoji} {msg}"}
        elif getattr(self.notifier, "_is_callmebot", False):
            payload = {"text": msg}
        elif "hooks.slack.com" in url:
            emoji = ":white_check_mark:" if success else ":x:"
            payload = {"text": f"{emoji} {msg}"}
        elif "discord.com/api/webhooks" in url:
            emoji = "+" if success else "x"
            payload = {"content": msg}
        else:
            payload = {
                "event": "trade_execution",
                "status": status,
                "type": opp_type,
                "market": market,
                "size": size,
                "profit": profit,
                "platforms": platforms,
                "legs": len(legs),
            }

        thread = _threading.Thread(
            target=self.notifier._send_raw, args=(payload,), daemon=True)
        thread.start()

    def _log_skipped(self, opportunity: dict, reason: str):
        """Log a skipped opportunity."""
        total_cost_str = opportunity.get("total_cost", "$0")
        total_cost = float(total_cost_str.replace("$", "")) if isinstance(total_cost_str, str) else float(total_cost_str)
        roi_str = opportunity.get("net_roi", "0%")
        roi = float(roi_str.replace("%", "")) / 100 if isinstance(roi_str, str) else 0

        self.db.log_opportunity(
            opp_type=opportunity.get("type", ""),
            market=opportunity.get("market", ""),
            prices=opportunity.get("prices", ""),
            total_cost=total_cost,
            net_profit=opportunity.get("net_profit", 0),
            net_roi=roi,
            depth=opportunity.get("_clob_depth", 0),
            action=f"skipped:{reason}",
        )
        self._write_decision(opportunity, "skip", reason)

    def _write_decision(self, opp: dict, decision: str, reason: str, risk_check: str | None = None):
        """Append one JSON line to decisions.jsonl (HARDEN-03).

        Args:
            opp: Opportunity dict from scanner.
            decision: One of "skip", "execute", "reject".
            reason: Human-readable reason string (e.g. "dry_run", "stale_prices").
            risk_check: Optional risk check result description.
        """
        entry = {
            "ts": time.time(),
            "strategy": opp.get("type", ""),
            "market": opp.get("market", ""),
            "decision": decision,
            "reason": reason,
            "prices": opp.get("prices", ""),
            "expected_profit": opp.get("net_profit", 0),
            "expected_roi": opp.get("net_roi", ""),
            "risk_check": risk_check,
        }
        line = json.dumps(entry) + "\n"
        with self._decision_log_lock:
            self._decision_fh.write(line)

    def close(self):
        """Release the JSONL decision log file handle."""
        if hasattr(self, "_decision_fh") and self._decision_fh and not self._decision_fh.closed:
            self._decision_fh.close()
