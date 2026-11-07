"""Power bests must come from full windows.

A rolling AVG over `ROWS BETWEEN N-1 PRECEDING` also yields a value for a ride's
first N-1 rows, so a 290 s ride reported a 60-minute best, and a hard opening
minute on a long ride was scored as a 20-minute effort.

These tests execute the panels' own SQL, read from the dashboard JSON, because
the behaviour under test *is* the SQL: whether a window was full, and what a
short ride emits. tests/test_dashboards.py carries the static half, which runs
without a database.

Run with:
    VELOMATE_TEST_DATABASE_URL=postgresql://... python3 -m pytest \
        tests/test_power_best_sql.py -q
"""

import json
import os

import pytest

from critical_power import compute_mean_maximal_power

from grafana_sql import expand
from pg_reset import reset_schema

TEST_DB = os.environ.get("VELOMATE_TEST_DATABASE_URL")

pytestmark = pytest.mark.skipif(
    not TEST_DB,
    reason="needs a throwaway PostgreSQL in VELOMATE_TEST_DATABASE_URL",
)

DASHBOARD_DIR = os.path.join(
    os.path.dirname(__file__), "..", "grafana", "dashboards", "metric"
)

CURVE_DURATIONS = {
    "5s": 5, "10s": 10, "30s": 30, "1min": 60, "2min": 120,
    "5min": 300, "10min": 600, "20min": 1200, "60min": 3600,
}
BEST_EFFORT_TARGETS = {"A": 60, "B": 300, "C": 1200}


def _hard_start(samples):
    """Ten hard seconds, then steady. The ride's opening partial windows average
    500 W, which beats every full window longer than 10 s."""
    return ([500] * 10 + [200] * samples)[:samples]


def _panel_sql(filename, panel_id, ref_id, activity_id=None):
    """The target's SQL as Grafana sends it: variables and macros expanded."""
    with open(os.path.join(DASHBOARD_DIR, filename)) as f:
        panel = next(p for p in json.load(f)["panels"] if p["id"] == panel_id)
    sql = next(t["rawSql"] for t in panel["targets"] if t["refId"] == ref_id)
    return expand(
        sql,
        {"sport_type": "all", "activity_id": activity_id},
        "2000-01-01T00:00:00Z",
        "2100-01-01T00:00:00Z",
    )


def _seed_ride(conn, powers, date="2026-04-26 10:00:00+00"):
    """Insert a ride with a 1 Hz power stream and return its id."""
    with conn.cursor() as cur:
        cur.execute(
            """INSERT INTO activities (name, date, distance_m, duration_s,
                                       elevation_m, sport_type)
               VALUES ('Synthetic', %s, 20000.0, %s, 100.0, 'cycling_outdoor')
               RETURNING id""",
            (date, len(powers)),
        )
        ride_id = cur.fetchone()[0]
        cur.execute(
            """INSERT INTO activity_streams (activity_id, time_offset, power)
               SELECT %s, ord - 1, p
               FROM unnest(%s::int[]) WITH ORDINALITY AS t(p, ord)""",
            (ride_id, powers),
        )
    return ride_id


def _curve(conn, ride_id):
    """Power Duration Curve for one ride, as an ordered list of (label, watts)."""
    with conn.cursor() as cur:
        cur.execute(_panel_sql("activity.json", 40, "A", ride_id))
        return [(label, watts) for label, watts in cur.fetchall()]


def _best_efforts(conn, ref_id):
    """One Best Efforts target, as a list of (date, watts)."""
    with conn.cursor() as cur:
        cur.execute(_panel_sql("all-time-progression.json", 21, ref_id))
        return cur.fetchall()


@pytest.fixture()
def conn():
    import psycopg2

    # Same leak as tests/test_route_id_stability.py: in a whole-suite run this
    # file can inherit another module's mocked psycopg2. A mock has no __file__.
    if not hasattr(psycopg2, "__file__"):
        pytest.skip(
            "psycopg2 is mocked by another module in this pytest session; run "
            "this file on its own: python -m pytest tests/test_power_best_sql.py"
        )

    import db as velomate_db

    connection = psycopg2.connect(TEST_DB)
    connection.autocommit = True
    reset_schema(connection)
    velomate_db.create_schema(connection)
    yield connection
    connection.close()


def test_curve_stops_at_the_length_of_the_ride(conn):
    """The reported bug: a 290 s ride has no 5-, 10-, 20- or 60-minute best."""
    ride = _seed_ride(conn, _hard_start(290))

    assert [label for label, _ in _curve(conn, ride)] == [
        "5s", "10s", "30s", "1min", "2min",
    ]


def test_curve_emits_no_row_at_all_for_a_ride_too_short_for_any_duration(conn):
    """No row, not a NULL row: the panel then renders its noValue text."""
    ride = _seed_ride(conn, [300, 300, 300])

    assert _curve(conn, ride) == []


@pytest.mark.parametrize("label, duration", CURVE_DURATIONS.items())
def test_curve_needs_exactly_a_full_window(conn, label, duration):
    """The guard's boundary, for every arm: N samples count, N-1 do not."""
    one_short = _seed_ride(conn, [250] * (duration - 1))
    exactly_full = _seed_ride(conn, [250] * duration)

    assert label not in dict(_curve(conn, one_short))
    assert dict(_curve(conn, exactly_full))[label] == 250


def test_curve_matches_the_ingestor_on_a_long_ride(conn):
    """Every arm of a long ride agrees with the ingestor's own MMP, including
    when the ride opens hard enough for a partial window to win."""
    powers = _hard_start(3700)
    curve = dict(_curve(conn, _seed_ride(conn, powers)))

    assert list(curve) == list(CURVE_DURATIONS)
    for label, duration in CURVE_DURATIONS.items():
        expected = compute_mean_maximal_power(powers, duration)
        assert abs(float(curve[label]) - expected) <= 0.5, (label, curve[label], expected)


@pytest.mark.parametrize("ref_id, duration", BEST_EFFORT_TARGETS.items())
def test_best_efforts_skip_rides_shorter_than_the_duration(conn, ref_id, duration):
    """A short ride gets no point at all rather than a NULL one, and a ride that
    is long enough scores its full-window best, not its opening seconds."""
    _seed_ride(conn, _hard_start(duration - 1), "2026-04-26 10:00:00+00")
    powers = _hard_start(duration)
    _seed_ride(conn, powers, "2026-04-27 10:00:00+00")

    rows = _best_efforts(conn, ref_id)

    assert [str(day) for day, _ in rows] == ["2026-04-27"]
    expected = compute_mean_maximal_power(powers, duration)
    assert abs(float(rows[0][1]) - expected) <= 0.5
