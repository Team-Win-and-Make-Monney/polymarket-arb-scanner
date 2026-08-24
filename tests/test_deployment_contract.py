"""Deployment defaults must preserve the parked, narrow research mission."""

from pathlib import Path


class TestRailwayEntrypoint:
    def test_continuous_worker_defaults_to_kalshi_mode(self):
        dockerfile = (Path(__file__).resolve().parent.parent / "Dockerfile").read_text(encoding="utf-8")
        assert 'ENTRYPOINT ["python", "scanner.py", "--continuous", "--mode", "kalshi"]' in dockerfile
