"""Tests for the read-only rewards platform catalog."""

import argparse
import csv
import datetime as dt
import json
import sys
from pathlib import Path

import pytest

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

    def test_ranking_uses_verified_and_alphabetical_tie_breakers(self):
        """Descending score must not reverse the remaining sort keys."""
        records = [
            {
                "platform": platform,
                "program": "Program",
                "source_status": source_status,
                "monitorability": "public_docs",
                "capital_intensity": "low",
                "execution_risk": "low",
                "automation_mode": "watch_only",
                "category": "other",
            }
            for platform, source_status in (
                ("Zulu", "secondary_needs_verification"),
                ("Beta", "primary_verified"),
                ("Alpha", "primary_verified"),
            )
        ]

        ranked = catalog.ranked_catalog(records)

        assert [row["platform"] for row in ranked[:2]] == ["Alpha", "Beta"]
        assert ranked[-1]["source_status"] == "secondary_needs_verification"

    def test_render_digest_explains_boundary_and_sources(self):
        """Digest should explain what can be automated and cite official sources."""
        now = dt.datetime(2026, 6, 13, 12, 0, tzinfo=dt.timezone.utc)
        reviewed = dt.date(2026, 8, 5)
        digest = catalog.render_digest(
            catalog.platform_catalog(), now, limit=5, source_reviewed_at=reviewed
        )

        assert "# Market Rewards Platform Catalog" in digest
        assert "Safety boundary:" in digest
        assert "Official sources reviewed: 2026-08-05" in digest
        assert "Can Codex capture?" in digest
        assert "https://help.kalshi.com/en/articles/13823851-liquidity-incentive-program" in digest
        assert "https://docs.merkl.xyz/merkl-mechanisms/incentive-mechanisms" in digest

    def test_write_csv_includes_safety_columns(self, tmp_path):
        """CSV should preserve the decision fields needed for automation gates."""
        output = tmp_path / "catalog.csv"
        catalog.write_csv(catalog.platform_catalog(), output)

        with output.open(newline="", encoding="utf-8") as f:
            rows = list(csv.DictReader(f))

        assert rows
        assert all(row["can_codex_capture"] == "No" for row in rows)
        assert all(row["why_not_autonomous"] for row in rows)
        assert len(rows) == len(catalog.platform_catalog())

    def test_machine_readable_outputs_distinguish_review_from_generation(self, tmp_path):
        """A rewrite must not imply that official sources were re-reviewed."""
        records = catalog.platform_catalog()
        csv_output = tmp_path / "catalog.csv"
        json_output = tmp_path / "catalog.json"
        reviewed = dt.date(2026, 8, 5)

        catalog.write_csv(records, csv_output, source_reviewed_at=reviewed)
        generated = dt.datetime(2026, 8, 5, 4, 5, tzinfo=dt.timezone.utc)
        catalog.write_json(
            records,
            json_output,
            source_reviewed_at=reviewed,
            now=generated,
        )

        assert "source_reviewed_at" in csv_output.read_text(encoding="utf-8").splitlines()[0]
        payload = json.loads(json_output.read_text(encoding="utf-8"))
        assert payload["source_reviewed_at"] == "2026-08-05"
        assert payload["generated_at"] == "2026-08-05T04:05:00+00:00"

    def test_review_date_rejects_non_iso_input(self):
        """Review evidence requires an unambiguous ISO calendar date."""
        assert catalog._review_date("2026-08-05") == dt.date(2026, 8, 5)

        with pytest.raises(argparse.ArgumentTypeError, match="YYYY-MM-DD"):
            catalog._review_date("August 5")

    def test_file_writes_require_explicit_source_review_date(self, tmp_path):
        """Default runs must not overwrite reviewed artifacts without evidence."""
        result = catalog.main([
            "--output", str(tmp_path / "latest.md"),
            "--csv-output", str(tmp_path / "latest.csv"),
            "--json-output", str(tmp_path / "latest.json"),
        ])

        assert result == 2
        assert not (tmp_path / "latest.md").exists()
