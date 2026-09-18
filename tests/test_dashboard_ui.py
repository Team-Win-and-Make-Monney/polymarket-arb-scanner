"""Tests for dashboard_ui.py — HTML template renderer + XSS regression guard.

The dashboard ships a single-page HTML+JS template. These tests lock in:
- the structural contract (DOCTYPE, title, chart.js include, key DOM ids)
- the REFRESH_SECONDS substitution
- the XSS regression guard (no innerHTML in the rendered output)
- the presence of the safe DOM helpers introduced by the XSS fix

Stdlib + pytest only — string assertions are sufficient for a renderer
whose output is built by .replace() on a single template literal.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import dashboard_ui


# ---------------------------------------------------------------------------
# Render smoke
# ---------------------------------------------------------------------------


class TestRenderSmoke:
    def test_returns_non_empty_string(self):
        html = dashboard_ui.get_dashboard_html()
        assert isinstance(html, str)
        assert len(html) > 10_000

    def test_renders_doctype_and_head(self):
        html = dashboard_ui.get_dashboard_html()
        assert html.startswith("<!DOCTYPE html>")
        assert "<title>" in html
        assert 'charset="utf-8"' in html

    def test_includes_chart_js(self):
        html = dashboard_ui.get_dashboard_html()
        assert "chart.js" in html.lower()

    def test_chart_js_has_sri_and_crossorigin(self):
        # Audit S07: the CDN script must be integrity-pinned so a CDN compromise
        # can't inject arbitrary JS into the trading dashboard.
        html = dashboard_ui.get_dashboard_html()
        assert 'integrity="sha384-' in html
        assert 'crossorigin="anonymous"' in html


# ---------------------------------------------------------------------------
# REFRESH_SECONDS substitution
# ---------------------------------------------------------------------------


class TestRefreshSubstitution:
    def test_default_refresh_substituted(self):
        html = dashboard_ui.get_dashboard_html()
        assert "__REFRESH_SECONDS__" not in html, (
            "placeholder must be replaced — found unresolved __REFRESH_SECONDS__"
        )

    @pytest.mark.parametrize("refresh", [5, 15, 30, 60, 300])
    def test_custom_refresh_appears_in_output(self, refresh: int):
        html = dashboard_ui.get_dashboard_html(refresh_seconds=refresh)
        assert "__REFRESH_SECONDS__" not in html
        # The refresh interval is multiplied into a setInterval(... * 1000) call;
        # the literal integer should appear somewhere in the JS.
        assert str(refresh) in html


# ---------------------------------------------------------------------------
# XSS regression guard — the whole point of Sprint 1
# ---------------------------------------------------------------------------


class TestXSSRegression:
    """If anyone re-introduces innerHTML, these tests fail loudly."""

    def test_no_innerhtml_in_rendered_html(self):
        html = dashboard_ui.get_dashboard_html()
        assert "innerHTML" not in html, (
            "innerHTML re-introduced in dashboard_ui.py — use createElement + "
            "textContent + replaceChildren instead. See the DOM helpers section."
        )

    def test_no_innerhtml_in_source(self):
        """Defense-in-depth: scan the source file itself, not just the rendered
        output, so the test catches innerHTML in branches the template might
        not include in every render."""
        source = Path(dashboard_ui.__file__).read_text(encoding="utf-8")
        assert "innerHTML" not in source, (
            "innerHTML found in dashboard_ui.py source — XSS regression."
        )

    def test_dom_helpers_present(self):
        """Sanity check that the safe DOM construction helpers exist."""
        html = dashboard_ui.get_dashboard_html()
        for helper in ("makeCell", "makeStatusCell", "setEmpty", "setEmptyDiv"):
            assert helper in html, f"missing DOM helper: {helper}"

    def test_dom_apis_used(self):
        """At least one of the safe DOM APIs must appear repeatedly in the
        rendered output (regression guard against silent revert)."""
        html = dashboard_ui.get_dashboard_html()
        count = (
            html.count("createElement")
            + html.count("textContent")
            + html.count("replaceChildren")
        )
        assert count >= 10, (
            f"expected >= 10 safe-DOM API calls in rendered output, found {count}"
        )


# ---------------------------------------------------------------------------
# Structural contract — key DOM ids the API endpoints update
# ---------------------------------------------------------------------------


class TestStructuralContract:
    """If a future refactor accidentally renames one of these element ids the
    dashboard JS will silently fail. Pin them."""

    @pytest.mark.parametrize(
        "element_id",
        [
            "kpi-daily-pnl",
            "kpi-cumulative-pnl",
            "kpi-positions",
            "kpi-opps",
            "strategy-tbody",
            "positions-tbody",
            "trades-tbody",
            "opps-tbody",
            "alerts-body",
            "failures-tbody",
            "balances-tbody",
            "strategy-pnl-tbody",
            "mode-badge",
            "kill-btn",
            "paused-banner",
        ],
    )
    def test_dom_id_present(self, element_id: str):
        html = dashboard_ui.get_dashboard_html()
        assert f'id="{element_id}"' in html, f"missing dom id: {element_id}"

    @pytest.mark.parametrize(
        "fn_name",
        [
            "renderStrategies",
            "renderPositions",
            "renderTrades",
            "renderOpportunities",
            "renderAlerts",
            "renderFailures",
            "renderBalancesTable",
            "renderStrategyPnlTable",
            "renderHealth",
            "renderCumulative",
        ],
    )
    def test_render_function_present(self, fn_name: str):
        html = dashboard_ui.get_dashboard_html()
        assert f"function {fn_name}" in html, (
            f"missing render function: {fn_name} — refactor may have dropped it"
        )
