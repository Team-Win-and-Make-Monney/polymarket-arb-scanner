"""Continuous mode: periodic re-scans with WebSocket feeds, settlement, and dashboard updates."""

from sentry_init import init_sentry
init_sentry()

import asyncio
import json
import logging
import os
import signal
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

from polymarket_api import fetch_all_markets, fetch_events
from ws_feeds import FeedManager
from db import TradeDB
from display import display_results
from dashboard import state as dashboard_state
from recovery import reconcile_orphaned_positions
from scripts.analytics import get_strategy_metrics
from credential_health import CredentialHealthChecker
from fees import (
    net_profit_binary_internal,
    net_profit_negrisk_internal,
    net_profit_cross_platform,
    net_profit_kalshi_binary,
    net_profit_kalshi_multi,
    net_profit_cross_betfair,
    net_profit_cross_generic,
)
import config
from config import (
    RESCAN_INTERVAL as CONFIG_RESCAN_INTERVAL,
    WS_SUBSCRIPTION_LIMIT as CONFIG_WS_SUBSCRIPTION_LIMIT,
    WS_TRIGGER_ENABLED as CONFIG_WS_TRIGGER_ENABLED,
    WS_TRIGGER_THRESHOLD as CONFIG_WS_TRIGGER_THRESHOLD,
    HEDGE_ENABLED as CONFIG_HEDGE_ENABLED,
    SNAPSHOT_ENABLED as CONFIG_SNAPSHOT_ENABLED,
    SNAPSHOT_INTERVAL as CONFIG_SNAPSHOT_INTERVAL,
    MAX_CONCURRENT_WS_EXECUTIONS as CONFIG_MAX_CONCURRENT_WS_EXECUTIONS,
    PRICE_CACHE_EVICTION_AGE as CONFIG_PRICE_CACHE_EVICTION_AGE,
    WS_STALE_FEED_SECONDS as CONFIG_WS_STALE_FEED_SECONDS,
    REWARDS_ENABLED as CONFIG_REWARDS_ENABLED,
    REWARDS_POLL_INTERVAL as CONFIG_REWARDS_POLL_INTERVAL,
)

# Conditional metrics import — never breaks if metrics.py is missing
try:
    from config import METRICS_ENABLED as _METRICS_ENABLED
    if _METRICS_ENABLED:
        from metrics import metrics as _metrics
    else:
        _metrics = None
except Exception:
    _metrics = None
from scans import (
    scan_binary_internal,
    scan_negrisk_internal,
    scan_cross_platform,
    scan_cross_all,
    scan_kalshi_binary,
    scan_kalshi_multi,
    scan_spread_polymarket,
    scan_betfair_backall,
    scan_betfair_backlay,
    scan_smarkets_backall,
    scan_smarkets_backlay,
    scan_sxbet_backall,
    scan_sxbet_backlay,
    scan_matchbook_backall,
    scan_matchbook_backlay,
    scan_gemini_binary,
    scan_gemini_multi,
    scan_ibkr_binary,
    scan_triangular,
    scan_nway_arb,
    scan_multi_cross,
    scan_polymarket_rewards,
    scan_kalshi_rewards,
    scan_lead_lag_mm,
    scan_toxic_flow_pause,
    scan_volatility_adjusted_mm,
    scan_jev_crypto,
    _fetch_kalshi_data,
    capital_efficiency_score,
)
# Layer-4 informed-trading scans — imported at module level (not lazily inside
# the loop) so the continuous-mode wiring is patchable in tests. These power the
# _scan_*_layer4 helpers below.
from scans.imbalance import scan_imbalance
from scans.news_snipe import scan_news_snipe
from scans.correlated import scan_correlated
from scans.time_decay import scan_time_decay

logger = logging.getLogger(__name__)


class OpportunityIndex:
    """Maps (platform, ticker/token) to opportunities for fast lookup on WS updates."""

    def __init__(self):
        self._index: dict[tuple[str, str], list[dict]] = {}
        self._lock = threading.Lock()

    def rebuild(self, opportunities: list[dict]):
        """Rebuild the index from a list of opportunities."""
        new_index: dict[tuple[str, str], list[dict]] = {}
        for opp in opportunities:
            keys = self._extract_keys(opp)
            for key in keys:
                new_index.setdefault(key, []).append(opp)
        with self._lock:
            self._index = new_index

    def lookup(self, platform: str, ticker: str) -> list[dict]:
        """Look up opportunities affected by a price update for (platform, ticker)."""
        with self._lock:
            return list(self._index.get((platform, ticker), []))

    def get_subscription_tokens(self, limit: int = 500) -> tuple[list[str], list[str]]:
        """Get top token IDs for WS subscription, prioritized by opportunity profit.

        Returns (poly_token_ids, kalshi_tickers).
        """
        poly_tokens = set()
        kalshi_tickers = set()
        with self._lock:
            # Sort by profit descending
            scored = []
            for key, opps in self._index.items():
                best_profit = max(o.get("net_profit", 0) for o in opps)
                scored.append((best_profit, key))
            scored.sort(reverse=True)

            for _, (platform, token) in scored:
                if platform == "polymarket" and len(poly_tokens) < limit:
                    poly_tokens.add(token)
                elif platform == "kalshi" and len(kalshi_tickers) < limit:
                    kalshi_tickers.add(token)
        return list(poly_tokens), list(kalshi_tickers)

    @staticmethod
    def _extract_keys(opp: dict) -> list[tuple[str, str]]:
        """Extract (platform, ticker) keys from an opportunity."""
        keys = []
        opp_type = opp.get("type", "")

        # Polymarket token IDs
        token_ids = opp.get("_token_ids", [])
        for tid in token_ids:
            if tid:
                keys.append(("polymarket", tid))

        # Kalshi tickers
        kalshi_ticker = opp.get("_kalshi_ticker", "")
        if kalshi_ticker:
            keys.append(("kalshi", kalshi_ticker))

        kalshi_tickers = opp.get("_kalshi_tickers", [])
        for t in kalshi_tickers:
            if t:
                keys.append(("kalshi", t))

        # Betfair
        bf_market_id = opp.get("_bf_market_id") or opp.get("_market_id", "")
        if bf_market_id and "betfair" in opp_type.lower():
            keys.append(("betfair", bf_market_id))

        # Smarkets
        sm_market_id = opp.get("_sm_market_id", "")
        if sm_market_id:
            keys.append(("smarkets", sm_market_id))

        # SX Bet
        sx_hash = opp.get("_sx_market_hash", "")
        if sx_hash:
            keys.append(("sxbet", sx_hash))

        # Matchbook
        mb_market_id = opp.get("_mb_market_id", "")
        if mb_market_id:
            keys.append(("matchbook", mb_market_id))

        # Gemini
        gm_event_id = opp.get("_gm_event_id", "")
        if gm_event_id:
            keys.append(("gemini", gm_event_id))

        # IBKR
        ibkr_event_id = opp.get("_ibkr_event_id", "")
        if ibkr_event_id:
            keys.append(("ibkr", ibkr_event_id))

        # EventDivergence: index by platform + metaculus question ID
        if opp_type == "EventDivergence":
            platform = opp.get("_platform", "")
            metaculus_id = opp.get("_metaculus_id")
            if platform and metaculus_id:
                keys.append((platform, f"metaculus_{metaculus_id}"))

        # TriangularCross: index by both platform keys
        if opp_type == "TriangularCross":
            for pkey in ("_platform_a", "_platform_b"):
                pname = opp.get(pkey, "")
                if pname:
                    keys.append((pname, opp.get("market", "")))

        return keys


_WINNING_SIDE_ALIASES = {
    "yes": {"yes", "y", "buy", "back"},
    "no": {"no", "n", "sell", "lay"},
}


def _leg_won(trade_side: str, winning_side: str) -> bool:
    """Return True if a trade's side matches the resolved winning outcome.

    Handles cross-platform side terminology: yes/no, buy/sell, back/lay,
    and free-form outcome names (case-insensitive equality).
    """
    ts = (trade_side or "").lower()
    ws = (winning_side or "").lower()
    if not ts or not ws:
        return False
    if ts == ws:
        return True
    aliases = _WINNING_SIDE_ALIASES.get(ws)
    return aliases is not None and ts in aliases


def _calc_realized_pnl(db: TradeDB, pos: dict, winning_side: str | None = None) -> float:
    """Calculate realized P&L from actual fill prices in the trades table.

    Args:
        db: TradeDB instance.
        pos: Position record.
        winning_side: Resolved winning outcome (e.g. "yes", "no", or an outcome
            name). When provided, per-leg payout is computed: winning legs pay
            contracts * $1, losing legs pay $0. Required for directional
            strategies (Imbalance, NewsSnipe, WhaleCopy, TimeDecay, etc.) —
            without it, losing directional bets would falsely appear profitable.

    Returns:
        Realized P&L in USD. Falls back to expected_pnl when no trade data is
        available. When winning_side is None, assumes an arbitrage payout of
        $1 (correct for Binary/NegRisk/Cross/etc. where one side guaranteed
        wins, INCORRECT for losing directional bets).
    """
    trades = db.get_trades_for_opportunity(pos["opportunity_id"])
    if not trades:
        return pos.get("expected_pnl", 0)
    total_fill_cost = sum(
        (t.get("fill_price") or t["price"]) * t["size"] for t in trades
    )
    if total_fill_cost <= 0:
        return pos.get("expected_pnl", 0)

    if winning_side is None:
        # Arbitrage assumption: exactly one side pays $1 total payout.
        return 1.0 - total_fill_cost

    # Per-leg payout based on resolved outcome.
    total_payout = 0.0
    for t in trades:
        if not _leg_won(t.get("side", ""), winning_side):
            continue
        fill = t.get("fill_price") or t["price"]
        if fill > 0:
            contracts = t["size"] / fill
            total_payout += contracts  # $1 per winning contract
    return total_payout - total_fill_cost


def check_settlements(
    db: TradeDB,
    kalshi_client,
    poly_markets: list[dict] | None,
    betfair_client=None,
    smarkets_client=None,
    sxbet_client=None,
    matchbook_client=None,
    gemini_client=None,
    ibkr_client=None,
):
    """Check open positions for settlement and update realised P&L.

    Iterates all open positions in the database, queries each platform's
    API for resolution status, and settles any positions whose underlying
    market has resolved. Calculates realised P&L from trade history and
    updates the position record. Also processes pending partial fills that
    need hedging.

    Args:
        db: TradeDB instance for reading open positions and writing settlements.
        kalshi_client: Authenticated KalshiClient, or None to skip Kalshi
            settlement checks.
        poly_markets: Latest Polymarket markets list (used to check resolution
            status), or None.
        betfair_client: Optional BetfairClient for Betfair settlement checks.
        smarkets_client: Optional SmarketsClient for Smarkets settlement checks.
        sxbet_client: Optional SXBetClient for SX Bet settlement checks.
        matchbook_client: Optional MatchbookClient for Matchbook settlement
            checks.
        gemini_client: Optional GeminiClient for Gemini settlement checks.
        ibkr_client: Optional IBKRClient for IBKR ForecastEx settlement checks.
    """
    open_positions = db.get_open_positions()
    if not open_positions:
        return

    logger.info("Checking %d open positions for settlement...", len(open_positions))

    # Account-scoped Kalshi reconciliation (June 2026 audit): the account's
    # /portfolio/settlements feed is the authoritative record of what settled —
    # one API call covers every open Kalshi position and cannot miss a
    # resolution the way per-market polling did. Build a ticker → result map
    # up front; per-market /markets/{ticker} lookup remains as fallback for
    # positions not (yet) present in the settlement feed.
    kalshi_settlements: dict[str, str] = {}
    if kalshi_client and any(p["platform"] == "kalshi" for p in open_positions):
        try:
            for s in kalshi_client.get_settlements():
                ticker = s.get("ticker", "")
                result = s.get("market_result", "")
                if ticker and result and ticker not in kalshi_settlements:
                    kalshi_settlements[ticker] = result
        except Exception as e:
            logger.warning("Kalshi settlements fetch failed (falling back to per-market lookups): %s", e)

    settled = 0
    for pos in open_positions:
        platform = pos["platform"]
        # Settlement lookups are keyed by the platform-native ticker, not the
        # human-readable title. Prefer market_ticker; fall back to
        # market_identifier for legacy rows written before that column existed.
        market_id = pos.get("market_ticker") or pos["market_identifier"]

        try:
            if platform == "kalshi" and kalshi_client:
                result = kalshi_settlements.get(market_id, "")
                if not result:
                    resp = kalshi_client._request("GET", f"/markets/{market_id}")
                    if resp and resp.status_code == 200:
                        data = resp.json()
                        market_data = data.get("market", data)
                        result = market_data.get("result", "")
                if result:
                    realized = _calc_realized_pnl(db, pos, winning_side=result)
                    db.settle_position(pos["id"], realized_pnl=realized, status="settled")
                    settled += 1
            elif platform in ("polymarket", "cross"):
                try:
                    from polymarket_api import _get_with_retry, GAMMA_BASE
                    resp = _get_with_retry(f"{GAMMA_BASE}/markets/{market_id}", timeout=15)
                    if resp and resp.status_code == 200:
                        pm_data = resp.json()
                        if pm_data.get("closed") or pm_data.get("resolvedOutcome"):
                            # "cross" positions are arbs (one side wins) — leave winning_side
                            # unset so the legacy 1.0-cost formula applies. For pure-Polymarket
                            # directional trades, pass the resolved outcome.
                            ws = pm_data.get("resolvedOutcome") if platform == "polymarket" else None
                            realized = _calc_realized_pnl(db, pos, winning_side=ws)
                            db.settle_position(pos["id"], realized_pnl=realized, status="settled")
                            settled += 1
                except Exception as e:
                    logger.warning("PM settlement check failed for position %s: %s", pos['id'], e)
            elif platform == "betfair" and betfair_client:
                try:
                    from betfair_api import BETFAIR_EXCHANGE_URL, _rate_limit as bf_rate_limit
                    bf_rate_limit()
                    resp = betfair_client.session.post(
                        f"{BETFAIR_EXCHANGE_URL}/listMarketBook/",
                        json={"marketIds": [market_id]},
                        timeout=15,
                    )
                    if resp and resp.status_code == 200:
                        books = resp.json()
                        if books and isinstance(books, list) and books:
                            mkt_status = books[0].get("status", "")
                            if mkt_status in ("CLOSED", "SETTLED"):
                                realized = _calc_realized_pnl(db, pos)
                                db.settle_position(pos["id"], realized_pnl=realized, status="settled")
                                settled += 1
                except Exception as e:
                    logger.warning("Betfair settlement check failed for position %s: %s", pos['id'], e)
            elif platform == "smarkets" and smarkets_client:
                try:
                    market_data = smarkets_client.get_market_status(market_id) if hasattr(smarkets_client, "get_market_status") else None
                    if market_data and market_data.get("state") in ("settled", "closed"):
                        realized = _calc_realized_pnl(db, pos)
                        db.settle_position(pos["id"], realized_pnl=realized, status="settled")
                        settled += 1
                except Exception as e:
                    logger.warning("Smarkets settlement check failed for position %s: %s", pos['id'], e)
            elif platform == "sxbet" and sxbet_client:
                try:
                    market_data = sxbet_client.get_market_status(market_id) if hasattr(sxbet_client, "get_market_status") else None
                    if market_data and market_data.get("status") in ("SETTLED", "CLOSED"):
                        realized = _calc_realized_pnl(db, pos)
                        db.settle_position(pos["id"], realized_pnl=realized, status="settled")
                        settled += 1
                except Exception as e:
                    logger.warning("SX Bet settlement check failed for position %s: %s", pos['id'], e)
            elif platform == "matchbook" and matchbook_client:
                try:
                    market_data = matchbook_client.get_market_status(market_id) if hasattr(matchbook_client, "get_market_status") else None
                    if market_data:
                        event_status = market_data.get("status", "")
                        if event_status in ("settled", "closed", "resulted"):
                            realized = _calc_realized_pnl(db, pos)
                            db.settle_position(pos["id"], realized_pnl=realized, status="settled")
                            settled += 1
                except Exception as e:
                    logger.warning("Matchbook settlement check failed for position %s: %s", pos['id'], e)
            elif platform == "gemini" and gemini_client:
                try:
                    market_data = gemini_client.get_market_status(market_id) if hasattr(gemini_client, "get_market_status") else None
                    if market_data and market_data.get("status") in ("settled", "closed", "resolved"):
                        realized = _calc_realized_pnl(db, pos)
                        db.settle_position(pos["id"], realized_pnl=realized, status="settled")
                        settled += 1
                except Exception as e:
                    logger.warning("Gemini settlement check failed for position %s: %s", pos['id'], e)
            elif platform == "ibkr" and ibkr_client:
                try:
                    market_data = ibkr_client.get_market_status(market_id) if hasattr(ibkr_client, "get_market_status") else None
                    if market_data and market_data.get("status") in ("settled", "closed", "expired"):
                        realized = _calc_realized_pnl(db, pos)
                        db.settle_position(pos["id"], realized_pnl=realized, status="settled")
                        settled += 1
                except Exception as e:
                    logger.warning("IBKR settlement check failed for position %s: %s", pos['id'], e)
        except Exception as e:
            logger.warning("Settlement check failed for position %s: %s", pos['id'], e)

    if settled:
        logger.info("Settled %d positions.", settled)


def _recalc_profit(opp: dict, platform: str, ticker: str, new_price: float, price_cache: dict) -> float | None:
    """Recalculate net profit for an opportunity using a fresh WS price.

    Dispatches to the correct fee function based on opportunity type.
    Returns recalculated net profit, or None if unable to compute.
    """
    opp_type = opp.get("type", "")
    try:
        if opp_type == "Binary":
            # Need both YES and NO prices from cache
            token_ids = opp.get("_token_ids", [])
            if len(token_ids) < 2:
                return None
            prices = []
            for tid in token_ids:
                if tid == ticker:
                    prices.append(new_price)
                else:
                    cached = price_cache.get((platform, tid))
                    if cached and cached.get("price") is not None:
                        prices.append(cached["price"])
                    else:
                        return None
            result = net_profit_binary_internal(prices[0], prices[1])
            return result["net_profit"]
        elif opp_type.startswith("NegRisk"):
            token_ids = opp.get("_token_ids", [])
            if not token_ids:
                return None
            prices = []
            for tid in token_ids:
                if tid == ticker:
                    prices.append(new_price)
                else:
                    cached = price_cache.get((platform, tid))
                    if cached and cached.get("price") is not None:
                        prices.append(cached["price"])
                    else:
                        return None
            result = net_profit_negrisk_internal(prices)
            return result["net_profit"]
        elif opp_type == "KalshiBinary":
            k_ticker = opp.get("_kalshi_ticker", "")
            cached = price_cache.get(("kalshi", k_ticker))
            if not cached:
                return None
            k_yes = cached.get("yes_price", opp.get("_kalshi_yes"))
            k_no = cached.get("no_price", opp.get("_kalshi_no"))
            if k_yes is None or k_no is None:
                return None
            result = net_profit_kalshi_binary(k_yes, k_no)
            return result["net_profit"]
        elif opp_type.startswith("Cross"):
            # Cross-platform: use the WS-updated price for one side and cached
            # price for the other. Requires _price_a/_price_b/_platform_a/_platform_b
            # metadata attached by the scan (added in cross.py).
            pa = opp.get("_platform_a", "")
            pb = opp.get("_platform_b", "")
            price_a = opp.get("_price_a")
            price_b = opp.get("_price_b")
            side_a = opp.get("_side_a", "yes")
            side_b = opp.get("_side_b", "no")
            if price_a is None or price_b is None or not pa or not pb:
                return None

            # Determine which side the WS update applies to
            if platform == pa:
                price_a = new_price
            elif platform == pb:
                price_b = new_price
            else:
                return None  # Update is for an unrelated platform

            result = net_profit_cross_generic(
                price_a, price_b, side_a, side_b,
                platform_a=pa, platform_b=pb,
            )
            return result["net_profit"]
        elif opp_type == "JevCrypto":
            from fees import net_profit_jev_crypto
            model_prob = opp.get("_model_prob")
            action = opp.get("_action", "buy_yes")
            token_ids = opp.get("_token_ids", [])
            if model_prob is None or len(token_ids) < 2:
                return None
            if action == "buy_yes" and ticker == token_ids[0]:
                exec_price = new_price
                prob_target = model_prob
            elif action == "buy_no" and ticker == token_ids[1]:
                exec_price = new_price
                prob_target = 1.0 - model_prob
            else:
                return None

            trade_size = 50.0
            res = net_profit_jev_crypto(
                price=exec_price,
                model_prob=prob_target,
                size=trade_size,
            )
            return res["net_profit"]
    except Exception as e:
        logger.debug("Error recalculating profit: %s", e)
        return None
    return None


# Per-market lock management for concurrent WS-triggered execution
_market_locks: dict[str, threading.Lock] = {}
_locks_lock = threading.Lock()


def _get_market_lock(market: str) -> threading.Lock:
    """Get or create a lock for a specific market."""
    with _locks_lock:
        if market not in _market_locks:
            _market_locks[market] = threading.Lock()
        return _market_locks[market]


# Priority weights: time-sensitive strategies execute before regular ones.
# Higher weight = execute sooner.
_PRIORITY_WEIGHTS = {
    "StalePriceOpp": 3.0,       # Most time-sensitive: stale prices disappear quickly
    "ResolutionSnipeOpp": 2.5,  # Resolution imminent: price converges fast
    "JevCrypto": 2.2,           # Model-calibrated crypto strike edge: priority execution
    "Binary": 2.0,              # Pure arb: guaranteed profit, execute quickly
    "KalshiBinary": 2.0,
    "Cross": 2.0,
    "TriangularCross": 2.0,
    "MultiCross": 2.0,
    "NegRisk": 1.8,
    "EventDivergence": 1.5,     # Signal-based: less urgent
    "ConvergenceOpp": 1.3,
    "MarketMake": 1.0,          # Lowest priority: always-on, not time-critical
}


def _execution_priority(opp: dict) -> float:
    """Score an opportunity for execution priority ordering.

    Combines time-sensitivity weight with capital efficiency.
    Time-sensitive opportunities (stale prices, resolution snipes) execute
    first regardless of absolute profit size.
    """
    opp_type = opp.get("type", "")
    efficiency = capital_efficiency_score(opp)

    # Find the matching priority weight via prefix
    weight = 1.0
    for prefix, w in _PRIORITY_WEIGHTS.items():
        if opp_type.startswith(prefix):
            weight = w
            break

    return weight * efficiency


class _StageTimer:
    """Context manager that records the elapsed wall-clock time of a scan stage.

    Usage::

        timings: dict[str, float] = {}
        with _StageTimer("fetch", timings):
            ...do work...
        # timings["fetch"] now holds elapsed seconds
    """

    __slots__ = ("name", "timings", "_start")

    def __init__(self, name: str, timings: dict):
        self.name = name
        self.timings = timings
        self._start = 0.0

    def __enter__(self):
        self._start = time.time()
        return self

    def __exit__(self, exc_type, exc, tb):
        self.timings[self.name] = time.time() - self._start
        # Don't suppress exceptions
        return False


def _format_stage_timings(timings: dict, total: float) -> str:
    """Render stage timings as a single sortable summary line."""
    parts = [f"total={total:.1f}s"]
    # Sort by elapsed desc so the bottleneck is first
    for name, elapsed in sorted(timings.items(), key=lambda kv: -kv[1]):
        pct = (elapsed / total * 100) if total > 0 else 0
        parts.append(f"{name}={elapsed:.1f}s({pct:.0f}%)")
    return " ".join(parts)


def _check_platform_balance(executor, opportunities, notifier, scan_count):
    """Check platform capital allocation and alert on imbalance.

    When one platform holds >60% of total capital but generates <30% of
    opportunities, emits a rebalancing alert via the notifier.
    """
    # Count opportunity flow per platform
    platform_opp_counts: dict[str, int] = {}
    for opp in opportunities:
        plat = opp.get("_platform", "")
        if not plat:
            # Infer from type
            opp_type = opp.get("type", "")
            if "Kalshi" in opp_type:
                plat = "kalshi"
            elif "Betfair" in opp_type:
                plat = "betfair"
            elif "Smarkets" in opp_type:
                plat = "smarkets"
            elif "Gemini" in opp_type:
                plat = "gemini"
            elif "IBKR" in opp_type:
                plat = "ibkr"
            else:
                plat = "polymarket"
        platform_opp_counts[plat] = platform_opp_counts.get(plat, 0) + 1

    total_opps = sum(platform_opp_counts.values())
    if total_opps < 5:
        return  # Not enough data to assess

    # Fetch balances (uses executor's cached balance fetch)
    try:
        balances = executor._fetch_balances("Cross")
    except Exception:
        return
    if not balances:
        return

    total_balance = sum(v for v in balances.values() if isinstance(v, (int, float)))
    if total_balance <= 0:
        return

    for platform, balance in balances.items():
        if not isinstance(balance, (int, float)) or balance <= 0:
            continue
        capital_pct = balance / total_balance
        opp_flow = platform_opp_counts.get(platform, 0)
        opp_pct = opp_flow / total_opps if total_opps > 0 else 0

        # Alert if capital is concentrated but opportunity flow is low
        if capital_pct > 0.60 and opp_pct < 0.30:
            msg = (
                f"REBALANCE ALERT: {platform} holds {capital_pct:.0%} of capital "
                f"(${balance:.0f}) but only {opp_pct:.0%} of opportunity flow "
                f"({opp_flow}/{total_opps}). Consider moving funds."
            )
            logger.warning(msg)
            if hasattr(notifier, "notify_text"):
                notifier.notify_text(msg)


def _feed_sprint3_trackers(platform: str, ticker: str, data: dict) -> None:
    """Feed the Sprint 2 VolatilityTracker + LeadLagMM singletons with a WS tick.

    Both trackers short-circuit internally when their respective config flags
    are false; we also short-circuit at this level to avoid the per-tick
    module-singleton lookup when the operator hasn't opted in. Wrapped in
    try/except because ``on_price_update`` is a hot WS path and an unrelated
    tracker bug must never break the feed.
    """
    try:
        if not (config.MM_VOLATILITY_ADJUSTED_ENABLED or config.LEAD_LAG_MM_ENABLED):
            return
        price_val = data.get("price") or data.get("yes") or data.get("yes_price")
        if price_val is None:
            return
        price_f = float(price_val)
        if config.MM_VOLATILITY_ADJUSTED_ENABLED:
            from market_maker import get_volatility_tracker
            get_volatility_tracker().record_price(ticker, price_f)
        if config.LEAD_LAG_MM_ENABLED:
            from market_maker import get_lead_lag_mm
            get_lead_lag_mm().record_price(ticker, platform, price_f)
    except Exception as exc:
        logger.debug("VolatilityTracker/LeadLagMM WS feed failed: %s", exc)


# ---------------------------------------------------------------------------
# Layer 4 informed-trading scans (flag-gated; disabled by default)
#
# These four scans were silently disabled in continuous mode: the inline loop
# blocks called them with stale kwargs (poly_markets=/kalshi_data=/min_profit=)
# and wrong config names, and a broad ``except`` swallowed every resulting
# TypeError/ImportError — so flipping the feature flag did nothing. Extracted
# into helpers with the correct, current signatures so the wiring is unit-
# testable (each scan is patched and asserted invoked).
# ---------------------------------------------------------------------------

def _build_poly_markets_by_key(poly_markets) -> dict[str, dict]:
    """Index Polymarket markets by ``polymarket-<condition_id>``.

    Shared by the Layer-4 scans, which all take a ``markets_by_key`` dict.
    Mirrors the inline pattern the imbalance block used previously; these
    scans do not fetch Kalshi order books, so only Polymarket is indexed.
    """
    markets_by_key: dict[str, dict] = {}
    for mkt in poly_markets or []:
        cid = mkt.get("condition_id") or mkt.get("conditionId") or ""
        if cid:
            markets_by_key[f"polymarket-{cid}"] = mkt
    return markets_by_key


def _parse_correlated_pairs(raw) -> list[tuple[str, str]]:
    """Parse the ``CORRELATED_PAIRS`` config (a JSON string of ``[a, b]``
    pairs) into the ``list[tuple[str, str]]`` ``scan_correlated`` expects.
    Returns ``[]`` on malformed input rather than raising."""
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (ValueError, TypeError):
            return []
    pairs: list[tuple[str, str]] = []
    for item in raw or []:
        if isinstance(item, (list, tuple)) and len(item) == 2:
            pairs.append((str(item[0]), str(item[1])))
    return pairs


def _scan_imbalance_layer4(poly_markets, price_cache, mode) -> list[dict]:
    """STRAT-01 order-book imbalance. Returns ``[]`` when the flag/mode gate
    is off or there are no Polymarket markets to scan."""
    if mode not in ("all", "imbalance") or not config.IMBALANCE_ENABLED:
        return []
    markets_by_key = _build_poly_markets_by_key(poly_markets)
    if not markets_by_key:
        return []
    return scan_imbalance(
        markets_by_key,
        min_ratio=config.IMBALANCE_RATIO,
        price_cache=price_cache,
    )


def _scan_news_snipe_layer4(poly_markets, mode, cooldown_cache=None) -> list[dict]:
    """STRAT-02 news-driven resolution sniping.

    Runs the scan once per enabled news source and merges the results. Two
    independent gates:
      * NEWS_SNIPE_ENABLED + FINNHUB_API_KEY       -> Finnhub headlines
      * FIRECRAWL_NEWS_ENABLED + FIRECRAWL_API_KEY  -> Firecrawl web search
    Either, both, or neither may be active. Returns ``[]`` when no source is
    enabled/configured or there are no markets. A failure building or running
    one client does not disable the other."""
    if mode not in ("all", "news-snipe"):
        return []

    finnhub_on = config.NEWS_SNIPE_ENABLED and bool(config.FINNHUB_API_KEY)
    firecrawl_on = config.FIRECRAWL_NEWS_ENABLED and bool(config.FIRECRAWL_API_KEY)
    if not (finnhub_on or firecrawl_on):
        return []

    markets_by_key = _build_poly_markets_by_key(poly_markets)
    if not markets_by_key:
        return []

    clients: list[object] = []
    if finnhub_on:
        try:
            from finnhub_api import FinnhubNewsClient
            clients.append(FinnhubNewsClient(api_key=config.FINNHUB_API_KEY))
        except ImportError:
            logger.debug("finnhub_api module not available")
        except Exception as exc:  # noqa: BLE001 - one client failing must not kill the other
            logger.warning("Finnhub news client init failed: %s", exc)
    if firecrawl_on:
        try:
            from firecrawl_news_client import FirecrawlNewsClient
            clients.append(FirecrawlNewsClient(api_key=config.FIRECRAWL_API_KEY))
        except ImportError:
            logger.debug("firecrawl_news_client module not available")
        except Exception as exc:  # noqa: BLE001
            logger.warning("Firecrawl news client init failed: %s", exc)

    opportunities: list[dict] = []
    for client in clients:
        try:
            opportunities.extend(
                scan_news_snipe(
                    markets_by_key,
                    client,
                    cooldown_cache=cooldown_cache,
                    fuzzy_threshold=config.FUZZY_MATCH_THRESHOLD,
                )
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "News-snipe scan failed for %s: %s", type(client).__name__, exc
            )
    return opportunities


def _scan_correlated_layer4(poly_markets, price_cache, mode) -> list[dict]:
    """STRAT-06 correlated market pairs. Returns ``[]`` when the gate is off,
    no valid pairs are configured, or there are no markets."""
    if mode not in ("all", "correlated") or not config.CORRELATED_ENABLED:
        return []
    pairs = _parse_correlated_pairs(config.CORRELATED_PAIRS)
    if not pairs:
        logger.debug("CORRELATED_ENABLED but CORRELATED_PAIRS has no valid pairs")
        return []
    markets_by_key = _build_poly_markets_by_key(poly_markets)
    if not markets_by_key:
        return []
    return scan_correlated(
        markets_by_key,
        pairs,
        min_spread=config.CORRELATION_DIVERGENCE_THRESHOLD,
        price_cache=price_cache,
    )


def _scan_time_decay_layer4(poly_markets, price_cache, mode,
                            signal_aggregator=None) -> list[dict]:
    """STRAT-07 time-decay convergence. Returns ``[]`` when the gate is off or
    there are no markets. ``signal_aggregator`` is injectable for testing; a
    fresh ``SignalAggregator`` is built when not supplied."""
    if mode not in ("all", "time-decay") or not config.TIME_DECAY_ENABLED:
        return []
    markets_by_key = _build_poly_markets_by_key(poly_markets)
    if not markets_by_key:
        return []
    if signal_aggregator is None:
        from signal_aggregator import SignalAggregator
        signal_aggregator = SignalAggregator()
    return scan_time_decay(
        markets_by_key,
        signal_aggregator,
        min_hours_to_expiry=config.TIME_DECAY_MIN_HOURS_EXPIRY,
        min_consensus=config.TIME_DECAY_MIN_CONSENSUS,
        buy_below_price=config.TIME_DECAY_BUY_BELOW_PRICE,
        price_cache=price_cache,
    )


def _scan_jev_crypto_continuous(
    poly_markets,
    mode: str,
    min_profit: float,
    db=None,
    jev_client=None,
    spot_prices=None,
) -> list[dict]:
    """Continuous-mode runner for Jev-powered crypto prediction market scanner.

    Evaluates BTC, ETH, SOL, XRP strike contracts against live spot prices.
    Returns [] when disabled, mode doesn't match ('all' or 'jev-crypto'),
    or if no markets are provided.
    """
    if mode not in ("all", "jev-crypto"):
        return []
    is_explicit = (mode == "jev-crypto")
    enabled = getattr(config, "JEV_CRYPTO_ENABLED", False)
    if not is_explicit and not enabled:
        return []
    markets_by_key = _build_poly_markets_by_key(poly_markets)
    if not markets_by_key:
        return []
    return scan_jev_crypto(
        markets_by_key,
        spot_prices=spot_prices,
        min_profit=min_profit,
        jev_client=jev_client,
        db=db,
        force=is_explicit,
    )


def run_continuous(args, min_profit, kalshi_client, kalshi_api_key_id,
                   kalshi_private_key_path, executor, db, price_cache,
                   extra_clients=None, notifier=None, pm_trader=None,
                   event_monitor=None, kalshi_private_key_base64=None):
    """Run the scanner in continuous mode with WebSocket price feeds.

    Sets up WebSocket connections to all configured platforms, runs periodic
    full re-scans at a configurable interval, and optionally triggers
    immediate execution when a WS price update moves a tracked opportunity
    above the profit threshold. Handles graceful shutdown on SIGINT/SIGTERM,
    periodic settlement checks, stale price eviction, and dashboard state
    updates.

    Args:
        args: Parsed CLI argparse namespace (uses mode, continuous, interval,
            min_confidence, min_depth, limit, json, dry_run, exec_mode,
            max_trade, dashboard_port).
        min_profit: Minimum net profit threshold (0-1 float, e.g. 0.01 = 1%).
        kalshi_client: Authenticated KalshiClient instance, or None.
        kalshi_api_key_id: Kalshi API key ID string for WS auth, or None.
        kalshi_private_key_path: Path to Kalshi RSA private key PEM, or None.
        executor: TradeExecutor instance for opportunity execution.
        db: TradeDB instance for logging and settlement tracking.
        price_cache: Shared dict keyed by (platform, ticker) storing latest
            price snapshots from WebSocket feeds.
        extra_clients: Optional dict of additional platform clients keyed by
            platform name (e.g. {"betfair": BetfairClient, ...}).
        notifier: Optional Notifier instance for webhook/Slack alerts.
        pm_trader: Optional PolymarketTrader for on-chain execution.
        event_monitor: Optional EventMonitor for cross-event divergence
            tracking.
    """
    extra_clients = extra_clients or {}
    rescan_interval = getattr(args, 'interval', None) or CONFIG_RESCAN_INTERVAL

    shutdown_event = asyncio.Event()

    def _signal_handler(sig, frame):
        logger.info("Shutting down gracefully...")
        shutdown_event.set()

    signal.signal(signal.SIGINT, _signal_handler)
    signal.signal(signal.SIGTERM, _signal_handler)

    opp_index = OpportunityIndex()
    ws_trigger_enabled = CONFIG_WS_TRIGGER_ENABLED
    ws_trigger_threshold = CONFIG_WS_TRIGGER_THRESHOLD
    ws_sub_limit = CONFIG_WS_SUBSCRIPTION_LIMIT
    _price_cache_lock = threading.Lock()
    _execution_semaphore = threading.Semaphore(CONFIG_MAX_CONCURRENT_WS_EXECUTIONS)

    # Event-driven Cross detection (Phase 2): persistent pair index that
    # turns every Polymarket / Kalshi WS price tick into an immediate
    # Cross arb evaluation, instead of waiting for the next 16-min scan.
    # Disabled via CROSS_PAIR_WS_ENABLED=false if it ever needs an
    # emergency kill switch in production.
    from cross_pair_index import CrossPairIndex
    cross_pair_index = CrossPairIndex()
    cross_pair_ws_enabled = os.getenv("CROSS_PAIR_WS_ENABLED", "true").lower() == "true"
    _cross_pair_min_profit_factor = float(os.getenv("CROSS_PAIR_WS_MIN_PROFIT_FACTOR", "1.0"))

    # Initialize PriceTracker for stale price detection (Layer 2)
    _price_tracker = None
    try:
        from price_tracker import PriceTracker
        from config import STALE_PRICE_THRESHOLD, STALE_PRICE_MOVE_PCT
        _price_tracker = PriceTracker(
            stale_threshold_seconds=STALE_PRICE_THRESHOLD,
            move_threshold_pct=STALE_PRICE_MOVE_PCT,
        )
        logger.info("PriceTracker enabled for stale price detection in continuous mode.")
    except Exception as exc:
        logger.debug("PriceTracker not available: %s", exc)

    # Initialize MarketMaker for passive MM (Layer 3)
    _market_maker = None
    try:
        from config import MM_ENABLED, MM_MIN_SPREAD, MM_QUOTE_SIZE, MM_MAX_INVENTORY, MM_MAX_TOTAL_EXPOSURE
        if MM_ENABLED:
            from market_maker import MarketMaker
            _market_maker = MarketMaker(
                min_spread=MM_MIN_SPREAD,
                quote_size=MM_QUOTE_SIZE,
                max_inventory=MM_MAX_INVENTORY,
                max_total_exposure=MM_MAX_TOTAL_EXPOSURE,
                dry_run=executor.dry_run,
            )
            logger.info("MarketMaker enabled in continuous mode (dry_run=%s).", executor.dry_run)
    except Exception as exc:
        logger.debug("MarketMaker not available: %s", exc)

    # Initialize reward trackers for liquidity rewards (Layer 3)
    _reward_tracker = None
    _kalshi_reward_tracker = None
    try:
        if CONFIG_REWARDS_ENABLED:
            from market_maker import RewardTracker, KalshiRewardTracker
            _reward_tracker = RewardTracker()
            _kalshi_reward_tracker = KalshiRewardTracker(db)
            logger.info("Reward trackers enabled in continuous mode.")
    except Exception as exc:
        logger.debug("Reward trackers not available: %s", exc)

    def on_price_update(platform, ticker, data):
        data["_ts"] = time.time()
        with _price_cache_lock:
            price_cache[(platform, ticker)] = data

        # Feed PriceTracker for stale price detection
        if _price_tracker:
            price_val = data.get("price") or data.get("yes") or data.get("yes_price")
            if price_val is not None:
                _price_tracker.update(platform, ticker, float(price_val))

        # Update MarketMaker mid-price for registered markets
        if _market_maker:
            price_val = data.get("price") or data.get("yes") or data.get("yes_price")
            if price_val is not None:
                _market_maker.update_price(ticker, float(price_val))

        # Sprint 3: Feed VolatilityTracker + LeadLagMM with per-tick prices
        _feed_sprint3_trackers(platform, ticker, data)

        nonlocal _seq_counter

        # Event-driven Cross detection (Phase 2): turn this WS tick into
        # an immediate Cross arb evaluation. Unlike opp_index below — which
        # only re-checks opps the slow scan already found — this surfaces
        # *new* Cross opps the moment a price moves into arb territory,
        # bypassing the 16-min scan-cycle latency entirely.
        if cross_pair_ws_enabled and ws_trigger_enabled:
            cross_min_profit = max(min_profit * _cross_pair_min_profit_factor,
                                   ws_trigger_threshold)
            for pair in cross_pair_index.lookup(platform, ticker):
                if _metrics:
                    _metrics.inc("cross_pair_eval_attempts")
                opp = cross_pair_index.evaluate(
                    pair, price_cache, min_profit=cross_min_profit,
                )
                if not opp:
                    continue
                if _metrics:
                    _metrics.inc("cross_pair_eval_hits")
                market_name = opp.get("market", "?")
                logger.info(
                    "WS Cross trigger: %s profit=$%.4f (%s)",
                    market_name[:50], opp["net_profit"], opp["type"],
                )
                priority = -_execution_priority(opp)
                seq = _seq_counter
                _seq_counter += 1
                try:
                    loop = asyncio.get_event_loop()
                    asyncio.run_coroutine_threadsafe(
                        _priority_queue.put((priority, seq, opp)), loop
                    )
                    if _metrics:
                        _metrics.inc("cross_pair_triggers")
                except Exception as exc:
                    logger.debug("Cross priority push failed, skipping: %s", exc)

        # Event-driven execution: check if this update affects a tracked opportunity
        if not ws_trigger_enabled:
            return
        affected = opp_index.lookup(platform, ticker)
        if not affected:
            return
        for opp in affected:
            # Recalculate profit using fresh WS price instead of stale value
            with _price_cache_lock:
                cached = price_cache.get((platform, ticker), {})
            new_price = cached.get("price")
            if new_price is None:
                continue
            recalculated_profit = _recalc_profit(opp, platform, ticker, new_price, price_cache)
            profit = recalculated_profit if recalculated_profit is not None else opp.get("net_profit", 0)
            if profit >= ws_trigger_threshold:
                market_name = opp.get("market", "?")
                # Push to priority queue for ordered execution (OPTIMIZE-03)
                # Time-sensitive opps (stale, resolution) get higher priority (lower value = dequeues first)
                opp_copy = dict(opp)
                opp_copy["net_profit"] = profit
                priority = -_execution_priority(opp_copy)
                seq = _seq_counter
                _seq_counter += 1
                try:
                    loop = asyncio.get_event_loop()
                    asyncio.run_coroutine_threadsafe(
                        _priority_queue.put((priority, seq, opp_copy)), loop
                    )
                except Exception as exc:
                    # Fallback: execute directly if queue push fails
                    logger.debug("Priority queue push failed, executing directly: %s", exc)
                    if not _execution_semaphore.acquire(blocking=False):
                        logger.debug("WS trigger: skipping %s — max concurrent executions reached",
                                     market_name[:30])
                        continue
                    lock = _get_market_lock(market_name)
                    if lock.acquire(blocking=False):
                        try:
                            logger.info("WS trigger: executing %s (profit $%.4f)",
                                        market_name[:30], profit)
                            executor.execute(opp_copy)
                        finally:
                            lock.release()
                            _execution_semaphore.release()
                    else:
                        _execution_semaphore.release()

    def _cleanup_price_cache():
        """Evict price cache entries older than the configured max age."""
        now = time.time()
        with _price_cache_lock:
            stale_keys = [k for k, v in price_cache.items()
                          if now - v.get("_ts", 0) > CONFIG_PRICE_CACHE_EVICTION_AGE]
            for k in stale_keys:
                del price_cache[k]
        if stale_keys:
            logger.debug("Evicted %d stale price cache entries.", len(stale_keys))

    # Crash recovery: reconcile orphaned positions from previous session
    reconcile_orphaned_positions(
        db,
        kalshi_client=kalshi_client,
        pm_trader=pm_trader,
        betfair_client=extra_clients.get("betfair"),
        smarkets_client=extra_clients.get("smarkets"),
        sxbet_client=extra_clients.get("sxbet"),
        matchbook_client=extra_clients.get("matchbook"),
        gemini_client=extra_clients.get("gemini"),
        ibkr_client=extra_clients.get("ibkr"),
    )

    # Initialize partial fill hedger for continuous mode
    hedger = None
    if CONFIG_HEDGE_ENABLED:
        from hedger import PartialFillHedger
        hedger = PartialFillHedger(
            pm_trader=pm_trader,
            kalshi_client=kalshi_client,
            betfair_client=extra_clients.get("betfair"),
            smarkets_client=extra_clients.get("smarkets"),
            sxbet_client=extra_clients.get("sxbet"),
            matchbook_client=extra_clients.get("matchbook"),
            gemini_client=extra_clients.get("gemini"),
            db=db,
        )

    # Initialize snapshot recorder for backtesting data collection
    snapshot_recorder = None
    if CONFIG_SNAPSHOT_ENABLED:
        try:
            from snapshot import SnapshotRecorder
            snapshot_recorder = SnapshotRecorder()
            logger.info("Snapshot recording enabled (interval=%ds).", CONFIG_SNAPSHOT_INTERVAL)
        except Exception as e:
            logger.warning("Failed to initialize snapshot recorder: %s", e)

    _last_snapshot_time = 0.0
    _last_bankroll_refresh = 0.0
    _bankroll_refresh_interval = 300.0  # 5 minutes
    _last_daily_reset_date = time.strftime("%Y-%m-%d", time.gmtime())
    _last_fee_refresh = 0.0
    _last_backtest_run = 0.0
    _last_rebalance_digest = 0.0
    _last_correlation_tracker_run = 0.0

    # Monotonic sequence counter for PriorityQueue tie-breaking (thread-safe via GIL for int ops)
    _seq_counter = 0
    # asyncio.PriorityQueue for WS-triggered high-priority execution (OPTIMIZE-03)
    _priority_queue: asyncio.PriorityQueue = asyncio.PriorityQueue()

    # Import alert_manager for daily resets
    try:
        from alerting import alert_manager as _alert_manager
    except Exception:
        _alert_manager = None

    # Strategy #20 (B3): surface backtest-tuning status at startup. The
    # override values already took effect at config import (behind
    # BACKTEST_TUNING_ENABLED + the recommendation-age gate); this makes the
    # shift visible in logs and fires an alert so threshold changes between
    # restarts never pass silently.
    if config.BACKTEST_TUNING_ENABLED:
        _tuning_applied = getattr(config, "BACKTEST_RECOMMENDATIONS_APPLIED", False)
        # recommended_by_strategy_count is bookkeeping, not an override — a
        # dict containing only it means nothing actually changed.
        _overrides = {
            k: v for k, v in _tuning_applied.items()
            if not isinstance(v, (dict, list)) and k != "recommended_by_strategy_count"
        } if isinstance(_tuning_applied, dict) else {}
        _n_strategies = len(getattr(config, "RECOMMENDED_BY_STRATEGY", {}) or {})
        if _overrides or _n_strategies:
            _scalar_summary = ", ".join(f"{k}={v}" for k, v in _overrides.items())
            _tuning_msg = (
                f"Backtest tuning applied at startup: {_scalar_summary or 'no global overrides'}"
                f" ({_n_strategies} per-strategy overrides)"
            )
            logger.info(_tuning_msg)
            if _alert_manager:
                _alert_manager.alert(
                    "BACKTEST_TUNING_APPLIED", "INFO", _tuning_msg,
                    details=_overrides,
                )
        else:
            logger.info(
                "BACKTEST_TUNING_ENABLED is on but no recommendations were "
                "applied (file missing, stale, invalid, or contained no usable "
                "overrides) — running on env/default thresholds.")

    # Initialize credential health checker
    platform_clients = {
        "polymarket": None,  # Will be set from polymarket_api module functions
        "kalshi": kalshi_client,
        "betfair": extra_clients.get("betfair"),
        "smarkets": extra_clients.get("smarkets"),
        "sxbet": extra_clients.get("sxbet"),
        "matchbook": extra_clients.get("matchbook"),
        "gemini": extra_clients.get("gemini"),
        "ibkr": extra_clients.get("ibkr"),
    }
    # Remove None clients
    platform_clients = {k: v for k, v in platform_clients.items() if v is not None}

    health_checker = None
    if _alert_manager and platform_clients:
        from config import CREDENTIAL_HEALTH_CHECK_INTERVAL
        health_checker = CredentialHealthChecker(
            platform_clients=platform_clients,
            alert_manager=_alert_manager,
            interval_seconds=CREDENTIAL_HEALTH_CHECK_INTERVAL,
        )
        logger.info("Credential health checker initialized for %d platforms", len(platform_clients))

    # Initialize WebSocket feed manager
    feed_manager = FeedManager(
        on_price_update=on_price_update,
        kalshi_api_key_id=kalshi_api_key_id,
        kalshi_private_key_path=kalshi_private_key_path,
        kalshi_private_key_base64=kalshi_private_key_base64,
    )

    async def _priority_consumer():
        """Drain the priority queue, executing WS-triggered opps in priority order.

        Time-sensitive opportunities (StalePriceOpp, ResolutionSnipeOpp) are
        inserted with a lower queue value and thus execute before lower-priority
        types. Logs a warning if execution latency exceeds 500ms (OPTIMIZE-03).
        """
        while not shutdown_event.is_set():
            try:
                try:
                    item = await asyncio.wait_for(_priority_queue.get(), timeout=1.0)
                except asyncio.TimeoutError:
                    continue

                _priority_val, _seq, opp = item
                market_name = opp.get("market", "?")
                profit = opp.get("net_profit", 0)

                if not _execution_semaphore.acquire(blocking=False):
                    logger.debug(
                        "Priority consumer: skipping %s — semaphore full", market_name[:30])
                    _priority_queue.task_done()
                    continue

                lock = _get_market_lock(market_name)
                if lock.acquire(blocking=False):
                    try:
                        _exec_start = time.time()
                        logger.info(
                            "Priority queue execute: %s (profit $%.4f, priority %.3f)",
                            market_name[:30], profit, -_priority_val,
                        )
                        result = executor.execute(opp)
                        _exec_elapsed_ms = (time.time() - _exec_start) * 1000
                        if _exec_elapsed_ms > 500:
                            logger.warning(
                                "Priority execution latency %.0fms exceeded 500ms for %s",
                                _exec_elapsed_ms, market_name[:30],
                            )
                        # Wire loss spike alerting (MONITOR-03)
                        if result is False and _alert_manager:
                            try:
                                loss = abs(profit)
                                _alert_manager.check_loss_spike(loss)
                            except Exception:
                                pass
                    finally:
                        lock.release()
                        _execution_semaphore.release()
                else:
                    _execution_semaphore.release()

                _priority_queue.task_done()
            except Exception as exc:
                logger.debug("Priority consumer error: %s", exc)

    async def _monitor_feed_staleness():
        """Background task: mark stale feeds every 5 seconds.

        Checks if WebSocket feeds have gone silent for 30+ seconds and marks
        all cached prices from stale feeds with _stale: true. When feeds
        recover, clears the stale flag.
        """
        while not shutdown_event.is_set():
            try:
                feed_manager.mark_stale_feeds(stale_threshold_seconds=30.0)
                await asyncio.sleep(5)  # Check every 5 seconds
            except Exception as e:
                logger.warning("Feed staleness check failed: %s", e)
                await asyncio.sleep(5)  # Retry after 5 seconds

    async def _monitor_credential_health():
        """Background task: check API credential health every 30 minutes.

        Probes each platform's auth status with a cheap endpoint, detects
        invalid credentials or approaching token expiry, and fires alerts.
        """
        while not shutdown_event.is_set():
            try:
                if health_checker:
                    results = await health_checker.check_all_platforms()
                    logger.info("Credential health check complete: %s", results)
                await asyncio.sleep(1800)  # 30 minutes
            except Exception as e:
                logger.warning("Credential health check failed: %s", e)
                await asyncio.sleep(1800)  # Retry after 30 minutes

    async def _continuous_loop():
        ws_task = None
        priority_consumer_task = None
        stale_monitor_task = None
        health_monitor_task = None
        scan_count = 0

        # Start priority consumer coroutine as a background task
        priority_consumer_task = asyncio.create_task(_priority_consumer())
        logger.info("Priority execution consumer started.")

        # Start feed staleness monitor as a background task
        stale_monitor_task = asyncio.create_task(_monitor_feed_staleness())

        # Start credential health monitor as a background task
        if health_checker:
            health_monitor_task = asyncio.create_task(_monitor_credential_health())
            logger.info("Credential health monitor started.")
        logger.info("Feed staleness monitor started.")

        while not shutdown_event.is_set():
            scan_count += 1
            logger.info("=" * 80)
            logger.info("CONTINUOUS SCAN #%d", scan_count)
            logger.info("=" * 80)

            _scan_start = time.time()
            _stage_timings: dict[str, float] = {}

            # Daily reset for metrics and alert state
            nonlocal _last_daily_reset_date
            _today = time.strftime("%Y-%m-%d", time.gmtime())
            if _today != _last_daily_reset_date:
                logger.info("Daily reset triggered (new day: %s)", _today)
                if _metrics:
                    _metrics.reset_daily()
                if _alert_manager:
                    _alert_manager.reset_daily()
                _last_daily_reset_date = _today

            try:
                from concurrent.futures import ThreadPoolExecutor

                # Stage 1: Fetch data from all platforms in parallel
                poly_markets = []
                poly_events = None
                kalshi_data = None

                with _StageTimer("fetch", _stage_timings):
                    fetch_futures = {}
                    with ThreadPoolExecutor(max_workers=3) as pool:
                        if args.mode not in ("kalshi", "betfair", "smarkets", "sxbet", "matchbook", "gemini", "ibkr", "triangular"):
                            fetch_futures["poly_markets"] = pool.submit(fetch_all_markets)
                        if args.mode in ("all", "negrisk", "multi-cross"):
                            fetch_futures["poly_events"] = pool.submit(fetch_events)
                        if args.mode in ("all", "kalshi", "cross", "spread", "multi-cross") and kalshi_client:
                            fetch_futures["kalshi_data"] = pool.submit(_fetch_kalshi_data, kalshi_client)

                        for key, future in fetch_futures.items():
                            try:
                                result = future.result()
                                if key == "poly_markets":
                                    poly_markets = result or []
                                elif key == "poly_events":
                                    poly_events = result
                                elif key == "kalshi_data":
                                    kalshi_data = result
                            except Exception as e:
                                logger.error("Failed to fetch %s: %s", key, e)

                all_opportunities = []

                # Stage 2: Run scans in parallel
                with _StageTimer("scan_parallel", _stage_timings):
                    scan_futures = {}
                    with ThreadPoolExecutor(max_workers=4) as pool:
                        if args.mode in ("all", "binary") and poly_markets:
                            scan_futures["binary"] = pool.submit(
                                scan_binary_internal, poly_markets, min_profit,
                                price_cache=price_cache)
                        if args.mode in ("all", "negrisk") and poly_events:
                            scan_futures["negrisk"] = pool.submit(
                                scan_negrisk_internal, poly_events, min_profit,
                                price_cache=price_cache)
                        if args.mode in ("all", "kalshi") and kalshi_client:
                            scan_futures["kalshi_binary"] = pool.submit(
                                scan_kalshi_binary, kalshi_client, min_profit, kalshi_data=kalshi_data)
                            # KalshiMulti kill-switch: disable for thin multi-outcome markets
                            # that cause Fill-or-Kill partial fills (no exit liquidity for hedge)
                            if config.KALSHI_MULTI_ENABLED:
                                scan_futures["kalshi_multi"] = pool.submit(
                                    scan_kalshi_multi, kalshi_client, min_profit, kalshi_data=kalshi_data)

                        for key, future in scan_futures.items():
                            try:
                                opps = future.result()
                                all_opportunities.extend(opps)
                            except Exception as e:
                                logger.error("Scan %s failed: %s", key, e)

                # Stage 3: Cross-platform scans (need data from above)
                kalshi_events_preloaded = kalshi_data[0] if kalshi_data else None
                _stage3_start = time.time()

                # Rebuild the persistent CrossPairIndex used by on_price_update
                # for event-driven Cross detection. Tying this rebuild to the
                # scan cycle (vs a separate timer) reuses the data we just
                # fetched for free; the WS handler then evaluates pairs on
                # every tick without waiting for the 16-min cycle to find them.
                if cross_pair_ws_enabled and poly_markets and kalshi_events_preloaded:
                    try:
                        n_pairs = cross_pair_index.rebuild(
                            poly_markets, kalshi_events_preloaded,
                            min_confidence=args.min_confidence,
                        )
                        logger.info("CrossPairIndex active: %d pairs available for WS-driven evaluation", n_pairs)
                        if _metrics:
                            _metrics.set("cross_pair_index_size", value=n_pairs)
                    except Exception as exc:
                        logger.warning("CrossPairIndex rebuild failed (non-fatal): %s", exc, exc_info=True)

                if args.mode in ("all", "cross"):
                    cross_opps = scan_cross_platform(
                        poly_markets, kalshi_client, min_profit,
                        min_confidence=args.min_confidence,
                        kalshi_events_preloaded=kalshi_events_preloaded,
                        price_cache=price_cache,
                    )
                    all_opportunities.extend(cross_opps)

                if args.mode == "cross-all":
                    platform_clients = {}
                    for name, client in extra_clients.items():
                        if client:
                            try:
                                if name == "betfair":
                                    events = client.list_events()
                                    markets = []
                                    for ev in events[:50]:
                                        ev_data = ev.get("event", {})
                                        ev_id = ev_data.get("id", "")
                                        if ev_id:
                                            mkt_list = client.list_markets(ev_id)
                                            markets.extend(mkt_list)
                                elif name in ("smarkets", "sxbet", "matchbook", "gemini", "ibkr"):
                                    markets = client.fetch_all_markets()
                                else:
                                    markets = []
                                if markets:
                                    platform_clients[name] = (client, markets)
                            except Exception as e:
                                logger.warning("Failed to fetch %s markets: %s", name, e)

                    cross_all_opps = scan_cross_all(
                        poly_markets, platform_clients, min_profit,
                        min_confidence=args.min_confidence,
                        price_cache=price_cache,
                    )
                    all_opportunities.extend(cross_all_opps)

                _stage_timings["cross"] = time.time() - _stage3_start
                _stage4_start = time.time()

                # Stage 4: Platform-specific scans (spread, betfair, etc.)
                if args.mode in ("all", "spread"):
                    if poly_markets:
                        spread_pm = scan_spread_polymarket(poly_markets, min_profit)
                        all_opportunities.extend(spread_pm)

                if args.mode in ("all", "betfair"):
                    betfair = extra_clients.get("betfair")
                    if betfair:
                        bf_backall = scan_betfair_backall(betfair, min_profit)
                        all_opportunities.extend(bf_backall)
                        bf_backlay = scan_betfair_backlay(betfair, min_profit)
                        all_opportunities.extend(bf_backlay)

                if args.mode in ("all", "smarkets"):
                    smarkets = extra_clients.get("smarkets")
                    if smarkets:
                        sm_backall = scan_smarkets_backall(smarkets, min_profit)
                        all_opportunities.extend(sm_backall)
                        sm_backlay = scan_smarkets_backlay(smarkets, min_profit)
                        all_opportunities.extend(sm_backlay)

                if args.mode in ("all", "sxbet"):
                    sxbet = extra_clients.get("sxbet")
                    if sxbet:
                        sx_backall = scan_sxbet_backall(sxbet, min_profit)
                        all_opportunities.extend(sx_backall)
                        sx_backlay = scan_sxbet_backlay(sxbet, min_profit)
                        all_opportunities.extend(sx_backlay)

                if args.mode in ("all", "matchbook"):
                    matchbook = extra_clients.get("matchbook")
                    if matchbook:
                        mb_backall = scan_matchbook_backall(matchbook, min_profit)
                        all_opportunities.extend(mb_backall)
                        mb_backlay = scan_matchbook_backlay(matchbook, min_profit)
                        all_opportunities.extend(mb_backlay)

                if args.mode in ("all", "gemini"):
                    gemini = extra_clients.get("gemini")
                    if gemini:
                        gm_binary = scan_gemini_binary(gemini, min_profit)
                        all_opportunities.extend(gm_binary)
                        gm_multi = scan_gemini_multi(gemini, min_profit)
                        all_opportunities.extend(gm_multi)

                if args.mode in ("all", "ibkr"):
                    ibkr = extra_clients.get("ibkr")
                    if ibkr:
                        ibkr_binary = scan_ibkr_binary(ibkr, min_profit)
                        all_opportunities.extend(ibkr_binary)

                _stage_timings["per_exchange"] = time.time() - _stage4_start
                _stage5_start = time.time()

                if args.mode in ("all", "event") and event_monitor:
                    platform_markets_for_event = {}
                    if poly_markets:
                        platform_markets_for_event["polymarket"] = poly_markets
                    if kalshi_data and kalshi_data[0]:
                        platform_markets_for_event["kalshi"] = kalshi_data[0]
                    if platform_markets_for_event:
                        event_opps = event_monitor.scan_event_divergences(
                            platform_markets_for_event, min_profit=min_profit)
                        all_opportunities.extend(event_opps)

                if args.mode in ("all", "triangular", "nway"):
                    platform_markets_for_tri = {}
                    platform_clients_for_tri = {}
                    if poly_markets:
                        platform_markets_for_tri["polymarket"] = poly_markets
                    if kalshi_data and kalshi_data[0]:
                        platform_markets_for_tri["kalshi"] = kalshi_data[0]
                        platform_clients_for_tri["kalshi"] = kalshi_client
                    for name, client in extra_clients.items():
                        if client:
                            try:
                                if name == "betfair":
                                    events = client.list_events()
                                    markets = []
                                    for ev in events[:50]:
                                        ev_data = ev.get("event", {})
                                        ev_id = ev_data.get("id", "")
                                        if ev_id:
                                            mkt_list = client.list_markets(ev_id)
                                            markets.extend(mkt_list)
                                elif name in ("smarkets", "sxbet", "matchbook", "gemini", "ibkr"):
                                    markets = client.fetch_all_markets()
                                else:
                                    markets = []
                                if markets:
                                    platform_markets_for_tri[name] = markets
                                    platform_clients_for_tri[name] = client
                            except Exception as e:
                                logger.warning("Triangular: failed to fetch %s: %s", name, e)
                    if args.mode in ("all", "triangular"):
                        tri_opps = scan_triangular(
                            platform_markets_for_tri, platform_clients_for_tri, min_profit,
                            min_confidence=args.min_confidence,
                        )
                        all_opportunities.extend(tri_opps)

                    if args.mode in ("all", "nway"):
                        try:
                            nway_opps = scan_nway_arb(
                                platform_markets_for_tri, platform_clients_for_tri,
                                min_profit, min_confidence=args.min_confidence,
                            )
                            all_opportunities.extend(nway_opps)
                        except Exception as exc:
                            logger.warning("NWayArb scan failed: %s", exc)

                # Sprint 3: LeadLagMM periodic scan. Mirrors the cli.py
                # --mode lead-lag-mm dispatch — uses poly+kalshi matched pairs
                # and asks the LeadLagMM detector which platform is lagging.
                # No-op when LEAD_LAG_MM_ENABLED is false (the scan itself
                # short-circuits on the flag).
                if args.mode in ("all", "lead-lag-mm"):
                    try:
                        from matcher import match_cross_platform
                        kalshi_events_for_ll = kalshi_data[0] if kalshi_data else None
                        if poly_markets and kalshi_events_for_ll:
                            pairs = match_cross_platform(
                                poly_markets, kalshi_events_for_ll,
                                platform_a="polymarket", platform_b="kalshi",
                            )
                            ll_opps = scan_lead_lag_mm(pairs)
                            all_opportunities.extend(ll_opps)
                    except Exception as exc:
                        logger.warning("LeadLagMM scan failed: %s", exc)

                # Sprint 3: ToxicFlowPause + VolatilityAdjustedMM periodic
                # observability scans. Both consume a market_keys list (built
                # once from currently-known poly+kalshi markets) and inspect
                # the respective detector singletons. No-op when their flags
                # are false.
                if args.mode in ("all", "toxic-flow", "vol-mm"):
                    try:
                        observability_market_keys: list[str] = []
                        if poly_markets:
                            for mkt in poly_markets[:50]:
                                cid = mkt.get("condition_id") or mkt.get("conditionId") or ""
                                if cid:
                                    observability_market_keys.append(cid)
                        if kalshi_data and kalshi_data[0]:
                            for evt in kalshi_data[0][:50]:
                                for mkt in evt.get("markets", [evt]):
                                    t = mkt.get("ticker", "")
                                    if t:
                                        observability_market_keys.append(t)
                        if args.mode in ("all", "toxic-flow"):
                            tox_opps = scan_toxic_flow_pause(observability_market_keys)
                            all_opportunities.extend(tox_opps)
                        if args.mode in ("all", "vol-mm"):
                            vol_opps = scan_volatility_adjusted_mm(observability_market_keys)
                            all_opportunities.extend(vol_opps)
                    except Exception as exc:
                        logger.warning("ToxicFlow/VolMM scans failed: %s", exc)

                # MultiCross kill-switch: same FOK partial-fill vulnerability
                # as KalshiMulti. Places N legs concurrently on thin Kalshi
                # multi-outcome markets, leaving unhedgeable orphans when legs
                # fail. Disabled until depth gate is added.
                if (args.mode in ("all", "multi-cross")
                        and poly_events and kalshi_client
                        and config.MULTI_CROSS_ENABLED):
                    mc_opps = scan_multi_cross(
                        poly_events, kalshi_client, min_profit,
                        kalshi_data=kalshi_data,
                        price_cache=price_cache,
                    )
                    all_opportunities.extend(mc_opps)

                # Layer 3: Liquidity Rewards
                if args.mode in ("all", "rewards") and CONFIG_REWARDS_ENABLED:
                    try:
                        if poly_markets and _reward_tracker:
                            pm_reward_opps = scan_polymarket_rewards(
                                markets=poly_markets,
                                reward_tracker=_reward_tracker,
                                price_cache=price_cache,
                            )
                            all_opportunities.extend(pm_reward_opps)

                        if kalshi_client and _kalshi_reward_tracker:
                            k_reward_opps = scan_kalshi_rewards(
                                kalshi_client=kalshi_client,
                                reward_tracker=_kalshi_reward_tracker,
                            )
                            all_opportunities.extend(k_reward_opps)

                        logger.debug(
                            "Rewards scan complete: %d Polymarket + %d Kalshi opps",
                            len(pm_reward_opps) if poly_markets and _reward_tracker else 0,
                            len(k_reward_opps) if kalshi_client and _kalshi_reward_tracker else 0,
                        )
                    except Exception as exc:
                        logger.debug("Rewards scanning error: %s", exc)

                # Layer 4: informed-trading scans (flag-gated; disabled by
                # default). Each helper gates on its own config flag + mode and
                # returns [] when off. Failures log at WARNING (not the old
                # silent debug) so an enabled-but-broken strategy stays visible.
                try:
                    all_opportunities.extend(
                        _scan_imbalance_layer4(poly_markets, price_cache, args.mode)
                    )
                except Exception as exc:
                    logger.warning("Imbalance scan failed: %s", exc)

                try:
                    all_opportunities.extend(
                        _scan_news_snipe_layer4(poly_markets, args.mode)
                    )
                except Exception as exc:
                    logger.warning("News-snipe scan failed: %s", exc)

                try:
                    all_opportunities.extend(
                        _scan_correlated_layer4(poly_markets, price_cache, args.mode)
                    )
                except Exception as exc:
                    logger.warning("Correlated-pairs scan failed: %s", exc)

                try:
                    all_opportunities.extend(
                        _scan_time_decay_layer4(poly_markets, price_cache, args.mode)
                    )
                except Exception as exc:
                    logger.warning("Time-decay scan failed: %s", exc)

                try:
                    all_opportunities.extend(
                        _scan_jev_crypto_continuous(
                            poly_markets,
                            args.mode,
                            min_profit,
                            db=db,
                        )
                    )
                except Exception as exc:
                    logger.warning("Jev crypto scan failed: %s", exc)

                # Structural alpha: Combinatorial logical arbitrage (Phase 9)
                if args.mode in ("all", "logical-arb"):
                    try:
                        from config import LOGICAL_ARB_ENABLED, LOGICAL_ARB_RULES, LOGICAL_ARB_PRICE_THRESHOLD
                        if LOGICAL_ARB_ENABLED and LOGICAL_ARB_RULES:
                            from scans.logical_arb import scan_logical_arb
                            logical_arb_opps = scan_logical_arb(
                                markets_by_key=poly_markets if poly_markets else [],
                                logical_arb_rules=LOGICAL_ARB_RULES,
                                price_threshold=LOGICAL_ARB_PRICE_THRESHOLD,
                            )
                            all_opportunities.extend(logical_arb_opps)
                            logger.info("Logical arb scan: found %d opportunities", len(logical_arb_opps))
                    except Exception as e:
                        logger.debug("Logical arb scan failed: %s", e)

                # Structural alpha: Whale copy trading (Phase 9)
                if args.mode in ("all", "whale-copy"):
                    try:
                        from config import WHALE_COPY_ENABLED, WHALE_WALLETS, POLYGONSCAN_API_KEY
                        if WHALE_COPY_ENABLED and WHALE_WALLETS:
                            from scans.whale_copy import scan_whale_copy
                            from polygonscan_api import PolygonscanClient
                            polygonscan = PolygonscanClient(api_key=POLYGONSCAN_API_KEY)
                            whale_copy_opps = scan_whale_copy(
                                whale_wallets=WHALE_WALLETS,
                                polygonscan_client=polygonscan,
                                last_block_cache=None,
                            )
                            all_opportunities.extend(whale_copy_opps)
                            logger.info("Whale copy scan: found %d opportunities", len(whale_copy_opps))
                    except Exception as e:
                        logger.debug("Whale copy scan failed: %s", e)

                # Seed PriceTracker from REST data (WS only covers subscribed markets)
                if _price_tracker:
                    if poly_markets:
                        for mkt in poly_markets:
                            cid = mkt.get("condition_id", "")
                            tokens = mkt.get("tokens", [])
                            for t in tokens:
                                if t.get("outcome", "").lower() == "yes":
                                    p = t.get("price")
                                    if p and cid:
                                        _price_tracker.update("polymarket", cid, float(p))
                    if kalshi_data and kalshi_data[0]:
                        for evt in kalshi_data[0]:
                            for mkt in evt.get("markets", [evt]):
                                ticker = mkt.get("ticker", "")
                                yp = mkt.get("yes_ask") or mkt.get("yes_price")
                                if ticker and yp:
                                    pv = float(yp)
                                    if pv > 1:
                                        pv /= 100.0
                                    _price_tracker.update("kalshi", ticker, pv)

                # Layer 2: Stale price detection (continuous mode — tracker has WS + REST data)
                if args.mode in ("all", "stale") and _price_tracker:
                    try:
                        from scans.stale import scan_stale_prices
                        from config import STALE_PRICE_MOVE_PCT, STALE_PRICE_THRESHOLD
                        # Build matched_markets from all keys the tracker has across 2+ platforms
                        _all_tracker_keys = set()
                        with _price_tracker._lock:
                            for mkey, plats in _price_tracker._prices.items():
                                if len(plats) >= 2:
                                    _all_tracker_keys.add(mkey)
                        _matched_for_stale = [{"market_key": k} for k in _all_tracker_keys]
                        stale_opps = scan_stale_prices(
                            _price_tracker, _matched_for_stale,
                            min_move_pct=STALE_PRICE_MOVE_PCT,
                            min_stale_seconds=STALE_PRICE_THRESHOLD, min_profit=min_profit,
                        )
                        all_opportunities.extend(stale_opps)
                    except Exception as exc:
                        logger.debug("Stale price scan failed: %s", exc)

                # Layer 2: Resolution sniping
                if args.mode in ("all", "resolution") and poly_markets:
                    try:
                        from scans.resolution import scan_resolution_snipes
                        res_opps = scan_resolution_snipes(
                            poly_markets, platform="polymarket", min_profit=min_profit,
                        )
                        all_opportunities.extend(res_opps)
                    except Exception as exc:
                        logger.debug("Resolution snipe scan failed: %s", exc)

                # Kalshi resolution sniping
                if args.mode in ("all", "resolution") and kalshi_data:
                    try:
                        from scans.resolution import scan_resolution_snipes
                        # kalshi_data is (events, markets_by_event, event_titles)
                        # Flatten markets_by_event dict into a flat list for resolution scan
                        kalshi_flat_markets = []
                        if len(kalshi_data) >= 2 and kalshi_data[1]:
                            for _evt_ticker, _mkts in kalshi_data[1].items():
                                kalshi_flat_markets.extend(_mkts)
                        if kalshi_flat_markets:
                            k_res_opps = scan_resolution_snipes(
                                kalshi_flat_markets, platform="kalshi", min_profit=min_profit,
                            )
                            all_opportunities.extend(k_res_opps)
                    except Exception as exc:
                        logger.debug("Kalshi resolution snipe scan failed: %s", exc)

                # Layer 4: Cross-platform convergence
                if args.mode in ("all", "convergence"):
                    try:
                        from scans.convergence import scan_convergence
                        from config import CONVERGENCE_MIN_DIVERGENCE, CONVERGENCE_MIN_PLATFORMS
                        from matcher import match_cross_platform
                        # Build platform_prices_map from current data
                        _conv_prices: dict[str, dict] = {}
                        if poly_markets:
                            for mkt in poly_markets:
                                cid = mkt.get("condition_id", "")
                                title = mkt.get("question") or mkt.get("title", "")
                                tokens = mkt.get("tokens", [])
                                yp = None
                                for t in tokens:
                                    if t.get("outcome", "").lower() == "yes":
                                        yp = t.get("price")
                                if cid and yp:
                                    _conv_prices.setdefault(cid, {})["polymarket"] = {
                                        "yes": float(yp), "no": 1.0 - float(yp),
                                    }
                                    _conv_prices[cid]["_title"] = title
                        if kalshi_data and kalshi_data[0] and poly_markets:
                            kflat = []
                            for evt in kalshi_data[0]:
                                for mkt in evt.get("markets", [evt]):
                                    kflat.append(mkt)
                            matches = match_cross_platform(
                                poly_markets, kflat, "polymarket", "kalshi",
                                threshold=72, min_confidence=args.min_confidence,
                            )
                            for m in matches:
                                pm_mkt = m.get("market_a", {})
                                k_mkt = m.get("market_b", {})
                                cid = pm_mkt.get("condition_id", "")
                                ya = k_mkt.get("yes_ask") or k_mkt.get("yes_price")
                                if cid and ya:
                                    pv = float(ya)
                                    if pv > 1:
                                        pv /= 100.0
                                    _conv_prices.setdefault(cid, {})["kalshi"] = {
                                        "yes": pv, "no": 1.0 - pv,
                                    }
                        _conv_matched = []
                        for mk, data in _conv_prices.items():
                            title = data.pop("_title", mk)
                            pp = {k: v for k, v in data.items() if isinstance(v, dict)}
                            if len(pp) >= 2:
                                _conv_matched.append({
                                    "market_key": mk, "title": title, "platform_prices": pp,
                                })
                        conv_opps = scan_convergence(
                            _conv_matched, min_divergence=CONVERGENCE_MIN_DIVERGENCE,
                            min_platforms=CONVERGENCE_MIN_PLATFORMS, min_profit=min_profit,
                        )
                        all_opportunities.extend(conv_opps)
                    except Exception as exc:
                        logger.debug("Convergence scan failed: %s", exc)

                # Layer 3: Market making — refresh quotes and generate pseudo-opps
                if args.mode in ("all", "mm") and _market_maker:
                    try:
                        # Register any new liquid markets
                        if poly_markets:
                            for mkt in poly_markets[:20]:
                                tokens = mkt.get("tokens", [])
                                if tokens:
                                    price = tokens[0].get("price")
                                    if price and 0.1 < float(price) < 0.9:
                                        cid = mkt.get("condition_id", "")
                                        if cid:
                                            _market_maker.add_market(cid, "polymarket", float(price))
                        # Refresh quotes
                        _market_maker.refresh_quotes(trader=pm_trader if not executor.dry_run else None)
                        mm_opps = _market_maker.generate_opportunities()
                        all_opportunities.extend(mm_opps)
                    except Exception as exc:
                        logger.debug("Market maker scan failed: %s", exc)

                # Periodic price tracker cleanup
                if _price_tracker and scan_count % 10 == 0:
                    _price_tracker.cleanup(max_age_seconds=300)

                # Platform fund rebalancing check (every 5 scans)
                if scan_count % 5 == 0 and notifier:
                    try:
                        _check_platform_balance(
                            executor, all_opportunities, notifier, scan_count)
                    except Exception as exc:
                        logger.debug("Rebalancing check failed: %s", exc)

                # Apply filters
                if args.min_depth > 0:
                    all_opportunities = [
                        opp for opp in all_opportunities
                        if opp.get("_clob_depth", 0) >= args.min_depth
                    ]

                all_opportunities.sort(key=_execution_priority, reverse=True)

                _stage_timings["advanced"] = time.time() - _stage5_start
                _stage_display_start = time.time()

                if args.limit:
                    all_opportunities = all_opportunities[:args.limit]

                display_results(all_opportunities, args.json)

                # Send webhook notification
                if notifier and all_opportunities:
                    notifier.notify(all_opportunities)

                # Update dashboard state
                dashboard_state.scan_count = scan_count
                dashboard_state.last_scan_time = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
                dashboard_state.opportunities_found += len(all_opportunities)
                dashboard_state.last_opportunities = all_opportunities[:20]
                dashboard_state.open_positions = db.get_open_positions_count()
                dashboard_state.daily_pnl = db.get_daily_pnl()
                dashboard_state.ws_connections = (1 if ws_task and not ws_task.done() else 0)
                # Update Layer 2-5 dashboard counters
                dashboard_state.stale_detections = sum(
                    1 for o in all_opportunities if o.get("type") == "StalePriceOpp")
                dashboard_state.resolution_snipes = sum(
                    1 for o in all_opportunities if o.get("type") == "ResolutionSnipeOpp")
                dashboard_state.convergence_signals = sum(
                    1 for o in all_opportunities if o.get("type") == "ConvergenceOpp")
                dashboard_state.jev_crypto_opps = sum(
                    1 for o in all_opportunities if o.get("type") == "JevCrypto")
                if _market_maker:
                    mm_status = _market_maker.get_status()
                    dashboard_state.mm_active_markets = mm_status["active_markets"]
                    dashboard_state.mm_active_orders = mm_status["active_orders"]
                    dashboard_state.mm_total_exposure = mm_status["total_exposure"]

                # Update reward tracker reference for dashboard metrics
                if CONFIG_REWARDS_ENABLED and _reward_tracker:
                    dashboard_state.reward_tracker = _reward_tracker

                # Update strategy metrics (MON-01: per-strategy P&L analytics)
                # Also update leaderboard (MON-02: strategy leaderboard endpoint)
                try:
                    data_dir = config.DATA_DIR if hasattr(config, 'DATA_DIR') else "."
                    db_path = f"{data_dir}/trades.db"
                    metrics = get_strategy_metrics(db_path=db_path, lookback_days=7)
                    dashboard_state.strategy_metrics = metrics
                    dashboard_state.update_strategy_metrics(metrics)
                    if metrics:
                        logger.info("Updated strategy metrics: %d strategies", len(metrics))
                except Exception as e:
                    logger.warning("Failed to update strategy metrics: %s", e)

                # Update metrics
                if _metrics:
                    _scan_duration = time.time() - _scan_start
                    _metrics.inc("scans_total")
                    _metrics.inc("opportunities_found", value=len(all_opportunities))
                    _metrics.observe("scan_duration_seconds", value=_scan_duration)
                    _metrics.set("scan_cycle_duration_seconds", value=_scan_duration)
                    _metrics.set("active_positions", value=dashboard_state.open_positions)
                    _metrics.set("daily_pnl", value=dashboard_state.daily_pnl)
                    if all_opportunities:
                        best_roi_str = all_opportunities[0].get("net_roi", "0%")
                        try:
                            best_roi = float(best_roi_str.replace("%", "")) / 100 if isinstance(best_roi_str, str) else float(best_roi_str)
                        except (ValueError, TypeError):
                            best_roi = 0
                        _metrics.set("best_opportunity_roi", value=best_roi)
                        for opp in all_opportunities:
                            _metrics.observe("opportunity_profit", value=opp.get("net_profit", 0))
                    _metrics.set("ws_connected", {"platform": "combined"},
                                 value=1 if ws_task and not ws_task.done() else 0)

                # Check for stale WS feeds (no data received for > 120s)
                stale_feeds = feed_manager.get_stale_feeds(max_silent_seconds=120.0)
                if stale_feeds:
                    logger.warning("Stale WS feeds detected (no data for >120s): %s",
                                   ", ".join(stale_feeds))
                    if _metrics:
                        for sf in stale_feeds:
                            _metrics.set("ws_connected", {"platform": sf}, value=0)

                # Evict stale price cache entries
                _cleanup_price_cache()

                # Check for settled positions
                check_settlements(
                    db, kalshi_client, poly_markets,
                    betfair_client=extra_clients.get("betfair"),
                    smarkets_client=extra_clients.get("smarkets"),
                    sxbet_client=extra_clients.get("sxbet"),
                    matchbook_client=extra_clients.get("matchbook"),
                    gemini_client=extra_clients.get("gemini"),
                    ibkr_client=extra_clients.get("ibkr"),
                )

                # Execute opportunities sequentially (balance must be rechecked between trades)
                if all_opportunities:
                    # Apply execution budget cap (selectivity control).
                    # Opportunities are already sorted by _execution_priority
                    # (weight * capital_efficiency_score), so slicing [:N]
                    # keeps the top N highest-priority candidates per cycle.
                    budget = getattr(config, "EXECUTION_BUDGET_PER_SCAN", 0)
                    exec_queue = (
                        all_opportunities[:budget]
                        if budget > 0 else all_opportunities
                    )
                    if budget > 0 and len(all_opportunities) > budget:
                        logger.info(
                            "Execution budget: top %d of %d opportunities selected",
                            budget, len(all_opportunities),
                        )
                    logger.info("--- Execution Pass ---")
                    executed = 0
                    for opp in exec_queue:
                        if shutdown_event.is_set():
                            break
                        try:
                            if executor.execute(opp):
                                executed += 1
                                # Immediate bankroll refresh after trade (per user decision)
                                try:
                                    balances = executor._fetch_balances("Cross")
                                    if balances and executor.position_sizer:
                                        total = sum(
                                            v for v in balances.values()
                                            if isinstance(v, (int, float))
                                        )
                                        if total > 0:
                                            executor.position_sizer.update_bankroll(total)
                                except Exception as exc:
                                    logger.debug("Post-trade bankroll refresh failed: %s", exc)
                        except Exception as e:
                            logger.error("Execution error: %s", e)
                    logger.info("Executed: %d/%d", executed, len(exec_queue))

                # Process any pending hedges from partial fills
                if hedger:
                    try:
                        hedger.process_pending_hedges()
                    except Exception as e:
                        logger.warning("Hedger processing failed: %s", e)

                # Record price snapshots for backtesting
                if snapshot_recorder and all_opportunities:
                    nonlocal _last_snapshot_time
                    now = time.time()
                    if now - _last_snapshot_time >= CONFIG_SNAPSHOT_INTERVAL:
                        try:
                            recorded = snapshot_recorder.record_snapshot(all_opportunities)
                            if recorded:
                                logger.debug("Recorded %d snapshots.", recorded)
                            _last_snapshot_time = now
                        except Exception as e:
                            logger.warning("Snapshot recording failed: %s", e)

                # Timer-based bankroll refresh (every 5 minutes)
                nonlocal _last_bankroll_refresh
                _now = time.time()
                if _now - _last_bankroll_refresh >= _bankroll_refresh_interval:
                    try:
                        balances = executor._fetch_balances("Cross")
                        if balances:
                            from dashboard import state as _ds
                            from datetime import datetime as _dt, timezone as _tz
                            _ds.platform_balances = dict(balances)
                            _ds.last_bankroll_refresh = _dt.now(_tz.utc).isoformat()
                            if executor.position_sizer:
                                total = sum(
                                    v for v in balances.values() if isinstance(v, (int, float))
                                )
                                if total > 0:
                                    executor.position_sizer.update_bankroll(total)
                                    logger.info(
                                        "Bankroll refreshed: $%.2f across %d platforms",
                                        total, len(balances),
                                    )
                                else:
                                    logger.warning(
                                        "Bankroll refresh returned $0 across %d platforms: %s",
                                        len(balances), balances,
                                    )
                        else:
                            logger.warning("Bankroll refresh: _fetch_balances returned no balances")
                        _last_bankroll_refresh = _now
                    except Exception as exc:
                        logger.warning("Bankroll refresh failed: %s", exc, exc_info=True)
                        _last_bankroll_refresh = _now  # Don't retry immediately on failure

                # Hourly fee rate reload (OPTIMIZE-01)
                nonlocal _last_fee_refresh
                if _now - _last_fee_refresh >= config.FEE_REFRESH_INTERVAL:
                    try:
                        fee_changes = config.reload_fee_rates()
                        if fee_changes:
                            logger.info("Fee rates updated: %s", fee_changes)
                    except Exception as exc:
                        logger.debug("Fee rate reload failed: %s", exc)
                    _last_fee_refresh = _now

                # Nightly backtest and threshold recommendations (OPTIMIZE-02)
                nonlocal _last_backtest_run
                if _now - _last_backtest_run >= config.BACKTEST_RUN_INTERVAL:
                    async def _run_nightly_backtest():
                        try:
                            loop = asyncio.get_event_loop()
                            from datetime import datetime as _dt, timedelta as _td, timezone as _tz
                            _end_iso = _dt.now(_tz.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
                            _start_iso = (_dt.now(_tz.utc) - _td(days=7)).strftime("%Y-%m-%dT%H:%M:%SZ")

                            def _sync_run():
                                from backtest import BacktestEngine, write_recommendations
                                engine = BacktestEngine()
                                result = engine.run(start_time=_start_iso, end_time=_end_iso)
                                write_recommendations(result, config.DATA_DIR)
                                logger.info(
                                    "Nightly backtest complete: %d trades, %.1f%% win rate",
                                    result.total_trades, result.win_rate * 100,
                                )
                            await loop.run_in_executor(None, _sync_run)
                        except Exception:
                            logger.exception("Nightly backtest failed")
                    asyncio.ensure_future(_run_nightly_backtest())
                    _last_backtest_run = _now

                # Weekly rebalance digest (MONITOR-04)
                nonlocal _last_rebalance_digest
                if _now - _last_rebalance_digest >= config.REBALANCE_DIGEST_INTERVAL:
                    try:
                        from dashboard import state as _ds
                        balances = getattr(_ds, "platform_balances", {})
                        opp_flow = getattr(_ds, "platform_opp_flow", {})
                        total = sum(v for v in balances.values() if isinstance(v, (int, float)))
                        if total > 0 and notifier:
                            total_opps = sum(opp_flow.values()) or 1
                            lines = ["Weekly Rebalance Digest:"]
                            for plat in sorted(balances.keys()):
                                bal = balances.get(plat, 0)
                                opps = opp_flow.get(plat, 0)
                                cur_pct = bal / total * 100
                                rec_pct = opps / total_opps * 100
                                lines.append(
                                    "  %s: $%.0f (%.0f%%) -> rec %.0f%%" % (
                                        plat, bal, cur_pct, rec_pct)
                                )
                            if hasattr(notifier, "notify_text"):
                                notifier.notify_text("\n".join(lines))
                            logger.info("Weekly rebalance digest sent.")
                    except Exception as exc:
                        logger.debug("Rebalance digest failed: %s", exc)
                    _last_rebalance_digest = _now

                # PR E: Auto-correlation tracker refresh (default 24h)
                nonlocal _last_correlation_tracker_run
                if (
                    config.CORRELATION_AUTO_DETECT_ENABLED
                    and _now - _last_correlation_tracker_run
                        >= config.CORRELATION_TRACKER_INTERVAL
                ):
                    async def _run_correlation_tracker():
                        try:
                            loop = asyncio.get_event_loop()

                            def _sync_run():
                                from snapshot import SnapshotRecorder
                                from correlation_tracker import (
                                    run_correlation_tracker,
                                )
                                rec = SnapshotRecorder()
                                try:
                                    n = run_correlation_tracker(rec)
                                    logger.info(
                                        "correlation_tracker: cached %d "
                                        "auto-correlated pairs", n,
                                    )
                                finally:
                                    rec.close()
                            await loop.run_in_executor(None, _sync_run)
                        except Exception:
                            logger.exception("correlation_tracker run failed")
                    asyncio.ensure_future(_run_correlation_tracker())
                    _last_correlation_tracker_run = _now

                # MON-03: Per-strategy zero-opportunity period detection (30-minute windows)
                if _alert_manager:
                    try:
                        # Count opportunities per strategy
                        strategy_opp_counts: dict[str, int] = {}
                        for opp in all_opportunities:
                            strategy_type = opp.get("type", "unknown")
                            strategy_opp_counts[strategy_type] = strategy_opp_counts.get(strategy_type, 0) + 1

                        # Check per-strategy zero-opp periods (30-min idle detection)
                        _alert_manager.check_zero_opp_period_per_strategy(strategy_opp_counts)

                        # Record strategy opportunities for tracking
                        for strategy_type in strategy_opp_counts:
                            _alert_manager.record_strategy_opportunity(strategy_type)

                        logger.debug(
                            "Scan cycle: %d opportunities across %d strategies",
                            len(all_opportunities),
                            len(strategy_opp_counts),
                        )
                    except Exception as e:
                        logger.warning("Error in strategy opportunity detection: %s", str(e))

                # Zero-opportunity anomaly detection (MONITOR-03 - overall period)
                if _alert_manager:
                    try:
                        _alert_manager.check_zero_opp_period(len(all_opportunities))
                    except Exception:
                        pass

                # Rebuild opportunity index for WS-triggered execution
                opp_index.rebuild(all_opportunities)

                # Subscribe to WebSocket feeds for discovered markets.
                # We subscribe to opportunity tokens AND also to broader
                # market tokens from cross-platform matched pairs so we
                # can detect arbs that appear between scan cycles.
                poly_sub_ids, kalshi_sub_tickers = opp_index.get_subscription_tokens(ws_sub_limit)

                # Broaden subscriptions: include top cross-platform matched
                # Kalshi tickers and Polymarket tokens even if no arb exists
                # yet.  This lets the WS trigger fire when prices move into
                # profitable range between polling scans.
                if poly_markets:
                    import json as _json
                    for pm in poly_markets[:ws_sub_limit]:
                        raw = pm.get("clobTokenIds")
                        if not raw:
                            continue
                        try:
                            tids = _json.loads(raw) if isinstance(raw, str) else raw
                        except Exception:
                            continue
                        if isinstance(tids, list):
                            for tid in tids:
                                if tid and tid not in poly_sub_ids:
                                    poly_sub_ids.append(tid)
                        if len(poly_sub_ids) >= ws_sub_limit:
                            break

                # Extract individual market tickers from Kalshi data.
                # kalshi_data is (events, markets_by_event, event_titles).
                # WS needs market tickers (e.g. KXBTCD-...), not event tickers.
                if kalshi_data and len(kalshi_data) >= 2 and kalshi_data[1]:
                    for _evt_ticker, _markets in kalshi_data[1].items():
                        for km in _markets:
                            kt = km.get("ticker", "")
                            if kt and kt not in kalshi_sub_tickers:
                                kalshi_sub_tickers.append(kt)
                            if len(kalshi_sub_tickers) >= ws_sub_limit:
                                break
                        if len(kalshi_sub_tickers) >= ws_sub_limit:
                            break

                if scan_count == 1 and not ws_task:
                    feed_manager.subscribe_polymarket(poly_sub_ids)
                    if kalshi_client:
                        feed_manager.subscribe_kalshi(kalshi_sub_tickers)
                    ws_task = asyncio.create_task(feed_manager.run())
                    logger.info(
                        "WS feeds started: %d Polymarket tokens, %d Kalshi tickers",
                        len(poly_sub_ids), len(kalshi_sub_tickers),
                    )
                elif ws_task and not ws_task.done():
                    feed_manager.update_subscriptions(
                        poly_token_ids=poly_sub_ids,
                        kalshi_tickers=kalshi_sub_tickers,
                    )

            except Exception as e:
                import traceback
                logger.error("Scan failed: %s\n%s", e, traceback.format_exc())
                if _metrics:
                    _metrics.inc("scans_total", {"status": "failed"})

            # Stage timing summary — sorted desc by elapsed so the bottleneck is first.
            # Read this in production logs to identify which stage is dominating
            # the scan cycle (target: total <2 min for arb-quality reaction time).
            try:
                _scan_total = time.time() - _scan_start
                _stage_timings.setdefault("display_exec", time.time() - _stage_display_start)
                logger.info(
                    "Scan #%d stage timings — %s",
                    scan_count,
                    _format_stage_timings(_stage_timings, _scan_total),
                )
            except Exception:
                # Never let instrumentation break the scan loop
                logger.exception("Stage-timing summary failed (non-fatal)")

            # Wait for next scan interval or shutdown
            logger.info("Next scan in %ds (Ctrl+C to stop)...", rescan_interval)
            try:
                await asyncio.wait_for(shutdown_event.wait(), timeout=rescan_interval)
            except asyncio.TimeoutError:
                pass

        # Cleanup
        logger.info("Stopping WebSocket feeds...")
        feed_manager.stop()
        if ws_task:
            ws_task.cancel()
            try:
                await ws_task
            except (asyncio.CancelledError, Exception):
                pass
        if priority_consumer_task:
            priority_consumer_task.cancel()
            try:
                await priority_consumer_task
            except (asyncio.CancelledError, Exception):
                pass
        if stale_monitor_task:
            stale_monitor_task.cancel()
            try:
                await stale_monitor_task
            except (asyncio.CancelledError, Exception):
                pass
        if health_monitor_task:
            health_monitor_task.cancel()
            try:
                await health_monitor_task
            except (asyncio.CancelledError, Exception):
                pass
        logger.info("Shutdown complete.")

    asyncio.run(_continuous_loop())
