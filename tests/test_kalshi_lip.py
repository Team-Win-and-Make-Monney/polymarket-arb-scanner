"""Unit tests for Kalshi LIP snapshot scoring (kalshi_lip.py)."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from kalshi_lip import (  # noqa: E402
    KalshiLipScorer,
    LIPScoreTracker,
    MAX_TARGET_SIZE,
    MIN_TARGET_SIZE,
    distance_multiplier,
    extract_book_levels,
    qualifying_share,
    reference_price,
    snapshot_score,
    tick_distance,
    ticks_worse,
)


class TestTickDistance:
    def test_same_price_is_zero(self):
        assert tick_distance(0.50, 0.50) == 0

    def test_one_cent_is_one_tick(self):
        assert tick_distance(0.49, 0.50) == 1

    def test_five_cents_is_five_ticks(self):
        assert tick_distance(0.45, 0.50) == 5

    def test_absolute_value(self):
        assert tick_distance(0.55, 0.50) == tick_distance(0.45, 0.50)

    def test_rejects_nonpositive_tick(self):
        with pytest.raises(ValueError):
            tick_distance(0.5, 0.5, tick=0.0)


class TestDistanceMultiplier:
    def test_at_best_price_full_credit(self):
        assert distance_multiplier(0.50, 0.50, discount_factor=0.5) == 1.0

    def test_discount_factor_one_never_penalises(self):
        assert distance_multiplier(0.40, 0.50, discount_factor=1.0) == 1.0

    def test_decays_per_tick(self):
        # 2 ticks away at df=0.5 -> 0.5**2 = 0.25
        assert distance_multiplier(0.48, 0.50, discount_factor=0.5) == pytest.approx(0.25)

    def test_discount_factor_zero_only_best_counts(self):
        assert distance_multiplier(0.50, 0.50, discount_factor=0.0) == 1.0
        assert distance_multiplier(0.49, 0.50, discount_factor=0.0) == 0.0

    def test_rejects_negative_discount_factor(self):
        with pytest.raises(ValueError):
            distance_multiplier(0.49, 0.50, discount_factor=-0.1)

    def test_rejects_discount_factor_above_one(self):
        with pytest.raises(ValueError):
            distance_multiplier(0.49, 0.50, discount_factor=1.2)


class TestSnapshotScore:
    def test_single_bid_at_best(self):
        orders = [{'side': 'bid', 'price': 0.50, 'size': 100}]
        score = snapshot_score(orders, best_bid=0.50, best_ask=None,
                               target_size=200, discount_factor=0.5)
        assert score == pytest.approx(100.0)

    def test_target_size_caps_qualifying_depth(self):
        # 500 contracts resting but target is only 200 -> only 200 score at best
        orders = [{'side': 'bid', 'price': 0.50, 'size': 500}]
        score = snapshot_score(orders, best_bid=0.50, best_ask=None,
                               target_size=200, discount_factor=1.0)
        assert score == pytest.approx(200.0)

    def test_closest_orders_fill_target_first(self):
        # 150 at best (mult 1.0) + 100 two ticks out (mult 0.25) with target 200.
        # First 150 at best, remaining 50 from the further order at 0.25.
        orders = [
            {'side': 'bid', 'price': 0.50, 'size': 150},
            {'side': 'bid', 'price': 0.48, 'size': 100},
        ]
        score = snapshot_score(orders, best_bid=0.50, best_ask=None,
                               target_size=200, discount_factor=0.5)
        assert score == pytest.approx(150.0 + 50 * 0.25)

    def test_both_sides_scored(self):
        orders = [
            {'side': 'bid', 'price': 0.40, 'size': 100},
            {'side': 'ask', 'price': 0.60, 'size': 100},
        ]
        score = snapshot_score(orders, best_bid=0.40, best_ask=0.60,
                               target_size=200, discount_factor=1.0)
        assert score == pytest.approx(200.0)

    def test_missing_reference_side_skipped(self):
        orders = [{'side': 'ask', 'price': 0.60, 'size': 100}]
        score = snapshot_score(orders, best_bid=0.40, best_ask=None,
                               target_size=200, discount_factor=1.0)
        assert score == 0.0


class TestKalshiLipScorer:
    def test_target_size_clamped_to_bounds(self):
        low = KalshiLipScorer('MKT', target_size=10, discount_factor=0.5)
        assert low.target_size == MIN_TARGET_SIZE
        high = KalshiLipScorer('MKT', target_size=99999, discount_factor=0.5)
        assert high.target_size == MAX_TARGET_SIZE

    def test_rejects_out_of_range_discount_factor(self):
        with pytest.raises(ValueError):
            KalshiLipScorer('MKT', target_size=200, discount_factor=1.5)

    def test_accumulates_snapshots(self):
        scorer = KalshiLipScorer('MKT', target_size=200, discount_factor=1.0)
        orders = [{'side': 'bid', 'price': 0.50, 'size': 100}]
        scorer.record_snapshot(orders, best_bid=0.50, best_ask=None)
        scorer.record_snapshot(orders, best_bid=0.50, best_ask=None)
        assert scorer.snapshot_count == 2
        assert scorer.accumulated_score == pytest.approx(200.0)

    def test_estimate_reward_pool_share(self):
        scorer = KalshiLipScorer('MKT', target_size=200, discount_factor=1.0)
        orders = [{'side': 'bid', 'price': 0.50, 'size': 100}]
        scorer.record_snapshot(orders, best_bid=0.50, best_ask=None)
        # our score 100, total market score 500 -> 20% of $100 pool = $20
        assert scorer.estimate_reward(reward_pool=100.0, total_market_score=500.0) == pytest.approx(20.0)

    def test_estimate_reward_zero_when_no_total(self):
        scorer = KalshiLipScorer('MKT', target_size=200, discount_factor=1.0)
        assert scorer.estimate_reward(reward_pool=100.0, total_market_score=0.0) == 0.0

    def test_estimate_reward_with_share(self):
        scorer = KalshiLipScorer('MKT', target_size=200, discount_factor=1.0)
        orders = [{'side': 'bid', 'price': 0.50, 'size': 100}]
        scorer.record_snapshot(orders, best_bid=0.50, best_ask=None)
        assert scorer.estimate_reward_with_share(reward_pool=100.0, participation_share=0.2) == pytest.approx(20.0)

    def test_estimate_reward_with_share_zero_without_liquidity(self):
        scorer = KalshiLipScorer('MKT', target_size=200, discount_factor=1.0)
        assert scorer.estimate_reward_with_share(reward_pool=100.0, participation_share=0.5) == 0.0


class TestPublishedReferencePrice:
    def test_walks_until_one_fifth_target(self):
        levels = [(0.50, 20.0), (0.49, 30.0)]
        # need = 200/5 = 40; 20 at 0.50 then 30 at 0.49 fills at 0.49
        assert reference_price(levels, 200) == pytest.approx(0.49)

    def test_none_when_book_too_thin(self):
        assert reference_price([(0.50, 10.0)], 200) is None

    def test_ticks_worse_floors_at_zero(self):
        assert ticks_worse(0.51, 0.50) == 0
        assert ticks_worse(0.48, 0.50) == 2

    def test_qualifying_share_at_reference(self):
        levels = [(0.50, 200.0)]
        share, reason = qualifying_share(levels, 200, 0.5, quote_price=0.50, quote_size=200)
        assert reason is None
        assert share == pytest.approx(0.5)

    def test_below_target_is_not_zero(self):
        # Depth enough for a reference (target/5) but below Target Size.
        share, reason = qualifying_share([(0.50, 50.0)], 200, 0.5, 0.50, 200)
        assert share is None
        assert reason == "below_target"

    def test_malformed_level_does_not_raise(self):
        levels = [(0.50, 1000.0), ("bad", 10.0), (0.49, 200.0)]
        ref = reference_price(levels, 200)
        assert ref == 0.50
        share, reason = qualifying_share(levels, 200, 0.5, 0.50, 200)
        assert reason is None
        assert share is not None


class TestExtractBookLevels:
    def test_extract_from_orderbook_fp(self):
        raw = {
            "orderbook_fp": {
                "yes_dollars": [["0.45", "100"], ["0.44", "200"]],
                "no_dollars": [["0.45", "150"]],  # NO bid at 0.45 is YES ask at 0.55
            }
        }
        bids, asks = extract_book_levels(raw)
        assert bids == [(0.45, 100.0), (0.44, 200.0)]
        assert asks == [(0.55, 150.0)]

    def test_extract_from_legacy_orderbook(self):
        raw = {
            "orderbook": {
                "yes": [[45, 100], [44, 200]],
                "no": [[45, 150]],
            }
        }
        bids, asks = extract_book_levels(raw)
        assert bids == [(0.45, 100.0), (0.44, 200.0)]
        assert asks == [(0.55, 150.0)]

    def test_extract_from_top_of_book(self):
        book = {
            "yes_bid": (0.45, 100.0),
            "yes_ask": (0.55, 150.0),
        }
        bids, asks = extract_book_levels(book)
        assert bids == [(0.45, 100.0)]
        assert asks == [(0.55, 150.0)]

    def test_extract_empty_or_none(self):
        assert extract_book_levels(None) == ([], [])
        assert extract_book_levels({}) == ([], [])
        assert extract_book_levels("bad_input") == ([], [])


class TestLIPScoreTracker:
    def test_set_market_program_defaults_and_bounds(self):
        tracker = LIPScoreTracker()
        tracker.set_market_program("TEST-TICKER", pool_dollars=-50, discount_factor=1.5, target_size=50)
        prog = tracker._programs["TEST-TICKER"]
        assert prog["pool_dollars"] == 0.0
        assert prog["discount_factor"] == 1.0
        assert prog["target_size"] == MIN_TARGET_SIZE

    def test_record_snapshot_single_order(self):
        tracker = LIPScoreTracker()
        tracker.set_market_program("TEST-TICKER", pool_dollars=700.0, discount_factor=0.95, target_size=500.0)

        book = {
            "yes_bid": (0.48, 100.0),
            "yes_ask": (0.52, 100.0),
            "raw": {
                "orderbook_fp": {
                    "yes_dollars": [["0.48", "100"]],
                    "no_dollars": [["0.48", "100"]],  # ask at 0.52
                }
            },
        }
        # Place our resting quote at touch
        our_orders = [
            {"purpose": "quote_bid", "side": "yes", "action": "buy", "price": 0.48, "count": 100},
            {"purpose": "quote_ask", "side": "no", "action": "buy", "price": 0.48, "count": 100},
        ]
        result = tracker.record_snapshot("TEST-TICKER", our_orders, book, now=1000.0, is_dry_run=True)
        assert result["ticker"] == "TEST-TICKER"
        assert result["our_score"] == pytest.approx(200.0)
        # Total market score = competitors (200) + ours (200) = 400
        assert result["total_market_score"] == pytest.approx(400.0)
        assert result["qualifying_share"] == pytest.approx(0.50)
        assert result["reward_delta"] > 0

    def test_record_snapshot_solo_quoter_gets_100_percent_share(self):
        tracker = LIPScoreTracker()
        tracker.set_market_program("TEST-TICKER", pool_dollars=1000.0)
        book = {"yes_bid": None, "yes_ask": None, "raw": {}}
        our_orders = [
            {"purpose": "quote_bid", "side": "yes", "action": "buy", "price": 0.50, "count": 100},
        ]
        result = tracker.record_snapshot("TEST-TICKER", our_orders, book, now=1000.0, is_dry_run=True)
        assert result["qualifying_share"] == 1.0

    def test_record_snapshot_no_orders_gets_zero_share(self):
        tracker = LIPScoreTracker()
        tracker.set_market_program("TEST-TICKER", pool_dollars=1000.0)
        book = {
            "yes_bid": (0.45, 100.0),
            "yes_ask": (0.55, 100.0),
        }
        result = tracker.record_snapshot("TEST-TICKER", [], book, now=1000.0)
        assert result["our_score"] == 0.0
        assert result["qualifying_share"] == 0.0
        assert result["reward_delta"] == 0.0

    def test_accrual_across_time_and_metrics(self):
        t0 = 1000.0
        tracker = LIPScoreTracker(time_fn=lambda: t0)
        # Weekly pool = $604.80 -> $0.001 / second
        tracker.set_market_program("TEST-TICKER", pool_dollars=604.80, discount_factor=1.0, target_size=1000.0)

        book = {"yes_bid": None, "yes_ask": None, "raw": {}}
        our_orders = [{"purpose": "quote_bid", "side": "yes", "action": "buy", "price": 0.50, "count": 100}]

        # Snapshot 1 at t0 (initial snapshot elapsed = 1.0s)
        r1 = tracker.record_snapshot("TEST-TICKER", our_orders, book, now=t0)
        assert r1["reward_delta"] == pytest.approx(0.001, rel=1e-3)

        # Snapshot 2 at t0 + 10s (elapsed = 10s)
        r2 = tracker.record_snapshot("TEST-TICKER", our_orders, book, now=t0 + 10.0)
        assert r2["reward_delta"] == pytest.approx(0.010, rel=1e-3)
        assert r2["accumulated_reward_usd"] == pytest.approx(0.011, rel=1e-3)

        metrics = tracker.get_metrics(
            capital_deployed_by_ticker={"TEST-TICKER": 100.0},
            realized_pnl_by_ticker={"TEST-TICKER": 5.0},
        )
        assert metrics["total_estimated_reward_usd"] == pytest.approx(0.011, rel=1e-3)
        assert "TEST-TICKER" in metrics["by_ticker"]
        t_meta = metrics["by_ticker"]["TEST-TICKER"]
        assert t_meta["pool_dollars"] == 604.80
        assert t_meta["qualifying_share_pct"] == 100.0
        assert t_meta["daily_rate_usd"] == pytest.approx(86.40, rel=1e-2)
        assert t_meta["weekly_rate_usd"] == pytest.approx(604.80, rel=1e-2)
        assert t_meta["lip_apr_pct"] > 0
        assert metrics["blended_apr_pct"] > 0

    def test_to_and_from_dict_roundtrip(self):
        tracker = LIPScoreTracker()
        tracker.set_market_program("TICKER-1", pool_dollars=500.0, discount_factor=0.92, target_size=600.0)
        tracker.record_snapshot("TICKER-1", [{"purpose": "quote_bid", "side": "yes", "action": "buy", "price": 0.50, "count": 50}], None, now=100.0)

        state = tracker.to_dict()
        assert "programs" in state
        assert "stats" in state
        assert "TICKER-1" in state["programs"]

        new_tracker = LIPScoreTracker()
        new_tracker.from_dict(state)
        assert "TICKER-1" in new_tracker._programs
        assert new_tracker._programs["TICKER-1"]["pool_dollars"] == 500.0
        assert new_tracker._stats["TICKER-1"]["snapshots_count"] == 1
