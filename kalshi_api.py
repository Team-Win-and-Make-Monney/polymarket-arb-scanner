"""Kalshi API client with RSA-PSS API key authentication."""

import base64
import datetime
import logging
import os
import threading
import time

import requests
from requests.adapters import HTTPAdapter

logger = logging.getLogger(__name__)
from cryptography.hazmat.primitives import serialization, hashes
from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives.asymmetric import padding
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type

from config import KALSHI_RATE_LIMIT
from kalshi_policy import live_kalshi_submit_allowed
from rate_limiter import PlatformCircuitBreaker
from url_guard import assert_public_url

KALSHI_BASE_URL = "https://api.elections.kalshi.com"
KALSHI_API_PATH = "/trade-api/v2"

# Rate limiting (thread-safe)
_last_request_time = 0
_rate_lock = threading.Lock()

# HARDEN-04: circuit breaker — opens after 3 consecutive failures, resets after 30s
_circuit = PlatformCircuitBreaker("kalshi", fail_limit=3, reset_timeout=30.0)


def _rate_limit():
    global _last_request_time
    with _rate_lock:
        now = time.time()
        elapsed = now - _last_request_time
        if elapsed < KALSHI_RATE_LIMIT:
            time.sleep(KALSHI_RATE_LIMIT - elapsed)
        _last_request_time = time.time()


def _load_private_key(file_path: str):
    """Load an RSA private key from a PEM file."""
    with open(file_path, "rb") as f:
        return serialization.load_pem_private_key(
            f.read(),
            password=None,
            backend=default_backend(),
        )


def _load_private_key_from_base64(b64_string: str):
    """Load an RSA private key from a base64-encoded PEM string."""
    pem_bytes = base64.b64decode(b64_string)
    return serialization.load_pem_private_key(
        pem_bytes,
        password=None,
        backend=default_backend(),
    )


def _sign_pss(private_key, message: str) -> str:
    """Sign a message with RSA-PSS (SHA-256, salt=DIGEST_LENGTH) and return base64."""
    signature = private_key.sign(
        message.encode("utf-8"),
        padding.PSS(
            mgf=padding.MGF1(hashes.SHA256()),
            salt_length=padding.PSS.DIGEST_LENGTH,
        ),
        hashes.SHA256(),
    )
    return base64.b64encode(signature).decode("utf-8")


class _RateLimitError(Exception):
    """Raised on HTTP 429 to trigger retry."""
    pass


class KalshiPortfolioQueryError(Exception):
    """Raised by portfolio-state reads (fills/positions/orders) when the
    caller opts into ``raise_on_error=True`` and the request fails.

    A bare ``[]`` on HTTP failure is indistinguishable from "confirmed
    empty" to any caller that cannot see inside this method — that
    ambiguity is unsafe for the MM pilot's fill polling and startup
    reconciliation, which must never mistake "unknown" for "zero".
    """
    pass


class KalshiOrderIndeterminate(RuntimeError):
    """Raised when an order may have reached Kalshi but no verdict returned."""


class KalshiClient:
    """Kalshi API client with RSA-PSS API key authentication."""

    def __init__(self):
        self.session = requests.Session()
        # Proxy support
        proxy_url = os.getenv("KALSHI_PROXY_URL")
        if proxy_url:
            proxy_url = assert_public_url(proxy_url, env_name="KALSHI_PROXY_URL")
            self.session.proxies = {"http": proxy_url, "https": proxy_url}
        self.session.mount("https://", HTTPAdapter(pool_connections=1, pool_maxsize=10))
        self.api_key_id = None
        self.private_key = None

    def login_with_api_key(self, api_key_id: str, private_key_path: str | None = None, private_key_base64: str | None = None) -> bool:
        """Authenticate using API key ID + RSA private key (file path or base64).

        Provide either private_key_path (PEM file) or private_key_base64
        (base64-encoded PEM string, e.g. for container deployments).
        """
        self.api_key_id = api_key_id
        try:
            if private_key_base64:
                self.private_key = _load_private_key_from_base64(private_key_base64)
            elif private_key_path:
                self.private_key = _load_private_key(private_key_path)
            else:
                logger.error("No Kalshi private key provided (need path or base64)")
                return False
            # Verify auth works with a lightweight call
            resp = self._request("GET", "/exchange/status")
            if resp and resp.status_code == 200:
                return True
            logger.error("Kalshi auth check returned status %s", resp.status_code if resp else 'None')
            return False
        except FileNotFoundError:
            logger.error("Private key file not found: %s", private_key_path)
            return False
        except Exception as e:
            logger.error("Failed to load private key: %s", e)
            return False

    def _auth_headers(self, method: str, path: str) -> dict:
        """Generate authentication headers for a request."""
        timestamp_ms = str(int(datetime.datetime.now().timestamp() * 1000))
        # Sign: timestamp + METHOD + path (no query params)
        path_no_query = path.split("?")[0]
        msg = timestamp_ms + method.upper() + KALSHI_API_PATH + path_no_query
        signature = _sign_pss(self.private_key, msg)

        return {
            "KALSHI-ACCESS-KEY": self.api_key_id,
            "KALSHI-ACCESS-TIMESTAMP": timestamp_ms,
            "KALSHI-ACCESS-SIGNATURE": signature,
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    def _request(self, method: str, path: str, params: dict | None = None,
                 json_body: dict | None = None) -> requests.Response | None:
        """Make one logical request, counting exhausted retries once."""
        if _circuit.is_open():
            raise _RateLimitError("Circuit open -- kalshi in backoff")
        try:
            return self._request_with_retry(method, path, params=params, json_body=json_body)
        except (_RateLimitError, requests.ConnectionError, requests.Timeout):
            _circuit.record_failure()
            raise

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=1, max=10),
        retry=retry_if_exception_type((_RateLimitError, requests.ConnectionError, requests.Timeout)),
        reraise=True,
    )
    def _request_with_retry(self, method: str, path: str, params: dict | None = None,
                            json_body: dict | None = None) -> requests.Response | None:
        """Execute one retried request without double-counting attempts."""
        _rate_limit()
        headers = self._auth_headers(method, path)
        url = KALSHI_BASE_URL + KALSHI_API_PATH + path
        try:
            resp = self.session.request(
                method.upper(),
                url,
                headers=headers,
                params=params,
                json=json_body,
                timeout=30,
            )
            if resp.status_code == 429:
                raise _RateLimitError(f"Rate limited: {method} {path}")
            if 200 <= resp.status_code < 300:
                _circuit.record_success()
            else:
                _circuit.record_failure()
            return resp
        except (requests.ConnectionError, requests.Timeout) as e:
            logger.warning("Kalshi request failed (%s %s): %s", method, path, e)
            raise
        except requests.RequestException as e:
            logger.warning("Kalshi request failed (%s %s): %s", method, path, e)
            return None

    def fetch_all_events(self, limit: int = 200, max_pages: int = 50,
                         with_nested_markets: bool = True) -> list[dict]:
        """Fetch all active events from Kalshi with cursor pagination.

        When *with_nested_markets* is True (default), each event in the
        response carries a ``markets`` array — eliminating the need for
        N follow-up ``/markets?event_ticker=…`` calls. This is the
        single biggest scan-cycle latency win available without
        switching to WebSocket-driven evaluation.
        """
        all_events = []
        cursor = None

        for _ in range(max_pages):
            params = {"limit": limit, "status": "open"}
            if with_nested_markets:
                params["with_nested_markets"] = "true"
            if cursor:
                params["cursor"] = cursor

            resp = self._request("GET", "/events", params=params)
            if not resp or resp.status_code != 200:
                logger.warning("Kalshi events request failed: %s", resp.status_code if resp else 'no response')
                break

            data = resp.json()
            events = data.get("events", [])
            all_events.extend(events)

            cursor = data.get("cursor")
            if not cursor or not events:
                break

        return all_events

    def fetch_markets_for_event(self, event_ticker: str) -> list[dict]:
        """Fetch all markets for a specific event."""
        resp = self._request("GET", "/markets", params={
            "event_ticker": event_ticker,
            "limit": 100,
            "status": "open",
        })
        if not resp or resp.status_code != 200:
            return []
        return resp.json().get("markets", [])

    def fetch_market(self, ticker: str) -> dict | None:
        """Fetch a single market's current state from GET /markets/{ticker}.

        Additive, read-only, general-purpose lookup (any status — open,
        closed, settled) for any ticker. Unlike get_settlements(), which hits
        the account-scoped /portfolio/settlements and only covers markets
        this account actually traded, this works for any ticker.

        Returns:
            The market dict (includes 'status' and, once resolved, 'result')
            or None on failure/not-found.
        """
        try:
            resp = self._request("GET", f"/markets/{ticker}")
        except Exception as exc:
            logger.warning("Kalshi fetch_market failed for %s: %s", ticker, exc)
            return None
        if resp is not None and resp.status_code == 200:
            data = resp.json()
            return data.get("market", data)
        return None

    def fetch_settled_markets(self, min_close_ts: int, limit: int = 1000, max_pages: int = 50) -> list[dict]:
        """Fetch settled markets closed at/after min_close_ts, cursor-paginated.

        The discovery step of the "T-24h/T-6h candle reconstruction" method
        (docs/plans/08-earnings-mention-oos.md, T1-pm-dispersion-novelty.md
        §a): find WHAT settled since the caller's last watermark, then
        reconstruct each one's historical price separately via
        fetch_candlesticks. Mirrors fetch_all_events' pagination shape but
        hits /markets directly with status=settled (same pattern validated
        by the command-center's longshot_fade_pull.py full-history pull).

        Kalshi settles on the order of millions of markets per year across
        all categories, so min_close_ts is load-bearing: callers MUST track
        and advance their own watermark forward each run rather than
        omitting it or passing a stale/epoch value, or this will attempt to
        page through the platform's entire settlement history every call.

        Returns:
            The complete list of settled market dicts (each carries
            'result', 'ticker', 'event_ticker', 'close_time', etc.).

        Raises:
            RuntimeError: if any page request fails, or if
                max_pages is exhausted while the cursor is still live —
                never returns a silently-partial list.
        """
        out: list[dict] = []
        cursor = None
        for _ in range(max_pages):
            params: dict = {"status": "settled", "limit": limit, "min_close_ts": min_close_ts}
            if cursor:
                params["cursor"] = cursor
            try:
                resp = self._request("GET", "/markets", params=params)
            except Exception as exc:
                raise RuntimeError(
                    "Kalshi settled-markets request failed mid-pagination: "
                    f"{type(exc).__name__} ({len(out)} markets fetched so far)"
                ) from exc
            if not resp or resp.status_code != 200:
                raise RuntimeError(
                    f"Kalshi settled-markets request failed mid-pagination: "
                    f"{resp.status_code if resp else 'no response'} ({len(out)} markets fetched so far)"
                )
            data = resp.json()
            markets = data.get("markets", [])
            out.extend(markets)
            cursor = data.get("cursor")
            if not cursor:
                break
        else:
            raise RuntimeError(
                f"Kalshi settled-markets pagination did not finish within max_pages={max_pages} "
                f"({len(out)} markets fetched, cursor still live) — raise max_pages or narrow min_close_ts"
            )
        return out

    def fetch_candlesticks(self, series_ticker: str, ticker: str, start_ts: int, end_ts: int,
                           period_interval: int = 60) -> list[dict] | None:
        """Fetch candlesticks via GET /series/{series_ticker}/markets/{ticker}/candlesticks.

        Reconstructs a settled market's YES price at a specific historical
        instant (e.g. T-24h before close) after the fact — the OOS logger's
        core method, since a weekly cron cannot reliably catch every market
        live during its narrow open T-24h..T-6h window. Price fields in the
        response are ``*_dollars`` strings (e.g. ``close_dollars="0.2200"``),
        NOT bare cents — confirmed gotcha from the in-sample pilot
        (T1-pm-dispersion-novelty.md methodology note); callers must read the
        dollar fields.

        Args:
            series_ticker: The market's series ticker (NOT its event or
                market ticker — passing the wrong one 404s this endpoint).
            ticker: The market ticker.
            start_ts: Unix seconds, inclusive window start.
            end_ts: Unix seconds, inclusive window end.
            period_interval: Candle width in minutes (default 60 = hourly).

        Returns:
            The raw 'candlesticks' list — [] if the request succeeded but
            found no candles in the window (e.g. the market didn't exist
            yet), or None if the request itself failed: a non-200 response,
            or a network/timeout/rate-limit exception that _request's
            internal retry (see the @retry decorator above) re-raises once
            exhausted (reraise=True). Callers MUST distinguish [] from None
            — they mean opposite things (permanent no-data vs. transient
            failure) and are not interchangeable.
        """
        try:
            resp = self._request(
                "GET",
                f"/series/{series_ticker}/markets/{ticker}/candlesticks",
                params={"start_ts": start_ts, "end_ts": end_ts, "period_interval": period_interval},
            )
        except (requests.RequestException, _RateLimitError) as exc:
            # _request already retried transient errors internally; this is
            # only reached once those retries are exhausted, so it's a real,
            # currently-unrecoverable failure. Convert to this method's own
            # None sentinel instead of propagating tenacity's implementation
            # detail up through price_at_t24h and crashing the OOS cycle.
            logger.warning("Kalshi candlesticks request failed for %s/%s: %s", series_ticker, ticker, exc)
            return None
        if resp is not None and resp.status_code == 200:
            return resp.json().get("candlesticks", [])
        return None

    def fetch_order_book(self, ticker: str) -> dict | None:
        """Fetch order book for a given market ticker."""
        resp = self._request("GET", f"/markets/{ticker}/orderbook")
        if resp and resp.status_code == 200:
            return resp.json()
        return None

    def get_market_price(self, market: dict) -> tuple[float | None, float | None]:
        """Extract best yes/no prices from a Kalshi market.

        Returns (yes_price, no_price) in dollar terms (0-1).
        Uses dollar fields when available, falls back to cent fields.
        """
        # Prefer dollar-denominated fields
        yes_dollars = market.get("yes_ask_dollars")
        no_dollars = market.get("no_ask_dollars")

        if yes_dollars is not None and no_dollars is not None:
            try:
                yes_price = float(yes_dollars)
                no_price = float(no_dollars)
                if yes_price > 0 and no_price > 0:
                    return yes_price, no_price
            except (ValueError, TypeError):
                pass

        # Fallback to cent fields
        yes_ask = market.get("yes_ask")
        no_ask = market.get("no_ask")

        yes_price = yes_ask / 100.0 if yes_ask is not None else None
        no_price = no_ask / 100.0 if no_ask is not None else None

        if yes_price is None or no_price is None:
            return None, None

        return yes_price, no_price

    def get_balance(self) -> float | None:
        """Get account balance in dollars."""
        resp = self._request("GET", "/portfolio/balance")
        if resp is None or resp.status_code != 200:
            logger.warning("Kalshi get_balance failed: %s",
                           resp.status_code if resp is not None else "no response")
            return None
        data = resp.json()
        # Balance is returned in cents
        balance_cents = data.get("balance", 0)
        return balance_cents / 100.0

    def get_positions(self, limit: int = 200, max_pages: int = 10,
                      raise_on_error: bool = False) -> list[dict]:
        """Get open positions, walking cursor pagination across all pages.

        Codex round-2 finding: this used to fetch a single page (limit=200)
        and silently ignore the documented ``cursor`` field, exactly like
        ``get_fills``/``get_settlements``/``get_open_orders`` already
        pattern-match elsewhere in this file. An account with more than
        ``limit`` resting positions would have had a pilot-market position
        land on page 2 and never be seen — ``reconcile()`` would then mark
        itself successful off an incomplete picture. Pagination now mirrors
        ``get_open_orders``'s cursor loop exactly.

        Args:
            limit: Page size per request.
            max_pages: Maximum pages to walk (cursor pagination).
            raise_on_error: When True, ANY page fetch failure — including
                one on page 2+, after earlier pages looked fine — raises
                ``KalshiPortfolioQueryError`` instead of returning whatever
                positions were accumulated so far. A partial cross-page
                result is exactly as ambiguous as a single-page failure:
                the MM pilot's startup reconciliation (mm_pilot.py) must
                never mistake "some pages missing" for "confirmed
                complete". Default False preserves the original
                silent-partial-return behavior for other callers.

        Raises:
            KalshiPortfolioQueryError: Only when ``raise_on_error`` is True
                and a page fetch fails.
        """
        positions: list[dict] = []
        cursor = None
        for _ in range(max_pages):
            params: dict = {"limit": limit}
            if cursor:
                params["cursor"] = cursor
            resp = self._request("GET", "/portfolio/positions", params=params)
            if not resp or resp.status_code != 200:
                status = resp.status_code if resp is not None else "no response"
                logger.warning("Kalshi get_positions failed: %s", status)
                if raise_on_error:
                    raise KalshiPortfolioQueryError(
                        f"get_positions page fetch failed ({status}) after "
                        f"{len(positions)} position(s) already accumulated "
                        f"this call — result would be ambiguous (partial "
                        f"vs. complete)")
                break
            data = resp.json()
            page = data.get("market_positions", [])
            positions.extend(page)
            cursor = data.get("cursor")
            if not cursor or not page:
                break
        else:
            # Codex round-3 finding: the for-loop exhausted every
            # max_pages iteration without ever hitting a `break` above —
            # meaning every page fetch SUCCEEDED and the cursor was STILL
            # non-empty after the very last one. More data genuinely
            # exists beyond max_pages*limit; we only stopped because of
            # our own bound. This is NOT "confirmed complete" (empty
            # cursor) — silently returning `positions` here would let a
            # caller (reconcile()) mistake an incomplete fetch for a full
            # one and mark itself successfully reconciled anyway.
            if cursor:
                logger.warning(
                    "Kalshi get_positions exhausted max_pages=%d while "
                    "more data was still available (cursor non-empty) — "
                    "%d position(s) accumulated is a PARTIAL result",
                    max_pages, len(positions))
                if raise_on_error:
                    raise KalshiPortfolioQueryError(
                        f"get_positions exhausted max_pages={max_pages} "
                        f"while the cursor still had more data — "
                        f"{len(positions)} position(s) accumulated is an "
                        f"incomplete result")
        return positions

    def get_open_orders(self, ticker: str | None = None,
                        limit: int = 200, max_pages: int = 5) -> list[dict]:
        """Fetch this account's resting (unfilled) orders.

        Used by the MM pilot's startup reconciliation gate
        (docs/plans/10-mm-pilot-prep.md, restart-persistence fix): a prior
        process crash can leave live GTC orders resting on Kalshi that a
        fresh in-memory registry knows nothing about. Unlike
        ``get_positions``, this always raises on failure — there is no
        pre-existing caller relying on a silent-empty result, and a caller
        that needs this list (reconciliation) must never treat "request
        failed" as "confirmed no resting orders".

        Raises:
            KalshiPortfolioQueryError: On any page-fetch HTTP failure.
        """
        params: dict = {"status": "resting", "limit": limit}
        if ticker:
            params["ticker"] = ticker
        orders: list[dict] = []
        cursor = None
        for _ in range(max_pages):
            if cursor:
                params["cursor"] = cursor
            resp = self._request("GET", "/portfolio/orders", params=params)
            if not resp or resp.status_code != 200:
                status = resp.status_code if resp is not None else "no response"
                logger.warning("Kalshi get_open_orders failed: %s", status)
                raise KalshiPortfolioQueryError(
                    f"get_open_orders page fetch failed ({status})")
            data = resp.json()
            page = data.get("orders", [])
            orders.extend(page)
            cursor = data.get("cursor")
            if not cursor or not page:
                break
        else:
            # Codex round-3 finding: exhausted max_pages while every page
            # fetch succeeded and the cursor was STILL non-empty — more
            # resting orders genuinely exist beyond max_pages*limit. This
            # method's whole contract is "never silently return zero/
            # partial on failure"; a partial result from hitting our own
            # page bound is exactly as unsafe as an HTTP failure would be.
            if cursor:
                raise KalshiPortfolioQueryError(
                    f"get_open_orders exhausted max_pages={max_pages} "
                    f"while the cursor still had more data — "
                    f"{len(orders)} order(s) accumulated is an incomplete "
                    f"result")
        return orders

    def get_settlements(self, limit: int = 200, max_pages: int = 5) -> list[dict]:
        """Fetch account settlement history from /portfolio/settlements.

        This is the authoritative record of what actually settled and for how
        much — unlike per-market /markets/{ticker} polling, it cannot miss a
        resolution and it reflects this account's real fills. Used by
        check_settlements to reconcile open DB positions (June 2026 audit:
        per-market polling keyed on titles never settled anything; 19 stale
        positions accumulated while true P&L was −$11.18).

        Returns a list of settlement records, newest first, each including
        ticker, market_result, yes_count/no_count, revenue, settled_time.
        """
        settlements: list[dict] = []
        cursor = None
        for _ in range(max_pages):
            params: dict = {"limit": limit}
            if cursor:
                params["cursor"] = cursor
            resp = self._request("GET", "/portfolio/settlements", params=params)
            if not resp or resp.status_code != 200:
                logger.warning("Kalshi get_settlements failed: %s",
                               resp.status_code if resp is not None else "no response")
                break
            data = resp.json()
            page = data.get("settlements", [])
            settlements.extend(page)
            cursor = data.get("cursor")
            if not cursor or not page:
                break
        return settlements

    def fetch_incentive_programs(self, status: str = "active",
                                 incentive_type: str = "liquidity",
                                 max_pages: int = 50) -> list[dict]:
        """Fetch Kalshi incentive programs (the per-market LIP pool list).

        GET /incentive_programs with cursor pagination. Each program dict
        gains a normalized ``period_reward_dollars`` field — the API's
        ``period_reward`` is in centi-cents (verified live 2026-06-11:
        1150000 -> $115.00). Other fields of interest: ``market_ticker``,
        ``discount_factor_bps``, ``target_size_fp``, ``start_date``,
        ``end_date``, ``incentive_description``.

        Args:
            status: Program status filter (default "active").
            incentive_type: Program type filter (default "liquidity" = LIP).
            max_pages: Pagination safety cap (200 programs/page).

        Returns:
            List of program dicts; empty list on request failure.
        """
        programs: list[dict] = []
        cursor = None
        for _ in range(max_pages):
            params: dict = {"status": status, "type": incentive_type, "limit": 200}
            if cursor:
                params["cursor"] = cursor
            resp = self._request("GET", "/incentive_programs", params=params)
            if resp is None or resp.status_code != 200:
                logger.warning("Kalshi fetch_incentive_programs failed: %s",
                               resp.status_code if resp is not None else "no response")
                return []
            data = resp.json()
            page = data.get("incentive_programs", [])
            for p in page:
                p["period_reward_dollars"] = (p.get("period_reward") or 0) / 10000.0
            programs.extend(page)
            cursor = data.get("next_cursor")
            if not cursor:
                break
        else:
            logger.warning(
                "Kalshi fetch_incentive_programs exhausted max_pages=%d with a live cursor; "
                "discarding partial results",
                max_pages,
            )
            return []
        return programs

    def place_order(
        self,
        ticker: str,
        side: str,
        action: str,
        count: int,
        price_dollars: float,
        time_in_force: str = "fill_or_kill",
        reducing: bool = False,
    ) -> dict | None:
        """Place a limit order on Kalshi.

        Args:
            ticker: Market ticker (e.g. "KXBTC-26FEB07-T101999.99")
            side: "yes" or "no"
            action: "buy" or "sell"
            count: Number of contracts
            price_dollars: Price per contract in dollars (0.01-0.99)
            time_in_force: "fill_or_kill" (default for arb safety) or "gtc"
            reducing: Whether the order strictly reduces an existing position.

        Returns:
            Order response dict on acceptance, or None after a confirmed rejection.

        Raises:
            KalshiOrderIndeterminate: If the submission outcome cannot be confirmed.
                Callers must reconcile the order before retrying.
        """
        if not live_kalshi_submit_allowed(ticker, reducing=reducing):
            logger.warning("Kalshi place_order blocked by live policy: %s", ticker)
            return None

        body = {
            "ticker": ticker,
            "side": side,
            "action": action,
            "count": count,
            "type": "limit",
            "time_in_force": time_in_force,
        }
        # Kalshi expects price in the appropriate side field
        if side == "yes":
            body["yes_price"] = int(round(price_dollars * 100))
        else:
            body["no_price"] = int(round(price_dollars * 100))

        try:
            resp = self._request("POST", "/portfolio/orders", json_body=body)
        except Exception as e:
            logger.error("Kalshi place_order exception: %s (ticker=%s)", e, ticker)
            raise KalshiOrderIndeterminate(
                f"Kalshi order outcome is indeterminate for {ticker}"
            ) from e
        if resp is None:
            logger.warning("Kalshi place_order got no response (ticker=%s body=%s)", ticker, body)
            raise KalshiOrderIndeterminate(
                f"Kalshi order outcome is indeterminate for {ticker}: no response"
            )
        if resp.status_code in (200, 201):
            return resp.json()
        logger.error("Kalshi place_order HTTP %s: %s (ticker=%s)", resp.status_code, resp.text[:300], ticker)
        return None

    def get_fills(self, limit: int = 200, max_pages: int = 5,
                  min_ts: int | None = None,
                  raise_on_error: bool = False) -> list[dict]:
        """Fetch this account's executed trade fills from /portfolio/fills.

        Fills are the authoritative record of contracts traded (VIP volume),
        distinct from settlements which only cover resolved positions. Each
        fill includes ticker, side, action, count, yes_price/no_price (cents),
        is_taker, and created_time.

        Args:
            limit: Page size per request.
            max_pages: Maximum pages to walk (cursor pagination).
            min_ts: Optional Unix seconds lower bound; passed as ``min_ts``.
            raise_on_error: When True, a failed page fetch raises
                ``KalshiPortfolioQueryError`` instead of returning whatever
                fills were accumulated so far. A partial/empty list on HTTP
                failure is indistinguishable from "confirmed no more fills"
                to a caller that cannot see this method's internals —
                callers that must never mistake "unknown" for "zero" (e.g.
                the MM pilot's fill poll, which drives inventory/hedge
                accounting) set this True. Default False preserves the
                original silent partial-return behavior for existing
                callers (e.g. kalshi_vip.py).

        Returns:
            A list of fill records, newest first.

        Raises:
            KalshiPortfolioQueryError: Only when ``raise_on_error`` is True
                and a page fetch fails.
        """
        fills: list[dict] = []
        cursor = None
        for _ in range(max_pages):
            params: dict = {"limit": limit}
            if cursor:
                params["cursor"] = cursor
            if min_ts is not None:
                params["min_ts"] = min_ts
            resp = self._request("GET", "/portfolio/fills", params=params)
            if not resp or resp.status_code != 200:
                status = resp.status_code if resp is not None else "no response"
                logger.warning("Kalshi get_fills failed: %s", status)
                if raise_on_error:
                    raise KalshiPortfolioQueryError(
                        f"get_fills page fetch failed ({status}) after "
                        f"{len(fills)} fill(s) already accumulated this "
                        f"call — result would be ambiguous (partial vs. "
                        f"complete)")
                break
            data = resp.json()
            page = data.get("fills", [])
            fills.extend(page)
            cursor = data.get("cursor")
            if not cursor or not page:
                break
        else:
            # Codex round-3 finding: exhausted max_pages while every page
            # fetch succeeded and the cursor was STILL non-empty — more
            # fills genuinely exist beyond max_pages*limit for this
            # min_ts window. Silently returning a partial list here is
            # exactly the "unknown looks like zero/some" ambiguity
            # raise_on_error exists to close for the HTTP-failure case;
            # hitting our own page bound must be treated identically.
            if cursor:
                logger.warning(
                    "Kalshi get_fills exhausted max_pages=%d while more "
                    "fills were still available (cursor non-empty) — %d "
                    "fill(s) accumulated is a PARTIAL result", max_pages,
                    len(fills))
                if raise_on_error:
                    raise KalshiPortfolioQueryError(
                        f"get_fills exhausted max_pages={max_pages} "
                        f"while the cursor still had more data — "
                        f"{len(fills)} fill(s) accumulated is an "
                        f"incomplete result")
        return fills

    def get_order_status(self, order_id: str) -> dict | None:
        """Get the status of a specific order.

        Returns dict with order details including 'status' field, or None.
        """
        resp = self._request("GET", f"/portfolio/orders/{order_id}")
        if resp is not None and resp.status_code == 200:
            data = resp.json()
            return data.get("order", data)
        return None

    def cancel_order(self, order_id: str) -> bool:
        """Cancel an open order."""
        resp = self._request("DELETE", f"/portfolio/orders/{order_id}")
        if resp is not None and resp.status_code in (200, 204):
            return True
        logger.warning("Kalshi cancel_order failed for %s: %s", order_id,
                       resp.status_code if resp is not None else 'no response')
        return False

    def get_order_book_depth(self, ticker: str) -> dict | None:
        """Fetch order book and return depth available at the best ask on each side.

        Kalshi orderbooks are BIDS only. yes_ask_size = depth of the best NO bid
        (because a NO bid at $0.96 = a YES ask at $0.04 with the same size).
        no_ask_size = depth of the best YES bid by symmetry.

        Returns:
            {"yes_ask_size": int, "no_ask_size": int} or None on fetch failure.
        """
        book = self.fetch_order_book(ticker)
        if not book:
            return None
        # Audit BEFORE normalization so we see the raw shape Kalshi sent.
        _audit_raw_orderbook(ticker, book)
        parsed = parse_orderbook(book)
        yes_ask = best_yes_ask(parsed)  # derived from best NO bid
        no_ask = best_no_ask(parsed)    # derived from best YES bid
        return {
            "yes_ask_size": int(yes_ask[1]) if yes_ask else 0,
            "no_ask_size": int(no_ask[1]) if no_ask else 0,
        }


# ---------------------------------------------------------------------------
# Orderbook schema layer
# ---------------------------------------------------------------------------
# Single source of truth for parsing Kalshi's orderbook response. All callers
# that read orderbook prices/depth go through these helpers — no consumer
# parses the raw dict directly.
#
# Current (2026) API shape:
#   {"orderbook_fp": {"yes_dollars": [["0.0100", "33348.00"], ...],
#                     "no_dollars":  [["0.0100", "33348.00"], ...]}}
# - yes_dollars / no_dollars are BIDS only.
# - Sorted ASCENDING by price; best bid = entries[-1].
# - Prices are dollar strings ("0.4200" = $0.42).
# - Quantities are fixed-point dollar strings ("13.00" = 13 contracts).
#
# Legacy fallback (kept for resilience if Kalshi reverts):
#   {"orderbook": {"yes": [[price_cents_int, qty_int], ...],
#                  "no":  [[...], ...]}}
#
# Important: Kalshi orderbooks contain BIDS only. To compute the YES ask price
# (what you pay to BUY YES), invert the best NO bid: yes_ask = 1.0 - best_no_bid.
# Likewise no_ask = 1.0 - best_yes_bid. The four best_* helpers handle this so
# callers never have to remember which side derives from which.
# ---------------------------------------------------------------------------


def parse_orderbook(book: dict | None) -> dict:
    """Normalize a Kalshi orderbook response to a stable shape.

    Args:
        book: Raw response from GET /markets/{ticker}/orderbook, or None.

    Returns:
        {
          "yes_bids": [(price_float, qty_float), ...],  # ASCENDING
          "no_bids":  [(price_float, qty_float), ...],  # ASCENDING
        }
        Empty arrays on either side are valid. Returns empty bids on both
        sides if input is None / malformed.
    """
    empty = {"yes_bids": [], "no_bids": []}
    if not book or not isinstance(book, dict):
        return empty

    # Current schema: orderbook_fp with dollar strings
    fp = book.get("orderbook_fp")
    if isinstance(fp, dict):
        return {
            "yes_bids": _parse_dollar_entries(fp.get("yes_dollars") or []),
            "no_bids":  _parse_dollar_entries(fp.get("no_dollars") or []),
        }

    # Legacy schema: orderbook with cent integers
    legacy = book.get("orderbook")
    if isinstance(legacy, dict):
        return {
            "yes_bids": _parse_cent_entries(legacy.get("yes") or []),
            "no_bids":  _parse_cent_entries(legacy.get("no") or []),
        }

    # Some endpoints return the orderbook at the top level
    if "yes_dollars" in book or "no_dollars" in book:
        return {
            "yes_bids": _parse_dollar_entries(book.get("yes_dollars") or []),
            "no_bids":  _parse_dollar_entries(book.get("no_dollars") or []),
        }
    if "yes" in book or "no" in book:
        return {
            "yes_bids": _parse_cent_entries(book.get("yes") or []),
            "no_bids":  _parse_cent_entries(book.get("no") or []),
        }
    return empty


def _parse_dollar_entries(entries: list) -> list[tuple[float, float]]:
    """Parse [[price_str_dollars, qty_str], ...] entries into floats."""
    out = []
    for e in entries:
        try:
            if isinstance(e, list) and len(e) >= 2:
                out.append((float(e[0]), float(e[1])))
            elif isinstance(e, dict):
                price = float(e.get("price", 0))
                qty = float(e.get("quantity", e.get("size", 0)))
                out.append((price, qty))
        except (ValueError, TypeError):
            continue
    return out


def _parse_cent_entries(entries: list) -> list[tuple[float, float]]:
    """Parse legacy [[price_cents_int, qty_int], ...] entries into dollar floats."""
    out = []
    for e in entries:
        try:
            if isinstance(e, list) and len(e) >= 2:
                out.append((float(e[0]) / 100.0, float(e[1])))
            elif isinstance(e, dict):
                # Cents schema dicts may use "price" in cents
                price = float(e.get("price", 0)) / 100.0
                qty = float(e.get("quantity", e.get("size", 0)))
                out.append((price, qty))
        except (ValueError, TypeError):
            continue
    return out


def best_yes_bid(parsed: dict) -> tuple[float, float] | None:
    """Best price someone is willing to pay for YES, with depth at that price.
    For SELLING YES (e.g. a hedge), this is the price you'll receive."""
    bids = parsed.get("yes_bids") or []
    return bids[-1] if bids else None


def best_no_bid(parsed: dict) -> tuple[float, float] | None:
    """Best price someone is willing to pay for NO, with depth at that price.
    For SELLING NO, this is the price you'll receive."""
    bids = parsed.get("no_bids") or []
    return bids[-1] if bids else None


def best_yes_ask(parsed: dict) -> tuple[float, float] | None:
    """Best price someone can BUY YES at. Derived from best NO bid:
    yes_ask = 1.0 - best_no_bid_price. Depth = depth at that NO bid.
    Returns None if no NO bids exist (cannot derive)."""
    nb = best_no_bid(parsed)
    if nb is None:
        return None
    no_price, no_qty = nb
    return (1.0 - no_price, no_qty)


def best_no_ask(parsed: dict) -> tuple[float, float] | None:
    """Best price someone can BUY NO at. Derived from best YES bid:
    no_ask = 1.0 - best_yes_bid_price."""
    yb = best_yes_bid(parsed)
    if yb is None:
        return None
    yes_price, yes_qty = yb
    return (1.0 - yes_price, yes_qty)


# ---------------------------------------------------------------------------
# Defensive sort-order audit for Kalshi orderbooks
# ---------------------------------------------------------------------------
# RESOLVED 2026-04-26 via real API fetch (KXBTC-26APR2717-T87749.99,
# 33-entry no_dollars array). See tests/fixtures/kalshi_orderbook_real_sample.json.
#
#   Sort order:  ASCENDING by price.
#   Best bid:    LAST element (entries[-1]).
#   Format:      orderbook_fp.{yes_dollars,no_dollars} = [[price_str, qty_str], ...]
#                (NOT the legacy orderbook.{yes,no} with integer cents that the
#                rest of this codebase still reads — separate schema-drift bug
#                tracked outside this audit.)
#
# Audit hook retained because (a) it cost nothing and (b) it'll catch any
# future schema reversal. It self-disables after one observation per process.
# ---------------------------------------------------------------------------

_orderbook_sort_audit_logged = False


def _audit_raw_orderbook(ticker: str, book: dict) -> None:
    """Convenience: audit both sides of a raw orderbook response."""
    if not book:
        return
    fp = book.get("orderbook_fp")
    if isinstance(fp, dict):
        _audit_orderbook_sort_order(ticker, "yes_dollars", fp.get("yes_dollars") or [])
        _audit_orderbook_sort_order(ticker, "no_dollars", fp.get("no_dollars") or [])
        return
    legacy = book.get("orderbook")
    if isinstance(legacy, dict):
        _audit_orderbook_sort_order(ticker, "yes", legacy.get("yes") or [])
        _audit_orderbook_sort_order(ticker, "no", legacy.get("no") or [])


def _audit_orderbook_sort_order(ticker: str, side: str, entries: list) -> None:
    """Emit a one-time WARNING with a real multi-entry sample so we can
    verify Kalshi's bid-only / ascending-sort assumption against live data.
    Self-disables after the first multi-entry observation per process."""
    global _orderbook_sort_audit_logged
    if _orderbook_sort_audit_logged:
        return
    if not entries or len(entries) < 2:
        return
    try:
        first = entries[0]
        last = entries[-1]
        first_price = float(first[0]) if isinstance(first, list) else float(first.get("price", 0))
        last_price = float(last[0]) if isinstance(last, list) else float(last.get("price", 0))
        if first_price == last_price:
            return
        sort_dir = "ASCENDING (best=last)" if last_price > first_price else "DESCENDING (best=first)"
        logger.warning(
            "KALSHI_ORDERBOOK_SORT_AUDIT ticker=%s side=%s n=%d first=%.2f last=%.2f -> %s. "
            "If this contradicts the assumption used by callers, fix BEFORE "
            "trusting Kalshi arb revalidation or get_order_book_depth.",
            ticker, side, len(entries), first_price, last_price, sort_dir,
        )
        _orderbook_sort_audit_logged = True
    except (KeyError, ValueError, TypeError, IndexError) as e:
        logger.debug("Orderbook sort audit skipped (parse error): %s", e)


# ---------------------------------------------------------------------------
# Environment-driven construction + self-heal support
# ---------------------------------------------------------------------------

def kalshi_creds_configured() -> bool:
    """True when the env carries enough material to attempt Kalshi auth."""
    return bool(
        os.getenv("KALSHI_API_KEY_ID")
        and (os.getenv("KALSHI_PRIVATE_KEY_PATH") or os.getenv("KALSHI_PRIVATE_KEY_BASE64"))
    )


def build_client_from_env(attempts: int = 1, retry_wait: float = 0.0) -> "KalshiClient | None":
    """Construct and authenticate a KalshiClient from env credentials.

    Auth verification pings /exchange/status, which fails during Kalshi's
    daily maintenance window even with valid keys — so callers that boot at
    an unlucky time can pass attempts/retry_wait to ride it out, and the
    continuous loop re-invokes this to heal a degraded start.

    Returns None when creds are absent or every attempt fails.
    """
    if not kalshi_creds_configured():
        return None
    api_key_id = os.getenv("KALSHI_API_KEY_ID")
    key_path = os.getenv("KALSHI_PRIVATE_KEY_PATH")
    key_b64 = os.getenv("KALSHI_PRIVATE_KEY_BASE64")
    for attempt in range(1, max(1, attempts) + 1):
        client = KalshiClient()
        if key_b64:
            ok = client.login_with_api_key(api_key_id, private_key_base64=key_b64)
        else:
            ok = client.login_with_api_key(api_key_id, private_key_path=os.path.expanduser(key_path))
        if ok:
            return client
        if attempt < max(1, attempts):
            logger.warning(
                "Kalshi auth failed (attempt %d/%d) — venue may be in its "
                "maintenance window; retrying in %.0fs",
                attempt, attempts, retry_wait,
            )
            time.sleep(retry_wait)
    return None
