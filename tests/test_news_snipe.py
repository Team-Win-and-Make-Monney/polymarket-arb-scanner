"""Unit tests for news-driven resolution sniping strategy (STRAT-02)."""

import sys
import time
from unittest.mock import Mock, MagicMock, patch

import pytest

# Mock finnhub_api before importing news_snipe
sys.modules["finnhub_api"] = MagicMock()

from scans.news_snipe import (
    extract_news_signals,
    _score_sentiment,
    _refine_news_with_confidence,
    YES_KEYWORDS,
    NO_KEYWORDS,
)


@pytest.fixture(autouse=True)
def cleanup_modules():
    """Clean up test module imports to prevent cross-test pollution."""
    yield
    sys.modules.pop("scans.news_snipe", None)
    sys.modules.pop("finnhub_api", None)


# ---------------------------------------------------------------------------
# Test Fixtures: Mock Data
# ---------------------------------------------------------------------------


@pytest.fixture
def sample_headlines():
    """Sample headlines from Finnhub."""
    return [
        {
            "headline": "FDA Approves New Treatment for Condition X",
            "summary": "The FDA has approved a new treatment option.",
            "url": "https://example.com/fda-approval",
            "datetime": 1712282400,
        },
        {
            "headline": "FDA Rejects Application for Drug Y",
            "summary": "The FDA has denied approval for the new drug application.",
            "url": "https://example.com/fda-rejection",
            "datetime": 1712282500,
        },
        {
            "headline": "Bitcoin Reaches New High",
            "summary": "Bitcoin trading continues upward trend.",
            "url": "https://example.com/bitcoin",
            "datetime": 1712282600,
        },
    ]


@pytest.fixture
def sample_markets():
    """Sample markets dict for matching."""
    return {
        "market_fda_approval": {
            "question": "Will the FDA approve a new treatment for Condition X?",
            "clobTokenIds": ["token-yes-1", "token-no-1"],
        },
        "market_fda_rejection": {
            "question": "Will the FDA reject the application for Drug Y?",
            "clobTokenIds": ["token-yes-2", "token-no-2"],
        },
        "market_bitcoin": {
            "question": "Will Bitcoin reach $100,000 by end of year?",
            "clobTokenIds": ["token-yes-3", "token-no-3"],
        },
    }


# ---------------------------------------------------------------------------
# Test Class: Headline Matching
# ---------------------------------------------------------------------------


class TestHeadlineMatching:
    """Test extract_news_signals() headline-to-market matching via fuzzy match."""

    def test_matches_headline_to_market(self, sample_headlines, sample_markets):
        """FDA approval headline matches FDA approval market."""
        headlines = [sample_headlines[0]]  # "FDA Approves New Treatment..."
        signals = extract_news_signals(headlines, sample_markets, fuzzy_threshold=70)

        # Should match to market_fda_approval
        assert len(signals) >= 1
        assert any("FDA" in sig["market"] for sig in signals)

    def test_rejects_low_similarity(self, sample_headlines, sample_markets):
        """Unrelated headline (Bitcoin) should not match FDA market."""
        headlines = [sample_headlines[2]]  # "Bitcoin Reaches New High"
        signals = extract_news_signals(headlines, sample_markets, fuzzy_threshold=70)

        # Should not match FDA market (similarity too low)
        fda_matches = [sig for sig in signals if "FDA" in sig["market"]]
        assert len(fda_matches) == 0

    def test_case_insensitive_matching(self, sample_markets):
        """Fuzzy matching should be case-insensitive."""
        headlines = [
            {
                "headline": "fda APPROVED the drug",
                "summary": "lowercase and uppercase mixed",
                "url": "https://example.com",
                "datetime": 1712282400,
            }
        ]
        signals = extract_news_signals(headlines, sample_markets, fuzzy_threshold=60)

        # Should match despite case differences
        assert len(signals) > 0

    def test_multiple_headlines_single_market(self, sample_headlines, sample_markets):
        """Multiple headlines can match to same market."""
        headlines = sample_headlines[:2]  # FDA approval and rejection
        signals = extract_news_signals(headlines, sample_markets, fuzzy_threshold=70)

        # Both FDA headlines should produce signals
        assert len(signals) >= 2


# ---------------------------------------------------------------------------
# Test Class: Sentiment Scoring
# ---------------------------------------------------------------------------


class TestSentimentScoring:
    """Test _score_sentiment() keyword detection and confidence scoring."""

    def test_yes_keywords_detected(self):
        """Sentiment scorer detects YES keywords."""
        for keyword in YES_KEYWORDS:
            text = f"The event result is {keyword}."
            result = _score_sentiment(text)
            assert result["sentiment"] == "YES"
            assert result["confidence"] == 0.8

    def test_no_keywords_detected(self):
        """Sentiment scorer detects NO keywords."""
        for keyword in NO_KEYWORDS:
            text = f"The application was {keyword}."
            result = _score_sentiment(text)
            assert result["sentiment"] == "NO"
            assert result["confidence"] == 0.8

    def test_no_sentiment_found(self):
        """Text without sentiment keywords returns None."""
        text = "Bitcoin traded higher today without any news."
        result = _score_sentiment(text)
        assert result["sentiment"] is None
        assert result["confidence"] == 0.0

    def test_keyword_substrings_do_not_match(self):
        result = _score_sentiment("The request was unapproved.")
        assert result["sentiment"] is None
        assert result["confidence"] == 0.0

    def test_confidence_level_yes(self):
        """YES keyword match returns 0.8 confidence."""
        text = "The proposal was approved by the committee."
        result = _score_sentiment(text)
        assert result["sentiment"] == "YES"
        assert result["confidence"] == 0.8

    def test_confidence_level_no(self):
        """NO keyword match returns 0.8 confidence."""
        text = "The bill was rejected in the senate vote."
        result = _score_sentiment(text)
        assert result["sentiment"] == "NO"
        assert result["confidence"] == 0.8

    def test_first_match_wins(self):
        """When both YES and NO keywords present, first match wins."""
        # Text with both: "approved" appears first
        text = "The plan was approved but later rejected."
        result = _score_sentiment(text)
        assert result["sentiment"] == "YES"  # approved is checked first

    def test_multiple_keywords_same_sentiment(self):
        """Multiple YES keywords in text still returns single YES with 0.8 confidence."""
        text = "The project was approved and completed successfully."
        result = _score_sentiment(text)
        assert result["sentiment"] == "YES"
        assert result["confidence"] == 0.8


# ---------------------------------------------------------------------------
# Test Class: Cooldown Logic
# ---------------------------------------------------------------------------


class TestCooldown:
    """Test _refine_news_with_confidence() cooldown enforcement."""

    def test_prevents_duplicate_execution(self):
        """Cooldown cache prevents executing same market within window."""
        opportunities = [
            {
                "type": "NewsSnipe",
                "market": "Test Market",
                "_headline": "Test headline",
                "_sentiment": "YES",
                "_confidence": 0.8,
                "_market_key": "market_1",
            }
        ]

        # Set cooldown: market_1 executed 1 second ago (within 30s window)
        current_time = time.time()
        cooldown_cache = {
            "market_1": current_time + 29.0,  # Expires in 29 seconds
        }

        refined = _refine_news_with_confidence(opportunities, cooldown_cache=cooldown_cache)

        # Should be filtered out (cooldown active)
        assert len(refined) == 0

    def test_allows_after_cooldown_expires(self):
        """Cooldown expiration allows re-execution."""
        opportunities = [
            {
                "type": "NewsSnipe",
                "market": "Test Market",
                "_headline": "Test headline",
                "_sentiment": "YES",
                "_confidence": 0.8,
                "_market_key": "market_1",
            }
        ]

        # Set cooldown: market_1 executed 60 seconds ago (expired)
        current_time = time.time()
        cooldown_cache = {
            "market_1": current_time - 1.0,  # Expired 1 second ago
        }

        refined = _refine_news_with_confidence(opportunities, cooldown_cache=cooldown_cache)

        # Should pass (cooldown expired)
        assert len(refined) == 1

    def test_cooldown_not_set_allows_execution(self):
        """Markets without cooldown entry pass through."""
        opportunities = [
            {
                "type": "NewsSnipe",
                "market": "Test Market",
                "_headline": "Test headline",
                "_sentiment": "YES",
                "_confidence": 0.8,
                "_market_key": "market_1",
            }
        ]

        cooldown_cache = {}

        refined = _refine_news_with_confidence(opportunities, cooldown_cache=cooldown_cache)

        # Should pass (no cooldown entry)
        assert len(refined) == 1


# ---------------------------------------------------------------------------
# Test Class: Refinement and Confidence Thresholds
# ---------------------------------------------------------------------------


class TestRefinement:
    """Test _refine_news_with_confidence() confidence threshold filtering."""

    def test_rejects_low_confidence(self):
        """Opportunities with confidence < 0.5 are rejected."""
        opportunities = [
            {
                "type": "NewsSnipe",
                "market": "Test Market",
                "_headline": "Test headline",
                "_sentiment": "YES",
                "_confidence": 0.3,  # Below threshold
                "_market_key": "market_1",
            }
        ]

        refined = _refine_news_with_confidence(opportunities, confidence_floor=0.5)

        assert len(refined) == 0

    def test_accepts_high_confidence(self):
        """Opportunities with confidence >= 0.5 are accepted."""
        opportunities = [
            {
                "type": "NewsSnipe",
                "market": "Test Market",
                "_headline": "Test headline",
                "_sentiment": "YES",
                "_confidence": 0.8,  # Above threshold
                "_market_key": "market_1",
            }
        ]

        refined = _refine_news_with_confidence(opportunities, confidence_floor=0.5)

        assert len(refined) == 1

    def test_returns_refined_list(self):
        """Stage 2 refinement filters mixed-confidence opportunities."""
        opportunities = [
            {
                "type": "NewsSnipe",
                "market": f"Market {i}",
                "_headline": f"Headline {i}",
                "_sentiment": "YES",
                "_confidence": conf,
                "_market_key": f"market_{i}",
            }
            for i, conf in enumerate([0.3, 0.5, 0.6, 0.7, 0.8])
        ]

        refined = _refine_news_with_confidence(opportunities, confidence_floor=0.5)

        # Should keep 4 opportunities (conf >= 0.5: 0.5, 0.6, 0.7, 0.8)
        assert len(refined) == 4

    def test_confidence_boundary(self):
        """Confidence = threshold is accepted (>= not >)."""
        opportunities = [
            {
                "type": "NewsSnipe",
                "market": "Test Market",
                "_headline": "Test headline",
                "_sentiment": "YES",
                "_confidence": 0.5,  # Exactly at threshold
                "_market_key": "market_1",
            }
        ]

        refined = _refine_news_with_confidence(opportunities, confidence_floor=0.5)

        assert len(refined) == 1


# ---------------------------------------------------------------------------
# Test Class: Integration
# ---------------------------------------------------------------------------


class TestIntegration:
    """Integration tests combining multiple components."""

    def test_full_pipeline_signal_extraction(self, sample_headlines, sample_markets):
        """Full pipeline: headline -> signal -> refinement."""
        headlines = [
            {
                "headline": "FDA Approves New Treatment for Condition X",
                "summary": "The FDA has approved the treatment.",
                "url": "https://example.com",
                "datetime": 1712282400,
            }
        ]

        # Extract signals
        signals = extract_news_signals(headlines, sample_markets, fuzzy_threshold=70)
        assert len(signals) > 0

        # Verify signal structure
        signal = signals[0]
        assert signal["type"] == "NewsSnipe"
        assert signal["_sentiment"] in ("YES", "NO", None)
        assert "headline" in signal["_headline"].lower() or "fda" in signal["_headline"].lower()

    def test_no_signal_on_missing_headline(self, sample_markets):
        """Missing headline text is handled gracefully."""
        headlines = [{"headline": "", "summary": "Empty headline", "datetime": 1712282400}]

        signals = extract_news_signals(headlines, sample_markets, fuzzy_threshold=70)

        assert len(signals) == 0

    def test_no_signal_on_missing_market_question(self, sample_headlines):
        """Missing market question is handled gracefully."""
        markets = {"market_1": {"question": "", "clobTokenIds": []}}

        signals = extract_news_signals(sample_headlines, markets, fuzzy_threshold=70)

        assert len(signals) == 0
