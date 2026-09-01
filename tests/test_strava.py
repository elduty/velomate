"""Tests for pure functions in ingestor/strava.py."""

from unittest.mock import patch, MagicMock

import pytest

import strava
from strava import _detect_device, _parse_activity, _merge_detail, _parse_streams, backfill


# --- _detect_device ---

class TestDetectDevice:
    def test_karoo(self):
        assert _detect_device({"device_name": "Hammerhead Karoo 3"}) == "karoo"

    def test_karoo_case_insensitive(self):
        assert _detect_device({"device_name": "KAROO 2"}) == "karoo"

    def test_apple_watch(self):
        assert _detect_device({"device_name": "Apple Watch SE"}) == "watch"

    def test_watch_generic(self):
        assert _detect_device({"device_name": "Garmin Watch"}) == "watch"

    def test_zwift_by_trainer(self):
        assert _detect_device({"device_name": "", "trainer": True}) == "zwift"

    def test_zwift_by_name(self):
        assert _detect_device({"device_name": "", "name": "Zwift - Watopia Loop"}) == "zwift"

    def test_unknown(self):
        assert _detect_device({"device_name": "Garmin Edge 540"}) == "unknown"

    def test_empty_dict(self):
        assert _detect_device({}) == "unknown"

    def test_missing_device_name(self):
        assert _detect_device({"name": "Morning Ride"}) == "unknown"

    def test_trainer_true_non_zwift_device_returns_zwift(self):
        """P3-4 finding: _detect_device returns 'zwift' for ALL trainer=True,
        regardless of actual device. This is because trainer check happens in
        the elif branch after karoo/watch, and returns 'zwift' unconditionally.
        classify_activity (in db.py) handles the trainer-vs-zwift distinction
        at a higher level using the device field."""
        result = _detect_device({"device_name": "Wahoo KICKR", "trainer": True})
        assert result == "zwift"

    def test_none_device_name(self):
        assert _detect_device({"device_name": None}) == "unknown"

    def test_garmin_edge_is_unknown(self):
        """Garmin Edge doesn't match karoo or watch patterns."""
        assert _detect_device({"device_name": "Garmin Edge 540"}) == "unknown"

    def test_garmin_edge_1040_is_unknown(self):
        assert _detect_device({"device_name": "Garmin Edge 1040"}) == "unknown"

    def test_hammerhead_karoo_2(self):
        assert _detect_device({"device_name": "Hammerhead Karoo 2"}) == "karoo"

    def test_empty_device_name_no_trainer(self):
        """Empty device name without trainer flag -> unknown."""
        assert _detect_device({"device_name": ""}) == "unknown"

    def test_empty_device_name_with_zwift_name(self):
        """Empty device, but activity name contains 'Zwift' -> zwift."""
        assert _detect_device({"device_name": "", "name": "Zwift - Road to Sky"}) == "zwift"


# --- _parse_activity ---

class TestParseActivity:
    def _raw(self, **overrides):
        base = {
            "id": 12345,
            "name": "Morning Ride",
            "start_date": "2026-03-15T08:00:00Z",
            "distance": 50000,
            "moving_time": 7200,
            "total_elevation_gain": 500,
            "average_heartrate": 145,
            "max_heartrate": 175,
            "average_watts": 200,
            "max_watts": 350,
            "average_cadence": 85,
            "average_speed": 6.944,  # m/s = 25 km/h
            "calories": 1200,
            "suffer_score": 120,
            "device_name": "Hammerhead Karoo 3",
            "type": "Ride",
            "trainer": False,
        }
        base.update(overrides)
        return base

    def test_basic_fields(self):
        result = _parse_activity(self._raw())
        assert result["strava_id"] == 12345
        assert result["name"] == "Morning Ride"
        assert result["distance_m"] == 50000
        assert result["duration_s"] == 7200
        assert result["elevation_m"] == 500

    def test_speed_conversion(self):
        """average_speed in m/s -> avg_speed_kmh."""
        result = _parse_activity(self._raw(average_speed=6.944))
        assert result["avg_speed_kmh"] == pytest.approx(25.0, abs=0.1)

    def test_speed_none_no_crash(self):
        result = _parse_activity(self._raw(average_speed=None))
        assert result["avg_speed_kmh"] == 0.0

    def test_speed_zero(self):
        result = _parse_activity(self._raw(average_speed=0))
        assert result["avg_speed_kmh"] == 0.0

    def test_device_detection_karoo(self):
        result = _parse_activity(self._raw(device_name="Hammerhead Karoo 3"))
        assert result["device"] == "karoo"

    def test_device_detection_zwift(self):
        result = _parse_activity(self._raw(device_name="", trainer=True))
        assert result["device"] == "zwift"

    def test_missing_optional_fields(self):
        """Missing HR/power/cadence should be None, not crash."""
        raw = {"id": 1, "device_name": "", "name": "Ride"}
        result = _parse_activity(raw)
        assert result["avg_hr"] is None
        assert result["avg_power"] is None
        assert result["avg_cadence"] is None

    def test_strava_type_preserved(self):
        result = _parse_activity(self._raw(type="VirtualRide"))
        assert result["strava_type"] == "VirtualRide"


# --- _merge_detail ---

class TestMergeDetail:
    def test_no_detail_returns_summary(self):
        summary = {"device": "karoo", "calories": 1200}
        assert _merge_detail(summary, None) == summary
        assert _merge_detail(summary, {}) == summary

    def test_karoo_calories_always_win(self):
        summary = {"device": "karoo", "calories": 1200}
        detail = {"calories": 800}
        result = _merge_detail(summary, detail)
        # Karoo detail calories override summary
        assert result["calories"] == 800

    def test_non_karoo_calories_fill_gap(self):
        summary = {"device": "unknown", "calories": None}
        detail = {"calories": 900}
        result = _merge_detail(summary, detail)
        assert result["calories"] == 900

    def test_non_karoo_doesnt_overwrite_existing_calories(self):
        summary = {"device": "unknown", "calories": 1200}
        detail = {"calories": 900}
        result = _merge_detail(summary, detail)
        assert result["calories"] == 1200

    def test_fills_missing_hr(self):
        summary = {"device": "unknown", "avg_hr": None}
        detail = {"average_heartrate": 150}
        result = _merge_detail(summary, detail)
        assert result["avg_hr"] == 150

    def test_doesnt_overwrite_existing_hr(self):
        summary = {"device": "unknown", "avg_hr": 145}
        detail = {"average_heartrate": 150}
        result = _merge_detail(summary, detail)
        assert result["avg_hr"] == 145

    def test_fills_suffer_score(self):
        summary = {"device": "unknown", "suffer_score": None}
        detail = {"suffer_score": 120}
        result = _merge_detail(summary, detail)
        assert result["suffer_score"] == 120

    def test_does_not_mutate_summary(self):
        summary = {"device": "karoo", "calories": 1200}
        original = dict(summary)
        _merge_detail(summary, {"calories": 800})
        assert summary == original


# --- _parse_streams ---

class TestParseStreams:
    def test_empty_input(self):
        assert _parse_streams({}) == []
        assert _parse_streams(None) == []

    def test_missing_time_key(self):
        assert _parse_streams({"heartrate": [150, 155]}) == []

    def test_basic_streams(self):
        raw = {
            "time": [0, 1, 2],
            "heartrate": [140, 145, 150],
            "watts": [200, 210, 220],
            "cadence": [85, 86, 87],
            "velocity_smooth": [6.944, 7.0, 7.2],
            "altitude": [100.0, 101.0, 102.0],
            "latlng": [[38.7, -9.14], [38.701, -9.139], [38.702, -9.138]],
        }
        points = _parse_streams(raw)
        assert len(points) == 3

    def test_point_fields(self):
        raw = {
            "time": [0],
            "heartrate": [150],
            "watts": [200],
            "cadence": [85],
            "velocity_smooth": [6.944],
            "altitude": [100.0],
            "latlng": [[38.7, -9.14]],
        }
        p = _parse_streams(raw)[0]
        assert p["time_offset"] == 0
        assert p["hr"] == 150
        assert p["power"] == 200
        assert p["cadence"] == 85
        assert p["altitude_m"] == 100.0
        assert p["lat"] == 38.7
        assert p["lng"] == -9.14

    def test_speed_conversion(self):
        """velocity_smooth in m/s -> speed_kmh."""
        raw = {"time": [0], "velocity_smooth": [6.944]}
        p = _parse_streams(raw)[0]
        assert p["speed_kmh"] == pytest.approx(25.0, abs=0.1)

    def test_missing_optional_arrays(self):
        """Only time is required; others default to None."""
        raw = {"time": [0, 1]}
        points = _parse_streams(raw)
        assert len(points) == 2
        assert points[0]["hr"] is None
        assert points[0]["power"] is None
        assert points[0]["lat"] is None

    def test_ragged_arrays(self):
        """HR array shorter than time array."""
        raw = {"time": [0, 1, 2], "heartrate": [150]}
        points = _parse_streams(raw)
        assert points[0]["hr"] == 150
        assert points[1]["hr"] is None
        assert points[2]["hr"] is None

    def test_empty_latlng_entry(self):
        """latlng with empty/None entries."""
        raw = {"time": [0, 1], "latlng": [[38.7, -9.14], None]}
        points = _parse_streams(raw)
        assert points[0]["lat"] == 38.7
        assert points[1]["lat"] is None


# --- backfill (months parameter → after_epoch) ---

class TestBackfill:
    """backfill() maps months -> after_epoch and delegates to sync_activities."""

    def test_positive_months_uses_cutoff(self):
        """months=12 -> after_epoch is ~12*30 days before now (non-zero)."""
        with patch("strava.sync_activities", return_value=7) as mock_sync:
            result = backfill(conn="conn", months=12)
        assert result == 7
        args, kwargs = mock_sync.call_args
        # sync_activities(conn, after_epoch) — positional
        assert args[0] == "conn"
        assert args[1] > 0  # a real epoch, not zero

    def test_zero_months_means_full_history(self):
        """months=0 -> after_epoch=0 signals full Strava history."""
        with patch("strava.sync_activities", return_value=500) as mock_sync:
            result = backfill(conn="conn", months=0)
        assert result == 500
        # months=0 uses the keyword form with after_epoch=0
        mock_sync.assert_called_once_with("conn", after_epoch=0)

    def test_default_is_twelve_months(self):
        """No months arg -> defaults to 12 (current behaviour preserved)."""
        with patch("strava.sync_activities", return_value=0) as mock_sync:
            backfill(conn="conn")
        args, _ = mock_sync.call_args
        assert args[1] > 0  # a cutoff epoch, not full history


class TestBackfillStravaSegmentsSentinel:
    """A ride with no uphill Strava segments (or a 404 detail) must get a
    source='strava' sentinel row so backfill_strava_segments stops re-fetching it
    from the Strava API on every recalculation. Without the sentinel the ride
    never gains a source='strava' row, so the NOT EXISTS selector keeps picking
    it up every cycle — burning API quota and stretching each recalc. Mirrors the
    detected-climbs 'none' sentinel; category='none' keeps it out of dashboards
    (their climbs query filters `category != 'none'`)."""

    def _conn(self, rides):
        conn = MagicMock()
        cur = MagicMock()
        conn.cursor.return_value.__enter__ = MagicMock(return_value=cur)
        conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
        cur.fetchall.return_value = rides
        cur.fetchone.return_value = None  # _store_strava_climbs "already stored?" checks
        return conn, cur

    def _sentinel_inserts(self, cur):
        return [
            c for c in cur.execute.call_args_list
            if "INSERT INTO ride_climbs" in c[0][0]
            and "'strava'" in c[0][0] and "'none'" in c[0][0]
        ]

    def test_ride_without_segments_gets_sentinel(self):
        conn, cur = self._conn([(1, 111)])
        detail_no_climbs = {"segment_efforts": []}  # no uphill segments at all
        with patch.object(strava, "_get_token", return_value="tok"), \
             patch.object(strava, "fetch_activity_detail", return_value=detail_no_climbs), \
             patch.object(strava.time, "sleep"):
            strava.backfill_strava_segments(conn)
        assert self._sentinel_inserts(cur), \
            "ride with no segments must get a source='strava' sentinel"

    def test_missing_detail_404_gets_sentinel(self):
        """A 404 (deleted on Strava) returns {} — sentinel it so it isn't re-fetched."""
        conn, cur = self._conn([(1, 111)])
        with patch.object(strava, "_get_token", return_value="tok"), \
             patch.object(strava, "fetch_activity_detail", return_value={}), \
             patch.object(strava.time, "sleep"):
            strava.backfill_strava_segments(conn)
        assert self._sentinel_inserts(cur), \
            "404 detail must get a source='strava' sentinel"

    def test_ride_with_segments_gets_no_sentinel(self):
        """A ride that DOES have storable segments stores them (source='strava')
        and must NOT also get a 'none' sentinel."""
        conn, cur = self._conn([(1, 111)])
        detail = {"segment_efforts": [{
            "elapsed_time": 600,
            "segment": {"average_grade": 5.0, "distance": 2000,
                        "elevation_high": 300, "elevation_low": 200, "name": "Test Climb"},
        }]}
        with patch.object(strava, "_get_token", return_value="tok"), \
             patch.object(strava, "fetch_activity_detail", return_value=detail), \
             patch.object(strava.time, "sleep"):
            strava.backfill_strava_segments(conn)
        assert not self._sentinel_inserts(cur), \
            "ride with real segments must not also get a 'none' sentinel"

    def test_transient_error_does_not_sentinel(self):
        """Review guard (#150): a transient failure — 429 rate-limit, 5xx, or a
        network blip — makes fetch_activity_detail RAISE (via raise_for_status);
        it returns {} only on a genuine 404. The raise is caught by the loop's
        except and the ride is retried next cycle — it must NOT be sentineled,
        or a backfill run during quota exhaustion would permanently mark real
        rides as segment-free. Any raised exception exercises this path (the
        loop catches broad Exception), so we don't couple to requests.HTTPError
        (other test modules replace `requests` with a MagicMock at import)."""
        conn, cur = self._conn([(1, 111)])
        with patch.object(strava, "_get_token", return_value="tok"), \
             patch.object(strava, "fetch_activity_detail",
                          side_effect=RuntimeError("simulated 429/5xx/network error")), \
             patch.object(strava.time, "sleep"):
            strava.backfill_strava_segments(conn)
        assert not self._sentinel_inserts(cur), \
            "a transient error must NOT sentinel the ride — it is retried next cycle"
