"""Cross-platform market matcher using fuzzy string matching and optional embeddings.

Matches Polymarket binary markets against Kalshi events by title similarity.
Kalshi events have clean titles suitable for matching, while their markets
endpoint is dominated by sports parlay combos with unhelpful titles.

When fastembed is available and SEMANTIC_MATCHING_ENABLED is true, an
embedding-based matcher provides higher-recall cross-platform matching
using all-MiniLM-L6-v2 cosine similarity.
"""

from __future__ import annotations

import logging
import re

try:
    import numpy as np
except ImportError:
    np = None  # type: ignore[assignment]
from thefuzz import fuzz

logger = logging.getLogger(__name__)

# Common filler words that don't help distinguish markets
_STOPWORDS = {
    "the", "a", "an", "of", "in", "on", "by", "to", "for", "and", "or",
    "be", "will", "does", "is", "can", "has", "have", "are", "was", "were",
    "do", "did", "been", "being", "this", "that", "it", "its", "with",
    "from", "at", "as", "but", "not", "no", "any", "all", "each", "every",
    "before", "after", "during", "than", "more", "most", "least", "new",
}


def normalize_title(title: str) -> str:
    """Normalize a market/event title for better matching."""
    title = title.lower().strip()
    # Remove markdown formatting
    title = re.sub(r"\*+", "", title)
    # Remove Gemini instrument prefixes (e.g. "GEMI-", "GM-")
    title = re.sub(r"^(gemi-|gm-)", "", title)
    # Remove IBKR ForecastEx prefixes (e.g. "FX-", "IBKR-")
    title = re.sub(r"^(fx-|ibkr-|forecastex\s*[-:]\s*)", "", title)
    # Remove trailing question marks and punctuation
    title = re.sub(r"[?!.]+$", "", title)
    # Normalize whitespace
    title = re.sub(r"\s+", " ", title).strip()
    return title


# Short tokens (< 3 chars) that carry strong identifying signal in prediction
# markets.  Country/region codes, US state abbreviations, and common acronyms
# that would otherwise be discarded by the length filter.
_CRITICAL_SHORT_ENTITIES = {
    "ai", "uk", "eu", "us", "un", "gp", "nz", "jp", "cn", "hk",
    "tx", "ca", "ny", "fl", "dc", "la", "sf", "gm", "fx", "rx",
    "ip", "ev", "ui", "ux", "qa", "pr", "hr", "ir", "vp", "pm",
    "uv", "gw", "vc", "iq", "io", "sp",
}


def _extract_entities(title: str) -> set[str]:
    """Extract key named entities and distinguishing terms from a title.

    Returns significant words (proper nouns, specific terms, numbers)
    that help identify what the market is actually about.  Two-character
    tokens are kept only when they appear in ``_CRITICAL_SHORT_ENTITIES``
    (country codes, state abbreviations, important acronyms).
    """
    normalized = title.lower()
    # Split into words, remove stopwords
    words = re.findall(r"\b[a-z0-9]+\b", normalized)
    entities = set()
    for w in words:
        if w in _STOPWORDS:
            continue
        if len(w) >= 3 or w in _CRITICAL_SHORT_ENTITIES:
            entities.add(w)
    return entities


def classify_confidence(similarity: int, entity_overlap: int, min_entities: int) -> str:
    """Classify match confidence as HIGH, MEDIUM, or LOW.

    HIGH:   similarity >= 90 AND entity overlap >= 60% of smaller entity set
    MEDIUM: similarity >= 80 AND entity overlap >= 40%
    LOW:    everything else that passed threshold
    """
    if min_entities > 0:
        overlap_ratio = entity_overlap / min_entities
    else:
        overlap_ratio = 0

    if similarity >= 90 and overlap_ratio >= 0.60:
        return "HIGH"
    elif similarity >= 80 and overlap_ratio >= 0.40:
        return "MEDIUM"
    return "LOW"


# Confidence tier ordering for filtering
CONFIDENCE_ORDER = {"HIGH": 3, "MEDIUM": 2, "LOW": 1}


def match_markets_to_events(
    polymarket_markets: list[dict],
    kalshi_events: list[dict],
    threshold: int = 80,
    min_confidence: str = "LOW",
) -> list[dict]:
    """Match Polymarket markets to Kalshi events by title similarity.

    Uses a two-stage filter:
    1. Fuzzy string similarity (token_sort_ratio >= threshold)
    2. Entity overlap validation (must share key terms, not just filler words)
    3. Confidence classification (HIGH/MEDIUM/LOW)

    Returns list of matched pairs with similarity scores and confidence.
    """
    matches = []
    min_conf_level = CONFIDENCE_ORDER.get(min_confidence.upper(), 1)

    # Pre-normalize Kalshi event titles and extract entities
    kalshi_prepared = []
    for ke in kalshi_events:
        title = ke.get("title", "")
        if not title:
            continue
        norm = normalize_title(title)
        entities = _extract_entities(title)
        kalshi_prepared.append((norm, entities, ke))

    for pm in polymarket_markets:
        pm_title = pm.get("question", "") or pm.get("title", "")
        pm_norm = normalize_title(pm_title)
        pm_entities = _extract_entities(pm_title)

        if not pm_norm or len(pm_norm) < 8:
            continue

        best_score = 0
        best_kalshi_event = None
        best_entity_overlap = 0
        best_min_entities = 0

        # Adaptive entity overlap: short titles (both <=6 words) need only 1
        pm_word_count = len(pm_norm.split())

        for k_norm, k_entities, ke in kalshi_prepared:
            if not k_norm:
                continue

            # Quick reject: if no entity overlap at all, skip
            overlap = pm_entities & k_entities
            k_word_count = len(k_norm.split())
            min_overlap = 1 if (pm_word_count <= 6 and k_word_count <= 6) else 2
            if len(overlap) < min_overlap:
                continue

            # Use token_sort_ratio for order-independent matching
            score = fuzz.token_sort_ratio(pm_norm, k_norm)

            # Fallback: try partial_ratio if token_sort is borderline
            if score < threshold and score >= threshold - 15:
                partial = fuzz.partial_ratio(pm_norm, k_norm)
                score = max(score, partial)

            # Require meaningful entity overlap proportional to title length
            min_entities = min(len(pm_entities), len(k_entities))
            if min_entities > 0:
                entity_ratio = len(overlap) / min_entities
            else:
                entity_ratio = 0

            # Combined score: fuzzy match + entity overlap bonus
            combined = score + (entity_ratio * 15)

            # Category scoring boost from Kalshi
            k_category = ke.get("category", "").lower()
            if k_category:
                category_words = set(k_category.replace("-", " ").split())
                if category_words & pm_entities:
                    combined += 5

            if combined > best_score:
                best_score = combined
                best_kalshi_event = ke
                best_entity_overlap = len(overlap)
                best_min_entities = min_entities

        # Require both fuzzy similarity AND meaningful entity overlap
        # Short titles (both <=6 words) need only 1 shared entity
        final_min_overlap = 1 if pm_word_count <= 6 else 2
        if best_score >= threshold and best_kalshi_event and best_entity_overlap >= final_min_overlap:
            confidence = classify_confidence(
                int(best_score), best_entity_overlap, best_min_entities
            )
            # Filter by minimum confidence
            if CONFIDENCE_ORDER.get(confidence, 0) < min_conf_level:
                continue
            matches.append({
                "polymarket": pm,
                "kalshi_event": best_kalshi_event,
                "similarity": int(best_score),
                "entity_overlap": best_entity_overlap,
                "confidence": confidence,
                "pm_title": pm_title,
                "kalshi_title": best_kalshi_event.get("title", ""),
            })

    # Sort by similarity descending
    matches.sort(key=lambda x: x["similarity"], reverse=True)

    # Deduplicate: only keep the best Polymarket match per Kalshi event
    seen_kalshi = set()
    deduped = []
    for m in matches:
        k_ticker = m["kalshi_event"].get("event_ticker", "")
        if k_ticker not in seen_kalshi:
            seen_kalshi.add(k_ticker)
            deduped.append(m)

    return deduped


def _get_title(market: dict) -> str:
    """Extract the best title from a market dict regardless of platform."""
    return (
        market.get("question", "")
        or market.get("title", "")
        or market.get("name", "")
        or market.get("shortName", "")
        or ""
    )


def _get_market_id(market: dict) -> str:
    """Extract a unique identifier from a market dict regardless of platform."""
    return (
        market.get("conditionId", "")
        or market.get("ticker", "")
        or market.get("event_ticker", "")
        or market.get("id", "")
        or market.get("slug", "")
        or _get_title(market)
    )


def verify_cross_platform_equivalence_jev(
    market_a: dict,
    market_b: dict,
    platform_a: str,
    platform_b: str,
    client=None,
    min_confidence: float = 0.60,
) -> tuple[bool, float, str]:
    """Verify semantic and resolution equivalence of two cross-platform markets using Jev.

    Args:
        market_a: Market dict from platform A.
        market_b: Market dict from platform B.
        platform_a: Platform name (e.g. 'polymarket').
        platform_b: Platform name (e.g. 'kalshi').
        client: Optional JevClient instance.
        min_confidence: Minimum confidence required to accept equivalence.

    Returns:
        Tuple of (is_equivalent: bool, confidence: float, reasoning: str).
    """
    try:
        from jev_client import get_jev_client
        j_client = client or get_jev_client()
        if not j_client.is_available():
            return False, 0.0, "Jev unavailable; fail-closed safety gate"

        from jev_semantics import equivalence_questions, settlement_rules
        rules_a, rules_b = settlement_rules(market_a), settlement_rules(market_b)
        if not rules_a or not rules_b:
            return False, 0.0, "Review required: missing settlement rules; fail-closed"
        state = {
            "platform_a": platform_a, "question_a": _get_title(market_a), "rules_a": rules_a,
            "platform_b": platform_b, "question_b": _get_title(market_b), "rules_b": rules_b,
        }
        questions = equivalence_questions()

        resp = j_client.query_decisions(state, questions)
        answers = resp.get("answers", {})
        choice = answers.get("is_equivalent", {}).get("choice", "different_events")
        conf = float(answers.get("is_equivalent", {}).get("confidence", 0.0))
        noul_p = float(answers.get("probability", {}).get("noul", 0.0))

        equiv = (choice == "identical") and (0.85 <= noul_p <= 1.0) and (max(0.90, min_confidence) <= conf <= 1.0)
        return equiv, conf, f"Jev: choice={choice}, P={noul_p:.2f}, conf={conf:.2f}"
    except Exception as e:
        logger.debug("Jev equivalence check failed: %s", e)
        return False, 0.0, f"Error: {e}"


def match_cross_platform(
    markets_a: list[dict],
    markets_b: list[dict],
    platform_a: str,
    platform_b: str,
    threshold: int = 80,
    min_confidence: str = "LOW",
    verify_with_jev: bool = False,
    jev_client=None,
) -> list[dict]:
    """Platform-agnostic cross-platform matching between any two market lists.

    Args:
        markets_a: List of market dicts from platform A.
        markets_b: List of market dicts from platform B.
        platform_a: Name of platform A (e.g. "polymarket").
        platform_b: Name of platform B (e.g. "kalshi").
        threshold: Minimum combined score for a match.
        min_confidence: Minimum confidence tier (HIGH/MEDIUM/LOW).

    Returns:
        List of matched pairs with similarity scores.
    """
    matches = []
    min_conf_level = CONFIDENCE_ORDER.get(min_confidence.upper(), 1)

    # Pre-normalize platform B markets
    b_prepared = []
    for mb in markets_b:
        title = _get_title(mb)
        if not title:
            continue
        norm = normalize_title(title)
        entities = _extract_entities(title)
        b_prepared.append((norm, entities, mb))

    for ma in markets_a:
        a_title = _get_title(ma)
        a_norm = normalize_title(a_title)
        a_entities = _extract_entities(a_title)

        if not a_norm or len(a_norm) < 8:
            continue

        best_score = 0
        best_match = None
        best_entity_overlap = 0
        best_min_entities = 0

        # Adaptive entity overlap: short titles (both <=6 words) need only 1
        a_word_count = len(a_norm.split())

        for b_norm, b_entities, mb in b_prepared:
            if not b_norm:
                continue

            overlap = a_entities & b_entities
            b_word_count = len(b_norm.split())
            min_overlap = 1 if (a_word_count <= 6 and b_word_count <= 6) else 2
            if len(overlap) < min_overlap:
                continue

            score = fuzz.token_sort_ratio(a_norm, b_norm)

            # Fallback: try partial_ratio if token_sort is borderline
            if score < threshold and score >= threshold - 15:
                partial = fuzz.partial_ratio(a_norm, b_norm)
                score = max(score, partial)

            min_entities = min(len(a_entities), len(b_entities))
            entity_ratio = len(overlap) / min_entities if min_entities > 0 else 0
            combined = score + (entity_ratio * 15)

            if combined > best_score:
                best_score = combined
                best_match = mb
                best_entity_overlap = len(overlap)
                best_min_entities = min_entities

        # Short titles need only 1 shared entity
        final_min_overlap = 1 if a_word_count <= 6 else 2
        if best_score >= threshold and best_match and best_entity_overlap >= final_min_overlap:
            confidence = classify_confidence(
                int(best_score), best_entity_overlap, best_min_entities
            )
            if CONFIDENCE_ORDER.get(confidence, 0) < min_conf_level:
                continue
            if verify_with_jev:
                is_eq, _, reason = verify_cross_platform_equivalence_jev(
                    ma, best_match, platform_a, platform_b, client=jev_client
                )
                if not is_eq:
                    logger.debug("Jev rejected equivalence: %s", reason)
                    continue
            matches.append({
                "market_a": ma,
                "market_b": best_match,
                "platform_a": platform_a,
                "platform_b": platform_b,
                "similarity": int(best_score),
                "entity_overlap": best_entity_overlap,
                "confidence": confidence,
                "title_a": a_title,
                "title_b": _get_title(best_match),
            })

    matches.sort(key=lambda x: x["similarity"], reverse=True)

    # Deduplicate: best match per platform B market
    seen_b = set()
    deduped = []
    for m in matches:
        b_id = _get_market_id(m["market_b"])
        if b_id not in seen_b:
            seen_b.add(b_id)
            deduped.append(m)

    return deduped


_INVERSION_SIGNALS = [
    "not", "won't", "wouldn't", "cannot", "can't",
    "below", "under", "less than", "fewer",
    "fail to", "fails to", "decline", "drop below",
    "lose", "loses", "defeat", "defeats", "against",
    "no longer", "never", "neither",
]


def detect_inverted(pm_title: str, kalshi_title: str) -> bool:
    """Detect if a Kalshi market is inverted relative to Polymarket.

    Some markets may be phrased oppositely:
    - Polymarket: "Will X happen?" (YES = it happens)
    - Kalshi: "X will NOT happen" (YES = it doesn't happen)

    Uses XOR logic: inversion only if one title has negation, not both.
    """
    pm_lower = pm_title.lower()
    kalshi_lower = kalshi_title.lower()

    def _has_inversion(text):
        return any(f" {signal} " in f" {text} " for signal in _INVERSION_SIGNALS)

    pm_inv = _has_inversion(pm_lower)
    kalshi_inv = _has_inversion(kalshi_lower)

    # XOR: inversion only if exactly one title has negation signals
    return pm_inv != kalshi_inv


# ---------------------------------------------------------------------------
# Embedding-based semantic matcher (optional — requires fastembed)
# ---------------------------------------------------------------------------

# Confidence tiers for semantic matching (cosine similarity thresholds)
SEMANTIC_HIGH = 0.90
SEMANTIC_MEDIUM = 0.80
# LOW = anything >= SEMANTIC_MATCH_THRESHOLD (default 0.70)


def _cosine_similarity(a: np.ndarray, b: np.ndarray) -> float:
    """Compute cosine similarity between two vectors.

    Args:
        a: First embedding vector.
        b: Second embedding vector.

    Returns:
        Cosine similarity in [-1, 1].
    """
    dot = np.dot(a, b)
    norm_a = np.linalg.norm(a)
    norm_b = np.linalg.norm(b)
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return float(dot / (norm_a * norm_b))


def _semantic_confidence(similarity: float) -> str:
    """Classify semantic match confidence based on cosine similarity.

    Args:
        similarity: Cosine similarity score.

    Returns:
        Confidence tier string: HIGH, MEDIUM, or LOW.
    """
    if similarity >= SEMANTIC_HIGH:
        return "HIGH"
    elif similarity >= SEMANTIC_MEDIUM:
        return "MEDIUM"
    return "LOW"


class EmbeddingMatcher:
    """Singleton embedding matcher using fastembed for semantic market matching.

    Lazily loads the all-MiniLM-L6-v2 model on first use and caches
    embeddings per normalized title to avoid re-computation across scan
    cycles. Only new/changed market titles are embedded each cycle.

    The model uses ONNX runtime (~150-200MB RAM) instead of PyTorch,
    making it suitable for the 2048MB Fargate task.
    """

    _instance: "EmbeddingMatcher | None" = None
    _MODEL_NAME = "sentence-transformers/all-MiniLM-L6-v2"

    def __init__(self):
        self._model = None
        self._cache: dict[str, np.ndarray] = {}

    @classmethod
    def get_instance(cls) -> "EmbeddingMatcher":
        """Return the singleton EmbeddingMatcher instance."""
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    def _ensure_model(self):
        """Lazy-load the fastembed model on first use."""
        if self._model is not None:
            return
        try:
            from fastembed import TextEmbedding
            self._model = TextEmbedding(model_name=self._MODEL_NAME)
            logger.info("Loaded embedding model %s", self._MODEL_NAME)
        except ImportError:
            logger.warning("fastembed not installed — semantic matching unavailable")
            raise
        except Exception:
            logger.exception("Failed to load embedding model %s", self._MODEL_NAME)
            raise

    def embed_titles(self, titles: list[str]) -> list[np.ndarray]:
        """Embed a batch of titles, using cache for previously seen titles.

        Args:
            titles: List of normalized title strings.

        Returns:
            List of embedding vectors (same order as input).
        """
        self._ensure_model()

        results: list[np.ndarray | None] = [None] * len(titles)
        to_embed: list[tuple[int, str]] = []

        for i, title in enumerate(titles):
            if title in self._cache:
                results[i] = self._cache[title]
            else:
                to_embed.append((i, title))

        if to_embed:
            texts = [t for _, t in to_embed]
            embeddings = list(self._model.embed(texts))
            for (idx, title), emb in zip(to_embed, embeddings):
                vec = np.array(emb, dtype=np.float32)
                self._cache[title] = vec
                results[idx] = vec

        return results

    def clear_cache(self):
        """Clear the embedding cache (e.g. between full scan cycles)."""
        self._cache.clear()

    @property
    def cache_size(self) -> int:
        """Number of cached embeddings."""
        return len(self._cache)


def match_cross_platform_semantic(
    markets_a: list[dict],
    markets_b: list[dict],
    platform_a: str,
    platform_b: str,
    threshold: float = 0.70,
    min_confidence: str = "LOW",
) -> list[dict]:
    """Embedding-based cross-platform matching using cosine similarity.

    Uses a two-stage pipeline:
    1. Entity overlap pre-filter (>= 1 shared entity) to skip obviously
       unrelated pairs and avoid O(N*M) cosine comparisons.
    2. Cosine similarity of normalized title embeddings.

    Falls back to fuzzy matching (match_cross_platform) if fastembed is
    unavailable.

    Args:
        markets_a: List of market dicts from platform A.
        markets_b: List of market dicts from platform B.
        platform_a: Name of platform A.
        platform_b: Name of platform B.
        threshold: Minimum cosine similarity for a match (0-1).
        min_confidence: Minimum confidence tier (HIGH/MEDIUM/LOW).

    Returns:
        List of matched pairs with similarity scores and confidence.
    """
    try:
        matcher = EmbeddingMatcher.get_instance()
    except Exception:
        logger.info("Falling back to fuzzy matching for %s vs %s", platform_a, platform_b)
        return match_cross_platform(
            markets_a, markets_b, platform_a, platform_b,
            threshold=80, min_confidence=min_confidence,
        )

    min_conf_level = CONFIDENCE_ORDER.get(min_confidence.upper(), 1)

    # Pre-normalize and extract entities for all markets
    a_prepared: list[tuple[str, set[str], dict]] = []
    for ma in markets_a:
        title = _get_title(ma)
        if not title or len(title) < 8:
            continue
        norm = normalize_title(title)
        entities = _extract_entities(title)
        a_prepared.append((norm, entities, ma))

    b_prepared: list[tuple[str, set[str], dict]] = []
    for mb in markets_b:
        title = _get_title(mb)
        if not title:
            continue
        norm = normalize_title(title)
        entities = _extract_entities(title)
        b_prepared.append((norm, entities, mb))

    if not a_prepared or not b_prepared:
        return []

    # Batch-embed all titles
    all_a_titles = [norm for norm, _, _ in a_prepared]
    all_b_titles = [norm for norm, _, _ in b_prepared]

    try:
        a_embeddings = matcher.embed_titles(all_a_titles)
        b_embeddings = matcher.embed_titles(all_b_titles)
    except Exception:
        logger.warning("Embedding failed, falling back to fuzzy matching")
        return match_cross_platform(
            markets_a, markets_b, platform_a, platform_b,
            threshold=80, min_confidence=min_confidence,
        )

    # Find best match for each A market
    matches = []
    for i, (a_norm, a_entities, ma) in enumerate(a_prepared):
        best_sim = 0.0
        best_j = -1
        best_overlap = 0
        best_min_ent = 0

        for j, (b_norm, b_entities, mb) in enumerate(b_prepared):
            # Stage 1: entity overlap pre-filter (at least 1 shared entity)
            overlap = a_entities & b_entities
            if len(overlap) < 1:
                continue

            # Stage 2: cosine similarity
            sim = _cosine_similarity(a_embeddings[i], b_embeddings[j])

            if sim > best_sim:
                best_sim = sim
                best_j = j
                best_overlap = len(overlap)
                best_min_ent = min(len(a_entities), len(b_entities))

        if best_sim >= threshold and best_j >= 0:
            confidence = _semantic_confidence(best_sim)
            if CONFIDENCE_ORDER.get(confidence, 0) < min_conf_level:
                continue

            b_norm, b_entities, mb = b_prepared[best_j]
            matches.append({
                "market_a": ma,
                "market_b": mb,
                "platform_a": platform_a,
                "platform_b": platform_b,
                "similarity": int(best_sim * 100),
                "cosine_similarity": best_sim,
                "entity_overlap": best_overlap,
                "confidence": confidence,
                "title_a": _get_title(ma),
                "title_b": _get_title(mb),
            })

    # Sort by cosine similarity descending
    matches.sort(key=lambda x: x["cosine_similarity"], reverse=True)

    # Deduplicate: best match per platform B market
    seen_b: set[str] = set()
    deduped: list[dict] = []
    for m in matches:
        b_id = _get_market_id(m["market_b"])
        if b_id not in seen_b:
            seen_b.add(b_id)
            deduped.append(m)

    return deduped


def match_markets_to_events_semantic(
    polymarket_markets: list[dict],
    kalshi_events: list[dict],
    threshold: float = 0.70,
    min_confidence: str = "LOW",
) -> list[dict]:
    """Embedding-based matching of Polymarket markets to Kalshi events.

    Semantic counterpart of ``match_markets_to_events()``.  Uses the same
    two-stage pipeline as ``match_cross_platform_semantic()`` —
    entity-overlap pre-filter followed by cosine similarity — but operates
    on PM markets vs Kalshi *events* rather than generic market lists.

    Falls back to fuzzy ``match_markets_to_events()`` if fastembed is
    unavailable.

    Args:
        polymarket_markets: List of Polymarket market dicts.
        kalshi_events: List of Kalshi event dicts (title/event_ticker).
        threshold: Minimum cosine similarity for a match (0-1).
        min_confidence: Minimum confidence tier (HIGH/MEDIUM/LOW).

    Returns:
        List of matched pairs with keys ``polymarket``, ``kalshi_event``,
        ``similarity``, ``cosine_similarity``, ``entity_overlap``, and
        ``confidence``.
    """
    try:
        matcher = EmbeddingMatcher.get_instance()
    except Exception:
        logger.info("Falling back to fuzzy matching for PM vs Kalshi events")
        return match_markets_to_events(
            polymarket_markets, kalshi_events,
            threshold=80, min_confidence=min_confidence,
        )

    min_conf_level = CONFIDENCE_ORDER.get(min_confidence.upper(), 1)

    # Pre-normalize Polymarket markets
    pm_prepared: list[tuple[str, set[str], dict]] = []
    for pm in polymarket_markets:
        title = pm.get("question", "") or pm.get("title", "")
        if not title or len(title) < 8:
            continue
        norm = normalize_title(title)
        entities = _extract_entities(title)
        pm_prepared.append((norm, entities, pm))

    # Pre-normalize Kalshi events
    ke_prepared: list[tuple[str, set[str], dict]] = []
    for ke in kalshi_events:
        title = ke.get("title", "")
        if not title:
            continue
        norm = normalize_title(title)
        entities = _extract_entities(title)
        ke_prepared.append((norm, entities, ke))

    if not pm_prepared or not ke_prepared:
        return []

    # Batch-embed all titles
    pm_titles = [norm for norm, _, _ in pm_prepared]
    ke_titles = [norm for norm, _, _ in ke_prepared]

    try:
        pm_embeddings = matcher.embed_titles(pm_titles)
        ke_embeddings = matcher.embed_titles(ke_titles)
    except Exception:
        logger.warning("Embedding failed, falling back to fuzzy matching")
        return match_markets_to_events(
            polymarket_markets, kalshi_events,
            threshold=80, min_confidence=min_confidence,
        )

    # Find best Kalshi event for each Polymarket market
    matches = []
    for i, (pm_norm, pm_entities, pm) in enumerate(pm_prepared):
        best_sim = 0.0
        best_j = -1
        best_overlap = 0
        best_min_ent = 0

        for j, (ke_norm, ke_entities, ke) in enumerate(ke_prepared):
            # Entity overlap pre-filter (>= 1 shared entity)
            overlap = pm_entities & ke_entities
            if len(overlap) < 1:
                continue

            sim = _cosine_similarity(pm_embeddings[i], ke_embeddings[j])

            if sim > best_sim:
                best_sim = sim
                best_j = j
                best_overlap = len(overlap)
                best_min_ent = min(len(pm_entities), len(ke_entities))

        if best_sim >= threshold and best_j >= 0:
            confidence = _semantic_confidence(best_sim)
            if CONFIDENCE_ORDER.get(confidence, 0) < min_conf_level:
                continue

            _, _, ke = ke_prepared[best_j]
            pm_title = pm.get("question", "") or pm.get("title", "")
            matches.append({
                "polymarket": pm,
                "kalshi_event": ke,
                "similarity": int(best_sim * 100),
                "cosine_similarity": best_sim,
                "entity_overlap": best_overlap,
                "confidence": confidence,
                "pm_title": pm_title,
                "kalshi_title": ke.get("title", ""),
            })

    # Sort by cosine similarity descending
    matches.sort(key=lambda x: x["cosine_similarity"], reverse=True)

    # Deduplicate: best match per Kalshi event
    seen_kalshi: set[str] = set()
    deduped: list[dict] = []
    for m in matches:
        k_ticker = m["kalshi_event"].get("event_ticker", "")
        if k_ticker not in seen_kalshi:
            seen_kalshi.add(k_ticker)
            deduped.append(m)

    return deduped
