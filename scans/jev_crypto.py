"""Jev-powered crypto prediction market scanner.

Evaluates high-volume crypto price prediction contracts (BTC, ETH, SOL)
against live exchange spot prices using TypeSafe's Jev-1.13 System One
decision model.

Conventions:
- Two-stage detection: Stage 1 (mid-price/strike envelope filter) -> Stage 2 (CLOB + Jev refinement)
- Code owns the workflow; Jev acts as a fast probabilistic oracle
- Returns standard opportunity dicts
"""

from __future__ import annotations

import json
import logging
import urllib.request
from datetime import datetime, timezone

from config import (
    JEV_CONFIDENCE_THRESHOLD,
    JEV_CRYPTO_ENABLED,
    JEV_MIN_EDGE,
    MIN_NET_ROI,
)
from fees import net_profit_jev_crypto
from jev_client import JevClient, get_jev_client
from .helpers import _extract_token_ids, _fetch_clob_for_market

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Spot Price Fetcher
# ---------------------------------------------------------------------------


def fetch_spot_prices() -> dict[str, dict]:
    """Fetch live spot prices for major crypto assets from Binance.US (or fallback)."""
    assets = {
        "BTC": "BTCUSDT",
        "ETH": "ETHUSDT",
        "SOL": "SOLUSDT",
        "XRP": "XRPUSDT",
    }
    results = {}

    for symbol, pair in assets.items():
        try:
            url = f"https://api.binance.us/api/v3/ticker/24hr?symbol={pair}"
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=6) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                results[symbol] = {
                    "price": float(data["lastPrice"]),
                    "change_24h": float(data["priceChangePercent"]),
                    "high_24h": float(data["highPrice"]),
                    "low_24h": float(data["lowPrice"]),
                    "vwap_24h": float(data.get("weightedAvgPrice", data["lastPrice"])),
                }
        except Exception as e:
            logger.debug("Failed to fetch spot for %s: %s", symbol, e)

    return results


# ---------------------------------------------------------------------------
# Stage 1: Fast Strike & Regime Filter
# ---------------------------------------------------------------------------


def _extract_strike_and_asset(question: str) -> tuple[str | None, float | None, str | None]:
    """Extract asset, strike price, and direction from contract question.

    Returns:
        Tuple of (asset, strike_usd, direction: 'reach' or 'dip').
    """
    q_lower = question.lower()
    asset = None
    if "bitcoin" in q_lower or "btc" in q_lower:
        asset = "BTC"
    elif "ethereum" in q_lower or "eth" in q_lower:
        asset = "ETH"
    elif "solana" in q_lower or "sol" in q_lower:
        asset = "SOL"
    elif "ripple" in q_lower or "xrp" in q_lower:
        asset = "XRP"

    if not asset:
        return None, None, None

    direction = "dip" if ("dip" in q_lower or "below" in q_lower) else "reach"

    import re
    match = re.search(r"\$([0-9,]+(?:\.[0-9]+)?)", question)
    if not match:
        return None, None, None

    try:
        strike = float(match.group(1).replace(",", ""))
        return asset, strike, direction
    except ValueError:
        return None, None, None


def scan_jev_crypto(
    markets_by_key: dict[str, dict],
    spot_prices: dict[str, dict] | None = None,
    min_profit: float = MIN_NET_ROI,
    jev_client: JevClient | None = None,
    db=None,
) -> list[dict]:
    """Scan Polymarket crypto markets and refine using Jev decisions.

    Args:
        markets_by_key: Dict of market_key -> market data dict.
        spot_prices: Optional pre-fetched spot prices dictionary.
        min_profit: Minimum net ROI threshold.
        jev_client: Optional JevClient instance (defaults to module singleton).
        db: Optional TradeDB instance for empirical calibration logging.

    Returns:
        List of refined JevCrypto opportunity dicts.
    """
    if not JEV_CRYPTO_ENABLED:
        logger.debug("JEV_CRYPTO_ENABLED is False; skipping Jev scan")
        return []

    client = jev_client or get_jev_client()
    if not client.is_available():
        logger.debug("Jev client not available (missing API key); skipping")
        return []

    spots = spot_prices or fetch_spot_prices()
    if not spots:
        logger.warning("No spot prices available for Jev scan")
        return []

    # Stage 1: Candidate pre-filter
    candidates: list[dict] = []

    for key, market in markets_by_key.items():
        q = market.get("question", "")
        asset, strike, direction = _extract_strike_and_asset(q)
        if not asset or strike is None or asset not in spots:
            continue

        spot = spots[asset]["price"]
        # Strike distance percentage
        dist_pct = abs(strike - spot) / spot * 100

        # Only evaluate strikes within realistic striking distance (<= 35% distance)
        if dist_pct > 35.0:
            continue

        raw_prices = market.get("outcomePrices")
        if not raw_prices:
            continue
        try:
            prices = json.loads(raw_prices) if isinstance(raw_prices, str) else raw_prices
            yes_p = float(prices[0])
            no_p = float(prices[1])
        except (ValueError, IndexError, TypeError):
            continue

        # Skip resolved or non-tradable contracts
        if yes_p <= 0.02 or yes_p >= 0.98:
            continue

        candidates.append({
            "market_key": key,
            "market": market,
            "asset": asset,
            "spot_info": spots[asset],
            "strike": strike,
            "direction": direction,
            "yes_price": yes_p,
            "no_price": no_p,
            "distance_pct": dist_pct,
        })

    if not candidates:
        return []

    # Limit batch to top 5 most relevant candidates to preserve API speed and token budget
    candidates.sort(key=lambda x: x["distance_pct"])
    top_candidates = candidates[:5]

    # Stage 2: Deep refinement with CLOB asks and Jev System One
    return _refine_jev_crypto_with_clob(top_candidates, client=client, min_profit=min_profit, db=db)


# ---------------------------------------------------------------------------
# Stage 2: CLOB & Jev Decision Refinement
# ---------------------------------------------------------------------------


def _refine_jev_crypto_with_clob(
    candidates: list[dict],
    client: JevClient,
    min_profit: float = MIN_NET_ROI,
    db=None,
) -> list[dict]:
    """Refine candidates using CLOB asks and Jev System One evaluations."""
    opportunities: list[dict] = []

    for cand in candidates:
        market = cand["market"]
        token_ids = _extract_token_ids(market)
        if not token_ids:
            continue

        # Fetch CLOB ask
        clob_res = _fetch_clob_for_market(market)
        if isinstance(clob_res, tuple):
            clob_data = clob_res[1]
        elif isinstance(clob_res, dict):
            clob_data = clob_res
        else:
            clob_data = None

        if clob_data:
            best_yes_ask = clob_data.get("yes_ask") or clob_data.get("best_ask") or cand["yes_price"]
            best_no_ask = clob_data.get("no_ask") or cand["no_price"]
        else:
            best_yes_ask = cand["yes_price"]
            best_no_ask = cand["no_price"]

        spot_info = cand["spot_info"]
        end_date_str = market.get("endDate", "2026-12-31T23:59:59Z")
        try:
            end_dt = datetime.fromisoformat(end_date_str.replace("Z", "+00:00"))
            days_left = max(1, (end_dt - datetime.now(timezone.utc)).days)
        except Exception:
            days_left = 60

        state = {
            "spot_market": {
                "asset": cand["asset"],
                "spot_price": spot_info["price"],
                "change_24h_pct": spot_info["change_24h"],
                "vwap_24h": spot_info["vwap_24h"],
            },
            "contract": {
                "question": market.get("question", ""),
                "target_strike_usd": cand["strike"],
                "direction": cand["direction"],
                "current_yes_ask": best_yes_ask,
                "current_no_ask": best_no_ask,
                "days_to_expiration": days_left,
            },
        }

        questions = {
            "strike_probability": {
                "type": "noul",
                "instructions": (
                    "Given spot price at `spot_market.spot_price` and `contract.days_to_expiration` days remaining, "
                    "what is the calibrated probability that the asset hits `contract.target_strike_usd`?"
                ),
            },
            "recommended_action": {
                "type": "choice",
                "instructions": (
                    "Comparing estimated true probability against market asks `contract.current_yes_ask` and `contract.current_no_ask`, "
                    "what is the optimal statistical trading action?"
                ),
                "criteria": {
                    "buy_yes": "Yes contract is underpriced (edge > 4%)",
                    "buy_no": "Yes contract is overpriced / No is underpriced (edge > 4%)",
                    "pass_fair": "Market is fairly priced or edge is within transaction fee spread",
                },
            },
            "tail_risk": {
                "type": "score",
                "instructions": "Rate tail volatility and drawdown risk.",
                "criteria": ["Low risk", "Moderate crypto volatility", "Severe tail risk"],
            },
            "conviction": {
                "type": "score",
                "instructions": "Rate execution conviction.",
                "criteria": ["No trade / pass", "Moderate tactical edge", "Strong institutional conviction"],
            },
        }

        try:
            jev_resp = client.query_decisions(state, questions)
            answers = jev_resp.get("answers", {})
        except Exception as e:
            logger.warning("Jev evaluation failed for market %s: %s", market.get("question"), e)
            continue

        prob_ans = answers.get("strike_probability", {})
        choice_ans = answers.get("recommended_action", {})
        risk_ans = answers.get("tail_risk", {})
        conv_ans = answers.get("conviction", {})

        model_prob = float(prob_ans.get("noul", 0.5))
        action = str(choice_ans.get("choice", "pass_fair"))
        conf = float(choice_ans.get("confidence", 0.0))
        risk = float(risk_ans.get("score", 1.0))
        conviction = float(conv_ans.get("score", 0.0))

        if action == "buy_yes":
            raw_edge = model_prob - best_yes_ask
        elif action == "buy_no":
            raw_edge = (1.0 - model_prob) - best_no_ask
        else:
            raw_edge = 0.0

        if db is not None and hasattr(db, "record_jev_decision"):
            try:
                db.record_jev_decision(
                    asset=cand["asset"],
                    strike=cand["strike"],
                    spot=spot_info["price"],
                    action=action,
                    market_prob=best_yes_ask,
                    jev_prob=model_prob,
                    edge=raw_edge,
                    confidence=conf,
                    details={
                        "risk_score": risk,
                        "conviction": conviction,
                        "token_ids": token_ids,
                        "question": market.get("question"),
                        "direction": cand.get("direction"),
                    },
                )
            except Exception as dbe:
                logger.debug("Failed to record Jev decision in DB: %s", dbe)

        # Apply deterministic gating rules
        if conf < JEV_CONFIDENCE_THRESHOLD or risk > 1.8:
            logger.debug("Rejected by risk/confidence guardrails: conf=%.2f, risk=%.2f", conf, risk)
            continue

        if action == "pass_fair":
            continue

        # Calculate edge and net profit
        trade_size = 50.0
        if action == "buy_yes":
            exec_price = best_yes_ask
            prob_target = model_prob
        else:  # buy_no
            exec_price = best_no_ask
            prob_target = 1.0 - model_prob

        if raw_edge < JEV_MIN_EDGE:
            continue

        net_calc = net_profit_jev_crypto(
            price=exec_price,
            model_prob=prob_target,
            size=trade_size,
        )

        if net_calc["net_profit"] <= 0 or net_calc["net_roi"] < min_profit:
            continue

        opportunities.append({
            "type": "JevCrypto",
            "market": market.get("question", ""),
            "prices": f"Y={best_yes_ask:.2f} N={best_no_ask:.2f}",
            "total_cost": net_calc["total_cost"],
            "net_profit": net_calc["net_profit"],
            "net_roi": net_calc["net_roi"],
            "_market_key": cand["market_key"],
            "_token_ids": token_ids,
            "_action": action,
            "_exec_price": exec_price,
            "_model_prob": model_prob,
            "_confidence": conf,
            "_risk_score": risk,
            "_conviction": conviction,
        })

    return opportunities
