"""A stalled pipeline has to be visible.

The 2026-06 to 08 outage ran for two months with the container up and nothing
arriving. What decides whether the health key advances is therefore the whole
point, and the interesting cases are the ones where no exception reaches the
poll: a swallowed per-activity failure, a window of nothing but stubs, an empty
200. Each test here drives the real poll and the real sync; only the network and
the row store are stubbed, and the assertion reads what ended up in sync_state.

tests/test_ingest_health_sql.py runs the same flow against a real PostgreSQL and
executes the panel and alert SQL.
"""

import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import yaml

sys.modules.setdefault("psycopg2", MagicMock())
sys.modules.setdefault("psycopg2.extras", MagicMock())
sys.modules.setdefault("requests", MagicMock())
sys.modules.setdefault("schedule", MagicMock())

_ingestor_dir = Path(__file__).resolve().parent.parent / "ingestor"
if str(_ingestor_dir) not in sys.path:
    sys.path.insert(0, str(_ingestor_dir))

import intervals_icu  # noqa: E402
import main as ingestor_main  # noqa: E402
import rwgps  # noqa: E402
import strava  # noqa: E402

_REPO = Path(__file__).resolve().parent.parent
ALERTS_FILE = _REPO / "grafana" / "provisioning" / "alerting" / "alerts.yaml"
OVERVIEW_FILES = ["metric/overview.json", "imperial/overview.json"]

LAST_SYNC_ID = 1800
SKIPPED_ID = 1801
RIDE_TYPES_ID = 402

SUCCESS_KEY = ingestor_main.LAST_SUCCESSFUL_POLL_KEY
SKIPPED_KEY = ingestor_main.SKIPPED_STUBS_KEY

_INTERVALS_ENV = {"INTERVALS_ICU_ATHLETE_ID": "i1", "INTERVALS_ICU_API_KEY": "k"}
_STRAVA_ENV = {"STRAVA_CLIENT_ID": "1", "STRAVA_CLIENT_SECRET": "s",
               "STRAVA_REFRESH_TOKEN": "r"}
_RWGPS_ENV = {"RWGPS_API_KEY": "k", "RWGPS_AUTH_TOKEN": "t"}

ANALYZED = "2026-11-01T10:00:00.000+00:00"
STUB = {"id": "18069132600", "source": "STRAVA",
        "_note": "STRAVA activities are not available via the API"}
STREAMS = [{"type": "time", "data": [0, 1]}, {"type": "watts", "data": [100, 110]}]


def _ride(ride_id="i1"):
    return {"id": ride_id, "name": "Ride", "start_date": "2026-11-01T09:00:00Z",
            "distance": 20000, "moving_time": 3600, "analyzed": ANALYZED}


@pytest.fixture(autouse=True)
def _fresh_health():
    ingestor_main._delivered.clear()
    ingestor_main._recalc_pending = False
    yield
    ingestor_main._delivered.clear()
    ingestor_main._recalc_pending = False


@pytest.fixture()
def store():
    """sync_state as a dict: tests read what was written, not how."""
    state = {}
    with patch.object(ingestor_main, "set_sync_state",
                      side_effect=lambda _conn, key, value: state.__setitem__(key, value)):
        yield state


def _db(stored=None, held=0):
    """The row layer. `stored` maps ride ids already held to their analyzed value."""
    stored = stored or {}
    db = MagicMock()
    db.ACTIVITY_ABSENT = object()
    db.upsert_activity.return_value = (1, False)
    db.get_intervals_icu_analyzed.side_effect = (
        lambda _conn, ride_id: stored.get(ride_id, db.ACTIVITY_ABSENT))
    db.count_intervals_icu_rides_since.return_value = held
    return db


def _poll(activities=None, *, list_error=None, stream_error=None, stored=None, held=0,
          env=None, recalc_error=None):
    """One real poll_intervals_icu over a stubbed network."""
    listing = MagicMock(return_value=activities or [], side_effect=list_error)
    streams = MagicMock(return_value=STREAMS, side_effect=stream_error)
    with patch.dict(os.environ, env or _INTERVALS_ENV, clear=True), \
            patch.dict(sys.modules, {"db": _db(stored, held)}), \
            patch.object(ingestor_main, "_get_healthy_conn", return_value=MagicMock()), \
            patch.object(intervals_icu, "list_activities", listing), \
            patch.object(intervals_icu, "get_streams", streams), \
            patch.object(intervals_icu, "time"), \
            patch.object(ingestor_main, "recalculate_fitness", side_effect=recalc_error):
        ingestor_main.poll_intervals_icu()


class TestWhatCountsAsSuccess:
    """last_successful_poll present = the two states every test here tells apart."""

    def test_a_ride_delivered_advances_the_key(self, store):
        before = datetime.now(timezone.utc)
        _poll([_ride()])
        written = datetime.fromisoformat(store[SUCCESS_KEY])
        assert before <= written <= datetime.now(timezone.utc)

    def test_no_rides_at_all_is_a_success(self, store):
        """The rider rested and we hold nothing recent. Nothing is wrong."""
        _poll([], held=0)
        assert SUCCESS_KEY in store

    def test_unchanged_rides_are_a_success(self, store):
        """The normal poll: the window holds rides we already have."""
        _poll([_ride()], stored={"i1": ANALYZED})
        assert SUCCESS_KEY in store

    def test_an_exception_from_the_source_does_not_count(self, store):
        """The 403 a paid tier actually returned: raised, logged, container up."""
        _poll(list_error=RuntimeError("[intervals_icu] auth failed"))
        assert store == {}

    def test_every_ride_failing_does_not_count(self, store):
        """sync_activities swallows per-ride errors, so no exception reaches the
        poll and it looks exactly like a window with nothing new."""
        _poll([_ride("i1"), _ride("i2")], stream_error=RuntimeError("402 Payment Required"))
        assert SUCCESS_KEY not in store

    def test_a_new_ride_failing_behind_unchanged_ones_does_not_count(self, store):
        """The list endpoint works and old rides never touch the stream endpoint,
        so a dead stream endpoint fails only the rides that matter."""
        _poll([_ride("i1"), _ride("i2")], stored={"i1": ANALYZED},
              stream_error=RuntimeError("402 Payment Required"))
        assert SUCCESS_KEY not in store

    def test_a_window_of_nothing_but_stubs_does_not_count(self, store):
        """Rides exist and the API answered 200, but none of them can be read."""
        _poll([STUB, dict(STUB, id="18069132601")])
        assert SUCCESS_KEY not in store

    def test_a_stub_beside_a_readable_ride_counts(self, store):
        _poll([STUB, _ride()], stored={"i1": ANALYZED})
        assert SUCCESS_KEY in store

    def test_an_empty_window_over_rides_we_hold_does_not_count(self, store):
        """A 200 with an empty list, while we have rides dated inside that window,
        is the source going quiet rather than the rider."""
        _poll([], held=2)
        assert SUCCESS_KEY not in store

    def test_a_failed_recalculation_does_not_count(self, store):
        _poll([_ride()], recalc_error=RuntimeError("recalc blew up"))
        assert SUCCESS_KEY not in store

    def test_a_recalculation_that_keeps_failing_keeps_the_poll_failing(self, store):
        """The ride is committed before recalculation runs, so the next poll sees
        it as unchanged. Withholding the key once wouldn't be enough: rides would
        keep landing, metrics would stop updating, and Last Sync would stay green."""
        boom = RuntimeError("recalc blew up")
        _poll([_ride()], recalc_error=boom)
        _poll([_ride()], stored={"i1": ANALYZED}, recalc_error=boom)
        assert SUCCESS_KEY not in store

    def test_a_failed_recalculation_is_retried_without_a_new_ride(self, store):
        retried = []
        _poll([_ride()], recalc_error=RuntimeError("recalc blew up"))
        _poll([_ride()], stored={"i1": ANALYZED}, recalc_error=retried.append)
        assert retried
        assert SUCCESS_KEY in store

    def test_a_clean_recalculation_is_not_repeated(self, store):
        repeated = []
        _poll([_ride()])
        _poll([_ride()], stored={"i1": ANALYZED}, recalc_error=repeated.append)
        assert not repeated

    def test_no_database_connection_writes_nothing(self, store):
        with patch.dict(os.environ, _INTERVALS_ENV, clear=True), \
                patch.object(ingestor_main, "_get_healthy_conn", return_value=None):
            ingestor_main.poll_intervals_icu()
        assert store == {}


class TestSkippedCount:
    def test_counts_what_the_sync_skipped(self, store):
        _poll([STUB, dict(STUB, id="18069132601"), _ride()], stored={"i1": ANALYZED})
        assert store[SKIPPED_KEY] == "2"

    def test_written_even_when_the_stubs_are_all_there_is(self, store):
        """This is the state the panel exists for, and the poll does not count as
        a success, so the count must not depend on that."""
        _poll([STUB])
        assert store[SKIPPED_KEY] == "1"

    def test_falls_to_zero_when_the_stubs_leave_the_window(self, store):
        """A gauge of the window, not a total: skipping the write at zero would
        leave the old count on the dashboard for good."""
        _poll([STUB, _ride()], stored={"i1": ANALYZED})
        _poll([_ride()], stored={"i1": ANALYZED})
        assert store[SKIPPED_KEY] == "0"

    def test_a_failed_poll_leaves_the_last_known_count(self, store):
        _poll([STUB, _ride()], stored={"i1": ANALYZED})
        _poll(list_error=RuntimeError("503"))
        assert store[SKIPPED_KEY] == "1"


class TestHealthWriteFailure:
    def test_a_failed_write_does_not_break_the_poll(self):
        conn = MagicMock()
        recalc = MagicMock()
        with patch.dict(os.environ, _INTERVALS_ENV, clear=True), \
                patch.dict(sys.modules, {"db": _db()}), \
                patch.object(ingestor_main, "_get_healthy_conn", return_value=conn), \
                patch.object(ingestor_main, "set_sync_state",
                             side_effect=RuntimeError("sync_state is read-only")) as write, \
                patch.object(intervals_icu, "list_activities", return_value=[_ride()]), \
                patch.object(intervals_icu, "get_streams", return_value=STREAMS), \
                patch.object(intervals_icu, "time"), \
                patch.object(ingestor_main, "recalculate_fitness", recalc), \
                patch.object(ingestor_main.traceback, "print_exc") as logged_as_a_poll_error:
            ingestor_main.poll_intervals_icu()
        assert write.called, "the test only proves something if the write was attempted"
        assert recalc.called
        assert not logged_as_a_poll_error.called
        conn.close.assert_called_once()

    def test_the_next_poll_retries_the_write(self, store):
        with patch.object(ingestor_main, "set_sync_state", side_effect=RuntimeError("blip")):
            _poll([_ride()])
        assert store == {}
        _poll([_ride()], stored={"i1": ANALYZED})
        assert SUCCESS_KEY in store


class TestEverySourceHasToDeliver:
    """The key is a single row, so one healthy source must not hide a dead one."""

    ENV = {**_INTERVALS_ENV, **_RWGPS_ENV}

    def _poll_rwgps(self, items=(), sync_error=None, trip_error=None, cursor="2026-10-01",
                    env=None):
        with patch.dict(os.environ, env or self.ENV, clear=True), \
                patch.dict(sys.modules, {"db": MagicMock()}), \
                patch.object(ingestor_main, "_get_healthy_conn", return_value=MagicMock()), \
                patch.object(ingestor_main, "get_sync_state", return_value=cursor), \
                patch.object(rwgps, "fetch_sync_items", return_value=(list(items), "c"),
                             side_effect=sync_error), \
                patch.object(rwgps, "fetch_trip", side_effect=trip_error), \
                patch.object(rwgps.time, "sleep"), \
                patch.object(ingestor_main, "recalculate_fitness"):
            ingestor_main.poll_rwgps()

    def test_one_of_two_sources_is_not_enough(self, store):
        _poll([_ride()], stored={"i1": ANALYZED}, env=self.ENV)
        assert SUCCESS_KEY not in store

    def test_a_raising_source_blocks_the_key_whoever_else_delivers(self, store):
        for _ in range(3):
            self._poll_rwgps(sync_error=RuntimeError("403 paid tier"))
            _poll([_ride()], stored={"i1": ANALYZED}, env=self.ENV)
        assert SUCCESS_KEY not in store

    def test_both_delivering_advances_it(self, store):
        self._poll_rwgps()
        _poll([_ride()], stored={"i1": ANALYZED}, env=self.ENV)
        assert SUCCESS_KEY in store

    def test_a_source_dying_after_working_stops_the_key(self, store):
        """How the real outage went: everything fine, then one source gone for
        good while the other carried on."""
        self._poll_rwgps()
        _poll([_ride()], stored={"i1": ANALYZED}, env=self.ENV)
        last_good = store.pop(SUCCESS_KEY)
        assert last_good
        for _ in range(3):
            self._poll_rwgps(sync_error=RuntimeError("403 paid tier"))
            _poll([_ride()], stored={"i1": ANALYZED}, env=self.ENV)
        assert SUCCESS_KEY not in store

    def test_a_disabled_source_does_not_hold_it_back(self, store):
        """Strava has stale rows on the live DB and no credentials."""
        _poll([_ride()], stored={"i1": ANALYZED}, env=_INTERVALS_ENV)
        assert SUCCESS_KEY in store

    @pytest.mark.parametrize("cursor", ["2026-10-01", None],
                             ids=["incremental", "bounded_backfill"])
    def test_rwgps_stopping_mid_pass_does_not_count(self, store, cursor):
        """sync_activities logs a transient failure and returns normally."""
        trip = {"item_type": "trip", "item_id": 7, "action": "created",
                "datetime": "2026-11-01T09:00:00Z"}
        self._poll_rwgps([trip], trip_error=ConnectionError("reset"), cursor=cursor,
                         env=_RWGPS_ENV)
        assert SUCCESS_KEY not in store

    def test_rwgps_completing_a_pass_counts(self, store):
        self._poll_rwgps(env=_RWGPS_ENV)
        assert SUCCESS_KEY in store

    def test_strava_stopping_mid_pass_does_not_count(self, store):
        """The list works, the detail endpoint is gone: the pass stops, the cursor
        holds, and the poll prints 0 new activities."""
        ride = {"id": 1, "name": "Ride", "type": "Ride", "start_date": "2026-11-01T09:00:00Z"}
        with patch.dict(os.environ, _STRAVA_ENV, clear=True), \
                patch.dict(sys.modules, {"db": MagicMock()}), \
                patch.object(ingestor_main, "_get_healthy_conn", return_value=MagicMock()), \
                patch.object(strava, "_get_token", return_value="t"), \
                patch.object(strava, "fetch_recent_activities", return_value=[ride]), \
                patch.object(strava, "fetch_activity_detail",
                             side_effect=RuntimeError("402 Payment Required")), \
                patch.object(strava.time, "sleep"), \
                patch.object(ingestor_main, "recalculate_fitness"):
            ingestor_main.poll_strava()
        assert SUCCESS_KEY not in store

    def test_strava_completing_a_pass_counts(self, store):
        with patch.dict(os.environ, _STRAVA_ENV, clear=True), \
                patch.dict(sys.modules, {"db": MagicMock()}), \
                patch.object(ingestor_main, "_get_healthy_conn", return_value=MagicMock()), \
                patch.object(strava, "_get_token", return_value="t"), \
                patch.object(strava, "fetch_recent_activities", return_value=[]), \
                patch.object(ingestor_main, "recalculate_fitness"):
            ingestor_main.poll_strava()
        assert SUCCESS_KEY in store


def _rule(uid):
    config = yaml.safe_load(ALERTS_FILE.read_text(encoding="utf-8"))
    rules = [r for group in config["groups"] for r in group["rules"]]
    return next(r for r in rules if r["uid"] == uid)


def _alert_threshold_hours():
    data = {d["refId"]: d for d in _rule("velomate-ingest-stalled")["data"]}
    return data["C"]["model"]["conditions"][0]["evaluator"]["params"][0]


def _shape(node):
    """Structure with the values taken out."""
    if isinstance(node, dict):
        return {key: _shape(value) for key, value in node.items()}
    if isinstance(node, list):
        return [_shape(item) for item in node]
    return type(node).__name__


class TestStalledAlert:
    def test_same_shape_as_the_rule_known_to_load(self):
        """Nobody can load this into a running Grafana before it ships, so it may
        not carry anything the live Detraining rule does not."""
        assert _shape(_rule("velomate-ingest-stalled")) == _shape(_rule("velomate-detraining"))

    def test_detraining_is_still_there(self):
        assert _rule("velomate-detraining")["title"] == "Detraining Warning"

    def test_reads_the_key_the_ingestor_writes(self):
        sql = _rule("velomate-ingest-stalled")["data"][0]["model"]["rawSql"]
        assert f"key = '{SUCCESS_KEY}'" in sql

    def test_query_leaves_no_token_for_provisioning_to_expand(self):
        """Grafana expands $VARS in provisioning files before parsing them."""
        assert "$" not in _rule("velomate-ingest-stalled")["data"][0]["model"]["rawSql"]

    def test_threshold_survives_the_real_poll_cadence(self):
        """Derived from the code, so moving the default interval or the retry
        budget up past the threshold fails here instead of paging somebody.
        A poll can hold the loop for three capped Retry-After waits on one
        request; three such polls in a row is an API blip, not a stall."""
        with patch.dict(os.environ, {}, clear=True):
            poll_minutes = ingestor_main._poll_interval_minutes()
        retry_budget_minutes = 3 * 900 / 60
        assert _alert_threshold_hours() * 60 > 3 * (poll_minutes + retry_budget_minutes)

    def test_threshold_leaves_time_before_the_sync_window_runs_out(self):
        """A stall inside the 14-day window heals itself on recovery. Past it,
        rides need a manual wide-window run."""
        default_window_days = 14
        assert _alert_threshold_hours() < default_window_days * 24 / 2


def _panel(filename, panel_id):
    path = _REPO / "grafana" / "dashboards" / filename
    panels = json.loads(path.read_text(encoding="utf-8"))["panels"]
    return next(p for p in panels if p["id"] == panel_id)


def _boxes_overlap(a, b):
    return (a["x"] < b["x"] + b["w"] and b["x"] < a["x"] + a["w"]
            and a["y"] < b["y"] + b["h"] and b["y"] < a["y"] + a["h"])


@pytest.mark.parametrize("filename", OVERVIEW_FILES)
class TestOverviewPanels:
    def test_panels_read_the_keys_the_ingestor_writes(self, filename):
        assert f"key = '{SUCCESS_KEY}'" in _panel(filename, LAST_SYNC_ID)["targets"][0]["rawSql"]
        assert f"key = '{SKIPPED_KEY}'" in _panel(filename, SKIPPED_ID)["targets"][0]["rawSql"]

    def test_red_is_where_the_alert_fires(self, filename):
        steps = _panel(filename, LAST_SYNC_ID)["fieldConfig"]["defaults"]["thresholds"]["steps"]
        assert steps[-1]["color"] == "#e74c3c"
        assert steps[-1]["value"] == _alert_threshold_hours() * 3600

    @pytest.mark.parametrize("panel_id, lines", [
        (LAST_SYNC_ID, ["🟢 < 1 h — Healthy", "🟡 1 – 6 h — Delayed", "🔴 > 6 h — Stalled"]),
        (SKIPPED_ID, ["🟢 0 — Every ride readable", "🟡 1+ — Rides missing"]),
    ])
    def test_tooltip_emoji_are_the_threshold_colours(self, filename, panel_id, lines):
        emoji = {"#2ecc71": "🟢", "#f1c40f": "🟡", "#e74c3c": "🔴"}
        panel = _panel(filename, panel_id)
        steps = panel["fieldConfig"]["defaults"]["thresholds"]["steps"]
        paragraphs = panel["description"].split("\n\n")
        assert [e for e in paragraphs if e[0] in emoji.values()] == lines
        assert [line[0] for line in lines] == [emoji[s["color"]] for s in steps]

    def test_no_overlap_with_any_other_panel(self, filename):
        path = _REPO / "grafana" / "dashboards" / filename
        panels = json.loads(path.read_text(encoding="utf-8"))["panels"]
        for mine in (LAST_SYNC_ID, SKIPPED_ID, RIDE_TYPES_ID):
            box = _panel(filename, mine)["gridPos"]
            hits = [p["id"] for p in panels
                    if p["id"] != mine and _boxes_overlap(box, p["gridPos"])]
            assert hits == [], f"panel {mine} overlaps {hits}"

    def test_the_strip_still_fills_the_row(self, filename):
        """Ride Types on the left, the two stats stacked to its right, level with it."""
        donut, sync, skipped = (_panel(filename, i)["gridPos"] for i in
                                (RIDE_TYPES_ID, LAST_SYNC_ID, SKIPPED_ID))
        assert donut["x"] == 0
        assert sync["x"] == skipped["x"] == donut["w"]
        assert sync["w"] == skipped["w"] == 24 - donut["w"]
        assert sync["y"] == donut["y"]
        assert skipped["y"] == sync["y"] + sync["h"]
        assert sync["h"] + skipped["h"] == donut["h"]

    def test_shows_a_value_not_a_colourless_blank_when_the_key_is_absent(self, filename):
        for panel_id in (LAST_SYNC_ID, SKIPPED_ID):
            panel = _panel(filename, panel_id)
            assert panel["options"]["colorMode"] == "value"
            assert panel["fieldConfig"]["defaults"]["noValue"]
