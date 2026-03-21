"""Tests for pure functions in ingestor/strava.py."""

import pytest

from strava import _detect_device, _parse_activity, _merge_detail, _parse_streams


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
