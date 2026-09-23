"""Unit tests for CTF fee and net profit calculations in fees.py."""

from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fees import (
    net_profit_ctf_merge,
    net_profit_ctf_mint,
    polymarket_taker_fee,
)


class TestCTFFees:
    """Test suite for CTFMerge and CTFMint net profit modeling."""

    def test_net_profit_ctf_merge_profitable(self) -> None:
        """Verify CTFMerge net profit when YES_ask + NO_ask < 1.00."""
        yes_ask = 0.45
        no_ask = 0.50
        gas_override = 0.01

        res = net_profit_ctf_merge(yes_ask, no_ask, gas_cost=gas_override)
        gross = 1.0 - (0.45 + 0.50)  # 0.05
        assert res["gross_spread"] == pytest.approx(gross)

        fee_y = polymarket_taker_fee(yes_ask)
        fee_n = polymarket_taker_fee(no_ask)
        expected_fees = fee_y + fee_n + gas_override
        assert res["fees"] == pytest.approx(expected_fees)
        assert res["net_profit"] == pytest.approx(gross - expected_fees)

    def test_net_profit_ctf_merge_unprofitable(self) -> None:
        """Verify CTFMerge returns non-positive spread when YES_ask + NO_ask >= 1.00."""
        res = net_profit_ctf_merge(0.52, 0.50)
        assert res["gross_spread"] == pytest.approx(-0.02)
        assert res["net_profit"] <= 0.0
        assert res["fees"] == 0.0

    def test_net_profit_ctf_mint_profitable(self) -> None:
        """Verify CTFMint net profit when YES_bid + NO_bid > 1.00."""
        yes_bid = 0.54
        no_bid = 0.52
        gas_override = 0.015

        res = net_profit_ctf_mint(yes_bid, no_bid, gas_cost=gas_override)
        gross = (0.54 + 0.52) - 1.0  # 0.06
        assert res["gross_spread"] == pytest.approx(gross)

        fee_y = polymarket_taker_fee(yes_bid)
        fee_n = polymarket_taker_fee(no_bid)
        expected_fees = fee_y + fee_n + gas_override
        assert res["fees"] == pytest.approx(expected_fees)
        assert res["net_profit"] == pytest.approx(gross - expected_fees)

    def test_net_profit_ctf_mint_unprofitable(self) -> None:
        """Verify CTFMint returns non-positive spread when YES_bid + NO_bid <= 1.00."""
        res = net_profit_ctf_mint(0.48, 0.49)
        assert res["gross_spread"] == pytest.approx(-0.03)
        assert res["net_profit"] <= 0.0
        assert res["fees"] == 0.0
