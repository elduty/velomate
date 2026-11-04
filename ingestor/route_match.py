"""Route identity from GPS — pure-function module.

A ride is reduced to the SET of ~111 m grid cells its track passes through.
Two rides are the same route when those sets overlap enough (Jaccard). Using a
set rather than a sequence makes matching independent of direction, speed,
pauses, and where on a loop the ride started.

See docs/design/specs/2026-09-01-route-comparison-design.md.
"""

from __future__ import annotations

import math

# Grid cell size in degrees. 0.001 degrees is ~111 m of latitude.
CELL_DEGREES = 0.001

# Minimum average Jaccard overlap to join an existing route. Measured, not
# tuned: within a route, real rides overlap 0.87-0.99; against any other ride
# they reach at most 0.15. Clustering is identical anywhere in 0.5-0.8.
MATCH_THRESHOLD = 0.7

# Below this many samples a track is too sparse to identify.
MIN_GPS_POINTS = 60

# Start and end within this distance makes the route a loop.
LOOP_CLOSURE_M = 500.0

_EARTH_RADIUS_M = 6371000.0


def haversine_m(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    """Great-circle distance in metres."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lng2 - lng1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * _EARTH_RADIUS_M * math.asin(math.sqrt(a))


def build_signature(points: list) -> set:
    """Grid cells a track passes through, as a set of (y, x) indices.

    Longitude is scaled by cos of THE POINT'S OWN latitude, so a cell index is a
    pure function of the coordinate and the same place always lands in the same
    cell. Deriving the factor from the ride's first sample instead would shift
    every cell when two rides start at different points on the same road: that
    factor multiplies absolute longitude, so 0.05 degrees of start latitude is
    5.1 cells of false offset.
    """
    # Drop unusable rows BEFORE the sparsity gate. lat and lng are
    # independently nullable, so counting raw rows would let a track with 60
    # rows and 55 nulls through and then build a signature from 5 points —
    # exactly the under-sampled case this constant exists to reject.
    usable = [(lat, lng) for lat, lng in points
              if lat is not None and lng is not None]
    if len(usable) < MIN_GPS_POINTS:
        return set()
    cells = set()
    for lat, lng in usable:
        x = lng * math.cos(math.radians(lat))
        cells.add((round(lat / CELL_DEGREES), round(x / CELL_DEGREES)))
    return cells


def encode_signature(cells: set) -> str:
    """Compact, stable text form for storage. Sorted, so it is deterministic."""
    return ",".join(f"{y}:{x}" for y, x in sorted(cells))


def decode_signature(text: str) -> set:
    if not text:
        return set()
    out = set()
    for part in text.split(","):
        y, _, x = part.partition(":")
        out.add((int(y), int(x)))
    return out


def jaccard(a: set, b: set) -> float:
    """Overlap of two cell sets: |A n B| / |A u B|. Empty inputs score 0."""
    union = a | b
    if not union:
        return 0.0
    return len(a & b) / len(union)


def best_route_match(cells: set, routes: dict) -> tuple:
    """Best route for a ride, by AVERAGE similarity to that route's members.

    Average linkage rather than single: single linkage chains, so A joining B
    and B joining C eventually merges routes that share no road. Returns
    (None, best_score) when nothing reaches MATCH_THRESHOLD, so the caller can
    log how close it came.
    """
    best_id, best_score = None, 0.0
    for route_id, members in routes.items():
        if not members:
            continue
        score = sum(jaccard(cells, m) for m in members) / len(members)
        if score > best_score:
            best_id, best_score = route_id, score
    if best_score < MATCH_THRESHOLD:
        return (None, best_score)
    return (best_id, best_score)


def is_loop(points: list) -> bool:
    """True when the track ends within LOOP_CLOSURE_M of where it started.

    Takes only the two endpoints in practice: the caller reads the first and
    last GPS row rather than the whole track, because the signature-reuse path
    never loads one.
    """
    if len(points) < 2:
        return False
    (lat1, lng1), (lat2, lng2) = points[0], points[-1]
    if None in (lat1, lng1, lat2, lng2):
        return False
    return haversine_m(lat1, lng1, lat2, lng2) <= LOOP_CLOSURE_M


def derive_label(distance_m, loop: bool) -> str:
    """Display label from the route's own shape. No network, no manual step."""
    shape = "loop" if loop else "point-to-point"
    if not distance_m:
        return f"unknown distance {shape}"
    return f"{distance_m / 1000.0:.1f} km {shape}"
