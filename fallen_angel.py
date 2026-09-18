"""Fallen-angel detection core — IG→HY rating-crossover classifier (ETF-sleeve trigger).

A "fallen angel" is an issuer/bond downgraded from investment grade (IG) to high
yield (HY). IG-mandated funds are forced to sell, creating a temporary discount —
the premium the ~$5-10K ETF sleeve harvests (deploy ONLY if the downgrade-wave
watcher fires). This core classifies a rating change as a fallen angel (or not),
across S&P / Fitch (shared scale) and Moody's.

The IG/HY boundary: S&P/Fitch ``BBB-`` (lowest IG) | ``BB+`` (highest HY);
Moody's ``Baa3`` | ``Ba1``. Pure + deterministic — the live downgrade feed is a
Phase-2 data pipeline.
"""
from __future__ import annotations

from dataclasses import dataclass

# Ordinal credit rank — better credit = lower index. S&P/Fitch and Moody's notches
# share the same rank space (BBB- and Baa3 are both rank 9, the IG floor).
_SP_FITCH = [
    "AAA", "AA+", "AA", "AA-", "A+", "A", "A-", "BBB+", "BBB", "BBB-",
    "BB+", "BB", "BB-", "B+", "B", "B-", "CCC+", "CCC", "CCC-", "CC", "C", "D",
]
_MOODYS = [
    "AAA", "AA1", "AA2", "AA3", "A1", "A2", "A3", "BAA1", "BAA2", "BAA3",
    "BA1", "BA2", "BA3", "B1", "B2", "B3", "CAA1", "CAA2", "CAA3", "CA", "C", "D",
]

_RANK: dict[str, int] = {}
for _scale in (_SP_FITCH, _MOODYS):
    for _i, _notch in enumerate(_scale):
        _RANK[_notch] = _i

_IG_FLOOR = 9  # BBB- / Baa3 — the lowest investment-grade notch


@dataclass(frozen=True)
class RatingChange:
    issuer: str
    from_rating: str
    to_rating: str
    agency: str = ""
    date: str = ""


@dataclass(frozen=True)
class FallenAngelEvent:
    issuer: str
    from_rating: str
    to_rating: str
    notches: int          # how many notches the downgrade moved
    note: str


def rating_rank(rating: str) -> int | None:
    """Ordinal rank for a rating notch (S&P/Fitch or Moody's).

    Args:
        rating: A rating notch such as "BBB-" or "Baa3" (case/space-insensitive).

    Returns:
        The ordinal rank (lower = better credit), or None if the notch is unknown.
    """
    return _RANK.get((rating or "").strip().upper())


def is_investment_grade(rating: str) -> bool:
    """Whether a rating notch is investment grade.

    Args:
        rating: A rating notch (S&P/Fitch or Moody's).

    Returns:
        True iff the rating is BBB-/Baa3 or better (a known IG notch).
    """
    rank = rating_rank(rating)
    return rank is not None and rank <= _IG_FLOOR


def is_fallen_angel(change: RatingChange) -> bool:
    """Whether a rating change crosses investment grade into high yield.

    Args:
        change: The rating change to test.

    Returns:
        True iff the change crosses IG → HY (the forced-selling trigger).
    """
    f = rating_rank(change.from_rating)
    t = rating_rank(change.to_rating)
    if f is None or t is None:
        return False
    return f <= _IG_FLOOR < t


def is_rising_star(change: RatingChange) -> bool:
    """Whether a rating change crosses high yield into investment grade.

    Args:
        change: The rating change to test.

    Returns:
        True iff the change crosses HY → IG (informational; not the sleeve trigger).
    """
    f = rating_rank(change.from_rating)
    t = rating_rank(change.to_rating)
    if f is None or t is None:
        return False
    return t <= _IG_FLOOR < f


def classify_fallen_angel(change: RatingChange) -> FallenAngelEvent | None:
    """Classify a rating change as a fallen-angel event.

    Args:
        change: The rating change to classify.

    Returns:
        A FallenAngelEvent for an IG→HY crossover, else None.
    """
    if not is_fallen_angel(change):
        return None
    f = rating_rank(change.from_rating)
    t = rating_rank(change.to_rating)
    return FallenAngelEvent(
        issuer=change.issuer,
        from_rating=change.from_rating,
        to_rating=change.to_rating,
        notches=t - f,
        note=f"IG→HY crossover: {change.from_rating}→{change.to_rating} ({change.issuer})",
    )


def scan_rating_changes(changes: list[RatingChange]) -> list[FallenAngelEvent]:
    """Classify a batch of rating changes, keeping only fallen angels.

    Args:
        changes: Rating changes to classify.

    Returns:
        The FallenAngelEvent for each IG→HY crossover, in input order.
    """
    out: list[FallenAngelEvent] = []
    for c in changes:
        event = classify_fallen_angel(c)
        if event is not None:
            out.append(event)
    return out
