"""Time W'bal < 25% reads the W' its ride was modelled with.

activity_streams.w_bal is modelled against the CP/W' current on the ride's own
date, and is never refreshed afterwards. The 25% line has to come from that same
W', so the W'bal step stores it on the ride. Looking the date up again later
would not do: estimates are rewritten and backfilled after a ride is modelled, so
a fresh lookup can name a W' this stream never saw.

The claims here are about what the SQL leaves in the tables and what the panels'
own queries return, so they run on a real PostgreSQL. tests/test_dashboards.py
carries the static half, which runs without a database.

Run with:
    VELOMATE_TEST_DATABASE_URL=postgresql://... python3 -m pytest \
        tests/test_wbal_per_ride.py -q
"""

import json
import os
from datetime import date
from decimal import Decimal

import pytest

from grafana_sql import expand
from pg_reset import reset_schema

TEST_DB = os.environ.get("VELOMATE_TEST_DATABASE_URL")

pytestmark = pytest.mark.skipif(
    not TEST_DB,
    reason="needs a throwaway PostgreSQL in VELOMATE_TEST_DATABASE_URL",
)

ACTIVITY_JSON = os.path.join(
    os.path.dirname(__file__), "..", "grafana", "dashboards", "metric", "activity.json"
)

RIDE_DAY = date(2026, 6, 1)
LATER_DAY = date(2026, 9, 1)

CP = 200.0

# 60 s at CP + 100 W, then 40 s of coasting. With W' = 6 kJ the ride drains
# 100 J/s and crosses the 1.5 kJ line after second 45, and 40 s of recovery only
# puts about 600 J back. So seconds 46-100 are below it: 55 of 100.
EFFORT = [300] * 60 + [0] * 40
OWN_W_PRIME_KJ = 6.0
PERCENT_BELOW_OWN_W_PRIME = Decimal("55.0")

# Against a latest W' of 24 kJ the same stream sits under the 6 kJ line throughout.
LATEST_W_PRIME_KJ = 24.0
PERCENT_BELOW_LATEST_W_PRIME = Decimal("100.0")


def _estimate(conn, day, w_prime_kj=OWN_W_PRIME_KJ, cp=CP, source="cp", fallback_ftp=None):
    with conn.cursor() as cur:
        cur.execute(
            """INSERT INTO cp_estimates (date, cp_watts, w_prime_kj, source, fallback_ftp)
               VALUES (%s, %s, %s, %s, %s)
               ON CONFLICT (date) DO UPDATE SET
                   cp_watts = EXCLUDED.cp_watts, w_prime_kj = EXCLUDED.w_prime_kj,
                   source = EXCLUDED.source, fallback_ftp = EXCLUDED.fallback_ftp""",
            (day, cp, w_prime_kj, source, fallback_ftp),
        )


def _ride(conn, day=RIDE_DAY, powers=EFFORT, **columns):
    """A ride at noon on `day` with a 1 Hz stream. A power of None is a gap."""
    row = {"name": "Synthetic", "date": f"{day}T12:00:00+00:00",
           "duration_s": len(powers), "distance_m": 20000.0, "elevation_m": 100.0,
           "sport_type": "cycling_outdoor", **columns}
    names = ", ".join(row)
    values = ", ".join(f"%({c})s" for c in row)
    with conn.cursor() as cur:
        cur.execute(f"INSERT INTO activities ({names}) VALUES ({values}) RETURNING id", row)
        ride = cur.fetchone()[0]
        cur.execute(
            """INSERT INTO activity_streams (activity_id, time_offset, power)
               SELECT %s, ord - 1, p
               FROM unnest(%s::int[]) WITH ORDINALITY AS t(p, ord)""",
            (ride, list(powers)),
        )
    return ride


def _stamp(conn, ride):
    with conn.cursor() as cur:
        cur.execute("SELECT wbal_cp, wbal_w_prime_j FROM activities WHERE id = %s", (ride,))
        return cur.fetchone()


def _scalar(conn, sql, *params):
    with conn.cursor() as cur:
        cur.execute(sql, params)
        return cur.fetchone()[0]


def _execute(conn, sql, *params):
    with conn.cursor() as cur:
        cur.execute(sql, params)


def _peak(conn, ride):
    return _scalar(conn, "SELECT MAX(w_bal) FROM activity_streams WHERE activity_id = %s", ride)


def _model_of(conn, ride):
    """The W' read back off the stored stream, not off the column. EFFORT's first
    sample is W' less one second of drain, and it never climbs back to that."""
    return _peak(conn, ride) + (EFFORT[0] - CP)


def _panel_rows(conn, panel_id, ride):
    """A panel's raw rows. [] is its noValue; [(None,)] would be a NULL value row."""
    with open(ACTIVITY_JSON) as f:
        panel = next(p for p in json.load(f)["panels"] if p["id"] == panel_id)
    with conn.cursor() as cur:
        cur.execute(expand(panel["targets"][0]["rawSql"], {"activity_id": ride}))
        return cur.fetchall()


def _ingest_new_stream(conn, ride, powers=EFFORT):
    import db as velomate_db

    velomate_db.upsert_streams(conn, ride, [
        {"time_offset": i, "power": watts} for i, watts in enumerate(powers)])


def _incoming(**overrides):
    return {"name": "Incoming", "date": f"{RIDE_DAY}T12:00:00+00:00", "duration_s": 100,
            "distance_m": 20000.0, "elevation_m": 100.0, "avg_hr": 150, "max_hr": 175,
            "avg_power": 210, "max_power": 450, "avg_cadence": 85, "avg_speed_kmh": 25.0,
            "calories": 1200, "suffer_score": None, "device": "karoo",
            "strava_type": "Ride", "trainer": False, **overrides}


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

    # A mocked psycopg2 has no __file__. See tests/test_vo2max_sql.py.
    if not hasattr(psycopg2, "__file__"):
        pytest.skip(
            "psycopg2 is mocked by another module in this pytest session; run "
            "this file on its own: python -m pytest tests/test_wbal_per_ride.py"
        )

    import db as velomate_db

    connection = psycopg2.connect(TEST_DB)
    connection.autocommit = True  # what db.get_connection hands production
    reset_schema(connection)
    velomate_db.create_schema(connection)
    yield connection
    connection.close()


def _model(conn):
    import fitness

    return fitness.compute_wbal_for_rides(conn)


# ---------------------------------------------------------------------------
# The W'bal step stores what it modelled with.
# ---------------------------------------------------------------------------

def test_each_ride_stores_the_estimate_of_its_own_date(conn):
    early = _ride(conn, RIDE_DAY)
    late = _ride(conn, LATER_DAY)
    _estimate(conn, RIDE_DAY, w_prime_kj=6.0, cp=200.0)
    _estimate(conn, LATER_DAY, w_prime_kj=24.0, cp=210.0)

    assert _model(conn) == 2

    assert _stamp(conn, early) == (200.0, 6000.0)
    assert _stamp(conn, late) == (210.0, 24000.0)


def test_the_stored_w_prime_is_the_one_that_shaped_the_stream(conn):
    """The whole point. A stamp that disagreed with the stream would be this bug
    again in a new column."""
    ride = _ride(conn)
    _estimate(conn, RIDE_DAY, w_prime_kj=6.0)

    _model(conn)

    assert _model_of(conn, ride) == pytest.approx(_stamp(conn, ride)[1])
    assert _stamp(conn, ride)[1] == 6000.0


def test_a_failed_fit_stores_the_20kj_default_and_the_fallback_cp(conn):
    ride = _ride(conn)
    _estimate(conn, RIDE_DAY, w_prime_kj=None, cp=None,
              source="rolling_20min", fallback_ftp=CP)

    _model(conn)

    assert _stamp(conn, ride) == (CP, 20000.0)
    assert _model_of(conn, ride) == pytest.approx(20000.0)


def test_a_ride_with_no_power_stores_nothing(conn):
    ride = _ride(conn, powers=[None] * 100)
    _estimate(conn, RIDE_DAY)

    assert _model(conn) == 0

    assert _stamp(conn, ride) == (None, None)


def test_the_stream_and_what_modelled_it_commit_together(conn):
    """A stream whose W' failed to store would read as modelled with whatever was
    there before. One ride failing must also not take the others down with it."""
    broken = _ride(conn, RIDE_DAY)
    fine = _ride(conn, LATER_DAY)
    _estimate(conn, RIDE_DAY)
    _execute(conn, """
        CREATE FUNCTION refuse_stamp() RETURNS trigger AS $$
        BEGIN RAISE EXCEPTION 'refused'; END $$ LANGUAGE plpgsql""")
    _execute(conn, f"""
        CREATE TRIGGER refuse BEFORE UPDATE ON activities FOR EACH ROW
        WHEN (OLD.id = {broken}) EXECUTE FUNCTION refuse_stamp()""")

    assert _model(conn) == 1

    assert _scalar(conn, """SELECT COUNT(*) FROM activity_streams
                            WHERE activity_id = %s AND w_bal IS NOT NULL""", broken) == 0
    assert _stamp(conn, fine) == (CP, OWN_W_PRIME_KJ * 1000)
    assert conn.autocommit is True, "the connection is shared with the rest of recalculation"


# ---------------------------------------------------------------------------
# Rides modelled before the columns existed, which is every ride on first start.
# ---------------------------------------------------------------------------

def test_a_ride_modelled_before_the_columns_existed_is_remodelled_and_stored(conn):
    """No version bump: only the W'bal step runs. The stream is modelled again
    rather than the date looked up, because a lookup would claim a W' for a stream
    that may have been modelled under an earlier estimate. -1 is a w_bal no model
    produces, so it surviving means the stream was not touched."""
    ride = _ride(conn)
    _estimate(conn, RIDE_DAY)
    _execute(conn, "UPDATE activity_streams SET w_bal = -1 WHERE activity_id = %s", ride)
    assert _stamp(conn, ride) == (None, None)

    assert _model(conn) == 1

    assert _stamp(conn, ride) == (CP, 6000.0)
    assert _model_of(conn, ride) == pytest.approx(6000.0)


def test_a_second_start_changes_nothing(conn):
    """Estimates move after a ride is modelled, and the stamp must not follow:
    it records what happened to this stream, not what a lookup would say today."""
    ride = _ride(conn)
    _estimate(conn, RIDE_DAY, w_prime_kj=6.0)
    _model(conn)
    _estimate(conn, RIDE_DAY, w_prime_kj=9.0)

    assert _model(conn) == 0

    assert _stamp(conn, ride) == (CP, 6000.0)
    assert _model_of(conn, ride) == pytest.approx(6000.0)


def test_schema_change_is_additive_and_repeatable(conn):
    """The state of a populated database on first start after this deploys."""
    import db as velomate_db

    ride = _ride(conn)
    _estimate(conn, RIDE_DAY)
    _model(conn)
    expected = _scalar(conn, "SELECT SUM(w_bal) FROM activity_streams")
    _execute(conn, "ALTER TABLE activities DROP COLUMN wbal_cp, DROP COLUMN wbal_w_prime_j")

    velomate_db.create_schema(conn)
    velomate_db.create_schema(conn)

    assert _stamp(conn, ride) == (None, None)
    assert _scalar(conn, "SELECT COUNT(*) FROM activities") == 1
    assert _scalar(conn, "SELECT SUM(w_bal) FROM activity_streams") == expected


# ---------------------------------------------------------------------------
# Every path that invalidates w_bal takes the stored W' with it.
# ---------------------------------------------------------------------------

def _recalculate(conn, stored_version):
    import db as velomate_db
    import fitness

    velomate_db.set_sync_state(conn, "metrics_version", stored_version)
    fitness.recalculate_fitness(conn)


def test_a_version_reset_clears_the_stored_w_prime_with_the_stream(conn, monkeypatch):
    """Without the W'bal step refilling them, what the reset leaves is visible: a
    stored W' for a stream that no longer has any W'bal."""
    import fitness

    ride = _ride(conn)
    _estimate(conn, RIDE_DAY)
    _model(conn)
    assert _stamp(conn, ride) == (CP, 6000.0)
    monkeypatch.setattr(fitness, "compute_wbal_for_rides", lambda _conn: 0)

    _recalculate(conn, stored_version="0")

    assert _peak(conn, ride) is None
    assert _stamp(conn, ride) == (None, None)


def test_a_version_reset_refills_from_the_current_estimate(conn):
    ride = _ride(conn)
    _estimate(conn, RIDE_DAY, w_prime_kj=6.0)
    _model(conn)
    _estimate(conn, RIDE_DAY, w_prime_kj=9.0)

    _recalculate(conn, stored_version="0")

    assert _stamp(conn, ride) == (CP, 9000.0)
    assert _model_of(conn, ride) == pytest.approx(9000.0)


def test_a_merge_with_streams_drops_the_old_w_prime_then_models_the_new_stream(conn):
    import db as velomate_db

    ride = _ride(conn, strava_id=555, device="watch")
    _estimate(conn, RIDE_DAY, w_prime_kj=6.0)
    _model(conn)
    _estimate(conn, RIDE_DAY, w_prime_kj=9.0)

    merged, _ = velomate_db.upsert_activity(
        conn, _incoming(intervals_icu_id="i1"),
        streams=[{"time_offset": i, "power": watts} for i, watts in enumerate(EFFORT)])

    assert merged == ride
    assert _stamp(conn, ride) == (None, None)
    assert _panel_rows(conn, 1201, ride) == []

    assert _model(conn) == 1
    assert _stamp(conn, ride) == (CP, 9000.0)
    assert _model_of(conn, ride) == pytest.approx(9000.0)


def test_a_merge_without_streams_remodels_the_stream_it_keeps(conn):
    """The merge resets every derived column, and here w_bal stays behind. Unless
    that NULL alone selects the ride, it would never be stamped again and the
    panel would show nothing for it forever."""
    import db as velomate_db

    ride = _ride(conn, strava_id=555, device="watch")
    _estimate(conn, RIDE_DAY, w_prime_kj=6.0)
    _model(conn)
    _estimate(conn, RIDE_DAY, w_prime_kj=9.0)

    merged, streams_written = velomate_db.upsert_activity(conn, _incoming(intervals_icu_id="i1"))

    assert (merged, streams_written) == (ride, False)
    assert _stamp(conn, ride) == (None, None)

    assert _model(conn) == 1
    assert _stamp(conn, ride) == (CP, 9000.0)
    assert _model_of(conn, ride) == pytest.approx(9000.0)


def test_a_resync_stores_what_modelled_the_new_stream(conn):
    ride = _ride(conn)
    _estimate(conn, RIDE_DAY, w_prime_kj=6.0)
    _model(conn)
    _estimate(conn, RIDE_DAY, w_prime_kj=9.0)

    _ingest_new_stream(conn, ride)
    assert _model(conn) == 1

    assert _stamp(conn, ride) == (CP, 9000.0)
    assert _model_of(conn, ride) == pytest.approx(9000.0)


def test_a_resync_that_loses_power_leaves_no_w_prime_behind(conn):
    """No power means the W'bal step never comes back for this ride, so nothing
    else would ever clear it."""
    ride = _ride(conn)
    _estimate(conn, RIDE_DAY)
    _model(conn)

    _ingest_new_stream(conn, ride, powers=[None] * 100)
    assert _model(conn) == 0

    assert _stamp(conn, ride) == (None, None)


# ---------------------------------------------------------------------------
# The panels, on their own SQL.
# ---------------------------------------------------------------------------

def test_the_25_percent_line_is_the_rides_own_w_prime_not_the_latest(conn):
    ride = _ride(conn)
    _estimate(conn, RIDE_DAY, w_prime_kj=OWN_W_PRIME_KJ)
    _estimate(conn, LATER_DAY, w_prime_kj=LATEST_W_PRIME_KJ)

    _model(conn)

    assert _panel_rows(conn, 1201, ride) == [(PERCENT_BELOW_OWN_W_PRIME,)]
    assert _scalar(conn, """
        SELECT ROUND(COUNT(*) FILTER (WHERE w_bal < %s) * 100.0 / COUNT(*), 1)
        FROM activity_streams WHERE activity_id = %s""",
                   LATEST_W_PRIME_KJ * 1000 * 0.25, ride) == PERCENT_BELOW_LATEST_W_PRIME, (
        "fixture is wrong: the two lines have to disagree")


def test_the_line_is_25_percent_of_w_prime_in_joules_and_strict(conn):
    """From 4 kJ at CP + 100 W: second 30 lands exactly on the 1 kJ line, which is
    not below it, seconds 31-35 are, and 30 s at 150 W puts only about 150 J back.
    So 35 of 65 samples, where a line in kilojoules would give 0 of them."""
    ride = _ride(conn, powers=[300] * 35 + [150] * 30)
    _estimate(conn, RIDE_DAY, w_prime_kj=4.0)

    _model(conn)

    assert _scalar(conn, """
        SELECT COUNT(*) FROM activity_streams
        WHERE activity_id = %s AND time_offset = 29 AND w_bal = 1000.0""", ride) == 1
    assert _panel_rows(conn, 1201, ride) == [(Decimal("53.8"),)]


def test_the_panel_does_not_consult_the_estimates_at_all(conn):
    """Deleted estimates can't change what a stored stream means."""
    ride = _ride(conn)
    _estimate(conn, RIDE_DAY)
    _model(conn)

    _execute(conn, "DELETE FROM cp_estimates")

    assert _panel_rows(conn, 1201, ride) == [(PERCENT_BELOW_OWN_W_PRIME,)]


@pytest.mark.parametrize("panel_id", [1200, 1201, 1202])
def test_a_ride_with_no_power_gives_no_row(conn, panel_id):
    ride = _ride(conn, powers=[None] * 100)
    _estimate(conn, RIDE_DAY)

    _model(conn)

    assert _panel_rows(conn, panel_id, ride) == []


def test_a_ride_not_yet_stamped_gives_no_row_rather_than_a_null_one(conn):
    """w_bal present and the column still NULL: a stream between deploy and the
    first W'bal step, or after a merge that kept its streams."""
    ride = _ride(conn)
    _estimate(conn, RIDE_DAY)
    _model(conn)
    _execute(conn, "UPDATE activities SET wbal_cp = NULL, wbal_w_prime_j = NULL")

    assert _panel_rows(conn, 1201, ride) == []


def test_min_wbal_and_the_chart_read_the_stream_in_kilojoules(conn):
    """Both read w_bal directly, so they need no stored W'. Pinned so a later
    change to the threshold panel can't quietly take them with it."""
    ride = _ride(conn, powers=[300] * 30 + [0] * 30)
    _estimate(conn, RIDE_DAY, w_prime_kj=6.0)

    _model(conn)

    assert _panel_rows(conn, 1200, ride) == [(Decimal("3.0"),)]
    chart = _panel_rows(conn, 1202, ride)
    assert len(chart) == 60
    assert chart[0][1] == Decimal("5.9")

