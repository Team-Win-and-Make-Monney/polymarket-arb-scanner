"""Reusable EIP-712 typed-data order signer.

Provides standard EIP-712 message hashing, order signature generation,
and signer address recovery for off-chain order books (SX Bet, Limitless, Opinion).
"""

from __future__ import annotations

import logging
import re
from typing import Any

from eth_account import Account
from eth_account.messages import encode_typed_data

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Default EIP-712 Domain Types
# ---------------------------------------------------------------------------

_DOMAIN_FIELD_TYPES: dict[str, str] = {
    "name": "string",
    "version": "string",
    "chainId": "uint256",
    "verifyingContract": "address",
    "salt": "bytes32",
}


def _infer_type(value: Any) -> str:
    """Infer Solidity type string from Python value for dynamic EIP-712 schemas.

    Args:
        value: Python value to infer type from.

    Returns:
        Inferred Solidity type name.
    """
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, int):
        return "uint256"
    if isinstance(value, str):
        if re.fullmatch(r"0x[0-9a-fA-F]{40}", value):
            return "address"
        if re.fullmatch(r"0x[0-9a-fA-F]{64}", value):
            return "bytes32"
        return "string"
    if isinstance(value, bytes):
        return "bytes"
    return "string"


def build_eip712_payload(
    domain: dict[str, Any],
    order_struct: dict[str, Any],
    primary_type: str = "Order",
    types: dict[str, list[dict[str, str]]] | None = None,
) -> dict[str, Any]:
    """Construct an EIP-712 structured data dict conforming to eth_account spec.

    Args:
        domain: EIP-712 domain fields (name, version, chainId, verifyingContract).
        order_struct: The order data dictionary to sign.
        primary_type: Primary type name, defaults to 'Order'.
        types: Optional custom type definitions. If None, types will be
            inferred or constructed from default fields.

    Returns:
        Structured dictionary consumable by encode_typed_data(full_message=...).

    Raises:
        ValueError: If domain or order_struct is empty.
    """
    if not domain:
        raise ValueError("EIP-712 domain cannot be empty")
    if not order_struct:
        raise ValueError("EIP-712 order struct cannot be empty")

    # Build EIP712Domain type list dynamically based on provided domain keys
    domain_types = [
        {"name": key, "type": _DOMAIN_FIELD_TYPES.get(key, "string")}
        for key in domain.keys()
        if key in _DOMAIN_FIELD_TYPES
    ]

    all_types: dict[str, list[dict[str, str]]] = {
        "EIP712Domain": domain_types,
    }

    if types and primary_type in types:
        for k, v in types.items():
            all_types[k] = v
    else:
        # Infer types from order_struct keys
        inferred_types = [
            {"name": k, "type": _infer_type(v)}
            for k, v in order_struct.items()
        ]
        all_types[primary_type] = inferred_types

    return {
        "types": all_types,
        "primaryType": primary_type,
        "domain": domain,
        "message": order_struct,
    }


def normalize_private_key(private_key: str) -> str:
    """Normalize private key to standard 0x-prefixed 64-char hex string.

    Args:
        private_key: Private key string with or without 0x prefix.

    Returns:
        Normalized 0x-prefixed 66-character string.

    Raises:
        ValueError: If key is not a 32-byte hexadecimal string.
    """
    cleaned = private_key.strip()
    if cleaned.startswith("0x") or cleaned.startswith("0X"):
        cleaned = cleaned[2:]
    if len(cleaned) != 64 or not re.fullmatch(r"[0-9a-fA-F]{64}", cleaned):
        raise ValueError("Invalid private key: must be a 32-byte hex string (64 characters)")
    return "0x" + cleaned.lower()


def sign_order(
    domain: dict[str, Any],
    order_struct: dict[str, Any],
    private_key: str,
    primary_type: str = "Order",
    types: dict[str, list[dict[str, str]]] | None = None,
) -> str:
    """Return the 0x-prefixed signature for an EIP-712 typed order.

    Used by Limitless, SX Bet, and Opinion off-chain CLOB APIs.

    Args:
        domain: EIP-712 domain descriptor.
        order_struct: Order parameters dictionary.
        private_key: Signer's private key.
        primary_type: Name of the primary struct type.
        types: Explicit type definitions.

    Returns:
        Hex signature string starting with '0x'.
    """
    pk = normalize_private_key(private_key)
    payload = build_eip712_payload(domain, order_struct, primary_type=primary_type, types=types)
    signable = encode_typed_data(full_message=payload)
    sig = Account.sign_message(signable, private_key=pk).signature.hex()
    if not sig.startswith("0x"):
        sig = "0x" + sig
    return sig


def recover_order_signer(
    domain: dict[str, Any],
    order_struct: dict[str, Any],
    signature: str,
    primary_type: str = "Order",
    types: dict[str, list[dict[str, str]]] | None = None,
) -> str:
    """Recover the Ethereum address that produced the EIP-712 signature.

    Args:
        domain: EIP-712 domain descriptor.
        order_struct: Order parameters dictionary.
        signature: 0x-prefixed hex signature.
        primary_type: Name of the primary struct type.
        types: Explicit type definitions.

    Returns:
        Checksum Ethereum address of the signer.
    """
    sig = signature.strip()
    if not sig.startswith("0x"):
        sig = "0x" + sig
    payload = build_eip712_payload(domain, order_struct, primary_type=primary_type, types=types)
    signable = encode_typed_data(full_message=payload)
    return Account.recover_message(signable, signature=sig)
