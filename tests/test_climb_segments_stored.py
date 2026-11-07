"""What climb segment discovery and matching leave in the tables.

Rides go through the real write path and a real recalculation, so the climbs that
seed a segment come out of the real detector. The road is simple enough to check by
hand: 1 km flat, 2 km at 6%, 1 km flat, running due north.
"""
import os
from datetime import date, timedelta
from unittest.mock import patch

import pytest

from pg_reset import reset_schema

TEST_DB = os.environ.get("VELOMATE_TEST_DATABASE_URL")

pytestmark = pytest.mark.skipif(
    not TEST_DB,
    reason="needs a throwaway PostgreSQL in VELOMATE_TEST_DATABASE_URL",
)

LAT0, LNG = 38.70, -9.14
M_PER_DEG_LAT = 111195.0
CLIMB_START_M, CLIMB_END_M, ROAD_END_M = 1000, 3000, 4000
GRADE = 0.06
BASE_ALT = 100.0


def _altitude(metres):
    climbed = min(max(metres - CLIMB_START_M, 0), CLIMB_END_M - CLIMB_START_M)
    return BASE_ALT + climbed * GRADE


def _stream(speed_ms=5.0, lng=LNG, reverse=False, watts=200, hr=140):
    """One sample a second along the road. At 5 m/s the climb takes 400 s."""
    seconds = int(ROAD_END_M / speed_ms)
    points = []
    for second in range(seconds):
        metres = second * speed_ms
        if reverse:
            metres = ROAD_END_M - metres
        points.append({
            "time_offset": second, "power": watts, "hr": hr, "cadence": 85,
            "speed_kmh": speed_ms * 3.6, "altitude_m": _altitude(metres),
            "lat": LAT0 + metres / M_PER_DEG_LAT, "lng": lng,
        })
    return points


def _ride_data(days_ago, stream, **columns):
    return {
        "name": f"Ride {days_ago}", "date": f"{date.today() - timedelta(days=days_ago)}T12:00:00+00:00",
        "duration_s": len(stream), "distance_m": float(ROAD_END_M), "elevation_m": 120.0,
        "avg_hr": 140, "max_hr": 150, "avg_power": 200, "max_power": 200, "avg_cadence": 85,
        "avg_speed_kmh": 18.0, "calories": None, "suffer_score": None, "device": "karoo",
        "strava_type": "Ride", "trainer": False, "intervals_icu_id": f"t{days_ago}",
        "intervals_icu_analyzed": f"{date.today()}T00:00:00+00:00", **columns,
    }


def _ride(conn, days_ago, **stream_args):
    import db as velomate_db

    stream = _stream(**stream_args)
    return velomate_db.upsert_activity(conn, _ride_data(days_ago, stream), streams=stream)[0]


def _recalculate(conn):
    import db as velomate_db
    import fitness

    velomate_db.set_sync_state(conn, "metrics_version", fitness.METRICS_VERSION)
    fitness.recalculate_fitness(conn)


def _rows(conn, sql, *params) -> list:
    with conn.cursor() as cur:
        cur.execute(sql, params)
        return cur.fetchall()


def _execute(conn, sql, *params):
    with conn.cursor() as cur:
        cur.execute(sql, params)


def _efforts(conn):
    """{activity id: elapsed seconds}, in a form a test can compare."""
    return dict(_rows(conn, "SELECT activity_id, elapsed_s FROM climb_efforts"))


@pytest.fixture(autouse=True)
def _no_ambient_config(monkeypatch):
    for name in ("VELOMATE_FTP", "VELOMATE_WEIGHT", "VELOMATE_MAX_HR", "VELOMATE_RESTING_HR",
                 "STRAVA_CLIENT_ID", "STRAVA_CLIENT_SECRET", "STRAVA_REFRESH_TOKEN"):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture()
def conn():
    import psycopg2

    if not hasattr(psycopg2, "__file__"):
        pytest.skip("psycopg2 is mocked by another module in this pytest session")

    import db as velomate_db

    connection = psycopg2.connect(TEST_DB)
    connection.autocommit = True
    reset_schema(connection)
    velomate_db.create_schema(connection)
    yield connection
    connection.close()


@pytest.fixture()
def climb(conn):
    """Two rides up the road, the second slower: (fast ride id, slow ride id)."""
    fast = _ride(conn, 3, speed_ms=5.0)
    slow = _ride(conn, 2, speed_ms=4.0)
    _recalculate(conn)
    return fast, slow


# ---------------------------------------------------------------------------
# Discovery and matching
# ---------------------------------------------------------------------------

def test_two_rides_up_the_same_climb_make_one_segment(conn, climb):
    """The climb is 2 km gaining 120 m at 6%. The detector picks the ends, so its
    length and gain are close to that rather than equal."""
    segments = _rows(conn, "SELECT length_m, gain_m, avg_grade, category FROM climb_segments")

    assert len(segments) == 1
    length_m, gain_m, avg_grade, category = segments[0]
    assert length_m == pytest.approx(2000, abs=150)
    assert gain_m == pytest.approx(120, abs=10)
    assert avg_grade == pytest.approx(6.0, abs=0.5)
    assert category == "Cat 4"


def test_each_ride_is_timed_on_its_own_pace(conn, climb):
    """2 km takes 400 s at 5 m/s and 500 s at 4 m/s, over the same two points."""
    fast, slow = climb
    length_m = _rows(conn, "SELECT length_m FROM climb_segments")[0][0]
    efforts = dict((row[0], row[1:]) for row in _rows(conn, """
        SELECT activity_id, elapsed_s, moving_s, avg_speed_kmh, avg_hr, avg_power
        FROM climb_efforts"""))

    assert efforts[fast][0] == pytest.approx(length_m / 5.0, abs=8)
    assert efforts[slow][0] == pytest.approx(length_m / 4.0, abs=8)
    assert efforts[fast][1] == efforts[fast][0], "this ride never stops"
    assert efforts[fast][2] == pytest.approx(18.0, abs=0.5)
    assert efforts[slow][2] == pytest.approx(14.4, abs=0.5)
    assert efforts[fast][3:] == (140, 200)


def test_one_ride_is_not_a_recurring_climb(conn):
    _ride(conn, 3)
    _recalculate(conn)

    assert _rows(conn, "SELECT COUNT(*) FROM climb_segments") == [(0,)]


def test_a_ride_on_another_road_is_not_matched(conn, climb):
    """About 870 m west of the climb, with the same profile."""
    elsewhere = _ride(conn, 1, lng=LNG - 0.01)

    _recalculate(conn)

    assert elsewhere not in _efforts(conn)
    assert set(_efforts(conn)) == set(climb)


def test_riding_it_downhill_is_not_an_effort(conn, climb):
    descent = _ride(conn, 1, reverse=True)

    _recalculate(conn)

    assert descent not in _efforts(conn)


def test_a_later_ride_is_matched_on_the_existing_segment(conn, climb):
    before = _rows(conn, "SELECT id, length_m, gain_m FROM climb_segments")

    later = _ride(conn, 1, speed_ms=2.5)
    _recalculate(conn)

    assert _rows(conn, "SELECT id, length_m, gain_m FROM climb_segments") == before
    assert _efforts(conn)[later] > max(_efforts(conn)[ride] for ride in climb)


def test_a_second_recalculation_stores_nothing_new(conn, climb):
    before = _rows(conn, "SELECT id, segment_id, activity_id FROM climb_efforts ORDER BY id")

    _recalculate(conn)

    assert _rows(conn, "SELECT id, segment_id, activity_id FROM climb_efforts ORDER BY id") == before
    assert _rows(conn, "SELECT COUNT(*) FROM climb_segments") == [(1,)]


def test_a_scanned_ride_is_not_read_again(conn, climb):
    """Every recalculation would otherwise reload every nearby ride's stream. A
    second effort row can't show that, because the unique key absorbs it."""
    import segments

    assert sorted(_rows(conn, "SELECT activity_id FROM climb_segment_scans")) == sorted(
        (ride,) for ride in climb)

    with patch.object(segments, "_load_track", wraps=segments._load_track) as load_track:
        assert segments.match_efforts(conn) == 0

    load_track.assert_not_called()


def test_a_climb_with_no_altitude_does_not_stop_discovery(conn):
    """The column allows NULL, and one sorted against the rest would raise, which the
    recalculation swallows, so no segment would ever appear again."""
    first = _ride(conn, 3)
    _ride(conn, 2)
    _recalculate(conn)
    _execute(conn, "DELETE FROM climb_segments")
    _execute(conn, """
        INSERT INTO ride_climbs (activity_id, start_offset, end_offset, gain_m, length_m,
                                 start_alt, peak_alt, source)
        VALUES (%s, 220, 590, 110, 1900, NULL, NULL, 'detected')""", first)

    _recalculate(conn)

    assert _rows(conn, "SELECT COUNT(*) FROM climb_segments") == [(1,)]


# ---------------------------------------------------------------------------
# Replacing a ride's streams
# ---------------------------------------------------------------------------

def _replace_through_upsert_streams(conn, ride, _days_ago, stream):
    import db as velomate_db

    velomate_db.upsert_streams(conn, ride, stream)


def _replace_through_a_reanalysis(conn, _ride_id, days_ago, stream):
    import db as velomate_db

    data = _ride_data(days_ago, stream, intervals_icu_analyzed=f"{date.today()}T06:00:00+00:00")
    velomate_db.upsert_activity(conn, data, streams=stream)


def _replace_through_a_merge(conn, ride, days_ago, stream):
    """A copy from a higher tier onto a row that came from a lower one."""
    import db as velomate_db

    _execute(conn, "UPDATE activities SET strava_id = %s, intervals_icu_id = NULL WHERE id = %s",
             555, ride)
    velomate_db.upsert_activity(conn, _ride_data(days_ago, stream), streams=stream)


@pytest.mark.parametrize("replace", [_replace_through_upsert_streams, _replace_through_a_reanalysis,
                                     _replace_through_a_merge],
                         ids=["upsert_streams", "reanalysis", "merge"])
def test_new_streams_are_timed_again(conn, climb, replace):
    """Every way a stream changes goes through _replace_streams, so a time taken on
    the old one can't survive, whichever path replaced it."""
    fast, slow = climb
    slow_before = _efforts(conn)[slow]
    assert _efforts(conn)[fast] < 500

    replace(conn, fast, 3, _stream(speed_ms=2.5))
    assert fast not in _efforts(conn), "the time was taken on the stream that just went"

    _recalculate(conn)

    assert _efforts(conn)[fast] == pytest.approx(_rows(
        conn, "SELECT length_m FROM climb_segments")[0][0] / 2.5, abs=8)
    assert _efforts(conn)[slow] == slow_before, "only the replaced ride is matched again"


def test_a_failed_stream_write_keeps_the_efforts(conn, climb):
    """The reset shares the stream write's transaction."""
    import db as velomate_db

    fast, _slow = climb
    before = _efforts(conn)

    with pytest.raises(Exception):
        velomate_db.upsert_streams(conn, fast, [{"time_offset": "not a number"}])

    assert _efforts(conn) == before


def test_deleting_a_ride_takes_its_efforts_and_keeps_the_segment(conn, climb):
    import db as velomate_db

    fast, slow = climb
    velomate_db.handle_intervals_icu_deletion(conn, "t3")

    assert set(_efforts(conn)) == {slow}
    assert _rows(conn, "SELECT COUNT(*) FROM climb_segment_scans WHERE activity_id = %s", fast) == [(0,)]
    assert _rows(conn, "SELECT COUNT(*) FROM climb_segments") == [(1,)]


# ---------------------------------------------------------------------------
# A segment outlives what seeded it
# ---------------------------------------------------------------------------

def test_a_metrics_version_bump_keeps_segments_and_their_names(conn, climb):
    """Detected climbs are rebuilt on a bump, so a segment that followed them would
    move, and a hand-set name would go with it."""
    import db as velomate_db
    import fitness

    _execute(conn, "UPDATE climb_segments SET name = 'Serra climb'")
    before = _rows(conn, "SELECT id, name, start_lat, end_lat FROM climb_segments")
    efforts = _efforts(conn)
    velomate_db.set_sync_state(conn, "metrics_version", "0")

    fitness.recalculate_fitness(conn)

    assert _rows(conn, "SELECT id, name, start_lat, end_lat FROM climb_segments") == before
    assert _efforts(conn) == efforts


def test_a_failure_in_matching_does_not_stop_the_recalculation(conn):
    _ride(conn, 3)
    _ride(conn, 2)

    with patch("segments.match_efforts", side_effect=RuntimeError("no")):
        _recalculate(conn)

    assert _rows(conn, "SELECT COUNT(*) FROM athlete_stats WHERE ctl > 0")[0][0] > 0
    assert _rows(conn, "SELECT COUNT(*) FROM climb_segments") == [(1,)], "discovery ran first"
