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

    def test_strength_keywords_weight(self):
        result = classify_activity(
            {"device": "watch", "name": "Weight Training", "distance_m": 0}
        )
        assert result["is_indoor"] is True
        assert result["sport_type"] == "strength"

    def test_outdoor_ride(self):
        result = classify_activity(
            {"device": "karoo", "distance_m": 50000, "name": "Morning Ride"}
        )
        assert result["is_indoor"] is False
        assert result["sport_type"] == "cycling_outdoor"

    def test_indoor_no_distance(self):
        result = classify_activity(
            {"device": "unknown", "distance_m": 0, "name": "Spin"}
        )
        assert result["is_indoor"] is True
        assert result["sport_type"] == "cycling_indoor"

    def test_gym_keyword(self):
        result = classify_activity(
            {"device": "watch", "name": "Gym Session", "distance_m": 0}
        )
        assert result["is_indoor"] is True
        assert result["sport_type"] == "strength"

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
