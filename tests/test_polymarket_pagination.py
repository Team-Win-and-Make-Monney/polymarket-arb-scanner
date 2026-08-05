"""Polymarket keyset and reward-market pagination regressions."""
import json
import sys
import os
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# Stub the CLOB SDK before importing the module under test.
for mod in ("py_clob_client_v2", "py_clob_client_v2.client", "py_clob_client_v2.clob_types", "py_clob_client_v2.http_helpers", "py_clob_client_v2.http_helpers.helpers"):
    sys.modules.setdefault(mod, MagicMock())

import polymarket_api


def _resp(rows):
    r = MagicMock()
    r.json.return_value = rows
    return r


def _keyset_server(resource, total_rows, cap=100):
    """Simulate Gamma keyset pages with opaque cursor tokens."""
    data = [{"id": i} for i in range(total_rows)]

    def handler(url, params=None, timeout=15):
        cursor = params.get("after_cursor")
        offset = int(cursor.split("-")[1]) if cursor else 0
        page_size = min(params["limit"], cap)
        rows = data[offset:offset + page_size]
        next_offset = offset + len(rows)
        next_cursor = f"cursor-{next_offset}" if next_offset < total_rows else None
        return _resp({resource: rows, "next_cursor": next_cursor})

    return handler


class TestGammaPagination:
    def test_markets_use_keyset_cursor_and_numeric_volume_order(self):
        with patch.object(
            polymarket_api, "_get_with_retry", side_effect=_keyset_server("markets", 250),
        ) as mock_get:
            markets = polymarket_api.fetch_all_markets(limit=500, max_pages=20)
        assert len(markets) == 250
        first_params = mock_get.call_args_list[0].kwargs["params"]
        second_params = mock_get.call_args_list[1].kwargs["params"]
        assert first_params["order"] == "volumeNum"
        assert first_params["ascending"] == "false"
        assert "after_cursor" not in first_params
        assert second_params["after_cursor"] == "cursor-100"

    def test_markets_stop_at_explicit_page_bound(self):
        with patch.object(
            polymarket_api, "_get_with_retry", side_effect=_keyset_server("markets", 250),
        ) as mock_get:
            markets = polymarket_api.fetch_all_markets(limit=500, max_pages=2)
        assert len(markets) == 200
        assert mock_get.call_count == 2

    def test_events_use_keyset_cursor_and_volume_order(self):
        with patch.object(
            polymarket_api, "_get_with_retry", side_effect=_keyset_server("events", 250),
        ) as mock_get:
            events = polymarket_api.fetch_events(limit=500, max_pages=20)
        assert len(events) == 250
        assert mock_get.call_args_list[0].kwargs["params"]["order"] == "volume"


def _sampling_market(condition_id, rate=25):
    return {
        "condition_id": condition_id,
        "question": f"Question {condition_id}",
        "active": True,
        "closed": False,
        "accepting_orders": True,
        "tags": ["Politics"],
        "rewards": {
            "rates": [{"rewards_daily_rate": rate}],
            "min_size": 20,
            "max_spread": 3.5,
        },
        "tokens": [
            {"token_id": f"no-{condition_id}", "outcome": "No", "price": 0.6},
            {"token_id": f"yes-{condition_id}", "outcome": "Yes", "price": 0.4},
        ],
    }


class TestRewardMarketPagination:
    def test_sampling_markets_paginate_dedupe_and_normalize(self):
        pages = [
            {"data": [_sampling_market("A"), _sampling_market("B")], "next_cursor": "two"},
            {"data": [_sampling_market("B"), _sampling_market("C")], "next_cursor": "LTE="},
        ]
        with patch.object(polymarket_api, "_get_with_retry", side_effect=map(_resp, pages)) as mock_get:
            markets = polymarket_api.fetch_reward_markets()

        assert {market["conditionId"] for market in markets} == {"A", "B", "C"}
        assert mock_get.call_args_list[1].kwargs["params"] == {"next_cursor": "two"}
        market = next(item for item in markets if item["conditionId"] == "A")
        assert market["incentives"]["pool_size_usdc"] == 25
        assert market["incentives"]["max_incentive_spread"] == 0.035
        assert json.loads(market["clobTokenIds"]) == ["yes-A", "no-A"]
        assert market["outcomePrices"] == [0.4, 0.6]

    def test_sampling_market_with_invalid_rewards_fails_closed(self):
        market = _sampling_market("BAD", rate=0)
        assert polymarket_api._normalize_sampling_market(market) is None
