"""Tests for pure functions in velomate/route_intelligence.py."""

import math
import pytest

from velomate.route_intelligence import _haversine_km, _density_at


# --- _haversine_km ---

class TestHaversineKm:
    def test_same_point(self):
        assert _haversine_km(38.7, -9.14, 38.7, -9.14) == 0.0

    def test_known_distance_lisbon_to_sintra(self):
        """Lisbon (38.7223, -9.1393) to Sintra (38.7980, -9.3880) ~25km."""
        result = _haversine_km(38.7223, -9.1393, 38.7980, -9.3880)
        assert result == pytest.approx(22.0, abs=3.0)

    def test_purely_north_south(self):
        """1 degree latitude ≈ 111 km."""
        result = _haversine_km(38.0, -9.0, 39.0, -9.0)
        assert result == pytest.approx(111.0, abs=1.0)

    def test_purely_east_west_at_equator(self):
        """1 degree longitude at equator ≈ 111 km."""
        result = _haversine_km(0.0, 0.0, 0.0, 1.0)
        assert result == pytest.approx(111.0, abs=1.0)

    def test_purely_east_west_at_lisbon_latitude(self):
        """1 degree longitude at ~38.7°N ≈ 86.7 km (111 * cos(38.7°))."""
        expected = 111.0 * math.cos(math.radians(38.7))
        result = _haversine_km(38.7, 0.0, 38.7, 1.0)
        assert result == pytest.approx(expected, abs=1.0)

    def test_symmetry(self):
        d1 = _haversine_km(38.7, -9.14, 39.0, -9.0)
        d2 = _haversine_km(39.0, -9.0, 38.7, -9.14)
        assert d1 == pytest.approx(d2, abs=0.001)

    def test_short_distance(self):
        """Two points ~100m apart."""
        result = _haversine_km(38.7000, -9.1400, 38.7009, -9.1400)
        assert result == pytest.approx(0.1, abs=0.02)


# --- _density_at ---

class TestDensityAt:
    def test_empty_grid(self):
        assert _density_at({}, 38.7, -9.14) == 0.0

    def test_exact_grid_hit(self):
        """Point at grid center with 5 visits -> 0.5 score."""
        grid_size = 0.005
        key = (round(38.7 / grid_size) * grid_size, round(-9.14 / grid_size) * grid_size)
        density = {key: 5}
        assert _density_at(density, 38.7, -9.14) == pytest.approx(0.5)

    def test_max_density_capped_at_1(self):
        grid_size = 0.005
        key = (round(38.7 / grid_size) * grid_size, round(-9.14 / grid_size) * grid_size)
        density = {key: 50}
        assert _density_at(density, 38.7, -9.14) == 1.0

    def test_ten_visits_is_max(self):
        grid_size = 0.005
        key = (round(38.7 / grid_size) * grid_size, round(-9.14 / grid_size) * grid_size)
        density = {key: 10}
        assert _density_at(density, 38.7, -9.14) == 1.0

    def test_one_visit(self):
        grid_size = 0.005
        key = (round(38.7 / grid_size) * grid_size, round(-9.14 / grid_size) * grid_size)
        density = {key: 1}
        assert _density_at(density, 38.7, -9.14) == pytest.approx(0.1)

    def test_miss_returns_zero(self):
        """Point not in grid returns 0."""
        density = {(40.0, -8.0): 10}
        assert _density_at(density, 38.7, -9.14) == 0.0
