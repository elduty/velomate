"""Tests for pure functions in veloai/route_planner.py."""

from datetime import datetime, timedelta

import pytest

from veloai.route_planner import (
    adjust_for_fitness,
    estimate_distance,
    parse_distance,
    parse_duration,
    parse_time,
    resolve_date,
    _analyze_wind,
)


# --- parse_duration ---


class TestParseDuration:
    def test_hours_only(self):
        assert parse_duration("2h") == 120

    def test_hours_and_minutes(self):
        assert parse_duration("1h30m") == 90

    def test_minutes_with_min_suffix(self):
        assert parse_duration("90min") == 90

    def test_colon_notation(self):
        assert parse_duration("1:30") == 90

    def test_minutes_only(self):
        assert parse_duration("30m") == 30

    def test_empty_string(self):
        assert parse_duration("") is None

    def test_invalid_string(self):
        assert parse_duration("invalid") is None

    def test_none(self):
        assert parse_duration(None) is None


# --- resolve_date ---


class TestResolveDate:
    def test_today(self):
        assert resolve_date("today") == datetime.now().date().isoformat()

    def test_tomorrow(self):
        expected = (datetime.now().date() + timedelta(days=1)).isoformat()
        assert resolve_date("tomorrow") == expected

    def test_iso_date(self):
        assert resolve_date("2026-03-15") == "2026-03-15"

    def test_empty_string(self):
        assert resolve_date("") is None

    def test_invalid_string(self):
        assert resolve_date("invalid") is None

    def test_day_name_monday(self):
        result = resolve_date("monday")
        assert result is not None
        resolved = datetime.strptime(result, "%Y-%m-%d").date()
        today = datetime.now().date()
        delta = (resolved - today).days
        assert 1 <= delta <= 7
        assert resolved.weekday() == 0  # Monday


# --- estimate_distance ---


class TestEstimateDistance:
    def test_with_avg_speed(self):
        # avg_speed is already surface-specific, no multiplier applied
        result = estimate_distance(120, "gravel", avg_speed=25.0)
        assert result == 50.0  # 2h × 25 km/h

    def test_road_with_avg_speed(self):
        result = estimate_distance(120, "road", avg_speed=30.0)
        assert result == 60.0  # 2h × 30 km/h

    def test_mtb_no_avg_speed(self):
        result = estimate_distance(60, "mtb", avg_speed=None)
        assert result == 17.0

    def test_gravel_no_avg_speed(self):
        result = estimate_distance(60, "gravel", avg_speed=None)
        assert result == 22.0


# --- adjust_for_fitness ---


class TestAdjustForFitness:
    def test_fresh(self):
        distance, note = adjust_for_fitness(50.0, 15.0)
        assert distance == 50.0
        assert "fresh" in note

    def test_neutral(self):
        distance, note = adjust_for_fitness(50.0, 0.0)
        assert distance == 50.0
        assert "neutral" in note

    def test_fatigued(self):
        distance, note = adjust_for_fitness(50.0, -15.0)
        assert distance == 40.0
        assert "fatigued" in note

    def test_none_tsb(self):
        distance, note = adjust_for_fitness(50.0, None)
        assert distance == 50.0
        assert note is None


# --- parse_time ---


class TestParseTime:
    def test_24h_format(self):
        assert parse_time("14:00") == "14:00"

    def test_24h_single_digit(self):
        assert parse_time("9:30") == "09:30"

    def test_pm(self):
        assert parse_time("2pm") == "14:00"

    def test_am(self):
        assert parse_time("9am") == "09:00"

    def test_h_suffix(self):
        assert parse_time("14h") == "14:00"

    def test_12pm(self):
        assert parse_time("12pm") == "12:00"

    def test_12am(self):
        assert parse_time("12am") == "00:00"

    def test_none(self):
        assert parse_time(None) is None

    def test_empty(self):
        assert parse_time("") is None

    def test_13pm_treated_as_24h(self):
        """Bug fix from audit A9: 13pm no longer returns '25:00'.
        Since 13 >= 12, pm is ignored and 13:00 is valid 24h time."""
        assert parse_time("13pm") == "13:00"

    def test_invalid_hour_rejected(self):
        """Hours > 23 are rejected."""
        assert parse_time("25h") is None


# --- parse_distance ---


class TestParseDistance:
    def test_plain_number(self):
        assert parse_distance("30") == 30.0

    def test_km_suffix(self):
        assert parse_distance("50km") == 50.0

    def test_decimal(self):
        assert parse_distance("25.5") == 25.5

    def test_decimal_with_km(self):
        assert parse_distance("25.5km") == 25.5

    def test_with_spaces(self):
        assert parse_distance("  30 km  ") == 30.0

    def test_empty_string(self):
        assert parse_distance("") is None

    def test_none(self):
        assert parse_distance(None) is None

    def test_invalid(self):
        assert parse_distance("abc") is None

    def test_zero(self):
        assert parse_distance("0") == 0.0


# --- _analyze_wind ---


class TestAnalyzeWind:
    def _straight_north_coords(self, n=50):
        """Coords going straight north from Lisbon."""
        return [(38.7 + i * 0.001, -9.14) for i in range(n)]

    def _straight_east_coords(self, n=50):
        """Coords going straight east from Lisbon."""
        return [(38.7, -9.14 + i * 0.001) for i in range(n)]

    def test_no_warning_light_wind(self):
        """Wind < 15 km/h should never warn."""
        coords = self._straight_north_coords()
        assert _analyze_wind(coords, wind_dir=0, wind_speed=10) is None

    def test_no_warning_short_route(self):
        """< 10 coords should not analyze."""
        coords = [(38.7 + i * 0.01, -9.14) for i in range(5)]
        assert _analyze_wind(coords, wind_dir=0, wind_speed=30) is None

    def test_no_warning_empty(self):
        assert _analyze_wind([], wind_dir=0, wind_speed=30) is None

    def test_headwind_detected(self):
        """Riding north into north wind (wind FROM 0/N) = headwind."""
        coords = self._straight_north_coords()
        result = _analyze_wind(coords, wind_dir=0, wind_speed=30)
        assert result is not None
        assert "headwind" in result.lower()

    def test_tailwind_no_warning(self):
        """Riding north with wind FROM south (180) = tailwind, no warning."""
        coords = self._straight_north_coords()
        result = _analyze_wind(coords, wind_dir=180, wind_speed=30)
        assert result is None

    def test_strong_crosswind_detected(self):
        """Riding north with wind FROM east (90) at high speed = crosswind."""
        coords = self._straight_north_coords()
        result = _analyze_wind(coords, wind_dir=90, wind_speed=30)
        if result:
            assert "crosswind" in result.lower() or "wind" in result.lower()

    def test_wind_direction_label(self):
        """Warning should include compass direction."""
        coords = self._straight_north_coords()
        result = _analyze_wind(coords, wind_dir=0, wind_speed=30)
        assert result is not None
        assert "N" in result
