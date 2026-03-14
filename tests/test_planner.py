"""Tests for veloai.planner pure functions."""

import pytest

from veloai.planner import _top_routes, _form_note


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


class TestTopRoutesFatigued:
    """When TSB < -10, routes should be sorted by distance ascending."""

    def test_tsb_fatigued_sorts_shortest_first(self):
        tours = [
            _tour(80000, 600, "2026-03-08", "Long"),
            _tour(30000, 200, "2026-03-09", "Short"),
            _tour(55000, 400, "2026-03-10", "Medium"),
        ]
        result = _top_routes(tours, n=3, tsb=-15)
        distances = [r["distance"] for r in result]
        assert distances == sorted(distances)


class TestTopRoutesFresh:
    """When TSB > 10, routes should be sorted by distance descending."""

    def test_tsb_fresh_sorts_longest_first(self):
        tours = [
            _tour(30000, 200, "2026-03-09", "Short"),
            _tour(80000, 600, "2026-03-08", "Long"),
            _tour(55000, 400, "2026-03-10", "Medium"),
        ]
        result = _top_routes(tours, n=3, tsb=15)
        distances = [r["distance"] for r in result]
        assert distances == sorted(distances, reverse=True)


class TestTopRoutesNeutral:
    """When TSB is 0 (within -10..10), routes should be sorted by date descending."""

    def test_tsb_neutral_sorts_most_recent_first(self):
        tours = [
            _tour(50000, 400, "2026-03-08", "Oldest"),
            _tour(60000, 500, "2026-03-12", "Newest"),
            _tour(40000, 300, "2026-03-10", "Middle"),
        ]
        result = _top_routes(tours, n=3, tsb=0)
        dates = [r["date"] for r in result]
        assert dates == sorted(dates, reverse=True)


class TestTopRoutesEmpty:
    """Empty input should return empty list."""

    def test_empty_tours(self):
        assert _top_routes([], n=3) == []


# ===========================================================================
# _form_note
# ===========================================================================


class TestFormNoteFresh:
    def test_fresh(self):
        result = _form_note({"tsb": 15})
        assert result is not None
        assert "fresh" in result.lower()


class TestFormNoteNeutral:
    def test_neutral(self):
        result = _form_note({"tsb": 0})
        assert result is not None
        assert "neutral" in result.lower()


class TestFormNoteFatigued:
    def test_fatigued(self):
        result = _form_note({"tsb": -15})
        assert result is not None
        assert "fatigued" in result.lower()


class TestFormNoteNoneTsb:
    def test_none_tsb(self):
        assert _form_note({}) is None


class TestFormNoteBoundaries:
    """Exact boundary values: >10 is fresh, -10..10 is neutral, <-10 is fatigued."""

    def test_tsb_10_is_neutral(self):
        result = _form_note({"tsb": 10})
        assert "neutral" in result.lower()

    def test_tsb_11_is_fresh(self):
        result = _form_note({"tsb": 11})
        assert "fresh" in result.lower()

    def test_tsb_neg10_is_fatigued(self):
        # -10 is NOT > -10, so it falls to the fatigued branch
        result = _form_note({"tsb": -10})
        assert "fatigued" in result.lower()

    def test_tsb_neg11_is_fatigued(self):
        result = _form_note({"tsb": -11})
        assert "fatigued" in result.lower()
