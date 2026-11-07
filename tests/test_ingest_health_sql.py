"""The health panels and the stalled-ingestion alert, executed.

The behaviour under test is SQL: whether a missing key emits no row or a NULL
one, and what the alert's number is in each state. A colorMode:value stat that
renders a NULL row blanks the whole dashboard, so that has to be observed, not
inferred. The queries are read from the dashboard JSON and alerts.yaml, and the
polls run for real against the schema with only the network stubbed.

Run with:
    VELOMATE_TEST_DATABASE_URL=postgresql://... python3 -m pytest \
        tests/test_ingest_health_sql.py -q
"""

import json
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import yaml

from pg_reset import reset_schema

TEST_DB = os.environ.get("VELOMATE_TEST_DATABASE_URL")

pytestmark = pytest.mark.skipif(
    not TEST_DB,
    reason="needs a throwaway PostgreSQL in VELOMATE_TEST_DATABASE_URL",
)

_REPO = Path(__file__).resolve().parent.parent
OVERVIEW = _REPO / "grafana" / "dashboards" / "metric" / "overview.json"
ALERTS_FILE = _REPO / "grafana" / "provisioning" / "alerting" / "alerts.yaml"

LAST_SYNC_ID = 1800
SKIPPED_ID = 1801

# Rows every ingestor start rewrites, so they exist on any deployed database.
SETTLED_KEY = "configured_ftp"


def _panel_sql(panel_id):
    panels = json.loads(OVERVIEW.read_text(encoding="utf-8"))["panels"]
    return next(p for p in panels if p["id"] == panel_id)["targets"][0]["rawSql"]


def _alert():
    config = yaml.safe_load(ALERTS_FILE.read_text(encoding="utf-8"))
    rules = [r for group in config["groups"] for r in group["rules"]]
    return next(r for r in rules if r["uid"] == "velomate-ingest-stalled")


def _alert_sql():
    return _alert()["data"][0]["model"]["rawSql"]


def _threshold_hours():
    data = {d["refId"]: d for d in _alert()["data"]}
    return data["C"]["model"]["conditions"][0]["evaluator"]["params"][0]


def _rows(conn, sql):
    with conn.cursor() as cur:
        cur.execute(sql)
        return cur.fetchall()


def _alert_value(conn):
    """What Grafana's reduce step gets. Exactly one row, or the rule goes NoData."""
    rows = _rows(conn, _alert_sql())
    assert len(rows) == 1, f"alert query must always emit one row, got {rows}"
    when, hours = rows[0]
    assert when is not None
    assert hours is not None, "a NULL would reach the threshold as NaN"
    return hours


def _alert_fires(conn):
    return _alert_value(conn) > _threshold_hours()


def _put(conn, key, value, age_hours):
    with conn.cursor() as cur:
        cur.execute(
            """INSERT INTO sync_state (key, value, last_synced_at)
               VALUES (%s, %s, NOW() - make_interval(hours => %s))
               ON CONFLICT (key) DO UPDATE
               SET value = EXCLUDED.value, last_synced_at = EXCLUDED.last_synced_at""",
            (key, value, age_hours),
        )


@pytest.fixture()
def conn():
    import psycopg2

    if not hasattr(psycopg2, "__file__"):
        pytest.skip(
            "psycopg2 is mocked by another module in this pytest session; run "
            "this file on its own: python -m pytest tests/test_ingest_health_sql.py"
        )

    import db as velomate_db

    connection = psycopg2.connect(TEST_DB)
    connection.autocommit = True
    reset_schema(connection)
    velomate_db.create_schema(connection)
    yield connection
    connection.close()


class TestLastSyncPanel:
    def test_key_missing_emits_no_row_at_all(self, conn):
        """Not a row holding NULL: that is what blanks the dashboard."""
        assert _rows(conn, _panel_sql(LAST_SYNC_ID)) == []

    def test_other_keys_present_still_emits_no_row(self, conn):
        _put(conn, SETTLED_KEY, "0", 1)
        assert _rows(conn, _panel_sql(LAST_SYNC_ID)) == []

    def test_null_timestamp_emits_no_row(self, conn):
        with conn.cursor() as cur:
            cur.execute("INSERT INTO sync_state (key, value) VALUES ('last_successful_poll', 'x')")
        assert _rows(conn, _panel_sql(LAST_SYNC_ID)) == []

    def test_fresh_is_a_small_number(self, conn):
        _put(conn, "last_successful_poll", "x", 0)
        [(seconds,)] = _rows(conn, _panel_sql(LAST_SYNC_ID))
        assert 0 <= seconds < 60

    def test_stale_is_the_real_age_in_seconds(self, conn):
        _put(conn, "last_successful_poll", "x", 30)
        [(seconds,)] = _rows(conn, _panel_sql(LAST_SYNC_ID))
        assert 30 * 3600 <= seconds < 30 * 3600 + 60

    def test_unreadable_value_changes_nothing(self, conn):
        _put(conn, "last_successful_poll", "not a timestamp", 2)
        [(seconds,)] = _rows(conn, _panel_sql(LAST_SYNC_ID))
        assert 2 * 3600 <= seconds < 2 * 3600 + 60


class TestSkippedPanel:
    def test_key_missing_emits_no_row_at_all(self, conn):
        assert _rows(conn, _panel_sql(SKIPPED_ID)) == []

    def test_zero_is_a_row_holding_zero(self, conn):
        """Not no-rows: green has to be reachable."""
        _put(conn, "intervals_icu_skipped_stubs", "0", 0)
        assert _rows(conn, _panel_sql(SKIPPED_ID)) == [(0,)]

    def test_a_count_comes_back_as_a_number(self, conn):
        _put(conn, "intervals_icu_skipped_stubs", "3", 0)
        assert _rows(conn, _panel_sql(SKIPPED_ID)) == [(3,)]

    @pytest.mark.parametrize("garbage", ["", "n/a", "3 rides"])
    def test_unreadable_value_emits_no_row_rather_than_an_error(self, conn, garbage):
        _put(conn, "intervals_icu_skipped_stubs", garbage, 0)
        assert _rows(conn, _panel_sql(SKIPPED_ID)) == []


class TestAlert:
    def test_fresh_does_not_fire(self, conn):
        _put(conn, "last_successful_poll", "x", 0)
        assert not _alert_fires(conn)

    def test_stale_fires(self, conn):
        _put(conn, "last_successful_poll", "x", _threshold_hours() + 1)
        assert _alert_fires(conn)

    def test_just_inside_the_threshold_does_not_fire(self, conn):
        _put(conn, "last_successful_poll", "x", _threshold_hours() - 1)
        assert not _alert_fires(conn)

    def test_empty_sync_state_does_not_fire(self, conn):
        """A brand new install where the ingestor has not run yet: a real row
        holding zero, so the rule stays Normal rather than going NoData."""
        assert _alert_value(conn) == 0

    def test_never_succeeded_on_a_young_install_does_not_fire(self, conn):
        """First start, first poll still to come."""
        _put(conn, SETTLED_KEY, "0", 1)
        assert not _alert_fires(conn)

    def test_never_succeeded_for_longer_than_the_threshold_fires(self, conn):
        """A key that never appeared must not read as healthy for ever: a first
        poll that never succeeds is the worst stall there is."""
        _put(conn, SETTLED_KEY, "0", _threshold_hours() + 1)
        assert _alert_fires(conn)

    def test_a_stale_key_is_not_rescued_by_fresher_rows(self, conn):
        """The ingestor restarting rewrites configured_*, and it must not look
        like a successful poll."""
        _put(conn, "last_successful_poll", "x", _threshold_hours() + 1)
        _put(conn, SETTLED_KEY, "0", 0)
        assert _alert_fires(conn)

    def test_unreadable_value_cannot_fail_the_query(self, conn):
        """execErrState defaults to Alerting, so a query error would fire. The
        rule reads the typed column and never casts `value`."""
        _put(conn, "last_successful_poll", "not a timestamp", 0)
        assert not _alert_fires(conn)


@pytest.fixture()
def polled(conn, monkeypatch):
    """Real poll_intervals_icu on the real schema, network stubbed."""
    sys.modules.setdefault("schedule", MagicMock())
    _ingestor_dir = str(_REPO / "ingestor")
    if _ingestor_dir not in sys.path:
        sys.path.insert(0, _ingestor_dir)
    import intervals_icu
    import main as ingestor_main

    ingestor_main._delivered.clear()
    ingestor_main._recalc_pending = False
    monkeypatch.setenv("DATABASE_URL", TEST_DB)
    monkeypatch.setenv("INTERVALS_ICU_ATHLETE_ID", "i1")
    monkeypatch.setenv("INTERVALS_ICU_API_KEY", "k")
    for name in ("STRAVA_CLIENT_ID", "RWGPS_API_KEY"):
        monkeypatch.delenv(name, raising=False)

    def run(activities=None, list_error=None, stream_error=None):
        with patch.object(intervals_icu, "list_activities",
                          return_value=activities or [], side_effect=list_error), \
                patch.object(intervals_icu, "get_streams", return_value=[
                    {"type": "time", "data": [0, 1]}, {"type": "watts", "data": [100, 110]},
                ], side_effect=stream_error), \
                patch.object(intervals_icu, "time"), \
                patch.object(ingestor_main, "recalculate_fitness"):
            ingestor_main.poll_intervals_icu()

    yield run
    ingestor_main._delivered.clear()
    ingestor_main._recalc_pending = False


def _ride():
    return {"id": "i42", "name": "Synthetic", "type": "Ride",
            "start_date": "2026-11-01T09:00:00Z", "distance": 20000,
            "moving_time": 3600, "analyzed": "2026-11-01T10:00:00.000+00:00"}


STUB = {"id": "18069132600", "source": "STRAVA", "_note": "STRAVA activities are not available"}


class TestRealPollThroughToTheQueries:
    def test_a_good_poll_turns_every_query_healthy(self, conn, polled):
        polled([STUB, _ride()])
        [(seconds,)] = _rows(conn, _panel_sql(LAST_SYNC_ID))
        assert seconds < 60
        assert _rows(conn, _panel_sql(SKIPPED_ID)) == [(1,)]
        assert not _alert_fires(conn)

    def test_a_rest_week_stays_healthy(self, conn, polled):
        """What separates this alert from Detraining: no rides, no fire."""
        polled([])
        assert _rows(conn, _panel_sql(LAST_SYNC_ID)) != []
        assert _rows(conn, _panel_sql(SKIPPED_ID)) == [(0,)]
        _put(conn, SETTLED_KEY, "0", _threshold_hours() + 1)
        assert not _alert_fires(conn)

    def test_an_empty_list_over_stored_rides_goes_quiet_for_real(self, conn, polled):
        """Same 200 as the rest week. The difference is only the stored ride, so
        this runs the query for real instead of stubbing the count."""
        from datetime import datetime, timedelta, timezone
        ride = dict(_ride(), start_date=(datetime.now(timezone.utc) - timedelta(days=3))
                    .strftime("%Y-%m-%dT%H:%M:%SZ"))
        polled([ride])
        with conn.cursor() as cur:
            cur.execute("DELETE FROM sync_state WHERE key = 'last_successful_poll'")
        polled([])
        assert _rows(conn, _panel_sql(LAST_SYNC_ID)) == []

    def test_a_ride_on_the_window_edge_does_not_make_an_empty_list_suspicious(self, conn, polled):
        """`date` is the UTC start and the API filters on local time, so a ride
        just inside our window can be just outside theirs. Every rest fortnight
        passes through this state as the last ride ages out."""
        from datetime import datetime, timedelta, timezone
        midnight = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
        edge = midnight - timedelta(days=14) + timedelta(hours=12)
        polled([dict(_ride(), start_date=edge.strftime("%Y-%m-%dT%H:%M:%SZ"))])
        with conn.cursor() as cur:
            cur.execute("DELETE FROM sync_state WHERE key = 'last_successful_poll'")
        polled([])
        assert _rows(conn, _panel_sql(LAST_SYNC_ID)) != []

    def test_a_recent_ride_from_another_source_proves_nothing(self, conn, polled):
        """Only a ride intervals.icu once served can be missing from its list."""
        from datetime import datetime, timedelta, timezone
        with conn.cursor() as cur:
            cur.execute(
                """INSERT INTO activities (name, date, distance_m, duration_s,
                                           elevation_m, sport_type)
                   VALUES ('Synthetic', %s, 20000.0, 3600, 100.0, 'cycling_outdoor')""",
                (datetime.now(timezone.utc) - timedelta(days=2),),
            )
        polled([])
        assert _rows(conn, _panel_sql(LAST_SYNC_ID)) != []

    def test_a_failed_poll_leaves_the_panel_empty_and_the_clock_running(self, conn, polled):
        _put(conn, SETTLED_KEY, "0", _threshold_hours() + 1)
        polled(list_error=RuntimeError("403"))
        assert _rows(conn, _panel_sql(LAST_SYNC_ID)) == []
        assert _alert_fires(conn)

    def test_a_stub_only_window_writes_the_count_but_not_the_success(self, conn, polled):
        polled([STUB])
        assert _rows(conn, _panel_sql(LAST_SYNC_ID)) == []
        assert _rows(conn, _panel_sql(SKIPPED_ID)) == [(1,)]

    def test_a_missing_table_does_not_break_the_poll(self, conn, polled):
        """First start safety, with a real failure rather than a stubbed one."""
        with conn.cursor() as cur:
            cur.execute("ALTER TABLE sync_state RENAME TO sync_state_away")
        try:
            polled([_ride()])       # must not raise
            with conn.cursor() as cur:
                cur.execute("SELECT COUNT(*) FROM activities WHERE intervals_icu_id = 'i42'")
                assert cur.fetchone()[0] == 1, "the ride itself still has to land"
        finally:
            with conn.cursor() as cur:
                cur.execute("ALTER TABLE sync_state_away RENAME TO sync_state")
