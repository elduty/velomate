# tests/test_geocode.py
"""Tests for velomate/geocode.py."""

import pytest
from unittest.mock import patch

from velomate.geocode import parse_location


class TestParseLocation:
    def test_coordinates_positive(self):
        result = parse_location("38.7,-9.14", 0, 0)
        assert result == {"lat": 38.7, "lng": -9.14, "name": "38.7,-9.14"}

    def test_coordinates_negative_both(self):
        result = parse_location("-33.87,151.21", 0, 0)
        assert result == {"lat": -33.87, "lng": 151.21, "name": "-33.87,151.21"}

    def test_coordinates_with_spaces(self):
        result = parse_location(" 38.7 , -9.14 ", 0, 0)
        assert result == {"lat": 38.7, "lng": -9.14, "name": "38.7,-9.14"}

    def test_place_name_geocoded(self):
        mock_result = {"lat": 38.72, "lng": -9.14, "display_name": "Cascais, Portugal"}
        with patch("velomate.geocode.geocode", return_value=mock_result):
            result = parse_location("Cascais", 38.7, -9.1)
        assert result == {"lat": 38.72, "lng": -9.14, "name": "Cascais"}

    def test_place_name_not_found(self):
        with patch("velomate.geocode.geocode", return_value=None):
            result = parse_location("Nonexistent Place", 0, 0)
        assert result is None

    def test_single_number_is_place_name(self):
        """A single number like '42' is not coordinates -- treat as place name."""
        with patch("velomate.geocode.geocode", return_value=None):
            result = parse_location("42", 0, 0)
        assert result is None

    def test_empty_string(self):
        result = parse_location("", 0, 0)
        assert result is None


class TestParseLocationEdgeCases:
    def test_three_comma_parts_is_place_name(self):
        with patch("velomate.geocode.geocode", return_value={"lat": 40.71, "lng": -74.01, "display_name": "New York"}):
            result = parse_location("New York, NY, USA", 0, 0)
        assert result["lat"] == 40.71

    def test_coordinate_with_high_precision(self):
        result = parse_location("38.7223456,-9.1398765", 0, 0)
        assert result["lat"] == pytest.approx(38.7223456)
        assert result["lng"] == pytest.approx(-9.1398765)

    def test_whitespace_only(self):
        result = parse_location("   ", 0, 0)
        assert result is None

    def test_coordinate_boundary_values(self):
        result = parse_location("90.0,180.0", 0, 0)
        assert result == {"lat": 90.0, "lng": 180.0, "name": "90.0,180.0"}

    def test_coordinate_negative_longitude(self):
        result = parse_location("40.71,-74.01", 0, 0)
        assert result["lng"] == -74.01

    def test_out_of_bounds_falls_through_to_geocode(self):
        """Coordinates outside valid range are not treated as coords."""
        with patch("velomate.geocode.geocode", return_value=None):
            result = parse_location("999,999", 0, 0)
        assert result is None

    def test_lat_out_of_range(self):
        with patch("velomate.geocode.geocode", return_value=None):
            result = parse_location("91.0,0.0", 0, 0)
        assert result is None
