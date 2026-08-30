"""Static regression checks for deployment and workflow hardening."""

import re
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent


class TestContainerHardening:
    def test_primary_container_runs_as_scanner_user(self):
        dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
        assert "USER scanner" in dockerfile
        assert "DATA_DIR=/data" in dockerfile

    def test_sxbet_proxy_requires_service_token(self):
        dockerfile = (ROOT / "sxbet-proxy" / "Dockerfile").read_text(encoding="utf-8")
        assert "SXBET_PROXY_TOKEN" in dockerfile
        assert "$http_x_proxy_token" in dockerfile
        assert 'proxy_set_header Authorization ""' in dockerfile


class TestWorkflowHardening:
    def test_actions_are_pinned_to_full_commit_shas(self):
        uses = []
        for workflow in (ROOT / ".github" / "workflows").glob("*.yml"):
            uses.extend(re.findall(r"uses:\s+[^@\s]+@([^\s#]+)", workflow.read_text(encoding="utf-8")))
        assert uses
        assert all(re.fullmatch(r"[0-9a-f]{40}", revision) for revision in uses)

    def test_kalshi_lip_job_has_no_token_permissions(self):
        workflow = (ROOT / ".github" / "workflows" / "kalshi-lip-scan.yml").read_text(encoding="utf-8")
        assert "permissions: {}" in workflow
        assert "persist-credentials: false" in workflow
