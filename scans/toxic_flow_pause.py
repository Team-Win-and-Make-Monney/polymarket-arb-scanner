"""Toxic Flow Pause observability scan (Strategy ToxicFlowPause).

Surfaces markets that the ``ToxicFlowDetector`` has currently paused due to
adverse-selection (informed-flow) detection. The runtime pause is enforced
inside ``MarketMaker.refresh_quotes`` — this scan exposes the same state so
operators can monitor paused markets without diffing logs.

Returns ``[]`` whenever ``MM_TOXIC_FLOW_ENABLED`` is false so an always-on
call in the orchestrator is a cheap no-op.
"""

import logging

logger = logging.getLogger(__name__)


def scan_toxic_flow_pause(
    market_keys: list[str] | None = None,
    detector=None,
    spot_deltas: dict[str, dict] | None = None,
    jev_client=None,
) -> list[dict]:
    """Emit ToxicFlowPause defensive opportunities for currently-paused markets.

    Args:
        market_keys: Markets to inspect. If empty/None, returns ``[]`` (we
            cannot enumerate markets ourselves without coupling to the live
            MarketMaker registry).
        detector: Optional ``ToxicFlowDetector`` instance. Defaults to the
            module singleton ``market_maker.get_toxic_flow_detector()``.
        spot_deltas: Optional mapping of market_key -> {"asset": str, "spot_delta_pct": float, "fill_skew": float}
            for proactive Jev-powered adverse selection evaluation.
        jev_client: Optional JevClient instance for spot toxicity evaluation.

    Returns:
        List of ToxicFlowPause opportunity dicts (possibly empty).
    """
    from config import MM_TOXIC_FLOW_ENABLED

    if not MM_TOXIC_FLOW_ENABLED:
        return []

    if not market_keys:
        return []

    if detector is None:
        from market_maker import get_toxic_flow_detector
        detector = get_toxic_flow_detector()

    # Proactively evaluate adverse selection via Jev System One
    if hasattr(detector, "evaluate_spot_toxicity_with_jev"):
        active_deltas = spot_deltas
        if active_deltas is None:
            # Proactively derive spot deltas from live spot feed if Jev is available
            try:
                from config import OPENROUTER_API_KEY
                if OPENROUTER_API_KEY:
                    from scans.jev_crypto import fetch_spot_prices
                    spots = fetch_spot_prices()
                    if spots:
                        active_deltas = {}
                        import re
                        for mk in market_keys:
                            mk_lower = mk.lower()
                            matched_asset = None
                            for sym in ("btc", "bitcoin"):
                                if re.search(rf"\b{sym}\b", mk_lower):
                                    matched_asset = "BTC"
                                    break
                            if not matched_asset:
                                for sym in ("eth", "ethereum"):
                                    if re.search(rf"\b{sym}\b", mk_lower):
                                        matched_asset = "ETH"
                                        break
                            if not matched_asset:
                                for sym in ("sol", "solana"):
                                    if re.search(rf"\b{sym}\b", mk_lower):
                                        matched_asset = "SOL"
                                        break
                            if not matched_asset:
                                for sym in ("xrp", "ripple"):
                                    if re.search(rf"\b{sym}\b", mk_lower):
                                        matched_asset = "XRP"
                                        break
                            if matched_asset and matched_asset in spots:
                                sinfo = spots[matched_asset]
                                change_pct = float(sinfo.get("change_24h", 0.0))
                                active_deltas[mk] = {
                                    "asset": matched_asset,
                                    "spot_delta_pct": change_pct,
                                    "fill_skew": 0.0,
                                }
            except Exception as e:
                logger.debug("Automatic spot delta lookup for toxic flow failed: %s", e)

        if active_deltas:
            for market_key in market_keys:
                if market_key in active_deltas and not detector.should_pause(market_key):
                    ctx = active_deltas[market_key]
                    asset = ctx.get("asset", "CRYPTO")
                    spot_delta_pct = float(ctx.get("spot_delta_pct", 0.0))
                    fill_skew = float(ctx.get("fill_skew", 0.0))
                    detector.evaluate_spot_toxicity_with_jev(
                        market_key=market_key,
                        asset=asset,
                        spot_delta_pct=spot_delta_pct,
                        recent_fill_skew=fill_skew,
                        client=jev_client,
                    )

    opps: list[dict] = []
    for market_key in market_keys:
        if not market_key:
            continue
        if not detector.should_pause(market_key):
            continue

        toxicity = detector.get_toxicity(market_key)
        pause_remaining = detector.get_pause_remaining(market_key)
        reason = getattr(detector, "get_pause_reason", lambda k: "")(market_key)

        price_summary = f"toxicity={toxicity:.2f} remaining={pause_remaining:.0f}s"
        if reason:
            price_summary = f"{price_summary} ({reason[:40]})"

        opps.append({
            "type": "ToxicFlowPause",
            "_layer": 3,
            "market": f"{market_key[:60]} (toxic flow pause)",
            "prices": price_summary,
            "total_cost": "$0.00",
            "net_profit": 0.0,
            "net_roi": 0.0,
            "confidence": 0.90,
            "reason": reason,
            "_market_key": market_key,
            "_toxicity": toxicity,
            "_pause_remaining_seconds": pause_remaining,
            "_reason": reason,
        })

    return opps
