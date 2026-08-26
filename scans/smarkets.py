"""Smarkets Exchange standalone arbitrage scans (back-all and back-lay)."""

import logging

from smarkets_api import SmarketsClient
from fees import net_profit_smarkets_backall, net_profit_smarkets_backlay
import config
from scans.helpers import filter_dust

logger = logging.getLogger(__name__)


def scan_smarkets_backall(smarkets_client: SmarketsClient, min_profit: float) -> list[dict]:
    """Scan for Smarkets back-all arbitrage (under-round books).

    Sum of implied back prices across all runners < 1.0.
    """
    opportunities = []

    if not smarkets_client or not smarkets_client.authenticated:
        return opportunities

    markets = smarkets_client.fetch_all_markets()
    if not markets:
        logger.warning("No Smarkets markets fetched.")
        return opportunities

    logger.info("Scanning %d Smarkets markets for back-all arbs...", len(markets))

    for market in markets:
        market_id = market.get("id", "")
        if not market_id:
            continue

        # Fetch runners/contracts for this market
        runners = smarkets_client.list_runners(market_id)
        if len(runners) < 2:
            continue

        implied_probs = []
        contract_ids = []
        valid = True

        for runner in runners:
            quotes = runner.get("quotes", {})
            best_back = quotes.get("best_available_to_back", {})
            if not best_back:
                valid = False
                break
            price_pct = best_back.get("price")
            if not price_pct or float(price_pct) <= 0:
                valid = False
                break
            implied = float(price_pct) / 100.0
            if implied <= 0 or implied >= 1:
                valid = False
                break
            implied_probs.append(implied)
            contract_ids.append(runner.get("id", ""))

        if not valid or not implied_probs:
            continue

        result = net_profit_smarkets_backall(implied_probs, config.SMARKETS_COMMISSION_RATE)
        if result["net_profit"] >= min_profit:
            total = sum(implied_probs)
            n = len(implied_probs)
            event_info = market.get("_event", {})
            event_name = event_info.get("name", "")
            market_name = market.get("name", "Unknown")
            title = f"{event_name} - {market_name}" if event_name else market_name

            price_summary = ", ".join(
                f"{p:.3f}" for p in sorted(implied_probs, reverse=True)[:5]
            )
            if n > 5:
                price_summary += f"... ({n} runners)"

            # Min depth from available quantities
            min_depth = float("inf")
            for runner in runners:
                quotes = runner.get("quotes", {})
                best_back = quotes.get("best_available_to_back", {})
                if best_back:
                    size = float(best_back.get("quantity", 0))
                    min_depth = min(min_depth, size)
            if min_depth == float("inf"):
                min_depth = 0

            opportunities.append({
                "type": "SmarketsBackAll",
                "_layer": 1,  # Layer 1: pure arbitrage
                "market": title[:60],
                "prices": price_summary,
                "total_cost": f"${total:.4f}",
                "gross_spread": f"{result['gross_spread']:.4f}",
                "fees": f"${result['fees']:.4f}",
                "net_profit": result["net_profit"],
                "net_roi": f"{result['net_profit'] / total * 100:.2f}%",
                "_sm_market_id": market_id,
                "_sm_contract_ids": contract_ids,
                "_sm_prices": implied_probs,
                "_clob_depth": min_depth,
            })

    logger.info("Found %d Smarkets back-all opportunities.", len(opportunities))
    opportunities = filter_dust(opportunities)
    return opportunities


def scan_smarkets_backlay(smarkets_client: SmarketsClient, min_profit: float) -> list[dict]:
    """Scan for Smarkets back-lay arbitrage (crossed books on same runner).

    Same runner has back_prob < lay_prob (crossed book).
    """
    opportunities = []

    if not smarkets_client or not smarkets_client.authenticated:
        return opportunities

    markets = smarkets_client.fetch_all_markets()
    if not markets:
        return opportunities

    logger.info("Scanning %d Smarkets markets for back-lay arbs...", len(markets))

    for market in markets:
        market_id = market.get("id", "")
        if not market_id:
            continue

        runners = smarkets_client.list_runners(market_id)

        for runner in runners:
            quotes = runner.get("quotes", {})
            best_back = quotes.get("best_available_to_back", {})
            best_lay = quotes.get("best_available_to_lay", {})

            if not best_back or not best_lay:
                continue

            back_pct = float(best_back.get("price", 0))
            lay_pct = float(best_lay.get("price", 0))

            if back_pct <= 0 or lay_pct <= 0:
                continue

            back_prob = back_pct / 100.0
            lay_prob = lay_pct / 100.0

            # Crossed book: lay_prob > back_prob
            if lay_prob <= back_prob:
                continue

            result = net_profit_smarkets_backlay(back_prob, lay_prob, config.SMARKETS_COMMISSION_RATE)
            if result["net_profit"] >= min_profit:
                event_info = market.get("_event", {})
                event_name = event_info.get("name", "")
                runner_name = runner.get("name", "")
                title = (f"{event_name} - {runner_name}" if event_name
                         else runner_name or market.get("name", "Unknown"))

                back_size = float(best_back.get("quantity", 0))
                lay_size = float(best_lay.get("quantity", 0))

                opportunities.append({
                    "type": "SmarketsBackLay",
                    "_layer": 1,  # Layer 1: pure arbitrage
                    "market": title[:60],
                    "prices": f"back={back_prob:.3f} lay={lay_prob:.3f}",
                    "total_cost": f"${back_prob:.4f}",
                    "gross_spread": f"{result['gross_spread']:.4f}",
                    "fees": f"${result['fees']:.4f}",
                    "net_profit": result["net_profit"],
                    "net_roi": (f"{result['net_profit'] / back_prob * 100:.2f}%"
                                if back_prob > 0 else "0%"),
                    "_sm_market_id": market_id,
                    "_sm_contract_id": runner.get("id", ""),
                    "_sm_back_price": back_prob,
                    "_sm_lay_price": lay_prob,
                    "_clob_depth": min(back_size, lay_size),
                })

    logger.info("Found %d Smarkets back-lay opportunities.", len(opportunities))
    opportunities = filter_dust(opportunities)
    return opportunities
