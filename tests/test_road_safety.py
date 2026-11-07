"""The road safety check: route first, then ask the router what the route rode on.

Every test stops at requests.post, so payloads, polyline decoding, classification
and the retry all run for real. The fake server judges each shape it is sent,
so a retried route is checked on where it went, not on a canned answer.
"""

import math
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import requests

from velomate import road_safety
from velomate.map_preview import preview
from velomate.road_safety import RoadSafetyUnavailable, check_route, exclusion_polygons, perimeter_m
from velomate.route_generator import VALHALLA_TRACE_URL, VALHALLA_URL, avoid_zone_ring, in_rings
from velomate.route_intelligence import _haversine_km
from velomate.route_planner import MAX_ROAD_RETRIES, plan

# Real Valhalla refusals, as returned by the public instance
NO_PATH = {"error_code": 442, "error": "No path could be found for input",
           "status_code": 400, "status": "Bad Request"}
PERIMETER_LIMIT = {"error_code": 167,
                   "error": "Exceeded maximum circumference for exclude_polygons: 10000 meters",
                   "status_code": 400, "status": "Bad Request"}

# An east-west road about 8.7 km long, and routes on and off it
LAT = 38.80
ON_TRUNK = [(38.79, -9.38), (LAT, -9.38), (LAT, -9.34), (38.79, -9.34)]
OFF_TRUNK = [(38.79, -9.38), (38.79, -9.34)]
# A second road two kilometres north, with a route of its own
NORTH = 38.82
ON_SECOND = [(38.79, -9.38), (NORTH, -9.38), (NORTH, -9.34), (38.79, -9.34)]
TRUNK = {"road_class": "trunk", "names": ["IC 30"]}
SECOND_TRUNK = {"road_class": "trunk", "names": ["A 16"]}
BUSY = {"road_class": "primary", "names": ["N 9"], "speed_limit": 90}
GPX = Path("/tmp/velomate_route_road_30km.gpx")


def east_of(lng, metres):
    return lng + metres / (111000 * math.cos(math.radians(LAT)))


def encode_polyline6(coords):
    def signed(value):
        value = ~(value << 1) if value < 0 else value << 1
        out = ""
        while value >= 0x20:
            out += chr((0x20 | (value & 0x1f)) + 63)
            value >>= 5
        return out + chr(value + 63)

    out, last_lat, last_lng = "", 0, 0
    for lat, lng in coords:
        lat, lng = round(lat * 1e6), round(lng * 1e6)
        out += signed(lat - last_lat) + signed(lng - last_lng)
        last_lat, last_lng = lat, lng
    return out


def ok(body):
    resp = MagicMock(status_code=200)
    resp.json.return_value = body
    return resp


def refused(body, status=400):
    resp = ok(body)
    resp.status_code = status
    resp.raise_for_status.side_effect = requests.HTTPError(f"{status} Error", response=resp)
    return resp


def same_points(actual, expected):
    return len(actual) == len(expected) and all(
        tuple(a) == pytest.approx(tuple(e), abs=1e-6) for a, e in zip(actual, expected))


def line_of(stretch):
    """A stretch's edges joined back into one line."""
    line = list(stretch["edges"][0])
    for edge in stretch["edges"][1:]:
        line += edge[1:]
    return line


def matched(shape, *edges):
    """A trace answer: the shape it matched, and (begin, end, attributes) edges."""
    return ok({"shape": encode_polyline6(shape), "edges": [
        {"length": sum(_haversine_km(*a, *b) for a, b in zip(shape[begin:end], shape[begin + 1:end + 1])),
         "begin_shape_index": begin, "end_shape_index": end, "road_class": "residential", **attrs}
        for begin, end, attrs in edges]})


class Network:
    """Valhalla's two endpoints as requests.post sees them.

    routes holds /route's answers in order, a list of points or a refusal body, and
    the last one repeats. A response or an exception is returned or raised as it is.
    roads maps a latitude to the road running along it, so any
    segment of a traced shape at that latitude rides it. trace_answer replaces the
    trace with a response to return or an exception to raise.
    """

    def __init__(self):
        self.routes = [OFF_TRUNK]
        self.roads = {}
        self.trace_answer = None
        self.sent = []
        self.traced = []

    def post(self, url, **kwargs):
        if url == VALHALLA_TRACE_URL:
            return self.trace(kwargs["json"])
        assert url == VALHALLA_URL
        self.sent.append(kwargs["json"])
        answer = self.routes[min(len(self.sent), len(self.routes)) - 1]
        if isinstance(answer, Exception):
            raise answer
        if isinstance(answer, MagicMock):
            return answer
        if isinstance(answer, dict):
            return refused(answer)
        length = sum(_haversine_km(*a, *b) for a, b in zip(answer, answer[1:]))
        return ok({"trip": {"summary": {"length": length}, "legs": [{"shape": encode_polyline6(answer)}]}})

    def trace(self, payload):
        self.traced.append(payload)
        if isinstance(self.trace_answer, Exception):
            raise self.trace_answer
        if self.trace_answer is not None:
            return self.trace_answer
        shape = [(point["lat"], point["lon"]) for point in payload["shape"]]
        return matched(shape, *((i, i + 1, self.roads.get(a[0], {}) if a[0] == b[0] else {})
                                for i, (a, b) in enumerate(zip(shape, shape[1:]))))


@pytest.fixture
def network():
    fake = Network()
    with patch("requests.post", side_effect=fake.post):
        yield fake


@pytest.fixture
def planning(network):
    """plan() with everything but Valhalla stubbed."""
    network.config = {"avoid": [], "display": {"units": "metric"}}
    network.preview = MagicMock(return_value="/tmp/preview.html")
    stubs = [
        patch("velomate.config.load", return_value=network.config),
        patch("velomate.map_preview.preview", network.preview),
        patch("velomate.route_intelligence.smart_waypoints", return_value=[]),
        patch("velomate.weather.fetch_forecast", return_value=[]),
        patch("velomate.db.get_connection", return_value=None),
        patch("velomate.route_planner._get_strava_token", return_value=None),
        patch("velomate.weather.fetch_air_quality", return_value=None),
        patch("velomate.weather.fetch_sunrise_sunset", return_value=None),
        patch("velomate.route_intelligence.verify_surface", return_value={"surfaces": {}, "warning": None}),
        patch("velomate.route_intelligence.score_scenic", return_value={}),
        patch("velomate.route_intelligence.get_elevation_profile", return_value={}),
        patch("velomate.route_intelligence.find_cycling_trails", return_value=[]),
        patch("velomate.route_intelligence.score_cycling_safety", return_value={}),
    ]
    for stub in stubs:
        stub.start()
    yield network
    patch.stopall()


def run_plan(network, roads=None, routes=None, **kwargs):
    network.roads = roads or {}
    network.routes = routes or network.routes
    return plan(distance_str="30km", home_lat=38.79, home_lng=-9.38, **kwargs)


def rings_sent(payload):
    """The polygons of a route request, back in (lat, lng) order."""
    return [[(lat, lng) for lng, lat in ring] for ring in payload.get("exclude_polygons", [])]


# --- which roads count ---

class TestClassify:
    @pytest.mark.parametrize("edge, why", [
        ({"road_class": "motorway"}, "motorway"),
        ({"road_class": "trunk"}, "trunk"),
        ({"road_class": "motorway", "use": "ramp"}, "motorway link"),
        ({"road_class": "trunk", "use": "ramp"}, "trunk link"),
        ({"road_class": "trunk", "speed_limit": 100}, "trunk"),
    ])
    def test_hard_by_class(self, edge, why):
        assert road_safety._classify(edge) == ("hard", why)

    @pytest.mark.parametrize("names", [["IC 30"], ["IC30"], ["IP 7"], ["IP7"], ["EN 9", "IC 30"], ["EN 9;IC 30"],
                                       ["EN 9; IC 30"], ["IC 2-1"]])
    def test_an_ic_or_ip_route_is_hard_whatever_its_class(self, names):
        """The IC 30 north of Sintra is a primary road, signed with the EN 9."""
        assert road_safety._classify({"road_class": "primary", "names": names}) == ("hard", "IC/IP route")

    @pytest.mark.parametrize("edge, why", [
        ({"road_class": "primary"}, "primary road"),
        ({"road_class": "primary", "speed_limit": 50}, "primary road"),
        ({"road_class": "tertiary", "speed_limit": 90}, "90 km/h"),
        ({"road_class": "secondary", "speed_limit": 120}, "120 km/h"),
        ({"road_class": "secondary", "speed_limit": "unlimited"}, "no speed limit"),
    ])
    def test_penalty(self, edge, why):
        assert road_safety._classify(edge) == ("penalty", why)

    @pytest.mark.parametrize("edge", [
        {"road_class": "secondary"}, {"road_class": "secondary", "speed_limit": 89},
        {"road_class": "residential", "names": None}, {},
        {"road_class": "secondary", "names": ["EN 9"]}, {"road_class": "secondary", "names": ["VIC 3"]},
        {"road_class": "secondary", "names": ["ICE 4"]}, {"road_class": "secondary", "names": ["IPA 2"]},
    ])
    def test_neither(self, edge):
        assert road_safety._classify(edge) is None

    @pytest.mark.parametrize("names, label", [(["EN 9", "IC 30"], "EN 9 / IC 30 (why)"), (None, "unnamed road (why)")])
    def test_a_road_is_named_by_everything_the_router_calls_it(self, names, label):
        assert road_safety._label({"names": names}, "why") == label


# --- what the route rode on ---

class TestCheckRoute:
    def test_a_route_on_a_trunk_is_caught_on_that_stretch_only(self, network):
        network.roads = {LAT: TRUNK}

        stretch, = check_route(ON_TRUNK)["hard"]

        assert stretch["roads"] == ["IC 30 (trunk)"]
        assert stretch["km"] == pytest.approx(3.47, abs=0.05)
        assert same_points(line_of(stretch), ON_TRUNK[1:3])

    def test_a_clean_route(self, network):
        network.roads = {LAT: TRUNK}
        assert check_route(OFF_TRUNK) == {"hard": [], "penalty": []}

    def test_the_lookup_goes_to_the_routing_server(self):
        assert VALHALLA_TRACE_URL == "https://valhalla1.openstreetmap.de/trace_attributes"

    def test_the_request_asks_only_for_what_is_used(self, network):
        check_route(OFF_TRUNK)

        payload = network.traced[0]
        assert payload["costing"] == "bicycle"
        assert payload["shape_match"] == "walk_or_snap"
        assert payload["filters"] == {"action": "include", "attributes": [
            "edge.road_class", "edge.use", "edge.names", "edge.speed_limit", "edge.length",
            "edge.begin_shape_index", "edge.end_shape_index", "shape"]}

    @pytest.mark.parametrize("surface, bicycle_type", [("road", "Road"), ("gravel", "Cross"), ("mtb", "Mountain")])
    def test_the_route_is_read_as_the_bicycle_it_was_planned_for(self, network, surface, bicycle_type):
        """A path open to a mountain bike is closed to a hybrid, which would push the
        lookup off the exact walk and onto another road."""
        check_route(OFF_TRUNK, surface)
        assert network.traced[0]["costing_options"] == {"bicycle": {"bicycle_type": bicycle_type}}

    def test_a_vertex_repeated_where_legs_join_is_not_sent(self, network):
        """The real server fails an exact walk on one, and every multi-leg route has them."""
        check_route([ON_TRUNK[0], ON_TRUNK[1], ON_TRUNK[1], ON_TRUNK[2]])
        assert [(p["lat"], p["lon"]) for p in network.traced[0]["shape"]] == ON_TRUNK[:3]

    def test_a_u_turn_back_over_the_same_points_is_kept(self, network):
        out_and_back = OFF_TRUNK + OFF_TRUNK[-2::-1]
        check_route(out_and_back)
        assert len(network.traced[0]["shape"]) == 3

    def test_a_run_across_two_edges_counts_as_one(self, network):
        """A trunk and its slip road, one stretch with both named."""
        shape = [(LAT, -9.36), (LAT, -9.35), (LAT, -9.34), (38.79, -9.34)]
        network.trace_answer = matched(shape, (0, 1, TRUNK), (1, 2, dict(TRUNK, use="ramp")), (2, 3, {}))

        stretch, = check_route(shape)["hard"]

        assert stretch["roads"] == ["IC 30 (trunk)", "IC 30 (trunk link)"]
        assert same_points(line_of(stretch), shape[:3])
        assert stretch["km"] == pytest.approx(_haversine_km(*shape[0], *shape[2]), abs=0.01)

    def test_an_unsigned_connector_splits_a_stretch(self, network):
        """As on the real IC 30: the edges either side are hard, and the one between
        them is a primary road carrying no ref."""
        shape = [(LAT, -9.36), (LAT, -9.355), (LAT, -9.35), (LAT, -9.345)]
        network.trace_answer = matched(shape, (0, 1, TRUNK), (1, 2, {"road_class": "primary"}), (2, 3, TRUNK))

        report = check_route(shape)

        assert len(report["hard"]) == 2
        assert same_points(line_of(report["hard"][0]), shape[:2])
        assert same_points(line_of(report["hard"][1]), shape[2:])
        assert [p["road"] for p in report["penalty"]] == ["unnamed road (primary road)"]

    def test_stretches_come_from_the_shape_the_server_returned(self, network):
        """Falling back to a map match, its indexes are into its own shape, which can
        hold a point more than was sent: seen on a real U-turn mid-street."""
        sent = [(38.79, -9.38), (LAT, -9.38), (LAT, -9.34)]
        returned = [(38.79, -9.38), (LAT, -9.38), (LAT, -9.379), (LAT, -9.34)]
        network.trace_answer = matched(returned, (0, 1, {}), (2, 3, TRUNK))

        assert same_points(line_of(check_route(sent)["hard"][0]), returned[2:])

    def test_a_penalty_road_is_reported_with_its_length(self, network):
        network.roads = {LAT: BUSY}
        report = check_route([(38.79, -9.38), (LAT, -9.38), (LAT, -9.36), (LAT, -9.34), (38.79, -9.34)])
        assert report["hard"] == []
        assert [p["road"] for p in report["penalty"]] == ["N 9 (90 km/h)"]
        assert report["penalty"][0]["km"] == pytest.approx(3.47, abs=0.05)

    def test_the_longest_penalty_road_comes_first(self, network):
        network.roads = {LAT: {"road_class": "primary", "names": ["N 117"]},
                         NORTH: {"road_class": "primary", "names": ["N 9"]}}
        report = check_route([(LAT, -9.38), (LAT, -9.37), (NORTH, -9.37), (NORTH, -9.34)])
        assert [p["road"] for p in report["penalty"]] == ["N 9 (primary road)", "N 117 (primary road)"]

    def test_no_route_is_nothing_to_check(self, network):
        assert check_route([]) == {"hard": [], "penalty": []}
        assert network.traced == []


class TestLookupFails:
    """If the lookup fails the route is unchecked, which is not the same as clean."""

    @pytest.mark.parametrize("failure", [
        requests.ConnectionError("refused"), requests.Timeout("slow"), refused({}, status=504),
        refused({}, status=429), refused({"error_code": 171, "error": "No suitable edges near location"}),
    ], ids=["connection", "timeout", "504", "429", "400"])
    def test_a_failed_request_is_unavailable_and_is_not_repeated(self, network, failure):
        network.trace_answer = failure
        with pytest.raises(RoadSafetyUnavailable):
            check_route(ON_TRUNK)
        assert len(network.traced) == 1

    def test_a_route_too_long_for_the_server_says_so(self, network):
        network.trace_answer = refused({"error_code": 153, "error": "Too many shape points: (16001). The limit is 16000"})
        with pytest.raises(RoadSafetyUnavailable, match="The limit is 16000"):
            check_route(ON_TRUNK)

    def test_a_body_that_is_not_json_is_unavailable(self, network):
        network.trace_answer = MagicMock(status_code=200)
        network.trace_answer.json.side_effect = ValueError("an HTML error page")
        with pytest.raises(RoadSafetyUnavailable):
            check_route(ON_TRUNK)

    @pytest.mark.parametrize("body", [{}, {"edges": []}, {"edges": [], "shape": ""}, {"shape": "??"}],
                             ids=["empty", "no-edges", "nothing-matched", "shape-only"])
    def test_a_200_with_no_edges_is_unavailable(self, network, body):
        """Reading one as a road with nothing against it passes without having looked."""
        network.trace_answer = ok(body)
        with pytest.raises(RoadSafetyUnavailable):
            check_route(ON_TRUNK)

    @staticmethod
    def _matched_short_of(network, route, unmatched_km):
        route_km = sum(_haversine_km(*a, *b) for a, b in zip(route, route[1:]))
        network.trace_answer = ok({"shape": encode_polyline6(route), "edges": [
            {"length": route_km - unmatched_km, "begin_shape_index": 0,
             "end_shape_index": len(route) - 1}]})

    @pytest.mark.parametrize("unmatched_km, passes", [(1.0, False), (0.3, False), (0.2, True), (-0.02, True)])
    def test_a_route_matched_only_in_part_is_unavailable(self, network, unmatched_km, passes):
        """The rest would be unchecked. The most the real server dropped from a route it
        had whole was 37 m, a map match clipping a U-turn."""
        self._matched_short_of(network, ON_TRUNK, unmatched_km)
        if passes:
            assert check_route(ON_TRUNK) == {"hard": [], "penalty": []}
        else:
            with pytest.raises(RoadSafetyUnavailable):
                check_route(ON_TRUNK)

    @pytest.mark.parametrize("unmatched_km, passes", [(0.3, False), (0.2, True)])
    def test_the_allowance_is_measured_on_the_routers_own_length(self, network, unmatched_km, passes):
        """A haversine reads about 0.3% under the router, so against one the allowance
        is 250 m on a short route and 580 m on this one, which it would pass whole."""
        route = [(38.0, -9.0), (39.0, -9.0)]
        route_km = sum(_haversine_km(*a, *b) for a, b in zip(route, route[1:])) * 1.003
        network.trace_answer = ok({"shape": encode_polyline6(route), "edges": [
            {"length": route_km - unmatched_km, "begin_shape_index": 0, "end_shape_index": 1}]})
        if passes:
            assert check_route(route, route_km=route_km) == {"hard": [], "penalty": []}
        else:
            with pytest.raises(RoadSafetyUnavailable):
                check_route(route, route_km=route_km)

    def test_the_allowance_does_not_grow_with_the_route(self, network):
        # 2 km unchecked on a 111 km route is 98% matched
        route = [(38.0, -9.0), (39.0, -9.0)]
        self._matched_short_of(network, route, 2.0)
        with pytest.raises(RoadSafetyUnavailable):
            check_route(route)

    def test_a_route_too_short_to_miss_still_needs_an_answer(self, network):
        network.trace_answer = ok({"shape": "", "edges": []})
        with pytest.raises(RoadSafetyUnavailable):
            check_route([(LAT, -9.36), (LAT, east_of(-9.36, 100))])


# --- what goes to Valhalla ---

class TestExclusionPolygons:
    @pytest.fixture
    def stretch(self, network):
        network.roads = {LAT: TRUNK}
        return check_route(ON_TRUNK)["hard"][0]

    def test_rings_are_closed_and_on_the_road_leaving_a_cycleway_beside_it(self, stretch):
        rings = exclusion_polygons([stretch])
        assert rings and all(ring[0] == ring[-1] for ring in rings)
        assert all(abs(lat - LAT) * 111000 < 4 for ring in rings for lat, _ in ring)
        assert not in_rings(LAT + 5 / 111000, -9.36, rings)
        assert not in_rings(LAT - 5 / 111000, -9.36, rings)

    @staticmethod
    def _edges(*lengths_m):
        """Consecutive edges along the road, and the junctions between them."""
        junctions = [-9.36]
        for metres in lengths_m:
            junctions.append(east_of(junctions[-1], metres))
        points = [(LAT, lng) for lng in junctions]
        return [[a, b] for a, b in zip(points, points[1:])], points

    def test_every_edge_gets_a_polygon_in_its_middle(self):
        """A polygon drops the edge it touches, so an edge without one stays rideable,
        however short it is, as long as a junction at each end lets a route on and off."""
        edges, _ = self._edges(40, 900, 15)
        rings = exclusion_polygons([{"edges": edges}])
        assert len(rings) == 3
        for ring, (start, end) in zip(rings, edges):
            middle = ((start[0] + end[0]) / 2, (start[1] + end[1]) / 2)
            assert in_rings(*middle, [ring])

    def test_no_polygon_touches_a_junction(self):
        """It would drop the side road there too, inside a stretch as much as at its ends."""
        edges, junctions = self._edges(40, 900, 300)
        for ring in exclusion_polygons([{"edges": edges}]):
            for junction in junctions:
                assert min(_haversine_km(*junction, *vertex) for vertex in ring) > 0.008

    @pytest.mark.parametrize("metres", [6, 45])
    def test_a_short_edge_gets_one_polygon_clear_of_its_ends(self, metres):
        line = [(LAT, -9.36), (LAT, east_of(-9.36, metres))]
        ring, = exclusion_polygons([{"edges": [line]}])
        assert min(lng for _, lng in ring) > -9.36
        assert max(lng for _, lng in ring) < line[1][1]

    def test_an_edge_with_no_length_gets_none(self):
        assert exclusion_polygons([{"edges": [[(LAT, -9.36), (LAT, -9.36)]]}]) == []

    def test_a_polygon_follows_a_bend_in_the_line(self):
        corner = (LAT, -9.36)
        line = [(LAT - 160 / 111000, -9.36), corner, (LAT, east_of(-9.36, 160))]
        ring, = exclusion_polygons([{"edges": [line]}])
        assert all(_haversine_km(*corner, *vertex) < 0.03 for vertex in ring)

    def test_perimeter_of_a_known_rectangle(self):
        side = 100 / 111000
        square = [(0.0, 0.0), (side, 0.0), (side, side), (0.0, side), (0.0, 0.0)]
        assert perimeter_m([square, square]) == pytest.approx(800, rel=0.01)


# --- the plan ---

class TestPlan:
    def test_a_clean_route_passes_untouched_on_the_first_try(self, planning):
        result = run_plan(planning, {LAT: TRUNK}, [OFF_TRUNK])
        assert len(planning.sent) == 1
        assert "exclude_polygons" not in planning.sent[0]
        assert "GPX:" in result
        assert "Rerouted" not in result and "Busy" not in result

    def test_a_route_on_a_trunk_retries_with_polygons_on_that_stretch_only(self, planning):
        run_plan(planning, {LAT: TRUNK}, [ON_TRUNK, OFF_TRUNK])
        first, second = planning.sent
        assert "exclude_polygons" not in first
        rings = rings_sent(second)
        assert rings
        for lat, lng in (vertex for ring in rings for vertex in ring):
            assert abs(lat - LAT) * 111000 < 4  # on the road, not beside it
            assert -9.38 < lng < -9.34  # on the part ridden, not the whole road

    def test_the_route_that_comes_back_is_kept_with_its_gpx_and_the_detour_said(self, planning):
        result = run_plan(planning, {LAT: TRUNK}, [ON_TRUNK, OFF_TRUNK])
        assert same_points(planning.preview.call_args[0][0], OFF_TRUNK)
        assert "GPX:" in result and GPX.exists()
        assert "Rerouted off: IC 30 (trunk)" in result

    def test_the_lookup_uses_the_plans_surface(self, planning):
        run_plan(planning, surface="mtb")
        assert planning.traced[0]["costing_options"]["bicycle"]["bicycle_type"] == "Mountain"

    def test_polygons_accumulate_across_retries(self, planning):
        """Dropping the first set would let the third route back onto the first road."""
        run_plan(planning, {LAT: TRUNK, NORTH: SECOND_TRUNK}, [ON_TRUNK, ON_SECOND, OFF_TRUNK])
        assert {round(lat, 2) for ring in rings_sent(planning.sent[2]) for lat, _ in ring} == {LAT, NORTH}

    def test_a_route_clean_only_on_the_last_retry_is_kept(self, planning):
        result = run_plan(planning, {LAT: TRUNK}, [ON_TRUNK] * MAX_ROAD_RETRIES + [OFF_TRUNK])
        assert len(planning.sent) == 1 + MAX_ROAD_RETRIES
        assert "GPX:" in result

    def test_retries_are_bounded_then_no_safe_route_names_the_road_and_leaves_no_gpx(self, planning):
        result = run_plan(planning, {LAT: TRUNK}, [ON_TRUNK])
        assert len(planning.sent) == 1 + MAX_ROAD_RETRIES
        assert result.startswith("no_safe_route:")
        assert "IC 30 (trunk)" in result and "GPX:" not in result
        assert not GPX.exists()

    def test_no_safe_route_names_every_road_it_met(self, planning):
        result = run_plan(planning, {LAT: TRUNK, NORTH: SECOND_TRUNK}, [ON_TRUNK, ON_SECOND] * 2)
        assert result.startswith("no_safe_route:")
        assert "IC 30" in result and "A 16" in result

    def test_no_path_around_the_road_names_it_and_blames_no_zone(self, planning):
        result = run_plan(planning, {LAT: TRUNK}, [ON_TRUNK, NO_PATH])
        assert result.startswith("no_safe_route:")
        assert "IC 30" in result and "avoid zone" not in result
        assert not GPX.exists()

    def test_no_path_with_zones_says_they_may_be_part_of_it(self, planning):
        planning.config["avoid"] = [{"lat": 38.70, "lng": -9.38, "radius": 500}]
        result = run_plan(planning, {LAT: TRUNK}, [ON_TRUNK, NO_PATH])
        assert result.startswith("no_safe_route:") and "avoid zone" in result

    @pytest.mark.parametrize("failure", [
        refused({}, status=503), refused({}, status=429),
        requests.ConnectionError("down"), requests.Timeout("slow"),
    ], ids=["503", "429", "connection", "timeout"])
    def test_a_router_failure_on_a_retry_stays_a_routing_failure(self, planning, failure):
        """It says nothing about the roads, and the same plan may work on another try."""
        result = run_plan(planning, {LAT: TRUNK}, [ON_TRUNK, failure])
        assert result.startswith("Route generation failed:")
        assert not GPX.exists()

    def test_any_other_refusal_on_a_retry_is_no_safe_route(self, planning):
        no_edges = {"error_code": 171, "error": "No suitable edges near location"}
        assert run_plan(planning, {LAT: TRUNK}, [ON_TRUNK, no_edges]).startswith("no_safe_route:")

    def test_a_routing_failure_on_the_first_try_stays_a_routing_failure(self, planning):
        result = run_plan(planning, routes=[{"error_code": 171, "error": "No suitable edges near location"}])
        assert result.startswith("Route generation failed:")

    def test_a_penalty_road_does_not_fail_the_route_and_is_reported(self, planning):
        result = run_plan(planning, {LAT: BUSY}, [ON_TRUNK])
        assert len(planning.sent) == 1
        assert "exclude_polygons" not in planning.sent[0]
        assert "GPX:" in result
        line = next(text for text in result.splitlines() if "N 9" in text)
        assert "90 km/h" in line and "3.5 km" in line
        info = planning.preview.call_args[1]["route_info"]
        assert [p["road"] for p in info["busy_roads"]] == ["N 9 (90 km/h)"]

    def test_the_report_follows_the_display_units(self, planning):
        planning.config["display"]["units"] = "imperial"
        result = run_plan(planning, {LAT: BUSY}, [ON_TRUNK])
        assert "2.2 mi" in next(text for text in result.splitlines() if "N 9" in text)

    @pytest.mark.parametrize("safety, hinted", [(0.5, True), (1.0, False)])
    def test_the_rider_is_pointed_at_safety_while_it_has_room(self, planning, safety, hinted):
        assert ("--safety" in run_plan(planning, {LAT: BUSY}, [ON_TRUNK], safety=safety)) is hinted


class TestPreviewCard:
    @staticmethod
    def render(tmp_path, busy_roads):
        return Path(preview(OFF_TRUNK, "Route", route_info={"busy_roads": busy_roads},
                            output_dir=str(tmp_path))).read_text()

    def test_it_shows_the_total_and_the_roads(self, tmp_path):
        page = self.render(tmp_path, [{"road": "N 9 (90 km/h)", "km": 2.0}, {"road": "N 117 (primary road)", "km": 1.5}])
        assert "Busy or fast roads" in page and "3.5 km" in page
        assert "N 9 (90 km/h)" in page and "N 117 (primary road)" in page

    def test_no_card_without_busy_roads(self, tmp_path):
        assert "Busy or fast roads" not in self.render(tmp_path, [])

    def test_a_road_name_from_osm_is_escaped(self, tmp_path):
        page = self.render(tmp_path, [{"road": "<script>alert(1)</script> (primary road)", "km": 1.0}])
        assert "<script>alert(1)" not in page and "&lt;script&gt;alert(1)" in page


class TestZonesAndRoadsTogether:
    """Two checks on one route, and neither may undo the other."""
    NEAR = {"lat": 38.70, "lng": -9.38, "radius": 500}
    # About 134 km north: out of a 30 km plan's reach, so it starts out left out
    FAR = {"lat": 40.0, "lng": -9.36, "radius": 500}
    THROUGH_FAR = [(38.79, -9.38), (40.0, -9.36), (38.79, -9.34)]

    @staticmethod
    def counts(payload):
        """How many zone rings and road polygons a request carried."""
        rings = rings_sent(payload)
        roads = sum(abs(ring[0][0] - LAT) < 0.001 for ring in rings)
        return len(rings) - roads, roads

    def test_reachable_zones_and_new_polygons_go_together_without_the_rest(self, planning):
        planning.config["avoid"] = [self.NEAR, self.FAR]
        run_plan(planning, {LAT: TRUNK}, [ON_TRUNK, OFF_TRUNK])
        zones, roads = self.counts(planning.sent[1])
        assert zones == 1 and roads > 0

    def test_a_retry_into_a_left_out_zone_keeps_the_road_polygons(self, planning):
        """Replacing the exclusions with every zone would send it back onto the trunk."""
        planning.config["avoid"] = [self.FAR]
        result = run_plan(planning, {LAT: TRUNK}, [ON_TRUNK, self.THROUGH_FAR, OFF_TRUNK])
        zones, roads = self.counts(planning.sent[2])
        assert zones == 1 and roads > 0
        assert same_points(planning.preview.call_args[0][0], OFF_TRUNK)
        assert "GPX:" in result

    def test_a_road_retry_after_the_zone_retry_keeps_every_zone(self, planning):
        planning.config["avoid"] = [self.FAR]
        run_plan(planning, {LAT: TRUNK}, [self.THROUGH_FAR, ON_TRUNK, OFF_TRUNK])
        zones, roads = self.counts(planning.sent[2])
        assert zones == 1 and roads > 0

    def test_a_zone_retry_does_not_use_up_a_road_retry(self, planning):
        planning.config["avoid"] = [self.FAR]
        result = run_plan(planning, {LAT: TRUNK}, [self.THROUGH_FAR] + [ON_TRUNK] * MAX_ROAD_RETRIES + [OFF_TRUNK])
        assert len(planning.sent) == 2 + MAX_ROAD_RETRIES
        assert "GPX:" in result

    def test_the_zone_retry_happens_once(self, planning):
        """Otherwise a route the server keeps answering with never ends."""
        planning.config["avoid"] = [self.FAR]
        run_plan(planning, routes=[self.THROUGH_FAR])
        assert len(planning.sent) == 2

    def test_the_zone_check_still_runs_with_no_road_involved(self, planning):
        planning.config["avoid"] = [self.FAR]
        run_plan(planning, routes=[self.THROUGH_FAR, OFF_TRUNK])
        assert [len(rings_sent(payload)) for payload in planning.sent] == [0, 1]


class TestPerimeterBudget:
    """Three 500 m zones are about 9.6 km of the server's 10 km."""
    ZONES = [{"lat": 38.70, "lng": lng, "radius": 500} for lng in (-9.42, -9.38, -9.34)]
    # A road of 30 edges, about 1.5 km of polygons, on top of them
    ALL_ALONG = [(38.79, -9.40), *((LAT, -9.40 + i * 0.0033) for i in range(31)), (38.79, -9.30)]

    def test_running_out_is_no_safe_route_and_nothing_is_sent(self, planning):
        planning.config["avoid"] = self.ZONES
        result = run_plan(planning, {LAT: TRUNK}, [self.ALL_ALONG])
        assert len(planning.sent) == 1
        assert result.startswith("no_safe_route:")
        assert "IC 30" in result and "avoid zone" in result and "10 km" in result

    def test_just_inside_the_budget_is_sent(self, planning):
        planning.config["avoid"] = self.ZONES
        planning.roads = {LAT: TRUNK}
        needed = (perimeter_m(exclusion_polygons(check_route(ON_TRUNK)["hard"]))
                  + perimeter_m([avoid_zone_ring(zone) for zone in self.ZONES]))
        with patch("velomate.route_planner.EXCLUDE_PERIMETER_LIMIT_M", needed + 1):
            result = run_plan(planning, {LAT: TRUNK}, [ON_TRUNK, OFF_TRUNK])
        assert len(planning.sent) == 2
        assert "GPX:" in result

    def test_without_zones_a_long_stretch_fits(self, planning):
        assert "GPX:" in run_plan(planning, {LAT: TRUNK}, [self.ALL_ALONG, OFF_TRUNK])

    def test_a_server_refusal_is_no_safe_route_not_a_raw_error(self, planning):
        """The limit is the server's, and it can be lower than the one assumed here."""
        result = run_plan(planning, {LAT: TRUNK}, [ON_TRUNK, PERIMETER_LIMIT])
        assert result.startswith("no_safe_route:")
        assert "IC 30" in result
        for raw in ("167", "Routing failed", "Exceeded maximum"):
            assert raw not in result

    def test_zones_alone_over_the_limit_keep_their_own_message(self, planning):
        planning.config["avoid"] = self.ZONES
        result = run_plan(planning, routes=[PERIMETER_LIMIT])
        assert result.startswith("Route generation failed:") and "Remove zones or shrink them" in result


class TestLookupDown:
    @pytest.mark.parametrize("failure", [requests.ConnectionError("refused"), refused({}, status=504)],
                             ids=["connection", "504"])
    def test_the_plan_fails_closed_after_one_lookup_and_leaves_no_gpx(self, planning, failure):
        planning.trace_answer = failure
        result = run_plan(planning)
        assert "unavailable" in result and "GPX:" not in result
        assert "30 days" not in result
        assert len(planning.traced) == 1 and len(planning.sent) == 1
        planning.preview.assert_not_called()
        assert not GPX.exists()

    def test_the_plan_measures_the_match_against_the_routers_length(self, planning):
        """The router's summary has to reach the check, or it quietly compares
        against a haversine. Here the two differ by more than the allowance."""
        length = sum(_haversine_km(*a, *b) for a, b in zip(OFF_TRUNK, OFF_TRUNK[1:])) + 0.3
        route = ok({"trip": {"summary": {"length": length},
                             "legs": [{"shape": encode_polyline6(OFF_TRUNK)}]}})

        result = run_plan(planning, {}, [route])

        assert "unavailable" in result and "GPX:" not in result

    def test_the_retry_failing_its_lookup_fails_closed_too(self, planning):
        real_trace = planning.trace

        def down_after_first(payload):
            if planning.traced:
                raise requests.ConnectionError("gone")
            return real_trace(payload)

        planning.trace = down_after_first
        result = run_plan(planning, {LAT: TRUNK}, [ON_TRUNK, OFF_TRUNK])
        assert "unavailable" in result and "GPX:" not in result
        assert len(planning.sent) == 2
        assert not GPX.exists()
