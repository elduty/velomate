"""Detected climbs against real SQL.

Two claims that a mocked cursor could only repeat back: what the stream query
hands the detector, and what a METRICS_VERSION bump leaves in ride_climbs.
Strava rows cannot be re-fetched, so losing them on a bump would be permanent.

Run with:
    VELOMATE_TEST_DATABASE_URL=postgresql://... python3 -m pytest \
        tests/test_climb_rebuild.py -q
"""

import math
import os

import pytest

from pg_reset import reset_schema

TEST_DB = os.environ.get("VELOMATE_TEST_DATABASE_URL")

pytestmark = pytest.mark.skipif(
    not TEST_DB,
    reason="needs a throwaway PostgreSQL in VELOMATE_TEST_DATABASE_URL",
)

PREVIOUS_VERSION = "16"
LATITUDE = 38.7
# A degree of longitude here: 2 x pi x 6371000 / 360 x cos(38.7 degrees). Riding
# east makes swapped columns 28% wrong, where riding north would hide them.
METRES_PER_DEGREE_EAST = 111194.93 * math.cos(math.radians(LATITUDE))
SPEED_MS = 5.0


def _climb_stream(gps=True, stop_s=300):
    """1 km flat, then 1200 m at 6% with a stop halfway up. 72 m of gain by hand.

    The stream has no samples during the stop, and the first one after it
    still carries riding speed, so speed x delta-t would add 1500 m.
    """
    rows = []
    offset = 0
    distance = 0.0
    while distance <= 2200:
        altitude = 100.0 + max(0.0, distance - 1000) * 0.06
        lat, lng = LATITUDE, -9.14 + distance / METRES_PER_DEGREE_EAST
        if not gps:
            lat, lng = None, None
        rows.append((offset, 140, 180, 85, SPEED_MS * 3.6, altitude, lat, lng))
        offset += 1
        if abs(distance - 1600) < SPEED_MS / 2:
            offset += stop_s
        distance += SPEED_MS
    return rows


def _seed_ride(conn, rows, name="Ride", date="2026-05-03 10:00:00+00", is_indoor=False):
    with conn.cursor() as cur:
        cur.execute(
            """INSERT INTO activities (name, date, distance_m, duration_s, elevation_m,
                                       avg_hr, avg_power, sport_type, is_indoor)
               VALUES (%s, %s, 2200.0, %s, 72.0, 140, 180, 'cycling_outdoor', %s)
               RETURNING id""",
            (name, date, rows[-1][0], is_indoor),
        )
        act_id = cur.fetchone()[0]
        cur.executemany(
            """INSERT INTO activity_streams
                   (activity_id, time_offset, hr, power, cadence, speed_kmh, altitude_m, lat, lng)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)""",
            [(act_id,) + row for row in rows],
        )
    return act_id


def _seed_strava_segment(conn, act_id, name="Serra Climb", gain=40, low=900, high=940):
    """Well away from the ride's altitudes, so it cannot suppress a detected climb."""
    with conn.cursor() as cur:
        cur.execute(
            """INSERT INTO ride_climbs
                   (activity_id, gain_m, length_m, avg_grade, start_alt, peak_alt,
                    duration_s, category, score, source, segment_name)
               VALUES (%s, %s, 800, 5.0, %s, %s, 200, 'Climb', 4000, 'strava', %s)""",
            (act_id, gain, low, high, name),
        )


def _seed_strava_sentinel(conn, act_id):
    with conn.cursor() as cur:
        cur.execute("""INSERT INTO ride_climbs (activity_id, gain_m, category, source)
                       VALUES (%s, 0, 'none', 'strava')""", (act_id,))


def _seed_stale_detection(conn, act_id):
    with conn.cursor() as cur:
        cur.execute("""INSERT INTO ride_climbs (activity_id, gain_m, length_m, avg_grade,
                                                category, source)
                       VALUES (%s, 999, 3000, 2.3, 'Cat 4', 'detected')""", (act_id,))


def _rows(conn, where="TRUE"):
    with conn.cursor() as cur:
        cur.execute(f"SELECT * FROM ride_climbs WHERE {where} ORDER BY id")
        return cur.fetchall()


def _detected(conn):
    with conn.cursor() as cur:
        cur.execute("""SELECT activity_id, length_m, avg_grade, gain_m FROM ride_climbs
                       WHERE source = 'detected' ORDER BY activity_id, start_offset""")
        return cur.fetchall()


def _set_version(conn, version):
    with conn.cursor() as cur:
        cur.execute("UPDATE sync_state SET value = %s WHERE key = 'metrics_version'", (version,))
        assert cur.rowcount == 1


@pytest.fixture()
def conn(monkeypatch):
    import psycopg2

    # Same reason as test_route_id_stability.py: other modules leave a mocked
    # psycopg2 in the shared pytest process.
    if not hasattr(psycopg2, "__file__"):
        pytest.skip(
            "psycopg2 is mocked by another module in this pytest session; run "
            "this file on its own: python -m pytest tests/test_climb_rebuild.py"
        )

    import db as velomate_db

    monkeypatch.setenv("VELOMATE_FTP", "200")
    connection = psycopg2.connect(TEST_DB)
    connection.autocommit = True  # what db.get_connection hands the recalculation
    reset_schema(connection)
    velomate_db.create_schema(connection)
    yield connection
    connection.close()


def test_a_stop_and_a_flat_approach_stay_out_of_the_climb(conn):
    """State A, the stream query's speed x delta-t: the stop adds 1500 m, so the
    climb is lost or reported at a fraction of its grade. State B: the true climb.

    The tolerance is the smoothing window, 10 s either side at 5 m/s, and the
    grade bounds follow from it: 69/1250 = 5.5% to 72/1150 = 6.3%.
    """
    import fitness

    act_id = _seed_ride(conn, _climb_stream())

    fitness.detect_climbs_for_rides(conn)

    climbs = _detected(conn)
    assert len(climbs) == 1, climbs
    climb_ride, length_m, avg_grade, gain_m = climbs[0]
    assert climb_ride == act_id
    assert length_m == pytest.approx(1200, abs=50)
    assert 5.5 <= avg_grade <= 6.3
    assert 69 <= gain_m <= 72


def test_a_ride_with_no_gps_is_marked_done_and_not_rechecked(conn):
    """No distance, so no climb, and the sentinel stops detection from visiting
    the ride on every recalculation. Speed alone must not produce a climb."""
    import fitness

    act_id = _seed_ride(conn, _climb_stream(gps=False), is_indoor=True)

    assert fitness.detect_climbs_for_rides(conn) == 1
    assert fitness.detect_climbs_for_rides(conn) == 0

    with conn.cursor() as cur:
        cur.execute("SELECT activity_id, category, source FROM ride_climbs")
        assert cur.fetchall() == [(act_id, "none", "none")]


def _installed_at_previous_version(conn):
    """A live database as the previous version left it: stale detections beside
    Strava rows that cannot be fetched again."""
    import fitness

    with_segment = _seed_ride(conn, _climb_stream(), name="Segment")
    with_sentinel = _seed_ride(conn, _climb_stream(), name="Sentinel",
                               date="2026-05-10 10:00:00+00")
    fitness.recalculate_fitness(conn)
    with conn.cursor() as cur:
        cur.execute("DELETE FROM ride_climbs")
    _seed_strava_segment(conn, with_segment)
    _seed_strava_segment(conn, with_segment, name="Other Segment", gain=20, low=950, high=970)
    _seed_strava_sentinel(conn, with_sentinel)
    _seed_stale_detection(conn, with_segment)
    _seed_stale_detection(conn, with_sentinel)
    _set_version(conn, PREVIOUS_VERSION)


def test_bump_rebuilds_detected_climbs_and_leaves_strava_rows_exactly_alone(conn):
    """State A: stale detected rows beside Strava rows. State B: detection redone
    from the stream, and every Strava row still there, ids included."""
    import fitness

    _installed_at_previous_version(conn)
    strava_before = _rows(conn, "source = 'strava'")
    assert len(strava_before) == 3

    fitness.recalculate_fitness(conn)

    assert _rows(conn, "source = 'strava'") == strava_before
    rebuilt = _detected(conn)
    assert len(rebuilt) == 2, rebuilt
    for _ride, length_m, _grade, gain_m in rebuilt:
        assert gain_m != 999
        assert length_m == pytest.approx(1200, abs=50)


def test_without_a_version_change_stored_climbs_are_left_alone(conn):
    """The control: the rebuild above really is the bump's doing, since a ride
    that already has a detected row is otherwise skipped."""
    import fitness

    _installed_at_previous_version(conn)
    _set_version(conn, fitness.METRICS_VERSION)
    before = _rows(conn)

    fitness.recalculate_fitness(conn)

    assert _rows(conn) == before


def test_a_second_start_changes_nothing(conn):
    """A restart after the bump must neither reset again nor add rows."""
    import fitness

    _installed_at_previous_version(conn)
    fitness.recalculate_fitness(conn)
    after_bump = _rows(conn)

    fitness.recalculate_fitness(conn)

    assert _rows(conn) == after_bump
