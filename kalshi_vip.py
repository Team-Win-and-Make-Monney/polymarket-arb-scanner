"""Kalshi Volume Incentive Program (VIP) tracking.

Deterministic accounting of VIP-eligible trading volume from account fills
(https://help.kalshi.com/en/articles/13823850-what-is-the-kalshi-volume-incentive-program).

Program mechanics implemented here:
  * VIP pays a pro-rata share of a per-market reward pool based on your share
    of qualifying volume, capped at ``$0.005`` per contract traded.
  * Only contracts traded at a price in ``[$0.03, $0.97]`` qualify.

No network, no auth, no LLM here — the tracker reads fills through an injected
Kalshi client and reduces them to deterministic counts and a reward estimate.
"""

from __future__ import annotations

import logging
import threading

logger = logging.getLogger(__name__)

# VIP program parameters (doc 06 / Kalshi help article).
ELIGIBLE_PRICE_MIN = 0.03
ELIGIBLE_PRICE_MAX = 0.97
VIP_PER_CONTRACT_CAP = 0.005


def fill_price_dollars(fill: dict) -> float | None:
    """Derive a fill's contract price in dollars from a Kalshi fill record.

    Kalshi fills carry integer-cent ``yes_price``/``no_price``. The yes price is
    used as the canonical contract price; eligibility bounds are symmetric about
    $0.50, so the yes price alone determines eligibility.

    Args:
        fill: A Kalshi fill record.

    Returns:
        Price in dollars (0-1), or None if no price field is present.
    """
    for field in ('yes_price', 'no_price'):
        raw = fill.get(field)
        if raw is None:
            continue
        try:
            cents = float(raw)
        except (TypeError, ValueError):
            logger.warning('Kalshi fill has non-numeric %s=%r', field, raw)
            continue
        price = cents / 100.0
        # no_price is the complement; convert back to the yes price for consistency.
        return price if field == 'yes_price' else 1.0 - price
    return None


def is_eligible_fill(fill: dict) -> bool:
    """Return True if a fill traded at a VIP-eligible price ($0.03-$0.97)."""
    price = fill_price_dollars(fill)
    if price is None:
        return False
    return ELIGIBLE_PRICE_MIN <= price <= ELIGIBLE_PRICE_MAX


def _fill_count(fill: dict) -> int:
    """Extract the contract count from a fill, defaulting to 0 on bad data.

    Negative counts are clamped to 0 so a bad record can never push the
    eligible/ineligible totals below zero.
    """
    raw = fill.get('count', 0)
    try:
        count = int(raw)
    except (TypeError, ValueError):
        logger.warning('Kalshi fill has non-numeric count=%r', raw)
        return 0
    if count < 0:
        logger.warning('Kalshi fill has negative count=%r; clamping to 0', raw)
        return 0
    return count


def count_eligible_contracts(fills: list[dict]) -> int:
    """Sum contract counts across VIP-eligible fills."""
    return sum(_fill_count(f) for f in fills if is_eligible_fill(f))


def estimate_vip_reward(your_contracts: int, total_contracts: int, reward_pool: float) -> float:
    """Estimate VIP reward as the smaller of pro-rata share and the per-contract cap.

    Reward = ``min((your_contracts / total_contracts) * pool, your_contracts * $0.005)``.

    Args:
        your_contracts: Your VIP-eligible contracts traded in the period.
        total_contracts: Total VIP-eligible contracts across all participants.
        reward_pool: Period reward pool in dollars.

    Returns:
        Estimated reward in dollars (0.0 if inputs are non-positive).
    """
    if your_contracts <= 0 or total_contracts <= 0 or reward_pool <= 0:
        return 0.0
    pro_rata = (your_contracts / total_contracts) * reward_pool
    cap = your_contracts * VIP_PER_CONTRACT_CAP
    return max(0.0, min(pro_rata, cap))


class KalshiVipTracker:
    """Aggregate VIP-eligible volume from account fills.

    Thread-safe. Reads fills through an injected client exposing ``get_fills``;
    persistence to the shared P&L layer happens downstream (Phase 2 sync).
    """

    def __init__(self, kalshi_client=None):
        """Initialise the tracker.

        Args:
            kalshi_client: Object exposing ``get_fills(min_ts=...) -> list[dict]``.
        """
        self._client = kalshi_client
        self._lock = threading.Lock()
        # Monotonic timestamp of the last poll; used by continuous-mode throttling.
        self.last_poll_ts = 0.0

    def summarize_fills(self, fills: list[dict]) -> dict:
        """Reduce a list of fills to VIP volume metrics.

        Args:
            fills: Kalshi fill records.

        Returns:
            Dict with eligible_contracts, ineligible_contracts, total_fills,
            and reward_cap_usd (the maximum possible VIP accrual at $0.005/contract).
        """
        eligible = 0
        ineligible = 0
        for fill in fills:
            count = _fill_count(fill)
            if is_eligible_fill(fill):
                eligible += count
            else:
                ineligible += count
        return {
            'eligible_contracts': eligible,
            'ineligible_contracts': ineligible,
            'total_fills': len(fills),
            'reward_cap_usd': round(eligible * VIP_PER_CONTRACT_CAP, 4),
        }

    def summarize_since(self, min_ts: int | None = None) -> dict:
        """Pull fills from the client and summarise VIP volume.

        Args:
            min_ts: Optional Unix-seconds lower bound passed to ``get_fills``.

        Returns:
            Summary dict from :meth:`summarize_fills`.

        Raises:
            RuntimeError: If no client was provided.
        """
        if self._client is None:
            raise RuntimeError('KalshiVipTracker has no client to fetch fills')
        with self._lock:
            fills = self._client.get_fills(min_ts=min_ts)
        return self.summarize_fills(fills)
