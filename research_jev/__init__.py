"""Bounded financial research over timestamped snapshots; no execution paths."""

from research_jev.workflows import (
    analyze_transcript,
    classify_event,
    compare_settlement_language,
    detect_incentive_changes,
    rank_research_attention,
    record_paper_features,
    screen_novelty,
    source_to_market_relevance,
)
from research_jev.paper import evaluate_paper_forecasts

__all__ = [
    "analyze_transcript", "classify_event", "compare_settlement_language",
    "detect_incentive_changes", "rank_research_attention", "record_paper_features",
    "screen_novelty", "source_to_market_relevance", "evaluate_paper_forecasts",
]
