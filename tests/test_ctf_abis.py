"""Unit tests for contracts/ctf_abis.py CTF function signatures, selectors, and type lists."""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from eth_abi import decode as abi_decode, encode as abi_encode

from contracts.ctf_abis import (
    CONVERT_POSITIONS_SELECTOR,
    CONVERT_POSITIONS_TYPES,
    ERC1155_BALANCE_OF_SELECTOR,
    ERC1155_BALANCE_OF_TYPES,
    ERC1155_IS_APPROVED_FOR_ALL_SELECTOR,
    ERC1155_SET_APPROVAL_FOR_ALL_SELECTOR,
    ERC20_ALLOWANCE_SELECTOR,
    ERC20_ALLOWANCE_TYPES,
    ERC20_APPROVE_SELECTOR,
    ERC20_APPROVE_TYPES,
    ERC20_BALANCE_OF_SELECTOR,
    ERC20_BALANCE_OF_TYPES,
    MERGE_POSITIONS_SELECTOR,
    MERGE_POSITIONS_TYPES,
    REDEEM_POSITIONS_SELECTOR,
    REDEEM_POSITIONS_TYPES,
    SPLIT_POSITION_SELECTOR,
    SPLIT_POSITION_TYPES,
    _calc_selector,
)


class TestCTFABIs:
    """Test suite for CTF ABI definitions and 4-byte selectors."""

    def test_computed_selectors(self) -> None:
        """Verify 4-byte Solidity selectors match canonical EVM keccak256 hashes."""
        assert SPLIT_POSITION_SELECTOR == "0x72ce4275"
        assert MERGE_POSITIONS_SELECTOR == "0x9e7212ad"
        assert REDEEM_POSITIONS_SELECTOR == "0x01b7037c"
        assert CONVERT_POSITIONS_SELECTOR == "0xc64748c4"
        assert ERC20_APPROVE_SELECTOR == "0x095ea7b3"
        assert ERC20_ALLOWANCE_SELECTOR == "0xdd62ed3e"
        assert ERC20_BALANCE_OF_SELECTOR == "0x70a08231"
        assert ERC1155_BALANCE_OF_SELECTOR == "0x00fdd58e"
        assert ERC1155_IS_APPROVED_FOR_ALL_SELECTOR == "0xe985e9c5"
        assert ERC1155_SET_APPROVAL_FOR_ALL_SELECTOR == "0xa22cb465"

    def test_calc_selector_helper(self) -> None:
        """Verify _calc_selector helper returns 10 chars (0x + 8 hex chars)."""
        sel = _calc_selector("transfer(address,uint256)")
        assert sel == "0xa9059cbb"
        assert len(sel) == 10

    def test_split_position_encoding(self) -> None:
        """Verify parameter encoding and decoding for splitPosition."""
        assert SPLIT_POSITION_TYPES == ["address", "bytes32", "bytes32", "uint256[]", "uint256"]
        collateral = "0xC011a7E12a19f7B1f670d46F03B03f3342E82DFB"
        parent_collection_id = b"\x00" * 32
        condition_id = b"\x12" * 32
        partition = [1, 2]
        amount = 100_000_000

        encoded = abi_encode(
            SPLIT_POSITION_TYPES,
            [collateral, parent_collection_id, condition_id, partition, amount],
        )
        decoded = abi_decode(SPLIT_POSITION_TYPES, encoded)
        assert decoded[0].lower() == collateral.lower()
        assert decoded[1] == parent_collection_id
        assert decoded[2] == condition_id
        assert list(decoded[3]) == partition
        assert decoded[4] == amount

    def test_merge_positions_encoding(self) -> None:
        """Verify parameter encoding and decoding for mergePositions."""
        assert MERGE_POSITIONS_TYPES == ["address", "bytes32", "bytes32", "uint256[]", "uint256"]
        collateral = "0xC011a7E12a19f7B1f670d46F03B03f3342E82DFB"
        parent_collection_id = b"\x00" * 32
        condition_id = b"\xab" * 32
        partition = [1, 2]
        amount = 50_000_000

        encoded = abi_encode(
            MERGE_POSITIONS_TYPES,
            [collateral, parent_collection_id, condition_id, partition, amount],
        )
        decoded = abi_decode(MERGE_POSITIONS_TYPES, encoded)
        assert decoded[0].lower() == collateral.lower()
        assert decoded[1] == parent_collection_id
        assert decoded[2] == condition_id
        assert list(decoded[3]) == partition
        assert decoded[4] == amount

    def test_redeem_positions_encoding(self) -> None:
        """Verify parameter encoding and decoding for redeemPositions."""
        assert REDEEM_POSITIONS_TYPES == ["address", "bytes32", "bytes32", "uint256[]"]
        collateral = "0xC011a7E12a19f7B1f670d46F03B03f3342E82DFB"
        parent_collection_id = b"\x00" * 32
        condition_id = b"\xcd" * 32
        index_sets = [1, 2]

        encoded = abi_encode(
            REDEEM_POSITIONS_TYPES,
            [collateral, parent_collection_id, condition_id, index_sets],
        )
        decoded = abi_decode(REDEEM_POSITIONS_TYPES, encoded)
        assert decoded[0].lower() == collateral.lower()
        assert decoded[1] == parent_collection_id
        assert decoded[2] == condition_id
        assert list(decoded[3]) == index_sets

    def test_convert_positions_encoding(self) -> None:
        """Verify parameter encoding and decoding for convertPositions."""
        assert CONVERT_POSITIONS_TYPES == ["bytes32", "uint256", "uint256"]
        market_id = b"\x42" * 32
        index_set = 1
        amount = 25_000_000

        encoded = abi_encode(
            CONVERT_POSITIONS_TYPES,
            [market_id, index_set, amount],
        )
        decoded = abi_decode(CONVERT_POSITIONS_TYPES, encoded)
        assert decoded[0] == market_id
        assert decoded[1] == index_set
        assert decoded[2] == amount

    def test_erc20_and_erc1155_types(self) -> None:
        """Verify ERC20 and ERC1155 balance/allowance types round-trip."""
        assert ERC20_ALLOWANCE_TYPES == ["address", "address"]
        assert ERC20_APPROVE_TYPES == ["address", "uint256"]
        assert ERC20_BALANCE_OF_TYPES == ["address"]
        owner = "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045"
        spender = "0xd91E80cF2E7be2e162c6513ceD06f1dD0dA35296"

        encoded_allowance = abi_encode(ERC20_ALLOWANCE_TYPES, [owner, spender])
        decoded_allowance = abi_decode(ERC20_ALLOWANCE_TYPES, encoded_allowance)
        assert decoded_allowance[0].lower() == owner.lower()
        assert decoded_allowance[1].lower() == spender.lower()

        encoded_approve = abi_encode(ERC20_APPROVE_TYPES, [spender, 1_000_000])
        decoded_approve = abi_decode(ERC20_APPROVE_TYPES, encoded_approve)
        assert decoded_approve[0].lower() == spender.lower()
        assert decoded_approve[1] == 1_000_000

        encoded_bal20 = abi_encode(ERC20_BALANCE_OF_TYPES, [owner])
        decoded_bal20 = abi_decode(ERC20_BALANCE_OF_TYPES, encoded_bal20)
        assert decoded_bal20[0].lower() == owner.lower()

        encoded_bal1155 = abi_encode(ERC1155_BALANCE_OF_TYPES, [owner, 123456789])
        decoded_bal1155 = abi_decode(ERC1155_BALANCE_OF_TYPES, encoded_bal1155)
        assert decoded_bal1155[0].lower() == owner.lower()
        assert decoded_bal1155[1] == 123456789
