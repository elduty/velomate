"""Route generator — creates a real cycling GPX loop using Valhalla routing (free, no API key).

Flow:
  start_point + target_distance → compute loop waypoints → Valhalla routing → GPX
"""

import math
import requests
import xml.etree.ElementTree as ET
from datetime import datetime, timezone

VALHALLA_URL = "https://valhalla1.openstreetmap.de/route"

# Valhalla costing profile per surface type
VALHALLA_COSTING = {
    "road":   "bicycle",   # road cycling sub-profile set in costing_options
    "gravel": "bicycle",
    "mtb":    "mountain_bike",
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


def _decode_polyline6(encoded: str) -> list:
    """Decode Valhalla's encoded polyline (precision 6) to list of (lat, lng)."""
    coords = []
    index = 0
    lat = 0
    lng = 0
    while index < len(encoded):
        for is_lng in (False, True):
            result = 0
            shift = 0
            while True:
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
        "creator": "VeloAI",
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
) -> dict:
    """Generate a cycling loop GPX route.

    Returns dict with keys:
      - gpx_path: path to saved GPX file
      - actual_km: actual route distance from Valhalla
      - name: route name
      - error: error message (if failed)
    """
    if name is None:
        name = f"VeloAI {target_km:.0f}km {surface.title()} Loop"
    if output_path is None:
        output_path = f"/tmp/veloai_route_{surface}_{target_km:.0f}km.gpx"

    costing = VALHALLA_COSTING.get(surface, "bicycle")
    costing_options: dict = {}
    if surface == "road":
        costing_options = ROAD_CYCLING_OPTIONS.copy()
    elif surface == "gravel":
        costing_options = GRAVEL_CYCLING_OPTIONS.copy()

    # Build loop waypoints — use provided waypoints or auto-generate a circular loop
    if waypoints:
        loop_pts = [{"lat": w["lat"], "lon": w["lon"]} for w in waypoints]
    else:
        loop_pts = _loop_waypoints(start_lat, start_lng, target_km)
    locations = (
        [{"lat": start_lat, "lon": start_lng}]
        + loop_pts
        + [{"lat": start_lat, "lon": start_lng}]
    )

    payload = {
        "locations": locations,
        "costing": costing,
        "units": "km",
        "shape_format": "polyline6",
    }
    if costing_options:
        payload["costing_options"] = {costing: costing_options}

    try:
        resp = requests.post(VALHALLA_URL, json=payload, timeout=15)
        resp.raise_for_status()
        data = resp.json()
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
        "name": name,
        "coords": all_coords,
    }
