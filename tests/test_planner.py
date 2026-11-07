"""Tests for velomate.planner pure functions."""

import pytest

from velomate.planner import _top_routes, _form_note, recommend


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _tour(distance, elevation_up=400, date="2026-03-10", name="Test"):
    return {
        "distance": distance,
        "elevation_up": elevation_up,
        "date": date,
        "name": name,
    }


# ===========================================================================
# _top_routes
# ===========================================================================


class TestTopRoutesDedup:
    """Two tours with the same distance/elevation bucket should be deduped."""

    def test_basic_dedup(self):
        tours = [
            _tour(50000, 400, "2026-03-10", "Route A"),
            _tour(50200, 410, "2026-03-11", "Route B"),  # same bucket (50 km, 400 m)
        ]
        result = _top_routes(tours, n=5)
        assert len(result) == 1

    def test_dedup_keeps_first_encountered(self):
        tours = [
            _tour(50000, 400, "2026-03-10", "Route A"),
            _tour(50200, 410, "2026-03-11", "Route B"),
        ]
        # Default sort is by date descending, so Route B (later) comes first
        result = _top_routes(tours, n=5)
        assert result[0]["name"] == "Route B"


class TestTopRoutesNLimit:
    """10 tours with distinct buckets should return at most n."""

    def test_respects_n_limit(self):
        tours = [_tour(i * 10000, i * 100, f"2026-03-{i:02d}") for i in range(1, 11)]
        result = _top_routes(tours, n=3)
        assert len(result) == 3


class TestTopRoutesHighRisk:
    """When TSB < -30, routes should be sorted by distance ascending."""

    def test_tsb_high_risk_sorts_shortest_first(self):
        tours = [
            _tour(80000, 600, "2026-03-08", "Long"),
            _tour(30000, 200, "2026-03-09", "Short"),
            _tour(55000, 400, "2026-03-10", "Medium"),
        ]
        result = _top_routes(tours, n=3, tsb=-35)
        distances = [r["distance"] for r in result]
        assert distances == sorted(distances)


class TestTopRoutesFresh:
    """When TSB >= 5 (fresh or detraining), routes should be sorted by distance descending."""

    @pytest.mark.parametrize("tsb", [5, 15, 30])
    def test_tsb_fresh_sorts_longest_first(self, tsb):
        tours = [
            _tour(30000, 200, "2026-03-09", "Short"),
            _tour(80000, 600, "2026-03-08", "Long"),
            _tour(55000, 400, "2026-03-10", "Medium"),
        ]
        result = _top_routes(tours, n=3, tsb=tsb)
        distances = [r["distance"] for r in result]
        assert distances == sorted(distances, reverse=True)


class TestTopRoutesNormalTraining:
    """Productive and transitional form are normal training, so routes should
    be sorted by date descending, not steered towards the shortest."""

    @pytest.mark.parametrize("tsb", [-30, -15, -10, 0, 4.9])
    def test_normal_training_sorts_most_recent_first(self, tsb):
        tours = [
            _tour(50000, 400, "2026-03-08", "Oldest"),
            _tour(60000, 500, "2026-03-12", "Newest"),
            _tour(40000, 300, "2026-03-10", "Middle"),
        ]
        result = _top_routes(tours, n=3, tsb=tsb)
        dates = [r["date"] for r in result]
        assert dates == sorted(dates, reverse=True)


class TestTopRoutesEmpty:
    """Empty input should return empty list."""

    def test_empty_tours(self):
        assert _top_routes([], n=3) == []


# ===========================================================================
# _form_note
# ===========================================================================


class TestFormNoteBands:
    """One note per band, with its emoji matching the dashboard colour. A
    boundary value belongs to the band above it, as on the dashboards."""

    @pytest.mark.parametrize("tsb, emoji, word", [
        (-31, "🔴", "high risk"),
        (-30, "🟢", "productive"),
        (-11, "🟢", "productive"),
        (-10, "🔘", "transitional"),
        (4, "🔘", "transitional"),
        (5, "🔵", "fresh"),
        (24, "🔵", "fresh"),
        (25, "🟡", "detraining"),
    ])
    def test_band(self, tsb, emoji, word):
        result = _form_note({"tsb": tsb})
        assert result.startswith(emoji)
        assert word in result.lower()

    @pytest.mark.parametrize("tsb", [-30, -15, -10.1])
    def test_productive_training_is_not_told_to_rest(self, tsb):
        assert "rest" not in _form_note({"tsb": tsb}).lower()

    def test_high_risk_is_told_to_rest(self):
        assert "rest" in _form_note({"tsb": -35}).lower()


class TestFormNoteNoneTsb:
    def test_none_tsb(self):
        assert _form_note({}) is None


# ===========================================================================
# recommend: route-suggestion caption
# ===========================================================================


class TestRouteCaption:
    """The caption above the routes has to describe how they were sorted."""

    def caption(self, tsb):
        report = recommend([], [_tour(50000)], {"tsb": tsb})
        return next((l for l in report.splitlines() if l.startswith("  _Showing")), None)

    def test_high_risk_shows_easier_routes(self):
        assert "easier" in self.caption(-35)

    @pytest.mark.parametrize("tsb, word", [(5, "fresh"), (30, "detraining")])
    def test_fresh_shows_challenging_routes(self, tsb, word):
        caption = self.caption(tsb)
        assert "challenging" in caption
        assert word in caption

    @pytest.mark.parametrize("tsb", [-15, 0])
    def test_normal_training_has_no_caption(self, tsb):
        assert self.caption(tsb) is None
