"""Fee promotional arbitrage scan (Strategy #9).

Re-scores cross-platform near-misses against the current fee schedule and
emits ``FeePromo`` opportunities for any that now clear MIN_NET_ROI. Runs
reactively (when ``config.reload_fee_rates`` reports a rate drop) and on
startup so any captured near-misses get a fresh look.

Calendar tracking lives in ``config.get_promo_expiry`` and
``notifier.WebhookNotifier.notify_promo_warning`` — those handle the
"promo about to expire" warning channel; this module focuses on detection.
"""

import logging

from fees import net_profit_cross_generic
from near_miss_cache import NearMissCache, get_global_cache

logger = logging.getLogger(__name__)


def scan_fee_promo(
    cache: NearMissCache | None = None,
    min_profit: float = 0.005,
) -> list[dict]:
    """Re-score every near-miss with the current fee globals.

    The cache stores cross-platform candidates that fell short of
    ``MIN_NET_ROI`` by a small margin at refinement time. When fees change
    (typically a 0% promo opening on Matchbook, Gemini, or a Polymarket fee
    holiday), some of those candidates flip profitable. We compute fresh
    net profit using ``net_profit_cross_generic`` (which reads the live
    ``config.*_RATE`` globals), then emit anything that now clears
    ``min_profit``.

    Args:
        cache: Near-miss cache to read from. Defaults to the process global.
        min_profit: Minimum net dollar profit to emit.

    Returns:
        List of opportunity dicts with ``type='FeePromo'``, sorted by net
        profit descending. Empty list if no entries qualify.
    """
    cache = cache or get_global_cache()
    entries = cache.snapshot()
    if not entries:
        return []

    opps: list[dict] = []
    for entry in entries:
        # Each cached entry carries the live yes/no prices and platform pair.
        # Re-evaluate using the current fee globals.
        platform_a = entry.get("_platform_a") or entry.get("_platform_yes")
        platform_b = entry.get("_platform_b") or entry.get("_platform_no")
        price_a = entry.get("_price_a")
        price_b = entry.get("_price_b")
        if price_a is None:
            price_a = entry.get("_yes_price")
        if price_b is None:
            price_b = entry.get("_no_price")
        side_a = entry.get("_side_a", "yes")
        side_b = entry.get("_side_b", "no")
        if not platform_a or not platform_b or price_a is None or price_b is None:
            continue
        try:
            result = net_profit_cross_generic(
                price_a, price_b, side_a, side_b,
                platform_a=platform_a, platform_b=platform_b,
            )
        except Exception as exc:
            logger.debug("fee_promo: skip entry %s — %s",
                         entry.get("_market_key"), exc)
            continue
        new_profit = result.get("net_profit", 0.0)
        if new_profit < min_profit:
            continue

        opp = {
            **entry,
            "type": "FeePromo",
            "_layer": 2,  # near-arb, same as StalePriceOpp / ResolutionSnipeOpp
            "net_profit": new_profit,
            "fees": f"${result.get('fees', 0.0):.4f}",
            "_promo_original_profit": entry.get("net_profit", 0.0),
            "_promo_uplift": new_profit - entry.get("net_profit", 0.0),
        }
        # Drop the near-miss bookkeeping fields so the executor sees a clean opp
        opp.pop("_near_miss_ts", None)
        opp.pop("_near_miss_gap", None)
        opps.append(opp)

    opps.sort(key=lambda o: o.get("net_profit", 0.0), reverse=True)
    if opps:
        logger.info("fee_promo scan: %d cached near-misses now profitable", len(opps))
    return opps
