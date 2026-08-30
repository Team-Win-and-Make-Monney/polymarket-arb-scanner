"""Matchbook Exchange standalone arbitrage scans (back-all and back-lay)."""

import logging

from matchbook_api import MatchbookClient
from fees import net_profit_matchbook_backall, net_profit_matchbook_backlay
from scans.helpers import filter_dust

logger = logging.getLogger(__name__)


def scan_matchbook_backall(matchbook_client: MatchbookClient, min_profit: float) -> list[dict]:
    """Scan for Matchbook back-all arbitrage (under-round books).

    Sum of implied back prices across all runners < 1.0.
    Matchbook has 0% commission on prediction markets so full spread is profit.
    """
    opportunities = []

    if not matchbook_client or not matchbook_client.authenticated:
        return opportunities

    markets = matchbook_client.fetch_all_markets()
    if not markets:
        logger.warning("No Matchbook markets fetched.")
        return opportunities

    logger.info("Scanning %d Matchbook markets for back-all arbs...", len(markets))

    for market in markets:
        market_id = market.get("id", "")
        if not market_id:
            continue

        event_info = market.get("_event", {})
        event_id = event_info.get("id")
        if not event_id:
            continue

        # Fetch runners with price data for this market
        runners = matchbook_client.list_runners(market_id, event_id=event_id)
        if len(runners) < 2:
            continue

        implied_probs = []
        runner_ids = []
        selected_depths = []
        valid = True

        for runner in runners:
            prices = runner.get("prices", [])
            # Find best back price
            best_back_odds = None
            best_back_depth = 0.0
            for price_entry in prices:
                side = price_entry.get("side", "").lower()
                odds = price_entry.get("odds")
                if side == "back" and odds and float(odds) > 1.0:
                    if best_back_odds is None or float(odds) > best_back_odds:
                        best_back_odds = float(odds)
                        best_back_depth = float(price_entry.get("available-amount", 0) or 0)

            if best_back_odds is None or best_back_odds <= 1.0:
                valid = False
                break

            implied = 1.0 / best_back_odds
            implied_probs.append(implied)
            runner_ids.append(runner.get("id", ""))
            selected_depths.append(best_back_depth)

        if not valid or not implied_probs:
            continue

        result = net_profit_matchbook_backall(implied_probs)
        if result["net_profit"] >= min_profit:
            total = sum(implied_probs)
            n = len(implied_probs)
            event_name = event_info.get("name", "")
            market_name = market.get("name", "Unknown")
            title = f"{event_name} - {market_name}" if event_name else market_name

            price_summary = ", ".join(
                f"{p:.3f}" for p in sorted(implied_probs, reverse=True)[:5]
            )
            if n > 5:
                price_summary += f"... ({n} runners)"

            min_depth = min(selected_depths) if selected_depths else 0

            opportunities.append({
                "type": "MatchbookBackAll",
                "_layer": 1,  # Layer 1: pure arbitrage
                "market": title[:60],
                "prices": price_summary,
                "total_cost": f"${total:.4f}",
                "gross_spread": f"{result['gross_spread']:.4f}",
                "fees": f"${result['fees']:.4f}",
                "net_profit": result["net_profit"],
                "net_roi": f"{result['net_profit'] / total * 100:.2f}%",
                "_mb_market_id": market_id,
                "_mb_runner_ids": runner_ids,
                "_mb_prices": implied_probs,
                "_clob_depth": min_depth,
            })

    logger.info("Found %d Matchbook back-all opportunities.", len(opportunities))
    opportunities = filter_dust(opportunities)
    return opportunities


def scan_matchbook_backlay(matchbook_client: MatchbookClient, min_profit: float) -> list[dict]:
    """Scan for Matchbook back-lay arbitrage (crossed books on same runner).

    Same runner has back_prob < lay_prob (crossed book).
    Matchbook has 0% commission on prediction markets so full spread is profit.
    """
    opportunities = []

    if not matchbook_client or not matchbook_client.authenticated:
        return opportunities

    markets = matchbook_client.fetch_all_markets()
    if not markets:
        return opportunities

    logger.info("Scanning %d Matchbook markets for back-lay arbs...", len(markets))

    for market in markets:
        market_id = market.get("id", "")
        if not market_id:
            continue

        event_info = market.get("_event", {})
        event_id = event_info.get("id")
        if not event_id:
            continue

        runners = matchbook_client.list_runners(market_id, event_id=event_id)

        for runner in runners:
            prices = runner.get("prices", [])

            # Find best back and lay odds
            best_back_odds = None
            best_lay_odds = None
            back_amount = 0
            lay_amount = 0

            for price_entry in prices:
                side = price_entry.get("side", "").lower()
                odds = price_entry.get("odds")
                if not odds or float(odds) <= 1.0:
                    continue
                if side == "back":
                    if best_back_odds is None or float(odds) > best_back_odds:
                        best_back_odds = float(odds)
                        back_amount = float(price_entry.get("available-amount", 0))
                elif side == "lay":
                    if best_lay_odds is None or float(odds) < best_lay_odds:
                        best_lay_odds = float(odds)
                        lay_amount = float(price_entry.get("available-amount", 0))

            if best_back_odds is None or best_lay_odds is None:
                continue

            # Crossed book: back odds > lay odds means we can profit
            if best_back_odds <= best_lay_odds:
                continue

            # Convert to implied probabilities
            back_prob = 1.0 / best_back_odds
            lay_prob = 1.0 / best_lay_odds

            result = net_profit_matchbook_backlay(back_prob, lay_prob)
            if result["net_profit"] >= min_profit:
                event_name = event_info.get("name", "")
                runner_name = runner.get("name", "")
                title = (f"{event_name} - {runner_name}" if event_name
                         else runner_name or market.get("name", "Unknown"))

                opportunities.append({
                    "type": "MatchbookBackLay",
                    "_layer": 1,  # Layer 1: pure arbitrage
                    "market": title[:60],
                    "prices": f"back={back_prob:.3f} lay={lay_prob:.3f}",
                    "total_cost": f"${back_prob:.4f}",
                    "gross_spread": f"{result['gross_spread']:.4f}",
                    "fees": f"${result['fees']:.4f}",
                    "net_profit": result["net_profit"],
                    "net_roi": (f"{result['net_profit'] / back_prob * 100:.2f}%"
                                if back_prob > 0 else "0%"),
                    "_mb_market_id": market_id,
                    "_mb_runner_id": runner.get("id", ""),
                    "_mb_back_price": back_prob,
                    "_mb_lay_price": lay_prob,
                    "_clob_depth": min(back_amount, lay_amount),
                })

    logger.info("Found %d Matchbook back-lay opportunities.", len(opportunities))
    opportunities = filter_dust(opportunities)
    return opportunities
