"""Stored rides with GPS and no speed get one, and nothing else changes.

Run with:
    VELOMATE_TEST_DATABASE_URL=postgresql://... python3 -m pytest \
        tests/test_speed_backfill.py -q
"""

import os
import sys
from pathlib import Path

import pytest

from pg_reset import reset_schema

_ingestor_dir = Path(__file__).resolve().parent.parent / "ingestor"
if str(_ingestor_dir) not in sys.path:
    sys.path.insert(0, str(_ingestor_dir))

TEST_DB = os.environ.get("VELOMATE_TEST_DATABASE_URL")

pytestmark = pytest.mark.skipif(
    not TEST_DB,
    reason="needs a throwaway PostgreSQL in VELOMATE_TEST_DATABASE_URL",
)

DEG_PER_SECOND_AT_25_KMH = 25 / 3.6 / 111195


@pytest.fixture
def conn():
    import psycopg2

    if not hasattr(psycopg2, "__file__"):
        pytest.skip("psycopg2 is mocked by another module in this pytest session")

    import db

    connection = psycopg2.connect(TEST_DB)
    connection.autocommit = True
    reset_schema(connection)
    db.create_schema(connection)
    yield connection
    connection.close()


def _ride(conn, samples) -> int:
    """Store a ride and these (time_offset, speed_kmh, lat, lng) samples, bypassing the code."""
    with conn.cursor() as cur:
        cur.execute("INSERT INTO activities (name, date) VALUES ('Ride', now()) RETURNING id")
        activity_id = cur.fetchone()[0]
        for sample in samples:
            cur.execute("""INSERT INTO activity_streams (activity_id, time_offset, speed_kmh, lat, lng)
                           VALUES (%s, %s, %s, %s, %s)""", (activity_id, *sample))
    return activity_id


def _gps(seconds=30, speed=None):
    return [(t, speed, 38.7 + t * DEG_PER_SECOND_AT_25_KMH, -9.14) for t in range(seconds)]


def _speeds(conn, activity_id):
    with conn.cursor() as cur:
        cur.execute("SELECT speed_kmh FROM activity_streams WHERE activity_id = %s ORDER BY time_offset",
                    (activity_id,))
        return [row[0] for row in cur.fetchall()]


def test_a_stored_ride_with_gps_and_no_speed_gets_it(conn):
    import db

    ride = _ride(conn, _gps())
    assert db.backfill_missing_speed(conn) == 1
    speeds = _speeds(conn, ride)
    assert speeds[:5] == [None] * 5
    assert speeds[5:] == pytest.approx([25.0] * 25, abs=0.1)


def test_a_ride_with_any_recorded_speed_is_left_alone(conn):
    import db

    samples = _gps(speed=30.0)
    samples[10] = (10, None, samples[10][2], samples[10][3])
    ride = _ride(conn, samples)
    assert db.backfill_missing_speed(conn) == 0
    assert _speeds(conn, ride).count(None) == 1


def test_an_indoor_ride_is_left_alone(conn):
    import db

    ride = _ride(conn, [(t, None, None, None) for t in range(30)])
    assert db.backfill_missing_speed(conn) == 0
    assert _speeds(conn, ride) == [None] * 30


def test_only_the_ride_that_needs_it_changes(conn):
    import db

    needs_it = _ride(conn, _gps())
    recorded = _ride(conn, _gps(speed=30.0))
    db.backfill_missing_speed(conn)
    assert _speeds(conn, needs_it)[5] == pytest.approx(25.0, abs=0.1)
    assert _speeds(conn, recorded) == [30.0] * 30


def test_running_it_again_changes_nothing(conn):
    import db

    ride = _ride(conn, _gps())
    db.backfill_missing_speed(conn)
    first = _speeds(conn, ride)
    assert db.backfill_missing_speed(conn) == 0
    assert _speeds(conn, ride) == first


def test_a_new_ride_is_stored_with_derived_speed(conn):
    import db

    ride = _ride(conn, [])
    points = [{"time_offset": t, "speed_kmh": None, "lat": lat, "lng": lng}
              for t, _, lat, lng in _gps()]
    db.upsert_streams(conn, ride, points)
    assert _speeds(conn, ride)[5] == pytest.approx(25.0, abs=0.1)
