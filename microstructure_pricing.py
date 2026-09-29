"""Microstructure pricing engine — predictive volatility and Poisson hazard rate spread sizing.

Theoretical foundation:
- Avellaneda & Stoikov (2008) high-frequency market making model
- Poisson order arrival hazard intensity estimation kappa(p)
- Realized tick-level EWMA volatility tracking sigma_micro
- Asymmetric inventory reservation pricing: r(s, q) = s - q * gamma * sigma^2
- Optimal half-spread: delta^*(s, q) = (1 / gamma) * ln(1 + gamma / kappa) + 0.5 * gamma * sigma^2
"""

import math
import threading
import time
from typing import Any

logger = logging = __import__("logging").getLogger(__name__)


# ---------------------------------------------------------------------------
# Data containers
# ---------------------------------------------------------------------------


class MicrostructurePricingResult:
    """Optimal quote parameters produced by Avellaneda-Stoikov pricing engine."""

    def __init__(
        self,
        ticker: str,
        mid: float,
        reservation_price: float,
        optimal_bid: float,
        optimal_ask: float,
        half_spread: float,
        micro_volatility: float,
        hazard_kappa: float,
        arrival_intensity: float,
        volatility_regime: str,
        sizing_multiplier: float,
    ):
        self.ticker = ticker
        self.mid = mid
        self.reservation_price = reservation_price
        self.optimal_bid = optimal_bid
        self.optimal_ask = optimal_ask
        self.half_spread = half_spread
        self.micro_volatility = micro_volatility
        self.hazard_kappa = hazard_kappa
        self.arrival_intensity = arrival_intensity
        self.volatility_regime = volatility_regime
        self.sizing_multiplier = sizing_multiplier

    @property
    def micro_vol(self) -> float:
        return self.micro_volatility

    @property
    def vol_regime(self) -> str:
        return self.volatility_regime

    @property
    def hazard_rate(self) -> float:
        return self.arrival_intensity

    @property
    def intensity_decay_kappa(self) -> float:
        return self.hazard_kappa

    def to_dict(self) -> dict[str, Any]:
        return {
            "ticker": self.ticker,
            "mid": round(self.mid, 4),
            "reservation_price": round(self.reservation_price, 4),
            "optimal_bid": round(self.optimal_bid, 4),
            "optimal_ask": round(self.optimal_ask, 4),
            "half_spread": round(self.half_spread, 4),
            "micro_volatility": round(self.micro_volatility, 6),
            "hazard_kappa": round(self.hazard_kappa, 2),
            "arrival_intensity": round(self.arrival_intensity, 3),
            "volatility_regime": self.volatility_regime,
            "sizing_multiplier": round(self.sizing_multiplier, 3),
        }


# ---------------------------------------------------------------------------
# MicroVolatilityTracker
# ---------------------------------------------------------------------------


class MicroVolatilityTracker:
    """Tracks tick-level high-frequency realized volatility using EWMA variance.

    Unlike slow rolling window standard deviation, this captures immediate regime shifts
    and volatility spikes within seconds, allowing quote spreads to widen before adverse selection.
    """

    def __init__(
        self,
        halflife_seconds: float = 30.0,
        min_samples: int = 2,
        floor_vol: float = 0.005,
        ceiling_vol: float = 0.50,
        time_fn: Any = time.time,
    ):
        self.halflife_seconds = max(1.0, float(halflife_seconds))
        self.min_samples = max(2, int(min_samples))
        self.floor_vol = floor_vol
        self.ceiling_vol = ceiling_vol
        self._time_fn = time_fn

        # Per-ticker state:
        # ticker -> {"last_price": float, "last_ts": float, "ewma_var": float, "samples": int, "high": float, "low": float}
        self._state: dict[str, dict[str, Any]] = {}
        self._lock = threading.Lock()

    def record_price(self, ticker: str, price: float, timestamp: float | None = None) -> float:
        """Record a price update and update EWMA variance.

        Args:
            ticker: Market ticker.
            price: Current mid price (0.01 - 0.99).
            timestamp: Optional timestamp (defaults to time_fn).

        Returns:
            Current realized volatility estimate sigma.
        """
        if not ticker or price <= 0.0:
            return self.floor_vol

        now = timestamp if timestamp is not None else self._time_fn()

        with self._lock:
            state = self._state.get(ticker)
            if state is None:
                self._state[ticker] = {
                    "last_price": price,
                    "last_ts": now,
                    "ewma_var": self.floor_vol ** 2,
                    "samples": 1,
                    "high": price,
                    "low": price,
                    "window_start": now,
                }
                return self.floor_vol

            dt = max(0.001, now - state["last_ts"])
            last_p = state["last_price"]

            # Log return calculation
            if last_p > 0.0 and price > 0.0:
                ret = math.log(price / last_p)
                ret_sq = ret * ret
            else:
                ret_sq = 0.0

            # Decay factor alpha = 1 - exp(-dt / halflife)
            alpha = 1.0 - math.exp(-dt / self.halflife_seconds)
            alpha = max(0.001, min(0.999, alpha))

            old_var = state["ewma_var"]
            new_var = alpha * ret_sq + (1.0 - alpha) * old_var

            # Reset high/low window every 60s for Parkinson reference
            if now - state["window_start"] > 60.0:
                state["high"] = price
                state["low"] = price
                state["window_start"] = now
            else:
                state["high"] = max(state["high"], price)
                state["low"] = min(state["low"], price)

            state["last_price"] = price
            state["last_ts"] = now
            state["ewma_var"] = new_var
            state["samples"] += 1

            sigma = math.sqrt(max(self.floor_vol ** 2, new_var))
            return min(self.ceiling_vol, max(self.floor_vol, sigma))

    def get_volatility(self, ticker: str) -> float:
        """Get current realized micro-volatility sigma for a ticker."""
        with self._lock:
            state = self._state.get(ticker)
            if state is None or state["samples"] < self.min_samples:
                return self.floor_vol
            sigma = math.sqrt(max(self.floor_vol ** 2, state["ewma_var"]))
            return min(self.ceiling_vol, max(self.floor_vol, sigma))

    def get_regime(self, ticker: str) -> str:
        """Classify volatility into regime: calm, normal, elevated, extreme."""
        vol = self.get_volatility(ticker)
        if vol < 0.02:
            return "calm"
        if vol < 0.06:
            return "normal"
        if vol < 0.15:
            return "elevated"
        return "extreme"

    def has_min_samples(self, ticker: str) -> bool:
        with self._lock:
            state = self._state.get(ticker)
            return state is not None and state["samples"] >= self.min_samples


# ---------------------------------------------------------------------------
# HazardRateEstimator
# ---------------------------------------------------------------------------


class HazardRateEstimator:
    """Estimates empirical Poisson order arrival hazard intensity kappa(p) and lambda.

    Models order arrival intensity as lambda(delta) = lambda_0 * exp(-kappa * delta).
    When trading is active and clustered around inside quotes, kappa is high.
    When large taker flow sweeps deep into the orderbook, kappa drops, signalling wider required spreads.
    """

    def __init__(
        self,
        window_seconds: float = 120.0,
        default_kappa: float = 75.0,
        min_trades: int = 3,
        time_fn: Any = time.time,
    ):
        self.window_seconds = max(10.0, float(window_seconds))
        self.default_kappa = max(1.0, float(default_kappa))
        self.min_trades = max(1, int(min_trades))
        self._time_fn = time_fn

        # Per-ticker list of (timestamp, distance_from_mid, count)
        self._trades: dict[str, list[tuple[float, float, int]]] = {}
        self._lock = threading.Lock()

    def record_trade(
        self,
        ticker: str,
        price: float,
        count: int,
        mid_price: float | None = None,
        timestamp: float | None = None,
    ) -> None:
        """Record an observed trade print from WebSocket stream."""
        if not ticker or count <= 0:
            return

        now = timestamp if timestamp is not None else self._time_fn()
        mid = mid_price if (mid_price is not None and mid_price > 0) else price
        distance = abs(price - mid)

        with self._lock:
            if ticker not in self._trades:
                self._trades[ticker] = []
            self._trades[ticker].append((now, distance, count))
            self._purge_locked(ticker, now)

    def _purge_locked(self, ticker: str, now: float) -> None:
        cutoff = now - self.window_seconds
        trades = self._trades.get(ticker, [])
        self._trades[ticker] = [t for t in trades if t[0] >= cutoff]

    def estimate_hazard(self, ticker: str, timestamp: float | None = None) -> tuple[float, float]:
        """Estimate arrival intensity lambda (trades/sec) and order density elasticity kappa.

        Returns:
            tuple of (arrival_intensity_lambda, hazard_rate_kappa).
        """
        now = timestamp if timestamp is not None else self._time_fn()
        with self._lock:
            self._purge_locked(ticker, now)
            trades = self._trades.get(ticker, [])
            n_trades = len(trades)

            if n_trades < self.min_trades:
                # Fall back to default parameters when trading history is insufficient
                return 0.1, self.default_kappa

            # Arrival intensity lambda = trades / window
            window_actual = max(1.0, now - trades[0][0])
            arrival_intensity = n_trades / window_actual

            # Estimate kappa: mean distance of fills from mid
            # In an exponential distribution lambda(delta) ~ exp(-kappa * delta), E[delta] = 1 / kappa
            # kappa = 1 / E[delta]
            weighted_dist_sum = sum(dist * ct for _, dist, ct in trades)
            total_contracts = sum(ct for _, _, ct in trades)

            if total_contracts > 0:
                mean_dist = weighted_dist_sum / total_contracts
            else:
                mean_dist = sum(dist for _, dist, _ in trades) / n_trades

            # Prevent zero-division; clamp mean_dist to between 0.5 cents and 20 cents
            clamped_dist = max(0.005, min(0.20, mean_dist))
            estimated_kappa = 1.0 / clamped_dist

            # Dampen toward default_kappa to prevent wild swings on small samples
            weight = min(1.0, n_trades / 20.0)
            final_kappa = weight * estimated_kappa + (1.0 - weight) * self.default_kappa
            final_kappa = max(10.0, min(200.0, final_kappa))

            return arrival_intensity, final_kappa


# ---------------------------------------------------------------------------
# AvellanedaStoikovEngine
# ---------------------------------------------------------------------------


class AvellanedaStoikovEngine:
    """Avellaneda-Stoikov quantitative microstructure pricing engine.

    Combines tick-level realized volatility with Poisson arrival hazard rates to determine
    the mathematically optimal reservation price, bid/ask half-spreads, and inventory skew.
    """

    def __init__(
        self,
        risk_aversion_gamma: float = 0.15,
        min_half_spread_cents: float = 1.0,
        max_half_spread_cents: float = 15.0,
        vol_halflife_seconds: float = 30.0,
        default_kappa: float = 75.0,
        adaptive_sizing_enabled: bool = True,
        time_fn: Any = time.time,
        gamma: float | None = None,
    ):
        eff_gamma = gamma if gamma is not None else risk_aversion_gamma
        self.gamma = max(0.01, min(2.0, float(eff_gamma)))
        self.min_half_spread = max(0.01, float(min_half_spread_cents) / 100.0)
        self.max_half_spread = max(self.min_half_spread, float(max_half_spread_cents) / 100.0)
        self.adaptive_sizing_enabled = adaptive_sizing_enabled
        self._time_fn = time_fn

        self.vol_tracker = MicroVolatilityTracker(
            halflife_seconds=vol_halflife_seconds,
            time_fn=time_fn,
        )
        self.hazard_estimator = HazardRateEstimator(
            default_kappa=default_kappa,
            time_fn=time_fn,
        )

    def record_price(self, ticker: str, price: float, timestamp: float | None = None) -> float:
        return self.vol_tracker.record_price(ticker, price, timestamp=timestamp)

    def record_trade(
        self,
        ticker: str,
        price: float,
        count: int,
        mid_price: float | None = None,
        timestamp: float | None = None,
    ) -> None:
        self.hazard_estimator.record_trade(
            ticker=ticker,
            price=price,
            count=count,
            mid_price=mid_price,
            timestamp=timestamp,
        )

    def calculate_pricing(
        self,
        ticker: str,
        mid_price: float,
        inventory: float = 0.0,
        max_inventory: float = 50.0,
        horizon_fraction: float = 1.0,
        timestamp: float | None = None,
        book: dict | None = None,
        toxicity_spread_multiplier: float = 1.0,
        skew_spread_multiplier: float = 1.0,
    ) -> MicrostructurePricingResult:
        """Compute optimal reservation price and bid/ask quotes using Avellaneda-Stoikov equations.

        Args:
            ticker: Market ticker.
            mid_price: Mid price in dollars (0.01 - 0.99).
            inventory: Current net inventory in dollars or contracts (signed).
            max_inventory: Maximum inventory capacity.
            horizon_fraction: Relative time horizon fraction (0.1 - 1.0).
            timestamp: Optional evaluation timestamp.
            book: Optional order book snapshot.
            toxicity_spread_multiplier: Optional multiplier from adverse selection.
            skew_spread_multiplier: Optional multiplier from inventory skew.

        Returns:
            MicrostructurePricingResult with optimal quotes and diagnostics.
        """
        # 1. Micro-volatility and hazard rate parameters
        sigma = self.vol_tracker.get_volatility(ticker)
        variance = sigma * sigma
        regime = self.vol_tracker.get_regime(ticker)
        arrival_intensity, kappa = self.hazard_estimator.estimate_hazard(ticker, timestamp=timestamp)

        # 2. Normalized inventory q in [-1, +1]
        norm_q = (inventory / max_inventory) if max_inventory > 0 else 0.0
        norm_q = max(-1.0, min(1.0, norm_q))

        # 3. Optimal symmetric half-spread from Avellaneda-Stoikov formula:
        # delta = (1 / gamma) * ln(1 + gamma / kappa) + 0.5 * gamma * sigma^2
        # First term: compensation for adverse selection & arrival intensity kappa
        # Second term: compensation for inventory price risk over the horizon
        liquidity_term = (1.0 / self.gamma) * math.log(1.0 + (self.gamma / kappa))
        volatility_term = 0.5 * self.gamma * variance * 50.0 * horizon_fraction
        raw_half_spread = liquidity_term + volatility_term

        if skew_spread_multiplier > 1.0:
            raw_half_spread *= skew_spread_multiplier
        if toxicity_spread_multiplier > 1.0:
            raw_half_spread *= toxicity_spread_multiplier

        # Regime-based widening
        if regime == "elevated":
            raw_half_spread *= 1.25
        elif regime == "extreme":
            raw_half_spread *= 1.75

        # 4. Bound half-spread within safety limits
        clamped_half_spread = max(self.min_half_spread, min(self.max_half_spread, raw_half_spread))

        # 5. Reservation (indifference) price: r(s, q) = s - q * gamma * sigma^2 * T
        # When long (norm_q > 0), reservation price drops below mid to encourage sales
        inventory_skew_factor = 0.5
        base_skew = norm_q * inventory_skew_factor * clamped_half_spread
        vol_skew = norm_q * self.gamma * variance * 50.0 * horizon_fraction
        inventory_skew_cents = base_skew + vol_skew
        reservation_price = mid_price - inventory_skew_cents

        # 6. Optimal Quotes centered on reservation price
        optimal_bid = reservation_price - clamped_half_spread
        optimal_ask = reservation_price + clamped_half_spread

        # Boundary clamping to valid contract limits [0.01, 0.99]
        optimal_bid = max(0.01, min(0.98, optimal_bid))
        optimal_ask = max(optimal_bid + 0.01, min(0.99, optimal_ask))

        # 7. Adaptive quote sizing multiplier
        sizing_mult = 1.0
        if self.adaptive_sizing_enabled:
            if regime == "extreme":
                sizing_mult = 0.35
            elif regime == "elevated":
                sizing_mult = 0.65
            else:
                sizing_mult = 1.0

        return MicrostructurePricingResult(
            ticker=ticker,
            mid=mid_price,
            reservation_price=reservation_price,
            optimal_bid=optimal_bid,
            optimal_ask=optimal_ask,
            half_spread=clamped_half_spread,
            micro_volatility=sigma,
            hazard_kappa=kappa,
            arrival_intensity=arrival_intensity,
            volatility_regime=regime,
            sizing_multiplier=sizing_mult,
        )

    def get_status(self, ticker: str | None = None) -> dict[str, Any]:
        """Diagnostic summary of microstructure pricing state."""
        res: dict[str, Any] = {
            "gamma": self.gamma,
            "min_half_spread_cents": self.min_half_spread * 100.0,
            "max_half_spread_cents": self.max_half_spread * 100.0,
            "adaptive_sizing_enabled": self.adaptive_sizing_enabled,
        }
        if ticker:
            res["ticker"] = ticker
            res["micro_volatility"] = round(self.vol_tracker.get_volatility(ticker), 6)
            res["volatility_regime"] = self.vol_tracker.get_regime(ticker)
            arrival, kappa = self.hazard_estimator.estimate_hazard(ticker)
            res["arrival_intensity"] = round(arrival, 3)
            res["hazard_kappa"] = round(kappa, 2)
            res["trades_recorded"] = len(self.hazard_estimator._trades.get(ticker, []))
        return res

    def get_all_metrics(self) -> dict[str, Any]:
        """Diagnostic summary across all tracked tickers."""
        tickers = set(self.vol_tracker._state.keys()) | set(self.hazard_estimator._trades.keys())
        return {t: self.get_status(t) for t in tickers}
