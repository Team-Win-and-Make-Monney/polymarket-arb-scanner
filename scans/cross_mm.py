"""Cross-platform market making scan (Strategy #11).

Posts opposing limit orders on two platforms for the same matched event so
the maker captures the cross-platform spread continuously, without waiting
for a pure cross-platform arbitrage to open.

Pattern: for each pair (platform_a, platform_b) representing the same
underlying market, fetch best bid on the cheaper platform and best ask on
the more expensive platform. If
``ask_high - bid_low - sum_fees > CROSS_MM_MIN_SPREAD``, emit a
``CrossPlatformMM`` opp with both legs pre-built. The executor's
``_build_legs`` branch unpacks them as ``_leg_a`` and ``_leg_b``.

Inventory tracking is handled by ``CrossPlatformMaker`` in
``market_maker.py``; this module is purely the detection layer.
"""

import logging

from fees import net_profit_cross_generic
from .helpers import capital_efficiency_score

logger = logging.getLogger(__name__)


def scan_cross_mm(
    matched_pairs: list[dict],
    depth_data: dict | None = None,
    min_spread: float = 0.04,
    quote_size: float = 5.0,
    platforms_whitelist: tuple[str, ...] = ("polymarket", "kalshi"),
) -> list[dict]:
    """Detect cross-platform MM opportunities.

    Args:
        matched_pairs: Output of ``matcher.match_cross_platform`` —
            list of dicts each with keys including ``platform_a``,
            ``platform_b``, ``market_a``, ``market_b``, plus prices.
        depth_data: Optional ``{(platform, market_key): {"bid": (price, size),
            "ask": (price, size)}}`` mapping. If absent, the function reads
            best bid/ask off the matched pair entries directly.
        min_spread: Minimum cross-platform net spread to emit (default $0.04).
        quote_size: Per-leg quote size in dollars.
        platforms_whitelist: Only emit opps where both platforms are in this set.

    Returns:
        List of ``CrossPlatformMM`` opportunity dicts sorted by net profit.
    """
    if not matched_pairs:
        return []

    opps = []
    for pair in matched_pairs:
        platform_a = pair.get("platform_a") or pair.get("platform_yes")
        platform_b = pair.get("platform_b") or pair.get("platform_no")
        if not platform_a or not platform_b:
            continue
        if platform_a not in platforms_whitelist or platform_b not in platforms_whitelist:
            continue

        market_a = pair.get("market_a", {})
        market_b = pair.get("market_b", {})
        market_key = (
            pair.get("market_key")
            or pair.get("_market_key")
            or market_a.get("conditionId")
            or market_a.get("ticker")
            or ""
        )
        if not market_key:
            continue

        # Pull best bid/ask. Prefer explicit depth_data, otherwise read from
        # the pair entry (matchers may have already attached prices).
        a_bid, a_ask = _best_levels(depth_data, platform_a, market_key, market_a)
        b_bid, b_ask = _best_levels(depth_data, platform_b, market_key, market_b)
        if a_bid is None or a_ask is None or b_bid is None or b_ask is None:
            continue

        # Two candidate orientations: buy on A and sell on B, or vice versa.
        # Pick whichever produces the larger net spread after fees.
        candidates = (
            (platform_a, platform_b, a_bid, b_ask, "yes", "no"),
            (platform_b, platform_a, b_bid, a_ask, "yes", "no"),
        )

        best = None
        for buy_plat, sell_plat, buy_price, sell_price, side_a, side_b in candidates:
            try:
                fee_result = net_profit_cross_generic(
                    buy_price, sell_price, side_a, side_b,
                    platform_a=buy_plat, platform_b=sell_plat,
                )
            except Exception:
                continue
            net_spread = sell_price - buy_price - fee_result.get("fees", 0.0)
            if net_spread < min_spread:
                continue
            if best is None or net_spread > best["spread"]:
                best = {
                    "buy_plat": buy_plat,
                    "sell_plat": sell_plat,
                    "buy_price": buy_price,
                    "sell_price": sell_price,
                    "spread": net_spread,
                    "fees": fee_result.get("fees", 0.0),
                }
        if best is None:
            continue

        leg_a = {
            "platform": best["buy_plat"],
            "side": "BUY",
            "token": "yes",
            "price": best["buy_price"],
            "size": quote_size,
            "_market_key": market_key,
        }
        leg_b = {
            "platform": best["sell_plat"],
            "side": "SELL",
            "token": "yes",
            "price": best["sell_price"],
            "size": quote_size,
            "_market_key": market_key,
        }

        opp = {
            "type": "CrossPlatformMM",
            "_layer": 3,  # Layer 3 — market making
            "market": pair.get("market_title")
                or market_a.get("question")
                or market_a.get("title", market_key),
            "prices": (
                f"{best['buy_plat']}_BID={best['buy_price']:.4f} "
                f"{best['sell_plat']}_ASK={best['sell_price']:.4f}"
            ),
            "total_cost": f"${quote_size * 2:.2f}",
            "net_profit": best["spread"] * quote_size,
            "net_roi": best["spread"] / max(best["buy_price"], 0.01),
            "_market_key": market_key,
            "_platform_a": best["buy_plat"],
            "_platform_b": best["sell_plat"],
            "_spread": best["spread"],
            "_fees": best["fees"],
            "_leg_a": leg_a,
            "_leg_b": leg_b,
        }
        opp["_efficiency"] = capital_efficiency_score(opp)
        opps.append(opp)

    opps.sort(key=lambda o: o["net_profit"], reverse=True)
    if opps:
        logger.info("cross_mm scan: %d paired-quote opportunities", len(opps))
    return opps


def _best_levels(depth_data: dict | None, platform: str, market_key: str,
                 market: dict) -> tuple[float | None, float | None]:
    """Return (best_bid, best_ask) for a given platform + market.

    Falls back to fields on the matched market dict if ``depth_data`` is
    absent or doesn't cover this entry. Returns ``(None, None)`` if neither
    source has both sides of the book.
    """
    if depth_data:
        entry = depth_data.get((platform, market_key))
        if entry:
            bid = entry.get("bid")
            ask = entry.get("ask")
            bid_price = bid[0] if isinstance(bid, (list, tuple)) and bid else bid
            ask_price = ask[0] if isinstance(ask, (list, tuple)) and ask else ask
            if bid_price is not None and ask_price is not None:
                return float(bid_price), float(ask_price)
    # Fallback: market dict carrying yes_bid / yes_ask attached during matching
    bid = market.get("yes_bid") if isinstance(market, dict) else None
    ask = market.get("yes_ask") if isinstance(market, dict) else None
    if bid is not None and ask is not None:
        return float(bid), float(ask)
    return None, None
