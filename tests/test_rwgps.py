"""Tests for ingestor/rwgps.py — Ride with GPS client."""

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.modules.setdefault("psycopg2", MagicMock())
sys.modules.setdefault("psycopg2.extras", MagicMock())

_ingestor_dir = Path(__file__).resolve().parent.parent / "ingestor"
if str(_ingestor_dir) not in sys.path:
    sys.path.insert(0, str(_ingestor_dir))

import rwgps


def _resp(status=200, body=None):
    resp = MagicMock()
    resp.status_code = status
    resp.json.return_value = body or {}
    return resp


_ENV = {"RWGPS_API_KEY": "key123", "RWGPS_AUTH_TOKEN": "tok456"}


class TestHeaders:
    def test_headers_from_env(self):
        with patch.dict("os.environ", _ENV):
            h = rwgps._headers()
        assert h == {"x-rwgps-api-key": "key123", "x-rwgps-auth-token": "tok456"}


class TestFetchSyncItems:
    def test_returns_trip_items_and_cursor(self):
        body = {
            "items": [
                {"item_type": "trip", "item_id": 1, "action": "created", "datetime": "2026-06-01T10:00:00Z"},
                {"item_type": "route", "item_id": 2, "action": "created", "datetime": "2026-06-01T10:00:00Z"},
                {"item_type": "trip", "item_id": 3, "action": "deleted", "datetime": "2026-06-02T10:00:00Z"},
            ],
            "meta": {"rwgps_datetime": "2026-06-03T00:00:00Z"},
        }
        with patch.dict("os.environ", _ENV), \
                patch.object(rwgps, "_request_with_retry", return_value=_resp(200, body)) as req:
            items, cursor = rwgps.fetch_sync_items("2026-05-01T00:00:00Z")
        assert [i["item_id"] for i in items] == [1, 3]  # routes filtered out
        assert cursor == "2026-06-03T00:00:00Z"
        params = req.call_args.kwargs["params"]
        assert params == {"since": "2026-05-01T00:00:00Z", "assets": "trips"}

    def test_401_raises_after_logging(self):
        resp = _resp(401)
        resp.raise_for_status.side_effect = Exception("401")
        with patch.dict("os.environ", _ENV), \
                patch.object(rwgps, "_request_with_retry", return_value=resp):
            with pytest.raises(Exception):
                rwgps.fetch_sync_items("2026-05-01T00:00:00Z")

    def test_missing_cursor_raises_value_error(self):
        with patch.dict("os.environ", _ENV), \
                patch.object(rwgps, "_request_with_retry", return_value=_resp(200, {"items": []})):
            with pytest.raises(ValueError, match="rwgps_datetime"):
                rwgps.fetch_sync_items("2026-05-01T00:00:00Z")


class TestFetchTrip:
    def test_returns_trip_dict(self):
        body = {"trip": {"id": 99, "name": "Morning Ride"}}
        with patch.dict("os.environ", _ENV), \
                patch.object(rwgps, "_request_with_retry", return_value=_resp(200, body)):
            assert rwgps.fetch_trip(99)["name"] == "Morning Ride"

    def test_404_returns_empty(self):
        with patch.dict("os.environ", _ENV), \
                patch.object(rwgps, "_request_with_retry", return_value=_resp(404)):
            assert rwgps.fetch_trip(99) == {}

    def test_403_returns_empty(self):
        with patch.dict("os.environ", _ENV), \
                patch.object(rwgps, "_request_with_retry", return_value=_resp(403)):
            assert rwgps.fetch_trip(99) == {}


class TestRequestWithRetry:
    def test_retries_on_429(self):
        rate_limited = _resp(429)
        ok = _resp(200)
        method = MagicMock(side_effect=[rate_limited, ok])
        with patch.object(rwgps.time, "sleep"):
            resp = rwgps._request_with_retry(method, "http://x")
        assert resp.status_code == 200
        assert method.call_count == 2

    def test_exhausts_retries_and_returns_429(self):
        rate_limited = _resp(429)
        method = MagicMock(return_value=rate_limited)
        with patch.object(rwgps.time, "sleep"):
            resp = rwgps._request_with_retry(method, "http://x", max_retries=2)
        assert resp.status_code == 429
        assert method.call_count == 3  # initial + 2 retries


def _trip(avg_hr=140, max_hr=170, avg_watts=200, max_watts=400, avg_cad=85,
          moving_time=7200, duration=8000, calories=1200, **overrides):
    # Shape of a real GET /trips/{id}.json (detail) response: sensor aggregates
    # are NESTED under `metrics` (hr/watts/cad sub-objects with avg/max, plus
    # movingTime/duration/calories) — NOT top-level. distance, elevation_gain,
    # departed_at and is_stationary ARE top-level. (The trips LIST endpoint
    # exposes the metrics top-level instead; _parse_trip handles both.)
    base = {
        "id": 777, "name": "Morning Ride", "departed_at": "2026-06-01T08:00:00Z",
        "distance": 50000.0, "elevation_gain": 500.0,
        "device": "Hammerhead Karoo 3",
        "activity_type": "cycling:road", "fit_sport": "cycling",
        "is_stationary": False,
        "metrics": {
            "hr": {"avg": avg_hr, "max": max_hr},
            "watts": {"avg": avg_watts, "max": max_watts},
            "cad": {"avg": avg_cad},
            "duration": duration,
            "movingTime": moving_time,
            "calories": calories,
        },
    }
    base.update(overrides)
    return base


class TestIsCycling:
    @pytest.mark.parametrize("atype", [
        "cycling:generic", "cycling:road", "cycling:gravel", "cycling:cyclocross",
        "cycling:mountain", "cycling:recumbent", "cycling:hand_cycling",
        "cycling:commute", "cycling:indoor", "cycling:virtual",
        "e_biking:generic", "e_biking:road", "e_biking:mountain",
    ])
    def test_cycling_types_accepted(self, atype):
        assert rwgps._is_cycling(_trip(activity_type=atype)) is True

    @pytest.mark.parametrize("atype", ["running:generic", "hiking:generic", "walking:generic"])
    def test_non_cycling_rejected(self, atype):
        assert rwgps._is_cycling(_trip(activity_type=atype)) is False

    def test_missing_type_falls_back_to_fit_sport(self):
        assert rwgps._is_cycling(_trip(activity_type=None, fit_sport="cycling")) is True

    def test_missing_type_and_non_cycling_fit_sport_rejected(self):
        assert rwgps._is_cycling(_trip(activity_type=None, fit_sport="running")) is False

    def test_missing_everything_rejected(self):
        assert rwgps._is_cycling({"id": 1}) is False


class TestMapStravaType:
    def test_virtual(self):
        assert rwgps._map_strava_type(_trip(activity_type="cycling:virtual")) == "VirtualRide"

    def test_ebike(self):
        assert rwgps._map_strava_type(_trip(activity_type="e_biking:mountain")) == "EBikeRide"

    def test_plain_ride(self):
        assert rwgps._map_strava_type(_trip(activity_type="cycling:road")) == "Ride"


class TestDetectDevice:
    def test_karoo(self):
        assert rwgps._detect_device(_trip(device="Hammerhead Karoo 3")) == "karoo"

    def test_watch(self):
        assert rwgps._detect_device(_trip(device="Apple Watch SE")) == "watch"

    def test_virtual_is_zwift(self):
        assert rwgps._detect_device(_trip(device="", activity_type="cycling:virtual")) == "zwift"

    def test_zwift_by_name(self):
        assert rwgps._detect_device(_trip(device="", name="Zwift - Watopia")) == "zwift"

    def test_unknown(self):
        assert rwgps._detect_device(_trip(device="Garmin Edge 540")) == "unknown"

    def test_none_device(self):
        assert rwgps._detect_device(_trip(device=None)) == "unknown"


class TestParseTrip:
    def test_basic_fields(self):
        d = rwgps._parse_trip(_trip())
        assert d["rwgps_id"] == 777
        assert d["name"] == "Morning Ride"
        assert d["date"] == "2026-06-01T08:00:00Z"
        assert d["distance_m"] == 50000.0
        assert d["duration_s"] == 7200  # moving_time preferred
        assert d["elevation_m"] == 500.0
        assert d["avg_hr"] == 140
        assert d["max_power"] == 400
        assert d["avg_cadence"] == 85
        assert d["calories"] == 1200
        assert d["device"] == "karoo"
        assert d["strava_type"] == "Ride"
        assert d["trainer"] is False
        assert d["suffer_score"] is None
        assert "strava_id" not in d

    def test_detail_endpoint_nests_metrics(self):
        """Regression: the trip-DETAIL response nests sensor aggregates under
        `metrics` (not top-level). _parse_trip must read them there, otherwise
        every RWGPS ride stores NULL power/HR/cadence and duration_s=0 — which
        blanks the Overview averages and zeroes TSS/speed."""
        trip = {
            "id": 42, "name": "Ride", "departed_at": "2026-06-01T08:00:00Z",
            "distance": 50000.0, "elevation_gain": 500.0,
            "activity_type": "cycling:road", "fit_sport": "cycling",
            "is_stationary": False, "device": "Hammerhead Karoo 3",
            "metrics": {
                "hr": {"avg": 142, "max": 178},
                "watts": {"avg": 210, "max": 620},
                "cad": {"avg": 88},
                "movingTime": 7200, "duration": 8000, "calories": 1350,
            },
        }
        d = rwgps._parse_trip(trip)
        assert d["avg_power"] == 210
        assert d["max_power"] == 620
        assert d["avg_hr"] == 142
        assert d["max_hr"] == 178
        assert d["avg_cadence"] == 88
        assert d["duration_s"] == 7200
        assert d["calories"] == 1350
        assert d["avg_speed_kmh"] == pytest.approx(25.0, abs=0.1)

    def test_list_shape_top_level_metrics_still_parse(self):
        """The trips LIST endpoint exposes the same values top-level — the
        top-level fallback must still parse them (no `metrics` object)."""
        trip = {
            "id": 43, "name": "Ride", "departed_at": "2026-06-01T08:00:00Z",
            "distance": 50000.0, "elevation_gain": 500.0,
            "activity_type": "cycling:road", "fit_sport": "cycling",
            "is_stationary": False,
            "avg_hr": 140, "max_hr": 170, "avg_watts": 200, "max_watts": 400,
            "avg_cad": 85, "moving_time": 7200, "duration": 8000, "calories": 1200,
        }
        d = rwgps._parse_trip(trip)
        assert d["avg_power"] == 200
        assert d["avg_hr"] == 140
        assert d["duration_s"] == 7200

    def test_moving_time_fallback_to_duration(self):
        d = rwgps._parse_trip(_trip(moving_time=None))
        assert d["duration_s"] == 8000

    def test_avg_speed_computed_from_distance_and_moving_time(self):
        # 50000 m / 7200 s * 3.6 = 25.0 km/h
        d = rwgps._parse_trip(_trip())
        assert d["avg_speed_kmh"] == pytest.approx(25.0, abs=0.1)

    def test_zero_duration_no_crash(self):
        d = rwgps._parse_trip(_trip(moving_time=0, duration=0))
        assert d["avg_speed_kmh"] == 0.0

    def test_stationary_sets_trainer(self):
        d = rwgps._parse_trip(_trip(is_stationary=True))
        assert d["trainer"] is True

    def test_indoor_type_sets_trainer(self):
        d = rwgps._parse_trip(_trip(activity_type="cycling:indoor"))
        assert d["trainer"] is True


class TestParseTrackPoints:
    def test_offsets_normalised_to_first_point(self):
        pts = [
            {"t": 1700000000, "x": -9.1, "y": 38.7, "e": 100, "s": 25.0, "h": 140, "c": 85, "p": 200},
            {"t": 1700000005, "x": -9.2, "y": 38.8, "e": 105, "s": 26.0, "h": 142, "c": 86, "p": 210},
        ]
        out = rwgps._parse_track_points(pts)
        assert out[0]["time_offset"] == 0
        assert out[1]["time_offset"] == 5
        assert out[0] == {"time_offset": 0, "hr": 140, "power": 200, "cadence": 85,
                          "speed_kmh": 25.0, "altitude_m": 100, "lat": 38.7, "lng": -9.1}

    def test_missing_sensor_keys_are_none(self):
        out = rwgps._parse_track_points([{"t": 1700000000, "x": -9.1, "y": 38.7}])
        assert out[0]["hr"] is None
        assert out[0]["power"] is None
        assert out[0]["speed_kmh"] is None

    def test_empty_list(self):
        assert rwgps._parse_track_points([]) == []

    def test_points_without_time_skipped(self):
        pts = [{"t": 1700000000, "h": 140}, {"h": 999}, {"t": 1700000010, "h": 150}]
        out = rwgps._parse_track_points(pts)
        assert len(out) == 2
        assert out[1]["time_offset"] == 10

    def test_first_point_missing_time_returns_empty(self):
        assert rwgps._parse_track_points([{"h": 140}]) == []

    def test_offsets_sorted_when_points_out_of_order(self):
        # RWGPS does not guarantee track_points arrive chronologically. Offsets
        # must be measured from the earliest point and come out ascending, with
        # each sample's sensor values travelling with its own timestamp.
        pts = [
            {"t": 1700000010, "h": 150},
            {"t": 1700000000, "h": 140},
            {"t": 1700000005, "h": 145},
        ]
        out = rwgps._parse_track_points(pts)
        assert [p["time_offset"] for p in out] == [0, 5, 10]
        assert [p["hr"] for p in out] == [140, 145, 150]

    def test_offsets_strictly_increasing_with_subsecond_samples(self):
        # Sub-second / same-second timestamps must not collapse to duplicate
        # integer offsets — the telemetry trend charts require a strictly
        # ascending x-index, and a plain int(t - t0) truncates ties.
        pts = [
            {"t": 1700000000.0, "h": 140},
            {"t": 1700000000.4, "h": 141},
            {"t": 1700000000.9, "h": 142},
            {"t": 1700000001.2, "h": 143},
        ]
        offsets = [p["time_offset"] for p in rwgps._parse_track_points(pts)]
        assert offsets == sorted(offsets)
        assert len(set(offsets)) == len(offsets)


from datetime import datetime, timezone, timedelta


def _sync_item(item_id, action="created", dt="2026-06-01T10:00:00Z"):
    return {"item_type": "trip", "item_id": item_id, "action": action,
            "datetime": dt}


class TestSyncActivities:
    def _run(self, items, cursor="2026-06-03T00:00:00Z", trips=None,
             deletion_outcome="deleted", since=None, departed_after=None):
        """Drive sync_activities with everything mocked. Returns
        (result, db_mock, fetch_trip_mock)."""
        db_mock = MagicMock()
        db_mock.get_sync_state.return_value = "2026-05-01T00:00:00Z"
        db_mock.upsert_activity.return_value = (1, False)
        db_mock.handle_rwgps_deletion.return_value = deletion_outcome

        def _fetch(tid):
            """Trips dict values may be Exceptions — raised to simulate transient failures."""
            val = (trips or {}).get(tid, {})
            if isinstance(val, Exception):
                raise val
            return val

        fetch_trip = MagicMock(side_effect=_fetch)
        with patch.dict("os.environ", _ENV), \
                patch.dict(sys.modules, {"db": db_mock}), \
                patch.object(rwgps, "fetch_sync_items", return_value=(items, cursor)), \
                patch.object(rwgps, "fetch_trip", fetch_trip), \
                patch.object(rwgps.time, "sleep"):
            result = rwgps.sync_activities(MagicMock(), since=since,
                                           departed_after=departed_after)
        return result, db_mock, fetch_trip

    def test_created_trip_ingested_with_streams(self):
        trip = _trip(track_points=[{"t": 1700000000, "h": 140}, {"t": 1700000001, "h": 141}])
        (ingested, deleted), db_mock, _ = self._run([_sync_item(777)], trips={777: trip})
        assert (ingested, deleted) == (1, 0)
        db_mock.upsert_activity.assert_called_once()
        db_mock.upsert_streams.assert_called_once()

    def test_streams_preserved_skips_stream_write(self):
        trip = _trip(track_points=[{"t": 1700000000, "h": 140}])
        db_mock = MagicMock()
        db_mock.get_sync_state.return_value = None
        db_mock.upsert_activity.return_value = (1, True)  # dedup preserved streams
        with patch.dict("os.environ", _ENV), \
                patch.dict(sys.modules, {"db": db_mock}), \
                patch.object(rwgps, "fetch_sync_items", return_value=([_sync_item(777)], "c")), \
                patch.object(rwgps, "fetch_trip", return_value=trip), \
                patch.object(rwgps.time, "sleep"):
            rwgps.sync_activities(MagicMock())
        db_mock.upsert_streams.assert_not_called()

    def test_non_cycling_trip_skipped(self):
        trip = _trip(activity_type="running:generic")
        (ingested, _), db_mock, _ = self._run([_sync_item(777)], trips={777: trip})
        assert ingested == 0
        db_mock.upsert_activity.assert_not_called()

    def test_deleted_item_routed_to_deletion_handler(self):
        (ingested, deleted), db_mock, fetch_trip = self._run(
            [_sync_item(777, action="deleted")])
        assert (ingested, deleted) == (0, 1)
        db_mock.handle_rwgps_deletion.assert_called_once()
        assert db_mock.handle_rwgps_deletion.call_args[0][1] == 777
        fetch_trip.assert_not_called()  # deletions never fetch the trip

    def test_unlinked_deletion_not_counted_as_deleted(self):
        (_, deleted), _, _ = self._run([_sync_item(777, action="deleted")],
                                       deletion_outcome="unlinked")
        assert deleted == 0

    def test_cursor_stored_after_batch(self):
        (_, _), db_mock, _ = self._run([], cursor="2026-06-09T12:00:00Z")
        db_mock.set_sync_state.assert_called_once()
        args = db_mock.set_sync_state.call_args[0]
        assert args[1] == "rwgps_last_sync_datetime"
        assert args[2] == "2026-06-09T12:00:00Z"

    def test_one_failing_trip_does_not_sink_batch(self):
        good = _trip()
        trips = {777: good}  # 888 returns {} -> skipped; 777 ok
        (ingested, _), _, _ = self._run(
            [_sync_item(888), _sync_item(777)], trips=trips)
        assert ingested == 1

    def test_transient_failure_withholds_cursor(self):
        """A transient failure in the FIRST (and only) timestamp group leaves no
        completed group to checkpoint, so the cursor is not advanced — the failed
        item would otherwise be silently lost until its next edit on RWGPS."""
        trips = {777: _trip(), 888: RuntimeError("boom")}  # both share the default datetime
        (ingested, _), db_mock, _ = self._run(
            [_sync_item(777), _sync_item(888)], trips=trips)
        assert ingested == 1
        db_mock.set_sync_state.assert_not_called()

    def test_transient_failure_checkpoints_completed_groups(self):
        """A transient failure partway through a multi-timestamp batch advances the
        cursor to the last fully-completed timestamp group — so a large/full-history
        backfill makes durable forward progress and converges, instead of discarding
        the whole pass on one blip and re-fetching the entire library every cycle."""
        items = [
            _sync_item(777, dt="2026-06-01T10:00:00Z"),   # group A — completes
            _sync_item(888, dt="2026-06-02T10:00:00Z"),   # group B — transient fail
            _sync_item(999, dt="2026-06-03T10:00:00Z"),   # group C — never reached
        ]
        trips = {777: _trip(), 888: RuntimeError("blip"), 999: _trip()}
        (ingested, _), db_mock, _ = self._run(items, trips=trips)
        assert ingested == 1                              # only group A landed this pass
        db_mock.set_sync_state.assert_called_once()
        assert db_mock.set_sync_state.call_args[0][2] == "2026-06-01T10:00:00Z"  # checkpoint = last done group

    def test_permanent_skip_still_advances_cursor(self):
        """fetch_trip returning {} (404/403) is a permanent skip, not a
        transient failure — retrying it forever would wedge the cursor."""
        (ingested, _), db_mock, _ = self._run([_sync_item(888)], trips={})
        assert ingested == 0
        db_mock.set_sync_state.assert_called_once()

    def test_malformed_trip_skipped_advances_cursor(self):
        """A deterministic per-item error (here a cycling trip whose detail is
        missing 'id' -> KeyError in _parse_trip) must be skipped like a 404, NOT
        bucketed as a transient failure. Otherwise the poison item replays every
        cycle and wedges the cursor, halting all ingestion past it — catastrophic
        during single-batch full-history backfill."""
        bad = {"name": "Broken", "activity_type": "cycling:road", "fit_sport": "cycling"}  # no 'id'
        (ingested, _), db_mock, _ = self._run(
            [_sync_item(777), _sync_item(888)], trips={777: _trip(), 888: bad})
        assert ingested == 1                          # the good trip still lands
        db_mock.set_sync_state.assert_called_once()   # cursor ADVANCES past the poison

    def test_typeerror_in_processing_is_permanent_skip(self):
        """A non-numeric track-point timestamp (int(t - t0) -> TypeError) is also
        deterministic — skipped, not withheld."""
        (ingested, _), db_mock, _ = self._run(
            [_sync_item(888)], trips={888: TypeError("non-numeric track point")})
        assert ingested == 0
        db_mock.set_sync_state.assert_called_once()

    def test_unparseable_departed_at_is_permanent_skip(self):
        """A non-ISO departed_at would make find_duplicate's ::timestamptz cast
        raise a psycopg2 DataError — neither KeyError nor TypeError, so without
        source validation it would be bucketed transient and wedge the cursor.
        Validate at the source: skip permanently, advance past it."""
        bad = _trip(departed_at="not-a-real-date")
        (ingested, _), db_mock, _ = self._run(
            [_sync_item(777), _sync_item(888)], trips={777: _trip(), 888: bad})
        assert ingested == 1                            # only the good trip lands
        db_mock.upsert_activity.assert_called_once()    # bad-date trip never reaches the DB
        db_mock.set_sync_state.assert_called_once()     # cursor ADVANCES

    def test_missing_departed_at_is_permanent_skip(self):
        """A trip with no departed_at is undated — skip it rather than storing a
        dateless ride (the dedup guard would also silently bypass)."""
        (ingested, _), db_mock, _ = self._run(
            [_sync_item(888)], trips={888: _trip(departed_at=None)})
        assert ingested == 0
        db_mock.upsert_activity.assert_not_called()
        db_mock.set_sync_state.assert_called_once()

    def test_departed_after_filters_old_rides(self):
        old_trip = _trip(departed_at="2024-01-01T08:00:00Z")
        cutoff = datetime(2026, 1, 1, tzinfo=timezone.utc)
        (ingested, _), db_mock, _ = self._run(
            [_sync_item(777)], trips={777: old_trip}, departed_after=cutoff)
        assert ingested == 0
        db_mock.upsert_activity.assert_not_called()

    def test_naive_departed_at_within_window_is_ingested(self):
        """A timezone-naive departed_at (no Z/offset) must be normalised to UTC
        before the window comparison. Otherwise naive<aware raises TypeError,
        which the permanent-skip branch swallows — silently dropping a ride that
        is actually inside the backfill window. Bug only shows with a cutoff set
        (bounded backfill), since polling passes departed_after=None."""
        naive_recent = _trip(departed_at="2026-06-10T08:00:00")  # naive, in window
        cutoff = datetime(2026, 1, 1, tzinfo=timezone.utc)
        (ingested, _), db_mock, _ = self._run(
            [_sync_item(777)], trips={777: naive_recent}, departed_after=cutoff)
        assert ingested == 1
        db_mock.upsert_activity.assert_called_once()
        db_mock.set_sync_state.assert_called_once()

    def test_naive_departed_at_before_window_is_filtered(self):
        """Naive timestamp older than the cutoff is filtered by the window
        (a clean skip), not dropped via a TypeError."""
        naive_old = _trip(departed_at="2024-01-01T08:00:00")  # naive, pre-window
        cutoff = datetime(2026, 1, 1, tzinfo=timezone.utc)
        (ingested, _), db_mock, _ = self._run(
            [_sync_item(777)], trips={777: naive_old}, departed_after=cutoff)
        assert ingested == 0
        db_mock.upsert_activity.assert_not_called()
        db_mock.set_sync_state.assert_called_once()

    def test_explicit_since_skips_sync_state_read(self):
        (_, _), db_mock, _ = self._run([], since="2026-06-01T00:00:00Z")
        db_mock.get_sync_state.assert_not_called()


class TestBackfill:
    def test_months_zero_uses_epoch_start(self):
        with patch.object(rwgps, "sync_activities", return_value=(5, 0)) as sync:
            assert rwgps.backfill(MagicMock(), months=0) == 5
        assert sync.call_args.kwargs["since"] == "1970-01-01T00:00:00Z"
        assert sync.call_args.kwargs.get("departed_after") is None

    def test_bounded_months_passes_cutoff(self):
        with patch.object(rwgps, "sync_activities", return_value=(3, 0)) as sync:
            assert rwgps.backfill(MagicMock(), months=12) == 3
        cutoff = sync.call_args.kwargs["departed_after"]
        expected = datetime.now(timezone.utc) - timedelta(days=360)
        assert abs((cutoff - expected).total_seconds()) < 60
        assert sync.call_args.kwargs["since"].startswith(str(expected.year))
