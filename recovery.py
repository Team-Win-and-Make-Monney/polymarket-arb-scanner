"""Crash recovery for continuous mode.

On startup, checks for orphaned positions (open positions whose trades
may have been interrupted) and attempts to reconcile with platform APIs.
"""

import logging

from db import TradeDB

logger = logging.getLogger(__name__)


def reconcile_orphaned_positions(
    db: TradeDB,
    kalshi_client=None,
    pm_trader=None,
    betfair_client=None,
    smarkets_client=None,
    sxbet_client=None,
    matchbook_client=None,
    gemini_client=None,
    ibkr_client=None,
):
    """Check for orphaned positions and pending trades from a previous crash.

    Orphans are:
    1. Trades stuck in 'pending' status (order may or may not have been placed)
    2. Open positions whose trades have unknown fill status

    For each, query the platform API to determine actual order status,
    then update the DB accordingly.
    """
    # 1. Check for pending trades (interrupted mid-execution)
    pending_trades = db.get_pending_trades()
    if pending_trades:
        logger.info("Found %d pending trades from previous session — reconciling...", len(pending_trades))
        _reconcile_pending_trades(db, pending_trades, kalshi_client, pm_trader,
                                 betfair_client, smarkets_client, sxbet_client,
                                 matchbook_client, gemini_client, ibkr_client)

    # 2. Check for open positions with no recent activity
    open_positions = db.get_open_positions()
    if open_positions:
        logger.info("Found %d open positions from previous session.", len(open_positions))
    else:
        logger.debug("No orphaned positions found.")


def _reconcile_pending_trades(
    db: TradeDB,
    pending_trades: list[dict],
    kalshi_client=None,
    pm_trader=None,
    betfair_client=None,
    smarkets_client=None,
    sxbet_client=None,
    matchbook_client=None,
    gemini_client=None,
    ibkr_client=None,
):
    """Attempt to determine the actual status of pending trades."""
    resolved = 0
    for trade in pending_trades:
        trade_id = trade["id"]
        platform = trade["platform"]
        order_id = trade.get("order_id")

        if not order_id:
            # No order_id means the order was never placed — mark as failed
            db.update_trade_status(trade_id, "failed")
            logger.info("Trade #%d: no order_id, marked as failed.", trade_id)
            resolved += 1
            continue

        if platform == "kalshi" and str(order_id).startswith("client:"):
            client_order_id = str(order_id).split(":", 1)[1]
            if kalshi_client is None:
                logger.warning(
                    "Trade #%d: Kalshi client unavailable; submission %s "
                    "remains pending for reconciliation.",
                    trade_id, client_order_id,
                )
                continue
            try:
                order = kalshi_client.find_order_by_client_order_id(
                    client_order_id)
            except Exception as e:
                logger.warning(
                    "Trade #%d: Kalshi client-order reconciliation failed; "
                    "submission remains pending: %s", trade_id, e,
                )
                continue
            venue_order_id = (
                order.get("order_id") or order.get("id")
                if isinstance(order, dict) else None
            )
            if not venue_order_id:
                logger.warning(
                    "Trade #%d: Kalshi submission %s remains indeterminate; "
                    "pending state retained and automatic retry blocked.",
                    trade_id, client_order_id,
                )
                continue
            order_id = str(venue_order_id)
            db.set_trade_order_id(trade_id, order_id)

        # HARDEN-05: Skip trades that have already been reconciled or re-placed.
        # If a sibling trade for the same opportunity is already filled/confirmed,
        # this trade is a duplicate — mark as dedup_skipped and do not re-submit.
        opportunity_id = trade.get("opportunity_id")
        if opportunity_id is not None:
            sibling_trades = db.get_trades_for_opportunity(opportunity_id)
            if any(
                t["status"] in ("filled", "confirmed", "settled")
                for t in sibling_trades
                if t["id"] != trade_id
            ):
                logger.info(
                    "Trade #%d already has a filled sibling — marking dedup_skipped",
                    trade_id,
                )
                db.update_trade_status(trade_id, "dedup_skipped")
                resolved += 1
                continue

        status = _check_order_status(
            platform, order_id,
            kalshi_client=kalshi_client,
            pm_trader=pm_trader,
            betfair_client=betfair_client,
            smarkets_client=smarkets_client,
            sxbet_client=sxbet_client,
            matchbook_client=matchbook_client,
            gemini_client=gemini_client,
            ibkr_client=ibkr_client,
        )

        if status == "filled":
            db.update_trade_status(trade_id, "filled")
            logger.info("Trade #%d on %s: confirmed filled.", trade_id, platform)
            resolved += 1
        elif status == "canceled":
            db.update_trade_status(trade_id, "failed")
            logger.info("Trade #%d on %s: confirmed canceled.", trade_id, platform)
            resolved += 1
        elif status == "unknown":
            # Could not determine — mark as orphaned for manual review
            db.update_trade_status(trade_id, "orphaned")
            logger.warning("Trade #%d on %s: could not determine status, marked orphaned.", trade_id, platform)
            resolved += 1
        # If status is "pending", leave it — the order may still be resting

    # Convert any existing orphaned trades to partial_fill records
    _convert_orphans_to_partial_fills(db)

    if resolved:
        logger.info("Reconciled %d/%d pending trades.", resolved, len(pending_trades))


def _check_order_status(
    platform: str,
    order_id: str,
    kalshi_client=None,
    pm_trader=None,
    betfair_client=None,
    smarkets_client=None,
    sxbet_client=None,
    matchbook_client=None,
    gemini_client=None,
    ibkr_client=None,
) -> str:
    """Query a platform API for the status of an order.

    Returns one of: "filled", "pending", "canceled", "unknown".
    """
    try:
        if platform == "polymarket" and pm_trader:
            resp = pm_trader.get_order_status(order_id)
            if resp:
                s = resp.get("status", "")
                if s == "matched":
                    return "filled"
                elif s in ("canceled", "expired"):
                    return "canceled"
                elif s in ("live", "resting"):
                    return "pending"
            return "unknown"

        elif platform == "kalshi" and kalshi_client:
            resp = kalshi_client.get_order_status(order_id)
            if resp:
                s = resp.get("status", "")
                if s == "executed":
                    return "filled"
                elif s in ("canceled", "expired"):
                    return "canceled"
                elif s == "resting":
                    return "pending"
            return "unknown"

        elif platform == "betfair" and betfair_client:
            resp = betfair_client.get_order_status(order_id)
            if resp:
                s = resp.get("status", "")
                if s == "EXECUTION_COMPLETE":
                    return "filled"
                elif s == "CANCELLED":
                    return "canceled"
                elif s == "EXECUTABLE":
                    return "pending"
            return "unknown"

        elif platform == "smarkets" and smarkets_client:
            resp = smarkets_client.get_order_status(order_id)
            if resp:
                s = resp.get("state", resp.get("status", ""))
                if s in ("matched", "settled", "filled"):
                    return "filled"
                elif s in ("cancelled", "expired"):
                    return "canceled"
                elif s in ("live", "open"):
                    return "pending"
            return "unknown"

        elif platform == "sxbet" and sxbet_client:
            resp = sxbet_client.get_order_status(order_id)
            if resp:
                s = resp.get("status", "")
                if s in ("FILLED", "matched"):
                    return "filled"
                elif s in ("CANCELLED", "EXPIRED"):
                    return "canceled"
                elif s in ("OPEN", "PENDING"):
                    return "pending"
            return "unknown"

        elif platform == "matchbook" and matchbook_client:
            resp = matchbook_client.get_order_status(order_id)
            if resp:
                s = resp.get("status", "")
                if s in ("matched", "settled"):
                    return "filled"
                elif s in ("cancelled", "expired"):
                    return "canceled"
                elif s in ("open", "unmatched"):
                    return "pending"
            return "unknown"

        elif platform == "gemini" and gemini_client:
            resp = gemini_client.get_order_status(order_id)
            if resp:
                s = resp.get("status", "")
                if s in ("filled", "closed"):
                    return "filled"
                elif s in ("cancelled", "expired"):
                    return "canceled"
                elif s in ("live", "open", "accepted"):
                    return "pending"
            return "unknown"

        elif platform == "ibkr" and ibkr_client:
            resp = ibkr_client.get_order_status(order_id)
            if resp:
                s = resp.get("status", "")
                if s in ("Filled", "filled"):
                    return "filled"
                elif s in ("Cancelled", "cancelled", "Inactive"):
                    return "canceled"
                elif s in ("Submitted", "PreSubmitted"):
                    return "pending"
            return "unknown"

    except Exception as e:
        logger.warning("Failed to check order %s on %s: %s", order_id, platform, e)

    return "unknown"


def _convert_orphans_to_partial_fills(db: TradeDB):
    """Convert legacy orphaned trades to partial_fill records for hedging."""
    with db._lock:
        rows = db.conn.execute(
            "SELECT * FROM trades WHERE status = 'orphaned'"
        ).fetchall()

    if not rows:
        return

    logger.info("Converting %d orphaned trades to partial fills...", len(rows))
    for trade in rows:
        trade = dict(trade)
        try:
            db.log_partial_fill(
                trade_id=trade["id"],
                opportunity_id=trade["opportunity_id"],
                platform=trade["platform"],
                token_id=trade.get("order_id", ""),
                side=trade["side"],
                fill_price=trade.get("fill_price") or trade["price"],
                size=trade["size"],
            )
            db.update_trade_status(trade["id"], "hedge_pending")
            logger.info("Converted orphaned trade #%d to partial fill.", trade["id"])
        except Exception as e:
            logger.warning("Failed to convert orphaned trade #%d: %s", trade["id"], e)
