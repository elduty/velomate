"""Tests for ingestor/intervals_icu.py — intervals.icu read-only client."""

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.modules.setdefault("psycopg2", MagicMock())
sys.modules.setdefault("psycopg2.extras", MagicMock())

_ingestor_dir = Path(__file__).resolve().parent.parent / "ingestor"
if str(_ingestor_dir) not in sys.path:
    sys.path.insert(0, str(_ingestor_dir))

import intervals_icu


def _resp(status=200, body=None, headers=None):
    resp = MagicMock()
    resp.status_code = status
    resp.json.return_value = body if body is not None else {}
    resp.headers = headers or {}
    return resp


_ENV = {"INTERVALS_ICU_ATHLETE_ID": "i123456", "INTERVALS_ICU_API_KEY": "key789"}


class TestAuth:
    def test_username_is_the_literal_api_key(self):
        with patch.dict("os.environ", _ENV):
            assert intervals_icu._auth() == ("API_KEY", "key789")

    def test_athlete_id_from_env(self):
        with patch.dict("os.environ", _ENV):
            assert intervals_icu._athlete_id() == "i123456"


class TestRetryAfter:
    def test_honours_retry_after_header(self):
        assert intervals_icu._retry_after_seconds(_resp(429, headers={"Retry-After": "42"}), 0) == 42

    def test_caps_absurd_retry_after(self):
        assert intervals_icu._retry_after_seconds(_resp(429, headers={"Retry-After": "99999"}), 0) == 900

    def test_falls_back_to_exponential_backoff(self):
        assert intervals_icu._retry_after_seconds(_resp(429), 0) == 60
        assert intervals_icu._retry_after_seconds(_resp(429), 2) == 240

    def test_ignores_malformed_retry_after(self):
        resp = _resp(429, headers={"Retry-After": "soon"})
        assert intervals_icu._retry_after_seconds(resp, 1) == 120


class TestRequestWithRetry:
    def test_retries_on_429_then_returns_success(self):
        method = MagicMock(side_effect=[_resp(429, headers={"Retry-After": "1"}), _resp(200, {"ok": True})])
        with patch.dict("os.environ", _ENV), patch("time.sleep"):
            resp = intervals_icu._request_with_retry(method, "https://example.test/x")
        assert resp.status_code == 200
        assert method.call_count == 2

    def test_gives_up_after_max_retries(self):
        method = MagicMock(return_value=_resp(429, headers={"Retry-After": "1"}))
        with patch.dict("os.environ", _ENV), patch("time.sleep"):
            resp = intervals_icu._request_with_retry(method, "https://example.test/x", max_retries=2)
        assert resp.status_code == 429
        assert method.call_count == 3

    def test_passes_basic_auth_and_default_timeout(self):
        method = MagicMock(return_value=_resp(200))
        with patch.dict("os.environ", _ENV), patch("time.sleep"):
            intervals_icu._request_with_retry(method, "https://example.test/x")
        assert method.call_args.kwargs["auth"] == ("API_KEY", "key789")
        assert method.call_args.kwargs["timeout"] == 30


class TestBudget:
    def test_warns_when_window_budget_low(self, capsys):
        intervals_icu._check_budget(_resp(200, headers={"X-RateLimit-Remaining": "12,4000"}))
        assert "rate budget low" in capsys.readouterr().out

    def test_silent_when_budget_healthy(self, capsys):
        intervals_icu._check_budget(_resp(200, headers={"X-RateLimit-Remaining": "2400,4900"}))
        assert capsys.readouterr().out == ""

    def test_malformed_header_is_ignored(self, capsys):
        intervals_icu._check_budget(_resp(200, headers={"X-RateLimit-Remaining": "nonsense"}))
        assert capsys.readouterr().out == ""

    def test_missing_header_is_ignored(self, capsys):
        intervals_icu._check_budget(_resp(200))
        assert capsys.readouterr().out == ""


class TestPacing:
    def test_sleeps_to_stay_under_ten_per_second(self):
        """Verify _pace() sleeps correctly based on elapsed time since last request."""
        intervals_icu._last_request_at = 0.0
        # Each _pace() call makes 2 calls to monotonic: once for elapsed, once for update.
        # side_effect values: [0.0, 0.0] for 1st pace, [0.05, 0.05] for 2nd pace, [0.20, 0.20] for 3rd pace
        with patch("time.monotonic", side_effect=[0.0, 0.0, 0.05, 0.05, 0.20, 0.20]), \
             patch("time.sleep") as slept:

            # First pace(): no elapsed time (0.0 - 0.0 = 0), should sleep full interval
            intervals_icu._pace()
            # Second pace(): 0.05s elapsed (0.05 - 0.0 = 0.05), should sleep 0.07s
            intervals_icu._pace()
            # Third pace(): 0.15s+ elapsed (0.20 - 0.05 = 0.15 >= 0.12), should not sleep
            intervals_icu._pace()

        # Verify sleep was called exactly twice (first and second pace calls only)
        assert slept.call_count == 2

        # First sleep should be full interval (no elapsed time)
        assert slept.call_args_list[0][0][0] == pytest.approx(intervals_icu._MIN_REQUEST_INTERVAL_S, abs=1e-6)

        # Second sleep should be reduced interval (partial elapsed time)
        assert slept.call_args_list[1][0][0] == pytest.approx(0.07, abs=1e-6)


class TestGet:
    def test_auth_failure_raises_actionable_error(self):
        with patch.dict("os.environ", _ENV), \
                patch.object(intervals_icu, "_request_with_retry", return_value=_resp(401)):
            with pytest.raises(RuntimeError, match="Developer Settings"):
                intervals_icu._get("/athlete/i123456/activities")

    def test_403_raises_the_same_error(self):
        with patch.dict("os.environ", _ENV), \
                patch.object(intervals_icu, "_request_with_retry", return_value=_resp(403)):
            with pytest.raises(RuntimeError, match="auth failed"):
                intervals_icu._get("/activity/i1")

    def test_missing_ok_returns_none_on_404(self):
        with patch.dict("os.environ", _ENV), \
                patch.object(intervals_icu, "_request_with_retry", return_value=_resp(404)):
            assert intervals_icu._get("/activity/i1", missing_ok=True) is None

    def test_404_raises_when_not_missing_ok(self):
        resp = _resp(404)
        resp.raise_for_status.side_effect = RuntimeError("404")
        with patch.dict("os.environ", _ENV), \
                patch.object(intervals_icu, "_request_with_retry", return_value=resp):
            with pytest.raises(RuntimeError):
                intervals_icu._get("/activity/i1")


class TestListActivities:
    def test_builds_url_and_required_params(self):
        body = [{"id": "i1"}, {"id": "i2"}]
        with patch.dict("os.environ", _ENV), \
                patch.object(intervals_icu, "_request_with_retry", return_value=_resp(200, body)) as req:
            out = intervals_icu.list_activities("2026-07-01T00:00:00")
        assert [a["id"] for a in out] == ["i1", "i2"]
        assert req.call_args[0][1] == "https://intervals.icu/api/v1/athlete/i123456/activities"
        assert req.call_args.kwargs["params"] == {"oldest": "2026-07-01T00:00:00"}

    def test_optional_params_are_serialised(self):
        with patch.dict("os.environ", _ENV), \
                patch.object(intervals_icu, "_request_with_retry", return_value=_resp(200, [])) as req:
            intervals_icu.list_activities(
                "2026-07-01T00:00:00", newest="2026-08-01T00:00:00",
                fields=["id", "start_date_local"], limit=50,
            )
        assert req.call_args.kwargs["params"] == {
            "oldest": "2026-07-01T00:00:00",
            "newest": "2026-08-01T00:00:00",
            "fields": "id,start_date_local",
            "limit": 50,
        }


class TestGetStreams:
    def test_requires_json_extension_and_include_defaults(self):
        body = [{"type": "watts", "data": [100, 110]}]
        with patch.dict("os.environ", _ENV), \
                patch.object(intervals_icu, "_request_with_retry", return_value=_resp(200, body)) as req:
            out = intervals_icu.get_streams("i74258403")
        assert out == body
        assert req.call_args[0][1].endswith("/activity/i74258403/streams.json")
        assert req.call_args.kwargs["params"] == {"includeDefaults": "true"}

    def test_types_are_comma_joined(self):
        with patch.dict("os.environ", _ENV), \
                patch.object(intervals_icu, "_request_with_retry", return_value=_resp(200, [])) as req:
            intervals_icu.get_streams("i1", types=["time", "watts"], include_defaults=False)
        assert req.call_args.kwargs["params"] == {"includeDefaults": "false", "types": "time,watts"}

    def test_missing_activity_returns_empty_list(self):
        with patch.dict("os.environ", _ENV), \
                patch.object(intervals_icu, "_request_with_retry", return_value=_resp(404)):
            assert intervals_icu.get_streams("i-gone") == []


class TestGetActivityAndWellness:
    def test_get_activity_passes_intervals_flag(self):
        with patch.dict("os.environ", _ENV), \
                patch.object(intervals_icu, "_request_with_retry", return_value=_resp(200, {"id": "i1"})) as req:
            assert intervals_icu.get_activity("i1", intervals=True)["id"] == "i1"
        assert req.call_args.kwargs["params"] == {"intervals": "true"}

    def test_get_activity_missing_returns_empty_dict(self):
        with patch.dict("os.environ", _ENV), \
                patch.object(intervals_icu, "_request_with_retry", return_value=_resp(404)):
            assert intervals_icu.get_activity("i-gone") == {}

    def test_get_wellness_hits_athlete_scoped_path(self):
        with patch.dict("os.environ", _ENV), \
                patch.object(intervals_icu, "_request_with_retry", return_value=_resp(200, [{"id": "2026-08-01"}])) as req:
            out = intervals_icu.get_wellness("2026-07-01")
        assert out[0]["id"] == "2026-08-01"
        assert req.call_args[0][1].endswith("/athlete/i123456/wellness.json")
        assert req.call_args.kwargs["params"] == {"oldest": "2026-07-01"}


def _full_activity(**over):
    a = {
        "id": "i179264457", "name": "Afternoon Ride", "type": "Ride",
        "source": "OAUTH_CLIENT", "start_date": "2026-08-24T13:09:25Z",
        "start_date_local": "2026-08-24T14:09:25", "distance": 20611.3,
        "moving_time": 4223, "elapsed_time": 4581, "total_elevation_gain": 371.0,
        "average_heartrate": 146, "max_heartrate": 175, "icu_average_watts": 120,
        "average_cadence": 60.7, "calories": 561, "device_name": "HAMMERHEAD Karoo",
        "analyzed": "2026-08-24T14:26:27.824+00:00",
    }
    a.update(over)
    return a


class TestStubDetection:
    def test_note_marks_a_stub(self):
        assert intervals_icu._is_unavailable_stub(
            {"id": "18069132600", "_note": "STRAVA activities are not available via the API"}) is True

    def test_strava_source_marks_a_stub(self):
        assert intervals_icu._is_unavailable_stub({"id": "1", "source": "STRAVA"}) is True

    def test_full_activity_is_not_a_stub(self):
        assert intervals_icu._is_unavailable_stub(_full_activity()) is False


class TestParseActivity:
    def test_maps_core_fields(self):
        d = intervals_icu._parse_activity(_full_activity())
        assert d["intervals_icu_id"] == "i179264457"
        assert d["date"] == "2026-08-24T13:09:25Z"
        assert d["distance_m"] == 20611.3
        assert d["duration_s"] == 4223
        assert d["elevation_m"] == 371.0
        assert d["avg_hr"] == 146
        assert d["avg_power"] == 120
        assert d["strava_type"] == "Ride"

    def test_avg_speed_computed_from_distance_and_moving_time(self):
        d = intervals_icu._parse_activity(_full_activity())
        assert d["avg_speed_kmh"] == pytest.approx(17.57, abs=0.01)

    def test_zero_duration_does_not_divide_by_zero(self):
        d = intervals_icu._parse_activity(_full_activity(moving_time=0))
        assert d["avg_speed_kmh"] == 0.0

    def test_suffer_score_is_none(self):
        assert intervals_icu._parse_activity(_full_activity())["suffer_score"] is None

    def test_carries_the_analyzed_timestamp(self):
        d = intervals_icu._parse_activity(_full_activity())
        assert d["intervals_icu_analyzed"] == "2026-08-24T14:26:27.824+00:00"

    def test_does_not_import_provider_computed_metrics(self):
        """The ingestor is the source of truth for anything it can compute."""
        d = intervals_icu._parse_activity(
            _full_activity(polarization_index=1.43, coasting_time=895,
                           icu_training_load=61, icu_intensity=71.6))
        for banned in ("polarization_index", "coasting_time_s", "coasting_time",
                       "tss", "intensity_factor"):
            assert banned not in d, f"{banned} must not be imported from the provider"

    def test_karoo_device_detected(self):
        assert intervals_icu._parse_activity(_full_activity())["device"] == "karoo"


class TestParseStreams:
    def test_time_array_drives_offsets_not_list_index(self):
        streams = [
            {"type": "time", "data": [0, 1, 19, 20]},
            {"type": "watts", "data": [0, 0, 94, 86]},
            {"type": "heartrate", "data": [99, 99, 95, 96]},
        ]
        pts = intervals_icu._parse_streams(streams)
        assert [p["time_offset"] for p in pts] == [0, 1, 19, 20]
        assert [p["power"] for p in pts] == [0, 0, 94, 86]

    def test_latlng_comes_from_two_parallel_arrays_not_pairs(self):
        """The real API shape, verified against a live ride: `data` holds
        latitudes and `data2` longitudes. Treating it as Strava-style
        [lat, lng] pairs raises "'float' object is not subscriptable" on every
        real ride — which unit tests using the pair shape happily missed."""
        streams = [
            {"type": "time", "data": [0, 1]},
            {"type": "latlng", "data": [38.7, 38.8], "data2": [-9.1, -9.2]},
        ]
        pts = intervals_icu._parse_streams(streams)
        assert (pts[0]["lat"], pts[0]["lng"]) == (38.7, -9.1)
        assert (pts[1]["lat"], pts[1]["lng"]) == (38.8, -9.2)

    def test_a_sample_without_a_gps_fix_is_left_null(self):
        """The first sample of a real ride commonly has no fix."""
        streams = [
            {"type": "time", "data": [0, 1]},
            {"type": "latlng", "data": [None, 38.8], "data2": [None, -9.2]},
        ]
        pts = intervals_icu._parse_streams(streams)
        assert pts[0]["lat"] is None and pts[0]["lng"] is None
        assert pts[1]["lat"] == 38.8

    def test_missing_data2_does_not_crash(self):
        streams = [
            {"type": "time", "data": [0]},
            {"type": "latlng", "data": [38.7]},
        ]
        assert intervals_icu._parse_streams(streams)[0]["lat"] is None

    def test_velocity_smooth_converted_to_kmh(self):
        streams = [{"type": "time", "data": [0]}, {"type": "velocity_smooth", "data": [5.0]}]
        assert intervals_icu._parse_streams(streams)[0]["speed_kmh"] == pytest.approx(18.0)

    def test_missing_time_stream_returns_empty(self):
        assert intervals_icu._parse_streams([{"type": "watts", "data": [1, 2]}]) == []

    def test_short_channel_pads_with_none(self):
        streams = [{"type": "time", "data": [0, 1, 2]}, {"type": "watts", "data": [10]}]
        pts = intervals_icu._parse_streams(streams)
        assert [p["power"] for p in pts] == [10, None, None]


class TestSyncActivities:
    def _db(self):
        m = MagicMock()
        m.upsert_activity.return_value = (1, False)
        m.get_intervals_icu_analyzed.return_value = None
        return m

    def test_ingests_a_full_activity_with_streams(self):
        db_mock = self._db()
        with patch.dict("os.environ", _ENV), patch.dict(sys.modules, {"db": db_mock}), \
             patch.object(intervals_icu, "list_activities", return_value=[_full_activity()]), \
             patch.object(intervals_icu, "get_streams", return_value=[
                 {"type": "time", "data": [0, 1]}, {"type": "watts", "data": [100, 110]}]), \
             patch.object(intervals_icu, "time"):
            ingested, skipped = intervals_icu.sync_activities(MagicMock(), window_days=14)
        assert (ingested, skipped) == (1, 0)
        assert db_mock.upsert_activity.call_args[0][1]["intervals_icu_id"] == "i179264457"
        assert db_mock.upsert_streams.called

    def test_stubs_are_skipped_and_counted_not_ingested(self):
        db_mock = self._db()
        stub = {"id": "18069132600", "source": "STRAVA",
                "_note": "STRAVA activities are not available via the API"}
        with patch.dict("os.environ", _ENV), patch.dict(sys.modules, {"db": db_mock}), \
             patch.object(intervals_icu, "list_activities", return_value=[stub, _full_activity()]), \
             patch.object(intervals_icu, "get_streams", return_value=[]), \
             patch.object(intervals_icu, "time"):
            ingested, skipped = intervals_icu.sync_activities(MagicMock(), window_days=14)
        assert (ingested, skipped) == (1, 1)
        assert db_mock.upsert_activity.call_count == 1

    def test_max_power_derived_from_watts_stream(self):
        db_mock = self._db()
        with patch.dict("os.environ", _ENV), patch.dict(sys.modules, {"db": db_mock}), \
             patch.object(intervals_icu, "list_activities", return_value=[_full_activity()]), \
             patch.object(intervals_icu, "get_streams", return_value=[
                 {"type": "time", "data": [0, 1, 2]}, {"type": "watts", "data": [100, 480, 110]}]), \
             patch.object(intervals_icu, "time"):
            intervals_icu.sync_activities(MagicMock(), window_days=14)
        assert db_mock.upsert_activity.call_args[0][1]["max_power"] == 480

    def test_one_bad_activity_does_not_sink_the_batch(self):
        db_mock = self._db()
        db_mock.upsert_activity.side_effect = [TypeError("bad payload"), (2, False)]
        with patch.dict("os.environ", _ENV), patch.dict(sys.modules, {"db": db_mock}), \
             patch.object(intervals_icu, "list_activities",
                          return_value=[_full_activity(id="iBAD"), _full_activity(id="iOK")]), \
             patch.object(intervals_icu, "get_streams", return_value=[]), \
             patch.object(intervals_icu, "time"):
            ingested, _ = intervals_icu.sync_activities(MagicMock(), window_days=14)
        assert ingested == 1

    def test_millisecond_and_microsecond_spellings_compare_equal(self):
        assert (intervals_icu._parse_ts("2026-08-24T14:26:27.824+00:00")
                == intervals_icu._parse_ts("2026-08-24T14:26:27.824000+00:00"))

    def test_parse_ts_returns_none_for_missing_or_junk(self):
        assert intervals_icu._parse_ts(None) is None
        assert intervals_icu._parse_ts("") is None
        assert intervals_icu._parse_ts("not a timestamp") is None

    def test_streams_not_refetched_when_analyzed_is_unchanged(self):
        """An unchanged timestamp means the ride has not been re-analysed, so its
        streams are already current. Mocked with the psycopg2 round-trip spelling
        (microseconds), NOT the verbatim API string, which would hide the
        precision mismatch this test exists to catch."""
        db_mock = self._db()
        db_mock.get_intervals_icu_analyzed.return_value = "2026-08-24T14:26:27.824000+00:00"
        with patch.dict("os.environ", _ENV), patch.dict(sys.modules, {"db": db_mock}), \
             patch.object(intervals_icu, "list_activities", return_value=[_full_activity()]), \
             patch.object(intervals_icu, "get_streams") as gs, \
             patch.object(intervals_icu, "time"):
            intervals_icu.sync_activities(MagicMock(), window_days=14)
        assert not gs.called, "streams must not be re-fetched when analyzed is unchanged"
        assert not db_mock.upsert_streams.called

    def test_unchanged_activities_are_not_counted_as_ingested(self):
        """`ingested` drives recalculate_fitness. The window re-queries the same
        rides every poll, so an unchanged ride must not report as ingested."""
        db_mock = self._db()
        db_mock.get_intervals_icu_analyzed.return_value = "2026-08-24T14:26:27.824000+00:00"
        with patch.dict("os.environ", _ENV), patch.dict(sys.modules, {"db": db_mock}), \
             patch.object(intervals_icu, "list_activities", return_value=[_full_activity()]), \
             patch.object(intervals_icu, "get_streams"), \
             patch.object(intervals_icu, "time"):
            ingested, _ = intervals_icu.sync_activities(MagicMock(), window_days=14)
        assert ingested == 0
        assert db_mock.upsert_activity.called

    def test_streams_refetched_when_analyzed_advances(self):
        db_mock = self._db()
        db_mock.get_intervals_icu_analyzed.return_value = "2026-08-01T00:00:00+00:00"
        with patch.dict("os.environ", _ENV), patch.dict(sys.modules, {"db": db_mock}), \
             patch.object(intervals_icu, "list_activities", return_value=[_full_activity()]), \
             patch.object(intervals_icu, "get_streams", return_value=[
                 {"type": "time", "data": [0]}, {"type": "watts", "data": [100]}]) as gs, \
             patch.object(intervals_icu, "time"):
            intervals_icu.sync_activities(MagicMock(), window_days=14)
        assert gs.called
        assert db_mock.upsert_streams.called


class TestReconcile:
    def _conn(self, local_ids):
        conn = MagicMock()
        conn.cursor.return_value.__enter__.return_value.fetchall.return_value = [
            (i,) for i in local_ids]
        return conn

    def test_local_row_missing_remotely_is_deleted(self):
        """One ride gone from an otherwise intact remote set."""
        db_mock = MagicMock()
        db_mock.handle_intervals_icu_deletion.return_value = "deleted"
        conn = self._conn(["iGONE", "i1", "i2", "i3"])
        with patch.dict("os.environ", _ENV), patch.dict(sys.modules, {"db": db_mock}), \
             patch.object(intervals_icu, "list_activities", return_value=[
                 _full_activity(id="i1"), _full_activity(id="i2"), _full_activity(id="i3")]):
            deleted, recovered = intervals_icu.reconcile(conn, sweep_days=90)
        assert deleted == 1
        db_mock.handle_intervals_icu_deletion.assert_called_once_with(conn, "iGONE")

    def test_stub_remote_never_deletes_a_local_ride(self):
        """The remote set here is healthy, so the plausibility guard does NOT
        fire — this proves the stub is excluded from both sides of the diff
        rather than being saved incidentally by the guard."""
        db_mock = MagicMock()
        conn = self._conn(["18069132600", "i1", "i2", "i3"])
        stub = {"id": "18069132600", "source": "STRAVA", "_note": "STRAVA activities..."}
        with patch.dict("os.environ", _ENV), patch.dict(sys.modules, {"db": db_mock}), \
             patch.object(intervals_icu, "list_activities", return_value=[
                 stub, _full_activity(id="i1"), _full_activity(id="i2"),
                 _full_activity(id="i3")]):
            deleted, _ = intervals_icu.reconcile(conn, sweep_days=90)
        assert deleted == 0
        assert not db_mock.handle_intervals_icu_deletion.called

    def test_empty_remote_set_never_deletes(self):
        """A wrongly-empty 200 must not wipe the library — CASCADE would take
        streams, intervals and climbs with each row."""
        db_mock = MagicMock()
        conn = self._conn(["i1", "i2", "i3"])
        with patch.dict("os.environ", _ENV), patch.dict(sys.modules, {"db": db_mock}), \
             patch.object(intervals_icu, "list_activities", return_value=[]):
            deleted, _ = intervals_icu.reconcile(conn, sweep_days=90)
        assert deleted == 0
        assert not db_mock.handle_intervals_icu_deletion.called

    def test_implausibly_small_remote_set_never_deletes(self):
        db_mock = MagicMock()
        conn = self._conn(["i1", "i2", "i3", "i4", "i5", "i6"])
        with patch.dict("os.environ", _ENV), patch.dict(sys.modules, {"db": db_mock}), \
             patch.object(intervals_icu, "list_activities",
                          return_value=[_full_activity(id="i1")]):
            deleted, _ = intervals_icu.reconcile(conn, sweep_days=90)
        assert deleted == 0
        assert not db_mock.handle_intervals_icu_deletion.called

    def test_reconcile_requests_source_so_the_stub_filter_has_its_input(self):
        db_mock = MagicMock()
        conn = self._conn([])
        with patch.dict("os.environ", _ENV), patch.dict(sys.modules, {"db": db_mock}), \
             patch.object(intervals_icu, "list_activities", return_value=[]) as la:
            intervals_icu.reconcile(conn, sweep_days=90)
        assert "source" in la.call_args.kwargs["fields"]

    def test_remote_id_absent_locally_is_reported_as_recovered(self):
        db_mock = MagicMock()
        conn = self._conn([])
        with patch.dict("os.environ", _ENV), patch.dict(sys.modules, {"db": db_mock}), \
             patch.object(intervals_icu, "list_activities", return_value=[_full_activity()]):
            _, recovered = intervals_icu.reconcile(conn, sweep_days=90)
        assert recovered == 1


class TestUnanalysedActivityIsNotReIngested:
    """A ride intervals.icu never analyses (still processing, manual entry,
    de-analysed) must be stored once, not re-ingested every poll. Collapsing
    'row absent' and 'stored NULL' to None re-downloads its streams and forces
    a full fitness recalculation on every cycle."""

    def test_stored_null_analyzed_is_not_counted_as_changed(self):
        import db as _db
        db_mock = MagicMock()
        db_mock.upsert_activity.return_value = (1, False)
        db_mock.ACTIVITY_ABSENT = _db.ACTIVITY_ABSENT
        db_mock.get_intervals_icu_analyzed.return_value = None   # stored, but NULL
        with patch.dict("os.environ", _ENV), patch.dict(sys.modules, {"db": db_mock}), \
             patch.object(intervals_icu, "list_activities",
                          return_value=[_full_activity(analyzed=None)]), \
             patch.object(intervals_icu, "get_streams") as gs, \
             patch.object(intervals_icu, "time"):
            ingested, _ = intervals_icu.sync_activities(MagicMock(), window_days=14)
        assert ingested == 0, "an unanalysed ride must not re-ingest every poll"
        assert not gs.called, "and its streams must not be re-downloaded"

    def test_absent_activity_is_still_ingested(self):
        import db as _db
        db_mock = MagicMock()
        db_mock.upsert_activity.return_value = (1, False)
        db_mock.ACTIVITY_ABSENT = _db.ACTIVITY_ABSENT
        db_mock.get_intervals_icu_analyzed.return_value = _db.ACTIVITY_ABSENT
        with patch.dict("os.environ", _ENV), patch.dict(sys.modules, {"db": db_mock}), \
             patch.object(intervals_icu, "list_activities",
                          return_value=[_full_activity(analyzed=None)]), \
             patch.object(intervals_icu, "get_streams", return_value=[]), \
             patch.object(intervals_icu, "time"):
            ingested, _ = intervals_icu.sync_activities(MagicMock(), window_days=14)
        assert ingested == 1


class TestDeletionCap:
    """The 0.5 ratio guard lets a 50-99% degraded response through, and the
    missing remainder is then CASCADE-deleted permanently — rides outside the
    14-day sync window never come back. A per-sweep cap bounds that."""

    def _conn(self, local_ids):
        conn = MagicMock()
        conn.cursor.return_value.__enter__.return_value.fetchall.return_value = [
            (i,) for i in local_ids]
        return conn

    def test_a_partial_response_above_the_cap_deletes_nothing(self):
        db_mock = MagicMock()
        # 20 local, remote returns 12 (60% — passes the 0.5 ratio guard),
        # so 8 look deleted, above the cap of 5.
        local = [f"i{n}" for n in range(20)]
        remote = [_full_activity(id=f"i{n}") for n in range(12)]
        with patch.dict("os.environ", _ENV), patch.dict(sys.modules, {"db": db_mock}), \
             patch.object(intervals_icu, "list_activities", return_value=remote):
            deleted, _ = intervals_icu.reconcile(self._conn(local), sweep_days=90)
        assert deleted == 0
        assert not db_mock.handle_intervals_icu_deletion.called

    def test_a_handful_of_real_deletions_still_processes(self):
        db_mock = MagicMock()
        db_mock.handle_intervals_icu_deletion.return_value = "deleted"
        local = [f"i{n}" for n in range(20)]
        remote = [_full_activity(id=f"i{n}") for n in range(18)]   # 2 genuinely gone
        with patch.dict("os.environ", _ENV), patch.dict(sys.modules, {"db": db_mock}), \
             patch.object(intervals_icu, "list_activities", return_value=remote):
            deleted, _ = intervals_icu.reconcile(self._conn(local), sweep_days=90)
        assert deleted == 2


class TestBulkDeletionConfirmation:
    """An over-cap batch must not be stranded forever — ghost rides would keep
    feeding CTL/ATL/TSB. It is confirmed across two sweeps instead: a degraded
    response is unlikely to repeat the identical missing set, a real bulk
    deletion reproduces it exactly."""

    def _conn(self, local_ids):
        conn = MagicMock()
        conn.cursor.return_value.__enter__.return_value.fetchall.return_value = [
            (i,) for i in local_ids]
        return conn

    def _run(self, db_mock, local, remote):
        with patch.dict("os.environ", _ENV), patch.dict(sys.modules, {"db": db_mock}), \
             patch.object(intervals_icu, "list_activities", return_value=remote):
            return intervals_icu.reconcile(self._conn(local), sweep_days=90)

    def test_first_over_cap_sweep_defers_and_records_the_set(self):
        db_mock = MagicMock()
        db_mock.get_sync_state.return_value = None
        local = [f"i{n}" for n in range(20)]
        remote = [_full_activity(id=f"i{n}") for n in range(12)]
        deleted, _ = self._run(db_mock, local, remote)
        assert deleted == 0
        assert not db_mock.handle_intervals_icu_deletion.called
        key, value = db_mock.set_sync_state.call_args[0][1:3]
        assert key == intervals_icu.PENDING_DELETIONS_KEY
        assert value.startswith("i1")

    def test_identical_set_on_the_next_sweep_is_processed(self):
        db_mock = MagicMock()
        db_mock.handle_intervals_icu_deletion.return_value = "deleted"
        local = [f"i{n}" for n in range(20)]
        remote = [_full_activity(id=f"i{n}") for n in range(12)]
        missing = ",".join(sorted(f"i{n}" for n in range(12, 20)))
        db_mock.get_sync_state.return_value = missing
        deleted, _ = self._run(db_mock, local, remote)
        assert deleted == 8, "a confirmed bulk deletion must not be stranded"

    def test_a_different_set_restarts_confirmation(self):
        db_mock = MagicMock()
        db_mock.get_sync_state.return_value = "iOTHER,iSTALE"
        local = [f"i{n}" for n in range(20)]
        remote = [_full_activity(id=f"i{n}") for n in range(12)]
        deleted, _ = self._run(db_mock, local, remote)
        assert deleted == 0, "a set that does not match the pending one must defer"

    def test_a_normal_batch_clears_any_pending_confirmation(self):
        db_mock = MagicMock()
        db_mock.handle_intervals_icu_deletion.return_value = "deleted"
        local = [f"i{n}" for n in range(20)]
        remote = [_full_activity(id=f"i{n}") for n in range(18)]
        deleted, _ = self._run(db_mock, local, remote)
        assert deleted == 2
        assert any(c[0][1] == intervals_icu.PENDING_DELETIONS_KEY and c[0][2] == ""
                   for c in db_mock.set_sync_state.call_args_list)
