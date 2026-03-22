"""Tests for velomate/db.py — CLI read-only DB client."""

import sys
from datetime import date
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# Ensure velomate package is importable
_project_dir = Path(__file__).resolve().parent.parent
if str(_project_dir) not in sys.path:
    sys.path.insert(0, str(_project_dir))


def _make_conn(fetchone_val=None, fetchall_val=None, raise_on_execute=False):
    """Build a mock psycopg2 connection."""
    conn = MagicMock()
    cur = MagicMock()
    conn.cursor.return_value.__enter__ = MagicMock(return_value=cur)
    conn.cursor.return_value.__exit__ = MagicMock(return_value=False)

    if raise_on_execute:
        cur.execute.side_effect = Exception("DB error")
    else:
        cur.fetchone.return_value = fetchone_val
        cur.fetchall.return_value = fetchall_val or []

    return conn


# ---------------------------------------------------------------------------
# get_latest_fitness
# ---------------------------------------------------------------------------

class TestGetLatestFitness:
    def test_returns_dict_with_expected_keys(self):
        """Should return dict with date, ctl, atl, tsb keys."""
        from velomate.db import get_latest_fitness

        conn = _make_conn(fetchone_val=(date(2026, 3, 20), 45.2, 62.1, -16.9))
        result = get_latest_fitness(conn)

        assert result["date"] == date(2026, 3, 20)
        assert result["ctl"] == 45.2
        assert result["atl"] == 62.1
        assert result["tsb"] == -16.9

    def test_returns_empty_dict_on_no_data(self):
        from velomate.db import get_latest_fitness

        conn = _make_conn(fetchone_val=None)
        result = get_latest_fitness(conn)
        assert result == {}

    def test_returns_empty_dict_on_none_conn(self):
        from velomate.db import get_latest_fitness
        assert get_latest_fitness(None) == {}

    def test_returns_empty_dict_on_db_error(self):
        from velomate.db import get_latest_fitness

        conn = _make_conn(raise_on_execute=True)
        result = get_latest_fitness(conn)
        assert result == {}


# ---------------------------------------------------------------------------
# get_routes
# ---------------------------------------------------------------------------

class TestGetRoutes:
    def test_returns_list_of_dicts(self):
        from velomate.db import get_routes

        conn = _make_conn(fetchall_val=[
            (1, "Serra da Arrabida Loop", 65000.0, 800.0, "cycling_outdoor", date(2026, 3, 15), 1),
            (2, "Cascais Coastal", 40000.0, 200.0, "cycling_outdoor", date(2026, 3, 10), 1),
        ])
        result = get_routes(conn)

        assert len(result) == 2
        assert result[0]["name"] == "Serra da Arrabida Loop"
        assert result[0]["distance"] == 65000.0
        assert result[0]["elevation_up"] == 800.0
        assert result[1]["date"] == "2026-03-10"

    def test_returns_empty_list_on_none_conn(self):
        from velomate.db import get_routes
        assert get_routes(None) == []

    def test_returns_empty_list_on_db_error(self):
        from velomate.db import get_routes

        conn = _make_conn(raise_on_execute=True)
        result = get_routes(conn)
        assert result == []


# ---------------------------------------------------------------------------
# get_avg_speed
# ---------------------------------------------------------------------------

class TestGetAvgSpeed:
    def test_returns_float_on_success(self):
        from velomate.db import get_avg_speed

        conn = _make_conn(fetchone_val=(24.5,))
        result = get_avg_speed(conn)
        assert result == 24.5

    def test_returns_none_on_none_conn(self):
        from velomate.db import get_avg_speed
        assert get_avg_speed(None) is None

    def test_returns_none_on_db_error(self):
        from velomate.db import get_avg_speed

        conn = _make_conn(raise_on_execute=True)
        result = get_avg_speed(conn)
        assert result is None
