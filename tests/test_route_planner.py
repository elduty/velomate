"""Tests for pure functions in velomate/route_planner.py."""

from datetime import datetime, timedelta

import pytest

from velomate.route_planner import (
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


# --- waypoint parsing ---


from unittest.mock import patch, MagicMock


class TestWaypointParsing:
    """Test that plan() parses semicolon-separated waypoints via parse_location."""

    @patch("velomate.route_generator.generate")
    @patch("velomate.route_planner._get_strava_token")
    @patch("velomate.geocode.geocode")
    @patch("velomate.db.get_connection", return_value=None)
    @patch("velomate.weather.fetch_forecast", return_value=[])
    def test_semicolon_separator(self, mock_weather, mock_db, mock_geocode, mock_token, mock_generate):
        """Semicolon-separated waypoints should each be geocoded."""
        mock_geocode.side_effect = [
            {"lat": 38.69, "lng": -9.42, "display_name": "Cascais, Portugal"},
            {"lat": 38.70, "lng": -9.40, "display_name": "Estoril, Portugal"},
        ]
        mock_token.return_value = None
        mock_generate.return_value = {
            "gpx_path": "/tmp/test.gpx",
            "actual_km": 30.0,
            "name": "test",
            "coords": [(38.7, -9.1), (38.69, -9.42)],
        }

        from velomate.route_planner import plan
        plan(
            distance_str="30km",
            home_lat=38.7, home_lng=-9.1,
            waypoints_str="Cascais;Estoril",
        )

        assert mock_geocode.call_count == 2
        mock_geocode.assert_any_call("Cascais", 38.7, -9.1)
        mock_geocode.assert_any_call("Estoril", 38.7, -9.1)

    @patch("velomate.route_generator.generate")
    @patch("velomate.route_planner._get_strava_token")
    @patch("velomate.geocode.geocode")
    @patch("velomate.db.get_connection", return_value=None)
    @patch("velomate.weather.fetch_forecast", return_value=[])
    def test_single_waypoint_no_semicolon(self, mock_weather, mock_db, mock_geocode, mock_token, mock_generate):
        """Single waypoint without semicolon still works."""
        mock_geocode.return_value = {"lat": 38.69, "lng": -9.42, "display_name": "Cascais, Portugal"}
        mock_token.return_value = None
        mock_generate.return_value = {
            "gpx_path": "/tmp/test.gpx",
            "actual_km": 30.0,
            "name": "test",
            "coords": [(38.7, -9.1), (38.69, -9.42)],
        }

        from velomate.route_planner import plan
        plan(
            distance_str="30km",
            home_lat=38.7, home_lng=-9.1,
            waypoints_str="Cascais",
        )

        mock_geocode.assert_called_once_with("Cascais", 38.7, -9.1)


# --- destination integration ---

import logging


class TestPlanWithDestination:
    """Integration tests for plan() with --destination."""

    @patch("velomate.route_generator.generate")
    @patch("velomate.route_planner._get_strava_token")
    @patch("velomate.db.get_connection", return_value=None)
    @patch("velomate.weather.fetch_forecast", return_value=[])
    def test_destination_only_no_distance(self, mock_weather, mock_db, mock_token, mock_generate):
        mock_token.return_value = None
        mock_generate.return_value = {
            "gpx_path": "/tmp/test.gpx", "actual_km": 35.0,
            "name": "test", "coords": [(38.7, -9.1), (38.69, -9.42)],
        }
        from velomate.route_planner import plan
        result = plan(
            home_lat=38.7, home_lng=-9.14,
            destination={"lat": 38.69, "lng": -9.42, "name": "Cascais"},
        )
        assert "Error" not in result
        call_kwargs = mock_generate.call_args[1]
        assert call_kwargs["destination"] == {"lat": 38.69, "lng": -9.42, "name": "Cascais"}

    @patch("velomate.route_generator.generate")
    @patch("velomate.route_planner._get_strava_token")
    @patch("velomate.db.get_connection", return_value=None)
    @patch("velomate.weather.fetch_forecast", return_value=[])
    def test_destination_no_loop(self, mock_weather, mock_db, mock_token, mock_generate):
        mock_token.return_value = None
        mock_generate.return_value = {
            "gpx_path": "/tmp/test.gpx", "actual_km": 30.0,
            "name": "test", "coords": [(38.7, -9.1), (38.69, -9.42)],
        }
        from velomate.route_planner import plan
        plan(
            home_lat=38.7, home_lng=-9.14,
            destination={"lat": 38.69, "lng": -9.42, "name": "Cascais"},
            loop=False,
        )
        assert mock_generate.call_args[1]["loop"] is False

    @patch("velomate.route_generator.generate")
    @patch("velomate.route_planner._get_strava_token")
    @patch("velomate.db.get_connection", return_value=None)
    @patch("velomate.weather.fetch_forecast", return_value=[])
    def test_destination_with_loop_doubles_distance(self, mock_weather, mock_db, mock_token, mock_generate):
        mock_token.return_value = None
        mock_generate.return_value = {
            "gpx_path": "/tmp/test.gpx", "actual_km": 60.0,
            "name": "test", "coords": [(38.7, -9.1), (38.69, -9.42), (38.7, -9.1)],
        }
        from velomate.route_planner import plan
        plan(
            home_lat=38.7, home_lng=-9.14,
            destination={"lat": 38.69, "lng": -9.42, "name": "Cascais"},
            loop=True,
        )
        assert mock_generate.call_args[1]["loop"] is True
        assert mock_generate.call_args[1]["target_km"] > 40

    @patch("velomate.route_generator.generate")
    @patch("velomate.route_planner._get_strava_token")
    @patch("velomate.db.get_connection", return_value=None)
    @patch("velomate.weather.fetch_forecast", return_value=[])
    def test_destination_auto_disables_loop_in_plan(self, mock_weather, mock_db, mock_token, mock_generate):
        """plan() with destination and loop=None should auto-set loop=False."""
        mock_token.return_value = None
        mock_generate.return_value = {
            "gpx_path": "/tmp/test.gpx", "actual_km": 30.0,
            "name": "test", "coords": [(38.7, -9.1), (38.69, -9.42)],
        }
        from velomate.route_planner import plan
        plan(
            home_lat=38.7, home_lng=-9.14,
            destination={"lat": 38.69, "lng": -9.42, "name": "Cascais"},
            # loop not passed — defaults to None, should resolve to False
        )
        assert mock_generate.call_args[1]["loop"] is False

    def test_no_destination_no_distance_errors(self):
        from velomate.route_planner import plan
        result = plan(home_lat=38.7, home_lng=-9.14)
        assert "Error" in result

    @patch("velomate.route_generator.generate")
    @patch("velomate.route_planner._get_strava_token")
    @patch("velomate.db.get_connection", return_value=None)
    @patch("velomate.weather.fetch_forecast", return_value=[])
    def test_route_name_includes_destination(self, mock_weather, mock_db, mock_token, mock_generate):
        mock_token.return_value = None
        mock_generate.return_value = {
            "gpx_path": "/tmp/test.gpx", "actual_km": 30.0,
            "name": "test", "coords": [(38.7, -9.1), (38.69, -9.42)],
        }
        from velomate.route_planner import plan
        plan(
            home_lat=38.7, home_lng=-9.14,
            destination={"lat": 38.69, "lng": -9.42, "name": "Cascais"},
            loop=False,
        )
        assert "Cascais" in mock_generate.call_args[1]["name"]

    @patch("velomate.route_generator.generate")
    @patch("velomate.route_planner._get_strava_token")
    @patch("velomate.db.get_connection", return_value=None)
    @patch("velomate.weather.fetch_forecast", return_value=[])
    def test_destination_valid_without_duration_or_distance(self, mock_weather, mock_db, mock_token, mock_generate):
        mock_token.return_value = None
        mock_generate.return_value = {
            "gpx_path": "/tmp/test.gpx", "actual_km": 30.0,
            "name": "test", "coords": [(38.7, -9.1), (38.69, -9.42)],
        }
        from velomate.route_planner import plan
        result = plan(
            home_lat=38.7, home_lng=-9.14,
            destination={"lat": 38.69, "lng": -9.42, "name": "Cascais"},
            loop=False,
        )
        assert "Error" not in result


class TestDestinationWarnings:
    """Test that log warnings fire for flag clashes."""

    @patch("velomate.route_generator.generate")
    @patch("velomate.route_planner._get_strava_token")
    @patch("velomate.db.get_connection", return_value=None)
    @patch("velomate.weather.fetch_forecast", return_value=[])
    def test_warns_baseline_exceeds_target(self, mock_weather, mock_db, mock_token, mock_generate, caplog):
        mock_token.return_value = None
        mock_generate.return_value = {
            "gpx_path": "/tmp/test.gpx", "actual_km": 10.0,
            "name": "test", "coords": [(38.7, -9.1), (38.69, -9.42)],
        }
        from velomate.route_planner import plan
        with caplog.at_level(logging.WARNING):
            plan(
                distance_str="10km",
                home_lat=38.7, home_lng=-9.14,
                destination={"lat": 38.69, "lng": -9.42, "name": "Cascais"},
                loop=False,
            )
        assert any("routing directly" in r.message.lower() for r in caplog.records)

    @patch("velomate.route_generator.generate")
    @patch("velomate.route_planner._get_strava_token")
    @patch("velomate.geocode.parse_location")
    @patch("velomate.db.get_connection", return_value=None)
    @patch("velomate.weather.fetch_forecast", return_value=[])
    def test_warns_explicit_waypoints_skip_padding(self, mock_weather, mock_db, mock_parse, mock_token, mock_generate, caplog):
        mock_parse.return_value = {"lat": 38.70, "lng": -9.30, "name": "Oeiras"}
        mock_token.return_value = None
        mock_generate.return_value = {
            "gpx_path": "/tmp/test.gpx", "actual_km": 40.0,
            "name": "test", "coords": [(38.7, -9.1), (38.70, -9.30), (38.69, -9.42)],
        }
        from velomate.route_planner import plan
        with caplog.at_level(logging.WARNING):
            plan(
                distance_str="50km",
                home_lat=38.7, home_lng=-9.14,
                destination={"lat": 38.69, "lng": -9.42, "name": "Cascais"},
                waypoints_str="Oeiras",
                loop=False,
            )
        assert any("ignoring" in r.message.lower() for r in caplog.records)
