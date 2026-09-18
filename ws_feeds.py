"""WebSocket real-time price feeds for Polymarket, Kalshi, and Betfair."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import random
import ssl
import threading
import time
from typing import Callable

logger = logging.getLogger(__name__)

# Conditional metrics import — never breaks if metrics.py is missing
try:
    from config import METRICS_ENABLED as _METRICS_ENABLED
    if _METRICS_ENABLED:
        from metrics import metrics as _ws_metrics
    else:
        _ws_metrics = None
except Exception:
    _ws_metrics = None

import websockets

from kalshi_api import _sign_pss, _load_private_key, _load_private_key_from_base64

# Proxy support for WebSocket connections
try:
    from python_socks.async_.asyncio import Proxy
    _SOCKS_AVAILABLE = True
except ImportError:
    _SOCKS_AVAILABLE = False

# WebSocket endpoints
KALSHI_WS_URL = "wss://api.elections.kalshi.com/trade-api/ws/v2"
POLYMARKET_WS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"

RECONNECT_DELAY = 5  # initial seconds before reconnect attempt
RECONNECT_MAX_DELAY = 60  # maximum backoff delay
# Bound a single Betfair stream message (audit S13): cap the StreamReader buffer
# so a server that never sends CRLF can't grow it unboundedly toward OOM.
_STREAM_LIMIT = 4 * 1024 * 1024  # 4 MiB
KEEPALIVE_INTERVAL = 10  # seconds between pings


class FeedManager:
    """Manages WebSocket connections to both platforms for real-time price feeds."""

    def __init__(
        self,
        on_price_update: Callable[[str, str, dict], None],
        kalshi_api_key_id: str | None = None,
        kalshi_private_key_path: str | None = None,
        kalshi_private_key_base64: str | None = None,
        betfair_app_key: str | None = None,
        betfair_session_token: str | None = None,
        price_cache: dict | None = None,
    ):
        """
        Args:
            on_price_update: Callback(platform, ticker/token_id, price_data)
            kalshi_api_key_id: Kalshi API key ID for WS auth
            kalshi_private_key_path: Path to Kalshi RSA private key PEM file
            kalshi_private_key_base64: Base64-encoded RSA private key (alternative to path)
            betfair_app_key: Betfair API application key for stream auth
            betfair_session_token: Betfair SSO session token (ssoid)
            price_cache: Optional shared dict for marking stale prices (keyed by (platform, ticker))
        """
        self.on_price_update = on_price_update
        self._price_cache = price_cache or {}
        self._price_cache_lock = threading.Lock()
        self.kalshi_api_key_id = kalshi_api_key_id
        self.kalshi_private_key = None
        if kalshi_private_key_base64:
            try:
                self.kalshi_private_key = _load_private_key_from_base64(kalshi_private_key_base64)
            except Exception as e:
                logger.warning("Could not load Kalshi private key from base64 for WS: %s", e)
        elif kalshi_private_key_path:
            try:
                self.kalshi_private_key = _load_private_key(kalshi_private_key_path)
            except Exception as e:
                logger.warning("Could not load Kalshi private key for WS: %s", e)

        self._kalshi_tickers: list[str] = []
        self._poly_token_ids: list[str] = []
        self._betfair_market_ids: list[str] = []
        self._running = False
        self._pm_proxy = os.getenv("POLYMARKET_PROXY_URL")
        self._kalshi_proxy = os.getenv("KALSHI_PROXY_URL")
        self._pending_poly_subs: list[str] = []
        self._pending_kalshi_subs: list[str] = []
        self._pending_betfair_subs: list[str] = []
        self._kalshi_ws = None
        self._kalshi_task_started = False
        self._kalshi_late_task: asyncio.Task | None = None
        # Per-ticker Kalshi book state: {ticker: {"yes": {price_cents: qty}, "no": {...}}}.
        # Needed because orderbook_delta messages carry single-level changes, not ladders.
        self._kalshi_books: dict[str, dict[str, dict[int, float]]] = {}
        self._poly_ws = None
        self._last_message_time: dict[str, float] = {}  # platform -> timestamp

        # Betfair Stream API credentials
        self._betfair_app_key = betfair_app_key
        self._betfair_session_token = betfair_session_token
        self._betfair_feed: BetfairFeed | None = None

    def subscribe_kalshi(self, tickers: list[str]):
        """Set Kalshi market tickers to subscribe to."""
        self._kalshi_tickers = list(set(tickers))

    def subscribe_polymarket(self, token_ids: list[str]):
        """Set Polymarket CLOB token IDs to subscribe to."""
        self._poly_token_ids = list(set(token_ids))

    def subscribe_betfair(self, market_ids: list[str]):
        """Set Betfair market IDs to subscribe to via the Stream API."""
        self._betfair_market_ids = list(set(market_ids))

    def update_subscriptions(self, poly_token_ids: list[str] | None = None,
                             kalshi_tickers: list[str] | None = None,
                             betfair_market_ids: list[str] | None = None):
        """Dynamically add new subscriptions without reconnecting.

        New tokens/tickers are queued and sent on the next message loop iteration.
        """
        if poly_token_ids:
            new_poly = [t for t in poly_token_ids if t and t not in self._poly_token_ids]
            if new_poly:
                self._poly_token_ids.extend(new_poly)
                self._pending_poly_subs.extend(new_poly)
                logger.info("Queued %d new Polymarket subscriptions.", len(new_poly))

        if kalshi_tickers:
            new_kalshi = [t for t in kalshi_tickers if t and t not in self._kalshi_tickers]
            if new_kalshi:
                self._kalshi_tickers.extend(new_kalshi)
                self._pending_kalshi_subs.extend(new_kalshi)
                logger.info("Queued %d new Kalshi subscriptions.", len(new_kalshi))

        if betfair_market_ids:
            new_bf = [m for m in betfair_market_ids if m and m not in self._betfair_market_ids]
            if new_bf:
                self._betfair_market_ids.extend(new_bf)
                self._pending_betfair_subs.extend(new_bf)
                logger.info("Queued %d new Betfair subscriptions.", len(new_bf))

    def prune_subscriptions(self, active_poly_token_ids: list[str] | None = None,
                            active_kalshi_tickers: list[str] | None = None,
                            active_betfair_market_ids: list[str] | None = None):
        """Remove subscriptions for markets no longer in the active scan set.

        Call periodically (e.g. once per scan cycle) with the current set of
        active market identifiers.  Any subscription not in the active set is
        dropped — the remote WS server will stop sending data for tickers the
        client is no longer interested in on the next reconnect.

        Args:
            active_poly_token_ids: Currently active Polymarket token IDs.
            active_kalshi_tickers: Currently active Kalshi tickers.
            active_betfair_market_ids: Currently active Betfair market IDs.
        """
        pruned = 0
        if active_poly_token_ids is not None:
            active_set = set(active_poly_token_ids)
            before = len(self._poly_token_ids)
            self._poly_token_ids = [t for t in self._poly_token_ids if t in active_set]
            self._pending_poly_subs = [t for t in self._pending_poly_subs if t in active_set]
            pruned += before - len(self._poly_token_ids)

        if active_kalshi_tickers is not None:
            active_set = set(active_kalshi_tickers)
            before = len(self._kalshi_tickers)
            self._kalshi_tickers = [t for t in self._kalshi_tickers if t in active_set]
            self._pending_kalshi_subs = [t for t in self._pending_kalshi_subs if t in active_set]
            pruned += before - len(self._kalshi_tickers)

        if active_betfair_market_ids is not None:
            active_set = set(active_betfair_market_ids)
            before = len(self._betfair_market_ids)
            self._betfair_market_ids = [m for m in self._betfair_market_ids if m in active_set]
            self._pending_betfair_subs = [m for m in self._pending_betfair_subs if m in active_set]
            pruned += before - len(self._betfair_market_ids)

        if pruned:
            logger.info("Pruned %d stale WS subscriptions (%d Kalshi, %d Poly, %d Betfair remain).",
                        pruned, len(self._kalshi_tickers), len(self._poly_token_ids),
                        len(self._betfair_market_ids))

    async def run(self):
        """Run all feed connections concurrently with auto-reconnect."""
        self._running = True
        tasks = []
        if self._kalshi_tickers and self.kalshi_api_key_id and self.kalshi_private_key:
            self._kalshi_task_started = True
            tasks.append(self._run_kalshi())
        if self._poly_token_ids:
            tasks.append(self._run_polymarket())
        if (self._betfair_market_ids and self._betfair_app_key
                and self._betfair_session_token):
            tasks.append(self._run_betfair())

        if not tasks:
            logger.info("No subscriptions configured.")
            return

        logger.info(
            "Starting feeds: %d Kalshi tickers, %d Polymarket tokens, %d Betfair markets",
            len(self._kalshi_tickers), len(self._poly_token_ids),
            len(self._betfair_market_ids),
        )
        await asyncio.gather(*tasks, return_exceptions=True)

    def start_kalshi_feed_late(self) -> bool:
        """Start the Kalshi WS task after run() began without any Kalshi tickers.

        Covers the self-heal path: when Kalshi auth fails at boot, run() starts
        with zero Kalshi tickers and never spawns _run_kalshi. Once re-auth
        succeeds and tickers arrive via update_subscriptions, this spawns the
        feed task into the running loop. Idempotent; returns True only when a
        task was actually started. Must be called from the event loop thread.
        """
        if self._kalshi_task_started:
            return False
        if not (self._running and self._kalshi_tickers
                and self.kalshi_api_key_id and self.kalshi_private_key):
            return False
        self._kalshi_task_started = True
        # _connect_kalshi's initial loop subscribes everything already in
        # _kalshi_tickers; drop queued pending subs to avoid a duplicate
        # subscribe message right after connect.
        self._pending_kalshi_subs.clear()
        self._kalshi_late_task = asyncio.create_task(self._run_kalshi())
        logger.info("Kalshi WS feed started late (%d tickers) after re-auth.",
                    len(self._kalshi_tickers))
        return True

    def get_stale_feeds(self, max_silent_seconds: float = 120.0) -> list[str]:
        """Return list of platform names that have gone silent beyond threshold.

        Args:
            max_silent_seconds: Alert if no message received for this long.

        Returns:
            List of platform names (e.g. ["kalshi", "polymarket"]) that are stale.
        """
        now = time.time()
        stale = []
        for platform, last_ts in self._last_message_time.items():
            if now - last_ts > max_silent_seconds:
                stale.append(platform)
        return stale

    def mark_stale_feeds(self, stale_threshold_seconds: float = 30.0) -> None:
        """Mark prices as stale when feeds haven't sent messages in threshold seconds.

        When a feed goes silent for >= stale_threshold_seconds, all prices cached
        from that platform are marked with _stale: true. When the feed recovers
        (receives a new message), the stale flag is cleared.

        Args:
            stale_threshold_seconds: Seconds of silence before marking as stale (default 30s).
        """
        now = time.time()
        with self._price_cache_lock:
            for platform in ["polymarket", "kalshi", "betfair"]:
                last_msg_time = self._last_message_time.get(platform, now)
                is_stale = (now - last_msg_time) > stale_threshold_seconds

                # Iterate through cache and mark/clear stale flag
                stale_markets = []
                recovered_markets = []
                for (p, token), price_data in list(self._price_cache.items()):
                    if p == platform:
                        if is_stale:
                            if not price_data.get("_stale", False):
                                price_data["_stale"] = True
                                stale_markets.append(token)
                        else:
                            if price_data.get("_stale", False):
                                price_data["_stale"] = False
                                recovered_markets.append(token)

                # Log state changes
                if stale_markets:
                    logger.warning(
                        "%s feed stale (%.0fs without message, %d markets marked)",
                        platform, now - last_msg_time, len(stale_markets)
                    )
                if recovered_markets:
                    logger.info(
                        "%s feed recovered (%d markets unmarked)", platform, len(recovered_markets)
                    )

    def is_feed_healthy(self, platform: str, threshold_seconds: float = 30.0) -> bool:
        """Check if a feed has received a message within the threshold.

        Args:
            platform: Platform name (e.g. "polymarket", "kalshi").
            threshold_seconds: Healthy if message received within this many seconds.

        Returns:
            True if feed is healthy (recent message), False if stale.
        """
        last_msg_time = self._last_message_time.get(platform, 0)
        return (time.time() - last_msg_time) <= threshold_seconds

    def stop(self):
        """Signal feeds to stop."""
        self._running = False
        if self._betfair_feed:
            self._betfair_feed.stop()

    async def _run_betfair(self):
        """Maintain Betfair Stream API connection with auto-reconnect and exponential backoff."""
        from config import BETFAIR_STREAM_HOST, BETFAIR_STREAM_PORT

        cache = BetfairMarketCache()
        feed = BetfairFeed(
            app_key=self._betfair_app_key,
            session_token=self._betfair_session_token,
            market_ids=list(self._betfair_market_ids),
            on_price_update=self.on_price_update,
            cache=cache,
            host=BETFAIR_STREAM_HOST,
            port=BETFAIR_STREAM_PORT,
        )
        self._betfair_feed = feed

        delay = RECONNECT_DELAY
        while self._running:
            try:
                await feed.connect()
                delay = RECONNECT_DELAY
            except Exception as e:
                if self._running:
                    jittered = delay * (0.5 + random.random() * 0.5)
                    logger.warning("Betfair stream error: %s. Reconnecting in %.1fs...", e, jittered)
                    await asyncio.sleep(jittered)
                    delay = min(delay * 2, RECONNECT_MAX_DELAY)

            # Pick up any dynamically queued betfair subs
            while self._pending_betfair_subs:
                mid = self._pending_betfair_subs.pop(0)
                feed.add_market_ids([mid])

        self._betfair_feed = None

    async def _run_kalshi(self):
        """Maintain Kalshi WebSocket connection with auto-reconnect and exponential backoff."""
        delay = RECONNECT_DELAY
        while self._running:
            try:
                if _ws_metrics:
                    _ws_metrics.set("ws_connected", {"platform": "kalshi"}, value=1)
                await self._connect_kalshi()
                delay = RECONNECT_DELAY  # Reset backoff on successful connection
            except Exception as e:
                if self._running:
                    if _ws_metrics:
                        _ws_metrics.inc("ws_reconnections", {"platform": "kalshi"})
                        _ws_metrics.set("ws_connected", {"platform": "kalshi"}, value=0)
                    jittered = delay * (0.5 + random.random() * 0.5)
                    logger.warning("Kalshi connection error: %s. Reconnecting in %.1fs...", e, jittered)
                    await asyncio.sleep(jittered)
                    delay = min(delay * 2, RECONNECT_MAX_DELAY)

    async def _connect_kalshi(self):
        """Connect to Kalshi WebSocket and subscribe to tickers."""
        # Build auth headers for WS connection
        # Per Kalshi docs, the WS signature signs: timestamp + "GET" + "/trade-api/ws/v2"
        # Note: this is NOT the REST API path (/trade-api/v2), it's the WS path.
        import datetime
        timestamp_ms = str(int(datetime.datetime.now().timestamp() * 1000))
        msg = timestamp_ms + "GET" + "/trade-api/ws/v2"
        signature = _sign_pss(self.kalshi_private_key, msg)

        headers = {
            "KALSHI-ACCESS-KEY": self.kalshi_api_key_id,
            "KALSHI-ACCESS-TIMESTAMP": timestamp_ms,
            "KALSHI-ACCESS-SIGNATURE": signature,
            "Content-Type": "application/json",
        }

        # Use proxy if configured
        connect_kwargs = {"additional_headers": headers}
        if self._kalshi_proxy and _SOCKS_AVAILABLE:
            proxy = Proxy.from_url(self._kalshi_proxy)
            sock = await proxy.connect(dest_host="api.elections.kalshi.com", dest_port=443)
            connect_kwargs["sock"] = sock

        async with websockets.connect(KALSHI_WS_URL, **connect_kwargs) as ws:
            logger.info("Kalshi connected. Subscribing to %d tickers...", len(self._kalshi_tickers))
            self._reset_kalshi_books()

            # Subscribe to orderbook updates for each ticker
            for ticker in self._kalshi_tickers:
                sub_msg = {
                    "id": 1,
                    "cmd": "subscribe",
                    "params": {
                        "channels": ["orderbook_delta"],
                        "market_tickers": [ticker],
                    },
                }
                await ws.send(json.dumps(sub_msg))

            self._kalshi_ws = ws

            # Read messages with keepalive
            while self._running:
                # Send any pending subscriptions
                while self._pending_kalshi_subs:
                    ticker = self._pending_kalshi_subs.pop(0)
                    sub_msg = {
                        "id": 1,
                        "cmd": "subscribe",
                        "params": {
                            "channels": ["orderbook_delta"],
                            "market_tickers": [ticker],
                        },
                    }
                    await ws.send(json.dumps(sub_msg))

                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=KEEPALIVE_INTERVAL)
                    data = json.loads(raw)
                    self._handle_kalshi_message(data)
                except asyncio.TimeoutError:
                    # Send ping to keep connection alive
                    try:
                        await ws.ping()
                    except Exception as e:
                        logger.debug("Kalshi ping failed: %s", e)
                        break

            self._kalshi_ws = None

    def _reset_kalshi_books(self):
        """Drop all cached Kalshi book state.

        Called on (re)connect: after a connection gap the cached ladders are
        stale, and deltas must not be applied until a fresh snapshot arrives.
        """
        self._kalshi_books.clear()

    def _handle_kalshi_message(self, data: dict):
        """Process a Kalshi WebSocket message.

        Normalises orderbook snapshots/deltas into a scan-friendly format
        with ``yes_ask``, ``no_ask``, ``yes_ask_size``, ``no_ask_size`` etc.
        so the scan modules can consume cached prices directly.
        """
        if _ws_metrics:
            _ws_metrics.inc("ws_messages_received", {"platform": "kalshi"})
        msg_type = data.get("type", "")
        if msg_type == "orderbook_snapshot" or msg_type == "orderbook_delta":
            msg = data.get("msg", {})
            ticker = msg.get("market_ticker", "")
            if not ticker:
                return

            # Kalshi WS "yes"/"no" ladders are BID ladders ([price_cents, qty],
            # ascending); the executable ask for one side is 100c minus the best
            # bid on the opposite side. Deltas carry a single (side, price, delta)
            # change, so a per-ticker book is maintained across messages.
            book = self._kalshi_books.get(ticker)
            if msg_type == "orderbook_snapshot":
                book = {"yes": {}, "no": {}}
                self._kalshi_books[ticker] = book
                for side in ("yes", "no"):
                    ladder = msg.get(side) or []
                    book[side] = {
                        int(level[0]): level[1]
                        for level in ladder
                        if isinstance(level, (list, tuple)) and len(level) >= 2
                    }
            elif book is not None:
                side = msg.get("side")
                price = msg.get("price")
                delta = msg.get("delta")
                if side in ("yes", "no") and isinstance(price, (int, float)) and isinstance(delta, (int, float)):
                    levels = book[side]
                    qty = levels.get(int(price), 0) + delta
                    if qty > 0:
                        levels[int(price)] = qty
                    else:
                        levels.pop(int(price), None)

            normalised = dict(msg)  # keep raw fields for backward compat
            for side, opposite in (("yes", "no"), ("no", "yes")):
                # A delta before the ticker's snapshot leaves book=None: the
                # ladder is unknown, so publish no executable prices at all.
                opposite_levels = book[opposite] if book is not None else {}
                if opposite_levels:
                    best_bid = max(opposite_levels)
                    normalised[f"{side}_ask"] = (100 - best_bid) / 100.0
                    normalised[f"{side}_ask_size"] = opposite_levels[best_bid]
                else:
                    normalised[f"{side}_ask"] = None
                    normalised[f"{side}_ask_size"] = 0

            self._last_message_time["kalshi"] = time.time()
            self.on_price_update("kalshi", ticker, normalised)

    async def _run_polymarket(self):
        """Maintain Polymarket WebSocket connection with auto-reconnect and exponential backoff."""
        delay = RECONNECT_DELAY
        while self._running:
            try:
                if _ws_metrics:
                    _ws_metrics.set("ws_connected", {"platform": "polymarket"}, value=1)
                await self._connect_polymarket()
                delay = RECONNECT_DELAY  # Reset backoff on successful connection
            except Exception as e:
                if self._running:
                    if _ws_metrics:
                        _ws_metrics.inc("ws_reconnections", {"platform": "polymarket"})
                        _ws_metrics.set("ws_connected", {"platform": "polymarket"}, value=0)
                    jittered = delay * (0.5 + random.random() * 0.5)
                    logger.warning("Polymarket connection error: %s. Reconnecting in %.1fs...", e, jittered)
                    await asyncio.sleep(jittered)
                    delay = min(delay * 2, RECONNECT_MAX_DELAY)

    async def _connect_polymarket(self):
        """Connect to Polymarket CLOB WebSocket and subscribe to markets.

        Per Polymarket docs the first subscription uses ``{"type": "market"}``
        format.  Subsequent dynamic subscriptions use
        ``{"operation": "subscribe"}`` format.  Heartbeat is the literal
        string ``PING`` sent every ~10 s (server replies ``PONG``).
        """
        # Use proxy if configured
        connect_kwargs = {}
        if self._pm_proxy and _SOCKS_AVAILABLE:
            proxy = Proxy.from_url(self._pm_proxy)
            sock = await proxy.connect(dest_host="ws-subscriptions-clob.polymarket.com", dest_port=443)
            connect_kwargs["sock"] = sock

        async with websockets.connect(POLYMARKET_WS_URL, **connect_kwargs) as ws:
            logger.info("Polymarket connected. Subscribing to %d tokens...", len(self._poly_token_ids))

            # ---- Initial subscription (first batch uses "type": "market") ----
            batch_size = 100
            first_batch = True
            for i in range(0, len(self._poly_token_ids), batch_size):
                batch = self._poly_token_ids[i:i + batch_size]
                if first_batch:
                    sub_msg = {
                        "assets_ids": batch,
                        "type": "market",
                        "custom_feature_enabled": True,
                    }
                    first_batch = False
                else:
                    # Subsequent batches use the dynamic subscribe format
                    sub_msg = {
                        "assets_ids": batch,
                        "operation": "subscribe",
                        "custom_feature_enabled": True,
                    }
                await ws.send(json.dumps(sub_msg))
                # Small delay between batches to avoid server rejection
                if i + batch_size < len(self._poly_token_ids):
                    await asyncio.sleep(0.5)

            logger.info("Polymarket subscription sent (%d batches).",
                        (len(self._poly_token_ids) + batch_size - 1) // batch_size)
            self._poly_ws = ws

            while self._running:
                # Send any pending dynamic subscriptions (always use "operation")
                if self._pending_poly_subs:
                    pending = list(self._pending_poly_subs)
                    self._pending_poly_subs.clear()
                    for i in range(0, len(pending), batch_size):
                        batch = pending[i:i + batch_size]
                        sub_msg = {
                            "assets_ids": batch,
                            "operation": "subscribe",
                            "custom_feature_enabled": True,
                        }
                        await ws.send(json.dumps(sub_msg))
                        await asyncio.sleep(0.5)

                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=KEEPALIVE_INTERVAL)
                    # Server responds to PING with PONG (literal strings)
                    if raw == "PONG":
                        continue
                    data = json.loads(raw)
                    self._handle_polymarket_message(data)
                except asyncio.TimeoutError:
                    # Polymarket requires literal "PING" string heartbeat
                    try:
                        await ws.send("PING")
                    except Exception as e:
                        logger.debug("Polymarket PING failed: %s", e)
                        break

            self._poly_ws = None

    def _handle_polymarket_message(self, data: dict | list):
        """Process a Polymarket WebSocket message.

        Normalises ``book``, ``price_change``, and ``best_bid_ask`` events
        into a scan-friendly cache entry with ``best_bid``, ``best_ask``,
        ``best_bid_size``, and ``best_ask_size`` fields so CLOB refinement
        can skip REST fetches when fresh WS data is available.
        """
        self._last_message_time["polymarket"] = time.time()
        if _ws_metrics:
            _ws_metrics.inc("ws_messages_received", {"platform": "polymarket"})
        # Polymarket sends arrays of events
        events = data if isinstance(data, list) else [data]
        for event in events:
            event_type = event.get("event_type", "")
            asset_id = event.get("asset_id", "")
            if not asset_id:
                # price_change events nest data inside price_changes array
                if event_type == "price_change":
                    for pc in event.get("price_changes", []):
                        aid = pc.get("asset_id", "")
                        if not aid:
                            continue
                        normalised = dict(pc)
                        normalised["event_type"] = "price_change"
                        # price_change events include best_bid / best_ask
                        try:
                            bb = pc.get("best_bid")
                            ba = pc.get("best_ask")
                            if bb is not None:
                                normalised["best_bid"] = float(bb)
                            if ba is not None:
                                normalised["best_ask"] = float(ba)
                        except (ValueError, TypeError):
                            pass
                        self.on_price_update("polymarket", aid, normalised)
                continue

            if event_type == "book":
                # Full order book snapshot — extract best bid/ask + size
                normalised = {"event_type": "book", "asset_id": asset_id}
                asks = event.get("asks", [])
                bids = event.get("bids", [])
                if asks and isinstance(asks, list):
                    try:
                        normalised["best_ask"] = float(asks[0].get("price", 0))
                        normalised["best_ask_size"] = float(asks[0].get("size", 0))
                    except (ValueError, TypeError, IndexError):
                        pass
                if bids and isinstance(bids, list):
                    try:
                        normalised["best_bid"] = float(bids[0].get("price", 0))
                        normalised["best_bid_size"] = float(bids[0].get("size", 0))
                    except (ValueError, TypeError, IndexError):
                        pass
                self.on_price_update("polymarket", asset_id, normalised)
            elif event_type == "best_bid_ask":
                # Direct best bid/ask update (requires custom_feature_enabled)
                normalised = {"event_type": "best_bid_ask", "asset_id": asset_id}
                try:
                    bb = event.get("best_bid")
                    ba = event.get("best_ask")
                    if bb is not None:
                        normalised["best_bid"] = float(bb)
                    if ba is not None:
                        normalised["best_ask"] = float(ba)
                except (ValueError, TypeError):
                    pass
                self.on_price_update("polymarket", asset_id, normalised)
            else:
                # last_trade_price, tick_size_change, etc. — store as-is
                self.on_price_update("polymarket", asset_id, event)


# ---------------------------------------------------------------------------
# Betfair Exchange Streaming API
# ---------------------------------------------------------------------------


class BetfairMarketCache:
    """Thread-safe cache maintaining current Betfair market state from stream deltas.

    The Betfair Stream API sends a full image (img=True) followed by incremental
    deltas.  This cache merges both into the current best-price state for each
    (market_id, selection_id) and provides probability-converted prices that are
    compatible with the scanner's ``on_price_update`` callback format.

    Price ladder structure from Betfair:
        batb = [[level, price, volume], ...]  (best available to back)
        batl = [[level, price, volume], ...]  (best available to lay)

    Conversion to prediction-market probabilities:
        yes_price = 1 / back_odds
        no_price  = 1 - (1 / lay_odds)
    """

    def __init__(self):
        self._lock = threading.Lock()
        # {market_id: {selection_id: {"batb": [[l,p,v],...], "batl": [[l,p,v],...], "ltp": float}}}
        self._markets: dict[str, dict[int, dict]] = {}
        self.clk: str | None = None
        self.initial_clk: str | None = None

    def apply_market_change(self, mc: dict):
        """Merge a single market change message into the cache.

        Args:
            mc: A market change dict from the ``mc`` array in an ``mcm`` message.
                Contains ``id`` (market ID), optional ``img`` flag, and ``rc``
                (runner changes) with price ladder updates.
        """
        market_id = mc.get("id", "")
        if not market_id:
            return

        is_image = mc.get("img", False)

        with self._lock:
            if is_image:
                # Full image replaces existing state
                self._markets[market_id] = {}

            runners = self._markets.setdefault(market_id, {})

            for rc in mc.get("rc", []):
                sel_id = rc.get("id")
                if sel_id is None:
                    continue

                if is_image:
                    # Full replacement for this runner
                    runners[sel_id] = {
                        "batb": rc.get("batb", []),
                        "batl": rc.get("batl", []),
                        "ltp": rc.get("ltp"),
                    }
                else:
                    # Delta merge — update only provided ladder levels
                    existing = runners.setdefault(sel_id, {"batb": [], "batl": [], "ltp": None})

                    if "ltp" in rc:
                        existing["ltp"] = rc["ltp"]

                    for side in ("batb", "batl"):
                        if side in rc:
                            self._merge_ladder(existing[side], rc[side])

    @staticmethod
    def _merge_ladder(existing: list, updates: list):
        """Merge ladder level updates into existing ladder.

        Each entry is [level, price, volume].  Level 0 is best.
        A volume of 0 means the level should be removed.
        """
        level_map = {entry[0]: entry for entry in existing}
        for update in updates:
            level = update[0]
            volume = update[2] if len(update) > 2 else 0
            if volume == 0:
                level_map.pop(level, None)
            else:
                level_map[level] = update
        # Rebuild sorted by level
        existing.clear()
        existing.extend(sorted(level_map.values(), key=lambda x: x[0]))

    def get_best_prices(self, market_id: str, selection_id: int) -> dict | None:
        """Get the best back/lay prices for a runner as prediction-market probabilities.

        Args:
            market_id: Betfair market ID (e.g. "1.234567890").
            selection_id: Betfair selection (runner) ID.

        Returns:
            Dict with ``back_price``, ``lay_price``, ``yes_price``, ``no_price``,
            ``back_volume``, ``lay_volume``, and ``ltp`` — or None if not cached.
        """
        with self._lock:
            runners = self._markets.get(market_id, {})
            runner = runners.get(selection_id)
            if runner is None:
                return None

            result: dict = {
                "back_price": None,
                "lay_price": None,
                "yes_price": None,
                "no_price": None,
                "back_volume": None,
                "lay_volume": None,
                "ltp": runner.get("ltp"),
            }

            batb = runner.get("batb", [])
            batl = runner.get("batl", [])

            if batb:
                best_back = batb[0]  # level 0 = best
                odds = best_back[1]
                vol = best_back[2] if len(best_back) > 2 else 0
                if odds > 0:
                    result["back_price"] = odds
                    result["back_volume"] = vol
                    result["yes_price"] = 1.0 / odds

            if batl:
                best_lay = batl[0]
                odds = best_lay[1]
                vol = best_lay[2] if len(best_lay) > 2 else 0
                if odds > 0:
                    result["lay_price"] = odds
                    result["lay_volume"] = vol
                    result["no_price"] = 1.0 - (1.0 / odds)

            # Derive missing side if possible
            if result["yes_price"] is not None and result["no_price"] is None:
                result["no_price"] = 1.0 - result["yes_price"]
            elif result["no_price"] is not None and result["yes_price"] is None:
                result["yes_price"] = 1.0 - result["no_price"]

            return result

    def get_runners(self, market_id: str) -> dict[int, dict]:
        """Return all cached runners for a market.

        Returns:
            {selection_id: price_dict} for each runner in the market.
        """
        with self._lock:
            runners = self._markets.get(market_id, {})
            result = {}
            for sel_id in runners:
                # Release lock briefly per runner via get_best_prices (re-acquires)
                pass
        # Call outside lock to avoid deadlock
        with self._lock:
            sel_ids = list(self._markets.get(market_id, {}).keys())
        for sel_id in sel_ids:
            prices = self.get_best_prices(market_id, sel_id)
            if prices:
                result[sel_id] = prices
        return result

    def update_tokens(self, clk: str | None, initial_clk: str | None):
        """Store stream continuation tokens for reconnection.

        Args:
            clk: Current clock token from latest message.
            initial_clk: Initial clock token from subscription image.
        """
        with self._lock:
            if clk is not None:
                self.clk = clk
            if initial_clk is not None:
                self.initial_clk = initial_clk


class BetfairFeed:
    """Betfair Exchange Streaming API client using TLS TCP socket.

    Unlike the Polymarket and Kalshi feeds which use WebSocket, Betfair's
    Stream API uses a raw TLS TCP socket on stream-api.betfair.com:443 with
    CRLF-delimited JSON messages.

    Lifecycle:
        1. TLS connect to host:port
        2. Receive ``{"op": "connection", ...}`` from server
        3. Send authentication with app_key + session token
        4. Receive ``{"op": "status", "statusCode": "SUCCESS"}``
        5. Send market subscription with market IDs
        6. Receive initial image then deltas via ``mcm`` messages
    """

    def __init__(
        self,
        app_key: str,
        session_token: str,
        market_ids: list[str],
        on_price_update: Callable[[str, str, dict], None],
        cache: BetfairMarketCache,
        host: str = "stream-api.betfair.com",
        port: int = 443,
        heartbeat_ms: int = 5000,
    ):
        """
        Args:
            app_key: Betfair API application key.
            session_token: Betfair SSO session token (ssoid).
            market_ids: Initial list of market IDs to subscribe to.
            on_price_update: Callback(platform, market_id, price_data).
            cache: Shared BetfairMarketCache instance.
            host: Stream API hostname.
            port: Stream API port.
            heartbeat_ms: Server heartbeat interval in milliseconds.
        """
        self._app_key = app_key
        self._session_token = session_token
        self._market_ids = list(market_ids)
        self._on_price_update = on_price_update
        self._cache = cache
        self._host = host
        self._port = port
        self._heartbeat_ms = heartbeat_ms
        self._running = False
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._sub_id = 1
        self._pending_market_ids: list[str] = []

    def stop(self):
        """Signal the feed to stop."""
        self._running = False

    def add_market_ids(self, market_ids: list[str]):
        """Queue additional market IDs for subscription on next loop iteration."""
        new = [m for m in market_ids if m and m not in self._market_ids]
        if new:
            self._market_ids.extend(new)
            self._pending_market_ids.extend(new)

    async def connect(self):
        """Connect to the Betfair Stream API, authenticate, subscribe, and read messages."""
        self._running = True
        ssl_ctx = ssl.create_default_context()

        self._reader, self._writer = await asyncio.open_connection(
            self._host, self._port, ssl=ssl_ctx, limit=_STREAM_LIMIT,
        )
        logger.info("Betfair stream: TCP connected to %s:%d", self._host, self._port)

        try:
            # Step 1: Receive connection message
            conn_msg = await self._read_line()
            if conn_msg.get("op") != "connection":
                raise ConnectionError(
                    f"Betfair stream: expected connection op, got {conn_msg.get('op')}"
                )
            conn_id = conn_msg.get("connectionId", "unknown")
            logger.info("Betfair stream: connected (connectionId=%s)", conn_id)

            # Step 2: Authenticate
            await self._send({
                "op": "authentication",
                "appKey": self._app_key,
                "session": self._session_token,
            })

            auth_resp = await self._read_line()
            if (auth_resp.get("op") != "status"
                    or auth_resp.get("statusCode") != "SUCCESS"):
                error_msg = auth_resp.get("errorMessage", auth_resp.get("statusCode", "unknown"))
                raise ConnectionError(f"Betfair stream auth failed: {error_msg}")
            logger.info("Betfair stream: authenticated")

            # Step 3: Subscribe to markets
            if self._market_ids:
                await self._subscribe(self._market_ids)
                logger.info("Betfair stream: subscribed to %d markets", len(self._market_ids))

            # Step 4: Read messages
            # Heartbeat timeout = heartbeat_ms * 2 to allow for network jitter
            timeout = (self._heartbeat_ms / 1000.0) * 2
            while self._running:
                # Send any pending subscription updates
                if self._pending_market_ids:
                    pending = list(self._pending_market_ids)
                    self._pending_market_ids.clear()
                    await self._subscribe(self._market_ids)  # Re-subscribe with full list
                    logger.info("Betfair stream: updated subscription (%d markets total)",
                                len(self._market_ids))

                try:
                    msg = await asyncio.wait_for(self._read_line(), timeout=timeout)
                    self._handle_message(msg)
                except asyncio.TimeoutError:
                    # No message within timeout — connection may be dead
                    logger.warning("Betfair stream: heartbeat timeout, reconnecting...")
                    break
        finally:
            self._close()

    async def _send(self, msg: dict):
        """Send a JSON message terminated by CRLF."""
        if self._writer is None:
            return
        line = json.dumps(msg, separators=(",", ":")) + "\r\n"
        self._writer.write(line.encode("utf-8"))
        await self._writer.drain()

    async def _read_line(self) -> dict:
        """Read one CRLF-delimited JSON message from the stream."""
        if self._reader is None:
            raise ConnectionError("Betfair stream: not connected")
        try:
            raw = await self._reader.readuntil(b"\r\n")
        except asyncio.LimitOverrunError as exc:
            # Audit S13: a line larger than the buffer limit (e.g. a server that
            # never sends CRLF) must not grow unbounded — force a reconnect.
            raise ConnectionError(
                f"Betfair stream: message exceeded {_STREAM_LIMIT} bytes — reconnecting"
            ) from exc
        except asyncio.IncompleteReadError as exc:
            raise ConnectionError("Betfair stream: connection closed mid-message") from exc
        return json.loads(raw.strip())

    async def _subscribe(self, market_ids: list[str]):
        """Send a market subscription message.

        Uses the full market_ids list each time (Betfair replaces the
        previous subscription on re-subscribe).
        """
        self._sub_id += 1
        sub_msg = {
            "op": "marketSubscription",
            "id": self._sub_id,
            "marketFilter": {"marketIds": market_ids},
            "marketDataFilter": {
                "fields": ["EX_BEST_OFFERS", "EX_LTP"],
                "ladderLevels": 3,
            },
            "heartbeatMs": self._heartbeat_ms,
        }
        # Include continuation tokens for reconnection if available
        if self._cache.clk:
            sub_msg["clk"] = self._cache.clk
        if self._cache.initial_clk:
            sub_msg["initialClk"] = self._cache.initial_clk
        await self._send(sub_msg)

    def _handle_message(self, msg: dict):
        """Route an incoming stream message to the appropriate handler."""
        op = msg.get("op", "")
        if op == "mcm":
            self._handle_mcm(msg)
        elif op == "status":
            status = msg.get("statusCode", "")
            if status != "SUCCESS":
                logger.warning("Betfair stream status: %s — %s",
                               status, msg.get("errorMessage", ""))
        elif op == "connection":
            pass  # Already handled during connect
        else:
            logger.debug("Betfair stream: unhandled op=%s", op)

    def _handle_mcm(self, msg: dict):
        """Handle a market change message (mcm).

        Updates the cache with market/runner changes and fires the
        on_price_update callback for each affected market.
        """
        if _ws_metrics:
            _ws_metrics.inc("ws_messages_received", {"platform": "betfair"})
        # Store continuation tokens
        self._cache.update_tokens(msg.get("clk"), msg.get("initialClk"))

        for mc in msg.get("mc", []):
            market_id = mc.get("id", "")
            if not market_id:
                continue

            self._cache.apply_market_change(mc)

            # Fire callback for each runner that changed
            changed_selections = set()
            for rc in mc.get("rc", []):
                sel_id = rc.get("id")
                if sel_id is not None:
                    changed_selections.add(sel_id)

            if changed_selections:
                # Build price_data with all runners' current prices
                all_runners = self._cache.get_runners(market_id)
                price_data = {
                    "market_id": market_id,
                    "runners": {},
                }
                for sel_id, prices in all_runners.items():
                    price_data["runners"][sel_id] = prices

                self._on_price_update("betfair", market_id, price_data)

    def _close(self):
        """Close the TCP connection."""
        if self._writer:
            try:
                self._writer.close()
            except Exception as e:
                logger.debug("Error closing Betfair stream: %s", e)
        self._reader = None
        self._writer = None


# ---------------------------------------------------------------------------
# Feed Health Tracker — supports #35 (API Outage Arb) and #48 (Redundant Feeds)
# ---------------------------------------------------------------------------

class FeedHealthTracker:
    """Track health metrics across multiple data feeds for arbitrage detection.

    Provides:
    - Per-platform health status and latency tracking
    - Outage detection for #35 API Outage Arbitrage
    - Feed comparison for #48 Redundant Data Feed Arbitrage
    """

    def __init__(
        self,
        stale_threshold_seconds: float = 120.0,
        latency_window_seconds: float = 60.0,
    ):
        """Initialize the feed health tracker.

        Args:
            stale_threshold_seconds: Seconds without message to mark as outage.
            latency_window_seconds: Rolling window for latency statistics.
        """
        self._stale_threshold = stale_threshold_seconds
        self._latency_window = latency_window_seconds
        self._lock = threading.Lock()

        self._last_message_time: dict[str, float] = {}
        self._latencies: dict[str, list[tuple[float, float]]] = {}
        self._message_counts: dict[str, int] = {}
        self._outage_start: dict[str, float | None] = {}
        self._health_callbacks: list[Callable] = []

    def record_message(
        self,
        platform: str,
        latency_ms: float | None = None,
    ) -> None:
        """Record a message received from a platform feed.

        Args:
            platform: Platform name (e.g., "polymarket", "kalshi").
            latency_ms: Optional message latency in milliseconds.
        """
        now = time.time()
        fire_recovery = False
        with self._lock:
            was_in_outage = self._outage_start.get(platform) is not None
            self._last_message_time[platform] = now
            self._message_counts[platform] = self._message_counts.get(platform, 0) + 1

            if self._outage_start.get(platform) is not None:
                outage_duration = now - self._outage_start[platform]
                self._outage_start[platform] = None
                logger.info(
                    "%s feed recovered after %.1fs outage",
                    platform, outage_duration
                )

            if latency_ms is not None:
                if platform not in self._latencies:
                    self._latencies[platform] = []
                self._latencies[platform].append((now, latency_ms))
                cutoff = now - self._latency_window
                self._latencies[platform] = [
                    (ts, lat) for ts, lat in self._latencies[platform]
                    if ts > cutoff
                ]

            if was_in_outage:
                fire_recovery = True

        if fire_recovery:
            self._fire_health_change(platform, is_healthy=True)

    def check_outages(self) -> dict[str, dict]:
        """Check all platforms for outages.

        Returns:
            Dict mapping platform name to outage info:
            {platform: {"in_outage": bool, "duration_seconds": float | None}}
        """
        now = time.time()
        results = {}
        new_outages = []

        with self._lock:
            for platform in list(self._last_message_time.keys()):
                last_msg = self._last_message_time.get(platform, 0)
                silent_seconds = now - last_msg

                if silent_seconds > self._stale_threshold:
                    if self._outage_start.get(platform) is None:
                        self._outage_start[platform] = last_msg
                        logger.warning(
                            "%s feed outage detected (no message for %.0fs)",
                            platform, silent_seconds
                        )
                        new_outages.append(platform)

                    results[platform] = {
                        "in_outage": True,
                        "duration_seconds": now - self._outage_start[platform],
                        "last_message_ago": silent_seconds,
                    }
                else:
                    results[platform] = {
                        "in_outage": False,
                        "duration_seconds": None,
                        "last_message_ago": silent_seconds,
                    }

        for platform in new_outages:
            self._fire_health_change(platform, is_healthy=False)

        return results

    def get_platform_health(self, platform: str) -> dict:
        """Get health metrics for a specific platform.

        Returns:
            Dict with is_healthy, last_message_ago, avg_latency_ms,
            message_count, in_outage.
        """
        now = time.time()
        with self._lock:
            last_msg = self._last_message_time.get(platform, 0)
            silent_seconds = now - last_msg if last_msg > 0 else float("inf")

            latency_samples = self._latencies.get(platform, [])
            if latency_samples:
                avg_latency = sum(lat for _, lat in latency_samples) / len(latency_samples)
            else:
                avg_latency = None

            return {
                "is_healthy": silent_seconds <= self._stale_threshold,
                "last_message_ago": silent_seconds,
                "avg_latency_ms": avg_latency,
                "message_count": self._message_counts.get(platform, 0),
                "in_outage": self._outage_start.get(platform) is not None,
            }

    def get_fastest_feed(self, platforms: list[str]) -> str | None:
        """Return the platform with the lowest average latency.

        Args:
            platforms: List of platform names to compare.

        Returns:
            Platform name with lowest latency, or None if no data.
        """
        with self._lock:
            best_platform = None
            best_latency = float("inf")

            for platform in platforms:
                samples = self._latencies.get(platform, [])
                if not samples:
                    continue
                avg = sum(lat for _, lat in samples) / len(samples)
                if avg < best_latency:
                    best_latency = avg
                    best_platform = platform

            return best_platform

    def get_leader_feed(self, platforms: list[str]) -> str | None:
        """Identify the feed that updates first (price discovery leader).

        Used by #37 Lead-Lag MM to identify which platform to use as
        the fair value reference.

        Args:
            platforms: List of platform names to compare.

        Returns:
            Platform name that typically updates first.
        """
        with self._lock:
            recent_update = {}
            for platform in platforms:
                last = self._last_message_time.get(platform, 0)
                if last > 0:
                    recent_update[platform] = last

            if not recent_update:
                return None

            return max(recent_update.keys(), key=lambda p: recent_update[p])

    def get_outage_opportunities(
        self,
        min_outage_seconds: float = 30.0,
    ) -> list[dict]:
        """Identify platforms in outage that may have stale prices.

        Used by #35 API Outage Arbitrage.

        Args:
            min_outage_seconds: Minimum outage duration to flag.

        Returns:
            List of {platform, outage_duration, last_message_ago} dicts.
        """
        outages = self.check_outages()
        opportunities = []

        for platform, info in outages.items():
            if info["in_outage"] and info["duration_seconds"] >= min_outage_seconds:
                opportunities.append({
                    "platform": platform,
                    "outage_duration": info["duration_seconds"],
                    "last_message_ago": info["last_message_ago"],
                })

        return opportunities

    def compare_feeds(
        self,
        platforms: list[str],
    ) -> dict:
        """Compare health metrics across multiple feeds.

        Used by #48 Redundant Data Feed Arbitrage.

        Returns:
            Dict with fastest, healthiest, leader, and per-platform metrics.
        """
        metrics = {}
        healthy_platforms = []

        for platform in platforms:
            health = self.get_platform_health(platform)
            metrics[platform] = health
            if health["is_healthy"]:
                healthy_platforms.append(platform)

        return {
            "fastest": self.get_fastest_feed(healthy_platforms),
            "leader": self.get_leader_feed(healthy_platforms),
            "healthy_count": len(healthy_platforms),
            "total_count": len(platforms),
            "platforms": metrics,
        }

    def register_health_callback(
        self,
        callback: Callable[[str, bool], None],
    ) -> None:
        """Register a callback for health state changes.

        Callback receives (platform: str, is_healthy: bool).
        """
        self._health_callbacks.append(callback)

    def _fire_health_change(self, platform: str, is_healthy: bool) -> None:
        """Fire all registered health change callbacks."""
        for callback in self._health_callbacks:
            try:
                callback(platform, is_healthy)
            except Exception as e:
                logger.error("Health callback error: %s", e)


_feed_health_tracker: FeedHealthTracker | None = None


def get_feed_health_tracker() -> FeedHealthTracker:
    """Get or create the module-level FeedHealthTracker instance."""
    global _feed_health_tracker
    if _feed_health_tracker is None:
        from config import API_OUTAGE_STALE_THRESHOLD
        _feed_health_tracker = FeedHealthTracker(
            stale_threshold_seconds=API_OUTAGE_STALE_THRESHOLD,
        )
    return _feed_health_tracker
