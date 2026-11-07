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


# --- corridor_waypoints ---

from velomate.route_intelligence import corridor_waypoints


class TestCorridorWaypoints:
    def test_returns_list(self):
        result = corridor_waypoints(
            start_lat=38.7, start_lng=-9.14,
            dest_lat=38.69, dest_lng=-9.42,
            target_km=50, baseline_km=30,
        )
        assert isinstance(result, list)

    def test_waypoints_between_start_and_dest(self):
        result = corridor_waypoints(
            start_lat=38.7, start_lng=-9.14,
            dest_lat=38.69, dest_lng=-9.42,
            target_km=50, baseline_km=30,
        )
        if result:
            mid_lat = (38.7 + 38.69) / 2
            mid_lng = (-9.14 + -9.42) / 2
            for wp in result:
                dist_lat = abs(wp["lat"] - mid_lat)
                dist_lng = abs(wp["lng"] - mid_lng)
                assert dist_lat < 0.5
                assert dist_lng < 0.5

    def test_no_padding_when_baseline_exceeds_target(self):
        result = corridor_waypoints(
            start_lat=38.7, start_lng=-9.14,
            dest_lat=38.69, dest_lng=-9.42,
            target_km=30, baseline_km=35,
        )
        assert result == []

    def test_returns_waypoints_with_required_keys(self):
        result = corridor_waypoints(
            start_lat=38.7, start_lng=-9.14,
            dest_lat=38.69, dest_lng=-9.42,
            target_km=60, baseline_km=30,
        )
        for wp in result:
            assert "lat" in wp
            assert "lng" in wp


def _offsets_km(start, dest, wp):
    """(along, sideways) km of a waypoint from start, relative to the start->dest line."""
    mid_lat = (start[0] + dest[0]) / 2
    km_lng = 111 * math.cos(math.radians(mid_lat))
    route_y, route_x = (dest[0] - start[0]) * 111, (dest[1] - start[1]) * km_lng
    length = math.hypot(route_x, route_y)
    wp_y, wp_x = (wp["lat"] - start[0]) * 111, (wp["lng"] - start[1]) * km_lng
    along = (wp_x * route_x + wp_y * route_y) / length
    sideways = (wp_x * route_y - wp_y * route_x) / length
    return along, sideways


class TestCorridorSidewaysOffset:
    """Padding only adds distance if the waypoints leave the direct line."""

    TRIPS = {
        "north": ((38.0, -9.0), (39.0, -9.0)),
        "south": ((39.0, -9.0), (38.0, -9.0)),
        "east": ((38.5, -9.0), (38.5, -7.7)),
        "west": ((38.5, -7.7), (38.5, -9.0)),
        "north-east at 60N": ((60.0, 10.0), (60.7, 12.0)),
    }
    TARGET_KM, BASELINE_KM = 200.0, 144.0
    # (200 - 144) / (2 * 3) * 1.2
    INTENDED_KM = 11.2

    @pytest.mark.parametrize("start, dest", TRIPS.values(), ids=TRIPS.keys())
    def test_each_waypoint_sits_the_intended_km_off_the_line(self, start, dest):
        waypoints = corridor_waypoints(*start, *dest, target_km=self.TARGET_KM, baseline_km=self.BASELINE_KM)

        assert len(waypoints) == 3
        for wp in waypoints:
            _, sideways = _offsets_km(start, dest, wp)
            assert abs(sideways) == pytest.approx(self.INTENDED_KM, abs=0.01)

    @pytest.mark.parametrize("start, dest", TRIPS.values(), ids=TRIPS.keys())
    def test_waypoints_stay_evenly_spaced_along_the_line(self, start, dest):
        waypoints = corridor_waypoints(*start, *dest, target_km=self.TARGET_KM, baseline_km=self.BASELINE_KM)
        length, _ = _offsets_km(start, dest, {"lat": dest[0], "lng": dest[1]})

        for i, wp in enumerate(waypoints, start=1):
            along, _ = _offsets_km(start, dest, wp)
            assert along == pytest.approx(length * i / 4, abs=0.01)

    @pytest.mark.parametrize("start, dest", TRIPS.values(), ids=TRIPS.keys())
    def test_waypoints_alternate_sides(self, start, dest):
        waypoints = corridor_waypoints(*start, *dest, target_km=self.TARGET_KM, baseline_km=self.BASELINE_KM)

        sides = [math.copysign(1, _offsets_km(start, dest, wp)[1]) for wp in waypoints]
        assert sides[0] == -sides[1] == sides[2]


class TestCorridorWaypointsEdgeCases:
    def test_very_close_start_and_dest(self):
        result = corridor_waypoints(
            start_lat=38.700, start_lng=-9.140,
            dest_lat=38.701, dest_lng=-9.141,
            target_km=10, baseline_km=1,
        )
        assert result == []

    def test_baseline_equals_target(self):
        result = corridor_waypoints(
            start_lat=38.7, start_lng=-9.14,
            dest_lat=38.69, dest_lng=-9.42,
            target_km=30, baseline_km=30,
        )
        assert result == []

    def test_max_waypoints_respected(self):
        result = corridor_waypoints(
            start_lat=38.7, start_lng=-9.14,
            dest_lat=38.69, dest_lng=-9.42,
            target_km=80, baseline_km=30,
            max_waypoints=2,
        )
        assert len(result) == 2

    def test_waypoints_ordered_along_corridor(self):
        result = corridor_waypoints(
            start_lat=38.7, start_lng=-9.14,
            dest_lat=37.0, dest_lng=-9.14,
            target_km=250, baseline_km=180,
            max_waypoints=3,
        )
        lats = [wp["lat"] for wp in result]
        assert lats[0] > lats[-1]
