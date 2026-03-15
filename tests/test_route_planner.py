"""Tests for pure functions in veloai/route_planner.py."""

from datetime import datetime, timedelta

import pytest

from veloai.route_planner import (
    adjust_for_fitness,
    estimate_distance,
    parse_duration,
    parse_time,
    resolve_date,
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
