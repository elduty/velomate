"""Tests for classify_activity, merge_activity_data (ingestor/db.py)
and _score_weather (veloai/weather.py)."""

import sys
from pathlib import Path
from unittest.mock import MagicMock

# Mock modules not installed locally before importing
sys.modules["psycopg2"] = MagicMock()
sys.modules["psycopg2.extras"] = MagicMock()
sys.modules["requests"] = MagicMock()

# ingestor/ has no __init__.py — add it to sys.path so `from db import ...` works
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "ingestor"))

from db import classify_activity, merge_activity_data
from veloai.weather import _score_weather


# ---------------------------------------------------------------------------
# classify_activity
# ---------------------------------------------------------------------------


class TestClassifyActivity:
    def test_zwift_device(self):
        result = classify_activity({"device": "zwift", "distance_m": 30000})
        assert result["is_indoor"] is True
        assert result["sport_type"] == "zwift"

    def test_virtual_ride_strava_type(self):
        result = classify_activity({"strava_type": "VirtualRide", "distance_m": 30000})
        assert result["is_indoor"] is True
        assert result["sport_type"] == "zwift"

    def test_outdoor_ride_strava_type(self):
        result = classify_activity(
            {"strava_type": "Ride", "device": "karoo", "distance_m": 50000, "name": "Morning Ride"}
        )
        assert result["is_indoor"] is False
        assert result["sport_type"] == "cycling_outdoor"

    def test_outdoor_ride_no_strava_type(self):
        result = classify_activity(
            {"device": "karoo", "distance_m": 50000, "name": "Morning Ride"}
        )
        assert result["is_indoor"] is False
        assert result["sport_type"] == "cycling_outdoor"

    def test_indoor_trainer(self):
        result = classify_activity(
            {"strava_type": "Ride", "trainer": True, "distance_m": 20000}
        )
        assert result["is_indoor"] is True
        assert result["sport_type"] == "cycling_indoor"

    def test_indoor_no_distance(self):
        result = classify_activity(
            {"device": "unknown", "distance_m": 0, "name": "Spin"}
        )
        assert result["is_indoor"] is True
        assert result["sport_type"] == "cycling_indoor"

    def test_ebike_strava_type(self):
        result = classify_activity(
            {"strava_type": "EBikeRide", "distance_m": 30000}
        )
        assert result["is_indoor"] is False
        assert result["sport_type"] == "ebike"

    def test_none_values_no_crash(self):
        result = classify_activity(
            {"device": None, "distance_m": None, "name": None}
        )
        assert "is_indoor" in result
        assert "sport_type" in result


# ---------------------------------------------------------------------------
# merge_activity_data
# ---------------------------------------------------------------------------


class TestMergeActivityData:
    def test_richer_data_wins(self):
        # existing has HR only (richness 2), new has power (richness 3) — new wins
        existing = (1, 100, "watch", 50000, 140, None)
        new_data = {
            "device": "karoo",
            "avg_hr": None,
            "avg_power": 200,
            "distance_m": 50000,
        }
        merged = merge_activity_data(existing, new_data)
        assert merged.get("_skip_insert") is None
        # HR filled from existing record
        assert merged["avg_hr"] == 140
        # Power kept from new record
        assert merged["avg_power"] == 200

    def test_poorer_data_skipped(self):
        # existing has HR + power (richness 5), new has nothing — skip
        existing = (1, 100, "karoo", 50000, 140, 200)
        new_data = {"device": "watch"}
        merged = merge_activity_data(existing, new_data)
        assert merged["_skip_insert"] is True

    def test_equal_richness_new_wins(self):
        # both have HR only — new wins (tie goes to new)
        existing = (1, 100, "watch", 50000, 130, None)
        new_data = {"device": "garmin", "avg_hr": 135, "distance_m": 50000}
        merged = merge_activity_data(existing, new_data)
        assert merged.get("_skip_insert") is None


# ---------------------------------------------------------------------------
# _score_weather
# ---------------------------------------------------------------------------


class TestScoreWeather:
    def test_perfect_day(self):
        assert _score_weather(precip=0, wind=10, temp_max=22, code=0) == 100

    def test_heavy_rain(self):
        # precip 15 > 10 → -50; code 65 >= 61 → -15; total 100-50-15 = 35
        assert _score_weather(precip=15, wind=10, temp_max=22, code=65) == 35

    def test_strong_wind(self):
        # wind 35 > 30 → -25; total 100-25 = 75
        assert _score_weather(precip=0, wind=35, temp_max=22, code=0) == 75

    def test_cold(self):
        # temp_max 3 < 5 → -30; total 100-30 = 70
        assert _score_weather(precip=0, wind=10, temp_max=3, code=0) == 70

    def test_hot(self):
        # temp_max 39 > 38 → -30; total 100-30 = 70
        assert _score_weather(precip=0, wind=10, temp_max=39, code=0) == 70

    def test_terrible_day(self):
        # precip 20 → -50, wind 45 → -40, temp 3 → -30, code 95 → -15
        # 100-50-40-30-15 = -35 → clamped to 0
        assert _score_weather(precip=20, wind=45, temp_max=3, code=95) == 0


# ---------------------------------------------------------------------------
# best_ride_hours
# ---------------------------------------------------------------------------

from veloai.weather import best_ride_hours


class TestBestRideHours:
    def _hour(self, hour, temp=22, wind=10, uv=3, precip=0, wind_dir=180):
        return {
            "time": f"2026-03-15T{hour:02d}:00",
            "temp": temp,
            "wind": wind,
            "uv": uv,
            "precip": precip,
            "wind_direction": wind_dir,
        }

    def test_returns_only_requested_date(self):
        hours = [self._hour(10), self._hour(14),
                 {**self._hour(10), "time": "2026-03-16T10:00"}]
        result = best_ride_hours(hours, "2026-03-15")
        assert len(result) == 2

    def test_filters_to_daylight(self):
        """Only hours 6-20 included."""
        hours = [self._hour(5), self._hour(6), self._hour(20), self._hour(21)]
        result = best_ride_hours(hours, "2026-03-15")
        assert len(result) == 2

    def test_sorted_by_score_descending(self):
        hours = [self._hour(8, temp=22), self._hour(14, temp=2)]
        result = best_ride_hours(hours, "2026-03-15")
        assert result[0]["score"] >= result[1]["score"]

    def test_perfect_hour_scores_100(self):
        hours = [self._hour(10, temp=22, wind=10, uv=3, precip=0)]
        result = best_ride_hours(hours, "2026-03-15")
        assert result[0]["score"] == 100

    def test_cold_penalty(self):
        hours = [self._hour(10, temp=3)]
        result = best_ride_hours(hours, "2026-03-15")
        assert result[0]["score"] == 70  # -30 for <5°C

    def test_hot_penalty(self):
        hours = [self._hour(14, temp=39)]
        result = best_ride_hours(hours, "2026-03-15")
        assert result[0]["score"] == 70  # -30 for >38°C

    def test_wind_penalty(self):
        hours = [self._hour(10, wind=35)]
        result = best_ride_hours(hours, "2026-03-15")
        assert result[0]["score"] == 75  # -25 for >30

    def test_heavy_rain_penalty(self):
        hours = [self._hour(10, precip=5)]
        result = best_ride_hours(hours, "2026-03-15")
        assert result[0]["score"] == 60  # -40 for >2

    def test_uv_extreme_penalty(self):
        hours = [self._hour(14, uv=11)]
        result = best_ride_hours(hours, "2026-03-15")
        assert result[0]["score"] == 75  # -25 for >=11

    def test_uv_high_penalty(self):
        hours = [self._hour(14, uv=8)]
        result = best_ride_hours(hours, "2026-03-15")
        assert result[0]["score"] == 85  # -15 for >=8

    def test_score_clamped_to_zero(self):
        hours = [self._hour(10, temp=2, wind=45, precip=5, uv=11)]
        result = best_ride_hours(hours, "2026-03-15")
        assert result[0]["score"] == 0

    def test_empty_input(self):
        assert best_ride_hours([], "2026-03-15") == []

    def test_no_matching_date(self):
        hours = [self._hour(10)]
        assert best_ride_hours(hours, "2026-03-20") == []
