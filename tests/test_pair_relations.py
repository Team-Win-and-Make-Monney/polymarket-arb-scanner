"""Tests for pair_relations.py — subset/implication pair discovery."""

import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from pair_relations import (
    parse_threshold_market,
    discover_subset_pairs,
    _normalize_num,
    _normalize_underlying,
)


class TestThresholdParsing:
    def test_normalize_num_basic_and_suffixes(self):
        assert _normalize_num("100") == 100.0
        assert _normalize_num("1,000") == 1000.0
        assert _normalize_num("95.5") == 95.5
        assert _normalize_num("100", "will btc be above 100k?") == 100000.0
        assert _normalize_num("2.5", "will eth reach 2.5m?") == 2500000.0
        assert _normalize_num("1", "market cap over 1b") == 1000000000.0
        assert _normalize_num("abc") is None

    def test_normalize_underlying(self):
        assert _normalize_underlying("Will Bitcoin be") == "btc"
        assert _normalize_underlying("Ethereum") == "eth"
        assert _normalize_underlying("Price of Solana") == "sol"
        assert _normalize_underlying("US Inflation Rate") == "us inflation rate"

    def test_parse_kalshi_ticker_above(self):
        mkt = {
            "ticker": "KXBTC-26DEC31-T100000",
            "title": "Will BTC be above $100,000 on Dec 31, 2026?",
            "platform": "kalshi",
        }
        spec = parse_threshold_market(mkt, platform="kalshi")
        assert spec is not None
        assert spec.underlying == "kxbtc"
        assert spec.direction == "above"
        assert spec.strike == 100000.0
        assert spec.date_key == "26DEC31"
        assert spec.platform == "kalshi"

    def test_parse_kalshi_ticker_below(self):
        mkt = {
            "ticker": "KXINFL-26DEC-B2.5",
            "title": "Will US Inflation be below 2.5%?",
            "platform": "kalshi",
        }
        spec = parse_threshold_market(mkt, platform="kalshi")
        assert spec is not None
        assert spec.direction == "below"
        assert spec.strike == 2.5
        assert spec.date_key == "26DEC"

    def test_parse_polymarket_above_titles(self):
        m1 = {
            "title": "Will Bitcoin be above $100k on Dec 31?",
            "end_date_iso": "2026-12-31T00:00:00Z",
            "condition_id": "0x111",
        }
        spec1 = parse_threshold_market(m1, platform="polymarket")
        assert spec1 is not None
        assert spec1.underlying == "btc"
        assert spec1.direction == "above"
        assert spec1.strike == 100000.0
        assert spec1.date_key == "2026-12-31"

        m2 = {
            "title": "ETH over $4,000 on Friday",
            "end_date_iso": "2026-10-15T00:00:00Z",
            "condition_id": "0x222",
        }
        spec2 = parse_threshold_market(m2, platform="polymarket")
        assert spec2 is not None
        assert spec2.underlying == "eth"
        assert spec2.direction == "above"
        assert spec2.strike == 4000.0

        m3 = {
            "title": "Solana $200+ by year end",
            "end_date_iso": "2026-12-31T00:00:00Z",
            "condition_id": "0x333",
        }
        spec3 = parse_threshold_market(m3, platform="polymarket")
        assert spec3 is not None
        assert spec3.underlying == "sol"
        assert spec3.direction == "above"
        assert spec3.strike == 200.0

    def test_parse_polymarket_below_titles(self):
        m1 = {
            "title": "Will Bitcoin be under $80k?",
            "end_date_iso": "2026-12-31T00:00:00Z",
            "condition_id": "0x444",
        }
        spec1 = parse_threshold_market(m1, platform="polymarket")
        assert spec1 is not None
        assert spec1.underlying == "btc"
        assert spec1.direction == "below"
        assert spec1.strike == 80000.0

        m2 = {
            "title": "CPI below 2.5% in Dec 2026",
            "end_date_iso": "2026-12-31T00:00:00Z",
            "condition_id": "0x555",
        }
        spec2 = parse_threshold_market(m2, platform="polymarket")
        assert spec2 is not None
        assert spec2.direction == "below"
        assert spec2.strike == 2.5

    def test_parse_non_threshold_market_returns_none(self):
        m = {
            "title": "Who will win the presidential election in 2028?",
            "end_date_iso": "2028-11-05T00:00:00Z",
            "condition_id": "0x666",
        }
        assert parse_threshold_market(m, platform="polymarket") is None


class TestSubsetPairDiscovery:
    def test_discover_subset_pairs_above_strikes(self):
        # BTC > $100k implies BTC > $90k
        # Higher strike (100k) is subset (A), lower strike (90k) is superset (B)
        m90 = {
            "condition_id": "0xbtc90",
            "title": "Will Bitcoin be above $90k by Dec 31?",
            "end_date_iso": "2026-12-31T00:00:00Z",
            "tokens": [{"token_id": "tok90_yes", "outcome": "Yes"}, {"token_id": "tok90_no", "outcome": "No"}],
        }
        m100 = {
            "condition_id": "0xbtc100",
            "title": "Will Bitcoin be above $100k by Dec 31?",
            "end_date_iso": "2026-12-31T00:00:00Z",
            "tokens": [{"token_id": "tok100_yes", "outcome": "Yes"}, {"token_id": "tok100_no", "outcome": "No"}],
        }

        pairs = discover_subset_pairs([m90, m100], platform="polymarket")
        assert len(pairs) == 1
        pair = pairs[0]
        # sub (A) should be 100k, sup (B) should be 90k
        assert pair["sub"]["condition_id"] == "0xbtc100"
        assert pair["sup"]["condition_id"] == "0xbtc90"
        assert pair["confidence"] == 1.0
        assert pair["_sub_strike"] == 100000.0
        assert pair["_sup_strike"] == 90000.0

    def test_discover_subset_pairs_below_strikes(self):
        # CPI < 2.0% implies CPI < 3.0%
        # Lower strike (2.0%) is subset (A), higher strike (3.0%) is superset (B)
        m2 = {
            "condition_id": "0xcpi2",
            "title": "US CPI under 2.0% in Dec",
            "end_date_iso": "2026-12-31T00:00:00Z",
        }
        m3 = {
            "condition_id": "0xcpi3",
            "title": "US CPI under 3.0% in Dec",
            "end_date_iso": "2026-12-31T00:00:00Z",
        }

        pairs = discover_subset_pairs([m2, m3], platform="polymarket")
        assert len(pairs) == 1
        pair = pairs[0]
        # sub (A) is 2.0%, sup (B) is 3.0%
        assert pair["sub"]["condition_id"] == "0xcpi2"
        assert pair["sup"]["condition_id"] == "0xcpi3"
        assert pair["_sub_strike"] == 2.0
        assert pair["_sup_strike"] == 3.0

    def test_discover_subset_pairs_date_mismatch_ignored(self):
        m1 = {
            "condition_id": "0x1",
            "title": "Will Bitcoin be above $90k by Oct 31?",
            "end_date_iso": "2026-10-31T00:00:00Z",
        }
        m2 = {
            "condition_id": "0x2",
            "title": "Will Bitcoin be above $100k by Dec 31?",
            "end_date_iso": "2026-12-31T00:00:00Z",
        }
        pairs = discover_subset_pairs([m1, m2], platform="polymarket")
        assert len(pairs) == 0

    def test_discover_subset_pairs_manual_rules(self):
        m_cand_nom = {
            "condition_id": "0xnom",
            "title": "Candidate X wins nomination",
            "platform": "polymarket",
        }
        m_cand_pres = {
            "condition_id": "0xpres",
            "title": "Candidate X wins presidency",
            "platform": "polymarket",
        }
        # Presidency implies nomination
        manual_rules = [
            {"sub": "0xpres", "sup": "0xnom", "confidence": 0.99}
        ]
        pairs = discover_subset_pairs(
            [m_cand_nom, m_cand_pres],
            manual_rules=manual_rules,
            platform="polymarket"
        )
        assert len(pairs) == 1
        assert pairs[0]["sub"]["condition_id"] == "0xpres"
        assert pairs[0]["sup"]["condition_id"] == "0xnom"
        assert pairs[0]["source"] == "manual"
        assert pairs[0]["confidence"] == 0.99

    def test_discover_subset_pairs_same_platform_only_filtering(self):
        m_poly = {
            "condition_id": "0xpoly",
            "title": "Event A",
            "platform": "polymarket",
        }
        m_kalshi = {
            "condition_id": "0xkalshi",
            "title": "Event B",
            "platform": "kalshi",
        }
        manual_rules = [
            {"sub": "0xpoly", "sup": "0xkalshi", "confidence": 0.95}
        ]
        # When same_platform_only=True, cross-platform rule is skipped
        pairs = discover_subset_pairs(
            [m_poly, m_kalshi],
            manual_rules=manual_rules,
            same_platform_only=True,
            platform="polymarket",
        )
        assert len(pairs) == 0

        # When same_platform_only=False, allowed
        pairs_allowed = discover_subset_pairs(
            [m_poly, m_kalshi],
            manual_rules=manual_rules,
            same_platform_only=False,
            platform="polymarket",
        )
        assert len(pairs_allowed) == 1


# ---------------------------------------------------------------------------
# Regression: live Polymarket ladders mis-paired on 2026-09-24
# ---------------------------------------------------------------------------

def _pm(question, yes_price, event_id, end="2028-01-01"):
    """Trimmed live Polymarket gamma market (only the fields pairing reads)."""
    return {
        "id": question,
        "question": question,
        "endDateIso": end,
        "endDate": f"{end}T04:59:00Z",
        "outcomePrices": f'["{yes_price}", "{round(1 - yes_price, 4)}"]',
        "events": [{"id": event_id}],
    }


class TestStrikeUnitsAndLadderGrouping:
    def test_units_directly_after_the_number(self):
        assert _normalize_num("1", "OpenAI IPO closing market cap above $1T?") == 1e12
        assert _normalize_num("1.2", "above $1.2T?") == 1.2e12
        assert _normalize_num("800", "above $800B?") == 8e11
        assert _normalize_num("2.5", "above $2.5 million") == 2.5e6
        assert _normalize_num("1", "above $1 trillion") == 1e12
        assert _normalize_num("100", "Will BTC be above $100 by March?") == 100.0  # "by" is not billion
        assert _normalize_num("1", "above 11t or $1 in 2021") == 1.0  # "11t" is not "1t"

    def test_openai_ladder_parses_real_magnitudes(self):
        strikes = {
            q: parse_threshold_market(_pm(q, 0.5, "193337"), platform="polymarket").strike
            for q in ("OpenAI IPO closing market cap above $800B?",
                      "OpenAI IPO closing market cap above $1T?",
                      "OpenAI IPO closing market cap above $1.2T?")
        }
        assert strikes == {
            "OpenAI IPO closing market cap above $800B?": 8e11,
            "OpenAI IPO closing market cap above $1T?": 1e12,
            "OpenAI IPO closing market cap above $1.2T?": 1.2e12,
        }

    def test_openai_ladder_pairs_higher_strike_as_subset(self):
        ladder = [
            _pm("OpenAI IPO closing market cap above $800B?", 0.8605, "193337"),
            _pm("OpenAI IPO closing market cap above $1T?", 0.715, "193337"),
            _pm("OpenAI IPO closing market cap above $1.2T?", 0.70, "193337"),
        ]
        pairs = discover_subset_pairs(ladder, platform="polymarket")
        assert len(pairs) == 3
        for p in pairs:
            assert p["_sub_strike"] > p["_sup_strike"]
        subsets = {(p["sub"]["question"], p["sup"]["question"]) for p in pairs}
        assert ("OpenAI IPO closing market cap above $800B?",
                "OpenAI IPO closing market cap above $1T?") not in subsets

    def test_coherent_openai_ladder_yields_no_frechet_candidate(self):
        import importlib
        frechet = sys.modules.get("scans.frechet") or importlib.import_module("scans.frechet")
        ladder = [
            _pm("OpenAI IPO closing market cap above $800B?", 0.8605, "193337"),
            _pm("OpenAI IPO closing market cap above $1T?", 0.715, "193337"),
            _pm("OpenAI IPO closing market cap above $1.2T?", 0.70, "193337"),
        ]
        assert frechet.scan_frechet(ladder, min_profit=0.0, min_violation=0.02, platform="polymarket") == []

    def test_incoherent_metamask_pair_is_still_detected(self):
        import importlib
        frechet = sys.modules.get("scans.frechet") or importlib.import_module("scans.frechet")
        pair = [
            _pm("Metamask FDV above $2B one day after launch?", 0.0315, "73236", end="2027-01-01"),
            _pm("Metamask FDV above $3B one day after launch?", 0.0545, "73236", end="2027-01-01"),
        ]
        cands = frechet.scan_frechet(pair, min_profit=0.0, min_violation=0.02, platform="polymarket")
        assert len(cands) == 1
        assert cands[0]["_sub_market"]["question"].startswith("Metamask FDV above $3B")
        assert cands[0]["_sup_market"]["question"].startswith("Metamask FDV above $2B")

    def test_different_expiry_or_event_never_pair(self):
        same_event_diff_date = [
            _pm("Will BTC be above $100k?", 0.6, "e1", end="2026-12-31"),
            _pm("Will BTC be above $90k?", 0.5, "e1", end="2027-06-30"),
        ]
        diff_event_same_date = [
            _pm("Will BTC be above $100k?", 0.6, "e1"),
            _pm("Will BTC be above $90k?", 0.5, "e2"),
        ]
        assert discover_subset_pairs(same_event_diff_date, platform="polymarket") == []
        assert discover_subset_pairs(diff_event_same_date, platform="polymarket") == []
