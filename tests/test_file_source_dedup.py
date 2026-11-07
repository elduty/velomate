"""The `file` source (a FIT/GPX import) in the dedup path.

A file's identity is the SHA-256 of its bytes, held in activities.file_sha256.
It is tier 0, below every cloud source, so a cloud copy of the same ride is the
merge base and the file only fills the gaps its richer data can. These tests
need a real PostgreSQL because the behaviour under test is the partial-unique
index, the ON CONFLICT targets and the COALESCEs.

Run with:
    VELOMATE_TEST_DATABASE_URL=postgresql://... python3 -m pytest \
        tests/test_file_source_dedup.py -q
"""

import os

import pytest

from pg_reset import reset_schema

TEST_DB = os.environ.get("VELOMATE_TEST_DATABASE_URL")

pytestmark = pytest.mark.skipif(
    not TEST_DB,
    reason="needs a throwaway PostgreSQL in VELOMATE_TEST_DATABASE_URL",
)

START = "2026-06-01T08:00:00+00:00"
OTHER_START = "2026-06-03T08:00:00+00:00"
HASH_A = "a" * 64
HASH_B = "b" * 64


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


def _file(sha=HASH_A, **overrides) -> dict:
    """An activity as the file importer will hand it over: a hash, no cloud id."""
    data = {
        "name": "File ride", "date": START, "distance_m": 50000.0,
        "duration_s": 7200, "elevation_m": 500.0, "avg_hr": 140, "max_hr": 170,
        "avg_power": 200, "max_power": 450, "avg_cadence": 85,
        "avg_speed_kmh": 25.0, "calories": None, "suffer_score": None,
        "device": "garmin", "strava_type": "Ride", "trainer": False,
        "file_sha256": sha,
    }
    data.update(overrides)
    return data


def _strava(**overrides) -> dict:
    data = {
        "name": "Strava ride", "date": START, "distance_m": 50100.0,
        "duration_s": 7190, "elevation_m": 490.0, "avg_hr": None, "max_hr": None,
        "avg_power": None, "max_power": None, "avg_cadence": None,
        "avg_speed_kmh": 25.1, "calories": None, "suffer_score": 40,
        "device": "unknown", "strava_type": "Ride", "trainer": False,
        "strava_id": 987654321,
    }
    data.update(overrides)
    return data


def _all(conn, *columns):
    with conn.cursor() as cur:
        cur.execute(f"SELECT {', '.join(columns)} FROM activities ORDER BY id")
        return cur.fetchall()


def test_file_insert_and_resync_same_hash(conn):
    import db

    first, _ = db.upsert_activity(conn, _file())
    second, _ = db.upsert_activity(conn, _file(name="Renamed"))

    assert second == first
    assert _all(conn, "name", "file_sha256") == [("Renamed", HASH_A)]


def test_file_then_strava_strava_becomes_base(conn):
    import db

    file_id, _ = db.upsert_activity(conn, _file())
    strava_id, _ = db.upsert_activity(conn, _strava())

    assert strava_id == file_id
    assert _all(conn, "name", "strava_id", "file_sha256") == [
        ("Strava ride", 987654321, HASH_A)]


def test_strava_then_file_takes_skip_path(conn):
    import db

    strava_row, _ = db.upsert_activity(conn, _strava())
    file_row, streams_preserved = db.upsert_activity(conn, _file(calories=1100))

    assert file_row == strava_row
    assert streams_preserved is True
    assert _all(conn, "name", "strava_id", "file_sha256", "calories",
                "avg_power", "avg_hr", "avg_cadence") == [
        ("Strava ride", 987654321, HASH_A, 1100, 200, 140, 85)]


def test_two_file_rows_same_tier_richness_decides(conn):
    import db

    thin = _file(HASH_A, avg_power=None, avg_cadence=None, calories=None)
    rich = _file(HASH_B, name="Richer file")
    first, _ = db.upsert_activity(conn, thin)
    second, _ = db.upsert_activity(conn, rich)

    assert second == first
    assert _all(conn, "name", "file_sha256", "avg_power") == [
        ("Richer file", HASH_B, 200)]


def test_source_id_on_another_row_sees_file_hash(conn):
    import db

    owner, _ = db.upsert_activity(conn, _file(HASH_A, date=OTHER_START))
    other, _ = db.upsert_activity(conn, _strava())
    assert owner != other

    # Same bytes, but the file's start time now matches the Strava row.
    resynced, _ = db.upsert_activity(conn, _file(HASH_A, name="Moved", date=START))

    assert resynced == owner
    assert _all(conn, "id", "name", "strava_id", "file_sha256") == [
        (owner, "Moved", None, HASH_A), (other, "Strava ride", 987654321, None)]


def test_schema_has_ledger_tables_and_lock(conn):
    import db

    with conn.cursor() as cur:
        cur.execute("""SELECT table_name FROM information_schema.tables
                       WHERE table_name IN ('imported_files', 'imported_paths')
                       ORDER BY 1""")
        assert [r[0] for r in cur.fetchall()] == ["imported_files", "imported_paths"]

    with db.ingest_lock(conn):
        with db.ingest_lock(conn):  # nests
            pass
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM pg_locks WHERE locktype = 'advisory' "
                        "AND pid = pg_backend_pid()")
            assert cur.fetchone()[0] == 1
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM pg_locks WHERE locktype = 'advisory' "
                    "AND pid = pg_backend_pid()")
        assert cur.fetchone()[0] == 0


def test_same_file_again_on_cloud_row_changes_nothing(conn):
    import db

    cloud_id, _ = db.upsert_activity(conn, _strava())
    streams = [{"time_offset": i, "power": 150, "hr": 120, "cadence": 80,
                "speed_kmh": 25.0, "altitude_m": 10.0, "lat": 38.7 + i * 0.001,
                "lng": -9.14} for i in range(30)]
    db.upsert_streams(conn, cloud_id, streams)

    columns = ("id", "name", "duration_s", "device", "sport_type", "avg_hr",
               "avg_power", "max_hr", "max_power", "avg_cadence", "avg_speed_kmh",
               "calories", "is_indoor", "strava_id", "file_sha256")

    def picture():
        with conn.cursor() as cur:
            cur.execute("SELECT activity_id, time_offset, power, hr, lat "
                        "FROM activity_streams ORDER BY time_offset")
            return _all(conn, *columns), cur.fetchall()

    first_id, preserved = db.upsert_activity(conn, _file(calories=1100))
    assert (first_id, preserved) == (cloud_id, True)
    after_first = picture()
    assert after_first[0][0][-1] == HASH_A and len(after_first[1]) == 30

    again = db.upsert_activity(
        conn, _file(calories=1100, name="Other name", duration_s=7300,
                    device="edge", avg_power=300))

    assert again == (cloud_id, True)
    assert picture() == after_first


def _insert(conn, **ids):
    cols = ", ".join(ids)
    marks = ", ".join(["%s"] * len(ids))
    with conn.cursor() as cur:
        cur.execute(
            f"INSERT INTO activities (name, date, {cols}) "
            f"VALUES ('r', %s, {marks}) RETURNING id", (START, *ids.values()))
        return cur.fetchone()[0]


def test_rwgps_delete_of_file_held_row_unlinks(conn):
    import db
    _insert(conn, rwgps_id=5, file_sha256=HASH_A)
    _insert(conn, rwgps_id=6)

    assert db.handle_rwgps_deletion(conn, 5) == "unlinked"
    assert _all(conn, "rwgps_id", "file_sha256")[0] == (None, HASH_A)
    assert db.handle_rwgps_deletion(conn, 6) == "deleted"
    assert len(_all(conn, "id")) == 1


def test_icu_delete_of_file_held_row_unlinks(conn):
    import db
    _insert(conn, intervals_icu_id="i1", file_sha256=HASH_A)
    _insert(conn, intervals_icu_id="i2")

    assert db.handle_intervals_icu_deletion(conn, "i1") == "unlinked"
    assert _all(conn, "intervals_icu_id", "file_sha256")[0] == (None, HASH_A)
    assert db.handle_intervals_icu_deletion(conn, "i2") == "deleted"
    assert len(_all(conn, "id")) == 1


def test_reclassify_non_cycling_keeps_file_held_row(conn):
    from unittest.mock import MagicMock, patch
    import strava

    _insert(conn, strava_id=111, file_sha256=HASH_A)
    _insert(conn, strava_id=222)
    resp = MagicMock(status_code=200)
    resp.json.return_value = {"type": "Run", "name": "Jog"}

    with patch.object(strava, "_request_with_retry", return_value=resp), \
            patch.object(strava, "_get_token", return_value="t"), \
            patch.object(strava.time, "sleep"):
        strava.reclassify_activities(conn)

    assert _all(conn, "strava_id", "file_sha256") == [(None, HASH_A)]


# ---------------------------------------------------------------------------
# Stream gap fill
# ---------------------------------------------------------------------------

DERIVED_COLUMNS = (
    "tss", "np", "ef", "work_kj", "intensity_factor", "trimp",
    "variability_index", "aerobic_decoupling", "coasting_time_s",
    "kj_above_ftp", "polarization_index", "route_signature",
    "wbal_cp", "wbal_w_prime_j",
)


def _points(n=5, **channels) -> list:
    return [{"time_offset": i, **{k: v + i for k, v in channels.items()}}
            for i in range(n)]


def _gps_points(n=5, **channels) -> list:
    return [{**p, "lat": 38.7 + i * 1e-4, "lng": -9.1 + i * 1e-4}
            for i, p in enumerate(_points(n, **channels))]


def _stored(conn, activity_id):
    with conn.cursor() as cur:
        cur.execute("""SELECT time_offset, hr, power, cadence, altitude_m, lat, lng
                       FROM activity_streams WHERE activity_id = %s
                       ORDER BY time_offset""", (activity_id,))
        return cur.fetchall()


def _seed_derived(conn, activity_id):
    nums = ", ".join(f"{c} = 7" for c in DERIVED_COLUMNS if c != "route_signature")
    with conn.cursor() as cur:
        cur.execute(f"UPDATE activities SET {nums}, route_signature = 'stale' "
                    "WHERE id = %s", (activity_id,))


def test_stream_channels_counts_gps_as_speed():
    import db

    assert db.stream_channels([{"time_offset": 0, "lat": 0.0, "lng": 0.0}]) == {
        "gps", "speed"}
    assert db.stream_channels([{"time_offset": 0, "power": 0}]) == {"power"}


def test_file_with_power_replaces_powerless_streams(conn):
    import db

    rid, _ = db.upsert_activity(conn, _strava())
    db.upsert_streams(conn, rid, _gps_points(hr=120, altitude_m=10))
    _seed_derived(conn, rid)
    file_streams = _gps_points(hr=125, altitude_m=11, power=200)

    assert db.replace_streams_if_richer(conn, rid, file_streams) is True

    stored = _stored(conn, rid)
    assert [r[2] for r in stored] == [200, 201, 202, 203, 204]
    assert [r[1] for r in stored] == [125, 126, 127, 128, 129]
    with conn.cursor() as cur:
        cur.execute(f"SELECT name, {', '.join(DERIVED_COLUMNS)} FROM activities "
                    "WHERE id = %s", (rid,))
        row = cur.fetchone()
    assert row[0] == "Strava ride"
    assert all(v is None for v in row[1:])


def test_file_missing_a_stored_channel_leaves_streams(conn):
    import db

    rid, _ = db.upsert_activity(conn, _strava())
    db.upsert_streams(conn, rid, _points(hr=120, cadence=80))
    before = _stored(conn, rid)
    _seed_derived(conn, rid)

    assert db.replace_streams_if_richer(
        conn, rid, _points(hr=150, power=250)) is False

    assert _stored(conn, rid) == before
    with conn.cursor() as cur:
        cur.execute("SELECT tss FROM activities WHERE id = %s", (rid,))
        assert cur.fetchone()[0] == 7


def test_file_adding_nothing_leaves_streams(conn):
    import db

    rid, _ = db.upsert_activity(conn, _strava())
    db.upsert_streams(conn, rid, _points(hr=120, power=200))
    before = _stored(conn, rid)

    assert db.replace_streams_if_richer(
        conn, rid, _points(hr=150, power=250)) is False

    assert _stored(conn, rid) == before
