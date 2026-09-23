"""Unit tests for uma_monitor.py dispute state classification."""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from uma_monitor import classify_dispute_state, fetch_dispute_states


class TestClassifyDisputeState:
    """Test dispute classification against multiple Gamma market states."""

    def test_disputed_market_blocked(self) -> None:
        market = {
            "conditionId": "0x123abc",
            "umaResolutionStatus": "disputed",
            "closed": True,
            "resolved": False,
            "acceptingOrders": False,
        }
        res = classify_dispute_state(market)
        assert res["condition_id"] == "0x123abc"
        assert res["blocked"] is True
        assert res["reason"] == "uma_disputed"
        assert res["state"] == "disputed"

    def test_proposed_market_blocked(self) -> None:
        market = {
            "conditionId": "0x456def",
            "umaResolutionStatus": "proposed",
            "closed": True,
            "resolved": False,
            "acceptingOrders": True,
        }
        res = classify_dispute_state(market)
        assert res["condition_id"] == "0x456def"
        assert res["blocked"] is True
        assert res["reason"] == "uma_proposed"
        assert res["state"] == "proposed"

    def test_closed_unresolved_blocked(self) -> None:
        market = {
            "conditionId": "0x789ghi",
            "umaResolutionStatus": "",
            "closed": True,
            "resolved": False,
            "acceptingOrders": True,
        }
        res = classify_dispute_state(market)
        assert res["condition_id"] == "0x789ghi"
        assert res["blocked"] is True
        assert res["reason"] == "closed_unresolved"
        assert res["state"] == "closed"

    def test_not_accepting_orders_blocked(self) -> None:
        market = {
            "conditionId": "0xaaa111",
            "umaResolutionStatus": "",
            "closed": False,
            "resolved": False,
            "acceptingOrders": False,
        }
        res = classify_dispute_state(market)
        assert res["condition_id"] == "0xaaa111"
        assert res["blocked"] is True
        assert res["reason"] == "not_accepting_orders"
        assert res["state"] == "open"

    def test_clean_open_market_clear(self) -> None:
        market = {
            "conditionId": "0xbbb222",
            "umaResolutionStatus": "",
            "closed": False,
            "resolved": False,
            "acceptingOrders": True,
        }
        res = classify_dispute_state(market)
        assert res["condition_id"] == "0xbbb222"
        assert res["blocked"] is False
        assert res["reason"] == "clear"
        assert res["state"] == "open"

    def test_resolved_market_clear(self) -> None:
        market = {
            "conditionId": "0xccc333",
            "umaResolutionStatus": "resolved",
            "closed": True,
            "resolved": True,
            "acceptingOrders": True,
        }
        res = classify_dispute_state(market)
        assert res["condition_id"] == "0xccc333"
        assert res["blocked"] is False
        assert res["reason"] == "clear"
        assert res["state"] == "resolved"


class TestFetchDisputeStates:
    """Test batch extraction and mapping of dispute states."""

    def test_fetch_maps_condition_ids(self) -> None:
        markets = [
            {"conditionId": "0x001", "umaResolutionStatus": "disputed"},
            {"conditionId": "0x002", "umaResolutionStatus": ""},
            {"title": "Missing condition ID"},
        ]
        res = fetch_dispute_states(markets)
        assert len(res) == 2
        assert "0x001" in res
        assert res["0x001"]["blocked"] is True
        assert "0x002" in res
        assert res["0x002"]["blocked"] is False

    def test_fetch_processes_nested_event_markets(self) -> None:
        event = {
            "id": "evt_1",
            "title": "Fed Event",
            "markets": [
                {"conditionId": "0xsub1", "umaResolutionStatus": "proposed"},
                {"condition_id": "0xsub2", "closed": True, "resolved": False},
            ],
        }
        res = fetch_dispute_states([event])
        assert len(res) == 2
        assert res["0xsub1"]["blocked"] is True
        assert res["0xsub1"]["reason"] == "uma_proposed"
        assert res["0xsub2"]["blocked"] is True
        assert res["0xsub2"]["reason"] == "closed_unresolved"

    def test_fetch_empty_or_invalid_inputs(self) -> None:
        assert fetch_dispute_states([]) == {}
        assert fetch_dispute_states([None, "invalid"]) == {}
