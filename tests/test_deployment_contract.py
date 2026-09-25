"""Deployment defaults must preserve the parked, narrow research mission."""

from pathlib import Path


class TestRailwayEntrypoint:
    def test_continuous_worker_defaults_to_research_mode(self):
        dockerfile = (Path(__file__).resolve().parent.parent / "Dockerfile").read_text(encoding="utf-8")
        assert 'ENTRYPOINT ["python", "scanner.py", "--continuous", "--mode", "research"]' in dockerfile

    def test_research_mode_cannot_trade(self, monkeypatch):
        """The deployed mode is observational: every research gate needs DRY_RUN."""
        import config

        monkeypatch.setattr(config, "DRY_RUN", False)
        assert config.research_dry_run(config.RESEARCH_MODE) is False
        monkeypatch.setattr(config, "DRY_RUN", True)
        assert config.research_dry_run(config.RESEARCH_MODE) is True
        assert config.research_dry_run("all") is False

    def test_research_mode_enables_polymarket_unless_pinned(self, monkeypatch):
        """Research mode scans Polymarket unless the venue pin restricts it to Kalshi."""
        import config

        monkeypatch.setattr(config, "_PAPER_SCAN_VENUES", "")
        assert config.polymarket_scan_enabled(config.RESEARCH_MODE) is True

        monkeypatch.setattr(config, "_PAPER_SCAN_VENUES", "kalshi")
        assert config.polymarket_scan_enabled(config.RESEARCH_MODE) is False
