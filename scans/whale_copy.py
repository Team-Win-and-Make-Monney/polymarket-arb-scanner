"""Whale copy trading strategy via on-chain wallet monitoring on Polygonscan."""

import logging
import time

from whale_copy_decoder import (
    CalldataDecodeError,
    decode_calldata,
    extract_whale_trade,
)

logger = logging.getLogger(__name__)

# Polymarket CLOB contract address on Polygon chain
POLYMARKET_CLOB_ADDRESS = "0x4bfb41d5b3570defd03c39a9a4d8de6bd8b8982e"

# Latency budget: 30 seconds from whale transaction detection to execution
WHALE_COPY_LATENCY_BUDGET_SECONDS = 30


# ---------------------------------------------------------------------------
# Stage 1: Whale transaction polling and parsing
# ---------------------------------------------------------------------------


def _parse_clob_transaction(tx: dict, whale_address: str) -> dict | None:
    """Extract trade details from a CLOB contract transaction.

    Decodes the transaction's calldata via ``whale_copy_decoder`` to surface
    the whale's effective trade direction, token ID, size, and price for
    every supported CTF Exchange method:

    - ``fillOrder`` — populates ``_whale_side``, ``_whale_token_id``,
      ``_whale_size``, ``_whale_price``, and ``_whale_role``.
    - ``fillOrders`` / ``matchOrders`` — opportunity is tagged with
      ``_whale_method`` and the raw decoded args; per-order extraction
      is left to downstream consumers (the order list can be large).
    - ``cancelOrder`` / ``cancelOrders`` — returns ``None`` (cancels are
      not copy-tradeable signals).
    - Calldata that doesn't match a known method — returns ``None``
      (e.g. random ERC20 transfers misrouted to the contract).

    Args:
        tx: Transaction dict from Polygonscan with keys:
            - hash: Transaction hash
            - blockNumber: Block number
            - timeStamp: Unix timestamp
            - to: Recipient address
            - input: Calldata (encoded function + params)
            - isError: "0" if success, "1" if reverted
        whale_address: The whale wallet address that initiated the trade.

    Returns:
        Opportunity dict with type="WhaleCopy", or None if not a valid
        copy-tradeable signal.
    """
    # Verify this is a successful transaction (not a revert)
    if tx.get("isError") == "1":
        return None

    tx_hash = tx.get("hash")
    try:
        whale_timestamp = int(tx.get("timeStamp", 0))
        whale_block = int(tx.get("blockNumber", 0))
    except (TypeError, ValueError):
        logger.debug("WhaleCopy: invalid timestamp/block for tx %s", tx_hash)
        return None

    base_opp: dict = {
        "type": "WhaleCopy",
        "_whale_address": whale_address,
        "_whale_tx_hash": tx_hash,
        "_whale_timestamp": whale_timestamp,
        "_whale_block": whale_block,
        "_market_key": tx_hash,  # tx hash is unique per CTF call
        "_layer": 4,
    }

    raw_input = tx.get("input")
    try:
        decoded = decode_calldata(raw_input)
    except CalldataDecodeError as e:
        logger.debug("WhaleCopy: malformed calldata for tx %s: %s", tx_hash, e)
        return None

    if decoded is None:
        # Calldata doesn't match any known CTF Exchange method — skip.
        return None

    method = decoded.get("method")
    base_opp["_whale_method"] = method

    if method in ("cancelOrder", "cancelOrders"):
        # Cancels are not copy-tradeable; surface them only at debug level.
        logger.debug("WhaleCopy: skipping %s tx %s", method, tx_hash)
        return None

    if method == "fillOrder":
        trade = extract_whale_trade(decoded, whale_address)
        if trade is None:
            return None
        base_opp.update({
            "_whale_role": trade["whale_role"],
            "_whale_side": trade["whale_side"],
            "_whale_token_id": trade["token_id"],
            "_whale_size": trade["token_amount"],
            "_whale_price": trade["price"],
            "_whale_counterparty": trade["maker_address"],
            "_fill_amount_raw": trade["fill_amount_raw"],
        })
        base_opp["market"] = (
            f"Whale {trade['whale_side']} token {trade['token_id'][:12]}... "
            f"size={trade['token_amount']:.2f} @ {trade.get('price') or 0:.3f}"
        )
        # Use token_id as the market key so cross-platform pairing can match.
        base_opp["_market_key"] = trade["token_id"]
        return base_opp

    # fillOrders / matchOrders — surface aggregate metadata for downstream
    # callers to iterate. We do not collapse multi-order calls here because
    # each maker order may target a different market.
    args = decoded.get("args", {})
    if method == "fillOrders":
        order_count = len(args.get("orders", []))
    else:  # matchOrders
        order_count = len(args.get("makerOrders", [])) + 1
    base_opp["_whale_order_count"] = order_count
    base_opp["_decoded_args"] = args
    base_opp["market"] = (
        f"Whale {method} ({order_count} orders) from {whale_address[:8]}... "
        f"at block {tx.get('blockNumber')}"
    )
    return base_opp


def scan_whale_copy(
    whale_wallets: list[str],
    polygonscan_client,
    last_block_cache: dict | None = None,
    fetch_order_book=None,
) -> list[dict]:
    """Stage 1 + 2: Scan whale wallets and refine with market prices.

    Polls Polygonscan API for recent transactions from tracked whale wallets,
    filters to CLOB contract interactions, parses trade details, then refines
    with current market prices and revalidation checks.

    Args:
        whale_wallets: List of whale wallet addresses (0x...).
        polygonscan_client: PolygonscanClient instance for fetching transactions.
        last_block_cache: Dict mapping wallet_address -> last_seen_block_number.
                         Updated in-place as transactions are processed.

    Returns:
        List of refined opportunity dicts with type="WhaleCopy".
    """
    if not whale_wallets or polygonscan_client is None:
        return []

    if last_block_cache is None:
        last_block_cache = {}

    opportunities = []

    # Stage 1: Fetch and parse whale transactions
    for wallet in whale_wallets:
        try:
            # Fetch latest transactions for this wallet
            start_block = last_block_cache.get(wallet, 0)
            txs = polygonscan_client.get_latest_transactions(
                address=wallet,
                start_block=start_block,
                sort="desc",  # Newest first
            )

            if not txs:
                continue

            # Filter to CLOB contract interactions
            clob_txs = [
                tx for tx in txs
                if tx.get("to", "").lower() == POLYMARKET_CLOB_ADDRESS.lower()
            ]

            # Parse each CLOB transaction into an opportunity
            for tx in clob_txs:
                opp = _parse_clob_transaction(tx, wallet)
                if opp:
                    opportunities.append(opp)

            # Update last seen block for this wallet (for next scan cycle)
            if txs:
                try:
                    highest_block = int(txs[0].get("blockNumber", 0))
                except (TypeError, ValueError):
                    logger.warning("Invalid block number returned for whale wallet %s", wallet)
                else:
                    last_block_cache[wallet] = highest_block
                    logger.debug(
                        "Updated block cache for %s: %d", wallet, highest_block
                    )

        except Exception as e:
            logger.warning("Failed to fetch whale wallet %s: %s", wallet, str(e))
            # Graceful degradation: skip this wallet and continue with others
            continue

    logger.info(
        "Whale copy: found %d opportunities from %d wallets",
        len(opportunities),
        len(whale_wallets),
    )

    # Stage 2: Refine with market prices and latency checks
    opportunities = _refine_whale_copy_with_prices(
        opportunities, fetch_order_book=fetch_order_book,
    )

    return opportunities


# ---------------------------------------------------------------------------
# Stage 2: Market price refinement and revalidation
# ---------------------------------------------------------------------------


def _refine_whale_copy_with_prices(
    opportunities: list[dict],
    fetch_order_book=None,
    max_trade_size: float | None = None,
    layer_floor: float | None = None,
    min_liquidity: float | None = None,
    current_time: float | None = None,
) -> list[dict]:
    """Stage 2: First-class refinement of whale copy opportunities.

    Builds on PR C's decoded ``_whale_*`` fields. Each opportunity is
    gated through:

    1. **Latency** — whale transaction younger than
       ``WHALE_COPY_LATENCY_BUDGET_SECONDS`` (existing gate, kept).
    2. **Decoded fields present** — drops fillOrders/matchOrders aggregate
       opps and any opp missing ``_whale_token_id`` / ``_whale_side`` /
       ``_whale_price``. Per-order fan-out for multi-order calls is the
       scan layer's job, not the refiner's.
    3. **Live CLOB ask** — fetches the current Polymarket order book for
       the whale's tokenId. Drops the opp if the book is empty, the ask
       is missing, or the fetch raises.
    4. **Price-move floor** — if the current ask has moved against the
       whale's entry by more than the Layer 4 reval floor (default 10%),
       drop. Direction-aware: BUY copies are dropped when the ask moved
       UP, SELL copies are dropped when the bid moved DOWN.
    5. **Liquidity / depth** — drops opps where the visible book size on
       our side is below ``min_liquidity``.
    6. **Size cap** — clamps the copy size against
       ``WHALE_COPY_MAX_TRADE_SIZE`` and writes ``_copy_size_capped`` so
       the executor consumes the bounded size. The original whale size
       is preserved on ``_whale_size``.

    Args:
        opportunities: List of opportunity dicts from Stage 1.
        fetch_order_book: Callable taking a token_id and returning an
            order-book dict (or None on failure). Defaults to
            ``polymarket_api.fetch_order_book`` when omitted; injectable
            for tests.
        max_trade_size: Override for ``config.WHALE_COPY_MAX_TRADE_SIZE``.
        layer_floor: Override for ``config.REVAL_FLOORS[4]`` (default 10%).
        min_liquidity: Override for ``config.MIN_LIQUIDITY``.
        current_time: Optional fixed timestamp for deterministic tests.

    Returns:
        Refined list of opportunities with ``_current_ask``,
        ``_current_bid``, ``_clob_depth``, ``_copy_size_capped`` populated.
    """
    if not opportunities:
        return opportunities

    if current_time is None:
        current_time = time.time()

    if fetch_order_book is None:
        from polymarket_api import fetch_order_book as _fetch
        from polymarket_api import get_best_bid_ask as _best
        fetch_order_book = _fetch  # type: ignore[assignment]
        _get_best_bid_ask = _best
    else:
        # Test path supplies its own fetcher; rely on a thin local
        # bid/ask extractor that matches polymarket_api's contract.
        _get_best_bid_ask = _local_best_bid_ask

    if max_trade_size is None:
        from config import WHALE_COPY_MAX_TRADE_SIZE
        max_trade_size = float(WHALE_COPY_MAX_TRADE_SIZE)
    if layer_floor is None:
        from config import REVAL_FLOORS
        layer_floor = float(REVAL_FLOORS.get(4, 0.10))
    if min_liquidity is None:
        from config import MIN_LIQUIDITY
        min_liquidity = float(MIN_LIQUIDITY)

    refined: list[dict] = []
    for opp in opportunities:
        # 1. Latency.
        age = current_time - float(opp.get("_whale_timestamp", 0) or 0)
        if age > WHALE_COPY_LATENCY_BUDGET_SECONDS:
            logger.debug(
                "WhaleCopy dropped: stale trade (%.1fs old)", age,
            )
            continue

        # 2. Decoded fields present.
        token_id = opp.get("_whale_token_id")
        side = opp.get("_whale_side")
        whale_price = opp.get("_whale_price")
        whale_size = opp.get("_whale_size")
        method = opp.get("_whale_method", "fillOrder")

        if method != "fillOrder" or not token_id or side not in ("BUY", "SELL"):
            logger.debug(
                "WhaleCopy dropped: not a single-order fill or missing "
                "decoded fields (method=%s token=%s side=%s)",
                method, token_id, side,
            )
            continue

        if not isinstance(whale_price, (int, float)) or whale_price <= 0:
            logger.debug(
                "WhaleCopy dropped: missing/invalid _whale_price for token %s",
                token_id,
            )
            continue

        # 3. Live CLOB ask/bid fetch.
        try:
            book = fetch_order_book(token_id)
        except Exception as e:
            logger.debug(
                "WhaleCopy CLOB fetch failed for token %s: %s", token_id, e,
            )
            continue

        if not book:
            logger.debug("WhaleCopy dropped: empty CLOB book for %s", token_id)
            continue

        bid_ask = _get_best_bid_ask(book) or {}
        live_ask = bid_ask.get("ask")
        live_bid = bid_ask.get("bid")
        ask_size = bid_ask.get("ask_size", 0) or 0
        bid_size = bid_ask.get("bid_size", 0) or 0

        # When copying a BUY we hit the ask; when copying a SELL we hit the bid.
        our_side_price = live_ask if side == "BUY" else live_bid
        our_side_size = ask_size if side == "BUY" else bid_size

        if our_side_price is None or our_side_price <= 0:
            logger.debug(
                "WhaleCopy dropped: no %s-side liquidity for token %s",
                "ask" if side == "BUY" else "bid", token_id,
            )
            continue

        # 4. Price-move floor.
        # BUY copy moves AGAINST us if the ask is higher than the whale's price.
        # SELL copy moves AGAINST us if the bid is lower than the whale's price.
        if side == "BUY":
            move = (our_side_price - whale_price) / whale_price
        else:
            move = (whale_price - our_side_price) / whale_price
        if move > layer_floor:
            logger.debug(
                "WhaleCopy dropped: price moved %.2f%% past Layer 4 floor "
                "%.2f%% (token %s, side %s, whale=%.4f live=%.4f)",
                move * 100, layer_floor * 100,
                token_id, side, whale_price, our_side_price,
            )
            continue

        # 5. Liquidity gate.
        if our_side_size < min_liquidity:
            logger.debug(
                "WhaleCopy dropped: depth %.2f below min_liquidity %.2f "
                "for token %s", our_side_size, min_liquidity, token_id,
            )
            continue

        # 6. Size cap. Whale size is in token units; cap is in dollars at
        # the current price.
        whale_dollars = float(whale_size or 0) * float(our_side_price)
        capped_dollars = min(whale_dollars, float(max_trade_size))

        opp.update({
            "_current_ask": live_ask,
            "_current_bid": live_bid,
            "_clob_depth": float(our_side_size),
            "_price_move": move,
            "_copy_size_capped": capped_dollars,
            "_market_price": our_side_price,
        })
        refined.append(opp)

    logger.info(
        "WhaleCopy refined: %d of %d opportunities passed Stage 2",
        len(refined), len(opportunities),
    )
    return refined


def _local_best_bid_ask(book: dict) -> dict:
    """Local fallback for tests/fakes that mimic polymarket_api.get_best_bid_ask.

    Accepts either the dict-of-lists shape (``{"asks": [...], "bids": [...]}``)
    used by Polymarket's CLOB or a flat shape with ``best_ask``/``best_bid``
    fields. Returns a normalised ``{"bid", "ask", "bid_size", "ask_size"}``.
    """
    if not isinstance(book, dict):
        return {}
    if "ask" in book or "best_ask" in book:
        return {
            "ask": book.get("best_ask", book.get("ask")),
            "bid": book.get("best_bid", book.get("bid")),
            "ask_size": book.get("best_ask_size", book.get("ask_size", 0)) or 0,
            "bid_size": book.get("best_bid_size", book.get("bid_size", 0)) or 0,
        }
    asks = book.get("asks") or []
    bids = book.get("bids") or []
    out: dict = {"ask": None, "bid": None, "ask_size": 0, "bid_size": 0}
    if asks:
        first = asks[0]
        if isinstance(first, dict):
            out["ask"] = float(first.get("price", 0)) or None
            out["ask_size"] = float(first.get("size", 0))
        elif isinstance(first, (list, tuple)) and len(first) >= 2:
            out["ask"] = float(first[0])
            out["ask_size"] = float(first[1])
    if bids:
        first = bids[0]
        if isinstance(first, dict):
            out["bid"] = float(first.get("price", 0)) or None
            out["bid_size"] = float(first.get("size", 0))
        elif isinstance(first, (list, tuple)) and len(first) >= 2:
            out["bid"] = float(first[0])
            out["bid_size"] = float(first[1])
    return out
