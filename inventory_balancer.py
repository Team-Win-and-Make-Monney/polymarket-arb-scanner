"""Cross-venue delta-neutral inventory balancer for prediction markets.

Tracks net directional inventory (delta) across Polymarket and Kalshi,
detects significant imbalances, generates delta-neutral rebalancing proposals,
and gates new trades in RiskManager to prevent worsening unhedged delta skew.
"""

import logging
import threading
import time

from config import (
    INVENTORY_BALANCER_ENABLED,
    INVENTORY_MAX_DELTA_CONTRACTS,
    INVENTORY_MAX_IMBALANCE_RATIO,
    INVENTORY_REBALANCE_MAX_COST,
)

logger = logging.getLogger(__name__)


class InventoryBalancer:
    """Tracks and balances cross-venue inventory delta across prediction markets.

    Delta definition in binary prediction markets:
      - Long YES: +1 delta (benefits if event resolves YES)
      - Long NO:  -1 delta (benefits if event resolves NO)
      - Net delta: Delta = sum(Q_yes) - sum(Q_no)
      - Delta neutrality: Delta = 0
    """

    def __init__(
        self,
        max_delta_contracts: float | None = None,
        max_imbalance_ratio: float | None = None,
        max_rebalance_cost: float | None = None,
        enabled: bool | None = None,
    ):
        """Initialize the cross-venue inventory balancer.

        Args:
            max_delta_contracts: Contract threshold for imbalance (|Delta| >= threshold).
            max_imbalance_ratio: Ratio threshold (|Delta| / total_contracts >= ratio).
            max_rebalance_cost: Maximum dollar cost allocated to a single rebalance.
            enabled: Master switch for inventory balancing and skew gating.
        """
        self.enabled = INVENTORY_BALANCER_ENABLED if enabled is None else bool(enabled)
        self.max_delta_contracts = (
            INVENTORY_MAX_DELTA_CONTRACTS if max_delta_contracts is None else float(max_delta_contracts)
        )
        self.max_imbalance_ratio = (
            INVENTORY_MAX_IMBALANCE_RATIO if max_imbalance_ratio is None else float(max_imbalance_ratio)
        )
        self.max_rebalance_cost = (
            INVENTORY_REBALANCE_MAX_COST if max_rebalance_cost is None else float(max_rebalance_cost)
        )

        # In-memory inventory tracking:
        # {market_key: {platform: {"yes": float, "no": float}}}
        self._positions: dict[str, dict[str, dict[str, float]]] = {}
        self._lock = threading.Lock()

    def update_position(
        self,
        market_key: str,
        platform: str,
        outcome: str,
        side: str,
        size: float,
    ) -> None:
        """Update tracked position for a market, platform, and outcome.

        Args:
            market_key: Unique market or condition identifier.
            platform: Trading venue (e.g. 'polymarket', 'kalshi').
            outcome: Contract outcome ('yes' or 'no').
            side: Trade side ('buy' adds position, 'sell' reduces position).
            size: Number of contracts.
        """
        if not market_key or not platform or not outcome:
            return

        m_key = str(market_key).strip()
        plat = str(platform).strip().lower()
        out = str(outcome).strip().lower()
        side_l = str(side).strip().lower()
        qty = float(size)

        if qty <= 0:
            return

        with self._lock:
            if m_key not in self._positions:
                self._positions[m_key] = {}
            if plat not in self._positions[m_key]:
                self._positions[m_key][plat] = {"yes": 0.0, "no": 0.0}

            curr = self._positions[m_key][plat].get(out, 0.0)
            if side_l == "buy":
                self._positions[m_key][plat][out] = curr + qty
            elif side_l == "sell":
                self._positions[m_key][plat][out] = max(0.0, curr - qty)

    def sync_from_db(self, db) -> int:
        """Synchronize in-memory positions from TradeDB open positions.

        Args:
            db: TradeDB instance.

        Returns:
            Number of open positions processed.
        """
        if not db:
            return 0

        try:
            open_positions = db.get_open_positions()
        except Exception as e:
            logger.debug("Failed querying open positions from DB: %s", e)
            return 0

        synced_count = 0
        new_positions: dict[str, dict[str, dict[str, float]]] = {}

        for pos in open_positions:
            opp_id = pos.get("opportunity_id")
            m_key = pos.get("market_identifier") or pos.get("market_ticker") or str(opp_id)
            if not m_key or not opp_id:
                continue

            try:
                trades = db.get_trades_for_opportunity(opp_id)
            except Exception as e:
                logger.debug("Failed querying trades for opportunity %s: %s", opp_id, e)
                continue

            for t in trades:
                if t.get("status") != "filled":
                    continue

                plat = (t.get("platform") or pos.get("platform") or "polymarket").lower()
                side = (t.get("side") or "buy").lower()
                outcome = (t.get("outcome") or "yes").lower()
                size = float(t.get("size") or 0.0)

                if m_key not in new_positions:
                    new_positions[m_key] = {}
                if plat not in new_positions[m_key]:
                    new_positions[m_key][plat] = {"yes": 0.0, "no": 0.0}

                if outcome not in ("yes", "no"):
                    # Default ambiguous outcome to yes
                    outcome = "yes"

                curr = new_positions[m_key][plat].get(outcome, 0.0)
                if side == "buy":
                    new_positions[m_key][plat][outcome] = curr + size
                elif side == "sell":
                    new_positions[m_key][plat][outcome] = max(0.0, curr - size)

            synced_count += 1

        with self._lock:
            self._positions = new_positions

        return synced_count

    def compute_inventory_deltas(
        self,
        positions_data: dict | list | None = None,
    ) -> dict[str, dict]:
        """Compute net directional delta and imbalance metrics per market.

        Args:
            positions_data: Optional explicit position dict or list of dicts.
                If None, uses internal `self._positions`.

        Returns:
            Dict mapping market_key to delta summary:
            {
                "market_key": str,
                "qty_yes": float,
                "qty_no": float,
                "total_qty": float,
                "delta_net": float,
                "imbalance_ratio": float,
                "is_imbalanced": bool,
                "platform_breakdown": dict,
            }
        """
        results: dict[str, dict] = {}

        # Handle list format
        if isinstance(positions_data, list):
            data_dict: dict[str, dict[str, dict[str, float]]] = {}
            for item in positions_data:
                m_key = str(item.get("market_key") or item.get("market") or "").strip()
                plat = str(item.get("platform", "polymarket")).strip().lower()
                out = str(item.get("outcome", "yes")).strip().lower()
                qty = float(item.get("size") or item.get("qty") or 0.0)
                if not m_key:
                    continue
                if m_key not in data_dict:
                    data_dict[m_key] = {}
                if plat not in data_dict[m_key]:
                    data_dict[m_key][plat] = {"yes": 0.0, "no": 0.0}
                if out in ("yes", "no"):
                    data_dict[m_key][plat][out] += qty
            positions_to_process = data_dict
        elif isinstance(positions_data, dict):
            positions_to_process = positions_data
        else:
            with self._lock:
                positions_to_process = {
                    mk: {p: dict(outs) for p, outs in plats.items()}
                    for mk, plats in self._positions.items()
                }

        for m_key, platforms in positions_to_process.items():
            total_yes = 0.0
            total_no = 0.0
            platform_breakdown: dict[str, dict[str, float]] = {}

            for plat, holdings in platforms.items():
                y = float(holdings.get("yes", 0.0) or 0.0)
                n = float(holdings.get("no", 0.0) or 0.0)
                total_yes += y
                total_no += n
                platform_breakdown[plat] = {
                    "yes": y,
                    "no": n,
                    "delta": y - n,
                }

            total_qty = total_yes + total_no
            delta_net = total_yes - total_no
            imbalance_ratio = (abs(delta_net) / total_qty) if total_qty > 0 else 0.0

            is_imbalanced = (
                abs(delta_net) >= self.max_delta_contracts
                and imbalance_ratio >= self.max_imbalance_ratio
            )

            results[m_key] = {
                "market_key": m_key,
                "qty_yes": total_yes,
                "qty_no": total_no,
                "total_qty": total_qty,
                "delta_net": delta_net,
                "imbalance_ratio": imbalance_ratio,
                "is_imbalanced": is_imbalanced,
                "platform_breakdown": platform_breakdown,
            }

        return results

    def get_imbalances(
        self,
        deltas: dict[str, dict] | None = None,
        threshold_contracts: float | None = None,
        threshold_ratio: float | None = None,
    ) -> list[dict]:
        """Return all markets exceeding the delta or ratio imbalance thresholds.

        Args:
            deltas: Optional precomputed deltas from `compute_inventory_deltas`.
            threshold_contracts: Optional override for contract threshold.
            threshold_ratio: Optional override for ratio threshold.

        Returns:
            List of imbalance summary dicts.
        """
        all_deltas = deltas if deltas is not None else self.compute_inventory_deltas()
        ct_thresh = self.max_delta_contracts if threshold_contracts is None else float(threshold_contracts)
        ratio_thresh = self.max_imbalance_ratio if threshold_ratio is None else float(threshold_ratio)

        imbalances = []
        for m_key, data in all_deltas.items():
            delta_net = data.get("delta_net", 0.0)
            ratio = data.get("imbalance_ratio", 0.0)
            if abs(delta_net) >= ct_thresh and ratio >= ratio_thresh:
                imbalances.append(data)

        return imbalances

    def generate_rebalancing_proposals(
        self,
        imbalances: list[dict] | None = None,
        feed_manager=None,
        price_cache: dict | None = None,
        kalshi_client=None,
        polymarket_client=None,
        max_rebalance_cost: float | None = None,
    ) -> list[dict]:
        """Generate delta-neutral rebalancing trade proposals for imbalanced markets.

        For each imbalanced market:
          - If delta_net > 0: long YES excess -> proposals buy deficient NO.
          - If delta_net < 0: long NO excess -> proposals buy deficient YES.
          - Sourcing checks feed_manager in-memory books and price_cache first,
            then REST clients.
          - Sizing is capped to balance delta to 0 within max_rebalance_cost.

        Args:
            imbalances: Optional list of imbalances from `get_imbalances`.
            feed_manager: FeedManager instance with streaming orderbooks.
            price_cache: WS price cache.
            kalshi_client: KalshiClient instance.
            polymarket_client: Polymarket client.
            max_rebalance_cost: Max dollar cost limit for proposal.

        Returns:
            List of structured rebalancing proposal dicts.
        """
        target_imbalances = imbalances if imbalances is not None else self.get_imbalances()
        max_cost = self.max_rebalance_cost if max_rebalance_cost is None else float(max_rebalance_cost)
        proposals = []

        for item in target_imbalances:
            m_key = item.get("market_key", "")
            delta_net = item.get("delta_net", 0.0)
            if abs(delta_net) < self.max_delta_contracts:
                continue

            # Determine deficient side to buy
            target_outcome = "no" if delta_net > 0 else "yes"
            needed_contracts = abs(delta_net)

            # Look up available quotes on Polymarket and Kalshi
            quotes = self._query_venue_quotes(
                m_key, target_outcome,
                feed_manager=feed_manager,
                price_cache=price_cache,
                kalshi_client=kalshi_client,
                polymarket_client=polymarket_client,
            )

            if not quotes:
                continue

            # Pick the best venue (lowest ask price with positive depth)
            best_quote = min(quotes, key=lambda q: (q["price"], -q["size"]))
            venue = best_quote["venue"]
            ask_price = best_quote["price"]
            depth = best_quote["size"]

            if ask_price <= 0 or ask_price >= 1.0:
                continue

            # Size the rebalancing trade
            max_qty_by_cost = max_cost / ask_price
            order_qty = min(needed_contracts, max_qty_by_cost)
            if depth > 0:
                order_qty = min(order_qty, depth)

            if order_qty <= 0.01:
                continue

            est_cost = order_qty * ask_price
            projected_delta = (
                (delta_net - order_qty) if target_outcome == "no" else (delta_net + order_qty)
            )

            proposals.append({
                "market_key": m_key,
                "action": "rebalance_buy",
                "target_venue": venue,
                "side": "buy",
                "outcome": target_outcome,
                "size": order_qty,
                "price": round(ask_price, 4),
                "estimated_cost": round(est_cost, 4),
                "current_delta": delta_net,
                "projected_delta": round(projected_delta, 4),
                "reason": (
                    f"Deficient {target_outcome.upper()}: buying {order_qty:.1f} {target_outcome.upper()} "
                    f"on {venue} @ {ask_price:.3f} to reduce delta from {delta_net:+.1f} to {projected_delta:+.1f}"
                ),
            })

        return proposals

    def check_trade_skew(
        self,
        market_identifier: str,
        proposed_outcome: str,
        proposed_side: str,
        proposed_qty: float,
    ) -> tuple[bool, str]:
        """Check if a proposed trade would worsen an existing severe inventory delta skew.

        Used as an execution gate in RiskManager.

        Args:
            market_identifier: Market or condition key.
            proposed_outcome: Proposed contract outcome ('yes' or 'no').
            proposed_side: Proposed trade side ('buy' or 'sell').
            proposed_qty: Proposed trade contract size.

        Returns:
            (allowed: bool, reason: str)
        """
        if not self.enabled:
            return True, "OK (InventoryBalancer disabled)"

        if not market_identifier or proposed_qty <= 0:
            return True, "OK"

        m_key = str(market_identifier).strip()
        outcome_l = str(proposed_outcome).strip().lower()
        side_l = str(proposed_side).strip().lower()

        # Get current net delta for this market
        with self._lock:
            market_positions = self._positions.get(m_key)
            if not market_positions:
                return True, "OK"

            total_yes = sum(float(p.get("yes", 0.0) or 0.0) for p in market_positions.values())
            total_no = sum(float(p.get("no", 0.0) or 0.0) for p in market_positions.values())

        current_delta = total_yes - total_no

        # If current delta is within limits, trade is allowed
        if abs(current_delta) < self.max_delta_contracts:
            return True, "OK"

        # Calculate delta effect of proposed trade
        # Long YES = +1 delta, Long NO = -1 delta
        # Sell YES = -1 delta, Sell NO = +1 delta
        if side_l == "buy":
            delta_change = proposed_qty if outcome_l == "yes" else -proposed_qty
        elif side_l == "sell":
            delta_change = -proposed_qty if outcome_l == "yes" else proposed_qty
        else:
            return True, "OK"

        projected_delta = current_delta + delta_change

        # If the trade increases the magnitude of an already severe delta imbalance, reject it
        if abs(projected_delta) > abs(current_delta):
            return False, (
                f"Trade rejected by InventoryBalancer: would worsen delta skew on {m_key} "
                f"(current: {current_delta:+.1f}, projected: {projected_delta:+.1f}, "
                f"limit: {self.max_delta_contracts:.1f})"
            )

        return True, "OK (trade reduces or maintains inventory delta skew)"

    def _query_venue_quotes(
        self,
        market_key: str,
        target_outcome: str,
        feed_manager=None,
        price_cache: dict | None = None,
        kalshi_client=None,
        polymarket_client=None,
    ) -> list[dict]:
        """Query available executable ask quotes for a market and outcome across venues."""
        quotes = []

        # 1. Check Kalshi WebSocket orderbook / cache
        if feed_manager:
            k_book, k_age = feed_manager.get_orderbook("kalshi", market_key)
            if k_book and k_age is not None and k_age <= 15.0:
                try:
                    from kalshi_api import parse_orderbook, best_yes_ask, best_no_ask
                    parsed = parse_orderbook(k_book)
                    ask = best_yes_ask(parsed) if target_outcome == "yes" else best_no_ask(parsed)
                    if ask and ask[0] > 0:
                        quotes.append({"venue": "kalshi", "price": ask[0], "size": ask[1]})
                except Exception as e:
                    logger.debug("Kalshi WS quote error for %s: %s", market_key, e)

        if not quotes and price_cache:
            cached_k = price_cache.get(("kalshi", market_key))
            if cached_k and (time.time() - cached_k.get("_ts", 0)) <= 15.0:
                ask_price = cached_k.get(f"{target_outcome}_ask")
                ask_size = cached_k.get(f"{target_outcome}_ask_size", 0)
                if ask_price is not None and ask_price > 0:
                    quotes.append({"venue": "kalshi", "price": ask_price, "size": ask_size or 0})

        # 2. Check Polymarket WebSocket orderbook / cache
        if feed_manager:
            pm_book, pm_age = feed_manager.get_orderbook("polymarket", market_key)
            if pm_book and pm_age is not None and pm_age <= 15.0:
                from scans.helpers import _extract_levels_from_book
                best_ask, ask_size, _, _ = _extract_levels_from_book(pm_book)
                if best_ask is not None and best_ask > 0:
                    quotes.append({"venue": "polymarket", "price": best_ask, "size": ask_size})

        if not any(q["venue"] == "polymarket" for q in quotes) and price_cache:
            cached_pm = price_cache.get(("polymarket", market_key))
            if cached_pm and (time.time() - cached_pm.get("_ts", 0)) <= 15.0:
                best_ask = cached_pm.get("best_ask")
                ask_size = cached_pm.get("best_ask_size", 0)
                if best_ask is not None and best_ask > 0:
                    quotes.append({"venue": "polymarket", "price": best_ask, "size": ask_size or 0})

        return quotes
