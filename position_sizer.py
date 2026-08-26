"""Kelly criterion + strategy-aware position sizing (Phase E1)."""

import logging

from config import (
    KELLY_FRACTION,
    KELLY_MAX_FRACTION,
    PLATFORM_MIN_ORDER_SIZE,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Opportunity type classification
# ---------------------------------------------------------------------------

# Pure arbitrage — edge is deterministic (locked in at execution).  Uses full
# Kelly because the "probability of winning" is effectively 1.0; the only risk
# is execution failure, which is handled upstream by revalidation.
_PURE_ARB_PREFIXES = (
    "Binary",
    "NegRisk",
    "Cross",
    "KalshiBinary",
    "KalshiMulti",
    "BetfairBackAll",
    "BetfairBackLay",
    "SmarketsBackAll",
    "SmarketsBackLay",
    "SXBetBackAll",
    "SXBetBackLay",
    "MatchbookBackAll",
    "MatchbookBackLay",
    "GeminiBinary",
    "GeminiMulti",
    "IBKRBinary",
    "MultiCross",
    "TriangularCross",
)

# Near-arbitrage — edge is very likely but not guaranteed (e.g. stale price
# may update before our order fills, resolution outcome may surprise).
_NEAR_ARB_PREFIXES = (
    "StalePriceOpp",
    "ResolutionSnipeOpp",
)

# Market-making — profit comes from capturing the spread; no directional
# edge.  Size is a fixed fraction based on spread width rather than Kelly.
_MARKET_MAKE_PREFIXES = (
    "MarketMake",
    "SpreadPM",
    "SpreadKalshi",
)

# Informed trading — directional bets driven by divergence signals.
# Edge is uncertain; size via fractional Kelly scaled by signal strength.
_INFORMED_PREFIXES = (
    "EventDivergence",
    "Convergence",
)

# Default confidence multiplier for near-arb opportunities when the
# opportunity dict does not carry an explicit "confidence" numeric field.
_NEAR_ARB_CONFIDENCE = 0.75

# Default signal-strength multiplier for informed-trading opportunities when
# the opportunity dict does not carry an explicit "signal_strength" field.
_DEFAULT_SIGNAL_STRENGTH = 0.50


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _match_prefix(opp_type: str, prefixes: tuple[str, ...]) -> bool:
    """Return True if *opp_type* starts with any of the given prefixes."""
    return any(opp_type.startswith(p) for p in prefixes)


def _platform_min_order(opportunity: dict) -> float:
    """Return the minimum order size for the opportunity's platform.

    Cross-platform opportunities touch multiple platforms; use the highest
    minimum so neither leg is below its platform floor.
    """
    opp_type = opportunity.get("type", "")

    # Single-platform types — infer platform from the type string
    platform_hints: dict[str, str] = {
        "Binary": "polymarket",
        "NegRisk": "polymarket",
        "KalshiBinary": "kalshi",
        "KalshiMulti": "kalshi",
        "BetfairBack": "betfair",
        "SmarketsBack": "smarkets",
        "SXBetBack": "sxbet",
        "MatchbookBack": "matchbook",
        "GeminiBinary": "gemini",
        "GeminiMulti": "gemini",
        "IBKRBinary": "ibkr",
        "SpreadPM": "polymarket",
        "SpreadKalshi": "kalshi",
    }

    # Check for an explicit platform list (multi-cross / triangular)
    platforms = opportunity.get("_platforms", [])
    if platforms:
        return max(
            PLATFORM_MIN_ORDER_SIZE.get(p.lower(), 0.01) for p in platforms
        )

    # Cross-platform — two legs on different platforms
    if opp_type.startswith("Cross") or opp_type == "TriangularCross":
        mins: list[float] = []
        for key in ("_platform_a", "_platform_b", "_yes_platform", "_no_platform", "_platform"):
            plat = opportunity.get(key, "")
            if plat:
                mins.append(PLATFORM_MIN_ORDER_SIZE.get(plat.lower(), 0.01))
        return max(mins) if mins else 0.01

    # Single-platform fallback
    for prefix, plat in platform_hints.items():
        if opp_type.startswith(prefix):
            return PLATFORM_MIN_ORDER_SIZE.get(plat, 0.01)

    return 0.01


# ---------------------------------------------------------------------------
# PositionSizer
# ---------------------------------------------------------------------------

class PositionSizer:
    """Kelly criterion + strategy-aware position sizing.

    Computes the dollar amount to trade for a given opportunity based on:
    1. Kelly criterion (``f* = edge / odds``)
    2. Strategy-specific confidence scaling
    3. Hard bankroll-fraction cap (``max_fraction``)
    4. Platform minimum order sizes
    """

    def __init__(
        self,
        bankroll: float,
        max_fraction: float = KELLY_MAX_FRACTION,
        kelly_fraction: float = KELLY_FRACTION,
    ):
        """Initialise the position sizer.

        Args:
            bankroll: Total available capital (dollars).
            max_fraction: Maximum fraction of bankroll per trade (hard cap).
            kelly_fraction: Fraction of full Kelly to use (0.5 = half Kelly).
        """
        if bankroll < 0:
            raise ValueError("bankroll must be >= 0")
        if not (0 < max_fraction <= 1):
            raise ValueError("max_fraction must be in (0, 1]")
        if not (0 < kelly_fraction <= 1):
            raise ValueError("kelly_fraction must be in (0, 1]")

        self.bankroll = bankroll
        self.max_fraction = max_fraction
        self.kelly_fraction = kelly_fraction

    # ------------------------------------------------------------------
    # Core Kelly
    # ------------------------------------------------------------------

    def kelly_size(self, edge: float, odds: float) -> float:
        """Compute the raw Kelly fraction (before safety multiplier).

        Formula: ``f* = edge / odds`` where *edge* is the expected profit
        rate and *odds* is the net payout ratio (profit per dollar risked
        when you win).

        Returns:
            Raw Kelly fraction in [0, 1].  Negative edges return 0.
        """
        if edge <= 0 or odds <= 0:
            return 0.0
        return min(edge / odds, 1.0)

    # ------------------------------------------------------------------
    # Edge estimation
    # ------------------------------------------------------------------

    def get_edge_estimate(self, opportunity: dict) -> float:
        """Estimate the expected edge from an opportunity dict.

        For pure arbs the edge equals ``net_roi`` directly (deterministic).
        For near-arbs the edge is discounted by a confidence factor.
        For informed/signal trades the edge incorporates signal strength
        and divergence magnitude.

        Returns:
            Estimated edge as a fraction (e.g. 0.05 = 5%).
        """
        opp_type = opportunity.get("type", "")
        net_roi = self._extract_net_roi(opportunity)

        # --- Pure arbitrage: edge is the ROI itself ---
        if _match_prefix(opp_type, _PURE_ARB_PREFIXES):
            return max(net_roi, 0.0)

        # --- Near-arbitrage: discount by confidence ---
        if _match_prefix(opp_type, _NEAR_ARB_PREFIXES):
            confidence = self._extract_confidence(opportunity)
            return max(net_roi * confidence, 0.0)

        # --- Market making: edge is the spread width ---
        if _match_prefix(opp_type, _MARKET_MAKE_PREFIXES):
            return max(net_roi, 0.0)

        # --- Informed trading: signal_strength * divergence ---
        if _match_prefix(opp_type, _INFORMED_PREFIXES):
            signal = opportunity.get("_signal_strength",
                                     opportunity.get("signal_strength",
                                                     _DEFAULT_SIGNAL_STRENGTH))
            divergence = opportunity.get("_divergence", abs(net_roi))
            return max(float(signal) * float(divergence), 0.0)

        # Unknown type — fall back to net_roi
        logger.debug("Unknown opp type %r for edge estimation; using net_roi", opp_type)
        return max(net_roi, 0.0)

    # ------------------------------------------------------------------
    # Main sizing method
    # ------------------------------------------------------------------

    def size_for_opportunity(self, opportunity: dict) -> float:
        """Return the dollar amount to trade for *opportunity*.

        Sizing pipeline:
        1. Classify the opportunity type.
        2. Compute the edge estimate and raw Kelly fraction.
        3. Apply strategy-specific multiplier.
        4. Apply the global ``kelly_fraction`` safety multiplier.
        5. Cap at ``max_fraction * bankroll``.
        6. Floor at the platform minimum order size.

        Returns:
            Dollar amount to trade.  Returns 0.0 if edge is non-positive
            or bankroll is zero.
        """
        if self.bankroll <= 0:
            return 0.0

        opp_type = opportunity.get("type", "")
        edge = self.get_edge_estimate(opportunity)

        if edge <= 0:
            return 0.0

        # Odds = net payout ratio.  For prediction-market arbs paying $1
        # per contract, odds ~ net_roi (edge / cost).  When edge == roi
        # the Kelly fraction simplifies to 1.0 for pure arbs, but we
        # still run through the formula for consistency.
        net_roi = self._extract_net_roi(opportunity)
        odds = max(net_roi, edge, 0.001)  # prevent division-by-zero

        raw_kelly = self.kelly_size(edge, odds)

        # --- Strategy-specific multiplier ---
        if _match_prefix(opp_type, _PURE_ARB_PREFIXES):
            # Deterministic edge — use full (fractional) Kelly
            strategy_mult = 1.0

        elif _match_prefix(opp_type, _NEAR_ARB_PREFIXES):
            # High-confidence but not certain — 75% of Kelly
            strategy_mult = _NEAR_ARB_CONFIDENCE

        elif _match_prefix(opp_type, _MARKET_MAKE_PREFIXES):
            # Fixed fraction based on spread width — wider spread = more size
            spread_width = self._extract_spread_width(opportunity)
            # Fraction scales linearly: 5% base + up to 10% for wide spreads
            strategy_mult = min(0.05 + spread_width, 0.15) / max(raw_kelly, 0.001)
            # Clamp so that the product raw_kelly * strategy_mult stays sane
            strategy_mult = min(strategy_mult, 1.0)

        elif _match_prefix(opp_type, _INFORMED_PREFIXES):
            # Scale by signal confidence level
            confidence = opportunity.get("confidence", "LOW")
            if isinstance(confidence, str):
                confidence_map = {"HIGH": 0.80, "MEDIUM": 0.50, "LOW": 0.25}
                strategy_mult = confidence_map.get(confidence.upper(), 0.25)
            else:
                strategy_mult = max(0.0, min(float(confidence), 1.0))

        else:
            # Unknown — conservative
            strategy_mult = 0.50

        # Apply strategy multiplier + global Kelly fraction
        fraction = raw_kelly * strategy_mult * self.kelly_fraction

        # Hard cap
        fraction = min(fraction, self.max_fraction)

        # Dollar amount
        size = fraction * self.bankroll

        # Floor at platform minimum
        min_order = _platform_min_order(opportunity)
        if size < min_order:
            # If we cannot even afford the minimum, return 0 (skip trade)
            if min_order > self.max_fraction * self.bankroll:
                logger.debug(
                    "Skipping %s — min order $%.2f exceeds max position $%.2f",
                    opp_type, min_order, self.max_fraction * self.bankroll,
                )
                return 0.0
            size = min_order

        logger.debug(
            "Sized %s: edge=%.4f odds=%.4f kelly=%.4f strat=%.2f frac=%.4f -> $%.2f",
            opp_type, edge, odds, raw_kelly, strategy_mult, fraction, size,
        )
        return round(size, 2)

    # ------------------------------------------------------------------
    # Bankroll management
    # ------------------------------------------------------------------

    def update_bankroll(self, new_bankroll: float) -> None:
        """Update the bankroll (e.g. after a trade settles or balance refresh).

        Args:
            new_bankroll: New total available capital (dollars).
        """
        if new_bankroll < 0:
            raise ValueError("bankroll must be >= 0")
        logger.info("Bankroll updated: $%.2f -> $%.2f", self.bankroll, new_bankroll)
        self.bankroll = new_bankroll

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _extract_net_roi(opportunity: dict) -> float:
        """Extract net ROI as a float from the opportunity dict.

        Handles both numeric values and formatted strings like ``"5.23%"``.
        """
        raw = opportunity.get("net_roi", 0)
        if isinstance(raw, str):
            is_percent = raw.strip().endswith("%")
            raw = raw.rstrip("%").strip()
            try:
                val = float(raw)
                return val / 100.0 if is_percent else val
            except (ValueError, TypeError):
                return 0.0
        try:
            return float(raw)
        except (ValueError, TypeError):
            return 0.0

    @staticmethod
    def _extract_confidence(opportunity: dict) -> float:
        """Extract a numeric confidence in [0, 1] from the opportunity dict.

        Falls back to ``_NEAR_ARB_CONFIDENCE`` when the field is missing or
        is a non-numeric string label.
        """
        raw = opportunity.get("confidence", opportunity.get("_confidence"))
        if raw is None:
            return _NEAR_ARB_CONFIDENCE
        if isinstance(raw, (int, float)):
            return max(0.0, min(float(raw), 1.0))
        if isinstance(raw, str):
            label_map = {"HIGH": 0.90, "MEDIUM": 0.75, "LOW": 0.50}
            return label_map.get(raw.upper(), _NEAR_ARB_CONFIDENCE)
        return _NEAR_ARB_CONFIDENCE

    @staticmethod
    def _extract_spread_width(opportunity: dict) -> float:
        """Extract spread width from opportunity dict.

        Tries ``_spread_width``, ``gross_spread``, then falls back to
        ``net_roi`` as a rough proxy.
        """
        for key in ("_spread_width", "gross_spread"):
            val = opportunity.get(key)
            if val is not None:
                try:
                    # Handle "$0.0123" formatted strings
                    if isinstance(val, str):
                        val = val.lstrip("$").strip()
                    return max(float(val), 0.0)
                except (ValueError, TypeError):
                    continue
        # Fallback — use net_roi as a rough proxy
        return max(PositionSizer._extract_net_roi(opportunity), 0.0)
