"""What the planner actually sends to Valhalla, and what it does with the answer.

Every test stops at the HTTP boundary: Valhalla's POST is stubbed, and the
plan() tests stub each enrichment service too. The offline fixture fails any
test that reaches for the network anyway.
"""

import logging
import math
from unittest.mock import MagicMock, patch

import pytest
import requests

from velomate import route_generator
from velomate.route_generator import _loop_waypoints, generate
from velomate.route_intelligence import _haversine_km, corridor_waypoints
from velomate.route_planner import plan

# Real Valhalla responses, as returned by the public instance
NO_PATH = {"error_code": 442, "error": "No path could be found for input",
           "status_code": 400, "status": "Bad Request"}
PERIMETER_LIMIT = {"error_code": 167,
                   "error": "Exceeded maximum circumference for exclude_polygons: 10000 meters",
                   "status_code": 400, "status": "Bad Request"}

ZONE = {"lat": 38.75, "lng": -9.33, "radius": 500}


def valhalla_ok():
    resp = MagicMock()
    resp.json.return_value = {"trip": {"summary": {"length": 30.0}, "legs": [{"shape": "??"}]}}
    return resp


def valhalla_error(body):
    resp = MagicMock()
    resp.json.return_value = body
    resp.raise_for_status.side_effect = requests.HTTPError("400 Client Error: Bad Request", response=resp)
    return resp


def sent_payload(post):
    return post.call_args[1]["json"]


@pytest.fixture
def post():
    with patch("velomate.route_generator.requests.post", return_value=valhalla_ok()) as mock_post:
        yield mock_post


@pytest.fixture
def gpx(tmp_path):
    return str(tmp_path / "route.gpx")


def _configure(cfg, zones):
    cfg.return_value = {"avoid": zones, "display": {"units": "metric"}}


@pytest.fixture
def offline(post):
    """plan() with every external service stubbed. Yields the mocks tests steer."""
    stubs = {
        "config": patch("velomate.config.load"),
        "smart": patch("velomate.route_intelligence.smart_waypoints", return_value=[]),
        "forecast": patch("velomate.weather.fetch_forecast", return_value=[]),
    }
    silent = [
        patch("velomate.db.get_connection", return_value=None),
        patch("velomate.route_planner._get_strava_token", return_value=None),
        patch("velomate.weather.fetch_air_quality", return_value=None),
        patch("velomate.weather.fetch_sunrise_sunset", return_value=None),
        patch("velomate.route_intelligence.verify_surface", return_value={"surfaces": {}, "warning": None}),
        patch("velomate.route_intelligence.score_scenic", return_value={}),
        patch("velomate.route_intelligence.get_elevation_profile", return_value={}),
        patch("velomate.route_intelligence.find_cycling_trails", return_value=[]),
        patch("velomate.route_intelligence.score_cycling_safety", return_value={}),
        patch("velomate.road_safety.check_route", return_value={"hard": [], "penalty": []}),
        patch("velomate.map_preview.preview", return_value="/tmp/preview.html"),
    ]
    mocks = {name: stub.start() for name, stub in stubs.items()}
    for stub in silent:
        stub.start()
    network = patch("requests.sessions.Session.request", side_effect=AssertionError("network call"))
    reached = network.start()
    _configure(mocks["config"], [])
    mocks["post"] = post
    try:
        yield mocks
    finally:
        network.stop()
        patch.stopall()
    assert not reached.called, "a test reached the network"


# --- surface -> costing ---

class TestCosting:
    def test_mtb_is_bicycle_costing_with_mountain_type(self, post, gpx):
        generate(38.7, -9.14, 30, surface="mtb", output_path=gpx)

        payload = sent_payload(post)
        assert payload["costing"] == "bicycle"
        assert payload["costing_options"] == {"bicycle": {"bicycle_type": "Mountain", "use_roads": 0.5}}

    def test_mtb_generates_a_route(self, post, gpx):
        result = generate(38.7, -9.14, 30, surface="mtb", output_path=gpx)

        assert "error" not in result
        assert result["gpx_path"] == gpx

    @pytest.mark.parametrize("safety, use_roads", [(0.0, 1.0), (1.0, 0.0)])
    def test_safety_still_steers_mtb(self, post, gpx, safety, use_roads):
        generate(38.7, -9.14, 30, surface="mtb", safety=safety, output_path=gpx)

        assert sent_payload(post)["costing_options"]["bicycle"]["use_roads"] == use_roads

    @pytest.mark.parametrize("surface, bicycle_type", [("road", "Road"), ("gravel", "Cross")])
    def test_other_surfaces_are_unchanged(self, post, gpx, surface, bicycle_type):
        generate(38.7, -9.14, 30, surface=surface, output_path=gpx)

        payload = sent_payload(post)
        assert payload["costing"] == "bicycle"
        assert payload["costing_options"]["bicycle"]["bicycle_type"] == bicycle_type


# --- avoid zone geometry ---

class TestAvoidZoneRing:
    def test_ring_is_closed(self):
        ring = route_generator.avoid_zone_ring(ZONE)

        assert ring[0] == ring[-1]

    def test_vertices_sit_just_outside_the_circle(self):
        ring = route_generator.avoid_zone_ring(ZONE)

        for lat, lng in ring:
            metres = _haversine_km(ZONE["lat"], ZONE["lng"], lat, lng) * 1000
            assert 500 <= metres <= 525

    @pytest.mark.parametrize("zone_lat", [0.0, 38.75, 60.0])
    def test_ring_covers_the_whole_circle_at_any_latitude(self, zone_lat):
        """Walk the circle: just inside the radius is covered, well outside is not."""
        zone = {"lat": zone_lat, "lng": -9.33, "radius": 1000}
        ring = route_generator.avoid_zone_ring(zone)
        km_per_deg_lng = 111.19 * math.cos(math.radians(zone_lat))

        for degrees in range(0, 360, 5):
            north = math.cos(math.radians(degrees))
            east = math.sin(math.radians(degrees))
            for km, expected in ((0.995, True), (1.1, False)):
                lat = zone_lat + km * north / 111.19
                lng = zone["lng"] + km * east / km_per_deg_lng
                assert route_generator.in_rings(lat, lng, [ring]) is expected, (degrees, km)

    def test_radius_defaults_to_500_metres(self):
        unset = route_generator.avoid_zone_ring({"lat": 38.75, "lng": -9.33})

        assert unset == route_generator.avoid_zone_ring(ZONE)


class TestInRings:
    SQUARE = [(0.0, 0.0), (0.0, 1.0), (1.0, 1.0), (1.0, 0.0)]
    FAR_SQUARE = [(5.0, 5.0), (5.0, 6.0), (6.0, 6.0), (6.0, 5.0)]

    def test_inside_and_outside(self):
        assert route_generator.in_rings(0.5, 0.5, [self.SQUARE]) is True
        assert route_generator.in_rings(1.5, 0.5, [self.SQUARE]) is False
        assert route_generator.in_rings(0.5, 1.5, [self.SQUARE]) is False

    def test_outside_on_the_side_of_the_closing_edge(self):
        """SQUARE is an open ring: its last vertex joins the first only implicitly."""
        assert route_generator.in_rings(0.5, -0.5, [self.SQUARE]) is False

    def test_any_ring_counts(self):
        assert route_generator.in_rings(5.5, 5.5, [self.SQUARE, self.FAR_SQUARE]) is True

    def test_open_and_closed_rings_agree(self):
        closed = self.SQUARE + self.SQUARE[:1]

        assert route_generator.in_rings(0.5, 0.5, [closed]) is True
        assert route_generator.in_rings(1.5, 0.5, [closed]) is False

    def test_no_rings(self):
        assert route_generator.in_rings(0.5, 0.5, []) is False


# --- exclusions in generate() ---

class TestGenerateExclusions:
    def test_polygon_reaches_the_payload_in_lon_lat_order(self, post, gpx):
        ring = [(38.75, -9.33), (38.76, -9.33), (38.76, -9.32), (38.75, -9.33)]

        generate(38.7, -9.14, 30, output_path=gpx, exclude_polygons=[ring])

        assert sent_payload(post)["exclude_polygons"] == [
            [[-9.33, 38.75], [-9.33, 38.76], [-9.32, 38.76], [-9.33, 38.75]],
        ]

    @pytest.mark.parametrize("no_zones", [None, []])
    def test_no_zones_sends_no_exclusion_key(self, post, gpx, no_zones):
        generate(38.7, -9.14, 30, output_path=gpx, exclude_polygons=no_zones)

        assert "exclude_polygons" not in sent_payload(post)

    def test_generated_loop_point_inside_a_zone_is_dropped(self, post, gpx):
        points = _loop_waypoints(38.7, -9.14, 30)
        blocked = points[1]
        ring = route_generator.avoid_zone_ring({"lat": blocked["lat"], "lng": blocked["lon"], "radius": 300})

        generate(38.7, -9.14, 30, output_path=gpx, exclude_polygons=[ring])

        start = {"lat": 38.7, "lon": -9.14}
        assert sent_payload(post)["locations"] == [start] + points[:1] + points[2:] + [start]

    def test_no_path_with_zones_suggests_the_zones_and_keeps_the_cause(self, post, gpx):
        """Valhalla says this for disconnected waypoints too, so the message must
        not push the rider to weaken a zone that had nothing to do with it."""
        post.return_value = valhalla_error(NO_PATH)

        result = generate(38.7, -9.14, 30, output_path=gpx,
                          exclude_polygons=[route_generator.avoid_zone_ring(ZONE)])

        assert "may be blocking" in result["error"]
        assert NO_PATH["error"] in result["error"]
        assert "gpx_path" not in result

    def test_no_path_without_zones_does_not_blame_zones(self, post, gpx):
        post.return_value = valhalla_error(NO_PATH)

        result = generate(38.7, -9.14, 30, output_path=gpx)

        assert result["error"].startswith("Routing failed:")
        assert "avoid" not in result["error"]

    def test_perimeter_limit_says_what_to_change(self, post, gpx):
        post.return_value = valhalla_error(PERIMETER_LIMIT)

        result = generate(38.7, -9.14, 30, output_path=gpx,
                          exclude_polygons=[route_generator.avoid_zone_ring(ZONE)])

        assert "10000 meters" in result["error"]
        assert "avoid zones" in result["error"]

    def test_other_rejections_stay_generic(self, post, gpx):
        post.return_value = valhalla_error({"error_code": 154, "error": "Path distance exceeds the max"})

        result = generate(38.7, -9.14, 30, output_path=gpx,
                          exclude_polygons=[route_generator.avoid_zone_ring(ZONE)])

        assert result["error"].startswith("Routing failed:")

    def test_non_json_error_body_stays_generic(self, post, gpx):
        resp = valhalla_error({})
        resp.json.side_effect = ValueError("not json")
        post.return_value = resp

        result = generate(38.7, -9.14, 30, output_path=gpx,
                          exclude_polygons=[route_generator.avoid_zone_ring(ZONE)])

        assert result["error"].startswith("Routing failed:")


# --- avoid zones through plan() ---

def intermediate(post):
    """The via points of the route that was sent, without its start and end."""
    return sent_payload(post)["locations"][1:-1]


class TestZonesInReach:
    """About 144 km north of the start, so no 30 km route gets near it."""
    FAR = {"lat": 40.0, "lng": -9.14, "radius": 500}

    def test_a_zone_no_route_can_reach_is_not_sent(self, offline):
        """Four of them pass the server's perimeter limit and would fail every
        plan, for a zone that has no effect on this route."""
        far = [dict(self.FAR, lng=self.FAR["lng"] + i / 10) for i in range(4)]
        _configure(offline["config"], [ZONE] + far)

        plan(distance_str="30km", home_lat=38.7, home_lng=-9.14)

        assert len(sent_payload(offline["post"])["exclude_polygons"]) == 1

    def test_a_zone_near_the_destination_is_sent(self, offline):
        _configure(offline["config"], [self.FAR])
        destination = {"lat": 39.9, "lng": -9.14, "name": "North"}

        plan(distance_str="30km", destination=destination, home_lat=38.7, home_lng=-9.14)

        assert len(sent_payload(offline["post"])["exclude_polygons"]) == 1

    def test_a_zone_near_a_user_waypoint_is_sent(self, offline):
        """The waypoint takes the route far past the distance asked for."""
        _configure(offline["config"], [self.FAR])
        place = {"lat": 39.9, "lng": -9.14, "name": "North"}

        with patch("velomate.geocode.parse_location", return_value=place):
            plan(distance_str="30km", waypoints_str="North", home_lat=38.7, home_lng=-9.14)

        assert len(sent_payload(offline["post"])["exclude_polygons"]) == 1

    def test_a_zone_midway_along_a_long_leg_is_sent(self, offline):
        """About 67 km from both ends, past the 30 km asked for, and right on the
        road between them. This one must not be left out."""
        _configure(offline["config"], [dict(self.FAR, lat=39.3)])
        place = {"lat": 39.9, "lng": -9.14, "name": "North"}

        with patch("velomate.geocode.parse_location", return_value=place):
            plan(distance_str="30km", waypoints_str="North", home_lat=38.7, home_lng=-9.14)

        assert len(sent_payload(offline["post"])["exclude_polygons"]) == 1

    def test_a_zone_midway_to_the_destination_is_sent(self, offline):
        _configure(offline["config"], [dict(self.FAR, lat=39.3)])
        destination = {"lat": 39.9, "lng": -9.14, "name": "North"}

        plan(distance_str="30km", destination=destination, home_lat=38.7, home_lng=-9.14)

        assert len(sent_payload(offline["post"])["exclude_polygons"]) == 1

    def test_a_route_through_a_left_out_zone_is_planned_again_with_every_zone(self, offline):
        """Whatever the reach estimate got wrong, the returned route is what counts."""
        _configure(offline["config"], [self.FAR])
        through = {"gpx_path": "/tmp/r.gpx", "actual_km": 30.0, "elevation_gain_m": 0,
                   "coords": [(38.7, -9.14), (self.FAR["lat"], self.FAR["lng"])]}
        clear = dict(through, coords=[(38.7, -9.14), (38.8, -9.14)])

        with patch("velomate.route_generator.generate", side_effect=[through, clear]) as gen:
            plan(distance_str="30km", home_lat=38.7, home_lng=-9.14)

        sent = [call.kwargs["exclude_polygons"] for call in gen.call_args_list]
        assert [len(rings) for rings in sent] == [0, 1]

    def test_a_clean_route_is_planned_once(self, offline):
        _configure(offline["config"], [self.FAR])

        plan(distance_str="30km", home_lat=38.7, home_lng=-9.14)

        assert offline["post"].call_count == 1

    def test_a_zone_just_inside_the_routes_length_is_sent(self, offline):
        """About 29.7 km out, twice what a 30 km loop can reach. The margin is on
        purpose: a real route can overrun the distance asked for."""
        _configure(offline["config"], [dict(self.FAR, lat=38.7 + 29.7 / 111)])

        plan(distance_str="30km", home_lat=38.7, home_lng=-9.14)

        assert len(sent_payload(offline["post"])["exclude_polygons"]) == 1


class TestPlanAvoidZones:
    def test_configured_zone_reaches_the_payload(self, offline):
        _configure(offline["config"], [ZONE])

        plan(distance_str="30km", home_lat=38.7, home_lng=-9.14)

        rings = sent_payload(offline["post"])["exclude_polygons"]
        assert len(rings) == 1
        for lon, lat in rings[0]:
            assert 500 <= _haversine_km(ZONE["lat"], ZONE["lng"], lat, lon) * 1000 <= 525

    def test_no_zones_configured_sends_no_exclusion(self, offline):
        plan(distance_str="30km", home_lat=38.7, home_lng=-9.14)

        assert "exclude_polygons" not in sent_payload(offline["post"])

    def test_user_waypoint_inside_a_zone_is_dropped(self, offline):
        _configure(offline["config"], [ZONE])
        places = {"Inside": {"lat": ZONE["lat"], "lng": ZONE["lng"], "name": "Inside"},
                  "Outside": {"lat": 38.72, "lng": -9.20, "name": "Outside"}}

        with patch("velomate.geocode.parse_location", side_effect=lambda p, *a: places[p]):
            result = plan(distance_str="30km", waypoints_str="Inside;Outside", home_lat=38.7, home_lng=-9.14)

        assert intermediate(offline["post"]) == [{"lat": 38.72, "lon": -9.20}]
        assert "Outside" in result
        assert "Inside" not in result

    def test_smart_waypoint_inside_a_zone_is_dropped(self, offline):
        _configure(offline["config"], [ZONE])
        offline["smart"].return_value = [
            {"lat": ZONE["lat"], "lng": ZONE["lng"], "name": "Inside", "reason": "poi"},
            {"lat": 38.72, "lng": -9.20, "name": "Outside", "reason": "poi"},
        ]

        plan(distance_str="30km", home_lat=38.7, home_lng=-9.14)

        assert intermediate(offline["post"]) == [{"lat": 38.72, "lon": -9.20}]

    def test_corridor_waypoint_inside_a_zone_is_dropped(self, offline):
        home = {"lat": 38.0, "lng": -9.0}
        dest = {"lat": 39.0, "lng": -9.0, "name": "Dest"}
        straight_km = 111.0
        padding = corridor_waypoints(home["lat"], home["lng"], dest["lat"], dest["lng"],
                                     target_km=200.0, baseline_km=straight_km * 1.3)
        assert len(padding) == 3
        blocked = padding[1]
        _configure(offline["config"], [{"lat": blocked["lat"], "lng": blocked["lng"], "radius": 500}])

        plan(distance_str="200km", home_lat=home["lat"], home_lng=home["lng"], destination=dest, loop=False)

        assert intermediate(offline["post"]) == [
            {"lat": padding[0]["lat"], "lon": padding[0]["lng"]},
            {"lat": padding[2]["lat"], "lon": padding[2]["lng"]},
        ]

    def test_start_inside_a_zone_fails_clearly_without_routing(self, offline):
        _configure(offline["config"], [ZONE])

        result = plan(distance_str="30km", home_lat=ZONE["lat"], home_lng=ZONE["lng"])

        assert result.startswith("Error:")
        assert "avoid zone" in result
        assert "start" in result.lower()
        offline["post"].assert_not_called()

    def test_destination_inside_a_zone_fails_clearly_without_routing(self, offline):
        _configure(offline["config"], [ZONE])

        result = plan(home_lat=38.7, home_lng=-9.14,
                      destination={"lat": ZONE["lat"], "lng": ZONE["lng"], "name": "Cascais"})

        assert result.startswith("Error:")
        assert "avoid zone" in result
        assert "Cascais" in result
        offline["post"].assert_not_called()

    def test_no_route_past_the_zones_fails_clearly(self, offline):
        _configure(offline["config"], [ZONE])
        offline["post"].return_value = valhalla_error(NO_PATH)

        result = plan(distance_str="30km", home_lat=38.7, home_lng=-9.14)

        assert result.startswith("Route generation failed:")
        assert "avoid zones" in result
        assert "GPX:" not in result


# --- destination plans ---

class TestDestinationPlans:
    # Rounding the estimate up used to leave a few metres of "padding"
    @pytest.mark.parametrize("dest_lat", [38.69, 38.60, 38.55, 38.31, 38.123])
    def test_destination_only_routes_directly(self, offline, dest_lat):
        offline["smart"].return_value = [{"lat": 38.72, "lng": -9.20, "name": "Poi", "reason": "poi"}]

        result = plan(home_lat=38.7, home_lng=-9.14,
                      destination={"lat": dest_lat, "lng": -9.42, "name": "Cascais"})

        assert sent_payload(offline["post"])["locations"] == [
            {"lat": 38.7, "lon": -9.14}, {"lat": dest_lat, "lon": -9.42},
        ]
        assert "Poi" not in result

    def test_target_below_the_direct_distance_routes_directly(self, offline):
        offline["smart"].return_value = [{"lat": 38.72, "lng": -9.20, "name": "Poi", "reason": "poi"}]

        plan(distance_str="10km", home_lat=38.7, home_lng=-9.14,
             destination={"lat": 38.69, "lng": -9.42, "name": "Cascais"}, loop=False)

        assert len(sent_payload(offline["post"])["locations"]) == 2

    def test_padding_is_still_added_when_asked_for(self, offline):
        plan(distance_str="200km", home_lat=38.0, home_lng=-9.0,
             destination={"lat": 39.0, "lng": -9.0, "name": "Dest"}, loop=False)

        assert len(intermediate(offline["post"])) == 3

    def test_no_padding_warning_without_a_target(self, offline, caplog):
        with caplog.at_level(logging.WARNING):
            plan(home_lat=38.7, home_lng=-9.14,
                 destination={"lat": 38.69, "lng": -9.42, "name": "Cascais"}, loop=False)

        assert not any("routing directly" in r.message.lower() for r in caplog.records)

    def test_no_ignored_target_warning_without_a_target(self, offline, caplog):
        place = {"lat": 38.70, "lng": -9.30, "name": "Oeiras"}

        with caplog.at_level(logging.WARNING), patch("velomate.geocode.parse_location", return_value=place):
            plan(home_lat=38.7, home_lng=-9.14, waypoints_str="Oeiras", loop=False,
                 destination={"lat": 38.69, "lng": -9.42, "name": "Cascais"})

        assert not any("ignoring" in r.message.lower() for r in caplog.records)

    def test_round_trip_counts_the_return_leg_as_distance(self, offline):
        """~144 km there, ~289 km both ways: a 200 km target is already exceeded."""
        plan(distance_str="200km", home_lat=38.0, home_lng=-9.0,
             destination={"lat": 39.0, "lng": -9.0, "name": "Dest"}, loop=True)

        locations = sent_payload(offline["post"])["locations"]
        assert locations == [{"lat": 38.0, "lon": -9.0}, {"lat": 39.0, "lon": -9.0}, {"lat": 38.0, "lon": -9.0}]

    def test_round_trip_pads_only_what_is_missing(self, offline):
        real = corridor_waypoints
        with patch("velomate.route_intelligence.corridor_waypoints", side_effect=real) as corridor:
            plan(distance_str="400km", home_lat=38.0, home_lng=-9.0,
                 destination={"lat": 39.0, "lng": -9.0, "name": "Dest"}, loop=True)

        assert corridor.call_args[1]["baseline_km"] == pytest.approx(2 * 111 * 1.3, abs=0.1)
        assert len(sent_payload(offline["post"])["locations"]) == 6


# --- wind analysis ---

def forecast_day(date, hourly):
    return {"date": date, "weather": "Clear", "temp_min": 10, "temp_max": 20, "wind": 20,
            "gusts": 0, "uv_max": 3, "precip": 0, "hourly": hourly}


def hour(date, clock, wind, wind_dir):
    return {"time": f"{date}T{clock}", "temp": 18, "wind": wind, "gusts": wind,
            "wind_dir": wind_dir, "uv": 1, "precip": 0}


class TestWindAtRideTime:
    """A route riding north. Calm air is the best-scored hour; a strong northerly is a headwind."""

    DATE = "2026-11-10"
    CALM_MORNING = hour(DATE, "09:00", 5, 0)
    WINDY_AFTERNOON = hour(DATE, "15:00", 35, 0)

    def _plan_north(self, offline, hourly, **kwargs):
        offline["forecast"].return_value = [forecast_day(self.DATE, hourly)]
        coords = [(38.0 + i * 0.01, -9.0) for i in range(60)]
        with patch("velomate.route_generator.generate", return_value={
            "gpx_path": "/tmp/r.gpx", "actual_km": 30.0, "name": "r", "coords": coords,
        }):
            return plan(distance_str="30km", date_str=self.DATE, home_lat=38.0, home_lng=-9.0, **kwargs)

    def test_wind_is_read_at_the_requested_time(self, offline):
        result = self._plan_north(offline, [self.CALM_MORNING, self.WINDY_AFTERNOON], time_str="15:00")

        assert "headwind" in result

    def test_without_a_time_the_calm_best_hour_gives_no_warning(self, offline):
        result = self._plan_north(offline, [self.CALM_MORNING, self.WINDY_AFTERNOON])

        assert "headwind" not in result

    def test_without_a_time_a_windy_best_hour_still_warns(self, offline):
        result = self._plan_north(offline, [self.WINDY_AFTERNOON])

        assert "headwind" in result

    def test_a_time_outside_the_forecast_falls_back_to_the_best_hour(self, offline):
        result = self._plan_north(offline, [self.WINDY_AFTERNOON], time_str="23:00")

        assert "headwind" in result
