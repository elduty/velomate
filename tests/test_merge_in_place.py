"""A cross-source merge must update the existing row, never replace it.

The merge used to DELETE the row and INSERT a new one. That cascaded away
ride_climbs (including Strava segments that cannot be re-fetched), dropped every
column only the losing copy had, re-stamped ride_weight and ride_ftp, and handed
the ride a new id, which broke Activity Details links and the "activity ids
never change" assumption route-id stability rests on.

What survives a merge, and why:

    activities row        kept. Winner's base columns, loser fills the gaps.
    id, route_id          kept.
    ride_weight, ride_ftp kept. Neither depends on this ride's streams.
    activity_streams      replaced by the incoming copy's, kept if it has none.
    ride_climbs (strava)  kept. External data with NULL offsets, so it is not
                          tied to the stream and may be impossible to re-fetch.
    ride_climbs (other)   rebuilt. Detected from the stream that just changed.
    ride_intervals        rebuilt. Wholly derived from the same stream.
    derived columns       cleared, because recalculation only fills NULLs.

These tests need a real PostgreSQL because the behaviour under test *is* the
SQL: the cascades, the COALESCEs and the transaction. A mocked cursor would only
re-assert the code.

Run with:
    VELOMATE_TEST_DATABASE_URL=postgresql://... python3 -m pytest \
        tests/test_merge_in_place.py -q
"""

import os
from datetime import datetime

import pytest

from pg_reset import reset_schema

TEST_DB = os.environ.get("VELOMATE_TEST_DATABASE_URL")

pytestmark = pytest.mark.skipif(
    not TEST_DB,
    reason="needs a throwaway PostgreSQL in VELOMATE_TEST_DATABASE_URL",
)

START = "2026-06-01T08:00:00+00:00"
ANALYZED = "2026-06-01T12:00:00+00:00"

# Every column the METRICS_VERSION reset clears, minus ride_ftp. Recalculation
# only fills NULLs (TSS and IF excepted), so one left behind would keep
# describing the stream the merge replaced.
DERIVED_COLUMNS = (
    "tss", "np", "ef", "work_kj", "intensity_factor", "trimp",
    "variability_index", "aerobic_decoupling", "coasting_time_s",
    "kj_above_ftp", "polarization_index", "route_signature",
    "wbal_cp", "wbal_w_prime_j",
)


def _seed_row(conn, **overrides) -> int:
    """Insert an activity directly, bypassing the code under test."""
    row = {
        "name": "Existing", "date": START, "distance_m": 50000.0,
        "duration_s": 7200, "elevation_m": 500.0, "avg_hr": 140, "max_hr": 170,
        "avg_power": 180, "max_power": 400, "avg_cadence": 80,
        "avg_speed_kmh": 25.0, "calories": 1100, "device": "watch",
        "is_indoor": False, "sport_type": "cycling_outdoor",
    }
    row.update(overrides)
    columns = ", ".join(row)
    placeholders = ", ".join(f"%({c})s" for c in row)
    with conn.cursor() as cur:
        cur.execute(
            f"INSERT INTO activities ({columns}) VALUES ({placeholders}) RETURNING id",
            row)
        return cur.fetchone()[0]


def _incoming(**overrides) -> dict:
    """An activity as a source's parser hands it to upsert_activity."""
    data = {
        "name": "Incoming", "date": START, "distance_m": 50200.0,
        "duration_s": 7180, "elevation_m": 480.0, "avg_hr": 150, "max_hr": 175,
        "avg_power": 210, "max_power": 450, "avg_cadence": 85,
        "avg_speed_kmh": 25.2, "calories": 1200, "suffer_score": None,
        "device": "karoo", "strava_type": "Ride", "trainer": False,
    }
    data.update(overrides)
    return data


def _stream(samples=120, watts=200, hr=140, lat0=38.7, lng=-9.14, alt=100.0):
    """A 1 Hz stream. `watts` is a constant, or a function of the second."""
    power = watts if callable(watts) else (lambda _second: watts)
    return [{
        "time_offset": i, "power": power(i), "hr": hr, "cadence": 85,
        "speed_kmh": 25.0, "altitude_m": alt, "lat": lat0 + i * 0.001, "lng": lng,
    } for i in range(samples)]


def _ingest(conn, data, streams):
    """Exactly what the intervals.icu and RWGPS sync loops do with one ride."""
    import db as velomate_db

    activity_id, streams_preserved = velomate_db.upsert_activity(
        conn, data, streams=streams)
    if streams and not streams_preserved:
        velomate_db.upsert_streams(conn, activity_id, streams)
    return activity_id


def _recalculate(conn):
    """Recalculate the way a running deployment does.

    A fresh database has no stored metrics_version, so recalculate_fitness would
    take the first run for a version bump and reset every derived column and
    ride_ftp itself. That would refill the very columns these tests need the
    merge to have cleared, and pass whether it did or not.
    """
    import db as velomate_db
    import fitness

    velomate_db.set_sync_state(conn, "metrics_version", fitness.METRICS_VERSION)
    fitness.recalculate_fitness(conn)


def _row(conn, activity_id, *columns) -> dict:
    with conn.cursor() as cur:
        cur.execute(f"SELECT {', '.join(columns)} FROM activities WHERE id = %s",
                    (activity_id,))
        values = cur.fetchone()
    return dict(zip(columns, values)) if values else {}


def _scalar(conn, sql, *params):
    with conn.cursor() as cur:
        cur.execute(sql, params)
        return cur.fetchone()[0]


def _rows(conn, sql, *params) -> list:
    with conn.cursor() as cur:
        cur.execute(sql, params)
        return cur.fetchall()


def _seed_climb(conn, activity_id, source, category, segment_name=None, gain=120):
    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO ride_climbs (activity_id, gain_m, category, source, segment_name)
            VALUES (%s, %s, %s, %s, %s)
        """, (activity_id, gain, category, source, segment_name))


def _seed_interval(conn, activity_id, classification="stale"):
    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO ride_intervals
                (activity_id, start_offset_s, duration_s, classification)
            VALUES (%s, 0, 60, %s)
        """, (activity_id, classification))


def _seed_derived(conn, activity_id):
    """Stamp every derived column with a value recalculation cannot produce."""
    with conn.cursor() as cur:
        cur.execute(f"""
            UPDATE activities
            SET {', '.join(f"{c} = 999" for c in DERIVED_COLUMNS if c != 'route_signature')},
                route_signature = 'stale'
            WHERE id = %s
        """, (activity_id,))


def _everything(conn) -> dict:
    """A full picture of the ride, to prove a rollback left nothing behind."""
    return {
        "activities": _rows(conn, "SELECT * FROM activities ORDER BY id"),
        "streams": _rows(conn, """
            SELECT activity_id, time_offset, power, hr, lat, lng, w_bal
            FROM activity_streams ORDER BY activity_id, time_offset"""),
        "intervals": _rows(conn, "SELECT * FROM ride_intervals ORDER BY id"),
        "climbs": _rows(conn, "SELECT * FROM ride_climbs ORDER BY id"),
    }


@pytest.fixture(autouse=True)
def _no_ambient_config(monkeypatch):
    """Recalculation reads these, and a developer's shell may set them."""
    for name in ("VELOMATE_FTP", "VELOMATE_WEIGHT", "VELOMATE_MAX_HR",
                 "VELOMATE_RESTING_HR", "STRAVA_CLIENT_ID",
                 "STRAVA_CLIENT_SECRET", "STRAVA_REFRESH_TOKEN"):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture()
def conn():
    import psycopg2

    # Other test modules install a MagicMock for psycopg2 in the shared pytest
    # process. A mocked module has no __file__. See test_route_id_stability.py.
    if not hasattr(psycopg2, "__file__"):
        pytest.skip(
            "psycopg2 is mocked by another module in this pytest session; run "
            "this file on its own: python -m pytest tests/test_merge_in_place.py"
        )

    import db as velomate_db

    connection = psycopg2.connect(TEST_DB)
    connection.autocommit = True  # what db.get_connection hands production
    reset_schema(connection)
    velomate_db.create_schema(connection)
    yield connection
    connection.close()


# ---------------------------------------------------------------------------
# Identity
# ---------------------------------------------------------------------------

def test_the_activity_id_does_not_change(conn):
    """Replaced: a fresh serial id. In place: the id Grafana already links to."""
    existing = _seed_row(conn, strava_id=555)

    merged = _ingest(conn, _incoming(intervals_icu_id="i1"), _stream())

    assert merged == existing
    assert _scalar(conn, "SELECT COUNT(*) FROM activities") == 1
    assert _row(conn, existing, "strava_id", "intervals_icu_id") == {
        "strava_id": 555, "intervals_icu_id": "i1"}


def test_source_ids_accumulate_on_one_row(conn):
    """Each source merges in turn, and each one's id lands on the same row."""
    existing = _seed_row(conn, rwgps_id=777, avg_power=None, avg_hr=None)

    first = _ingest(conn, _incoming(strava_id=555), _stream())
    second = _ingest(conn, _incoming(intervals_icu_id="i1"), _stream())

    assert first == second == existing
    assert _row(conn, existing, "rwgps_id", "strava_id", "intervals_icu_id") == {
        "rwgps_id": 777, "strava_id": 555, "intervals_icu_id": "i1"}


# ---------------------------------------------------------------------------
# Precedence: the base is chosen exactly as before, in both directions
# ---------------------------------------------------------------------------

def test_existing_lower_tier_takes_the_incoming_base(conn):
    """intervals.icu arrives over a Strava row: its columns become the base,
    even though both copies are complete and the Strava values differ."""
    existing = _seed_row(conn, strava_id=555, name="Strava name", device="watch",
                         is_indoor=True, sport_type="cycling_indoor")
    data = _incoming(intervals_icu_id="i1", name="ICU name",
                     date="2026-06-01T08:02:00+00:00", intervals_icu_analyzed=ANALYZED)

    merged = _ingest(conn, data, _stream())

    assert merged == existing
    base = ("name", "device", "duration_s", "distance_m", "elevation_m", "avg_hr",
            "max_hr", "avg_power", "max_power", "avg_cadence", "avg_speed_kmh",
            "calories")
    assert _row(conn, existing, *base) == {column: data[column] for column in base}
    row = _row(conn, existing, "date", "intervals_icu_analyzed", "is_indoor",
               "sport_type", "synced_at")
    assert row["date"] == datetime.fromisoformat(data["date"])
    assert row["intervals_icu_analyzed"] == datetime.fromisoformat(ANALYZED)
    assert (row["is_indoor"], row["sport_type"]) == (False, "cycling_outdoor")
    assert row["synced_at"] is not None


def test_existing_higher_tier_keeps_its_own_base(conn):
    """A richer Strava copy arrives over an intervals.icu row: nothing about the
    row's base changes, its streams stay, and the Strava id is still recorded."""
    existing = _seed_row(conn, intervals_icu_id="i1", name="ICU name", device="karoo",
                         avg_cadence=None, calories=None)
    original_stream = _stream(watts=150)
    import db as velomate_db
    velomate_db.upsert_streams(conn, existing, original_stream)

    activity_id, streams_preserved = velomate_db.upsert_activity(
        conn, _incoming(strava_id=555, name="Strava name"), streams=_stream(watts=300))

    assert (activity_id, streams_preserved) == (existing, True)
    row = _row(conn, existing, "name", "device", "duration_s", "strava_id",
               "avg_cadence", "calories")
    assert row == {"name": "ICU name", "device": "karoo", "duration_s": 7200,
                   "strava_id": 555, "avg_cadence": 85, "calories": 1200}
    assert _scalar(conn, "SELECT MAX(power) FROM activity_streams") == 150


@pytest.mark.parametrize("existing_power, incoming_power, base", [
    (None, 210, "Incoming"),   # thin row, rich arrival: the arrival wins
    (210, None, "Existing"),   # rich row, thin arrival: the row wins
])
def test_richness_breaks_the_tie_within_a_tier(conn, existing_power, incoming_power, base):
    """Strava and RWGPS share a tier, so the richer copy is the base."""
    existing = _seed_row(conn, rwgps_id=777, avg_power=existing_power)

    merged = _ingest(conn, _incoming(strava_id=555, avg_power=incoming_power,
                                     avg_cadence=None, calories=None,
                                     elevation_m=None), _stream())

    assert merged == existing
    assert _row(conn, existing, "name") == {"name": base}


def test_tier_is_the_max_across_a_rows_source_ids(conn):
    """A row with an intervals.icu id outranks Strava even though it also has a
    tier-1 id. Taking the lowest, or the first found, would demote it."""
    existing = _seed_row(conn, rwgps_id=777, intervals_icu_id="i1", name="Keep me")

    _ingest(conn, _incoming(strava_id=555), _stream())

    assert _row(conn, existing, "name", "strava_id") == {
        "name": "Keep me", "strava_id": 555}


# ---------------------------------------------------------------------------
# Columns
# ---------------------------------------------------------------------------

def test_columns_only_the_loser_had_are_kept(conn):
    """The audit's list. intervals.icu supplies none of these on this copy, so a
    replaced row stores NULL for all of them."""
    existing = _seed_row(conn, strava_id=555, max_hr=188, max_power=900,
                         avg_cadence=88, calories=1500, suffer_score=77,
                         elevation_m=512.0, komoot_tour_id=42)

    _ingest(conn, _incoming(intervals_icu_id="i1", max_hr=None, max_power=None,
                            avg_cadence=None, calories=None, elevation_m=None),
            _stream())

    assert _row(conn, existing, "max_hr", "max_power", "avg_cadence", "calories",
                "suffer_score", "elevation_m", "komoot_tour_id") == {
        "max_hr": 188, "max_power": 900, "avg_cadence": 88, "calories": 1500,
        "suffer_score": 77, "elevation_m": 512.0, "komoot_tour_id": 42}


def test_a_zero_or_blank_from_the_winner_is_a_gap_not_a_measurement(conn):
    """intervals.icu and RWGPS coerce an absent value to 0, so a plain COALESCE
    would overwrite the loser's real elevation with it."""
    existing = _seed_row(conn, strava_id=555, name="Keep me", device="watch",
                         elevation_m=512.0, avg_speed_kmh=25.0, calories=1500,
                         max_power=900, max_hr=188, avg_cadence=88)

    _ingest(conn, _incoming(intervals_icu_id="i1", name="", device="", elevation_m=0,
                            avg_speed_kmh=0.0, calories=0, max_power=0, max_hr=0,
                            avg_cadence=0), _stream())

    assert _row(conn, existing, "name", "device", "elevation_m", "avg_speed_kmh",
                "calories", "max_power", "max_hr", "avg_cadence") == {
        "name": "Keep me", "device": "watch", "elevation_m": 512.0,
        "avg_speed_kmh": 25.0, "calories": 1500, "max_power": 900, "max_hr": 188,
        "avg_cadence": 88}


def test_an_undetected_device_is_a_gap(conn):
    """Every source reports "unknown" when it can't name the device, so a winner
    that couldn't must not overwrite the device the loser did name."""
    existing = _seed_row(conn, strava_id=555, device="karoo")

    _ingest(conn, _incoming(intervals_icu_id="i1", device="unknown"), _stream())

    assert _row(conn, existing, "device") == {"device": "karoo"}


def test_an_undetected_device_still_lands_on_a_row_with_none(conn):
    existing = _seed_row(conn, strava_id=555, device=None)

    _ingest(conn, _incoming(intervals_icu_id="i1", device="unknown"), _stream())

    assert _row(conn, existing, "device") == {"device": "unknown"}


def test_a_zero_suffer_score_is_a_measurement(conn):
    """The one column where 0 is a real value: an easy ride scores 0."""
    existing = _seed_row(conn, rwgps_id=777, suffer_score=40, avg_power=None)

    _ingest(conn, _incoming(strava_id=555, suffer_score=0), _stream())

    assert _row(conn, existing, "suffer_score") == {"suffer_score": 0}


def test_ride_weight_and_ride_ftp_survive_a_recalculation(conn, monkeypatch):
    """Both describe the rider on the day, not this copy's streams. Replacing
    the row left them NULL, so recalculation re-stamped today's values and
    silently rewrote a historical W/kg and TSS."""

    existing = _seed_row(conn, strava_id=555, ride_weight=70.0, ride_ftp=250.0)
    monkeypatch.setenv("VELOMATE_WEIGHT", "90")
    monkeypatch.setenv("VELOMATE_FTP", "300")

    _ingest(conn, _incoming(intervals_icu_id="i1"), _stream(samples=3600, watts=250))
    _recalculate(conn)

    row = _row(conn, existing, "ride_weight", "ride_ftp", "intensity_factor")
    assert row["ride_weight"] == 70.0
    assert row["ride_ftp"] == 250.0
    assert row["intensity_factor"] == pytest.approx(1.0, abs=0.01), (
        "IF must be scored against the ride's own FTP, not the configured 300")


def test_derived_columns_are_cleared_so_they_recompute(conn):
    """Recalculation only fills NULLs, so a value left in place describes the
    stream the merge just replaced. Seeded with 999, which no recompute yields."""

    existing = _seed_row(conn, strava_id=555)
    _seed_derived(conn, existing)

    _ingest(conn, _incoming(intervals_icu_id="i1"), _stream(samples=600, watts=200))

    assert _row(conn, existing, *DERIVED_COLUMNS) == dict.fromkeys(DERIVED_COLUMNS)

    _recalculate(conn)

    row = _row(conn, existing, "np", "work_kj", "route_signature")
    assert row["np"] == pytest.approx(200, abs=5)
    assert row["work_kj"] == pytest.approx(120, abs=1)
    assert row["route_signature"] not in (None, "stale")


# ---------------------------------------------------------------------------
# Child rows
# ---------------------------------------------------------------------------

def test_streams_are_replaced_by_the_incoming_copy(conn):
    import db as velomate_db

    existing = _seed_row(conn, strava_id=555)
    velomate_db.upsert_streams(conn, existing, _stream(samples=50, watts=150))

    # Called directly: the caller writing them again would hide a merge that
    # did not, and True is what stops the caller doing exactly that.
    activity_id, streams_preserved = velomate_db.upsert_activity(
        conn, _incoming(intervals_icu_id="i1"), streams=_stream(samples=80, watts=300))

    assert (activity_id, streams_preserved) == (existing, True)
    assert _rows(conn, """
        SELECT COUNT(*), MIN(power), MAX(power) FROM activity_streams
        WHERE activity_id = %s""", existing) == [(80, 300, 300)]


def test_streams_are_kept_when_the_incoming_copy_has_none(conn):
    """Replacing with nothing would leave the merged ride with no data at all."""
    import db as velomate_db

    existing = _seed_row(conn, strava_id=555)
    velomate_db.upsert_streams(conn, existing, _stream(samples=50, watts=150))

    activity_id, streams_preserved = velomate_db.upsert_activity(
        conn, _incoming(intervals_icu_id="i1"))

    assert (activity_id, streams_preserved) == (existing, False)
    assert _scalar(conn, "SELECT COUNT(*) FROM activity_streams WHERE activity_id = %s",
                   existing) == 50


def test_strava_segment_climbs_survive(conn):
    """The audit's headline loss: 12 rides lost their segments this way, and
    they cannot be re-fetched while Strava is gated off."""
    existing = _seed_row(conn, strava_id=555)
    _seed_climb(conn, existing, "strava", "Cat 3", segment_name="Serra Climb")
    _seed_climb(conn, existing, "strava", "none")  # the "no segments" sentinel

    _ingest(conn, _incoming(intervals_icu_id="i1"), _stream())

    assert _rows(conn, """
        SELECT category, segment_name FROM ride_climbs
        WHERE activity_id = %s AND source = 'strava' ORDER BY category""", existing) == [
        ("Cat 3", "Serra Climb"), ("none", None)]


def test_detected_climbs_are_rebuilt_from_the_new_stream(conn):
    """detect_climbs_for_rides skips a ride with any detected or 'none' row, so
    these have to go or detection never reruns against the replaced stream. The
    Strava row stays through the rebuild."""

    existing = _seed_row(conn, strava_id=555)
    _seed_climb(conn, existing, "strava", "Cat 3", segment_name="Serra Climb")
    _seed_climb(conn, existing, "detected", "Cat 4", gain=999)
    _seed_climb(conn, existing, "none", "none")

    _ingest(conn, _incoming(intervals_icu_id="i1"), _stream())

    assert _rows(conn, "SELECT source FROM ride_climbs WHERE activity_id = %s",
                 existing) == [("strava",)]

    _recalculate(conn)

    rows = _rows(conn, "SELECT source, gain_m FROM ride_climbs WHERE activity_id = %s",
                 existing)
    assert ("strava", 120) in rows
    assert {source for source, _gain in rows} & {"detected", "none"}, (
        "detection must have rerun, and left at least its sentinel")
    assert ("detected", 999) not in rows


def test_ride_intervals_are_rebuilt_from_the_new_stream(conn, monkeypatch):
    """Step 2.5 only detects on a ride with no rows, and every row indexes into
    the old stream, so they are cleared rather than preserved."""

    existing = _seed_row(conn, strava_id=555, ride_ftp=250.0)
    _seed_interval(conn, existing, classification="stale")

    hard_effort = _stream(samples=600, watts=lambda s: 280 if 300 <= s < 540 else 120)
    _ingest(conn, _incoming(intervals_icu_id="i1"), hard_effort)

    assert _scalar(conn, "SELECT COUNT(*) FROM ride_intervals WHERE activity_id = %s",
                   existing) == 0

    _recalculate(conn)

    assert _rows(conn, """
        SELECT start_offset_s, duration_s, classification FROM ride_intervals
        WHERE activity_id = %s""", existing) == [(300, 240, "vo2")]


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

def test_route_reference_and_founding_activity_still_resolve(conn):
    """Route identity is keyed on the founding ride's id. Replacing the founder
    leaves founding_activity_id dangling, and it is a plain INTEGER, so nothing
    objects until the rebuild quietly renumbers the route and every saved
    var-route=<id> URL goes empty."""
    import db as velomate_db

    founder = _seed_row(conn, strava_id=1, date="2026-04-26T10:00:00+00:00")
    follower = _seed_row(conn, strava_id=2, date="2026-05-03T10:00:00+00:00")
    for ride in (founder, follower):
        velomate_db.upsert_streams(conn, ride, _stream(samples=150))
    _recalculate(conn)

    route = _row(conn, founder, "route_id")["route_id"]
    assert route is not None, "fixture is wrong: the rides did not form a route"
    assert _row(conn, follower, "route_id")["route_id"] == route

    merged = _ingest(conn, _incoming(intervals_icu_id="i1", date="2026-04-26T10:00:00+00:00"),
                     _stream(samples=150))

    # Resolves straight after the merge, before anything has rebuilt the table.
    assert merged == founder
    assert _row(conn, founder, "route_id")["route_id"] == route
    assert _rows(conn, """
        SELECT r.id FROM ride_routes r
        JOIN activities a ON a.id = r.founding_activity_id
        WHERE a.id = %s""", founder) == [(route,)]

    _recalculate(conn)

    assert _row(conn, founder, "route_id")["route_id"] == route
    assert _row(conn, follower, "route_id")["route_id"] == route
    assert _rows(conn, "SELECT id, founding_activity_id FROM ride_routes") == [
        (route, founder)]


# ---------------------------------------------------------------------------
# Atomicity
# ---------------------------------------------------------------------------

def _half_mergeable_ride(conn):
    """A row with everything a half-finished merge would visibly damage."""
    import db as velomate_db

    existing = _seed_row(conn, strava_id=555, ride_weight=70.0, ride_ftp=250.0)
    _seed_derived(conn, existing)
    _seed_interval(conn, existing)
    _seed_climb(conn, existing, "detected", "Cat 4")
    _seed_climb(conn, existing, "strava", "Cat 3", segment_name="Serra Climb")
    velomate_db.upsert_streams(conn, existing, _stream(samples=50, watts=150))
    return existing


def test_a_stream_failure_rolls_the_whole_merge_back(conn):
    """Force the failure between the UPDATE and the stream write. Two commits
    would leave the new base and id over the old streams, with the derived rows
    already gone. One transaction leaves the row exactly as it was."""
    import psycopg2
    import db as velomate_db

    existing = _half_mergeable_ride(conn)
    before = _everything(conn)

    unwritable = _stream(samples=10)
    unwritable[5]["time_offset"] = "not a number"
    with pytest.raises(psycopg2.Error):
        velomate_db.upsert_activity(conn, _incoming(intervals_icu_id="i1"),
                                    streams=unwritable)

    assert _everything(conn) == before
    assert conn.autocommit is True, (
        "a connection left in a transaction would silently stop persisting "
        "everything the sync loop writes next")

    # The rollback must leave the row mergeable: the retry completes normally.
    assert _ingest(conn, _incoming(intervals_icu_id="i1"), _stream(samples=80)) == existing
    assert _row(conn, existing, "intervals_icu_id") == {"intervals_icu_id": "i1"}


def _icu_activity() -> dict:
    return {
        "id": "i1", "name": "ICU copy", "start_date": START, "distance": 50200.0,
        "moving_time": 7180, "total_elevation_gain": 480.0, "average_heartrate": 150,
        # Fractional on the real API, for columns that are INTEGER here.
        "icu_average_watts": 209.6, "average_cadence": 60.7, "type": "Ride",
        "analyzed": ANALYZED,
    }


def test_intervals_icu_poll_retries_a_merge_whose_streams_failed(conn, monkeypatch):
    """The production path. A half-merge also commits `analyzed`, and the next
    poll then sees the ride as unchanged and never re-fetches its streams, so
    the row would keep the wrong copy's streams permanently."""
    import intervals_icu

    existing = _half_mergeable_ride(conn)
    before = _everything(conn)

    responses = iter([
        [{"type": "time", "data": [0, 1, "not a number"]}],
        [{"type": "time", "data": [0, 1, 2]}, {"type": "watts", "data": [300, 300, 300]}],
    ])
    fetched = []

    def _get_streams(activity_id):
        fetched.append(activity_id)
        return next(responses)

    monkeypatch.setattr(intervals_icu, "list_activities", lambda **_kw: [_icu_activity()])
    monkeypatch.setattr(intervals_icu, "get_streams", _get_streams)
    monkeypatch.setattr(intervals_icu.time, "sleep", lambda _s: None)

    assert intervals_icu.sync_activities(conn)[0] == 0, "the failed ride is not ingested"
    assert _everything(conn) == before

    assert intervals_icu.sync_activities(conn)[0] == 1
    assert _row(conn, existing, "name", "intervals_icu_id", "avg_power", "avg_cadence") == {
        "name": "ICU copy", "intervals_icu_id": "i1", "avg_power": 210, "avg_cadence": 61}
    assert _rows(conn, """
        SELECT time_offset, power FROM activity_streams WHERE activity_id = %s
        ORDER BY time_offset""", existing) == [(0, 300), (1, 300), (2, 300)]

    # And now the gate holds: nothing to do, nothing re-downloaded.
    assert intervals_icu.sync_activities(conn)[0] == 0
    assert fetched == ["i1", "i1"]


def test_rwgps_sync_rolls_the_merge_back_when_its_streams_fail(conn, monkeypatch):
    import psycopg2
    import rwgps

    existing = _half_mergeable_ride(conn)
    with conn.cursor() as cur:
        cur.execute("UPDATE activities SET avg_power = NULL, avg_hr = NULL")
    before = _everything(conn)

    trip = {
        "id": 777, "name": "RWGPS copy", "departed_at": START, "distance": 50200.0,
        "elevation_gain": 480.0, "activity_type": "cycling:road",
        "metrics": {"movingTime": 7180, "hr": {"avg": 150, "max": 175},
                    "watts": {"avg": 210, "max": 450}, "cad": {"avg": 85},
                    "calories": 1200},
        "track_points": [{"t": 1000, "p": 200, "x": -9.14, "y": 38.7},
                         {"t": 1001, "p": "not a number", "x": -9.14, "y": 38.701}],
    }
    monkeypatch.setattr(rwgps, "fetch_trip", lambda _item_id: trip)

    with pytest.raises(psycopg2.Error):
        rwgps._process_trip_item(conn, {"item_id": 777})

    assert _everything(conn) == before

    trip["track_points"][1]["p"] = 300
    assert rwgps._process_trip_item(conn, {"item_id": 777}) is True
    assert _row(conn, existing, "name", "rwgps_id", "strava_id") == {
        "name": "RWGPS copy", "rwgps_id": 777, "strava_id": 555}
    assert _rows(conn, """
        SELECT time_offset, power FROM activity_streams WHERE activity_id = %s
        ORDER BY time_offset""", existing) == [(0, 200), (1, 300)]


def test_strava_stream_fetch_failure_is_recovered_by_the_next_pass(conn, monkeypatch):
    """Strava fetches streams after the upsert, so its merge cannot share their
    transaction. What keeps that safe is the cursor: a failed fetch does not
    advance it, so the next pass re-fetches the ride and completes it, on the
    same id."""
    import db as velomate_db
    import strava

    existing = _seed_row(conn, rwgps_id=777, avg_power=None, avg_hr=None)
    velomate_db.upsert_streams(conn, existing, _stream(samples=50, watts=150))
    _seed_climb(conn, existing, "detected", "Cat 4")

    raw = {
        "id": 555, "type": "Ride", "name": "Strava copy", "start_date": START,
        "distance": 50200.0, "moving_time": 7180, "total_elevation_gain": 480.0,
        "average_heartrate": 150, "max_heartrate": 175, "average_watts": 210,
        "max_watts": 450, "average_cadence": 85, "average_speed": 7.0,
        "calories": 1200,
    }
    streams = iter([ConnectionError("connection reset by peer"),
                    {"time": [0, 1, 2], "watts": [300, 300, 300]}])

    def _fetch_streams(_token, _activity_id):
        result = next(streams)
        if isinstance(result, Exception):
            raise result
        return result

    monkeypatch.setattr(strava, "_get_token", lambda: "token")
    monkeypatch.setattr(strava, "fetch_recent_activities", lambda _token, _after: [raw])
    monkeypatch.setattr(strava, "fetch_activity_detail", lambda _token, _id: {})
    monkeypatch.setattr(strava, "fetch_activity_streams", _fetch_streams)
    monkeypatch.setattr(strava.time, "sleep", lambda _s: None)

    assert strava.sync_activities(conn, after_epoch=0) == 0
    assert velomate_db.get_sync_state(conn, "strava_last_activity_epoch") is None, (
        "the ride must stay ahead of the cursor or it is never retried")
    assert _scalar(conn, "SELECT MAX(power) FROM activity_streams") == 150

    assert strava.sync_activities(conn, after_epoch=0) == 1
    assert _scalar(conn, "SELECT COUNT(*) FROM activities") == 1
    assert _row(conn, existing, "strava_id", "rwgps_id") == {
        "strava_id": 555, "rwgps_id": 777}
    assert _rows(conn, "SELECT time_offset, power FROM activity_streams ORDER BY 1") == [
        (0, 300), (1, 300), (2, 300)]


# ---------------------------------------------------------------------------
# Unique source ids, and the paths that are not merges
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("source_id", [
    {"strava_id": 555}, {"rwgps_id": 777}, {"intervals_icu_id": "i1"},
], ids=["strava", "rwgps", "intervals_icu"])
def test_an_incoming_id_already_on_another_row_is_a_resync_of_that_row(conn, source_id):
    """find_duplicate can return a row other than the one that already holds the
    incoming id, here because the ride's date was edited upstream. Writing the id
    onto that row is a unique violation, which the Strava and RWGPS loops treat
    as transient, so one such ride would stop every pass. It must update the row
    holding the id and leave the other one alone."""
    holder = _seed_row(conn, **source_id, name="Holder",
                       date="2026-06-20T08:00:00+00:00")
    bystander = _seed_row(conn, name="Bystander")  # no source id, so any copy outranks it
    before = _rows(conn, "SELECT * FROM activities WHERE id = %s", bystander)

    activity_id = _ingest(conn, _incoming(**source_id, name="Moved"), _stream())

    assert activity_id == holder
    assert _row(conn, holder, "name") == {"name": "Moved"}
    assert _rows(conn, "SELECT * FROM activities WHERE id = %s", bystander) == before


@pytest.mark.parametrize("source_id", [
    {"strava_id": 555}, {"rwgps_id": 777},
], ids=["strava", "rwgps"])
def test_a_weaker_copy_whose_id_is_on_another_row_resyncs_that_row(conn, source_id):
    """The same collision on the skip branch: an intervals.icu row outranks the
    copy, and the skip UPDATE writes the incoming id onto it, which is the unique
    violation that stops every Strava and RWGPS pass."""
    holder = _seed_row(conn, **source_id, name="Holder",
                       date="2026-06-20T08:00:00+00:00")
    primary = _seed_row(conn, intervals_icu_id="i1", name="Primary")
    before = _rows(conn, "SELECT * FROM activities WHERE id = %s", primary)

    activity_id = _ingest(conn, _incoming(**source_id, name="Moved"), _stream())

    assert activity_id == holder
    assert _row(conn, holder, "name") == {"name": "Moved"}
    assert _rows(conn, "SELECT * FROM activities WHERE id = %s", primary) == before


def test_a_resync_of_a_row_find_duplicate_did_not_pick_names_both_rows(conn, capsys):
    """Both rows stay, and find_duplicate will keep treating them as one ride, so
    the log is the only sign of it."""
    holder = _seed_row(conn, id=1001, rwgps_id=777, name="Holder",
                       date="2026-06-20T08:00:00+00:00")
    bystander = _seed_row(conn, id=2002, name="Bystander")

    _ingest(conn, _incoming(rwgps_id=777, name="Moved"), _stream())

    lines = [line for line in capsys.readouterr().out.splitlines() if "[dedup]" in line]
    assert len(lines) == 1
    assert str(holder) in lines[0] and str(bystander) in lines[0]
    assert _scalar(conn, "SELECT COUNT(*) FROM activities") == 2


def test_a_copy_carrying_the_rows_own_id_still_merges(conn):
    """The guard asks about OTHER rows. Finding the id on the row being merged
    into is no reason to abandon the merge."""
    existing = _seed_row(conn, rwgps_id=777, avg_power=None)

    merged = _ingest(conn, _incoming(strava_id=555, rwgps_id=777), _stream())

    assert merged == existing
    assert _row(conn, existing, "strava_id", "rwgps_id", "name") == {
        "strava_id": 555, "rwgps_id": 777, "name": "Incoming"}


def test_plain_paths_ignore_the_streams_argument(conn):
    """Outside intervals.icu, only a merge may write streams itself. A plain
    insert or a same-source re-sync reports False and the caller writes them, as
    it always has, and a re-sync clears nothing."""
    import db as velomate_db

    streams = _stream(samples=10)
    data = _incoming(rwgps_id=777)

    inserted, preserved = velomate_db.upsert_activity(conn, data, streams=streams)
    assert preserved is False
    assert _scalar(conn, "SELECT COUNT(*) FROM activity_streams") == 0

    _seed_derived(conn, inserted)
    _seed_interval(conn, inserted)
    _seed_climb(conn, inserted, "detected", "Cat 4")

    resynced, preserved = velomate_db.upsert_activity(
        conn, {**data, "name": "Renamed"}, streams=streams)

    assert (resynced, preserved) == (inserted, False)
    assert _scalar(conn, "SELECT COUNT(*) FROM activity_streams") == 0
    assert _row(conn, inserted, "name", "np") == {"name": "Renamed", "np": 999}
    assert _scalar(conn, "SELECT COUNT(*) FROM ride_intervals") == 1
    assert _scalar(conn, "SELECT COUNT(*) FROM ride_climbs") == 1
