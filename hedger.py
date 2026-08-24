"""Partial fill hedger — sells filled legs when the other side fails."""

import logging
import time

from config import HEDGE_MAX_ATTEMPTS, HEDGE_MAX_SPREAD_LOSS_PCT
from db import TradeDB

logger = logging.getLogger(__name__)


class PartialFillHedger:
    """Attempts to sell filled legs to recover capital after partial fills."""

    def __init__(
        self,
        pm_trader=None,
        kalshi_client=None,
        betfair_client=None,
        smarkets_client=None,
        sxbet_client=None,
        matchbook_client=None,
        gemini_client=None,
        ibkr_client=None,
        db: TradeDB = None,
    ):
        self.pm_trader = pm_trader
        self.kalshi_client = kalshi_client
        self.betfair_client = betfair_client
        self.smarkets_client = smarkets_client
        self.sxbet_client = sxbet_client
        self.matchbook_client = matchbook_client
        self.gemini_client = gemini_client
        self.ibkr_client = ibkr_client
        # Note: IBKR accepted for test coverage but cannot hedge (BUY-only platform)
        self.db = db

    def queue_hedge(
        self,
        trade_id: int,
        platform: str,
        token_id: str,
        side: str,
        fill_price: float,
        size: float,
        opportunity_id: int,
        market_id: str | None = None,
        selection_id: str | None = None,
        contract_id: str | None = None,
        market_hash: str | None = None,
        runner_id: str | None = None,
        outcome_id: str | None = None,
        symbol: str | None = None,
    ):
        """Record a partial fill for hedging.

        Platform-specific identifiers must be passed through so Betfair,
        Smarkets, SX Bet, Matchbook, and Gemini hedges can locate the market.
        """
        if self.db:
            self.db.log_partial_fill(
                trade_id=trade_id,
                opportunity_id=opportunity_id,
                platform=platform,
                token_id=token_id,
                side=side,
                fill_price=fill_price,
                size=size,
                market_id=market_id,
                selection_id=selection_id,
                contract_id=contract_id,
                market_hash=market_hash,
                runner_id=runner_id,
                outcome_id=outcome_id,
                symbol=symbol,
            )
            logger.info("Queued hedge for trade #%d on %s (fill=$%.3f)", trade_id, platform, fill_price)

    def process_pending_hedges(self):
        """Process all pending partial fills — attempt to sell each."""
        if not self.db:
            return
        pending = self.db.get_pending_partial_fills()
        if not pending:
            return

        logger.info("Processing %d pending hedges...", len(pending))
        for pf in pending:
            pf_id = pf["id"]
            attempts = pf.get("hedge_attempts", 0)
            if attempts >= HEDGE_MAX_ATTEMPTS:
                self.db.update_partial_fill(pf_id, "failed", attempts)
                logger.warning("Hedge #%d exceeded max attempts (%d). Marking failed.", pf_id, HEDGE_MAX_ATTEMPTS)
                continue

            success = self._attempt_hedge(pf)
            if success:
                self.db.update_partial_fill(pf_id, "hedged", attempts + 1)
                logger.info("Hedge #%d successful.", pf_id)
            else:
                self.db.update_partial_fill(pf_id, "pending", attempts + 1)
                logger.info("Hedge #%d attempt %d failed. Will retry.", pf_id, attempts + 1)

    def _attempt_hedge(self, pf: dict) -> bool:
        """Attempt to sell a partial fill position.

        Strategy: Fetch current bid, sell if loss < HEDGE_MAX_SPREAD_LOSS_PCT
        of fill price. Returns True if sell succeeded.
        """
        platform = pf["platform"]
        token_id = pf.get("token_id", "")
        fill_price = pf["fill_price"]
        size = pf["size"]
        max_loss = fill_price * HEDGE_MAX_SPREAD_LOSS_PCT

        try:
            if platform == "polymarket":
                return self._hedge_polymarket(token_id, fill_price, size, max_loss)
            elif platform == "kalshi":
                return self._hedge_kalshi(token_id, fill_price, size, max_loss,
                                          pf.get("side", "yes"),
                                          action=pf.get("_reduce_action", "sell"),
                                          max_contracts=pf.get("_max_contracts"))
            elif platform == "betfair":
                return self._hedge_betfair(pf, fill_price, size, max_loss)
            elif platform == "smarkets":
                return self._hedge_smarkets(pf, fill_price, size, max_loss)
            elif platform == "sxbet":
                return self._hedge_sxbet(pf, fill_price, size, max_loss)
            elif platform == "matchbook":
                return self._hedge_matchbook(pf, fill_price, size, max_loss)
            elif platform == "gemini":
                return self._hedge_gemini(pf, fill_price, size, max_loss)
            # IBKR: cannot hedge — BUY-only platform, no sell capability
        except Exception as e:
            logger.warning("Hedge attempt failed for %s on %s: %s", token_id, platform, e)

        return False

    def _hedge_polymarket(self, token_id: str, fill_price: float, size: float, max_loss: float) -> bool:
        """Sell a Polymarket position at current bid."""
        if not self.pm_trader:
            return False
        from polymarket_api import fetch_order_book, get_best_bid_ask
        book = fetch_order_book(token_id)
        if not book:
            return False
        ba = get_best_bid_ask(book)
        bid = ba.get("bid")
        if bid is None or bid <= 0:
            return False
        loss = fill_price - bid
        if loss > max_loss:
            logger.info("Polymarket hedge: bid $%.3f too far from fill $%.3f (loss $%.3f > max $%.3f)",
                        bid, fill_price, loss, max_loss)
            return False
        resp = self.pm_trader.place_order(token_id=token_id, side="SELL", price=bid, size=size)
        return bool(resp and resp.get("success"))

    def _hedge_kalshi(self, ticker: str, fill_price: float, size: float, max_loss: float,
                      side: str, action: str = "sell",
                      max_contracts: int | None = None) -> bool:
        """Reduce a Kalshi position at the current touch.

        ``action="sell"`` (default, original behavior) sells an over-long
        position into the best bid on ``side``. ``action="buy"`` buys back an
        over-short position at the best ask on ``side`` (plan 10: the
        buy-to-reduce direction — the ask is derived from the opposite side's
        best bid since Kalshi orderbooks are bids-only).

        Args:
            max_contracts: Hard ceiling on the contract count, independent
                of the dollars/touch sizing math below. ``size`` is a DOLLAR
                excess computed against the entry/reference price, while
                ``touch`` is the CURRENT best bid/ask — if price has moved
                since entry, ``size / touch`` can compute a count that
                EXCEEDS actual holdings, turning a reduce into a flip to the
                opposite side. When provided (the MM pilot always provides
                its own inventory-tracked position size here), the count is
                clamped so a reduce/hedge order can never be sized to exceed
                the position it is reducing. ``max_contracts <= 0`` means
                there is nothing left to reduce — no order is placed.
        """
        if not self.kalshi_client:
            return False
        if max_contracts is not None and max_contracts <= 0:
            logger.info("Kalshi hedge: max_contracts=%s — nothing to reduce, "
                       "skipping %s %s on %s", max_contracts, action, side, ticker)
            return False
        from kalshi_policy import live_kalshi_submit_allowed
        if not live_kalshi_submit_allowed(ticker, reducing=True):
            logger.warning("Kalshi hedge blocked by live policy: %s", ticker)
            return False
        book = self.kalshi_client.fetch_order_book(ticker)
        if not book:
            return False
        # Selling our YES position requires hitting the best YES bid (and
        # symmetrically for NO). best_*_bid returns the highest bid + depth.
        # Buying back a short hits the best ask instead (best_*_ask helpers).
        from kalshi_api import (parse_orderbook, best_yes_bid, best_no_bid,
                                best_yes_ask, best_no_ask, _audit_raw_orderbook)
        _audit_raw_orderbook(ticker, book)
        parsed = parse_orderbook(book)
        if action == "buy":
            touch_info = best_yes_ask(parsed) if side == "yes" else best_no_ask(parsed)
        else:
            touch_info = best_yes_bid(parsed) if side == "yes" else best_no_bid(parsed)
        if touch_info is None:
            return False
        touch = touch_info[0]
        if touch <= 0:
            return False
        # Loss check: selling below the fill price loses money; buying back
        # above the (short-entry) fill price loses money.
        loss = (touch - fill_price) if action == "buy" else (fill_price - touch)
        if loss > max_loss:
            return False
        count = max(1, int(size / touch)) if touch > 0 else 1
        if max_contracts is not None and count > max_contracts:
            logger.info("Kalshi hedge: clamping reduce count %d -> %d "
                       "(actual position size) on %s — price moved since "
                       "entry, dollars/touch sizing overshot", count,
                       max_contracts, ticker)
            count = max_contracts
        resp = self.kalshi_client.place_order(ticker=ticker, side=side, action=action,
                                               count=count, price_dollars=touch,
                                               reducing=True)
        return resp is not None

    def _hedge_betfair(self, pf: dict, fill_price: float, size: float, max_loss: float) -> bool:
        """Hedge a Betfair position with an opposing bet."""
        if not self.betfair_client or not self.betfair_client.authenticated:
            return False
        # For Betfair, hedging means placing the opposite bet (LAY if we BACKed)
        market_id = pf.get("_market_id", "")
        selection_id = pf.get("_selection_id")
        if not market_id or not selection_id:
            return False
        original_side = pf.get("side", "BACK")
        hedge_side = "LAY" if original_side == "BACK" else "BACK"
        decimal_odds = round(1.0 / fill_price, 2) if fill_price > 0 else 2.0
        instructions = [{
            "selectionId": selection_id,
            "side": hedge_side,
            "orderType": "LIMIT",
            "limitOrder": {
                "size": round(size, 2),
                "price": decimal_odds,
                "persistenceType": "LAPSE",
            },
        }]
        resp = self.betfair_client.place_orders(market_id, instructions)
        return bool(resp and resp.get("status") == "SUCCESS")

    def _hedge_smarkets(self, pf: dict, fill_price: float, size: float, max_loss: float) -> bool:
        """Hedge a Smarkets position with an opposing bet."""
        if not self.smarkets_client or not self.smarkets_client.authenticated:
            return False
        market_id = pf.get("_market_id", "")
        contract_id = pf.get("_contract_id", "")
        if not market_id:
            return False
        original_side = pf.get("side", "BACK")
        hedge_side = "LAY" if original_side == "BACK" else "BACK"
        quantity = max(1, int(size / fill_price)) if fill_price > 0 else 1
        resp = self.smarkets_client.place_order(
            market_id=market_id, contract_id=contract_id,
            side=hedge_side, price=fill_price, quantity=quantity,
        )
        return resp is not None

    def _hedge_sxbet(self, pf: dict, fill_price: float, size: float, max_loss: float) -> bool:
        """Hedge an SX Bet position with an opposing bet."""
        if not self.sxbet_client or not self.sxbet_client.authenticated:
            return False
        market_hash = pf.get("_market_hash", "")
        outcome_id = pf.get("_outcome_id", "")
        if not market_hash:
            return False
        original_side = pf.get("side", "BACK")
        hedge_side = "LAY" if original_side == "BACK" else "BACK"
        quantity = max(1, int(size / fill_price)) if fill_price > 0 else 1
        resp = self.sxbet_client.place_order(
            market_hash=market_hash, outcome_id=outcome_id,
            side=hedge_side, price=fill_price, quantity=quantity,
        )
        return resp is not None

    def _hedge_gemini(self, pf: dict, fill_price: float, size: float, max_loss: float) -> bool:
        """Hedge a Gemini position by selling at market (IOC at worst ask)."""
        if not self.gemini_client or not self.gemini_client.authenticated:
            return False
        symbol = pf.get("_symbol") or pf.get("token_id", "")
        if not symbol:
            return False
        # Fetch current order book to get best bid
        book = self.gemini_client.get_order_book(symbol, limit=1)
        if not book or not book.get("bids"):
            return False
        bid = book["bids"][0].get("price", 0)
        if bid <= 0:
            return False
        loss = fill_price - bid
        if loss > max_loss:
            logger.info("Gemini hedge: bid $%.3f too far from fill $%.3f (loss $%.3f > max $%.3f)",
                        bid, fill_price, loss, max_loss)
            return False
        outcome = pf.get("side", "yes").lower()
        if outcome not in ("yes", "no"):
            outcome = "yes"
        quantity = max(1, int(size / bid)) if bid > 0 else 1
        resp = self.gemini_client.place_order(
            symbol=symbol, side="sell", outcome=outcome,
            quantity=quantity, price=bid, time_in_force="immediate-or-cancel",
        )
        return resp is not None

    def _hedge_matchbook(self, pf: dict, fill_price: float, size: float, max_loss: float) -> bool:
        """Hedge a Matchbook position with an opposing bet."""
        if not self.matchbook_client or not self.matchbook_client.authenticated:
            return False
        market_id = pf.get("_market_id", "")
        runner_id = pf.get("_runner_id", "")
        if not market_id or not runner_id:
            return False
        original_side = pf.get("side", "back")
        hedge_side = "lay" if original_side.lower() == "back" else "back"
        decimal_odds = round(1.0 / fill_price, 2) if fill_price > 0 else 2.0
        resp = self.matchbook_client.place_order(
            market_id=market_id, runner_id=runner_id,
            side=hedge_side, odds=decimal_odds, stake=round(size, 2),
        )
        return resp is not None

    def hedge_inventory(
        self,
        market_key: str,
        platform: str,
        side: str,
        fill_price: float,
        size: float,
        token_id: str = "",
        max_contracts: int | None = None,
        **identifiers,
    ) -> bool:
        """Hedge an MM inventory imbalance by selling at current best bid.

        Triggered by MarketMaker.on_fill when InventoryTracker.needs_hedge is True.
        Reuses the same per-platform _hedge_* logic as partial-fill recovery,
        plus a DB audit row in the partial_fills table flagged as
        ``opportunity_id=-1`` so MM hedges remain queryable separately.

        Args:
            market_key: Market identifier (token_id, ticker, market hash, etc.).
            platform: Platform that holds the over-position.
            side: Side that was filled (we want to sell this exposure).
            fill_price: Price the inventory was acquired at.
            size: Size of the imbalance to hedge in dollars.
            token_id: Polymarket / Gemini token symbol (if applicable).
            max_contracts: Kalshi only — hard ceiling on the reduce order's
                contract count, sourced from the caller's own authoritative
                inventory tracker (not recomputed from dollars/price here).
                Prevents a reduce order from being sized past the position
                it is reducing when price has moved since entry (see
                ``_hedge_kalshi``). ``None`` leaves the count unclamped
                (non-pilot / non-Kalshi callers).
            **identifiers: Extra platform-specific keys forwarded to ``pf``
                (``market_id``, ``selection_id``, ``contract_id``,
                ``market_hash``, ``runner_id``, ``outcome_id``, ``symbol``).

        Returns:
            True if the hedge order placed successfully, False otherwise.
        """
        pf = {
            "platform": platform,
            "token_id": token_id or market_key,
            "side": side,
            "fill_price": fill_price,
            "size": size,
            "_market_id": identifiers.get("market_id", ""),
            "_selection_id": identifiers.get("selection_id"),
            "_contract_id": identifiers.get("contract_id", ""),
            "_market_hash": identifiers.get("market_hash", ""),
            "_runner_id": identifiers.get("runner_id", ""),
            "_outcome_id": identifiers.get("outcome_id", ""),
            "_symbol": identifiers.get("symbol", token_id or market_key),
            # Plan 10: "sell" reduces an over-long position at the bid;
            # "buy" reduces an over-short position at the ask (Kalshi only).
            "_reduce_action": identifiers.get("reduce_action", "sell"),
            "_max_contracts": max_contracts,
        }
        success = self._attempt_hedge(pf)
        logger.info(
            "MM inventory hedge %s: %s %s %s size=$%.2f fill=$%.4f",
            "OK" if success else "FAILED",
            platform, side, market_key, size, fill_price,
        )
        if self.db:
            self.db.log_partial_fill(
                trade_id=0,
                opportunity_id=-1,
                platform=platform,
                token_id=token_id or market_key,
                side=side,
                fill_price=fill_price,
                size=size,
                market_id=identifiers.get("market_id"),
                selection_id=identifiers.get("selection_id"),
                contract_id=identifiers.get("contract_id"),
                market_hash=identifiers.get("market_hash"),
                runner_id=identifiers.get("runner_id"),
                outcome_id=identifiers.get("outcome_id"),
                symbol=identifiers.get("symbol", token_id or market_key),
            )
        return success
