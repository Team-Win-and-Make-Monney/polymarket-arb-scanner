"""Risk management gates for arbitrage execution."""

import logging
import math

logger = logging.getLogger(__name__)


class RiskManager:
    """Pure gate: returns (allowed, reason) for each opportunity."""

    def __init__(self, config: dict):
        self.base_trade_size = config.get("base_trade_size", 5.0)
        self.max_trade_size = config.get("max_trade_size", 25.0)
        self.daily_loss_limit = config.get("daily_loss_limit", 25.0)
        self.max_open_positions = config.get("max_open_positions", 25)
        self.min_liquidity = config.get("min_liquidity", 25.0)
        self.min_liquidity_high_roi = config.get("min_liquidity_high_roi", 10.0)
        self.min_net_roi = config.get("min_net_roi", 0)
        self.allow_better_reentry = config.get("allow_better_reentry", True)
        self.reentry_improvement_threshold = config.get("reentry_improvement_threshold", 0.20)
        # MM-specific limits
        self.mm_max_inventory_per_market = config.get("mm_max_inventory", 50.0)
        self.mm_max_total_exposure = config.get("mm_max_total_exposure", 500.0)
        # Daily trade limit (0 = unlimited)
        self.max_daily_trades = config.get("max_daily_trades", 0)

    # Opportunity types that skip depth and dedup checks
    _SKIP_DEPTH_TYPES = frozenset({
        "MarketMake", "EventDivergence", "ConvergenceOpp",
        # KalshiMulti/MultiCross: CLOB order book API returns empty for many
        # multi-outcome markets, but REST prices are real and tradeable.
        # Depth was already checked during scan-phase CLOB refinement.
        "KalshiMulti", "MultiCross",
        # Phase 8-9 signal-based strategies — liquidity verified at revalidation
        "Imbalance", "NewsSnipe", "Correlated", "TimeDecay",
        "LogicalArb", "WhaleCopy",
        # Reward strategies — we're placing orders, not filling existing ones
        "PolymarketRewards", "KalshiRewards",
    })
    _SKIP_DEDUP_TYPES = frozenset({"MarketMake"})

    def check(self, opportunity: dict, db, balances: dict | None = None) -> tuple[bool, str]:
        """Check if an opportunity passes all risk gates.

        Args:
            opportunity: The arbitrage opportunity dict from scanner
            db: TradeDB instance for querying state
            balances: Optional dict with "polymarket" and/or "kalshi" balance floats

        Returns:
            (allowed, reason) tuple
        """
        # 1. Daily P&L within limit
        daily_pnl = db.get_daily_pnl()
        if daily_pnl < -self.daily_loss_limit:
            return False, f"Daily loss limit hit (P&L: ${daily_pnl:.2f}, limit: -${self.daily_loss_limit:.2f})"

        # 1b. Daily trade count limit
        if self.max_daily_trades > 0:
            daily_trade_count = db.get_daily_trade_count()
            if daily_trade_count >= self.max_daily_trades:
                return False, f"Daily trade limit reached ({daily_trade_count}/{self.max_daily_trades})"

        # 2. Open positions limit
        open_count = db.get_open_positions_count()
        if open_count >= self.max_open_positions:
            return False, f"Max open positions reached ({open_count}/{self.max_open_positions})"

        # 3. Balance check
        opp_type = opportunity.get("type", "")
        if balances:
            # Parse total cost from string like "$0.9500"
            total_cost_str = opportunity.get("total_cost", "$0")
            total_cost = float(total_cost_str.replace("$", "")) if isinstance(total_cost_str, str) else float(total_cost_str)
            trade_cost = min(self.max_trade_size, total_cost)

            if "Kalshi" in opp_type and "Cross" not in opp_type:
                # Kalshi-only arb: entire cost is on Kalshi
                k_balance = balances.get("kalshi", 0)
                if not isinstance(k_balance, (int, float)) or not math.isfinite(k_balance):
                    return False, "Kalshi balance unavailable"
                if k_balance < trade_cost:
                    return False, f"Insufficient Kalshi balance (${k_balance:.2f})"
            elif opp_type.startswith("Gemini"):
                # Gemini-only arb: entire cost is on Gemini
                g_balance = balances.get("gemini", 0)
                if not isinstance(g_balance, (int, float)) or not math.isfinite(g_balance):
                    return False, "Gemini balance unavailable"
                if g_balance < trade_cost:
                    return False, f"Insufficient Gemini balance (${g_balance:.2f})"
            elif opp_type.startswith("IBKR"):
                # IBKR-only arb: entire cost is on IBKR
                i_balance = balances.get("ibkr", 0)
                if not isinstance(i_balance, (int, float)) or not math.isfinite(i_balance):
                    return False, "IBKR balance unavailable"
                if i_balance < trade_cost:
                    return False, f"Insufficient IBKR balance (${i_balance:.2f})"
            elif "Cross" in opp_type:
                # Cross-platform: check both participating platforms
                platform_a = opportunity.get("_platform_a", "")
                platform_b = opportunity.get("_platform_b", "")
                platforms_involved = {platform_a, platform_b} if platform_a else {"polymarket", "kalshi"}
                for plat in platforms_involved:
                    if not plat:
                        continue
                    bal = balances.get(plat, 0)
                    # A missing per-leg notional must not be guessed as 50/50.
                    # Requiring each venue to cover the full bounded size is
                    # conservative and cannot authorize an underfunded leg.
                    if not isinstance(bal, (int, float)) or not math.isfinite(bal):
                        return False, f"{plat.capitalize()} balance unavailable"
                    if bal < trade_cost:
                        return False, f"Insufficient {plat.capitalize()} balance (${bal:.2f})"
            else:
                pm_balance = balances.get("polymarket", 0)
                if not isinstance(pm_balance, (int, float)) or not math.isfinite(pm_balance):
                    return False, "Polymarket balance unavailable"
                if pm_balance < trade_cost:
                    return False, f"Insufficient Polymarket balance (${pm_balance:.2f})"

        # 4. Order book depth check (tiered by ROI)
        # Skip depth check for types that create liquidity or are signal-based
        depth = opportunity.get("_clob_depth", 0)
        net_profit = opportunity.get("net_profit", 0)
        total_cost_str = opportunity.get("total_cost", "$0")
        total_cost = float(total_cost_str.replace("$", "")) if isinstance(total_cost_str, str) else float(total_cost_str)
        roi = net_profit / total_cost if total_cost > 0 else 0

        # Use prefix matching: "KalshiMulti(4)" matches "KalshiMulti"
        skip_depth = any(opp_type.startswith(t) for t in self._SKIP_DEPTH_TYPES)
        if not skip_depth:
            # High-ROI opportunities (>5%) use lower depth threshold
            depth_threshold = self.min_liquidity_high_roi if roi > 0.05 else self.min_liquidity
            if depth < depth_threshold:
                return False, f"Insufficient depth ({depth:.0f} < {depth_threshold:.0f})"

        # 5. Net ROI check (skip when min_net_roi == 0)
        if self.min_net_roi > 0 and total_cost > 0:
            if roi < self.min_net_roi:
                return False, f"ROI too low ({roi:.2%} < {self.min_net_roi:.2%})"

        # 6. Dedup: not already trading this market (with smart re-entry)
        # Skip for MM — market makers continuously quote the same markets
        market = opportunity.get("market", "")
        if opp_type not in self._SKIP_DEDUP_TYPES and db.is_market_active(market):
            if self.allow_better_reentry:
                existing_pnl = db.get_active_market_expected_pnl(market)
                improvement_floor = (
                    existing_pnl + abs(existing_pnl) * self.reentry_improvement_threshold
                    if existing_pnl is not None
                    else None
                )
                if improvement_floor is not None and net_profit > improvement_floor:
                    pass  # Allow re-entry — new opportunity is significantly better
                else:
                    return False, "Already trading this market"
            else:
                return False, "Already trading this market"

        # 7. MM-specific: check inventory exposure limits
        if opp_type == "MarketMake":
            inventory = opportunity.get("_inventory", 0)
            if abs(inventory) >= self.mm_max_inventory_per_market:
                return False, f"MM inventory limit reached ({abs(inventory):.0f} >= {self.mm_max_inventory_per_market:.0f})"

        return True, "OK"

    def calculate_dynamic_size(self, opportunity: dict, aggressiveness: float = 0.5) -> float:
        """Calculate trade size based on opportunity quality using half-Kelly sizing.

        Starts from base_trade_size and scales up toward max_trade_size based
        on ROI.  Formula: base * (1 + ROI * aggressiveness * 20), capped at
        50% of available depth and max_trade_size.

        Args:
            opportunity: Opportunity dict with net_profit, total_cost, _clob_depth
            aggressiveness: Fraction of Kelly criterion (0.5 = half-Kelly)

        Returns:
            Calculated trade size in dollars.
        """
        base_size = self.base_trade_size
        net_profit = opportunity.get("net_profit", 0)
        total_cost_str = opportunity.get("total_cost", "$0")
        total_cost = float(total_cost_str.replace("$", "")) if isinstance(total_cost_str, str) else float(total_cost_str)

        if total_cost <= 0 or net_profit <= 0:
            return base_size

        roi = net_profit / total_cost
        size = base_size * (1 + roi * aggressiveness * 20)

        # Cap at 50% of available depth to avoid slippage
        depth = opportunity.get("_clob_depth", 0)
        if depth > 0:
            size = min(size, depth * 0.5)

        # Still capped by max_trade_size
        size = min(size, self.max_trade_size)
        return max(0, size)

    def clamp_size(self, desired_size: float, depth: float, balance: float | None) -> float:
        """Calculate safe trade size given constraints."""
        size = min(desired_size, self.max_trade_size)
        if depth > 0:
            size = min(size, depth)
        if balance is not None and balance > 0:
            size = min(size, balance)
        return max(0, size)
