"""Event-driven speed trading — Metaculus divergence signals."""

import logging
import time

from thefuzz import fuzz

from metaculus_api import MetaculusClient
from matcher import normalize_title, _extract_entities

logger = logging.getLogger(__name__)


class EventMonitor:
    """Monitors multi-source signals for divergence from platform prices.

    When a platform's price diverges significantly from consensus
    probability (Metaculus + Manifold + other sources), flags the
    opportunity for execution.

    This is informed speculation, not pure arbitrage. Multi-source
    consensus with historical calibration provides high-edge signals.
    """

    def __init__(
        self,
        metaculus_client: MetaculusClient,
        divergence_threshold: float = 0.10,
        min_metaculus_forecasters: int = 20,
        match_threshold: int = 72,
        signal_aggregator=None,
    ):
        """
        Args:
            metaculus_client: Initialized MetaculusClient.
            divergence_threshold: Minimum absolute divergence to flag (default 0.10 = 10%).
            min_metaculus_forecasters: Skip questions with fewer forecasters.
            match_threshold: Fuzzy match threshold for matching questions to markets.
            signal_aggregator: Optional SignalAggregator for multi-source consensus.
        """
        self.client = metaculus_client
        self.divergence_threshold = divergence_threshold
        self.min_forecasters = min_metaculus_forecasters
        self.match_threshold = match_threshold
        self.signal_aggregator = signal_aggregator

        # Questions cache
        self._questions_cache: list[dict] = []
        self._questions_cache_ts: float = 0
        from config import METACULUS_CACHE_TTL
        self._cache_ttl: float = METACULUS_CACHE_TTL

    def _get_cached_questions(self) -> list[dict]:
        """Fetch active Metaculus questions, using cache if fresh."""
        now = time.time()
        if self._questions_cache and (now - self._questions_cache_ts) < self._cache_ttl:
            return self._questions_cache

        logger.info("Fetching active Metaculus questions...")
        questions = self.client.fetch_active_questions(limit=200)
        if questions:
            self._questions_cache = questions
            self._questions_cache_ts = time.time()
            logger.info("Cached %d Metaculus questions.", len(questions))
        return self._questions_cache

    def _match_market_to_question(
        self, market_title: str, questions: list[dict]
    ) -> dict | None:
        """Fuzzy-match a market title to the best Metaculus question.

        Uses the same two-stage matching as matcher.py:
        1. Entity overlap validation (must share >= 2 key terms)
        2. Fuzzy string similarity (token_sort_ratio + entity ratio bonus)

        Args:
            market_title: The platform market title to match.
            questions: List of Metaculus question dicts.

        Returns:
            Best matching question dict or None if no match found.
        """
        m_norm = normalize_title(market_title)
        m_entities = _extract_entities(market_title)

        if not m_norm or len(m_norm) < 8:
            return None

        best_score = 0
        best_question = None

        for question in questions:
            q_title = question.get("title", "") or ""
            if not q_title:
                continue

            q_norm = normalize_title(q_title)
            q_entities = _extract_entities(q_title)

            if not q_norm:
                continue

            # Stage 1: Quick reject if no meaningful entity overlap
            overlap = m_entities & q_entities
            if len(overlap) < 2:
                continue

            # Stage 2: Fuzzy string similarity
            score = fuzz.token_sort_ratio(m_norm, q_norm)

            # Fallback: try partial_ratio if token_sort is borderline
            if score < self.match_threshold and score >= self.match_threshold - 15:
                partial = fuzz.partial_ratio(m_norm, q_norm)
                score = max(score, partial)

            # Combined score with entity overlap bonus (same formula as matcher.py)
            min_entities = min(len(m_entities), len(q_entities))
            entity_ratio = len(overlap) / min_entities if min_entities > 0 else 0
            combined = score + (entity_ratio * 15)

            if combined > best_score:
                best_score = combined
                best_question = question

        if best_score >= self.match_threshold and best_question:
            return best_question

        return None

    def _get_platform_yes_price(
        self, market: dict, platform_name: str
    ) -> float | None:
        """Extract the YES price from a platform market dict.

        Args:
            market: Platform market dict.
            platform_name: Name of the platform (e.g. "polymarket", "kalshi").

        Returns:
            Float price (0-1) or None if unable to extract.
        """
        platform = platform_name.lower()

        if platform == "polymarket":
            # Try outcomePrices field (JSON string or list)
            raw = market.get("outcomePrices")
            if raw:
                try:
                    import json
                    if isinstance(raw, str):
                        prices = json.loads(raw)
                    else:
                        prices = raw
                    if prices and len(prices) >= 1:
                        return float(prices[0])
                except (ValueError, TypeError, IndexError) as e:
                    logger.debug("Price parse failed for Polymarket outcomePrices: %s", e)

            # Try tokens array
            tokens = market.get("tokens", [])
            if tokens and len(tokens) >= 1:
                price = tokens[0].get("price")
                if price is not None:
                    return float(price)

            return None

        if platform == "kalshi":
            # Kalshi prices are in cents (0-100)
            yes_price = market.get("yes_price")
            if yes_price is not None:
                try:
                    return float(yes_price) / 100.0
                except (ValueError, TypeError) as e:
                    logger.debug("Price parse failed for Kalshi yes_price: %s", e)

            # Fallback: last_price
            last_price = market.get("last_price")
            if last_price is not None:
                try:
                    return float(last_price) / 100.0
                except (ValueError, TypeError) as e:
                    logger.debug("Price parse failed for Kalshi last_price: %s", e)

            return None

        # Exchange platforms store prices directly on the market dict
        # (injected by the scan layer or fetch step)
        if platform in ("betfair", "smarkets", "sxbet", "matchbook"):
            # Try common price field names
            for key in ("_yes_price", "yes_price", "back_price"):
                val = market.get(key)
                if val is not None:
                    try:
                        return float(val)
                    except (ValueError, TypeError):
                        continue
            return None

        return None

    def find_divergences(
        self, platform_markets: list[dict], platform_name: str
    ) -> list[dict]:
        """Find markets where platform price diverges from Metaculus consensus.

        For each platform market, tries to fuzzy-match to a Metaculus question.
        If matched, compares platform YES price to Metaculus community probability.
        Records divergences exceeding the threshold.

        Args:
            platform_markets: List of market dicts from a trading platform.
            platform_name: Name of the platform (e.g. "polymarket", "kalshi").

        Returns:
            List of divergence dicts with keys: market_title, platform_price,
            metaculus_prob, divergence, question_id, platform_name, direction.
        """
        questions = self._get_cached_questions()
        if not questions:
            logger.warning("No Metaculus questions available for divergence scan.")
            return []

        divergences = []
        matched_count = 0

        for market in platform_markets:
            # Extract market title
            market_title = (
                market.get("question", "")
                or market.get("title", "")
                or market.get("name", "")
                or ""
            )
            if not market_title:
                continue

            # Extract platform price
            platform_price = self._get_platform_yes_price(market, platform_name)
            if platform_price is None:
                continue

            # Try to match to a Metaculus question
            question = self._match_market_to_question(market_title, questions)
            if not question:
                continue
            matched_count += 1

            # Check forecaster count
            num_forecasters = question.get("number_of_forecasters", 0) or 0
            if num_forecasters < self.min_forecasters:
                continue

            # Get Metaculus community probability
            community_pred = question.get("community_prediction")
            if not community_pred:
                continue

            try:
                metaculus_prob = float(community_pred["full"]["q2"])
            except (KeyError, TypeError, ValueError):
                continue

            # If signal aggregator is available, use multi-source consensus
            # instead of Metaculus-only probability
            consensus_prob = metaculus_prob
            num_sources = 1
            if self.signal_aggregator:
                market_key = market.get("condition_id") or market.get("ticker") or market_title[:40]
                self.signal_aggregator.add_signal(market_key, "metaculus", metaculus_prob)
                # Fetch additional signals (Manifold, etc.)
                self.signal_aggregator.fetch_external_signals(market_key, market_title)
                consensus = self.signal_aggregator.get_consensus(market_key)
                if consensus and consensus["num_sources"] > 1:
                    # Use consensus excluding the platform's own price
                    # (we want external consensus vs platform price)
                    consensus_prob = consensus["probability"]
                    num_sources = consensus["num_sources"]

            # Calculate divergence
            divergence = abs(platform_price - consensus_prob)
            if divergence >= self.divergence_threshold:
                direction = "BUY_YES" if consensus_prob > platform_price else "BUY_NO"
                divergences.append({
                    "market_title": market_title,
                    "platform_price": platform_price,
                    "metaculus_prob": metaculus_prob,
                    "consensus_prob": consensus_prob,
                    "num_sources": num_sources,
                    "divergence": divergence,
                    "question_id": question.get("id"),
                    "platform_name": platform_name,
                    "direction": direction,
                    "num_forecasters": num_forecasters,
                    "market": market,
                })

        logger.info(
            "Metaculus divergence scan: %d/%d markets matched, %d divergences found "
            "(threshold=%.0f%%, platform=%s)",
            matched_count,
            len(platform_markets),
            len(divergences),
            self.divergence_threshold * 100,
            platform_name,
        )
        return divergences

    def build_signal_opportunities(
        self, divergences: list[dict]
    ) -> list[dict]:
        """Convert divergence dicts to standard opportunity dicts.

        Args:
            divergences: List of divergence dicts from find_divergences().

        Returns:
            List of opportunity dicts matching the project's standard format.
        """
        opportunities = []

        for div in divergences:
            platform_price = div["platform_price"]
            metaculus_prob = div["metaculus_prob"]
            divergence = div["divergence"]
            market_title = div["market_title"]
            platform_name = div["platform_name"]
            question_id = div["question_id"]

            # Confidence based on divergence magnitude
            if divergence >= 0.20:
                confidence = "HIGH"
            elif divergence >= 0.15:
                confidence = "MEDIUM"
            else:
                confidence = "LOW"

            # Conservative profit estimate: assume 50% convergence
            net_profit = divergence * 0.5
            if platform_price > 0:
                net_roi = f"{(net_profit / platform_price * 100):.2f}%"
            else:
                net_roi = "0%"

            direction = div["direction"]

            consensus_prob = div.get("consensus_prob", metaculus_prob)
            num_sources = div.get("num_sources", 1)
            prices_str = f"platform={platform_price:.3f} metaculus={metaculus_prob:.3f}"
            if num_sources > 1:
                prices_str = f"platform={platform_price:.3f} consensus={consensus_prob:.3f} (n={num_sources})"

            opp = {
                "type": "EventDivergence",
                "_layer": 4,  # Layer 4: informed trading
                "market": market_title[:50],
                "prices": prices_str,
                "total_cost": f"${platform_price:.4f}",
                "gross_spread": f"{divergence:.4f}",
                "fees": "$0.0000",
                "net_profit": net_profit,
                "net_roi": net_roi,
                "confidence": confidence,
                "_platform": platform_name,
                "_metaculus_id": question_id,
                "_metaculus_prob": metaculus_prob,
                "_consensus_prob": consensus_prob,
                "_num_sources": num_sources,
                "_divergence": divergence,
                "_direction": direction,
                "_clob_depth": 0,
            }

            # Attach execution metadata from the source market dict
            source_market = div.get("market", {})
            if platform_name == "polymarket":
                tokens = source_market.get("tokens", [])
                if tokens:
                    opp["_token_ids"] = [t.get("token_id", "") for t in tokens]
            elif platform_name == "kalshi":
                opp["_kalshi_ticker"] = source_market.get("ticker", "")
            elif platform_name == "betfair":
                opp["_market_id"] = source_market.get("marketId", source_market.get("_market_id", ""))
                runners = source_market.get("runners", [])
                if runners:
                    opp["_selection_id"] = runners[0].get("selectionId") or runners[0].get("id")
            elif platform_name == "smarkets":
                opp["_sm_market_id"] = str(source_market.get("id", ""))
                contracts = source_market.get("contracts", [])
                if contracts:
                    opp["_sm_contract_id"] = str(contracts[0].get("id", ""))
            elif platform_name == "sxbet":
                opp["_sx_market_hash"] = source_market.get("marketHash", "")
                outcomes = source_market.get("outcomes", [])
                if outcomes:
                    opp["_sx_outcome_id"] = str(outcomes[0].get("outcomeId", outcomes[0].get("id", "")))
            elif platform_name == "matchbook":
                opp["_mb_market_id"] = str(source_market.get("id", source_market.get("_market_id", "")))
                runners = source_market.get("runners", [])
                if runners:
                    opp["_mb_runner_id"] = str(runners[0].get("id", ""))

            opportunities.append(opp)

        return opportunities

    def scan_event_divergences(
        self,
        platform_markets: dict[str, list],
        min_profit: float = 0.005,
    ) -> list[dict]:
        """Main entry point for event divergence scanning.

        Scans all provided platform market lists against Metaculus consensus
        and returns profitable divergence opportunities.

        Args:
            platform_markets: Dict of {platform_name: markets_list}.
            min_profit: Minimum net_profit to include in results.

        Returns:
            Sorted list of opportunity dicts (highest profit first).
        """
        all_divergences = []

        for platform_name, markets in platform_markets.items():
            if not markets:
                continue
            divergences = self.find_divergences(markets, platform_name)
            all_divergences.extend(divergences)

        if not all_divergences:
            return []

        opportunities = self.build_signal_opportunities(all_divergences)

        # Filter by minimum profit
        filtered = [
            opp for opp in opportunities if opp["net_profit"] >= min_profit
        ]

        # Sort by net_profit descending (best opportunities first)
        filtered.sort(key=lambda x: x["net_profit"], reverse=True)

        logger.info(
            "Event divergence scan: %d opportunities after min_profit filter (%.4f).",
            len(filtered),
            min_profit,
        )
        return filtered
