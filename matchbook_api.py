"""Matchbook Exchange API client for market data and trading."""

import logging
import os
import threading
import time

import requests

from config import MATCHBOOK_RATE_LIMIT
from rate_limiter import PlatformCircuitBreaker

logger = logging.getLogger(__name__)

MATCHBOOK_API_URL = "https://api.matchbook.com/edge/rest"

# Rate limiting (thread-safe)
_last_request_time = 0
_rate_lock = threading.Lock()

# HARDEN-04: circuit breaker — opens after 3 consecutive failures, resets after 30s
_circuit = PlatformCircuitBreaker("matchbook", fail_limit=3, reset_timeout=30.0)


class _RateLimitError(Exception):
    """Raised when circuit is open — prevents further requests during backoff."""


def _rate_limit():
    global _last_request_time
    with _rate_lock:
        now = time.time()
        elapsed = now - _last_request_time
        if elapsed < MATCHBOOK_RATE_LIMIT:
            time.sleep(MATCHBOOK_RATE_LIMIT - elapsed)
        _last_request_time = time.time()


class MatchbookClient:
    """Matchbook Exchange API client with session-based authentication."""

    def __init__(self):
        self.session = requests.Session()
        proxy_url = os.getenv("MATCHBOOK_PROXY_URL")
        if proxy_url:
            self.session.proxies = {"http": proxy_url, "https": proxy_url}
        self.token = None
        self.authenticated = False

    def login(self, username: str = None, password: str = None) -> bool:
        """Authenticate with Matchbook via session login.

        Args:
            username: Matchbook username (falls back to MATCHBOOK_USERNAME env var).
            password: Matchbook password (falls back to MATCHBOOK_PASSWORD env var).

        Returns:
            True if login succeeded.
        """
        username = username or os.getenv("MATCHBOOK_USERNAME")
        password = password or os.getenv("MATCHBOOK_PASSWORD")

        if not all([username, password]):
            logger.error("Matchbook credentials not provided")
            return False

        _rate_limit()
        try:
            resp = self.session.post(
                f"{MATCHBOOK_API_URL}/sessions",
                json={"username": username, "password": password},
                headers={"Accept": "application/json", "Content-Type": "application/json"},
                timeout=30,
            )
            if resp.status_code == 200:
                data = resp.json()
                token = data.get("session-token")
                if token:
                    self.token = token
                    self.session.headers.update({
                        "session-token": self.token,
                        "Accept": "application/json",
                        "Content-Type": "application/json",
                    })
                    self.authenticated = True
                    return True
                logger.error("Matchbook login failed: no session-token in response")
                return False
            logger.error("Matchbook login returned status %s", resp.status_code)
            return False
        except requests.RequestException as e:
            logger.error("Matchbook login request failed: %s", e)
            return False

    def _request(self, method: str, endpoint: str, params: dict = None,
                 json_data: dict = None) -> dict | None:
        """Make an authenticated API request.

        Args:
            method: HTTP method (GET, POST, DELETE, etc.).
            endpoint: API path relative to base URL.
            params: Query parameters.
            json_data: JSON body payload.

        Returns:
            Response JSON or None on failure.
        """
        if not self.authenticated:
            logger.error("Matchbook: must login before making API calls")
            return None

        if _circuit.is_open():
            raise _RateLimitError("Circuit open -- matchbook in backoff")
        _rate_limit()
        try:
            url = f"{MATCHBOOK_API_URL}{endpoint}"
            resp = self.session.request(method, url, params=params,
                                        json=json_data, timeout=30)
            if resp.status_code in (200, 201):
                _circuit.record_success()
                return resp.json()
            logger.warning("Matchbook %s %s returned %s: %s",
                           method, endpoint, resp.status_code, resp.text[:200])
            _circuit.record_failure()
            return None
        except requests.RequestException as exc:
            logger.warning("Matchbook %s %s failed: %s", method, endpoint, exc)
            _circuit.record_failure()
            return None

    def fetch_all_events(self) -> list[dict]:
        """Fetch all open prediction market events from Matchbook.

        Returns:
            List of event dicts. Handles pagination if total > per-page.
        """
        all_events = []
        offset = 0
        per_page = 500

        while True:
            data = self._request("GET", "/events", params={
                "status": "open",
                "per-page": per_page,
                "offset": offset,
                "category-id": "politics",
            })

            if not data or "events" not in data:
                break

            events = data.get("events", [])
            all_events.extend(events)

            total = data.get("total", 0)
            offset += per_page
            if offset >= total:
                break

        return all_events

    def fetch_event_markets(self, event_id) -> list[dict]:
        """Fetch markets for a specific event, including price data.

        Args:
            event_id: Matchbook event ID.

        Returns:
            List of market dicts with price data.
        """
        data = self._request("GET", f"/events/{event_id}/markets", params={
            "include-prices": "true",
            "price-depth": 1,
        })

        if data and "markets" in data:
            return data["markets"]
        return []

    def list_runners(self, market_id, event_id=None) -> list[dict]:
        """Fetch runners for a market with price data.

        Args:
            market_id: Matchbook market ID.
            event_id: Matchbook event ID (required for the API path).

        Returns:
            List of runner dicts with price data.
        """
        if not event_id:
            logger.warning("Matchbook list_runners requires event_id")
            return []

        data = self._request("GET",
                             f"/events/{event_id}/markets/{market_id}/runners",
                             params={
                                 "include-prices": "true",
                                 "price-depth": 1,
                             })
        if data and "runners" in data:
            return data["runners"]
        return []

    def get_market_price(self, market: dict) -> tuple[float | None, float | None]:
        """Extract best back/lay prices from a Matchbook market as YES/NO equivalents.

        For prediction market compatibility, back price maps to YES price
        and lay price maps to the implied NO probability.

        Matchbook uses decimal odds. Back price -> 1/odds = implied prob.

        Args:
            market: Matchbook runner dict or market dict with runners containing
                    price data (``prices`` array with ``back`` and ``lay`` entries).

        Returns:
            (yes_price, no_price) in probability terms (0-1), or (None, None).
        """
        runners = market.get("runners", [market] if "prices" in market else [])
        if not runners:
            return None, None

        runner = runners[0] if isinstance(runners, list) else runners
        prices = runner.get("prices", [])

        if not prices:
            return None, None

        # Matchbook prices array contains dicts with "side" (back/lay),
        # "odds" (decimal), and "available-amount"
        best_back_odds = None
        best_lay_odds = None

        for price_entry in prices:
            side = price_entry.get("side", "").lower()
            odds = price_entry.get("odds")
            if not odds or float(odds) <= 1.0:
                continue
            if side == "back":
                if best_back_odds is None or float(odds) > best_back_odds:
                    best_back_odds = float(odds)
            elif side == "lay":
                if best_lay_odds is None or float(odds) < best_lay_odds:
                    best_lay_odds = float(odds)

        if best_back_odds is None and best_lay_odds is None:
            return None, None

        # Convert decimal odds to implied probability (0-1)
        yes_price = None
        no_price = None

        if best_back_odds is not None and best_back_odds > 0:
            yes_price = 1.0 / best_back_odds

        if best_lay_odds is not None and best_lay_odds > 0:
            no_price = 1.0 - (1.0 / best_lay_odds)

        # If only one side available, derive the other
        if yes_price is not None and no_price is None:
            no_price = 1.0 - yes_price
        elif no_price is not None and yes_price is None:
            yes_price = 1.0 - no_price

        return yes_price, no_price

    def place_order(self, market_id, runner_id, side: str,
                    odds: float, stake: float) -> dict | None:
        """Place an order on Matchbook.

        Args:
            market_id: Matchbook market ID.
            runner_id: Runner/selection ID.
            side: 'back' or 'lay'.
            odds: Decimal odds for the offer.
            stake: Stake amount.

        Returns:
            Order response dict or None on failure.
        """
        if not self.authenticated:
            return None

        # DRY_RUN guard (audit S12): honor the flag at the client boundary so a
        # direct place_order() call can never hit the live venue when DRY_RUN=true,
        # independent of the executor's own guard.
        from config import DRY_RUN
        if DRY_RUN:
            logger.info(
                "Matchbook place_order DRY_RUN — not placed (%s runner=%s odds=%.3f stake=%.2f)",
                side, runner_id, odds, stake,
            )
            # Mirror the live success shape (id/status) so callers that read
            # resp["id"] don't get an empty order id; "DRY_RUN" is unmistakable.
            return {"id": "DRY_RUN", "status": "dry_run", "dry_run": True,
                    "market-id": market_id, "runner-id": runner_id,
                    "side": side, "odds": odds, "stake": stake}

        if _circuit.is_open():
            logger.warning("Matchbook place_order skipped: circuit open")
            return None
        _rate_limit()
        try:
            resp = self.session.post(
                f"{MATCHBOOK_API_URL}/offers",
                json={
                    "odds": odds,
                    "side": side,
                    "stake": stake,
                    "runner-id": runner_id,
                },
                timeout=30,
            )
            if resp.status_code in (200, 201):
                _circuit.record_success()
                return resp.json()
            logger.error("Matchbook place_order: %s %s",
                         resp.status_code, resp.text[:200])
            _circuit.record_failure()
            return None
        except requests.RequestException as exc:
            logger.error("Matchbook place_order failed: %s", exc)
            _circuit.record_failure()
            return None

    def get_balance(self) -> float | None:
        """Get available account balance.

        Returns:
            Balance as float or None on failure.
        """
        if not self.authenticated:
            return None
        data = self._request("GET", "/account/balance")
        if data:
            return float(data.get("balance", data.get("available-balance", 0)))
        return None

    def get_order_status(self, offer_id) -> dict | None:
        """Get status of a specific offer/order.

        Args:
            offer_id: Matchbook offer ID.

        Returns:
            Offer dict or None.
        """
        if not self.authenticated:
            return None
        return self._request("GET", f"/offers/{offer_id}")

    def get_market_status(self, event_id) -> dict | None:
        """Get event/market status for settlement detection.

        Args:
            event_id: Matchbook event ID.

        Returns:
            Event dict with status info, or None on failure.
        """
        return self._request("GET", f"/events/{event_id}")

    def cancel_order(self, offer_id) -> bool:
        """Cancel an offer/order.

        Args:
            offer_id: Matchbook offer ID.

        Returns:
            True if cancellation succeeded.
        """
        if not self.authenticated:
            return False
        from config import DRY_RUN
        if DRY_RUN:
            logger.info("Matchbook cancel_order DRY_RUN — not cancelling offer %s", offer_id)
            return True
        if _circuit.is_open():
            logger.warning("Matchbook cancel_order skipped: circuit open")
            return False
        _rate_limit()
        try:
            resp = self.session.delete(
                f"{MATCHBOOK_API_URL}/offers/{offer_id}",
                timeout=30,
            )
            if resp.status_code in (200, 204):
                _circuit.record_success()
                return True
            logger.warning("Matchbook cancel_order: %s %s",
                           resp.status_code, resp.text[:200])
            _circuit.record_failure()
            return False
        except requests.RequestException as exc:
            logger.warning("Matchbook cancel_order failed: %s", exc)
            _circuit.record_failure()
            return False

    def fetch_all_markets(self) -> list[dict]:
        """Fetch all active markets from Matchbook.

        Convenience method: fetches all events, then all markets for each event.
        Returns a flat list of market dicts, each with an attached ``_event`` parent.

        Returns:
            List of market dicts with ``_event`` parent attached.
        """
        all_markets = []

        events = self.fetch_all_events()
        if not events:
            return all_markets

        for event in events:
            event_id = event.get("id")
            if not event_id:
                continue

            markets = self.fetch_event_markets(event_id)
            for market in markets:
                market["_event"] = event
                all_markets.append(market)

        return all_markets
