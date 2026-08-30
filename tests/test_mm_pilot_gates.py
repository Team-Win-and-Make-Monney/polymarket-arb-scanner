"""Plan 10 tests — inventory caps in the order path, hot-path gates, invariants.

Spec test-plan cases 6-12, 16, 17 (docs/plans/10-mm-pilot-prep.md section 10).
Fail-before: on origin/master InventoryTracker.can_trade is never consulted at
placement time, toxicity is permanently 0.0 (record_fill unwired), and none of
the MM_ pilot config keys exist — every test here fails on master.
"""

import re
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))

import importlib
from unittest.mock import patch

import pytest

import config
import market_maker
from market_maker import ToxicFlowDetector, VolatilityTracker
from mm_pilot import FillEvent, KalshiMMPilot

from test_mm_pilot import (TICKER, FakeKalshiClient, RecordingHedger,
                           build_pilot, live_config, make_book)


@pytest.fixture
def clock():
    return [1_000_000.0]


@pytest.fixture
def pilot_env(monkeypatch):
    """Enable pilot preconditions only for tests that request this fixture."""
    cfg = live_config()
    monkeypatch.setattr(cfg, "MM_KALSHI_PILOT_ENABLED", True)
    monkeypatch.setattr(cfg, "MM_TOXIC_FLOW_ENABLED", True)
    monkeypatch.setattr(cfg, "MM_VOLATILITY_ADJUSTED_ENABLED", True)
    monkeypatch.setattr(cfg, "MM_AUTO_HEDGE_ENABLED", True)
    yield cfg


def live_market_maker():
    """Resolve the LIVE market_maker module (see live_config)."""
    return importlib.import_module("market_maker")


# ---------------------------------------------------------------------------
# 6. Per-market USD cap enforced in the order path; reducing orders exempt
# ---------------------------------------------------------------------------

class TestPerMarketCap:
    def test_accumulating_order_past_usd_cap_rejected(self, pilot_env, clock):
        pilot = build_pilot(clock, client=FakeKalshiClient())
        # Drive net inventory to $99 (198 contracts @ $0.50)
        pilot.inventory.apply_fill(TICKER, "yes", "buy", 198, 0.50)
        assert pilot.inventory.net_usd(TICKER) == pytest.approx(99.0)
        # Next accumulating $10 quote -> rejected with the cap reason
        result = pilot.authorize_order(TICKER, "yes", "buy", 20, 0.50)
        assert result.allowed is False
        assert result.reason == "per_market_inventory_cap"

    def test_reducing_direction_order_still_allowed_at_cap(self, pilot_env,
                                                           clock):
        pilot = build_pilot(clock, client=FakeKalshiClient())
        pilot.inventory.apply_fill(TICKER, "yes", "buy", 198, 0.50)
        # Buying NO reduces |net| on fill: caps must never block the exit
        result = pilot.authorize_order(TICKER, "no", "buy", 20, 0.50)
        assert result.allowed is True
        # And an explicit reducing (hedge) order likewise
        hedge = pilot.authorize_order(TICKER, "yes", "sell", 20, 0.50,
                                      reducing=True)
        assert hedge.allowed is True
        assert hedge.reason == "ok_reducing"


# ---------------------------------------------------------------------------
# 7. Contract-unit cap binds independently at low prices
# ---------------------------------------------------------------------------

class TestContractCap:
    def test_contract_cap_rejects_before_usd_cap_at_low_price(self, pilot_env,
                                                              clock):
        pilot = build_pilot(clock, client=FakeKalshiClient())
        # 240 contracts @ $0.10 = only $24 of the $100 USD cap
        pilot.inventory.apply_fill(TICKER, "yes", "buy", 240, 0.10)
        result = pilot.authorize_order(TICKER, "yes", "buy", 20, 0.10)
        assert result.allowed is False
        assert result.reason == "per_market_contract_cap"


# ---------------------------------------------------------------------------
# 8. Total cap one-sides ALL markets (bids stopped, asks kept)
# ---------------------------------------------------------------------------

class TestTotalCap:
    TICKERS = ("KXAAA-1", "KXBBB-2", "KXCCC-3")

    def test_total_cap_stops_accumulating_side_everywhere(self, pilot_env,
                                                          clock):
        books = {t: make_book() for t in self.TICKERS}
        client = FakeKalshiClient(books=books)
        pilot = build_pilot(clock, client=client, selection=self.TICKERS)
        pilot.canary_graduated = True
        # ~$84 net long in each of 3 markets -> total $252 >= $250 cap
        for t in self.TICKERS:
            pilot.inventory.apply_fill(t, "yes", "buy", 168, 0.50)
        placed = pilot.refresh_all()
        assert placed, "reducing-side quotes must still be placed"
        purposes = {pilot._orders[oid]["purpose"] for oid in placed}
        assert "quote_bid" not in purposes   # accumulating side stopped
        assert purposes == {"quote_ask"}     # only inventory-reducing quotes
        # ...in every market
        quoted_markets = {pilot._orders[oid]["ticker"] for oid in placed}
        assert quoted_markets == set(self.TICKERS)

    def test_authorize_rejects_accumulating_order_at_total_cap(self, pilot_env,
                                                               clock):
        pilot = build_pilot(clock, client=FakeKalshiClient(),
                            selection=self.TICKERS)
        for t in self.TICKERS:
            pilot.inventory.apply_fill(t, "yes", "buy", 168, 0.50)
        result = pilot.authorize_order("KXAAA-1", "yes", "buy", 10, 0.50)
        assert result.allowed is False
        assert result.reason == "total_inventory_cap"


# ---------------------------------------------------------------------------
# 9. Gross cap: inventory at cost + resting notional <= $300
# ---------------------------------------------------------------------------

class TestGrossCap:
    def test_gross_over_300_rejected(self, pilot_env, clock, monkeypatch):
        monkeypatch.setattr(live_config(), "MM_MAX_TOTAL_INVENTORY_USD", 1_000.0)
        pilot = build_pilot(clock, client=FakeKalshiClient())
        # $60 net inventory + $230 resting notional
        pilot.inventory.apply_fill(TICKER, "yes", "buy", 120, 0.50)
        pilot._orders["resting_1"] = {
            "ticker": TICKER, "side": "yes", "action": "buy",
            "count": 460, "price": 0.50, "purpose": "quote_bid",
            "placed_at": clock[0],
        }
        # New $20 quote: 60 + 230 + 20 = $310 > $300 gross ceiling
        result = pilot.authorize_order(TICKER, "yes", "buy", 40, 0.50)
        assert result.allowed is False
        assert result.reason == "gross_cap"

    def test_gross_under_ceiling_allowed(self, pilot_env, clock, monkeypatch):
        monkeypatch.setattr(live_config(), "MM_MAX_TOTAL_INVENTORY_USD", 1_000.0)
        pilot = build_pilot(clock, client=FakeKalshiClient())
        pilot.inventory.apply_fill(TICKER, "yes", "buy", 120, 0.50)
        pilot._orders["resting_1"] = {
            "ticker": TICKER, "side": "yes", "action": "buy",
            "count": 400, "price": 0.50, "purpose": "quote_bid",
            "placed_at": clock[0],
        }
        # 60 + 200 + 20 = $280 <= $300
        result = pilot.authorize_order(TICKER, "yes", "buy", 40, 0.50)
        assert result.allowed is True


# ---------------------------------------------------------------------------
# 10. No bypass: place_order referenced exactly once in mm_pilot.py
# ---------------------------------------------------------------------------

class TestNoBypass:
    def test_place_order_referenced_exactly_once_in_pilot_source(self):
        src_path = os.path.join(os.path.dirname(__file__), "..", "mm_pilot.py")
        with open(src_path, encoding="utf-8") as fh:
            source = fh.read()
        refs = re.findall(r"_client\.place_order\(", source)
        assert len(refs) == 1, (
            "mm_pilot.py must reach kalshi_client.place_order in exactly one "
            f"place (behind authorize_order); found {len(refs)}"
        )
        # And that single reference sits inside place_pilot_order, which
        # authorizes first.
        idx = source.index("_client.place_order(")
        fn_start = source.rindex("def place_pilot_order", 0, idx)
        assert "authorize_order" in source[fn_start:idx]

    def test_rejected_order_never_reaches_the_client(self, pilot_env, clock):
        client = FakeKalshiClient()
        pilot = build_pilot(clock, client=client)
        pilot.inventory.apply_fill(TICKER, "yes", "buy", 198, 0.50)
        oid = pilot.place_pilot_order(TICKER, "yes", "buy", 40, 0.50,
                                      purpose="quote_bid")
        assert oid is None
        assert client.place_order_calls == 0


# ---------------------------------------------------------------------------
# 11. Toxicity pause pulls quotes (fail-before: ratio stayed 0.0 on master)
# ---------------------------------------------------------------------------

class TestToxicityGate:
    @staticmethod
    def feed_fills(detector, adverse: int, total: int = 20, ticker=TICKER):
        for i in range(total):
            mid = 0.45 if i < adverse else 0.55  # bid fill, mid below = adverse
            detector.record_fill(ticker, "bid", 0.50, 2.0, mid)

    def test_toxic_ratio_over_threshold_pulls_quotes(self, pilot_env, clock):
        detector = ToxicFlowDetector()
        client = FakeKalshiClient()
        pilot = build_pilot(clock, client=client, detector=detector)
        oid = pilot.place_pilot_order(TICKER, "yes", "buy", 4, 0.49,
                                      purpose="quote_bid")
        self.feed_fills(detector, adverse=13)  # 0.65 >= 0.60
        assert pilot.refresh_market(TICKER) == []
        assert oid in client.cancelled
        assert pilot.resting_orders(TICKER) == []
        # Observability scan surfaces the pause for the pilot tickers
        detector.trigger_pause(TICKER)
        from scans.toxic_flow_pause import scan_toxic_flow_pause
        opps = scan_toxic_flow_pause([TICKER], detector=detector)
        assert len(opps) == 1
        assert opps[0]["type"] == "ToxicFlowPause"

    def test_ratio_below_threshold_quotes_placed(self, pilot_env, clock):
        detector = ToxicFlowDetector()
        client = FakeKalshiClient()
        pilot = build_pilot(clock, client=client, detector=detector)
        self.feed_fills(detector, adverse=11)  # 0.55 < 0.60
        placed = pilot.refresh_market(TICKER)
        assert placed != []

    def test_pilot_fills_feed_the_detector_and_arm_the_pause(self, pilot_env,
                                                             clock,
                                                             monkeypatch):
        monkeypatch.setattr(live_config(), "MM_CANARY_QUOTE_SIZE_USD", 100.0)
        detector = ToxicFlowDetector()
        client = FakeKalshiClient()
        pilot = build_pilot(clock, client=client, detector=detector)
        # Seed 19 adverse fills; the 20th comes from the pilot's own poll.
        self.feed_fills(detector, adverse=19, total=19)
        oid = pilot.place_pilot_order(TICKER, "yes", "buy", 4, 0.49,
                                      purpose="quote_bid")
        from test_mm_pilot import kfill
        client.fills_script = [kfill(oid, count=4, created=clock[0])]
        pilot.poll_fills()
        assert detector.get_toxicity(TICKER) >= 0.60
        assert detector.get_pause_remaining(TICKER) > 0  # trigger_pause armed


# ---------------------------------------------------------------------------
# 12. Volatility: widen first (G9), pull at the ceiling (G8)
# ---------------------------------------------------------------------------

class FakeVolTracker:
    def __init__(self, multiplier, has_samples=True):
        self.multiplier = multiplier
        self.has_samples = has_samples

    def get_spread_multiplier(self, market_key, base_multiplier=1.0,
                              max_multiplier=3.0):
        return self.multiplier

    def has_min_samples(self, market_key):
        return self.has_samples

    def record_price(self, market_key, price):
        pass


class TestVolatilityGate:
    def _pilot_with_multiplier(self, clock, multiplier):
        fake = FakeVolTracker(multiplier)
        # QuoteEngine reads the module singleton for widening; G8 reads the
        # injected tracker. Point both at the fake (on the LIVE module).
        live_market_maker()._volatility_tracker = fake
        client = FakeKalshiClient()
        pilot = build_pilot(clock, client=client, vol=fake)
        return pilot, client

    def teardown_method(self, method):
        live_market_maker()._volatility_tracker = None

    def test_multiplier_two_widens_spread(self, pilot_env, clock):
        pilot, _client = self._pilot_with_multiplier(clock, 2.0)
        placed = pilot.refresh_market(TICKER)
        assert placed
        orders = {pilot._orders[oid]["purpose"]: pilot._orders[oid]
                  for oid in placed}
        bid = orders["quote_bid"]["price"]
        ask_yes_terms = 1.0 - orders["quote_ask"]["price"]
        base_spread = config.MM_MIN_SPREAD
        assert ask_yes_terms - bid > base_spread  # wider than base

    def test_multiplier_at_ceiling_pulls_quotes(self, pilot_env, clock):
        pilot, client = self._pilot_with_multiplier(clock, 2.6)  # >= 2.5
        oid = pilot.place_pilot_order(TICKER, "yes", "buy", 4, 0.49,
                                      purpose="quote_bid")
        assert pilot.refresh_market(TICKER) == []
        assert oid in client.cancelled

    # -----------------------------------------------------------------
    # Finding #9: insufficient samples must fail G8, not read as "calm".
    # get_volatility() returns 0.0 (the calmest possible multiplier) both
    # when a market is truly calm and when there just isn't enough data
    # yet — G8 must distinguish "known calm" from "unknown" and fail
    # closed on the latter.
    # Fail-before: G8 only checked the multiplier value, so a fresh
    # market with zero price samples (multiplier reads 1.0 = calmest)
    # would quote at base spread during warm-up even if the underlying
    # market were moving violently.
    # -----------------------------------------------------------------

    def test_insufficient_samples_pulls_quotes_even_at_calmest_reading(
            self, pilot_env, clock):
        # multiplier=1.0 (the calmest possible reading) but has_samples=False
        # — this is exactly what a real VolatilityTracker reports during
        # warm-up (get_volatility returns 0.0 for lack of data, which
        # get_spread_multiplier maps to base_multiplier=1.0).
        fake = FakeVolTracker(1.0, has_samples=False)
        live_market_maker()._volatility_tracker = fake
        client = FakeKalshiClient()
        pilot = build_pilot(clock, client=client, vol=fake)
        oid = pilot.place_pilot_order(TICKER, "yes", "buy", 4, 0.49,
                                      purpose="quote_bid")
        assert pilot.refresh_market(TICKER) == []
        assert oid in client.cancelled
        reasons = [d["reason"] for d in pilot._decisions
                  if d["gate"] == "G8_volatility_ceiling"]
        assert "insufficient_samples" in reasons

    def test_sufficient_samples_at_calm_reading_still_quotes(self, pilot_env,
                                                              clock):
        # Sanity check the fix isn't overly strict: calm + enough samples
        # must still quote normally.
        pilot, _client = self._pilot_with_multiplier(clock, 1.0)
        assert pilot.refresh_market(TICKER)

    def test_real_tracker_warm_up_blocks_quoting_by_default(self, pilot_env,
                                                             clock):
        """End-to-end with the REAL VolatilityTracker (production default
        min_samples=5, not the test fixture's relaxed min_samples=1) —
        a single book update is not enough samples to quote."""
        client = FakeKalshiClient()
        real_tracker = VolatilityTracker()  # production default min_samples
        pilot = build_pilot(clock, client=client, vol=real_tracker)
        assert pilot.refresh_market(TICKER) == []


# ---------------------------------------------------------------------------
# 16. Config invariants: pilot refuses live start without its preconditions
# ---------------------------------------------------------------------------

class TestConfigInvariants:
    @pytest.mark.parametrize("missing_flag", [
        "MM_AUTO_HEDGE_ENABLED",
        "MM_TOXIC_FLOW_ENABLED",
        "MM_VOLATILITY_ADJUSTED_ENABLED",
    ])
    def test_live_pilot_without_precondition_raises(self, monkeypatch, tmp_path,
                                                    missing_flag):
        from live_envelope_fixtures import write_test_envelope
        monkeypatch.setenv("LIVE_ENVELOPE_PATH", str(write_test_envelope(tmp_path)))
        cfg = live_config()
        monkeypatch.setattr(cfg, "MM_KALSHI_PILOT_ENABLED", True)
        monkeypatch.setattr(cfg, "DRY_RUN", False)
        for flag in ("MM_AUTO_HEDGE_ENABLED", "MM_TOXIC_FLOW_ENABLED",
                     "MM_VOLATILITY_ADJUSTED_ENABLED"):
            monkeypatch.setattr(cfg, flag, flag != missing_flag)
        with pytest.raises(cfg.ConfigError, match=missing_flag):
            cfg.validate_config()

    def test_live_pilot_with_all_preconditions_validates(self, monkeypatch, tmp_path):
        from live_envelope_fixtures import write_test_envelope
        monkeypatch.setenv("LIVE_ENVELOPE_PATH", str(write_test_envelope(tmp_path)))
        cfg = live_config()
        monkeypatch.setattr(cfg, "MM_KALSHI_PILOT_ENABLED", True)
        monkeypatch.setattr(cfg, "DRY_RUN", False)
        for flag in ("MM_AUTO_HEDGE_ENABLED", "MM_TOXIC_FLOW_ENABLED",
                     "MM_VOLATILITY_ADJUSTED_ENABLED"):
            monkeypatch.setattr(cfg, flag, True)
        cfg.validate_config()  # must not raise

    def test_dry_run_pilot_does_not_require_preconditions(self, monkeypatch):
        cfg = live_config()
        monkeypatch.setattr(cfg, "MM_KALSHI_PILOT_ENABLED", True)
        monkeypatch.setattr(cfg, "DRY_RUN", True)
        monkeypatch.setattr(cfg, "MM_AUTO_HEDGE_ENABLED", False)
        cfg.validate_config()  # D0 dry-run may start without them

    def test_pilot_without_kalshi_in_allowlist_raises(self, monkeypatch):
        cfg = live_config()
        monkeypatch.setattr(cfg, "MM_KALSHI_PILOT_ENABLED", True)
        monkeypatch.setattr(cfg, "DRY_RUN", True)
        monkeypatch.setattr(cfg, "ENABLED_EXECUTION_PLATFORMS",
                            frozenset({"polymarket"}))
        with pytest.raises(cfg.ConfigError, match="Kalshi-only"):
            cfg.validate_config()

    def test_cap_sanity_warnings(self, monkeypatch):
        cfg = live_config()
        monkeypatch.setattr(cfg, "MM_KALSHI_PILOT_ENABLED", True)
        monkeypatch.setattr(cfg, "DRY_RUN", True)
        monkeypatch.setattr(cfg, "MM_MAX_GROSS_PER_MARKET_USD", 500.0)
        monkeypatch.setattr(cfg, "MM_MAX_TOTAL_INVENTORY_USD", 400.0)
        warnings = cfg.validate_config()
        assert any("MM pilot cap sanity" in w for w in warnings)

    def test_pilot_flag_default_is_false(self):
        # Default must stay false — activation is operator-gated (D1/D2).
        # (CodeRabbit round-3: the previous `or` let this pass without ever
        # checking cfg.MM_KALSHI_PILOT_ENABLED when the env var was unset.)
        cfg = live_config()
        assert cfg.MM_KALSHI_PILOT_ENABLED is False


# ---------------------------------------------------------------------------
# 17. Crossing guard: never a marketable quote (post-only semantics)
# ---------------------------------------------------------------------------

class FixedQuoteEngine:
    def __init__(self, bid, ask):
        self._bid, self._ask = bid, ask

    def calculate_quotes(self, mid_price, inventory=0.0, max_inventory=0.0,
                         volatility=0.0, market_key=""):
        return {"bid": self._bid, "ask": self._ask,
                "spread": self._ask - self._bid, "skew": 0.0,
                "mid": mid_price}


class TestCrossingGuard:
    def test_crossing_bid_repriced_one_tick_inside(self, pilot_env, clock):
        # Best ask 0.52 (no_bid 0.48); engine wants bid 0.53 -> reprice 0.51
        client = FakeKalshiClient(
            books={TICKER: make_book(yes_bid=0.40, no_bid=0.48)})
        pilot = build_pilot(clock, client=client)
        pilot._quote_engine = FixedQuoteEngine(bid=0.53, ask=0.60)
        placed = pilot.refresh_market(TICKER)
        bids = [pilot._orders[oid] for oid in placed
                if pilot._orders[oid]["purpose"] == "quote_bid"]
        assert len(bids) == 1
        assert bids[0]["price"] == pytest.approx(0.51)
        assert bids[0]["price"] < 0.52  # never marketable

    def test_crossing_ask_repriced_or_skipped(self, pilot_env, clock):
        # Best yes bid 0.50; engine wants ask 0.45 -> reprice to 0.51
        client = FakeKalshiClient(
            books={TICKER: make_book(yes_bid=0.50, no_bid=0.40)})
        pilot = build_pilot(clock, client=client)
        pilot._quote_engine = FixedQuoteEngine(bid=0.30, ask=0.45)
        placed = pilot.refresh_market(TICKER)
        asks = [pilot._orders[oid] for oid in placed
                if pilot._orders[oid]["purpose"] == "quote_ask"]
        assert len(asks) == 1
        ask_yes_terms = 1.0 - asks[0]["price"]
        assert ask_yes_terms == pytest.approx(0.51)
        assert ask_yes_terms > 0.50  # never marketable


# ---------------------------------------------------------------------------
# Codex round-2 finding #2: TOCTOU pre-submit crossing re-check.
# G11/G12 (above) validate against the CACHED book at gate-evaluation
# time; place_pilot_order's _would_cross re-fetches the LIVE book
# immediately before the real kalshi_client.place_order() call and aborts
# if the price would now cross — closing the gap between "gates said this
# was safe" and "the exchange actually received it".
# Fail-before: place_pilot_order had no crossing check of its own at all;
# it trusted whatever price refresh_market's G12 had already vetted
# against a book that could have moved by submission time.
# ---------------------------------------------------------------------------

class TestToctouCrossingGuard:
    class MovingBookClient(FakeKalshiClient):
        """Returns ``first_book`` for the very first fetch_order_book call
        (refresh_market's own gate-evaluation refresh) and ``moved_book``
        for every call after that (place_pilot_order's pre-submit
        re-checks) — simulates the live market moving in the gap."""

        def __init__(self, first_book, moved_book):
            super().__init__(books={TICKER: first_book})
            self._moved_book = moved_book
            self.fetch_calls = 0

        def fetch_order_book(self, ticker):
            self.fetch_calls += 1
            if self.fetch_calls == 1:
                return self.books.get(ticker)
            return self._moved_book

    def test_aborts_bid_when_live_ask_drops_before_submission(
            self, pilot_env, clock, monkeypatch):
        monkeypatch.setattr(live_config(), "MM_CANARY_QUOTE_SIZE_USD", 10.0)
        # Gate-evaluation book: yes_ask=0.70 -> a bid at 0.49 is safely
        # non-crossing, G12 leaves it unrepriced.
        first_book = make_book(yes_bid=0.30, no_bid=0.30,
                               yes_qty=500.0, no_qty=500.0)
        # Live book by the time the bid's pre-submit check runs: the ask
        # dropped to 0.45 (no_bid rose to 0.55) — 0.49 is now marketable.
        moved_book = make_book(yes_bid=0.30, no_bid=0.55,
                               yes_qty=500.0, no_qty=500.0)
        client = self.MovingBookClient(first_book, moved_book)
        pilot = build_pilot(clock, client=client)
        pilot._quote_engine = FixedQuoteEngine(bid=0.49, ask=0.60)
        placed = pilot.refresh_market(TICKER)
        purposes = {pilot._orders[oid]["purpose"] for oid in placed}
        assert "quote_bid" not in purposes  # aborted by the TOCTOU guard
        # The ask's own pre-submit check reads a still-non-crossing price
        # off the same moved book (no_price=0.40 vs. no_ask=0.70) — the
        # guard is per-order, not an all-or-nothing pull of the market.
        assert "quote_ask" in purposes
        reasons = [d["reason"] for d in pilot._decisions
                  if d["gate"] == "place_order"]
        assert "would_cross_toctou" in reasons

    def test_hedge_orders_exempt_from_toctou_guard(self, pilot_env, clock):
        """Hedges are IOC/fill_or_kill and DELIBERATELY marketable — the
        guard must never block them, even when they clearly cross."""
        client = FakeKalshiClient(
            books={TICKER: make_book(yes_bid=0.60, no_bid=0.60)})  # yes_ask=0.40
        pilot = build_pilot(clock, client=client)
        pilot.inventory.apply_fill(TICKER, "no", "buy", 5, 0.45)
        oid = pilot.place_pilot_order(TICKER, "yes", "buy", 5, 0.55,
                                      purpose="hedge", reducing=True)
        assert oid is not None
        assert len(client.placed) == 1

    def test_fails_closed_when_presubmit_fetch_raises(self, pilot_env, clock):
        class RaisingClient(FakeKalshiClient):
            def fetch_order_book(self, ticker):
                raise RuntimeError("network blip")

        pilot = build_pilot(clock, client=RaisingClient())
        oid = pilot.place_pilot_order(TICKER, "yes", "buy", 4, 0.49,
                                      purpose="quote_bid")
        assert oid is None

    def test_fails_closed_when_presubmit_fetch_returns_empty(self, pilot_env,
                                                              clock):
        class EmptyBookClient(FakeKalshiClient):
            def fetch_order_book(self, ticker):
                return None

        pilot = build_pilot(clock, client=EmptyBookClient())
        oid = pilot.place_pilot_order(TICKER, "yes", "buy", 4, 0.49,
                                      purpose="quote_bid")
        assert oid is None

    def test_non_crossing_placement_unaffected(self, pilot_env, clock):
        """Sanity check: the guard isn't overly strict on the common case
        of a genuinely still-non-crossing quote."""
        client = FakeKalshiClient()  # default book: yes_ask=0.51
        pilot = build_pilot(clock, client=client)
        oid = pilot.place_pilot_order(TICKER, "yes", "buy", 4, 0.49,
                                      purpose="quote_bid")
        assert oid is not None


# ---------------------------------------------------------------------------
# G6/G11 companions: staleness and depth sizing
# ---------------------------------------------------------------------------

class TestBookGates:
    def test_stale_book_pulls_quotes(self, pilot_env, clock):
        client = FakeKalshiClient()
        pilot = build_pilot(clock, client=client)
        oid = pilot.place_pilot_order(TICKER, "yes", "buy", 4, 0.49,
                                      purpose="quote_bid")
        client.books = {}  # book feed dies: refresh can no longer update it
        clock[0] += 31  # past MM_BOOK_MAX_STALE_SECONDS
        assert pilot.refresh_market(TICKER) == []
        assert pilot.resting_orders(TICKER) == []
        assert oid in client.cancelled

    def test_live_pilot_without_client_places_nothing(self, pilot_env, clock):
        pilot = build_pilot(clock, client=None)
        pilot.update_book(TICKER, make_book())
        oid = pilot.place_pilot_order(TICKER, "yes", "buy", 4, 0.49,
                                      purpose="quote_bid")
        assert oid is None  # fail closed, no AttributeError

    def test_depth_sizing_caps_quote_to_book_fraction(self, pilot_env, clock,
                                                      monkeypatch):
        monkeypatch.setattr(live_config(), "MM_CANARY_QUOTE_SIZE_USD", 50.0)
        # Same-side best size 8 -> depth cap = int(0.25 * 8) = 2 contracts
        client = FakeKalshiClient(
            books={TICKER: make_book(yes_qty=8.0, no_qty=8.0)})
        pilot = build_pilot(clock, client=client)
        placed = pilot.refresh_market(TICKER)
        assert placed
        for oid in placed:
            assert pilot._orders[oid]["count"] <= 2

    def test_depth_below_one_contract_skips_side(self, pilot_env, clock,
                                                 monkeypatch):
        monkeypatch.setattr(live_config(), "MM_CANARY_QUOTE_SIZE_USD", 50.0)
        # Best size 3 -> int(0.25 * 3) = 0 contracts -> side skipped
        client = FakeKalshiClient(
            books={TICKER: make_book(yes_qty=3.0, no_qty=500.0)})
        pilot = build_pilot(clock, client=client)
        placed = pilot.refresh_market(TICKER)
        purposes = {pilot._orders[oid]["purpose"] for oid in placed}
        assert "quote_bid" not in purposes

    # -----------------------------------------------------------------
    # Finding #6: G11 must fail closed (skip the side) when depth is
    # completely UNKNOWN (best is None), not just when it's thin. The two
    # tests above cover "thin but known" (a real tuple with small qty);
    # this covers "no resting-order data at all for that side" — a book
    # source that can report top-of-book without necessarily deriving both
    # sides consistently (e.g. a WS-partial or genuinely one-sided book).
    # Fail-before: _sized_count only applied the depth cap `if best is not
    # None`, so a None best fell through to the UNBOUNDED
    # `int(size_usd / price)` — the opposite of a depth cap.
    #
    # Once Codex round-2's TOCTOU crossing guard (finding #2) landed, this
    # scenario also blocks the BID side, not just the ask G11 already knew
    # was unsizeable: yes_ask is derived FROM no_bid, so no_bid missing
    # means the guard cannot verify a bid wouldn't cross either. See the
    # inline comment in the test body for the full chain.
    # -----------------------------------------------------------------

    def test_unknown_depth_on_one_side_skips_that_side_not_full_size(
            self, pilot_env, clock, monkeypatch):
        monkeypatch.setattr(live_config(), "MM_CANARY_QUOTE_SIZE_USD", 50.0)
        client = FakeKalshiClient(
            books={TICKER: make_book(yes_qty=500.0, no_qty=500.0)})
        pilot = build_pilot(clock, client=client)
        # Seed a normal two-sided book, then simulate the NO side's depth
        # becoming unavailable independently of price/mid — the cached
        # book dict is manipulated directly because the only way to reach
        # this exact state through the public API (an empty no_dollars
        # array) also zeroes `mid` and gets pulled earlier by G6, which
        # would prove nothing about G11 specifically. Emptying
        # client.books (same trick as test_stale_book_pulls_quotes) makes
        # the next auto-refetch a no-op so the manual seed below survives,
        # while `_client` stays real so live placement still works.
        client.books = {}
        with pilot._lock:
            pilot._books[TICKER]["no_bid"] = None
        placed = pilot.refresh_market(TICKER)
        purposes = {pilot._orders[oid]["purpose"] for oid in placed}
        # Our ask sizing reads book["no_bid"] as `best` — unknown depth
        # there must skip the ask side, never fall through to full size.
        assert "quote_ask" not in purposes
        # Codex round-2 finding #2 (TOCTOU pre-submit crossing guard) means
        # the bid side is ALSO blocked here now, for a different reason:
        # yes_ask is derived FROM no_bid (kalshi_api.best_yes_ask), so with
        # no_bid unavailable there is no way to verify a "buy yes" bid
        # wouldn't cross — _would_cross fails closed (aborts) exactly like
        # it would for a genuinely stale/unreachable book. This also means
        # the PRE-EXISTING G12 crossing guard (mm_pilot.py refresh_market)
        # had its own latent gap here — `if best_yes_ask is not None and
        # bid >= best_yes_ask[0]` short-circuits to "no reprice, no skip"
        # when best_yes_ask is None, silently letting an unverified bid
        # through. The TOCTOU guard closes that gap as a side effect: an
        # unknown ask means neither side of this market can be safely
        # quoted, not just the one G11 already knew was unsizeable.
        assert "quote_bid" not in purposes
        assert placed == []

    # -----------------------------------------------------------------
    # Finding #7: book LEVELS staleness must be judged independently from
    # mid/price staleness. A WS mid tick refreshes `updated_at` far more
    # often than a REST book fetch refreshes actual resting-order levels;
    # G11/G12 consume levels and must never treat them as fresh just
    # because a WS price tick looked fresh.
    # Fail-before: only one `updated_at` timestamp existed, refreshed by
    # BOTH update_book (REST, carries levels) and on_ws_price (WS, mid
    # only) — a live WS feed with a dead REST book poller would read as
    # "fresh" forever while G11/G12 quietly sized off ancient levels.
    # -----------------------------------------------------------------

    def test_ws_price_tick_does_not_paper_over_stale_levels(self, pilot_env,
                                                             clock):
        client = FakeKalshiClient()
        pilot = build_pilot(clock, client=client)
        oid = pilot.place_pilot_order(TICKER, "yes", "buy", 4, 0.49,
                                      purpose="quote_bid")
        # One real REST fetch seeds levels_updated_at at clock[0].
        pilot.refresh_market(TICKER)
        # REST book feed dies (mirrors test_stale_book_pulls_quotes), but a
        # WS price tick keeps arriving and refreshing `updated_at`/mid.
        client.books = {}
        clock[0] += 31  # past MM_BOOK_MAX_STALE_SECONDS for LEVELS
        pilot.on_ws_price(TICKER, 0.50)
        # G6 (mid staleness) alone would now read "fresh" (WS just ticked);
        # G6b (levels staleness) must still fail closed and pull.
        assert pilot.refresh_market(TICKER) == []
        assert oid in client.cancelled
        reasons = [d["reason"] for d in pilot._decisions
                  if d["gate"] == "G6b_book_levels_staleness"]
        assert "book_levels_stale" in reasons

    def test_rest_fetch_refreshes_both_mid_and_levels(self, pilot_env, clock):
        """Sanity check: a normal REST-only flow (no WS at all) is
        unaffected by the new gate — levels_updated_at tracks updated_at."""
        client = FakeKalshiClient()
        pilot = build_pilot(clock, client=client)
        assert pilot.refresh_market(TICKER)  # quotes normally
        book = pilot._book(TICKER)
        assert book["levels_updated_at"] == pytest.approx(book["updated_at"])

    def test_deselected_market_gracefully_exits(self, pilot_env, clock):
        client = FakeKalshiClient()
        pilot = build_pilot(clock, client=client)
        oid = pilot.place_pilot_order(TICKER, "yes", "buy", 4, 0.49,
                                      purpose="quote_bid")
        pilot.update_selection([])  # lip_select dropped the market
        assert pilot.refresh_market(TICKER) == []
        assert oid in client.cancelled

    def test_no_selection_snapshot_fails_closed(self, pilot_env, clock):
        client = FakeKalshiClient()
        pilot = build_pilot(clock, client=client)
        pilot._selected = None  # PR #43 selector never ran
        assert pilot.refresh_market(TICKER) == []


class TestLiveEnvelopeOrderGate:
    def test_live_without_envelope_rejected(self, pilot_env, clock, monkeypatch):
        monkeypatch.setattr(pilot_env, "DRY_RUN", False)
        monkeypatch.setattr(pilot_env, "LIVE_ENVELOPE", None)
        pilot = build_pilot(clock, client=FakeKalshiClient())
        result = pilot.authorize_order(TICKER, "yes", "buy", 10, 0.50)
        assert result.allowed is False
        assert result.reason == "live_envelope_missing"

    def test_live_wrong_series_rejected(self, pilot_env, clock, monkeypatch):
        monkeypatch.setattr(pilot_env, "DRY_RUN", False)
        monkeypatch.setattr(pilot_env, "LIVE_ENVELOPE", {
            "venue": "kalshi-d0",
            "pair": "KXFED",
            "max_notional_usd": 150,
            "max_daily_loss_usd": 25,
            "kill_switch": "quit",
        })
        pilot = build_pilot(clock, client=FakeKalshiClient())
        result = pilot.authorize_order(TICKER, "yes", "buy", 10, 0.50)
        assert result.allowed is False
        assert result.reason == "envelope_pair"

    def test_live_wrong_venue_rejected(self, pilot_env, clock, monkeypatch):
        monkeypatch.setattr(pilot_env, "DRY_RUN", False)
        monkeypatch.setattr(pilot_env, "LIVE_ENVELOPE", {
            "venue": "kraken-live",
            "pair": "KXTEST",
            "max_notional_usd": 150,
            "max_daily_loss_usd": 25,
            "kill_switch": "quit",
        })
        pilot = build_pilot(clock, client=FakeKalshiClient())
        result = pilot.authorize_order(TICKER, "yes", "buy", 10, 0.50)
        assert result.allowed is False
        assert result.reason == "envelope_venue"

    def test_live_matching_series_allowed(self, pilot_env, clock, monkeypatch):
        monkeypatch.setattr(pilot_env, "DRY_RUN", False)
        monkeypatch.setattr(pilot_env, "LIVE_ENVELOPE", {
            "venue": "kalshi-d0",
            "pair": "KXTEST",
            "max_notional_usd": 150,
            "max_daily_loss_usd": 25,
            "kill_switch": "quit",
        })
        monkeypatch.setattr(pilot_env, "MM_KALSHI_PILOT_ENABLED", True)
        pilot = build_pilot(clock, client=FakeKalshiClient())
        result = pilot.authorize_order(TICKER, "yes", "buy", 10, 0.50)
        assert result.allowed is True

    def test_daily_loss_still_blocks_after_canary_graduation(self, pilot_env,
                                                              clock, monkeypatch):
        monkeypatch.setattr(pilot_env, "MM_MAX_DAILY_LOSS_USD", 10.0)
        pilot = build_pilot(clock, client=FakeKalshiClient())
        pilot.canary_graduated = True
        pilot.inventory.apply_fill(TICKER, "yes", "buy", 20, 0.60)
        pilot.inventory.apply_fill(TICKER, "yes", "sell", 20, 0.05)

        result = pilot.authorize_order(TICKER, "yes", "buy", 2, 0.50)

        assert result.allowed is False
        assert result.reason == "daily_loss_limit"

    def test_first_realized_fill_after_utc_rollover_counts_toward_limit(
            self, pilot_env, clock, monkeypatch):
        monkeypatch.setattr(pilot_env, "MM_MAX_DAILY_LOSS_USD", 10.0)
        pilot = build_pilot(clock, client=FakeKalshiClient())
        pilot.canary_graduated = True
        pilot.inventory.apply_fill(TICKER, "yes", "buy", 20, 0.60)
        prior_day = pilot._daily_pnl_date
        clock[0] += 86_400

        pilot._process_fill(
            FillEvent(
                fill_id="fill-after-midnight",
                order_id="hedge-after-midnight",
                ticker=TICKER,
                side="yes",
                action="sell",
                count=20,
                price=0.05,
                is_taker=False,
                created_ts=clock[0],
                mid_at_detect=0.50,
            ),
            {"purpose": "hedge"},
        )

        assert pilot._daily_pnl_date != prior_day
        assert pilot._daily_realized_pnl() == pytest.approx(-11.0)
        assert pilot.halted is True
        assert "daily realized P&L" in pilot.halt_reason

    def test_aggregate_resting_notional_is_capped(self, pilot_env, clock,
                                                   monkeypatch):
        monkeypatch.setattr(pilot_env, "MM_MAX_TOTAL_INVENTORY_USD", 100.0)
        pilot = build_pilot(clock, client=FakeKalshiClient())
        pilot.inventory.apply_fill("KXTEST-A", "yes", "buy", 80, 0.50)
        pilot._orders["resting-b"] = {
            "ticker": "KXTEST-B", "side": "yes", "action": "buy",
            "count": 80, "price": 0.50, "purpose": "quote_bid",
            "placed_at": clock[0],
        }

        result = pilot.authorize_order("KXTEST-C", "yes", "buy", 50, 0.50)

        assert result.allowed is False
        assert result.reason == "aggregate_notional_cap"

    def test_dashboard_pause_blocks_mm_authorization(self, pilot_env, clock):
        pilot = build_pilot(clock, client=FakeKalshiClient())
        with patch("dashboard.is_paused", return_value=True):
            result = pilot.authorize_order(TICKER, "yes", "buy", 2, 0.50)
        assert result.allowed is False
        assert result.reason == "dashboard_paused"

    def test_live_sports_ticker_rejected(self, pilot_env, clock, monkeypatch):
        monkeypatch.setattr(pilot_env, "DRY_RUN", False)
        monkeypatch.setattr(pilot_env, "LIVE_ENVELOPE", {
            "venue": "kalshi-d0",
            "pair": "KXNFLGAME",
            "max_notional_usd": 150,
            "max_daily_loss_usd": 25,
            "kill_switch": "quit",
        })
        monkeypatch.setattr(pilot_env, "MM_KALSHI_PILOT_ENABLED", True)
        pilot = build_pilot(clock, client=FakeKalshiClient())
        result = pilot.authorize_order("KXNFLGAME-26AUG18", "yes", "buy", 10, 0.50)
        assert result.allowed is False
        assert result.reason == "policy_blocked"
