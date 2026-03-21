"""Structural validation of Grafana dashboard JSON files."""

import json
import os
import pytest

DASHBOARD_DIR = os.path.join(os.path.dirname(__file__), "..", "grafana", "dashboards")


def get_dashboard_files():
    return [f for f in os.listdir(DASHBOARD_DIR) if f.endswith(".json")]


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
        for p in d["panels"]:
            assert "id" in p, f"Panel '{p.get('title', '?')}' missing id"

    def test_no_duplicate_panel_ids(self, filename):
        d = self.load(filename)
        ids = [p["id"] for p in d["panels"]]
        dupes = [i for i in ids if ids.count(i) > 1]
        assert not dupes, f"Duplicate panel ids: {set(dupes)}"

    def test_all_panels_have_gridpos(self, filename):
        d = self.load(filename)
        for p in d["panels"]:
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
