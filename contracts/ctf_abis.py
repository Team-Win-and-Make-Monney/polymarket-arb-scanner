"""Polymarket Conditional Token Framework (CTF) and NegRiskAdapter ABIs & selectors.

Contains the canonical Solidity function signatures, 4-byte selectors, and ABI
type definitions for Gnosis ConditionalTokens, NegRiskAdapter, and ERC-20/1155 tokens.

References:
- Gnosis Conditional Tokens:
    splitPosition(address collateralToken, bytes32 parentCollectionId, bytes32 conditionId, uint256[] partition, uint256 amount)
    mergePositions(address collateralToken, bytes32 parentCollectionId, bytes32 conditionId, uint256[] partition, uint256 amount)
    redeemPositions(address collateralToken, bytes32 parentCollectionId, bytes32 conditionId, uint256[] indexSets)
- Polymarket NegRiskAdapter:
    convertPositions(bytes32 _marketId, uint256 _indexSet, uint256 _amount)
- Polygon mainnet contract addresses (configured in config.py):
    ConditionalTokens, NegRiskAdapter, Collateral (USDC.e)
"""

from __future__ import annotations

from eth_utils import keccak


def _calc_selector(signature: str) -> str:
    """Return the 4-byte Solidity function selector as a 0x-prefixed hex string."""
    return "0x" + keccak(text=signature)[:4].hex()


# ---------------------------------------------------------------------------
# Canonical Solidity Function Signatures
# ---------------------------------------------------------------------------

SPLIT_POSITION_SIG = "splitPosition(address,bytes32,bytes32,uint256[],uint256)"
MERGE_POSITIONS_SIG = "mergePositions(address,bytes32,bytes32,uint256[],uint256)"
REDEEM_POSITIONS_SIG = "redeemPositions(address,bytes32,bytes32,uint256[])"
CONVERT_POSITIONS_SIG = "convertPositions(bytes32,uint256,uint256)"

ERC20_APPROVE_SIG = "approve(address,uint256)"
ERC20_ALLOWANCE_SIG = "allowance(address,address)"
ERC20_BALANCE_OF_SIG = "balanceOf(address)"

ERC1155_BALANCE_OF_SIG = "balanceOf(address,uint256)"
ERC1155_IS_APPROVED_FOR_ALL_SIG = "isApprovedForAll(address,address)"
ERC1155_SET_APPROVAL_FOR_ALL_SIG = "setApprovalForAll(address,bool)"


# ---------------------------------------------------------------------------
# Computed 4-byte Selectors
# ---------------------------------------------------------------------------

SPLIT_POSITION_SELECTOR = _calc_selector(SPLIT_POSITION_SIG)          # 0x72ce4275
MERGE_POSITIONS_SELECTOR = _calc_selector(MERGE_POSITIONS_SIG)        # 0x9e7212ad
REDEEM_POSITIONS_SELECTOR = _calc_selector(REDEEM_POSITIONS_SIG)      # 0x01b7037c
CONVERT_POSITIONS_SELECTOR = _calc_selector(CONVERT_POSITIONS_SIG)    # 0xc64748c4

ERC20_APPROVE_SELECTOR = _calc_selector(ERC20_APPROVE_SIG)            # 0x095ea7b3
ERC20_ALLOWANCE_SELECTOR = _calc_selector(ERC20_ALLOWANCE_SIG)        # 0xdd62ed3e
ERC20_BALANCE_OF_SELECTOR = _calc_selector(ERC20_BALANCE_OF_SIG)      # 0x70a08231

ERC1155_BALANCE_OF_SELECTOR = _calc_selector(ERC1155_BALANCE_OF_SIG)  # 0x00fdd58e
ERC1155_IS_APPROVED_FOR_ALL_SELECTOR = _calc_selector(ERC1155_IS_APPROVED_FOR_ALL_SIG)  # 0xe985e9c5
ERC1155_SET_APPROVAL_FOR_ALL_SELECTOR = _calc_selector(ERC1155_SET_APPROVAL_FOR_ALL_SIG)  # 0xa22cb465


# ---------------------------------------------------------------------------
# ABI Parameter Type Lists (for eth_abi.encode / eth_abi.decode)
# ---------------------------------------------------------------------------

SPLIT_POSITION_TYPES: list[str] = ["address", "bytes32", "bytes32", "uint256[]", "uint256"]
MERGE_POSITIONS_TYPES: list[str] = ["address", "bytes32", "bytes32", "uint256[]", "uint256"]
REDEEM_POSITIONS_TYPES: list[str] = ["address", "bytes32", "bytes32", "uint256[]"]
CONVERT_POSITIONS_TYPES: list[str] = ["bytes32", "uint256", "uint256"]

ERC20_APPROVE_TYPES: list[str] = ["address", "uint256"]
ERC20_ALLOWANCE_TYPES: list[str] = ["address", "address"]
ERC20_BALANCE_OF_TYPES: list[str] = ["address"]

ERC1155_BALANCE_OF_TYPES: list[str] = ["address", "uint256"]
ERC1155_IS_APPROVED_FOR_ALL_TYPES: list[str] = ["address", "address"]
ERC1155_SET_APPROVAL_FOR_ALL_TYPES: list[str] = ["address", "bool"]


# ---------------------------------------------------------------------------
# Minimal ABI JSON Fragments
# ---------------------------------------------------------------------------

CONDITIONAL_TOKENS_ABI = [
    {
        "constant": False,
        "inputs": [
            {"name": "collateralToken", "type": "address"},
            {"name": "parentCollectionId", "type": "bytes32"},
            {"name": "conditionId", "type": "bytes32"},
            {"name": "partition", "type": "uint256[]"},
            {"name": "amount", "type": "uint256"},
        ],
        "name": "splitPosition",
        "outputs": [],
        "payable": False,
        "stateMutability": "nonpayable",
        "type": "function",
    },
    {
        "constant": False,
        "inputs": [
            {"name": "collateralToken", "type": "address"},
            {"name": "parentCollectionId", "type": "bytes32"},
            {"name": "conditionId", "type": "bytes32"},
            {"name": "partition", "type": "uint256[]"},
            {"name": "amount", "type": "uint256"},
        ],
        "name": "mergePositions",
        "outputs": [],
        "payable": False,
        "stateMutability": "nonpayable",
        "type": "function",
    },
    {
        "constant": False,
        "inputs": [
            {"name": "collateralToken", "type": "address"},
            {"name": "parentCollectionId", "type": "bytes32"},
            {"name": "conditionId", "type": "bytes32"},
            {"name": "indexSets", "type": "uint256[]"},
        ],
        "name": "redeemPositions",
        "outputs": [],
        "payable": False,
        "stateMutability": "nonpayable",
        "type": "function",
    },
    {
        "constant": True,
        "inputs": [
            {"name": "owner", "type": "address"},
            {"name": "id", "type": "uint256"},
        ],
        "name": "balanceOf",
        "outputs": [{"name": "", "type": "uint256"}],
        "payable": False,
        "stateMutability": "view",
        "type": "function",
    },
]

NEG_RISK_ADAPTER_ABI = [
    {
        "constant": False,
        "inputs": [
            {"name": "_marketId", "type": "bytes32"},
            {"name": "_indexSet", "type": "uint256"},
            {"name": "_amount", "type": "uint256"},
        ],
        "name": "convertPositions",
        "outputs": [],
        "payable": False,
        "stateMutability": "nonpayable",
        "type": "function",
    },
]

ERC20_ABI = [
    {
        "constant": True,
        "inputs": [{"name": "account", "type": "address"}],
        "name": "balanceOf",
        "outputs": [{"name": "", "type": "uint256"}],
        "payable": False,
        "stateMutability": "view",
        "type": "function",
    },
    {
        "constant": True,
        "inputs": [
            {"name": "owner", "type": "address"},
            {"name": "spender", "type": "address"},
        ],
        "name": "allowance",
        "outputs": [{"name": "", "type": "uint256"}],
        "payable": False,
        "stateMutability": "view",
        "type": "function",
    },
    {
        "constant": False,
        "inputs": [
            {"name": "spender", "type": "address"},
            {"name": "amount", "type": "uint256"},
        ],
        "name": "approve",
        "outputs": [{"name": "", "type": "bool"}],
        "payable": False,
        "stateMutability": "nonpayable",
        "type": "function",
    },
]
