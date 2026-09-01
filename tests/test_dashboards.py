"""Structural validation of Grafana dashboard JSON files."""

import glob
import json
import os
import pytest

DASHBOARD_DIR = os.path.join(os.path.dirname(__file__), "..", "grafana", "dashboards")


def get_dashboard_files():
    # All dashboards under grafana/dashboards/<set>/*.json (metric, imperial)
    pattern = os.path.join(DASHBOARD_DIR, "*", "*.json")
    return sorted(os.path.relpath(p, DASHBOARD_DIR) for p in glob.glob(pattern))


def iter_all_panels(dashboard):
    """Yield every panel in the dashboard, including panels nested inside
    collapsed row panels. A collapsed row stores its children in its own
    `panels` array instead of the top-level one."""
    for p in dashboard.get("panels", []):
        yield p
        if p.get("type") == "row":
            for child in p.get("panels", []) or []:
                yield child


@pytest.mark.parametrize("filename", get_dashboard_files())
class TestDashboardStructure:
    def load(self, filename):
        with open(os.path.join(DASHBOARD_DIR, filename)) as f:
            return json.load(f)

    def test_valid_json(self, filename):
        """Dashboard file is valid JSON."""
        self.load(filename)

    def test_has_uid(self, filename):
        d = self.load(filename)
        assert "uid" in d and d["uid"], f"{filename} missing uid"

    def test_has_panels(self, filename):
        d = self.load(filename)
        assert "panels" in d and len(d["panels"]) > 0

    def test_all_panels_have_id(self, filename):
        d = self.load(filename)
        for p in iter_all_panels(d):
            assert "id" in p, f"Panel '{p.get('title', '?')}' missing id"

    def test_no_duplicate_panel_ids(self, filename):
        d = self.load(filename)
        ids = [p["id"] for p in iter_all_panels(d)]
        dupes = [i for i in ids if ids.count(i) > 1]
        assert not dupes, f"Duplicate panel ids: {set(dupes)}"

    def test_all_panels_have_gridpos(self, filename):
        d = self.load(filename)
        for p in iter_all_panels(d):
            assert "gridPos" in p, f"Panel {p['id']} missing gridPos"
            gp = p["gridPos"]
            for key in ["h", "w", "x", "y"]:
                assert key in gp, f"Panel {p['id']} gridPos missing '{key}'"

    def test_has_graph_tooltip(self, filename):
        d = self.load(filename)
        assert d.get("graphTooltip") == 2, f"{filename} should have shared crosshair (graphTooltip: 2)"

    def test_has_nav_links(self, filename):
        d = self.load(filename)
        links = d.get("links", [])
        assert len(links) >= 1, f"{filename} should link to at least one other dashboard"

    def test_value_stat_panels_guard_null(self, filename):
        """A colorMode:value stat panel reading a single activity's column must
        filter NULLs (IS NOT NULL) so it returns *no rows* — not a row with a
        NULL value — when the metric is absent (e.g. a GPS-only ride with no
        HR/power sensors). A NULL value-row in a colorMode:value stat panel
        crashes the Grafana 12.4 scene renderer, which blanks every sibling
        panel on the dashboard (the no-sensor "all N/A" bug)."""
        d = self.load(filename)
        for p in iter_all_panels(d):
            if p.get("type") != "stat":
                continue
            if (p.get("options") or {}).get("colorMode") != "value":
                continue
            sql = " ".join(t.get("rawSql", "") for t in (p.get("targets") or []))
            if "${activity_id}" not in sql:
                continue  # per-ride single-activity stat panels only
            assert "IS NOT NULL" in sql.upper(), (
                f"{filename} panel {p['id']} '{p.get('title')}': colorMode:value "
                f"per-ride stat with no NULL guard — a no-sensor ride emits a NULL "
                f"value-row and crashes the dashboard scene. Add an IS NOT NULL "
                f"filter so it returns no rows (graceful noValue) instead."
            )
