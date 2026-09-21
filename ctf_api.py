"""Polymarket Conditional Token Framework (CTF) Client.

Provides read, calldata encoding, simulation, and gas estimation for on-chain
interactions with Gnosis ConditionalTokens and Polymarket NegRiskAdapter contracts.

Phase 4a: Read-only + dry-run simulation mode. Calldata generation, balance checks,
and gas estimation are active; raw on-chain transaction broadcast (`send`) is
disabled with `NotImplementedError`.
"""

from __future__ import annotations

import logging
import os
from typing import Any

from eth_abi import decode as abi_decode, encode as abi_encode
from eth_account import Account
import requests

from config import (
    COLLATERAL_TOKEN_ADDRESS,
    CONDITIONAL_TOKENS_ADDRESS,
    NEG_RISK_ADAPTER_ADDRESS,
)
from contracts.ctf_abis import (
    CONVERT_POSITIONS_SELECTOR,
    CONVERT_POSITIONS_TYPES,
    ERC1155_BALANCE_OF_SELECTOR,
    ERC1155_BALANCE_OF_TYPES,
    ERC20_ALLOWANCE_SELECTOR,
    ERC20_ALLOWANCE_TYPES,
    ERC20_BALANCE_OF_SELECTOR,
    ERC20_BALANCE_OF_TYPES,
    MERGE_POSITIONS_SELECTOR,
    MERGE_POSITIONS_TYPES,
    REDEEM_POSITIONS_SELECTOR,
    REDEEM_POSITIONS_TYPES,
    SPLIT_POSITION_SELECTOR,
    SPLIT_POSITION_TYPES,
)
from url_guard import assert_public_url

logger = logging.getLogger(__name__)

# Scaling factor for 6-decimal collateral tokens (USDC / pUSD)
COLLATERAL_SCALE = 1_000_000

# Default gas limits for CTF multi-token operations
DEFAULT_CTF_GAS_LIMIT = 150_000


def _to_bytes32(hex_or_bytes: str | bytes) -> bytes:
    """Normalize a condition ID or market ID into exact 32 bytes."""
    if isinstance(hex_or_bytes, bytes):
        if len(hex_or_bytes) == 32:
            return hex_or_bytes
        raise ValueError(f"Bytes value must be exactly 32 bytes, got {len(hex_or_bytes)}")
    clean = hex_or_bytes.strip()
    if clean.startswith("0x"):
        clean = clean[2:]
    clean = clean.zfill(64)
    if len(clean) != 64:
        raise ValueError(f"Hex string cannot be normalized to 32 bytes: {hex_or_bytes!r}")
    return bytes.fromhex(clean)


class CTFClient:
    """Client for reading balances, generating calldata, and estimating gas for CTF operations."""

    def __init__(
        self,
        rpc_url: str | None = None,
        private_key: str | None = None,
        conditional_tokens: str | None = None,
        neg_risk_adapter: str | None = None,
        collateral_token: str | None = None,
        dry_run: bool = True,
        request_timeout: float = 10.0,
    ):
        """Initialize the CTFClient.

        Args:
            rpc_url: Polygon JSON-RPC endpoint.
            private_key: Optional private key for signing / sender identification.
            conditional_tokens: Address of the ConditionalTokens contract.
            neg_risk_adapter: Address of the NegRiskAdapter contract.
            collateral_token: Address of the collateral ERC-20 token (pUSD/USDC).
            dry_run: If True, operates in simulation mode.
            request_timeout: Timeout in seconds for RPC HTTP requests.
        """
        raw_rpc = rpc_url or os.getenv("POLYGON_RPC_URL", "https://polygon-rpc.com")
        self.rpc_url = assert_public_url(raw_rpc, env_name="POLYGON_RPC_URL")

        self.conditional_tokens = conditional_tokens or CONDITIONAL_TOKENS_ADDRESS
        self.neg_risk_adapter = neg_risk_adapter or NEG_RISK_ADAPTER_ADDRESS
        self.collateral_token = collateral_token or COLLATERAL_TOKEN_ADDRESS

        self.dry_run = dry_run
        self.request_timeout = request_timeout

        self._private_key = private_key or os.getenv("POLYMARKET_PRIVATE_KEY")
        self.account = None
        self.address = None
        if self._private_key:
            try:
                self.account = Account.from_key(self._private_key)
                self.address = self.account.address
            except Exception as e:
                logger.debug("Failed to derive account from private key: %s", e)

    # -----------------------------------------------------------------------
    # JSON-RPC Transport
    # -----------------------------------------------------------------------

    def _rpc_call(self, method: str, params: list[Any]) -> Any:
        """Execute a JSON-RPC call against the Polygon node."""
        payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": method,
            "params": params,
        }
        resp = requests.post(
            self.rpc_url,
            json=payload,
            headers={"Content-Type": "application/json"},
            timeout=self.request_timeout,
        )
        resp.raise_for_status()
        data = resp.json()
        if "error" in data:
            raise RuntimeError(f"RPC error: {data['error']}")
        return data.get("result")

    # -----------------------------------------------------------------------
    # Reads
    # -----------------------------------------------------------------------

    def collateral_balance(self, account: str | None = None) -> float:
        """Get the ERC-20 collateral balance in dollar terms (6 decimals)."""
        target = account or self.address
        if not target or not self.collateral_token:
            return 0.0

        calldata = ERC20_BALANCE_OF_SELECTOR + abi_encode(
            ERC20_BALANCE_OF_TYPES, [target]
        ).hex()

        try:
            result_hex = self._rpc_call("eth_call", [{"to": self.collateral_token, "data": calldata}, "latest"])
            if not result_hex or result_hex == "0x":
                return 0.0
            (balance_units,) = abi_decode(["uint256"], bytes.fromhex(result_hex[2:]))
            return float(balance_units) / COLLATERAL_SCALE
        except Exception as e:
            logger.warning("Failed to fetch collateral balance for %s: %s", target, e)
            return 0.0

    def position_balance(self, token_id: int | str, account: str | None = None) -> float:
        """Get the ERC-1155 position balance for a token ID in contracts/shares."""
        target = account or self.address
        if not target or not self.conditional_tokens:
            return 0.0

        try:
            tid_int = int(token_id) if isinstance(token_id, str) and not token_id.startswith("0x") else int(token_id, 16) if isinstance(token_id, str) else int(token_id)
        except (ValueError, TypeError):
            logger.warning("Invalid token_id: %r", token_id)
            return 0.0

        calldata = ERC1155_BALANCE_OF_SELECTOR + abi_encode(
            ERC1155_BALANCE_OF_TYPES, [target, tid_int]
        ).hex()

        try:
            result_hex = self._rpc_call("eth_call", [{"to": self.conditional_tokens, "data": calldata}, "latest"])
            if not result_hex or result_hex == "0x":
                return 0.0
            (balance_units,) = abi_decode(["uint256"], bytes.fromhex(result_hex[2:]))
            return float(balance_units) / COLLATERAL_SCALE
        except Exception as e:
            logger.warning("Failed to fetch position balance for %s, token %s: %s", target, token_id, e)
            return 0.0

    def get_allowance(self, spender: str, owner: str | None = None) -> float:
        """Get the ERC-20 collateral allowance for a spender in dollar terms."""
        target_owner = owner or self.address
        if not target_owner or not spender or not self.collateral_token:
            return 0.0

        calldata = ERC20_ALLOWANCE_SELECTOR + abi_encode(
            ERC20_ALLOWANCE_TYPES, [target_owner, spender]
        ).hex()

        try:
            result_hex = self._rpc_call("eth_call", [{"to": self.collateral_token, "data": calldata}, "latest"])
            if not result_hex or result_hex == "0x":
                return 0.0
            (allowance_units,) = abi_decode(["uint256"], bytes.fromhex(result_hex[2:]))
            return float(allowance_units) / COLLATERAL_SCALE
        except Exception as e:
            logger.warning("Failed to fetch allowance for spender %s: %s", spender, e)
            return 0.0

    def ensure_allowance(self, spender: str, min_amount: float) -> bool:
        """Check if current collateral allowance meets the required amount."""
        current = self.get_allowance(spender)
        return current >= min_amount

    # -----------------------------------------------------------------------
    # Calldata Construction & Simulation
    # -----------------------------------------------------------------------

    def build_split(
        self,
        condition_id: str,
        amount_collateral: float,
        partition: list[int] | None = None,
    ) -> dict[str, Any]:
        """Build calldata for ConditionalTokens.splitPosition().

        Args:
            condition_id: 32-byte hex condition identifier.
            amount_collateral: Dollar amount of collateral to split.
            partition: Outcome partition list (default [1, 2] for binary YES/NO).

        Returns:
            Dictionary with transaction call parameters.
        """
        cond_bytes = _to_bytes32(condition_id)
        part = partition or [1, 2]
        amount_units = int(round(amount_collateral * COLLATERAL_SCALE))
        zero_bytes32 = b"\x00" * 32

        encoded_args = abi_encode(
            SPLIT_POSITION_TYPES,
            [self.collateral_token, zero_bytes32, cond_bytes, part, amount_units],
        )
        calldata = SPLIT_POSITION_SELECTOR + encoded_args.hex()

        return {
            "to": self.conditional_tokens,
            "data": calldata,
            "value": "0x0",
            "condition_id": condition_id,
            "amount": amount_collateral,
            "action": "split",
            "gas_estimate": DEFAULT_CTF_GAS_LIMIT,
        }

    def build_merge(
        self,
        condition_id: str,
        amount: float,
        partition: list[int] | None = None,
    ) -> dict[str, Any]:
        """Build calldata for ConditionalTokens.mergePositions().

        Args:
            condition_id: 32-byte hex condition identifier.
            amount: Number of complete contract sets to merge into collateral.
            partition: Outcome partition list (default [1, 2] for binary YES/NO).

        Returns:
            Dictionary with transaction call parameters.
        """
        cond_bytes = _to_bytes32(condition_id)
        part = partition or [1, 2]
        amount_units = int(round(amount * COLLATERAL_SCALE))
        zero_bytes32 = b"\x00" * 32

        encoded_args = abi_encode(
            MERGE_POSITIONS_TYPES,
            [self.collateral_token, zero_bytes32, cond_bytes, part, amount_units],
        )
        calldata = MERGE_POSITIONS_SELECTOR + encoded_args.hex()

        return {
            "to": self.conditional_tokens,
            "data": calldata,
            "value": "0x0",
            "condition_id": condition_id,
            "amount": amount,
            "action": "merge",
            "gas_estimate": DEFAULT_CTF_GAS_LIMIT,
        }

    def build_redeem(
        self,
        condition_id: str,
        index_sets: list[int] | None = None,
    ) -> dict[str, Any]:
        """Build calldata for ConditionalTokens.redeemPositions().

        Args:
            condition_id: 32-byte hex condition identifier.
            index_sets: List of outcome index sets to redeem (default [1, 2]).

        Returns:
            Dictionary with transaction call parameters.
        """
        cond_bytes = _to_bytes32(condition_id)
        sets = index_sets or [1, 2]
        zero_bytes32 = b"\x00" * 32

        encoded_args = abi_encode(
            REDEEM_POSITIONS_TYPES,
            [self.collateral_token, zero_bytes32, cond_bytes, sets],
        )
        calldata = REDEEM_POSITIONS_SELECTOR + encoded_args.hex()

        return {
            "to": self.conditional_tokens,
            "data": calldata,
            "value": "0x0",
            "condition_id": condition_id,
            "action": "redeem",
            "gas_estimate": DEFAULT_CTF_GAS_LIMIT,
        }

    def build_convert(
        self,
        market_id: str,
        index_set: int,
        amount: float,
    ) -> dict[str, Any]:
        """Build calldata for NegRiskAdapter.convertPositions().

        Args:
            market_id: 32-byte hex market identifier.
            index_set: Bitmask of outcomes being converted.
            amount: Number of positions to convert.

        Returns:
            Dictionary with transaction call parameters.
        """
        mkt_bytes = _to_bytes32(market_id)
        amount_units = int(round(amount * COLLATERAL_SCALE))

        encoded_args = abi_encode(
            CONVERT_POSITIONS_TYPES,
            [mkt_bytes, int(index_set), amount_units],
        )
        calldata = CONVERT_POSITIONS_SELECTOR + encoded_args.hex()

        return {
            "to": self.neg_risk_adapter,
            "data": calldata,
            "value": "0x0",
            "market_id": market_id,
            "index_set": index_set,
            "amount": amount,
            "action": "convert",
            "gas_estimate": DEFAULT_CTF_GAS_LIMIT,
        }

    def estimate_gas(self, tx_dict: dict[str, Any]) -> int:
        """Estimate gas units needed for the transaction via eth_estimateGas."""
        call_obj: dict[str, Any] = {
            "to": tx_dict.get("to"),
            "data": tx_dict.get("data"),
        }
        if self.address:
            call_obj["from"] = self.address

        try:
            result_hex = self._rpc_call("eth_estimateGas", [call_obj])
            if result_hex:
                return int(result_hex, 16)
        except Exception as e:
            logger.debug("eth_estimateGas failed; using default estimate: %s", e)

        return tx_dict.get("gas_estimate", DEFAULT_CTF_GAS_LIMIT)

    # -----------------------------------------------------------------------
    # Broadcast Guard (Phase 4a: Disabled)
    # -----------------------------------------------------------------------

    def send(self, built_tx: dict[str, Any]) -> str | None:
        """Sign and broadcast a transaction.

        In Phase 4a, on-chain execution is intentionally gated.
        Raises:
            NotImplementedError: Always raised in Phase 4a.
        """
        raise NotImplementedError(
            "CTF on-chain execution is disabled in Phase 4a (read-only and simulation mode only)."
        )
