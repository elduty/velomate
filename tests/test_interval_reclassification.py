"""The METRICS_VERSION bump for the interval classes, against real SQL.

Stored rows carry the old class names. These tests start from that state and
run the same recalculation the container runs on first start. A mocked cursor
could only repeat the statements back, and the claim here is about what is left
in the tables afterwards.

Run with:
    VELOMATE_TEST_DATABASE_URL=postgresql://... python3 -m pytest \
        tests/test_interval_reclassification.py -q
"""

import os

import pytest

from pg_reset import reset_schema

TEST_DB = os.environ.get("VELOMATE_TEST_DATABASE_URL")

pytestmark = pytest.mark.skipif(
    not TEST_DB,
    reason="needs a throwaway PostgreSQL in VELOMATE_TEST_DATABASE_URL",
)

PREVIOUS_VERSION = "15"
FTP = 200


def _stream():
    """An hour with one tempo block, one 5-minute VO2 effort and one surge."""
    power = [100] * 600 + [160] * 900 + [100] * 300 + [220] * 300 + [100] * 300
    power += [280] * 40 + [100] * 1160
    rows = []
    for offset, watts in enumerate(power):
        lat, lng = 38.7 + offset * 0.0001, -9.14
        rows.append((offset, 140, watts, 85, 25.0, 50.0 + offset % 40, lat, lng))
    return rows


def _seed_ride(conn, name, date):
    rows = _stream()
    with conn.cursor() as cur:
        cur.execute(
            """INSERT INTO activities (name, date, distance_m, duration_s, elevation_m,
                                       avg_hr, avg_power, sport_type)
               VALUES (%s, %s, 20000.0, %s, 100.0, 140, 150, 'cycling_outdoor')
               RETURNING id""",
            (name, date, len(rows)),
        )
        act_id = cur.fetchone()[0]
        cur.executemany(
            """INSERT INTO activity_streams
                   (activity_id, time_offset, hr, power, cadence, speed_kmh, altitude_m, lat, lng)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)""",
            [(act_id,) + row for row in rows],
        )
    return act_id


def _store_old_classes(conn, act_id):
    with conn.cursor() as cur:
        cur.execute("DELETE FROM ride_intervals WHERE activity_id = %s", (act_id,))
        for offset, name in enumerate(("sprint", "anaerobic", "vo2", "threshold", "sweetspot", "tempo")):
            cur.execute(
                """INSERT INTO ride_intervals
                       (activity_id, start_offset_s, duration_s, avg_power, np, max_power, classification)
                   VALUES (%s, %s, 40, 250, 250, 300, %s)""",
                (act_id, offset * 100, name),
            )


def _set_version(conn, version):
    with conn.cursor() as cur:
        cur.execute("UPDATE sync_state SET value = %s WHERE key = 'metrics_version'", (version,))
        assert cur.rowcount == 1


def _classes(conn):
    with conn.cursor() as cur:
        cur.execute("""SELECT start_offset_s, duration_s, classification
                       FROM ride_intervals ORDER BY activity_id, start_offset_s""")
        return cur.fetchall()


def _everything_but_intervals(conn):
    """Every table the reset touches, in a comparable form."""
    snapshot = {}
    with conn.cursor() as cur:
        for name, query in {
            "activities": "SELECT * FROM activities ORDER BY id",
            "athlete_stats": "SELECT * FROM athlete_stats ORDER BY date",
            "ride_climbs": "SELECT * FROM ride_climbs ORDER BY activity_id, start_offset, id",
            "w_bal": """SELECT activity_id, time_offset, w_bal FROM activity_streams
                        ORDER BY activity_id, time_offset""",
            "routes": "SELECT id, ride_count FROM ride_routes ORDER BY id",
        }.items():
            cur.execute(query)
            columns = [c.name for c in cur.description]
            snapshot[name] = [dict(zip(columns, row)) for row in cur.fetchall()]
    # Serial ids of rebuilt rows say nothing about their contents.
    for row in snapshot["ride_climbs"]:
        row.pop("id", None)
    return snapshot


@pytest.fixture()
def conn(monkeypatch):
    import psycopg2

    # Same reason as test_route_id_stability.py: other modules leave a mocked
    # psycopg2 in the shared pytest process.
    if not hasattr(psycopg2, "__file__"):
        pytest.skip(
            "psycopg2 is mocked by another module in this pytest session; run "
            "this file on its own: python -m pytest tests/test_interval_reclassification.py"
        )

    import db as velomate_db

    monkeypatch.setenv("VELOMATE_FTP", str(FTP))
    connection = psycopg2.connect(TEST_DB)
    connection.autocommit = True  # what db.get_connection hands the recalculation
    reset_schema(connection)
    velomate_db.create_schema(connection)
    yield connection
    connection.close()


def _populated_old_install(conn):
    """A database as the previous version left it: old classes, older version."""
    import fitness

    first = _seed_ride(conn, "First", "2026-05-03 10:00:00+00")
    second = _seed_ride(conn, "Second", "2026-05-10 10:00:00+00")
    fitness.recalculate_fitness(conn)
    _store_old_classes(conn, first)
    _store_old_classes(conn, second)
    _set_version(conn, PREVIOUS_VERSION)
    return first, second


def test_bump_replaces_every_old_class(conn):
    """State A: six rows per ride under the old names, including 'sprint'.
    State B: the rides' real efforts under the new names, version stored."""
    import fitness

    _populated_old_install(conn)

    fitness.recalculate_fitness(conn)

    ride = [(600, 900, "tempo"), (1800, 300, "vo2"), (2400, 40, "surge")]
    assert _classes(conn) == ride + ride
    with conn.cursor() as cur:
        cur.execute("SELECT value FROM sync_state WHERE key = 'metrics_version'")
        assert cur.fetchone()[0] == fitness.METRICS_VERSION


def test_without_a_version_change_stored_classes_are_left_alone(conn):
    """The control: the replacement above really is the bump's doing, since a
    ride that already has rows is otherwise skipped by the detection step."""
    import fitness

    _populated_old_install(conn)
    _set_version(conn, fitness.METRICS_VERSION)

    fitness.recalculate_fitness(conn)

    assert "sprint" in {row[2] for row in _classes(conn)}


def test_a_second_start_changes_nothing(conn):
    """A restart after the bump must neither reset again nor add rows."""
    import fitness

    _populated_old_install(conn)
    fitness.recalculate_fitness(conn)
    after_bump = _classes(conn)

    fitness.recalculate_fitness(conn)

    assert _classes(conn) == after_bump


def test_bump_leaves_everything_else_as_it_was(conn):
    """The reset clears far more than intervals; the rest of the pass has to
    rebuild it to the same values."""
    import fitness

    _populated_old_install(conn)
    fitness.recalculate_fitness(conn)
    before = _everything_but_intervals(conn)
    assert before["activities"][0]["tss"] is not None, "fixture is wrong: no TSS to compare"
    assert before["w_bal"][0]["w_bal"] is not None, "fixture is wrong: no W'bal to compare"

    _set_version(conn, PREVIOUS_VERSION)
    fitness.recalculate_fitness(conn)

    assert _everything_but_intervals(conn) == before
