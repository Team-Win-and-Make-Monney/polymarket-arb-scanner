"""Unit tests for eip712_signer.py."""

from __future__ import annotations

import os
import sys
import pytest
from eth_account import Account

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from eip712_signer import (
    build_eip712_payload,
    normalize_private_key,
    recover_order_signer,
    sign_order,
)


class TestEIP712Signer:
    """Test suite for EIP-712 structured data signing and verification."""

    _SAMPLE_KEY = "0x" + "1" * 64

    @property
    def _sample_address(self) -> str:
        return Account.from_key(self._SAMPLE_KEY).address

    @property
    def _sample_domain(self) -> dict:
        return {
            "name": "Limitless Exchange",
            "version": "1",
            "chainId": 8453,
            "verifyingContract": "0x1111111111111111111111111111111111111111",
        }

    @property
    def _sample_order(self) -> dict:
        return {
            "maker": self._sample_address,
            "marketId": 12345,
            "price": 500000,
            "amount": 1000000,
            "side": 0,
            "nonce": 1,
        }

    def test_sign_order_recovers_expected_signer(self) -> None:
        """Verify that sign_order generates a signature that recovers the expected maker address."""
        sig = sign_order(self._sample_domain, self._sample_order, self._SAMPLE_KEY)
        assert sig.startswith("0x")
        assert len(sig) == 132  # 65 bytes in hex + '0x'

        recovered = recover_order_signer(self._sample_domain, self._sample_order, sig)
        assert recovered.lower() == self._sample_address.lower()

    def test_private_key_normalization_accepts_0x_and_raw(self) -> None:
        """Verify normalize_private_key handles keys with and without '0x' prefix."""
        raw_key = self._SAMPLE_KEY[2:]
        sig_prefixed = sign_order(self._sample_domain, self._sample_order, self._SAMPLE_KEY)
        sig_raw = sign_order(self._sample_domain, self._sample_order, raw_key)
        assert sig_prefixed == sig_raw

    def test_invalid_private_key_raises_valueerror(self) -> None:
        """Verify invalid or malformed private keys raise ValueError."""
        with pytest.raises(ValueError, match="Invalid private key"):
            normalize_private_key("short_key")

        with pytest.raises(ValueError, match="Invalid private key"):
            normalize_private_key("0x" + "z" * 64)

        with pytest.raises(ValueError, match="Invalid private key"):
            normalize_private_key("")

    def test_empty_domain_or_order_raises_valueerror(self) -> None:
        """Verify empty domain or order struct raises ValueError."""
        with pytest.raises(ValueError, match="domain cannot be empty"):
            build_eip712_payload({}, self._sample_order)

        with pytest.raises(ValueError, match="order struct cannot be empty"):
            build_eip712_payload(self._sample_domain, {})

    def test_custom_types_explicitly_used(self) -> None:
        """Verify explicit custom types mapping is respected."""
        custom_types = {
            "Order": [
                {"name": "maker", "type": "address"},
                {"name": "marketId", "type": "uint256"},
                {"name": "price", "type": "uint256"},
                {"name": "amount", "type": "uint256"},
                {"name": "side", "type": "uint8"},
                {"name": "nonce", "type": "uint256"},
            ]
        }
        sig = sign_order(
            self._sample_domain,
            self._sample_order,
            self._SAMPLE_KEY,
            primary_type="Order",
            types=custom_types,
        )
        recovered = recover_order_signer(
            self._sample_domain,
            self._sample_order,
            sig,
            primary_type="Order",
            types=custom_types,
        )
        assert recovered.lower() == self._sample_address.lower()

    def test_dynamic_type_inference(self) -> None:
        """Verify automatic type inference handles addresses, bytes32, ints, bools, and strings."""
        order = {
            "maker": "0x1111111111111111111111111111111111111111",
            "conditionId": "0x" + "2" * 64,
            "count": 100,
            "isActive": True,
            "label": "test_order",
        }
        payload = build_eip712_payload(self._sample_domain, order)
        order_fields = {f["name"]: f["type"] for f in payload["types"]["Order"]}
        assert order_fields["maker"] == "address"
        assert order_fields["conditionId"] == "bytes32"
        assert order_fields["count"] == "uint256"
        assert order_fields["isActive"] == "bool"
        assert order_fields["label"] == "string"
