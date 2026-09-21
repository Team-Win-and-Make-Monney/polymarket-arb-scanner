"""Unit tests for ctf_api.py CTFClient."""

from __future__ import annotations

import os
import sys
from unittest.mock import MagicMock, patch

import pytest
from eth_abi import decode as abi_decode

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from contracts.ctf_abis import (
    CONVERT_POSITIONS_SELECTOR,
    CONVERT_POSITIONS_TYPES,
    MERGE_POSITIONS_SELECTOR,
    MERGE_POSITIONS_TYPES,
    REDEEM_POSITIONS_SELECTOR,
    REDEEM_POSITIONS_TYPES,
    SPLIT_POSITION_SELECTOR,
    SPLIT_POSITION_TYPES,
)
from ctf_api import CTFClient, _to_bytes32


class TestCTFClient:
    """Test suite for CTFClient read, encode, simulation, and execution guards."""

    def test_to_bytes32_normalization(self) -> None:
        """Verify _to_bytes32 handles raw bytes, 0x-hex, and non-prefixed hex."""
        raw_bytes = b"\x01" * 32
        assert _to_bytes32(raw_bytes) == raw_bytes

        hex_with_prefix = "0x" + "aa" * 32
        assert _to_bytes32(hex_with_prefix) == bytes.fromhex("aa" * 32)

        short_hex = "0x1234"
        padded = _to_bytes32(short_hex)
        assert len(padded) == 32
        assert padded[-2:] == bytes.fromhex("1234")

        with pytest.raises(ValueError):
            _to_bytes32(b"\x01" * 30)

        with pytest.raises(ValueError):
            _to_bytes32("invalid_hex_string_that_is_too_long_or_not_hex" * 4)

    def test_build_split(self) -> None:
        """Verify build_split returns correctly formatted transaction dict and calldata."""
        client = CTFClient(dry_run=True)
        condition_id = "0x" + "11" * 32
        amount = 50.0

        tx = client.build_split(condition_id, amount_collateral=amount)
        assert tx["to"] == client.conditional_tokens
        assert tx["action"] == "split"
        assert tx["amount"] == 50.0

        data = tx["data"]
        assert data.startswith(SPLIT_POSITION_SELECTOR)
        encoded_args = bytes.fromhex(data[len(SPLIT_POSITION_SELECTOR):])
        decoded = abi_decode(SPLIT_POSITION_TYPES, encoded_args)
        assert decoded[0].lower() == client.collateral_token.lower()
        assert decoded[1] == b"\x00" * 32
        assert decoded[2] == bytes.fromhex("11" * 32)
        assert list(decoded[3]) == [1, 2]
        assert decoded[4] == 50_000_000  # 50 USDC in 6 decimals

    def test_build_merge(self) -> None:
        """Verify build_merge returns correctly formatted transaction dict and calldata."""
        client = CTFClient(dry_run=True)
        condition_id = "0x" + "22" * 32
        amount = 25.5

        tx = client.build_merge(condition_id, amount=amount)
        assert tx["to"] == client.conditional_tokens
        assert tx["action"] == "merge"
        assert tx["amount"] == 25.5

        data = tx["data"]
        assert data.startswith(MERGE_POSITIONS_SELECTOR)
        encoded_args = bytes.fromhex(data[len(MERGE_POSITIONS_SELECTOR):])
        decoded = abi_decode(MERGE_POSITIONS_TYPES, encoded_args)
        assert decoded[0].lower() == client.collateral_token.lower()
        assert decoded[1] == b"\x00" * 32
        assert decoded[2] == bytes.fromhex("22" * 32)
        assert list(decoded[3]) == [1, 2]
        assert decoded[4] == 25_500_000

    def test_build_redeem(self) -> None:
        """Verify build_redeem returns correctly formatted transaction dict and calldata."""
        client = CTFClient(dry_run=True)
        condition_id = "0x" + "33" * 32

        tx = client.build_redeem(condition_id, index_sets=[1, 2])
        assert tx["to"] == client.conditional_tokens
        assert tx["action"] == "redeem"

        data = tx["data"]
        assert data.startswith(REDEEM_POSITIONS_SELECTOR)
        encoded_args = bytes.fromhex(data[len(REDEEM_POSITIONS_SELECTOR):])
        decoded = abi_decode(REDEEM_POSITIONS_TYPES, encoded_args)
        assert decoded[0].lower() == client.collateral_token.lower()
        assert decoded[1] == b"\x00" * 32
        assert decoded[2] == bytes.fromhex("33" * 32)
        assert list(decoded[3]) == [1, 2]

    def test_build_convert(self) -> None:
        """Verify build_convert returns correctly formatted transaction dict and calldata."""
        client = CTFClient(dry_run=True)
        market_id = "0x" + "44" * 32
        index_set = 2
        amount = 10.0

        tx = client.build_convert(market_id, index_set=index_set, amount=amount)
        assert tx["to"] == client.neg_risk_adapter
        assert tx["action"] == "convert"
        assert tx["market_id"] == market_id

        data = tx["data"]
        assert data.startswith(CONVERT_POSITIONS_SELECTOR)
        encoded_args = bytes.fromhex(data[len(CONVERT_POSITIONS_SELECTOR):])
        decoded = abi_decode(CONVERT_POSITIONS_TYPES, encoded_args)
        assert decoded[0] == bytes.fromhex("44" * 32)
        assert decoded[1] == 2
        assert decoded[2] == 10_000_000

    def test_collateral_balance_rpc(self) -> None:
        """Verify collateral_balance decodes 6-decimal uint256 from eth_call result."""
        client = CTFClient(dry_run=True)
        # 100.50 USDC = 100_500_000 units = 0x05fd58a0
        mock_result = "0x" + hex(100_500_000)[2:].zfill(64)
        client._rpc_call = MagicMock(return_value=mock_result)

        balance = client.collateral_balance("0x1111111111111111111111111111111111111111")
        assert balance == pytest.approx(100.50)

    def test_position_balance_rpc(self) -> None:
        """Verify position_balance decodes 6-decimal uint256 from eth_call result."""
        client = CTFClient(dry_run=True)
        # 50 shares = 50_000_000 units = 0x02faf080
        mock_result = "0x" + hex(50_000_000)[2:].zfill(64)
        client._rpc_call = MagicMock(return_value=mock_result)

        balance = client.position_balance(12345678, "0x1111111111111111111111111111111111111111")
        assert balance == pytest.approx(50.0)

    def test_allowance_and_ensure_allowance(self) -> None:
        """Verify get_allowance and ensure_allowance."""
        client = CTFClient(dry_run=True)
        client.address = "0x1111111111111111111111111111111111111111"
        mock_result = "0x" + hex(200_000_000)[2:].zfill(64)  # 200 USDC
        client._rpc_call = MagicMock(return_value=mock_result)

        allowance = client.get_allowance("0x2222222222222222222222222222222222222222")
        assert allowance == pytest.approx(200.0)
        assert client.ensure_allowance("0x2222222222222222222222222222222222222222", 150.0) is True
        assert client.ensure_allowance("0x2222222222222222222222222222222222222222", 250.0) is False

    def test_estimate_gas(self) -> None:
        """Verify estimate_gas calls RPC and falls back on exception."""
        client = CTFClient(dry_run=True)
        tx = {"to": client.conditional_tokens, "data": "0x1234", "gas_estimate": 150_000}

        # Successful estimate
        client._rpc_call = MagicMock(return_value="0x1e848")  # 125,000 in hex
        assert client.estimate_gas(tx) == 125_000

        # Fallback on RPC failure
        client._rpc_call = MagicMock(side_effect=RuntimeError("RPC down"))
        assert client.estimate_gas(tx) == 150_000

    def test_send_raises_not_implemented_error(self) -> None:
        """Phase 4a requirement: on-chain broadcast must strictly fail closed with NotImplementedError."""
        client = CTFClient(dry_run=True)
        tx = client.build_merge("0x" + "00" * 32, 10.0)
        with pytest.raises(NotImplementedError, match="disabled in Phase 4a"):
            client.send(tx)
