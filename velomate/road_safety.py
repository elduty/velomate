"""Road safety check for planned routes.

A route comes back from the router first, and the router is then asked what that
route rides on. Roads a cyclist must not use come back as stretches, with small
polygons that keep the next attempt off them. Only those stretches: Valhalla refuses
more than 10 km of polygon perimeter in total, which excluding every such road in
an area passes straight away.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from itertools import groupby

import requests

from velomate.route_generator import (
    GRAVEL_CYCLING_OPTIONS,
    MTB_CYCLING_OPTIONS,
    ROAD_CYCLING_OPTIONS,
    VALHALLA_TRACE_URL,
    _decode_polyline6,
)
from velomate.route_intelligence import _haversine_km


class RoadSafetyUnavailable(Exception):
    """Raised when the router can't say what a route rides on, so it stays unchecked."""


HARD = "hard"
PENALTY = "penalty"

# motorroad=yes, bicycle=no and access=no aren't in the lookup's answer. The route comes
# from the router's bicycle costing, which is what has to refuse those.
_HARD_CLASSES = {"motorway", "trunk"}
# Portuguese IC and IP routes, off limits whatever class they have: the IC 30 north
# of Sintra is a primary road, signed together with the EN 9
_HARD_REF = re.compile(r"(^|;)\s*(IC|IP)\s*\d")
_FAST_KMH = 90

_CYCLING_OPTIONS = {"road": ROAD_CYCLING_OPTIONS, "gravel": GRAVEL_CYCLING_OPTIONS, "mtb": MTB_CYCLING_OPTIONS}
_ATTRIBUTES = ["edge.road_class", "edge.use", "edge.names", "edge.speed_limit", "edge.length",
               "edge.begin_shape_index", "edge.end_shape_index", "shape"]
# How far the lookup's answer may fall short of the route, because a map match clips a
# U-turn: 37 m on the worst one measured. Anything more was never checked. A distance
# rather than a share, which would let a long route hide kilometres.
_MAX_UNMATCHED_KM = 0.25

_M_PER_DEG_LAT = 111000.0
_POLYGON_LENGTH_M = 20.0
_POLYGON_WIDTH_M = 6.0


def _classify(edge: dict) -> tuple | None:
    """Return (HARD or PENALTY, why), or None for a road with nothing against it.

    Traffic counts aren't available, so a primary road stands in for high traffic.
    """
    road_class = edge.get("road_class")
    if road_class in _HARD_CLASSES:
        return HARD, f"{road_class} link" if edge.get("use") == "ramp" else road_class
    if _HARD_REF.search(";".join(edge.get("names") or [])):
        return HARD, "IC/IP route"
    limit = edge.get("speed_limit")
    if limit == "unlimited":
        return PENALTY, "no speed limit"
    if isinstance(limit, (int, float)) and limit >= _FAST_KMH:
        return PENALTY, f"{limit} km/h"
    if road_class == "primary":
        return PENALTY, "primary road"
    return None


def _label(edge: dict, why: str) -> str:
    return f"{' / '.join(edge.get('names') or ['unnamed road'])} ({why})"


def _road(edge: dict) -> tuple:
    """Return an edge's (tier, label), or (None, None) when nothing counts against it."""
    found = _classify(edge)
    return (found[0], _label(edge, found[1])) if found else (None, None)


def _line_km(line: list) -> float:
    return sum(_haversine_km(*a, *b) for a, b in zip(line, line[1:]))


def _trace(coords: list, surface: str, route_km: float | None = None) -> tuple:
    """Return the edges the router says a route rides, and the shape they index into.

    walk_or_snap walks the route's own edges, and then the returned shape is the route.
    Only a U-turn partway along an edge can't be walked. That falls back to a map match,
    whose indexes are into its own shape, so stretches are always read from the returned
    one. A vertex repeated where two legs join fails the walk, so those are dropped.

    route_km is the router's length for the route. The edges are in the router's
    lengths too, and a haversine reads about 0.3% under them, which would stretch
    the allowance by that share of the route.
    """
    points = [point for i, point in enumerate(coords) if i == 0 or point != coords[i - 1]]
    payload = {
        "shape": [{"lat": lat, "lon": lng} for lat, lng in points],
        "costing": "bicycle",
        "costing_options": {"bicycle": {"bicycle_type": _CYCLING_OPTIONS[surface]["bicycle_type"]}},
        "shape_match": "walk_or_snap",
        "filters": {"action": "include", "attributes": _ATTRIBUTES},
    }
    try:
        resp = requests.post(VALHALLA_TRACE_URL, json=payload, timeout=30)
        body = resp.json()
    except (requests.RequestException, ValueError) as e:
        raise RoadSafetyUnavailable(f"road lookup failed: {e}") from e
    if resp.status_code != 200:
        raise RoadSafetyUnavailable(f"road lookup refused: {body.get('error', resp.status_code)}")

    edges = body.get("edges") or []
    matched_km = sum(edge.get("length", 0) for edge in edges)
    expected_km = _line_km(points) if route_km is None else route_km
    if not edges or expected_km - matched_km > _MAX_UNMATCHED_KM:
        raise RoadSafetyUnavailable("road lookup matched only part of the route")
    return edges, _decode_polyline6(body.get("shape", ""))


def check_route(coords: list, surface: str = "road", route_km: float | None = None) -> dict:
    """Find the stretches of a route on listed roads.

    Returns {"hard": [{"roads": [label, ...], "km": km, "edges": [line, ...]}, ...],
    "penalty": [{"road": label, "km": km}, ...]}, the longest penalty road first. A line
    is an edge's [(lat, lng), ...].
    Raises RoadSafetyUnavailable when the lookup fails: the route is unchecked, and
    that is not the same as clean.
    """
    if not coords:
        return {"hard": [], "penalty": []}

    edges, shape = _trace(coords, surface, route_km)
    hard = []
    penalty: Counter = Counter()
    for tier, run in groupby(edges, key=lambda edge: _road(edge)[0]):
        run = list(run)
        if tier == HARD:
            hard.append({"roads": list(dict.fromkeys(_road(edge)[1] for edge in run)),
                         "km": sum(edge["length"] for edge in run),
                         "edges": [shape[edge["begin_shape_index"]:edge["end_shape_index"] + 1]
                                   for edge in run]})
        elif tier == PENALTY:
            for edge in run:
                penalty[_road(edge)[1]] += edge["length"]

    return {"hard": hard, "penalty": [{"road": road, "km": km} for road, km in penalty.most_common()]}


def _rectangle(start: tuple, end: tuple, width_m: float) -> list:
    """Return a closed ring width_m wide around the line from start to end."""
    lat1, lng1 = start
    lat2, lng2 = end
    cos_lat = math.cos(math.radians((lat1 + lat2) / 2))
    dx_m = (lng2 - lng1) * _M_PER_DEG_LAT * cos_lat
    dy_m = (lat2 - lat1) * _M_PER_DEG_LAT
    length_m = math.hypot(dx_m, dy_m)
    off_lat = (width_m / 2) * (dx_m / length_m) / _M_PER_DEG_LAT
    off_lng = (width_m / 2) * (-dy_m / length_m) / (_M_PER_DEG_LAT * cos_lat)
    ring = [(lat1 + off_lat, lng1 + off_lng), (lat2 + off_lat, lng2 + off_lng),
            (lat2 - off_lat, lng2 - off_lng), (lat1 - off_lat, lng1 - off_lng)]
    return ring + ring[:1]


def _point_along(line: list, metres: float) -> tuple:
    """Return the point this far along a line."""
    for a, b in zip(line, line[1:]):
        length_m = _haversine_km(*a, *b) * 1000
        if length_m and metres <= length_m:
            t = metres / length_m
            return a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t
        metres -= length_m
    return line[-1]


def exclusion_polygons(stretches: list) -> list:
    """Return a small ring on every edge of these stretches, as closed rings of (lat, lng).

    Valhalla drops a whole edge for any polygon touching it. One in the middle of an
    edge takes that edge out and stays clear of the junctions at its ends, where it
    would also drop the side road the route came in on.
    """
    rings = []
    for stretch in stretches:
        for line in stretch["edges"]:
            length_m = _line_km(line) * 1000
            if not length_m:
                continue
            half = min(_POLYGON_LENGTH_M, length_m / 2) / 2
            rings.append(_rectangle(_point_along(line, length_m / 2 - half),
                                    _point_along(line, length_m / 2 + half), _POLYGON_WIDTH_M))
    return rings


def perimeter_m(rings: list) -> float:
    """Return the total perimeter of these rings, as the routing server counts it."""
    return sum(_haversine_km(*a, *b) * 1000
               for ring in rings
               for a, b in zip(ring, ring[1:] + ring[:1]))
