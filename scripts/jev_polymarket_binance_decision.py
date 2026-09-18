#!/usr/bin/env python3
"""Jev System One Decision Engine for Binance + Polymarket BTC Markets.

Demonstrates integrating TypeSafe's Jev-1.13 decision model with live spot exchange
data (Binance) and prediction market contracts (Polymarket).

System One Philosophy:
- "AI-powered software, not agents"
- Code retains 100% control over workflow, risk checks, and execution.
- Jev provides ultra-fast (70-250ms), calibrated probabilistic evaluations.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone


def fetch_binance_btc() -> dict:
    """Fetch live 24h ticker and recent price data for BTCUSDT from Binance.US (or Binance)."""
    endpoints = [
        "https://api.binance.us/api/v3/ticker/24hr?symbol=BTCUSDT",
        "https://api.binance.com/api/v3/ticker/24hr?symbol=BTCUSDT",
    ]
    for url in endpoints:
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=8) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                return {
                    "venue": "Binance",
                    "symbol": "BTCUSDT",
                    "last_price": float(data["lastPrice"]),
                    "price_change_pct_24h": float(data["priceChangePercent"]),
                    "high_24h": float(data["highPrice"]),
                    "low_24h": float(data["lowPrice"]),
                    "volume_btc_24h": float(data["volume"]),
                    "vwap_24h": float(data.get("weightedAvgPrice", data["lastPrice"])),
                }
        except Exception:
            continue

    # Fallback to public CoinGecko if Binance endpoints are geoblocked
    try:
        cg_url = "https://api.coingecko.com/api/v3/simple/price?ids=bitcoin&vs_currencies=usd&include_24hr_change=true"
        req = urllib.request.Request(cg_url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=8) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            btc = data["bitcoin"]
            price = float(btc["usd"])
            chg = float(btc.get("usd_24h_change", 0.0))
            return {
                "venue": "CoinGecko-Aggregated",
                "symbol": "BTC/USD",
                "last_price": price,
                "price_change_pct_24h": chg,
                "high_24h": price * 1.02,
                "low_24h": price * 0.98,
                "volume_btc_24h": 0.0,
                "vwap_24h": price,
            }
    except Exception as e:
        raise RuntimeError(f"Failed to fetch live BTC spot price: {e}") from e


def fetch_polymarket_btc_markets() -> list[dict]:
    """Fetch active Polymarket Bitcoin price markets from Gamma API."""
    url = "https://gamma-api.polymarket.com/events/89502"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            event = json.loads(resp.read().decode("utf-8"))
            active_markets = []
            for m in event.get("markets", []):
                raw_prices = m.get("outcomePrices")
                if not raw_prices:
                    continue
                prices = json.loads(raw_prices) if isinstance(raw_prices, str) else raw_prices
                if len(prices) >= 2 and prices[0] not in ("0", "1"):
                    active_markets.append({
                        "question": m.get("question"),
                        "condition_id": m.get("conditionId"),
                        "token_ids": m.get("clobTokenIds"),
                        "yes_price": float(prices[0]),
                        "no_price": float(prices[1]),
                        "volume_24h": float(m.get("volume24hr") or 0.0),
                        "end_date": m.get("endDate"),
                    })
            return active_markets
    except Exception as e:
        raise RuntimeError(f"Failed to fetch Polymarket events: {e}") from e


def fetch_clob_orderbook(token_id: str) -> dict:
    """Fetch live CLOB orderbook depth for a specific token."""
    url = f"https://clob.polymarket.com/book?token_id={token_id}"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    try:
        with urllib.request.urlopen(req, timeout=8) as resp:
            book = json.loads(resp.read().decode("utf-8"))
            bids = book.get("bids", [])
            asks = book.get("asks", [])
            best_bid = float(bids[0]["price"]) if bids else 0.0
            best_ask = float(asks[0]["price"]) if asks else 1.0
            bid_depth = sum(float(b["size"]) for b in bids[:5]) if bids else 0.0
            ask_depth = sum(float(a["size"]) for a in asks[:5]) if asks else 0.0
            return {
                "best_bid": best_bid,
                "best_ask": best_ask,
                "spread": round(best_ask - best_bid, 4),
                "bid_depth_top5": round(bid_depth, 2),
                "ask_depth_top5": round(ask_depth, 2),
            }
    except Exception:
        return {"best_bid": 0.0, "best_ask": 1.0, "spread": 1.0, "bid_depth_top5": 0.0, "ask_depth_top5": 0.0}


def query_jev_decisions(api_key: str, state: dict, questions: dict) -> dict:
    """Send structured decision request to OpenRouter typesafe/jev-1.13 endpoint."""
    url = "https://openrouter.ai/api/alpha/decisions"
    payload = {
        "model": "typesafe/jev-1.13",
        "state": state,
        "questions": questions,
    }
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "HTTP-Referer": "https://github.com/johnsnow92/polymarket-arb-scanner",
            "X-OpenRouter-Title": "Polymarket-Arb-Scanner / Jev Decision Engine",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=20) as resp:
        return json.loads(resp.read().decode("utf-8"))


def run_decision_pipeline(api_key: str, target_strike: float = 90000.0) -> dict:
    """Execute end-to-end data gathering, Jev probabilistic inference, and deterministic execution logic."""
    print("[1/4] Fetching live Binance BTC/USDT spot data...")
    binance = fetch_binance_btc()
    print(f"      -> Price: ${binance['last_price']:,.2f} | 24h Chg: {binance['price_change_pct_24h']:+.2f}% | VWAP: ${binance['vwap_24h']:,.2f}")

    print("[2/4] Fetching live Polymarket BTC markets...")
    markets = fetch_polymarket_btc_markets()

    # Find the matching strike market or closest one
    selected_market = None
    target_str = f"${int(target_strike):,}"
    for m in markets:
        if target_str in m["question"] and "reach" in m["question"].lower():
            selected_market = m
            break
    if not selected_market and markets:
        selected_market = markets[0]

    print(f"      -> Selected Market: {selected_market['question']}")
    print(f"      -> Mid Prices: Yes = {selected_market['yes_price']:.3f} | No = {selected_market['no_price']:.3f} | 24h Vol = ${selected_market['volume_24h']:,.2f}")

    # Fetch CLOB depth if token IDs present
    clob_info = {}
    if selected_market.get("token_ids"):
        yes_token = selected_market["token_ids"][0]
        clob_info = fetch_clob_orderbook(yes_token)
        print(f"      -> CLOB Orderbook: Best Bid = {clob_info['best_bid']:.3f} | Best Ask = {clob_info['best_ask']:.3f} | Spread = {clob_info['spread']:.3f}")

    # Calculate time to expiry
    end_dt = datetime.fromisoformat(selected_market["end_date"].replace("Z", "+00:00"))
    now_dt = datetime.now(timezone.utc)
    days_left = max(1, (end_dt - now_dt).days)

    # Calculate distance to strike
    strike_gap_pct = ((target_strike - binance["last_price"]) / binance["last_price"]) * 100

    # Build structured state for Jev
    state = {
        "spot_market": {
            "venue": binance["venue"],
            "pair": binance["symbol"],
            "current_price_usd": binance["last_price"],
            "change_24h_pct": binance["price_change_pct_24h"],
            "high_24h_usd": binance["high_24h"],
            "low_24h_usd": binance["low_24h"],
            "vwap_24h_usd": binance["vwap_24h"],
            "current_regime": "bullish_momentum" if binance["price_change_pct_24h"] > 1.5 else ("bearish" if binance["price_change_pct_24h"] < -1.5 else "sideways_consolidation"),
        },
        "polymarket_contract": {
            "venue": "Polymarket",
            "question": selected_market["question"],
            "strike_target_usd": target_strike,
            "strike_distance_pct": round(strike_gap_pct, 2),
            "current_yes_price": selected_market["yes_price"],
            "current_no_price": selected_market["no_price"],
            "market_implied_probability_pct": round(selected_market["yes_price"] * 100, 1),
            "clob_best_bid": clob_info.get("best_bid", selected_market["yes_price"]),
            "clob_best_ask": clob_info.get("best_ask", selected_market["yes_price"]),
            "days_to_expiration": days_left,
            "resolution_date": selected_market["end_date"],
        }
    }

    # Define typed questions according to Jev primitives
    questions = {
        "strike_probability": {
            "type": "noul",
            "instructions": (
                "Given spot BTC at `spot_market.current_price_usd` with `polymarket_contract.days_to_expiration` days "
                "until expiry, what is the calibrated probability that Bitcoin reaches or exceeds `polymarket_contract.strike_target_usd` "
                "(a `polymarket_contract.strike_distance_pct`% move)?"
            ),
            "criteria": {
                "true": "Bitcoin trades at or above the target strike on or before the resolution date",
                "false": "Bitcoin fails to reach the target strike by resolution date"
            }
        },
        "valuation_action": {
            "type": "choice",
            "instructions": (
                "Compare the model's true estimated probability of reaching `polymarket_contract.strike_target_usd` "
                "against Polymarket's current market price `polymarket_contract.current_yes_price`. What is the recommended trading action?"
            ),
            "criteria": {
                "buy_yes": "Contract is underpriced relative to true probability (>4% positive edge after fees)",
                "buy_no": "Contract is overpriced relative to true probability (>4% negative edge after fees)",
                "pass_fair": "Contract is fairly priced or edge is within market spread / transaction cost"
            }
        },
        "volatility_tail_risk": {
            "type": "score",
            "instructions": "Evaluate the tail risk and directional volatility of this contract.",
            "criteria": [
                "Low risk: high margin of safety, calm market regime",
                "Moderate risk: standard crypto asset volatility, manageable drawdown",
                "Severe tail risk: high unpredictability, large asymmetric drawdown hazard"
            ]
        },
        "conviction_score": {
            "type": "score",
            "instructions": "Score execution conviction for capital commitment.",
            "criteria": [
                "0: No edge / pass",
                "1: Moderate tactical edge",
                "2: High conviction institutional edge"
            ]
        }
    }

    print("[3/4] Calling Jev (typesafe/jev-1.13) via OpenRouter Decisions API...")
    jev_response = query_jev_decisions(api_key, state, questions)
    answers = jev_response.get("answers", {})

    print("[4/4] Evaluating Jev answers in deterministic decision engine...")
    prob_answer = answers.get("strike_probability", {})
    action_answer = answers.get("valuation_action", {})
    risk_answer = answers.get("volatility_tail_risk", {})
    conviction_answer = answers.get("conviction_score", {})

    model_p = prob_answer.get("noul", 0.5)
    market_p = selected_market["yes_price"]
    raw_edge = model_p - market_p
    choice = action_answer.get("choice", "pass_fair")
    choice_conf = action_answer.get("confidence", 0.0)
    risk_score = risk_answer.get("score", 1.0)
    conviction = conviction_answer.get("score", 0.0)

    # Deterministic risk and sizing rules
    decision = "PASS"
    sizing_usd = 0.0
    reason = "Edge below threshold or insufficient confidence"

    CONFIDENCE_FLOOR = 0.35
    EDGE_THRESHOLD = 0.04
    MAX_RISK_SCORE = 1.8

    if choice_conf >= CONFIDENCE_FLOOR and risk_score <= MAX_RISK_SCORE:
        if raw_edge > EDGE_THRESHOLD and choice == "buy_yes":
            decision = "BUY_YES"
            base_size = 50.0
            sizing_usd = round(base_size * (raw_edge / 0.10) * max(0.5, conviction) * (2.0 - risk_score), 2)
            sizing_usd = max(10.0, min(sizing_usd, 250.0))
            reason = f"Bullish mispricing: Jev P={model_p:.1%} vs Market={market_p:.1%} (Edge: +{raw_edge*100:.1f}%)"
        elif raw_edge < -EDGE_THRESHOLD and choice == "buy_no":
            decision = "BUY_NO"
            base_size = 50.0
            sizing_usd = round(base_size * (abs(raw_edge) / 0.10) * max(0.5, conviction) * (2.0 - risk_score), 2)
            sizing_usd = max(10.0, min(sizing_usd, 250.0))
            reason = f"Bearish mispricing: Jev P={model_p:.1%} vs Market={market_p:.1%} (Edge: {raw_edge*100:.1f}%)"
        else:
            reason = f"Market is fairly priced within fee band (Edge: {raw_edge*100:+.1f}%, Jev Choice: {choice})"
    else:
        reason = f"Risk/Confidence guardrail triggered: Conf={choice_conf:.2f} (min {CONFIDENCE_FLOOR}), Risk={risk_score:.2f} (max {MAX_RISK_SCORE})"

    usage = jev_response.get("usage", {})

    return {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "spot": binance,
        "polymarket": selected_market,
        "clob": clob_info,
        "jev_answers": answers,
        "usage": usage,
        "engine_output": {
            "decision": decision,
            "recommended_allocation_usd": sizing_usd,
            "model_probability": round(model_p, 4),
            "market_price": round(market_p, 4),
            "net_edge_pct": round(raw_edge * 100, 2),
            "choice_selected": choice,
            "choice_confidence": round(choice_conf, 3),
            "risk_score": round(risk_score, 2),
            "conviction_score": round(conviction, 2),
            "rationale": reason,
        }
    }


def main():
    parser = argparse.ArgumentParser(description="Jev Decision Engine for Polymarket BTC Markets")
    parser.add_argument("--api-key", default=os.getenv("OPENROUTER_API_KEY"), help="OpenRouter API Key")
    parser.add_argument("--strike", type=float, default=90000.0, help="Target BTC strike (e.g. 90000, 85000, 95000)")
    args = parser.parse_args()

    api_key = args.api_key or os.getenv("OPENROUTER_API_KEY")
    if not api_key:
        print("Error: OpenRouter API key required. Pass --api-key or set OPENROUTER_API_KEY.")
        sys.exit(1)

    result = run_decision_pipeline(api_key, target_strike=args.strike)

    print("\n" + "=" * 70)
    print("           JEV SYSTEM ONE DECISION REPORT")
    print("=" * 70)
    print(f"Contract: {result['polymarket']['question']}")
    print(f"Binance BTC: ${result['spot']['last_price']:,.2f} ({result['spot']['price_change_pct_24h']:+.2f}%)")
    print(f"Polymarket Price: Yes = ${result['polymarket']['yes_price']:.3f} | No = ${result['polymarket']['no_price']:.3f}")
    if result['clob'].get('spread') is not None:
        print(f"CLOB Best Bid/Ask: {result['clob']['best_bid']:.3f} / {result['clob']['best_ask']:.3f} (Spread: {result['clob']['spread']:.3f})")

    eng = result["engine_output"]
    print("-" * 70)
    print(f"Jev Strike Probability (Noul):     {eng['model_probability']:.1%}")
    print(f"Polymarket Implied Probability:     {eng['market_price']:.1%}")
    print(f"Net Statistical Edge:               {eng['net_edge_pct']:+.2f}%")
    print(f"Jev Recommended Action (Choice):    {eng['choice_selected']} (Confidence: {eng['choice_confidence']:.2f})")
    print(f"Jev Tail Risk Score:                {eng['risk_score']:.2f} / 2.0")
    print(f"Jev Conviction Score:               {eng['conviction_score']:.2f} / 2.0")
    print("-" * 70)
    print(f"FINAL ENGINE DECISION:              >>> {eng['decision']} <<<")
    print(f"ALLOCATION SIZE:                    ${eng['recommended_allocation_usd']:.2f} USD")
    print(f"RATIONALE:                          {eng['rationale']}")
    print("-" * 70)
    u = result["usage"]
    print(f"Latency/Tokens: Input={u.get('input_tokens')} | Output={u.get('output_tokens')} | Cost: ${u.get('cost', 0):.6f}")
    print("=" * 70)


if __name__ == "__main__":
    main()
