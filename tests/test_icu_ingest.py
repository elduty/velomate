"""intervals.icu ingestion against a real PostgreSQL, with the API stubbed.

The stored `analyzed` timestamp is the only thing that tells a later poll a
ride's streams are current. Everything here is about keeping that true: what the
database holds after a failure decides whether the next poll retries or walks
past the ride for good, and a mocked cursor would only re-assert the code.

Run with:
    VELOMATE_TEST_DATABASE_URL=postgresql://... python3 -m pytest \
        tests/test_icu_ingest.py -q
"""

import os
import sys
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from pg_reset import reset_schema

TEST_DB = os.environ.get("VELOMATE_TEST_DATABASE_URL")

pytestmark = pytest.mark.skipif(
    not TEST_DB,
    reason="needs a throwaway PostgreSQL in VELOMATE_TEST_DATABASE_URL",
)

START = "2026-06-01T08:00:00+00:00"
ANALYZED = "2026-06-01T12:00:00+00:00"
REANALYZED = "2026-06-02T12:00:00+00:00"

# Fails on the INTEGER time_offset column, inside the stream write itself.
UNWRITABLE_STREAMS = [{"type": "time", "data": [0, 1, "not a number"]}]


def _activity(activity_id="i1", **overrides) -> dict:
    """An activity as the intervals.icu list endpoint returns it."""
    activity = {
        "id": activity_id, "name": "ICU ride", "start_date": START, "distance": 50200.0,
        "moving_time": 7180, "total_elevation_gain": 480.0, "average_heartrate": 150,
        "icu_average_watts": 210, "average_cadence": 85, "type": "Ride",
        "analyzed": ANALYZED,
    }
    activity.update(overrides)
    return activity


def _stub(activity_id, days_ago=5) -> dict:
    """The placeholder intervals.icu serves for a Strava-relayed activity."""
    return {"id": activity_id, "source": "STRAVA", "start_date_local": _days_ago(days_ago),
            "_note": "STRAVA activities are not available via the API"}


def _days_ago(days: int) -> str:
    moment = datetime.now(timezone.utc) - timedelta(days=days)
    return moment.strftime("%Y-%m-%dT%H:%M:%S+00:00")


def _streams(watts=300, samples=3) -> list:
    return [{"type": "time", "data": list(range(samples))},
            {"type": "watts", "data": [watts] * samples}]


class _Api:
    """Stands in for the intervals.icu client and records what it was asked.

    Behaves like the real list endpoint where it matters here: `oldest` bounds
    the result, and `fields` projects it, so a field nobody asked for is absent.
    """

    def __init__(self, monkeypatch):
        import intervals_icu

        self.activities = []
        self.responses = {}   # activity id -> queue of stream responses
        self.fetched = []
        self.list_calls = []
        self.sleeps = []
        monkeypatch.setattr(intervals_icu, "list_activities", self._list)
        monkeypatch.setattr(intervals_icu, "get_streams", self._get_streams)
        monkeypatch.setattr(intervals_icu.time, "sleep", self.sleeps.append)

    def _list(self, oldest, newest=None, fields=None, limit=None):
        self.list_calls.append({"oldest": oldest, "fields": fields})
        started = lambda a: a.get("start_date") or a.get("start_date_local")
        listed = [a for a in self.activities if started(a)[:19] >= oldest[:19]]
        if fields:
            # A stub carries _note whatever the mask asks for.
            listed = [{k: v for k, v in a.items() if k in fields or k == "_note"}
                      for a in listed]
        return listed

    def _get_streams(self, activity_id):
        self.fetched.append(activity_id)
        queue = self.responses.get(activity_id)
        response = queue.pop(0) if queue else _streams()
        if isinstance(response, BaseException):
            raise response
        return response


def _poll(conn) -> int:
    """One sync pass, on a window wide enough for the fixed dates above."""
    import intervals_icu

    return intervals_icu.sync_activities(conn, window_days=3650)[0]


def _row(conn, activity_id, *columns) -> dict:
    with conn.cursor() as cur:
        cur.execute(f"SELECT {', '.join(columns)} FROM activities "
                    "WHERE intervals_icu_id = %s", (activity_id,))
        values = cur.fetchone()
    return dict(zip(columns, values)) if values else {}


def _rows(conn, sql, *params) -> list:
    with conn.cursor() as cur:
        cur.execute(sql, params)
        return cur.fetchall()


def _stream_powers(conn) -> list:
    return [power for (power,) in _rows(
        conn, "SELECT power FROM activity_streams ORDER BY activity_id, time_offset")]


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


@pytest.fixture()
def conn():
    import psycopg2

    # Other test modules install a MagicMock for psycopg2 in the shared pytest
    # process. A mocked module has no __file__. See test_route_id_stability.py.
    if not hasattr(psycopg2, "__file__"):
        pytest.skip(
            "psycopg2 is mocked by another module in this pytest session; run "
            "this file on its own: python -m pytest tests/test_icu_ingest.py"
        )

    import db as velomate_db

    connection = psycopg2.connect(TEST_DB)
    connection.autocommit = True  # what db.get_connection hands production
    reset_schema(connection)
    velomate_db.create_schema(connection)
    yield connection
    connection.close()


@pytest.fixture()
def api(monkeypatch):
    return _Api(monkeypatch)


# ---------------------------------------------------------------------------
# A failed stream write must be retried by the next poll
# ---------------------------------------------------------------------------

def test_a_new_ride_whose_streams_fail_is_retried_and_completes(conn, api):
    """Row first, streams second: the first poll commits `analyzed` with no
    streams, the second sees the ride as unchanged, and it stays empty for good."""
    api.activities = [_activity()]
    api.responses["i1"] = [UNWRITABLE_STREAMS, _streams(watts=300)]

    assert _poll(conn) == 0, "a ride whose streams failed is not ingested"
    assert _rows(conn, "SELECT COUNT(*) FROM activities") == [(0,)], (
        "nothing may survive the failure that a later poll would take as finished")
    assert conn.autocommit is True, (
        "a connection left in a transaction would silently stop persisting "
        "everything the sync loop writes next")

    assert _poll(conn) == 1
    assert _stream_powers(conn) == [300, 300, 300]
    assert _row(conn, "i1", "intervals_icu_analyzed") == {
        "intervals_icu_analyzed": datetime.fromisoformat(ANALYZED)}

    # And now the gate holds: nothing to do, nothing re-downloaded.
    assert _poll(conn) == 0
    assert api.fetched == ["i1", "i1"]
    # Checked after the unchanged poll, because that poll still upserts the row and
    # the intervals.icu target overwrites `analyzed`. Clearing it would only show
    # as a re-download on the poll after this one.
    assert _row(conn, "i1", "intervals_icu_analyzed") == {
        "intervals_icu_analyzed": datetime.fromisoformat(ANALYZED)}


def test_a_reanalysed_ride_whose_streams_fail_keeps_its_old_state_and_is_retried(conn, api):
    """The re-sync form of the same bug: a committed new `analyzed` over the old
    streams means the fresh ones are never fetched."""
    api.activities = [_activity()]
    api.responses["i1"] = [_streams(watts=100)]
    assert _poll(conn) == 1

    api.activities = [_activity(name="Renamed", analyzed=REANALYZED)]
    api.responses["i1"] = [UNWRITABLE_STREAMS, _streams(watts=300)]

    assert _poll(conn) == 0
    assert _row(conn, "i1", "name", "intervals_icu_analyzed") == {
        "name": "ICU ride", "intervals_icu_analyzed": datetime.fromisoformat(ANALYZED)}
    assert _stream_powers(conn) == [100, 100, 100], (
        "the failed write must not have cleared the streams already stored")

    assert _poll(conn) == 1
    assert _row(conn, "i1", "name", "intervals_icu_analyzed") == {
        "name": "Renamed", "intervals_icu_analyzed": datetime.fromisoformat(REANALYZED)}
    assert _stream_powers(conn) == [300, 300, 300]


def test_one_ride_failing_does_not_stop_the_rest_of_the_poll(conn, api):
    api.activities = [_activity("i1"), _activity("i2", start_date="2026-06-03T08:00:00+00:00")]
    api.responses["i1"] = [UNWRITABLE_STREAMS]

    assert _poll(conn) == 1
    assert _row(conn, "i1", "name") == {}
    assert _row(conn, "i2", "name") == {"name": "ICU ride"}


def test_a_resync_of_a_row_find_duplicate_did_not_pick_is_retried_too(conn, api):
    """The ride's date moved upstream, so find_duplicate lands on a bystander and
    the row holding the id is re-synced from that branch instead. Nothing else
    writes the streams on that path."""
    holder = _seed_row(conn, intervals_icu_id="i1", intervals_icu_analyzed=ANALYZED,
                       date="2026-06-20T08:00:00+00:00")
    _seed_row(conn, name="Bystander")
    api.activities = [_activity(analyzed=REANALYZED)]
    api.responses["i1"] = [UNWRITABLE_STREAMS, _streams(watts=300)]

    assert _poll(conn) == 0
    assert _row(conn, "i1", "intervals_icu_analyzed") == {
        "intervals_icu_analyzed": datetime.fromisoformat(ANALYZED)}

    assert _poll(conn) == 1
    assert _rows(conn, "SELECT DISTINCT activity_id, power FROM activity_streams") == [
        (holder, 300)]


def test_the_row_and_its_streams_land_through_upsert_activity(conn):
    """intervals.icu has no caller-side stream write any more, so True here is
    what makes that safe."""
    import db as velomate_db

    points = [{"time_offset": i, "power": 200} for i in range(5)]
    data = {"intervals_icu_id": "i1", "name": "Ride", "date": START, "distance_m": 1000.0,
            "duration_s": 600, "elevation_m": 10.0, "avg_hr": None, "max_hr": None,
            "avg_power": None, "max_power": None, "avg_cadence": None, "avg_speed_kmh": 6.0,
            "calories": None, "suffer_score": None, "device": "karoo", "strava_type": "Ride",
            "intervals_icu_analyzed": ANALYZED}

    activity_id, streams_written = velomate_db.upsert_activity(conn, data, streams=points)

    assert streams_written is True
    assert _rows(conn, "SELECT COUNT(*) FROM activity_streams WHERE activity_id = %s",
                 activity_id) == [(5,)]


# ---------------------------------------------------------------------------
# `analyzed` on the conflict targets
# ---------------------------------------------------------------------------

def test_a_ride_that_lost_its_analyzed_value_settles_after_one_resync(conn, api):
    """Remote `analyzed` goes back to null. COALESCE keeps the old timestamp, so
    stored and incoming never compare equal and the ride re-downloads and
    re-counts as ingested on every poll."""
    api.activities = [_activity()]
    assert _poll(conn) == 1

    api.activities = [_activity(analyzed=None)]
    assert _poll(conn) == 1
    assert _row(conn, "i1", "intervals_icu_analyzed") == {"intervals_icu_analyzed": None}

    assert _poll(conn) == 0
    assert api.fetched == ["i1", "i1"]


@pytest.mark.parametrize("source_id", [{"strava_id": 555}, {"rwgps_id": 777}],
                         ids=["strava", "rwgps"])
def test_another_sources_resync_keeps_a_dual_source_rows_analyzed(conn, source_id):
    """Only intervals.icu writes `analyzed` as sent. A Strava or RWGPS re-sync
    carries None for it, and clearing it would re-fetch the ride's streams."""
    import db as velomate_db

    _seed_row(conn, **source_id, intervals_icu_id="i1", intervals_icu_analyzed=ANALYZED)
    data = {"name": "Re-sync", "date": START, "distance_m": 50000.0, "duration_s": 7200,
            "elevation_m": 500.0, "avg_hr": 140, "max_hr": 170, "avg_power": 180,
            "max_power": 400, "avg_cadence": 80, "avg_speed_kmh": 25.0, "calories": 1100,
            "suffer_score": None, "device": "watch", "strava_type": "Ride", **source_id}

    velomate_db.upsert_activity(conn, data)

    assert _row(conn, "i1", "name", "intervals_icu_analyzed") == {
        "name": "Re-sync", "intervals_icu_analyzed": datetime.fromisoformat(ANALYZED)}


# ---------------------------------------------------------------------------
# The cycling allow-list
# ---------------------------------------------------------------------------

def _sweep(conn):
    import intervals_icu

    return intervals_icu.reconcile(conn, sweep_days=90)


def _seed_recent(conn, activity_id, days_ago=5, **overrides) -> int:
    return _seed_row(conn, intervals_icu_id=activity_id, date=_days_ago(days_ago),
                     intervals_icu_analyzed=ANALYZED, **overrides)


def _recent(activity_id, days_ago=5, **overrides) -> dict:
    return _activity(activity_id, start_date=_days_ago(days_ago), **overrides)


def test_a_run_from_the_watch_is_not_stored_or_downloaded(conn, api):
    api.activities = [_activity("run1", type="Run"),
                      _activity("ride1", start_date="2026-06-03T08:00:00+00:00")]

    assert _poll(conn) == 1
    assert _row(conn, "run1", "name") == {}
    assert _row(conn, "ride1", "name") == {"name": "ICU ride"}
    assert api.fetched == ["ride1"]


def test_a_stored_ride_retyped_upstream_is_left_alone_by_the_poll(conn, api):
    """Nothing already stored may change because of the filter."""
    _seed_recent(conn, "i1", name="Stored")
    before = _rows(conn, "SELECT * FROM activities")
    api.activities = [_recent("i1", type="Walk", name="Now a walk", analyzed=REANALYZED)]

    assert _poll(conn) == 0
    assert _rows(conn, "SELECT * FROM activities") == before
    assert api.fetched == []


def test_a_stored_ride_retyped_upstream_does_not_look_deleted(conn, api):
    """The trap in filtering the sweep. Dropping the Walk from the remote set
    would leave this ride in local-minus-remote, below the cap and above the
    ratio guard, and CASCADE would take it."""
    for activity_id in ("i1", "i2", "i3", "i4"):
        _seed_recent(conn, activity_id)
    api.activities = [_recent("i1", type="Walk"), _recent("i2", days_ago=6),
                      _recent("i3", days_ago=7), _recent("i4", days_ago=8)]

    assert _sweep(conn) == (0, 0)
    assert _rows(conn, "SELECT COUNT(*) FROM activities") == [(4,)]

    # And on the sweep after, in case anything was deferred rather than dropped.
    assert _sweep(conn) == (0, 0)
    assert _rows(conn, "SELECT COUNT(*) FROM activities") == [(4,)]


def test_every_stored_ride_retyped_at_once_deletes_nothing(conn, api):
    """Past the cap, where a filtered remote set would queue a bulk delete."""
    ids = [f"i{n}" for n in range(8)]
    for days_ago, activity_id in enumerate(ids, start=2):
        _seed_recent(conn, activity_id, days_ago=days_ago)
    api.activities = [_recent(activity_id, days_ago=days_ago, type="Workout")
                      for days_ago, activity_id in enumerate(ids, start=2)]

    _sweep(conn)
    _sweep(conn)

    assert _rows(conn, "SELECT COUNT(*) FROM activities") == [(8,)]


def test_a_filtered_activity_is_not_reported_as_missing(conn, api):
    """It was never stored and never will be, so it is not a gap to recover."""
    _seed_recent(conn, "i1")
    api.activities = [_recent("i1"), _recent("run1", days_ago=6, type="Run"), _stub("s1")]

    assert _sweep(conn) == (0, 0)


def test_a_real_deletion_is_still_mirrored_alongside_a_filtered_activity(conn, api):
    for days_ago, activity_id in enumerate(("gone", "i1", "i2", "i3"), start=2):
        _seed_recent(conn, activity_id, days_ago=days_ago)
    api.activities = [_recent("i1", days_ago=3), _recent("i2", days_ago=4),
                      _recent("i3", days_ago=5), _recent("run1", days_ago=6, type="Run")]

    assert _sweep(conn) == (1, 0)
    assert _row(conn, "gone", "name") == {}
    assert _rows(conn, "SELECT COUNT(*) FROM activities") == [(3,)]


# ---------------------------------------------------------------------------
# Backfill
# ---------------------------------------------------------------------------

def _backfill(conn, months=12) -> int:
    import intervals_icu

    return intervals_icu.backfill(conn, months=months)


def _backfill_pending(conn):
    import db as velomate_db
    import intervals_icu

    return velomate_db.get_sync_state(conn, intervals_icu.BACKFILL_PENDING_KEY)


def _stored_ids(conn) -> list:
    return [row[0] for row in _rows(conn, """
        SELECT intervals_icu_id FROM activities
        WHERE intervals_icu_id IS NOT NULL ORDER BY intervals_icu_id""")]


def _rate_limited():
    import requests

    return requests.HTTPError("429", response=SimpleNamespace(status_code=429))


def test_backfill_stores_history_through_the_normal_path(conn, api):
    api.activities = [_recent("i1", days_ago=100), _recent("i2", days_ago=200),
                      _stub("s1", days_ago=150), _recent("run1", days_ago=120, type="Run"),
                      _recent("too_old", days_ago=400)]

    assert _backfill(conn, months=12) == 2

    assert _stored_ids(conn) == ["i1", "i2"]
    assert sorted(api.fetched) == ["i1", "i2"]
    assert _stream_powers(conn) == [300] * 6
    assert _row(conn, "i1", "intervals_icu_analyzed") == {
        "intervals_icu_analyzed": datetime.fromisoformat(ANALYZED)}
    assert not _backfill_pending(conn)
    assert api.sleeps == [1.0, 1.0], "the sync loop's per-ride pace is kept"


def test_full_history_has_no_window(conn, api):
    api.activities = [_recent("ancient", days_ago=4000)]

    assert _backfill(conn, months=0) == 1
    assert api.list_calls[0]["oldest"].startswith("1970-")


def test_an_activity_that_will_not_parse_does_not_stop_the_rest(conn, api, monkeypatch):
    """Sorting the pass parses every ride, ahead of the per-ride isolation, so one
    bad ride used to stop the sweep and the backfill before anything was stored."""
    import intervals_icu

    real_parse = intervals_icu._parse_activity

    def parse(activity):
        if activity["id"] == "bad":
            raise ValueError("unparseable")
        return real_parse(activity)

    monkeypatch.setattr(intervals_icu, "_parse_activity", parse)
    api.activities = [_recent("bad", days_ago=100), _recent("good", days_ago=101)]

    ingested, finished = intervals_icu._ingest_missing(conn, api.activities)

    assert ingested == 1 and finished is False
    assert _stored_ids(conn) == ["good"]


def test_one_failing_ride_does_not_stop_the_rest_and_a_later_pass_finishes_it(conn, api):
    api.activities = [_recent("i1", days_ago=100), _recent("i2", days_ago=200),
                      _recent("i3", days_ago=300)]
    api.responses["i2"] = [UNWRITABLE_STREAMS, _streams()]

    assert _backfill(conn) == 2
    assert _stored_ids(conn) == ["i1", "i3"]
    assert _backfill_pending(conn), "the failed ride still has to be fetched"
    api.fetched.clear()

    assert _backfill(conn) == 1
    assert api.fetched == ["i2"], "finished rides must not be fetched again"
    assert _stored_ids(conn) == ["i1", "i2", "i3"]
    assert not _backfill_pending(conn)


def test_a_crash_midway_leaves_a_state_the_next_run_completes(conn, api):
    """KeyboardInterrupt stands in for the process dying: no handler sees it."""
    api.activities = [_recent("i1", days_ago=100), _recent("i2", days_ago=200),
                      _recent("i3", days_ago=300)]
    api.responses["i2"] = [KeyboardInterrupt()]

    with pytest.raises(KeyboardInterrupt):
        _backfill(conn)

    done = _stored_ids(conn)
    assert done == ["i1"]
    assert _backfill_pending(conn), (
        "with rows stored and no key, the install reads as finished and the "
        "rest of its history is never fetched")
    api.fetched.clear()
    api.responses.clear()

    assert _backfill(conn) == 2
    assert not set(api.fetched) & set(done)
    assert _stored_ids(conn) == ["i1", "i2", "i3"]
    assert not _backfill_pending(conn)


def test_a_rate_limit_that_outlasts_the_retries_stops_the_pass(conn, api):
    """The client has already slept through its retries. Carrying on would repeat
    that wait for every remaining ride, which is what blocks polling for days."""
    api.activities = [_recent(f"i{n}", days_ago=100 + n) for n in range(5)]
    api.responses = {f"i{n}": [_rate_limited()] for n in range(5)}

    assert _backfill(conn) == 0
    assert len(api.fetched) == 1
    assert _backfill_pending(conn)

    api.responses.clear()
    assert _backfill(conn) == 5
    assert not _backfill_pending(conn)


def test_a_pass_stops_at_its_time_budget_and_a_later_one_finishes(conn, api, monkeypatch):
    import intervals_icu

    api.activities = [_recent("i1", days_ago=100), _recent("i2", days_ago=200)]
    monkeypatch.setattr(intervals_icu, "MISSING_RIDES_PASS_SECONDS", 0)

    assert _backfill(conn) == 0
    assert api.fetched == []
    assert _backfill_pending(conn)

    monkeypatch.setattr(intervals_icu, "MISSING_RIDES_PASS_SECONDS", 600)
    assert _backfill(conn) == 2
    assert not _backfill_pending(conn)


def test_a_stored_ride_is_left_exactly_as_it_was(conn, api):
    """Re-analysed upstream, and outside the sync window. A backfill fills gaps:
    it must not re-download old rides or rewrite them."""
    _seed_recent(conn, "i1", days_ago=100, name="Stored")
    before = _rows(conn, "SELECT * FROM activities")
    api.activities = [_recent("i1", days_ago=100, name="Renamed", analyzed=REANALYZED)]

    assert _backfill(conn) == 0

    assert _rows(conn, "SELECT * FROM activities") == before
    assert api.fetched == []
    assert not _backfill_pending(conn)


def test_a_stored_ride_whose_date_moved_upstream_is_left_alone_too(conn, api):
    """Moved past find_duplicate's tolerance, so only its id says it is stored."""
    _seed_recent(conn, "i1", days_ago=100, name="Stored")
    before = _rows(conn, "SELECT * FROM activities")
    api.activities = [_recent("i1", days_ago=103, name="Renamed")]

    assert _backfill(conn) == 0

    assert _rows(conn, "SELECT * FROM activities") == before
    assert api.fetched == []


def test_a_reupload_whose_original_id_is_gone_upstream_takes_over_the_row(conn, api):
    """The stored id is no longer listed, so this is not a second upload: it is
    the only copy left, and skipping it would orphan the row from its source."""
    started = _days_ago(100)
    existing = _seed_row(conn, intervals_icu_id="old_upload", date=started, duration_s=7180,
                         intervals_icu_analyzed=ANALYZED)
    api.activities = [_activity("new_upload", start_date=started)]

    assert _backfill(conn) == 1

    assert _rows(conn, "SELECT id, intervals_icu_id FROM activities") == [
        (existing, "new_upload")]


def test_an_empty_listing_does_not_finish_a_backfill(conn, api):
    """It may be a degraded 200, and the poll storing rows next would otherwise
    leave a new install on two weeks of history with nothing left to retry it."""
    assert _backfill(conn) == 0
    assert _backfill_pending(conn)


def test_a_listing_at_its_ceiling_says_so(conn, api, monkeypatch, capsys):
    import intervals_icu

    monkeypatch.setattr(intervals_icu, "RECONCILE_FETCH_LIMIT", 2)
    api.activities = [_recent("i1", days_ago=100), _recent("i2", days_ago=200)]

    _backfill(conn)

    assert "ceiling" in capsys.readouterr().out


def test_a_backfilled_ride_merges_into_a_strava_row_in_place(conn, api):
    """Same ride, stored from Strava long before intervals.icu was the source."""
    import db as velomate_db

    started = _days_ago(100)
    existing = _seed_row(conn, strava_id=555, name="Strava name", date=started,
                         suffer_score=77, np=999.0)
    velomate_db.upsert_streams(conn, existing, [{"time_offset": 0, "power": 150}])
    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO ride_climbs (activity_id, gain_m, category, source, segment_name)
            VALUES (%s, 120, 'Cat 3', 'strava', 'Serra Climb')
        """, (existing,))
    api.activities = [_activity("i1", start_date=started, name="ICU name")]

    assert _backfill(conn) == 1

    assert _rows(conn, """
        SELECT id, strava_id, intervals_icu_id, name, suffer_score, np FROM activities
    """) == [(existing, 555, "i1", "ICU name", 77, None)]
    assert _stream_powers(conn) == [300, 300, 300]
    assert _rows(conn, "SELECT segment_name FROM ride_climbs") == [("Serra Climb",)]


def test_a_failed_stream_write_rolls_a_backfilled_merge_back(conn, api):
    started = _days_ago(100)
    existing = _seed_row(conn, strava_id=555, name="Strava name", date=started)
    before = _rows(conn, "SELECT * FROM activities")
    api.activities = [_activity("i1", start_date=started)]
    api.responses["i1"] = [UNWRITABLE_STREAMS, _streams()]

    assert _backfill(conn) == 0
    assert _rows(conn, "SELECT * FROM activities") == before
    assert _backfill_pending(conn)

    assert _backfill(conn) == 1
    assert _rows(conn, "SELECT id, intervals_icu_id FROM activities") == [(existing, "i1")]


def test_of_two_uploads_of_one_ride_the_richer_is_kept_and_stays_kept(conn, api):
    """Listed thin copy first. Taken in order, the copy with no power would found
    the row and the copy with power would be the one left out."""
    started = _days_ago(100)
    thin = _activity("thin", start_date=started, icu_average_watts=None)
    rich = _activity("rich", start_date=started)
    api.activities = [thin, rich]

    assert _backfill(conn) == 1
    assert _stored_ids(conn) == ["rich"]
    assert api.fetched == ["rich"]
    assert not _backfill_pending(conn)

    assert _backfill(conn) == 0
    assert api.fetched == ["rich"], "a re-run must not re-download the second upload"


def test_backfilled_history_is_outside_the_sweeps_reach(conn, api):
    """Backfilling must not widen deletion mirroring. Rides past the sweep window
    are never compared, so one the API stops listing is not deleted."""
    api.activities = [_recent("old1", days_ago=200), _recent("old2", days_ago=300)]
    assert _backfill(conn) == 2

    api.activities = [_recent("new1", days_ago=5)]

    assert _sweep(conn) == (0, 1)
    assert _stored_ids(conn) == ["new1", "old1", "old2"]


# ---------------------------------------------------------------------------
# The sweep's remote-only rides
# ---------------------------------------------------------------------------

def test_a_remote_only_ride_past_the_sync_window_is_ingested_once(conn, api):
    _seed_recent(conn, "i1", days_ago=5)
    api.activities = [_recent("i1", days_ago=5), _recent("gap", days_ago=40)]

    assert _sweep(conn) == (0, 1)
    assert _stored_ids(conn) == ["gap", "i1"]
    assert _stream_powers(conn) == [300, 300, 300]
    assert api.fetched == ["gap"]
    api.list_calls.clear()

    assert _sweep(conn) == (0, 0)
    assert api.fetched == ["gap"]
    assert len(api.list_calls) == 1, "nothing missing, so no second listing"


def test_an_unchanged_library_costs_one_masked_listing(conn, api):
    """What a populated install does on an ordinary day."""
    for days_ago, activity_id in enumerate(("i1", "i2", "i3"), start=2):
        _seed_recent(conn, activity_id, days_ago=days_ago)
    before = _rows(conn, "SELECT * FROM activities")
    api.activities = [_recent("i1", days_ago=2), _recent("i2", days_ago=3),
                      _recent("i3", days_ago=4), _stub("s1", days_ago=40)]

    assert _sweep(conn) == (0, 0)

    assert api.fetched == []
    assert [bool(call["fields"]) for call in api.list_calls] == [True]
    assert _rows(conn, "SELECT * FROM activities") == before


def test_stubs_and_non_cycling_rides_are_never_retried_or_counted(conn, api):
    _seed_recent(conn, "i1", days_ago=5)
    api.activities = [_recent("i1", days_ago=5), _stub("s1", days_ago=40),
                      _recent("run1", days_ago=41, type="Run"),
                      _recent("walk1", days_ago=42, type="Walk")]

    for _sweep_number in range(3):
        assert _sweep(conn) == (0, 0)

    assert api.fetched == []
    assert len(api.list_calls) == 3, "one masked listing a sweep and nothing more"
    assert _stored_ids(conn) == ["i1"]


def test_a_sweep_stops_at_its_cap_and_the_next_takes_the_rest(conn, api, capsys):
    import intervals_icu

    cap = intervals_icu.MAX_INGESTS_PER_SWEEP
    api.activities = [_recent(f"i{n:02d}", days_ago=10 + n) for n in range(cap + 3)]

    assert _sweep(conn) == (0, cap)
    assert "3 missing rides left" in capsys.readouterr().out
    assert len(api.fetched) == cap

    assert _sweep(conn) == (0, 3)
    assert _sweep(conn) == (0, 0)
    assert len(_stored_ids(conn)) == cap + 3
    assert len(api.fetched) == cap + 3, "each ride downloaded once"


def test_a_ride_that_keeps_failing_is_not_counted_and_blocks_nothing(conn, api):
    api.activities = [_recent("bad", days_ago=40), _recent("good", days_ago=41)]
    api.responses["bad"] = [UNWRITABLE_STREAMS, UNWRITABLE_STREAMS, _streams()]

    assert _sweep(conn) == (0, 1)
    assert _stored_ids(conn) == ["good"]

    assert _sweep(conn) == (0, 0), "a failure is retried, not counted"
    assert _sweep(conn) == (0, 1)
    assert _stored_ids(conn) == ["bad", "good"]


def test_a_stored_ride_on_the_sweeps_edge_is_not_remote_only(conn, api):
    """The local window is a day narrower than the remote one, so this ride is in
    remote-minus-local while being stored all along."""
    for days_ago, activity_id in enumerate(("i1", "i2", "i3"), start=2):
        _seed_recent(conn, activity_id, days_ago=days_ago)
    _seed_recent(conn, "edge", days_ago=90)
    api.activities = [_recent("i1", days_ago=2), _recent("i2", days_ago=3),
                      _recent("i3", days_ago=4), _recent("edge", days_ago=90)]

    assert _sweep(conn) == (0, 0)
    assert len(api.list_calls) == 1


def test_a_second_upload_does_not_swap_the_stored_id_every_sweep(conn, api):
    """Ingesting it would move the row to the other id, which makes the first look
    missing tomorrow, and each swap throws away the ride's derived metrics."""
    started = _days_ago(40)
    _seed_row(conn, intervals_icu_id="first", date=started, duration_s=7180,
              intervals_icu_analyzed=ANALYZED, np=250.0)
    api.activities = [_activity("first", start_date=started),
                      _activity("second", start_date=started)]

    for _sweep_number in range(3):
        assert _sweep(conn) == (0, 0)

    assert _stored_ids(conn) == ["first"]
    assert _rows(conn, "SELECT np FROM activities") == [(250.0,)]
    assert api.fetched == []


def test_a_response_too_doubtful_to_delete_from_is_not_ingested_from(conn, api):
    for days_ago in range(2, 8):
        _seed_recent(conn, f"i{days_ago}", days_ago=days_ago)
    api.activities = [_recent("i2", days_ago=2), _recent("new", days_ago=40)]

    assert _sweep(conn) == (0, 0)
    assert api.fetched == []
    assert _row(conn, "new", "name") == {}


def test_a_deferred_bulk_deletion_ingests_nothing_and_deletes_nothing(conn, api):
    """The deletion cap and its two-sweep confirmation are untouched."""
    import intervals_icu

    ids = [f"i{n:02d}" for n in range(20)]
    for days_ago, activity_id in enumerate(ids, start=2):
        _seed_recent(conn, activity_id, days_ago=days_ago)
    survivors = [_recent(activity_id, days_ago=days_ago)
                 for days_ago, activity_id in enumerate(ids[:12], start=2)]
    api.activities = survivors + [_recent("new", days_ago=40)]

    assert _sweep(conn) == (0, 0)
    assert len(_stored_ids(conn)) == 20
    assert intervals_icu.MAX_DELETIONS_PER_SWEEP == 5

    # The identical set again is a real bulk deletion, and then the sweep carries on.
    assert _sweep(conn) == (8, 1)
    assert sorted(_stored_ids(conn)) == sorted(ids[:12] + ["new"])


def test_a_ride_reuploaded_after_a_deletion_lands_after_the_deletion_runs(conn, api):
    started = _days_ago(40)
    for days_ago, activity_id in enumerate(("i1", "i2", "i3"), start=2):
        _seed_recent(conn, activity_id, days_ago=days_ago)
    _seed_row(conn, intervals_icu_id="old_upload", date=started, duration_s=7180,
              intervals_icu_analyzed=ANALYZED)
    api.activities = [_recent("i1", days_ago=2), _recent("i2", days_ago=3),
                      _recent("i3", days_ago=4), _activity("new_upload", start_date=started)]

    assert _sweep(conn) == (1, 1)
    assert _stored_ids(conn) == ["i1", "i2", "i3", "new_upload"]


# ---------------------------------------------------------------------------
# Startup: what a deploy does to an install that is already running
# ---------------------------------------------------------------------------

class _ReachedThePollLoop(Exception):
    pass


class _Startup:
    """Runs main.run() against the test database until it reaches the poll loop."""

    def __init__(self, conn, api, monkeypatch):
        sys.modules.setdefault("schedule", MagicMock())
        import intervals_icu
        import main

        self.conn, self.api, self.main = conn, api, main
        self.monkeypatch = monkeypatch
        self.backfills = []
        self.recalculations = 0
        real_backfill = intervals_icu.backfill

        def _spy(conn, months=12):
            self.backfills.append(months)
            return real_backfill(conn, months=months)

        def _recalculate(_conn):
            self.recalculations += 1

        monkeypatch.setattr(intervals_icu, "backfill", _spy)
        monkeypatch.setattr(main, "recalculate_fitness", _recalculate)
        monkeypatch.setattr(main, "_recalc_pending", False)
        main._delivered.clear()
        # Replaces the whole module rather than one attribute of it: pytest 9.2's
        # undo deletes an attribute a MagicMock only ever made up, and a deleted
        # attribute stays deleted, so patching it fails for every later test.
        monkeypatch.setattr(main, "schedule",
                            MagicMock(**{"run_pending.side_effect": _ReachedThePollLoop}))
        monkeypatch.setenv("DATABASE_URL", TEST_DB)
        monkeypatch.setenv("INTERVALS_ICU_ATHLETE_ID", "i0")
        monkeypatch.setenv("INTERVALS_ICU_API_KEY", "not-a-real-key")
        for name in ("STRAVA_CLIENT_ID", "STRAVA_CLIENT_SECRET", "STRAVA_REFRESH_TOKEN",
                     "RWGPS_API_KEY", "RWGPS_AUTH_TOKEN", "INTERVALS_ICU_SYNC_WINDOW_DAYS",
                     "VELOMATE_RESET_RIDE_FTP"):
            monkeypatch.delenv(name, raising=False)

    def run(self, months="12"):
        self.monkeypatch.setenv("VELOMATE_BACKFILL_MONTHS", months)
        with pytest.raises(_ReachedThePollLoop):
            self.main.run()

    def oldest_listing_days_back(self) -> int:
        oldest = min(call["oldest"] for call in self.api.list_calls)
        then = datetime.fromisoformat(oldest).replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - then).days


@pytest.fixture()
def startup(conn, api, monkeypatch):
    return _Startup(conn, api, monkeypatch)


def _seed_populated_install(conn, strava_key=True, persisted_months="12", icu_rows=True):
    """The deployed state this change lands on, with synthetic rides."""
    import db as velomate_db

    _seed_row(conn, strava_id=1, date=_days_ago(250))
    if icu_rows:
        _seed_row(conn, strava_id=2, intervals_icu_id="both", date=_days_ago(200),
                  intervals_icu_analyzed=ANALYZED)
        _seed_row(conn, intervals_icu_id="icu_only", date=_days_ago(100),
                  intervals_icu_analyzed=ANALYZED)
    if strava_key:
        velomate_db.set_sync_state(conn, "strava_last_activity_epoch", "1782126011")
    if persisted_months is not None:
        velomate_db.set_sync_state(conn, "configured_backfill_months", persisted_months)


# Rides intervals.icu holds that this install does not. A backfill would take them.
def _unfetched_history():
    return [_recent("history1", days_ago=120), _recent("history2", days_ago=220)]


@pytest.mark.parametrize("state", [
    pytest.param({}, id="as_deployed"),
    pytest.param({"strava_key": False}, id="no_strava_key_so_only_rows_say_there_is_data"),
    pytest.param({"icu_rows": False}, id="newly_configured_source_over_strava_rows"),
    pytest.param({"icu_rows": False, "strava_key": False},
                 id="newly_configured_source_and_no_cursor_at_all"),
    pytest.param({"persisted_months": None}, id="window_never_persisted"),
])
def test_a_populated_install_does_not_backfill_on_first_start(conn, api, startup, state):
    """The hard requirement. A merge deploys here unattended, with
    VELOMATE_BACKFILL_MONTHS unchanged and no backfill ever run for this source."""
    _seed_populated_install(conn, **state)
    before = _rows(conn, "SELECT * FROM activities ORDER BY id")
    api.activities = _unfetched_history()

    startup.run(months="12")

    assert startup.backfills == []
    assert startup.oldest_listing_days_back() <= 14, "only the normal poll window"
    assert api.fetched == []
    assert _rows(conn, "SELECT * FROM activities ORDER BY id") == before
    assert not _backfill_pending(conn)
    assert startup.recalculations == 1, "the usual startup recalculation still runs"

    # Nor on the restart after, now that the first start has persisted its state.
    startup.run(months="12")
    assert startup.backfills == []


def test_an_empty_install_backfills_once(conn, api, startup):
    import db as velomate_db

    api.activities = _unfetched_history()

    startup.run(months="12")

    assert startup.backfills == [12]
    assert _stored_ids(conn) == ["history1", "history2"]
    assert not _backfill_pending(conn)
    assert velomate_db.get_sync_state(conn, "configured_backfill_months") == "12"
    assert startup.recalculations == 1

    startup.run(months="12")
    assert startup.backfills == [12], "a finished install does not backfill again"


@pytest.mark.parametrize("months, expected", [("24", 24), ("0", 0)], ids=["longer", "full"])
def test_extending_the_window_is_the_deliberate_trigger(conn, api, startup, months, expected):
    import db as velomate_db

    _seed_populated_install(conn)
    api.activities = _unfetched_history()

    startup.run(months=months)

    assert startup.backfills == [expected]
    assert _stored_ids(conn) == ["both", "history1", "history2", "icu_only"]
    assert velomate_db.get_sync_state(conn, "configured_backfill_months") == months

    startup.run(months=months)
    assert startup.backfills == [expected], "the extension fires once"


def test_shrinking_the_window_does_not_backfill(conn, api, startup):
    _seed_populated_install(conn)
    api.activities = _unfetched_history()

    startup.run(months="6")

    assert startup.backfills == []
    assert api.fetched == []


def test_a_restart_resumes_a_backfill_that_crashed_on_an_empty_install(conn, api, startup):
    """Rows now exist and the window has not changed, so nothing but the pending
    key can say the history is incomplete."""
    api.activities = _unfetched_history()
    api.responses["history2"] = [KeyboardInterrupt()]

    with pytest.raises(KeyboardInterrupt):
        startup.run(months="12")
    assert _stored_ids(conn) == ["history1"]
    assert _backfill_pending(conn)

    startup.run(months="12")

    assert startup.backfills == [12, 12]
    assert _stored_ids(conn) == ["history1", "history2"]
    assert not _backfill_pending(conn)


def test_a_backfill_that_cannot_reach_the_api_does_not_stop_the_container(conn, api, startup,
                                                                          monkeypatch):
    """Docker restarts a dead ingestor, so raising here is a crash loop."""
    import intervals_icu

    def _down(**_kwargs):
        raise ConnectionError("intervals.icu unreachable")

    monkeypatch.setattr(intervals_icu, "list_activities", _down)

    startup.run(months="12")   # reaches the poll loop

    assert startup.backfills == [12]
    assert _backfill_pending(conn), "the next start or daily sweep has to retry it"


def _break_recalculation(startup, monkeypatch):
    """Returns the working stub, to put back once the test wants it fixed."""
    def _broken(_conn):
        raise ValueError("a ride recalculation cannot handle")

    working = startup.main.recalculate_fitness
    monkeypatch.setattr(startup.main, "recalculate_fitness", _broken)
    return working


def test_a_recalculation_failure_at_startup_stops_nothing_and_is_retried(
        conn, api, startup, monkeypatch):
    """The rides are stored, so no later poll sees a change. Dropping the failure
    would leave them without metrics, and raising it restarts into the same one."""
    working = _break_recalculation(startup, monkeypatch)
    api.activities = _unfetched_history()

    startup.run(months="12")   # empty install: run_backfill's recalculation
    startup.run(months="12")   # populated: the startup recalculation

    assert startup.backfills == [12]
    assert _stored_ids(conn) == ["history1", "history2"]
    assert startup.main._recalc_pending is True

    monkeypatch.setattr(startup.main, "recalculate_fitness", working)
    startup.main.poll_intervals_icu()

    assert startup.recalculations == 1, "the next poll retries it with nothing new to ingest"
    assert startup.main._recalc_pending is False


def test_a_recalculation_failure_after_the_sweep_is_retried_too(conn, api, startup, monkeypatch):
    working = _break_recalculation(startup, monkeypatch)
    api.activities = [_recent("gap", days_ago=40)]

    startup.main._reconcile_intervals_icu()   # must not raise

    assert _stored_ids(conn) == ["gap"]
    assert startup.main._recalc_pending is True

    monkeypatch.setattr(startup.main, "recalculate_fitness", working)
    startup.main.poll_intervals_icu()

    assert startup.recalculations == 1


def test_a_backfill_or_sweep_is_never_counted_as_a_poll(conn, api, startup):
    """Ingest health reports on the poll loop. A backfill that worked says nothing
    about it, and last_successful_poll must not move on its account."""
    import db as velomate_db

    api.activities = _unfetched_history()

    startup.main.run_backfill(["intervals_icu"])
    startup.main._reconcile_intervals_icu()

    assert _stored_ids(conn) == ["history1", "history2"]
    assert startup.main._delivered == set()
    assert velomate_db.get_sync_state(conn, startup.main.LAST_SUCCESSFUL_POLL_KEY) is None


def test_a_first_run_stopped_by_another_source_still_backfills_intervals_icu(
        conn, api, startup, monkeypatch):
    """Rows from an earlier source make has_data true on the next start, so the
    pending key is all that says intervals.icu still has to run."""
    monkeypatch.setattr(startup.main.rwgps, "backfill",
                        MagicMock(side_effect=RuntimeError("killed")))

    with pytest.raises(RuntimeError):
        startup.main.run_backfill(["rwgps", "intervals_icu"])

    assert _backfill_pending(conn)


def test_a_failing_backfill_does_not_fail_the_poll_after_it(conn, api, startup):
    """The reverse: a ride a backfill could not store is outside the poll window,
    so it must not read as the poll delivering less than a healthy one."""
    import db as velomate_db

    api.activities = [_recent("bad", days_ago=100)]
    api.responses["bad"] = [UNWRITABLE_STREAMS]

    startup.main.run_backfill(["intervals_icu"])
    assert _backfill_pending(conn)
    assert velomate_db.get_sync_state(conn, startup.main.LAST_SUCCESSFUL_POLL_KEY) is None

    startup.main.poll_intervals_icu()

    assert velomate_db.get_sync_state(conn, startup.main.LAST_SUCCESSFUL_POLL_KEY)


def test_the_daily_sweep_resumes_an_unfinished_backfill(conn, api, startup, monkeypatch):
    import db as velomate_db
    import intervals_icu

    monkeypatch.setenv("VELOMATE_BACKFILL_MONTHS", "12")
    velomate_db.set_sync_state(conn, intervals_icu.BACKFILL_PENDING_KEY, "12")
    api.activities = _unfetched_history()

    startup.main._reconcile_intervals_icu()

    assert startup.backfills == [12]
    assert _stored_ids(conn) == ["history1", "history2"]
    assert not _backfill_pending(conn)
    assert startup.recalculations == 1


def test_the_daily_sweep_leaves_a_finished_install_alone(conn, api, startup):
    """Rides past the sweep window stay unfetched, however long they wait."""
    _seed_recent(conn, "i1", days_ago=5)
    api.activities = [_recent("i1", days_ago=5)] + _unfetched_history()

    startup.main._reconcile_intervals_icu()

    assert startup.backfills == []
    assert api.fetched == []
    assert startup.recalculations == 0, "nothing landed, so nothing to recalculate"


def test_the_daily_sweep_recalculates_after_ingesting(conn, api, startup):
    api.activities = [_recent("gap", days_ago=40)]

    startup.main._reconcile_intervals_icu()

    assert _stored_ids(conn) == ["gap"]
    assert startup.recalculations == 1


def test_a_backfilled_ride_comes_out_of_a_real_recalculation_with_metrics(conn, api, startup,
                                                                          monkeypatch):
    """End to end, with nothing between the stored ride and CTL stubbed."""
    import db as velomate_db
    import fitness

    monkeypatch.setattr(startup.main, "recalculate_fitness", fitness.recalculate_fitness)
    for name in ("VELOMATE_FTP", "VELOMATE_WEIGHT", "VELOMATE_MAX_HR", "VELOMATE_RESTING_HR",
                 "STRAVA_CLIENT_ID", "STRAVA_CLIENT_SECRET", "STRAVA_REFRESH_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    velomate_db.set_sync_state(conn, "metrics_version", fitness.METRICS_VERSION)
    api.activities = [_recent("i1", days_ago=30)]
    api.responses["i1"] = [_streams(watts=200, samples=600)]

    startup.run(months="12")

    assert _row(conn, "i1", "np")["np"] == pytest.approx(200, abs=5)
    assert _row(conn, "i1", "tss")["tss"] > 0
    assert _rows(conn, "SELECT COUNT(*) FROM athlete_stats WHERE ctl > 0")[0][0] > 0


# ---------------------------------------------------------------------------
# A re-analysis replaces the streams, so everything read from them goes too
# ---------------------------------------------------------------------------

def _ride_streams(watts, samples=600) -> list:
    """Long enough for recalculation to fill NP, and on a road that climbs."""
    power = watts if callable(watts) else (lambda _s: watts)
    return [
        {"type": "time", "data": list(range(samples))},
        {"type": "watts", "data": [power(s) for s in range(samples)]},
        {"type": "heartrate", "data": [140] * samples},
        {"type": "cadence", "data": [85] * samples},
        {"type": "velocity_smooth", "data": [7.0] * samples},
        {"type": "altitude", "data": [100.0 + s * 0.5 for s in range(samples)]},
        {"type": "latlng", "data": [38.7 + s * 0.001 for s in range(samples)],
         "data2": [-9.14] * samples},
    ]


def _recalculate(conn):
    """Recalculate as a running deployment does: with the version already stored,
    or the first run would reset every ride and pass whatever the sync left."""
    import db as velomate_db
    import fitness

    velomate_db.set_sync_state(conn, "metrics_version", fitness.METRICS_VERSION)
    fitness.recalculate_fitness(conn)


def _analysed_ride(conn, api, streams) -> int:
    """One ride, polled and recalculated, as it stands before a re-analysis."""
    api.activities = [_activity()]
    api.responses["i1"] = [streams]
    assert _poll(conn) == 1
    _recalculate(conn)
    return _rows(conn, "SELECT id FROM activities WHERE intervals_icu_id = 'i1'")[0][0]


def _reanalyse(conn, api, streams) -> int:
    api.activities = [_activity(analyzed=REANALYZED)]
    api.responses["i1"] = [streams]
    return _poll(conn)


def test_a_reanalysed_ride_is_recomputed_from_its_new_streams(conn, api):
    """Recalculation only selects a ride with no NP, so without the reset the
    numbers go on describing streams that are no longer stored."""
    _analysed_ride(conn, api, _ride_streams(200))
    assert _row(conn, "i1", "np")["np"] == pytest.approx(200, abs=5)

    assert _reanalyse(conn, api, _ride_streams(300)) == 1
    _recalculate(conn)

    row = _row(conn, "i1", "np", "work_kj", "tss", "wbal_w_prime_j")
    assert row["np"] == pytest.approx(300, abs=5)
    assert row["work_kj"] == pytest.approx(180, abs=1)
    assert row["tss"] > 0
    assert row["wbal_w_prime_j"] is not None


def test_a_reanalysis_rebuilds_the_rides_intervals(conn, api):
    """Step 2.5 only detects on a ride with no rows, and these index into the old
    stream, so a stale one would point at the wrong seconds for good."""
    ride = _analysed_ride(conn, api, _ride_streams(
        lambda s: 280 if 100 <= s < 340 else 120))
    with conn.cursor() as cur:
        cur.execute("UPDATE activities SET ride_ftp = 250 WHERE id = %s", (ride,))
        cur.execute("DELETE FROM ride_intervals WHERE activity_id = %s", (ride,))
    _recalculate(conn)
    assert _rows(conn, "SELECT start_offset_s, duration_s, classification "
                       "FROM ride_intervals WHERE activity_id = %s", ride
                 ) == [(100, 240, "vo2")]

    assert _reanalyse(conn, api, _ride_streams(
        lambda s: 280 if 300 <= s < 540 else 120)) == 1
    _recalculate(conn)

    assert _rows(conn, "SELECT start_offset_s, duration_s, classification "
                       "FROM ride_intervals WHERE activity_id = %s", ride
                 ) == [(300, 240, "vo2")]


def test_a_reanalysis_rebuilds_detected_climbs_and_keeps_stravas(conn, api):
    """Detection skips a ride holding any detected or 'none' row. Strava climbs
    are external and hold no stream offsets, so a new stream can't invalidate one."""
    ride = _analysed_ride(conn, api, _ride_streams(200))
    with conn.cursor() as cur:
        cur.execute("""
            UPDATE ride_climbs SET gain_m = 999 WHERE activity_id = %s;
            INSERT INTO ride_climbs (activity_id, gain_m, category, source, segment_name)
            VALUES (%s, 120, 'Cat 3', 'strava', 'Serra Climb');
        """, (ride, ride))

    assert _reanalyse(conn, api, _ride_streams(200)) == 1
    _recalculate(conn)

    rows = _rows(conn, "SELECT source, gain_m FROM ride_climbs WHERE activity_id = %s", ride)
    assert ("strava", 120) in rows
    assert {source for source, _gain in rows} & {"detected", "none"}
    assert not [row for row in rows if row[1] == 999], rows


def test_a_reanalysis_keeps_ride_ftp_and_weight(conn, api):
    """Neither comes from this ride's streams. Re-deriving ride_ftp under a
    since-changed FTP would rewrite a historical TSS."""
    ride = _analysed_ride(conn, api, _ride_streams(200))
    with conn.cursor() as cur:
        cur.execute("UPDATE activities SET ride_ftp = 277, ride_weight = 71.5 WHERE id = %s",
                    (ride,))

    assert _reanalyse(conn, api, _ride_streams(300)) == 1
    _recalculate(conn)

    assert _row(conn, "i1", "ride_ftp", "ride_weight") == {
        "ride_ftp": 277, "ride_weight": 71.5}


def _stamp_derived(conn, ride):
    """Values recalculation cannot produce, so a survivor is plain to see."""
    with conn.cursor() as cur:
        cur.execute("UPDATE activities SET np = 999, tss = 999 WHERE id = %s", (ride,))


def test_a_poll_that_sees_no_reanalysis_touches_nothing(conn, api):
    """The control. The window holds the same rides every poll, so a reset here
    would recompute every recent ride every cycle."""
    ride = _analysed_ride(conn, api, _ride_streams(200))
    _stamp_derived(conn, ride)
    intervals_and_climbs = _rows(conn, """
        SELECT 'interval', COUNT(*) FROM ride_intervals WHERE activity_id = %s
        UNION ALL SELECT 'climb', COUNT(*) FROM ride_climbs WHERE activity_id = %s
    """, ride, ride)

    assert _poll(conn) == 0

    assert _row(conn, "i1", "np", "tss") == {"np": 999, "tss": 999}
    assert _rows(conn, """
        SELECT 'interval', COUNT(*) FROM ride_intervals WHERE activity_id = %s
        UNION ALL SELECT 'climb', COUNT(*) FROM ride_climbs WHERE activity_id = %s
    """, ride, ride) == intervals_and_climbs


def test_a_reanalysis_with_no_streams_keeps_what_the_old_ones_gave(conn, api):
    """Nothing was replaced, so the stored numbers still describe the stored streams."""
    ride = _analysed_ride(conn, api, _ride_streams(200))
    _stamp_derived(conn, ride)

    assert _reanalyse(conn, api, []) == 1

    assert _row(conn, "i1", "np", "tss") == {"np": 999, "tss": 999}
    assert _rows(conn, "SELECT COUNT(*) FROM activity_streams WHERE activity_id = %s",
                 ride) == [(600,)]


def test_a_failed_stream_write_keeps_the_derived_values(conn, api):
    """The reset shares the stream write's transaction, or a retry would find a
    ride with its old streams and no numbers."""
    ride = _analysed_ride(conn, api, _ride_streams(200))
    _stamp_derived(conn, ride)

    assert _reanalyse(conn, api, UNWRITABLE_STREAMS) == 0

    assert _row(conn, "i1", "np", "tss") == {"np": 999, "tss": 999}
