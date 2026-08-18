"""Sports / mention skip for live Kalshi selection."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from kalshi_policy import event_blocked as _event_blocked


def test_sports_category_blocked():
    assert _event_blocked({"category": "Sports", "event_ticker": "KXFOO"})


def test_mention_ticker_blocked():
    assert _event_blocked({"category": "Economics", "event_ticker": "KXEARNINGSMENTIONBA"})


def test_conf_series_not_blocked_as_f1():
    assert not _event_blocked({"category": "Economics", "event_ticker": "KXCONF1"})


def test_fed_allowed():
    assert not _event_blocked({"category": "Economics", "event_ticker": "KXFED"})
