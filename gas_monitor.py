"""Real-time gas price monitor for dynamic fee-aware arbitrage thresholds."""

import logging
import math
import threading
import time

import requests

from url_guard import assert_public_url

logger = logging.getLogger(__name__)


class GasMonitor:
    """Fetches real-time gas prices for Polygon for gas-aware thresholds.

    When enabled, replaces static POLYGON_GAS_ESTIMATE with real-time data
    and computes dynamic execution thresholds based on actual costs.
    """

    # Number of Polygon transactions required per platform leg
    # Polymarket trades settle on-chain; others are off-chain.
    PLATFORM_GAS_TXNS = {
        "polymarket": 1,
        "kalshi": 0,
        "betfair": 0,
        "smarkets": 0,
        "sxbet": 0,
        "matchbook": 0,
    }

    def __init__(
        self,
        polygon_rpc_url: str = None,
        cache_ttl: float = 15.0,
        safety_margin: float = 1.2,
        fallback_gas_cost: float = 0.03,
        enabled: bool = True,
    ):
        """
        Args:
            polygon_rpc_url: Polygon JSON-RPC endpoint URL. Falls back to
                POLYGON_RPC_URL env var, then to public endpoint
                "https://polygon-rpc.com".
            cache_ttl: Seconds to cache gas price (default 15).
            safety_margin: Multiplier on execution cost for threshold
                (default 1.2 = 20% safety).
            fallback_gas_cost: Dollar cost to use if RPC call fails
                (default $0.03).
            enabled: If False, always uses fallback values.
        """
        import os

        if polygon_rpc_url is not None:
            self.polygon_rpc_url = polygon_rpc_url
        else:
            self.polygon_rpc_url = os.getenv(
                "POLYGON_RPC_URL", "https://polygon-rpc.com"
            )

        # SSRF guard: the RPC endpoint is read from env and POSTed JSON-RPC; an
        # injected internal URL would let gas calls reach the internal network.
        self.polygon_rpc_url = assert_public_url(
            self.polygon_rpc_url, env_name="POLYGON_RPC_URL"
        )

        self.cache_ttl = cache_ttl
        self.safety_margin = safety_margin
        self.fallback_gas_cost = fallback_gas_cost
        self.enabled = enabled

        # Gas price cache (Gwei)
        self._gas_gwei: float | None = None
        self._gas_gwei_ts: float = 0.0
        self._gas_lock = threading.Lock()

        # MATIC price cache (USD)
        self._matic_price: float | None = None
        self._matic_price_ts: float = 0.0
        self._matic_lock = threading.Lock()
        self._matic_cache_ttl = 60.0  # Cache MATIC price for 60s

        # Default fallbacks
        self._default_gas_gwei = 30.0
        self._default_matic_price = 0.50
        self._gas_source_valid = False
        self._matic_source_valid = False

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def get_polygon_gas_gwei(self) -> float:
        """Fetch current Polygon gas price in Gwei.

        Uses a cached value if still within TTL. Thread-safe.
        Returns the default (30 Gwei) if the RPC call fails.
        """
        if not self.enabled:
            return self._default_gas_gwei

        with self._gas_lock:
            now = time.time()
            if (
                self._gas_gwei is not None
                and (now - self._gas_gwei_ts) < self.cache_ttl
            ):
                return self._gas_gwei

        # Fetch outside the lock to avoid blocking other threads
        gas_gwei = self._fetch_gas_price()

        with self._gas_lock:
            self._gas_gwei = gas_gwei
            self._gas_gwei_ts = time.time()

        return gas_gwei

    def get_polygon_gas_cost(self) -> float:
        """Convert current gas price to dollar cost per transaction.

        Formula: gas_gwei * 21000 * matic_price / 1e9

        Returns fallback_gas_cost if disabled or if all fetches fail.
        """
        if not self.enabled:
            return self.fallback_gas_cost

        try:
            gas_gwei = self.get_polygon_gas_gwei()
            matic_price = self._fetch_matic_price()
            cost = gas_gwei * 21000 * matic_price / 1e9
            return cost
        except Exception:
            logger.warning(
                "Failed to compute gas cost, using fallback $%.4f",
                self.fallback_gas_cost,
            )
            return self.fallback_gas_cost

    def get_effective_threshold(
        self, platform_a: str, platform_b: str
    ) -> float:
        """Calculate dynamic minimum profit threshold for a platform pair.

        Accounts for Polygon gas costs on each on-chain leg (platform
        trading fees are already netted out of scan net_profit by fees.py),
        multiplied by the safety margin.

        Args:
            platform_a: First platform name (e.g. "polymarket").
            platform_b: Second platform name (e.g. "kalshi").

        Returns:
            Minimum net profit (in dollars) to justify execution.
        """
        gas_cost_per_tx = self.get_polygon_gas_cost()

        # Count Polygon transactions needed for each platform leg
        txns_a = self.PLATFORM_GAS_TXNS.get(platform_a.lower(), 0)
        txns_b = self.PLATFORM_GAS_TXNS.get(platform_b.lower(), 0)
        total_gas = gas_cost_per_tx * (txns_a + txns_b)

        # Gas only: platform trading fees are already netted out of every
        # scan's net_profit by fees.py — re-adding flat estimates here
        # double-charged fees and skipped fee-netted opportunities (a $0.04
        # KalshiMulti profit lost to a phantom $0.048 "gas" threshold).
        return total_gas * self.safety_margin

    def should_execute(self, opp: dict) -> bool:
        """Check if an opportunity's profit exceeds the dynamic threshold.

        When disabled, always returns True (defer to other checks).

        Args:
            opp: Opportunity dict with at least 'net_profit' and ideally
                '_platform_a'/'_platform_b' or 'type'.

        Returns:
            True if the opportunity is worth executing given current costs.
        """
        if not self.enabled:
            return True

        try:
            net_profit = float(opp.get("net_profit", 0))
        except (TypeError, ValueError):
            return False
        if not math.isfinite(net_profit):
            return False
        platform_a, platform_b = self._infer_platforms(opp)
        gas_txns = self.PLATFORM_GAS_TXNS.get(platform_a, 0) + self.PLATFORM_GAS_TXNS.get(platform_b, 0)
        if gas_txns == 0:
            return True
        threshold = self.get_effective_threshold(platform_a, platform_b)

        if not self._gas_source_valid or not self._matic_source_valid:
            logger.warning("Gas-aware execution blocked: live gas or token-price input unavailable")
            return False

        if net_profit < threshold:
            logger.debug(
                "Opportunity below gas threshold: profit=$%.4f < threshold=$%.4f "
                "(%s vs %s)",
                net_profit,
                threshold,
                platform_a,
                platform_b,
            )
            return False

        return True

    def get_current_gas_cost(self) -> float:
        """Public getter for the current gas cost estimate (for display/logging)."""
        return self.get_polygon_gas_cost()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _fetch_gas_price(self) -> float:
        """Fetch gas price from Polygon RPC (eth_gasPrice), return in Gwei.

        Falls back to default_gas_gwei on any error.
        """
        try:
            resp = requests.post(
                self.polygon_rpc_url,
                json={
                    "jsonrpc": "2.0",
                    "method": "eth_gasPrice",
                    "params": [],
                    "id": 1,
                },
                timeout=5,
            )
            resp.raise_for_status()
            data = resp.json()
            # Result is hex string in Wei
            gas_wei = int(data["result"], 16)
            gas_gwei = gas_wei / 1e9
            if not math.isfinite(gas_gwei) or gas_gwei <= 0:
                raise ValueError("non-positive or non-finite gas price")
            self._gas_source_valid = True
            logger.debug("Polygon gas price: %.2f Gwei", gas_gwei)
            return gas_gwei
        except Exception as exc:
            self._gas_source_valid = False
            logger.warning(
                "Failed to fetch Polygon gas price: %s. Using default %.1f Gwei",
                exc,
                self._default_gas_gwei,
            )
            return self._default_gas_gwei

    def _fetch_matic_price(self) -> float:
        """Fetch MATIC/USD price from CoinGecko, cached for 60s.

        Falls back to $0.50 on any error.
        """
        with self._matic_lock:
            now = time.time()
            if (
                self._matic_price is not None
                and (now - self._matic_price_ts) < self._matic_cache_ttl
            ):
                return self._matic_price

        # Fetch outside lock
        price = self._do_fetch_matic_price()

        with self._matic_lock:
            if price is not None:
                self._matic_source_valid = True
                self._matic_price = price
                self._matic_price_ts = time.time()
                return price
            if self._matic_price is not None:
                self._matic_source_valid = False
                # Fetch failed: keep the last-good price rather than
                # poisoning the cache with the default; bump ts so a flaky
                # CoinGecko is not hammered every call.
                self._matic_price_ts = time.time()
                logger.warning(
                    "MATIC price fetch failed — reusing last-good $%.4f",
                    self._matic_price)
                return self._matic_price
            logger.warning(
                "MATIC price unavailable and no last-good value — using default $%.2f",
                self._default_matic_price)
            self._matic_price = self._default_matic_price
            self._matic_price_ts = time.time()
            self._matic_source_valid = False
            return self._default_matic_price

    def _do_fetch_matic_price(self) -> float | None:
        """Raw HTTP call to CoinGecko for the Polygon gas-token price.

        Polygon's gas token migrated MATIC→POL (Sept 2024). CoinGecko serves
        the live token as polygon-ecosystem-token; the legacy matic-network
        id intermittently returns an empty dict, which produced the
        KeyError-'usd' failures seen in prod. Query both, prefer POL.

        Returns None on failure so the caller can keep the last-good price
        instead of poisoning the cache with the default.
        """
        try:
            resp = requests.get(
                "https://api.coingecko.com/api/v3/simple/price",
                params={
                    "ids": "polygon-ecosystem-token,matic-network",
                    "vs_currencies": "usd",
                },
                timeout=5,
            )
            resp.raise_for_status()
            data = resp.json()
            for coin_id in ("polygon-ecosystem-token", "matic-network"):
                usd = (data.get(coin_id) or {}).get("usd")
                if usd is not None:
                    price = float(usd)
                    if not math.isfinite(price) or price <= 0:
                        raise ValueError("non-positive or non-finite Polygon token price")
                    logger.debug("Polygon gas-token price: $%.4f (%s)", price, coin_id)
                    return price
            logger.warning("CoinGecko returned no usd price for POL/MATIC: %s", data)
            return None
        except Exception as exc:
            logger.warning("Failed to fetch MATIC price: %s", exc)
            return None

    def _infer_platforms(self, opp: dict) -> tuple[str, str]:
        """Extract or infer platform pair from an opportunity dict.

        Checks '_platform_a'/'_platform_b' keys first, then falls back
        to inferring from the 'type' string.
        """
        platform_a = opp.get("_platform_a", "")
        platform_b = opp.get("_platform_b", "")

        if platform_a and platform_b:
            return platform_a.lower(), platform_b.lower()

        # Infer from opportunity type string
        opp_type = opp.get("type", "").lower()

        if "cross" in opp_type:
            # Cross-platform: try to identify which platforms
            if "kalshi" in opp_type:
                return "polymarket", "kalshi"
            if "betfair" in opp_type:
                return "polymarket", "betfair"
            if "smarkets" in opp_type:
                return "polymarket", "smarkets"
            if "sxbet" in opp_type or "sx" in opp_type:
                return "polymarket", "sxbet"
            # Default cross-platform assumption
            return "polymarket", "kalshi"
        elif "kalshi" in opp_type:
            return "kalshi", "kalshi"
        elif "betfair" in opp_type:
            return "betfair", "betfair"
        elif "smarkets" in opp_type:
            return "smarkets", "smarkets"
        elif "sxbet" in opp_type or "sx" in opp_type:
            return "sxbet", "sxbet"
        else:
            # Default: Polymarket internal arb
            return "polymarket", "polymarket"
