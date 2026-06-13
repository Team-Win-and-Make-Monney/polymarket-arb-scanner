"""Tests for the read-only rewards platform catalog."""

import datetime as dt
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts import rewards_platform_catalog as catalog


class TestRewardsPlatformCatalog:
    def test_catalog_includes_core_platforms(self):
        """Catalog should cover the primary rewards surfaces researched."""
        records = catalog.platform_catalog()
        keys = {(row["platform"], row["program"]) for row in records}

        assert ("Kalshi", "Liquidity Incentive Program") in keys
        assert ("Polymarket", "Global Liquidity Rewards") in keys
        assert ("Polymarket US", "Liquidity Incentive Program") in keys
        assert ("Merkl", "Live DeFi Campaigns") in keys
        assert ("dYdX", "Rewards Directory and Surge") in keys
        assert ("Hyperliquid", "Maker Rebates, Fee Tiers, Staking Discounts, Referrals") in keys
        assert ("Interactive Brokers", "Stock Yield Enhancement Program") in keys

    def test_all_records_block_autonomous_capture(self):
        """Every live financial capture path must stay behind manual approval."""
        records = catalog.platform_catalog()

        assert all(row["can_codex_capture"] == "No" for row in records)
        assert all(row["why_not_autonomous"] for row in records)
        assert "No unattended trading" in catalog.SAFETY_BOUNDARY

    def test_ranking_prioritizes_monitorable_primary_sources(self):
        """Public API and primary docs should rank ahead of speculative watchlists."""
        ranked = catalog.ranked_catalog(catalog.platform_catalog())

        assert ranked[0]["source_status"] == "primary_verified"
        assert ranked[0]["monitorability"] in {"public_api", "public_docs"}
        assert ranked[-1]["source_status"] == "secondary_needs_verification"

    def test_render_digest_explains_boundary_and_sources(self):
        """Digest should explain what can be automated and cite official sources."""
        now = dt.datetime(2026, 6, 13, 12, 0, tzinfo=dt.timezone.utc)
        digest = catalog.render_digest(catalog.platform_catalog(), now, limit=5)

        assert "# Market Rewards Platform Catalog" in digest
        assert "Safety boundary:" in digest
        assert "Can Codex capture?" in digest
        assert "https://help.kalshi.com/en/articles/13823851-liquidity-incentive-program" in digest
        assert "https://docs.merkl.xyz/merkl-mechanisms/incentive-mechanisms" in digest

    def test_write_csv_includes_safety_columns(self, tmp_path):
        """CSV should preserve the decision fields needed for automation gates."""
        output = tmp_path / "catalog.csv"
        catalog.write_csv(catalog.platform_catalog(), output)

        text = output.read_text(encoding="utf-8")
        assert "can_codex_capture" in text
        assert "why_not_autonomous" in text
        assert "No" in text
