"""Route generator — creates a real cycling GPX loop using Valhalla routing (free, no API key).

Flow:
  start_point + target_distance → compute loop waypoints → Valhalla routing → GPX
"""

import math
import requests
import xml.etree.ElementTree as ET
from datetime import datetime, timezone

VALHALLA_URL = "https://valhalla1.openstreetmap.de/route"
VALHALLA_TRACE_URL = VALHALLA_URL.replace("/route", "/trace_attributes")

# Valhalla costing profile per surface type
VALHALLA_COSTING = {
    "road":   "bicycle",   # road cycling sub-profile set in costing_options
    "gravel": "bicycle",
    "mtb":    "bicycle",
}

# Road cycling sub-profile within Valhalla's bicycle costing
ROAD_CYCLING_OPTIONS = {
    "bicycle_type": "Road",
    "use_roads": 1.0,
    "use_hills": 0.3,
}
GRAVEL_CYCLING_OPTIONS = {
    "bicycle_type": "Cross",
    "use_roads": 0.5,
    "use_hills": 0.5,
}
MTB_CYCLING_OPTIONS = {
    "bicycle_type": "Mountain",
}


def _loop_waypoints(lat: float, lng: float, target_km: float, num_points: int = 4) -> list:
    """Generate waypoints in a rough circle of the right radius.
    Places them so the full loop approximates target_km distance.
    Adds a small random bias to avoid out-and-back on the same road.
    """
    # Circumference ≈ target_km, so radius = target_km / (2π)
    # Reduce by 20% because road routing adds distance vs straight-line circles
    radius_km = (target_km / (2 * math.pi)) * 0.8
    radius_lat = radius_km / 111.0          # degrees latitude per km
    radius_lng = radius_km / (111.0 * math.cos(math.radians(lat)))

    waypoints = []
    for i in range(num_points):
        angle = (2 * math.pi * i / num_points) - (math.pi / 2)  # start North
        wlat = lat + radius_lat * math.sin(angle)
        wlng = lng + radius_lng * math.cos(angle)
        waypoints.append({"lat": round(wlat, 5), "lon": round(wlng, 5)})
    return waypoints


def avoid_zone_ring(zone: dict, points: int = 16) -> list:
    """Approximate a circular avoid zone as a closed ring of (lat, lng) pairs.

    Vertices sit just outside the circle, so the polygon's flat sides still cover it.
    """
    radius_km = zone.get("radius", 500) / 1000 / math.cos(math.pi / points)
    radius_lat = radius_km / 111.0
    radius_lng = radius_km / (111.0 * math.cos(math.radians(zone["lat"])))

    ring = []
    for i in range(points):
        angle = 2 * math.pi * i / points
        ring.append((round(zone["lat"] + radius_lat * math.cos(angle), 5),
                     round(zone["lng"] + radius_lng * math.sin(angle), 5)))
    return ring + ring[:1]


def in_rings(lat: float, lng: float, rings: list) -> bool:
    """True when the point is inside any ring of (lat, lng) pairs (ray casting)."""
    for ring in rings:
        inside = False
        for (lat1, lng1), (lat2, lng2) in zip(ring, ring[1:] + ring[:1]):
            if (lat1 > lat) != (lat2 > lat) and lng < lng1 + (lat - lat1) * (lng2 - lng1) / (lat2 - lat1):
                inside = not inside
        if inside:
            return True
    return False


def _exclusion_failure(response) -> tuple:
    """Return the error code and an explanation of a Valhalla refusal caused by the
    exclusions, or (None, None) if it was something else."""
    try:
        body = response.json()
    except ValueError:
        return None, None
    error_code = body.get("error_code")
    if error_code == 442:  # no path, which disconnected waypoints also produce
        return error_code, (f"Valhalla found no route ({body.get('error')}). The configured avoid "
                            "zones may be blocking it: check them, or change the waypoints")
    if error_code == 167:  # polygons over the server's perimeter limit
        return error_code, (f"Valhalla refused the configured avoid zones ({body.get('error')}). "
                            "Remove zones or shrink them in your config")
    return None, None


def _decode_polyline6(encoded: str) -> list:
    """Decode Valhalla's encoded polyline (precision 6) to list of (lat, lng)."""
    coords = []
    index = 0
    lat = 0
    lng = 0
    length = len(encoded)
    while index < length:
        for is_lng in (False, True):
            result = 0
            shift = 0
            while index < length:
                b = ord(encoded[index]) - 63
                index += 1
                result |= (b & 0x1F) << shift
                shift += 5
                if b < 0x20:
                    break
            delta = ~(result >> 1) if (result & 1) else (result >> 1)
            if is_lng:
                lng += delta
            else:
                lat += delta
        coords.append((lat / 1e6, lng / 1e6))
    return coords


def _build_gpx(coords: list, name: str, surface: str) -> str:
    """Build a GPX string from a list of (lat, lng) tuples."""
    ns = "http://www.topografix.com/GPX/1/1"
    ET.register_namespace("", ns)
    root = ET.Element(f"{{{ns}}}gpx", {
        "version": "1.1",
        "creator": "VeloMate",
    })

    metadata = ET.SubElement(root, f"{{{ns}}}metadata")
    ET.SubElement(metadata, f"{{{ns}}}name").text = name
    ET.SubElement(metadata, f"{{{ns}}}time").text = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    trk = ET.SubElement(root, f"{{{ns}}}trk")
    ET.SubElement(trk, f"{{{ns}}}name").text = name
    ET.SubElement(trk, f"{{{ns}}}type").text = surface
    trkseg = ET.SubElement(trk, f"{{{ns}}}trkseg")
    for lat, lng in coords:
        ET.SubElement(trkseg, f"{{{ns}}}trkpt", {"lat": str(lat), "lon": str(lng)})

    return '<?xml version="1.0" encoding="UTF-8"?>\n' + ET.tostring(root, encoding="unicode")


def generate(
    start_lat: float,
    start_lng: float,
    target_km: float,
    surface: str = "gravel",
    name: str = None,
    output_path: str = None,
    waypoints: list = None,
    safety: float = 0.5,
    destination: dict = None,
    loop: bool = True,
    exclude_polygons: list | None = None,
) -> dict:
    """Generate a cycling loop GPX route.

    exclude_polygons is a list of rings of (lat, lng) pairs the route must stay
    out of. No path exists to a location inside one, so callers drop those first.

    Returns dict with keys:
      - gpx_path: path to saved GPX file
      - actual_km: actual route distance from Valhalla
      - name: route name
      - error: error message (if failed)
      - error_code: Valhalla's code when the exclusions caused the failure, else None
      - status: the HTTP status when the router answered with an error
    """
    if name is None:
        if destination:
            name = f"VeloMate {target_km:.0f}km {surface.title()} to {destination.get('name', 'destination')}"
        else:
            name = f"VeloMate {target_km:.0f}km {surface.title()} Loop"
    if output_path is None:
        output_path = f"/tmp/velomate_route_{surface}_{target_km:.0f}km.gpx"

    costing = VALHALLA_COSTING.get(surface, "bicycle")
    costing_options: dict = {}
    if surface == "road":
        costing_options = ROAD_CYCLING_OPTIONS.copy()
    elif surface == "gravel":
        costing_options = GRAVEL_CYCLING_OPTIONS.copy()
    elif surface == "mtb":
        costing_options = MTB_CYCLING_OPTIONS.copy()

    # Safety: 0.0 = fastest (use all roads), 1.0 = safest (avoid busy roads)
    # Maps inversely to Valhalla's use_roads: safety 1.0 → use_roads 0.0
    if costing_options:
        costing_options["use_roads"] = round(1.0 - safety, 1)

    # Build location list — loop, destination one-way, or destination round-trip
    start = {"lat": start_lat, "lon": start_lng}
    if waypoints:
        loop_pts = [{"lat": w["lat"], "lon": w["lon"]} for w in waypoints]
    elif not destination:
        loop_pts = [p for p in _loop_waypoints(start_lat, start_lng, target_km)
                    if not in_rings(p["lat"], p["lon"], exclude_polygons or [])]
    else:
        loop_pts = []

    if destination:
        dest = {"lat": destination["lat"], "lon": destination["lng"]}
        if loop:
            locations = [start] + loop_pts + [dest, start]
        else:
            locations = [start] + loop_pts + [dest]
    else:
        locations = [start] + loop_pts + [start]

    payload = {
        "locations": locations,
        "costing": costing,
        "units": "km",
        "shape_format": "polyline6",
    }
    if costing_options:
        payload["costing_options"] = {costing: costing_options}

    if exclude_polygons:
        # Valhalla expects each polygon as a list of [lon, lat] pairs
        payload["exclude_polygons"] = [
            [[lon, lat] for (lat, lon) in ring]
            for ring in exclude_polygons
        ]

    try:
        resp = requests.post(VALHALLA_URL, json=payload, timeout=15)
        resp.raise_for_status()
        data = resp.json()
    except requests.HTTPError as e:
        error_code, failure = _exclusion_failure(e.response) if exclude_polygons else (None, None)
        return {"error": failure or f"Routing failed: {e}", "error_code": error_code,
                "status": e.response.status_code}
    except Exception as e:
        return {"error": f"Routing failed: {e}"}

    # Extract route summary
    trip = data.get("trip", {})
    summary = trip.get("summary", {})
    actual_km = summary.get("length", target_km)

    # Decode shape from legs
    all_coords = []
    for leg in trip.get("legs", []):
        shape = leg.get("shape", "")
        coords = _decode_polyline6(shape)
        all_coords.extend(coords)

    if not all_coords:
        return {"error": "No route coordinates returned from Valhalla"}

    # Build and save GPX
    gpx_content = _build_gpx(all_coords, name, surface)
    with open(output_path, "w", encoding="utf-8") as f:
        f.write(gpx_content)

    return {
        "gpx_path": output_path,
        "actual_km": round(actual_km, 1),
        # As the router gave it. actual_km is rounded, and falls back to the target.
        "route_km": summary.get("length"),
        "name": name,
        "coords": all_coords,
    }
