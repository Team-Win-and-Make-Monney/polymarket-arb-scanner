"""Tests for in-memory WebSocket orderbook scanning across scan modules."""

import sys
import os
from unittest.mock import MagicMock, patch

# Add project root to sys.path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))



class TestBinaryScanWSOrderbook:
    """Test in-memory WebSocket orderbook usage in binary scans."""

    def test_binary_scan_uses_fresh_feed_manager_orderbook(self):
        """When fresh in-memory books exist in feed_manager, REST get_clob_prices is skipped."""
        market = {
            "question": "Will team A win?",
            "conditionId": "cond1",
            "outcomes": ["Yes", "No"],
            "clobTokenIds": ["tok_yes", "tok_no"],
            "outcomePrices": ["0.45", "0.45"],
            "volume": 1000,
            "category": "sports",
        }
        mock_feed = MagicMock()
        mock_feed.get_polymarket_orderbook.side_effect = lambda tok: {
            "tok_yes": {"asks": [{"price": 0.46, "size": 150}], "bids": [{"price": 0.44, "size": 100}]},
            "tok_no": {"asks": [{"price": 0.47, "size": 200}], "bids": [{"price": 0.43, "size": 100}]},
        }.get(tok)
        mock_feed.get_polymarket_orderbook_age.return_value = 2.0  # fresh (2s old)

        import scans.binary as sb
        with patch.object(sb, "get_binary_markets", return_value=[market]), \
             patch.object(sb, "parse_outcome_prices", return_value=[0.45, 0.45]), \
             patch.object(sb, "_within_resolution_window", return_value=True), \
             patch("scans.helpers.get_clob_prices") as mock_rest:
            opps = sb.scan_binary_internal([market], min_profit=0.01, feed_manager=mock_feed)

        # 0.46 + 0.47 = 0.93 -> profit = 1 - 0.93 = 0.07 >= 0.01
        assert len(opps) == 1
        assert opps[0]["prices"] == "Y=0.460 N=0.470"
        assert opps[0]["_clob_depth"] == 150
        mock_rest.assert_not_called()

    def test_binary_scan_falls_back_to_rest_when_ws_book_stale(self):
        """When feed_manager book is stale (>30s), fall back to REST get_clob_prices."""
        market = {
            "question": "Will team A win?",
            "conditionId": "cond1",
            "outcomes": ["Yes", "No"],
            "clobTokenIds": ["tok_yes", "tok_no"],
            "outcomePrices": ["0.45", "0.45"],
            "volume": 1000,
            "category": "sports",
        }
        mock_feed = MagicMock()
        mock_feed.get_polymarket_orderbook.return_value = {
            "asks": [{"price": 0.46, "size": 150}], "bids": [{"price": 0.44, "size": 100}],
        }
        mock_feed.get_polymarket_orderbook_age.return_value = 45.0  # stale (>30s)

        import scans.binary as sb
        with patch.object(sb, "get_binary_markets", return_value=[market]), \
             patch.object(sb, "parse_outcome_prices", return_value=[0.45, 0.45]), \
             patch.object(sb, "_within_resolution_window", return_value=True), \
             patch("scans.helpers.get_clob_prices") as mock_rest:
            mock_rest.return_value = {
                "yes_ask": 0.46, "yes_ask_size": 100,
                "no_ask": 0.46, "no_ask_size": 100,
                "yes_bid": 0.44, "yes_bid_size": 100,
                "no_bid": 0.44, "no_bid_size": 100,
            }
            opps = sb.scan_binary_internal([market], min_profit=0.01, feed_manager=mock_feed)

        mock_rest.assert_called_once_with(market)
        assert len(opps) == 1
        assert opps[0]["prices"] == "Y=0.460 N=0.460"


class TestCrossScanWSOrderbook:
    """Test in-memory WebSocket orderbook usage in cross-platform scans."""

    def test_cross_scan_uses_fresh_orderbooks_both_sides(self):
        """Cross scan refines using feed_manager orderbooks for PM and Kalshi without REST."""
        poly_market = {
            "question": "Fed rate cut in May?",
            "conditionId": "poly_cond1",
            "outcomes": ["Yes", "No"],
            "clobTokenIds": ["tok_y", "tok_n"],
            "outcomePrices": ["0.35", "0.65"],
            "volume": 5000,
        }
        kalshi_market = {
            "ticker": "FED-MAY-CUT",
            "title": "Fed rate cut in May?",
            "yes_bid": 38,
            "no_bid": 52,
        }
        kalshi_event = {
            "event_ticker": "FED-MAY",
            "title": "Fed rate cut in May?",
            "markets": [kalshi_market],
        }

        mock_kalshi_client = MagicMock()
        mock_kalshi_client.fetch_all_events.return_value = [kalshi_event]
        mock_kalshi_client.get_market_price.return_value = (0.40, 0.45)

        mock_feed = MagicMock()
        # Polymarket book: yes_ask = 0.35, size 80; no_ask = 0.65, size 90
        mock_feed.get_polymarket_orderbook.side_effect = lambda tok: {
            "tok_y": {"asks": [{"price": 0.35, "size": 80}], "bids": [{"price": 0.33, "size": 50}]},
            "tok_n": {"asks": [{"price": 0.65, "size": 90}], "bids": [{"price": 0.63, "size": 50}]},
        }.get(tok)
        mock_feed.get_polymarket_orderbook_age.return_value = 1.0

        # Kalshi book: yes bids at 55c (implies no_ask = 45c), no bids at 60c (implies yes_ask = 40c)
        kalshi_ws_book = {
            "orderbook": {
                "yes": [[55, 120]],
                "no": [[60, 110]],
            }
        }
        mock_feed.get_orderbook.return_value = (kalshi_ws_book, 1.5)

        import scans.cross as sc
        with patch.object(sc, "get_binary_markets", return_value=[poly_market]), \
             patch.object(sc, "_within_resolution_window", return_value=True), \
             patch.object(sc, "match_markets_to_events", return_value=[{
                 "polymarket": poly_market,
                 "kalshi_event": kalshi_event,
                 "similarity": 95,
                 "confidence": "HIGH",
                 "inverted": False,
             }]), patch("scans.helpers.get_clob_prices") as mock_pm_rest:

            opps = sc.scan_cross_platform(
                [poly_market], mock_kalshi_client, min_profit=0.01,
                kalshi_events_preloaded=[kalshi_event],
                kalshi_markets_by_event={"FED-MAY": [kalshi_market]},
                feed_manager=mock_feed,
            )

        # PM_YES (0.35) + K_NO (0.45) = 0.80 -> net profit ~0.18 >= 0.01
        assert len(opps) == 1
        opp = opps[0]
        assert opp["type"] == "Cross(PM_YES + K_NO)"
        assert opp["_kalshi_yes"] == 0.40
        assert opp["_kalshi_no"] == 0.45
        assert opp["_clob_depth"] == 80  # min(80, 110)
        mock_pm_rest.assert_not_called()

    def test_cross_scan_ws_preserves_inverted_kalshi_orientation(self):
        """Cross scan with inverted=True swaps raw Kalshi YES/NO ask prices."""
        poly_market = {
            "question": "Candidate A will lose?",
            "conditionId": "poly_cond2",
            "outcomes": ["Yes", "No"],
            "clobTokenIds": ["tok_y2", "tok_n2"],
            "outcomePrices": ["0.35", "0.65"],
            "volume": 5000,
        }
        kalshi_market = {
            "ticker": "CAND-A-WIN",
            "title": "Candidate A will win?",
            "yes_bid": 38,
            "no_bid": 52,
        }
        kalshi_event = {
            "event_ticker": "CAND-A",
            "title": "Candidate A will win?",
            "markets": [kalshi_market],
        }

        mock_kalshi_client = MagicMock()
        mock_kalshi_client.fetch_all_events.return_value = [kalshi_event]
        # Inverted: Kalshi YES=0.60, NO=0.40 becomes stage 1 raw (0.40, 0.60)
        mock_kalshi_client.get_market_price.return_value = (0.40, 0.60)

        mock_feed = MagicMock()
        mock_feed.get_polymarket_orderbook.side_effect = lambda tok: {
            "tok_y2": {"asks": [{"price": 0.35, "size": 80}], "bids": [{"price": 0.33, "size": 50}]},
            "tok_n2": {"asks": [{"price": 0.65, "size": 90}], "bids": [{"price": 0.63, "size": 50}]},
        }.get(tok)
        mock_feed.get_polymarket_orderbook_age.return_value = 1.0

        # Raw Kalshi WS: yes bids at 55c (implies no_ask = 45c), no bids at 60c (implies yes_ask = 40c)
        # Inverted should swap them: _kalshi_yes = 0.45, _kalshi_no = 0.40
        kalshi_ws_book = {
            "orderbook": {
                "yes": [[55, 120]],
                "no": [[60, 110]],
            }
        }
        mock_feed.get_orderbook.return_value = (kalshi_ws_book, 1.5)

        import scans.cross as sc
        with patch.object(sc, "get_binary_markets", return_value=[poly_market]), \
             patch.object(sc, "detect_inverted", return_value=True), \
             patch.object(sc, "_within_resolution_window", return_value=True), \
             patch.object(sc, "match_markets_to_events", return_value=[{
                 "polymarket": poly_market,
                 "kalshi_event": kalshi_event,
                 "similarity": 95,
                 "confidence": "HIGH",
                 "inverted": True,
             }]), patch("scans.helpers.get_clob_prices") as mock_pm_rest:

            opps = sc.scan_cross_platform(
                [poly_market], mock_kalshi_client, min_profit=0.01,
                kalshi_events_preloaded=[kalshi_event],
                kalshi_markets_by_event={"CAND-A": [kalshi_market]},
                feed_manager=mock_feed,
            )

        assert len(opps) == 1
        opp = opps[0]
        # Inverted: raw y_ask (0.40) and n_ask (0.45) were swapped
        assert opp["_kalshi_yes"] == 0.45
        assert opp["_kalshi_no"] == 0.40
        mock_pm_rest.assert_not_called()


class TestKalshiScanWSOrderbook:
    """Test in-memory WebSocket orderbook usage in Kalshi scans."""

    def test_kalshi_binary_uses_ws_orderbook_in_stage1_and_stage2(self):
        """Kalshi binary scan reads prices from WS orderbook and skips REST depth."""
        km = {
            "ticker": "KXTEST-01",
            "title": "Will X occur?",
        }
        events = [{"event_ticker": "KXTEST", "title": "KXTEST Event"}]
        markets_by_event = {"KXTEST": [km]}

        mock_kalshi_client = MagicMock()
        mock_kalshi_client.fetch_all_events.return_value = events

        # Kalshi orderbook with:
        # yes bids at 50c (no_ask = 50c, size 250)
        # no bids at 60c (yes_ask = 40c, size 200)
        # sum of asks = 0.40 + 0.50 = 0.90 -> net profit = 0.06 >= 0.005!
        book = {
            "orderbook": {
                "yes": [[50, 250]],
                "no": [[60, 200]],
            }
        }
        mock_feed = MagicMock()
        mock_feed.get_orderbook.return_value = (book, 1.0)

        import scans.kalshi as sk
        with patch.object(sk, "_within_resolution_window", return_value=True):
            opps = sk.scan_kalshi_binary(
                mock_kalshi_client, min_profit=0.005,
                kalshi_data=(events, markets_by_event, {}),
                feed_manager=mock_feed,
            )

        assert len(opps) == 1
        opp = opps[0]
        assert opp["type"] == "KalshiBinary"
        assert opp["_kalshi_yes"] == 0.40
        assert opp["_kalshi_no"] == 0.50
        assert opp["_clob_depth"] == 200  # min(200, 250)
        # REST depth fetch must not be called
        mock_kalshi_client.get_order_book_depth.assert_not_called()

    def test_kalshi_multi_uses_ws_orderbook_for_depth(self):
        """Kalshi multi scan reads depth from WS orderbook and skips REST depth."""
        km1 = {"ticker": "KXM-01", "title": "Outcome 1", "yes_bid": 30}
        km2 = {"ticker": "KXM-02", "title": "Outcome 2", "yes_bid": 30}
        km3 = {"ticker": "KXM-03", "title": "Other", "yes_bid": 30}
        events = [{
            "event_ticker": "KXMEVT",
            "title": "Multi Event",
            "mutually_exclusive": True,
            "markets": [km1, km2, km3],
        }]
        markets_by_event = {"KXMEVT": [km1, km2, km3]}

        mock_kalshi_client = MagicMock()
        mock_kalshi_client.get_market_price.side_effect = lambda m: (0.30, 0.70)

        mock_feed = MagicMock()
        # For each ticker, WS book provides yes depth
        mock_feed.get_orderbook.side_effect = lambda plat, ticker: (
            {"orderbook": {"yes": [[30, 100]], "no": [[70, 75]]}},
            2.0,
        )

        import scans.kalshi as sk
        with patch.object(sk, "_within_resolution_window", return_value=True), \
             patch.object(sk, "_is_exhaustive_categorical", return_value=True):
            opps = sk.scan_kalshi_multi(
                mock_kalshi_client, min_profit=0.01,
                kalshi_data=(events, markets_by_event, {"KXMEVT": "Multi Event"}),
                feed_manager=mock_feed,
            )

        assert len(opps) == 1
        assert opps[0]["_clob_depth"] == 75  # derived from best NO bid (which is yes ask size)
        mock_kalshi_client.get_order_book_depth.assert_not_called()


class TestNegriskScanWSOrderbook:
    """Test in-memory WebSocket orderbook usage in NegRisk scans."""

    def test_negrisk_uses_feed_manager_orderbooks_in_stage2(self):
        """NegRisk scan Stage 2 refinement uses feed_manager without calling REST."""
        m1 = {
            "groupItemTitle": "Cand A",
            "conditionId": "cond_a",
            "clobTokenIds": ["tok_a_y", "tok_a_n"],
            "outcomePrices": ["0.30", "0.70"],
        }
        m2 = {
            "groupItemTitle": "Cand B",
            "conditionId": "cond_b",
            "clobTokenIds": ["tok_b_y", "tok_b_n"],
            "outcomePrices": ["0.30", "0.70"],
        }
        m3 = {
            "groupItemTitle": "Cand C",
            "conditionId": "cond_c",
            "clobTokenIds": ["tok_c_y", "tok_c_n"],
            "outcomePrices": ["0.30", "0.70"],
        }
        event = {
            "id": "evt_nr",
            "title": "Who will win election?",
            "category": "politics",
            "markets": [m1, m2, m3],
        }

        mock_feed = MagicMock()
        mock_feed.get_polymarket_orderbook.side_effect = lambda tok: {
            "tok_a_y": {"asks": [{"price": 0.31, "size": 100}], "bids": [{"price": 0.29, "size": 50}]},
            "tok_a_n": {"asks": [{"price": 0.71, "size": 100}], "bids": [{"price": 0.69, "size": 50}]},
            "tok_b_y": {"asks": [{"price": 0.31, "size": 120}], "bids": [{"price": 0.29, "size": 50}]},
            "tok_b_n": {"asks": [{"price": 0.71, "size": 100}], "bids": [{"price": 0.69, "size": 50}]},
            "tok_c_y": {"asks": [{"price": 0.31, "size": 80}], "bids": [{"price": 0.29, "size": 50}]},
            "tok_c_n": {"asks": [{"price": 0.71, "size": 100}], "bids": [{"price": 0.69, "size": 50}]},
        }.get(tok)
        mock_feed.get_polymarket_orderbook_age.return_value = 1.0

        import scans.negrisk as sn
        with patch.object(sn, "get_negrisk_events", return_value=[event]), \
             patch.object(sn, "parse_outcome_prices", side_effect=lambda m: [float(p) for p in m.get("outcomePrices", [])]), \
             patch.object(sn, "_within_resolution_window", return_value=True), \
             patch("scans.helpers.get_clob_prices") as mock_rest:

            opps = sn.scan_negrisk_internal([event], min_profit=0.01, feed_manager=mock_feed)

        # 0.31 + 0.31 + 0.31 = 0.93 -> profit 0.07 >= 0.01
        assert len(opps) == 1
        assert opps[0]["_clob_depth"] == 80  # min(100, 120, 80)
        mock_rest.assert_not_called()
