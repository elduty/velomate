"""Route intelligence — smart waypoint selection using OSM POIs and Strava segments."""

import requests


OVERPASS_URL = "https://overpass-api.de/api/interpreter"


def get_pois(lat: float, lng: float, radius_km: float, poi_types: list | None = None) -> list[dict]:
    """Query OSM Overpass API for cycling-relevant POIs within radius.
    Returns list of {lat, lng, name, type}.
    """
    if poi_types is None:
        poi_types = [
            'tourism=viewpoint',
            'amenity=cafe',
            'amenity=drinking_water',
            'natural=peak',
            'amenity=bicycle_repair_station',
        ]

    radius_m = int(radius_km * 1000)
    union_parts = []
    for pt in poi_types:
        key, val = pt.split('=', 1)
        union_parts.append(f'node["{key}"="{val}"](around:{radius_m},{lat},{lng});')

    query = f"""
    [out:json][timeout:10];
    (
      {chr(10).join(union_parts)}
    );
    out body;
    """

    try:
        resp = requests.post(OVERPASS_URL, data={"data": query}, timeout=15)
        resp.raise_for_status()
        data = resp.json()
    except (requests.RequestException, ValueError) as e:
        print(f"  [intelligence] Overpass API error: {e}")
        return []

    results = []
    for el in data.get("elements", []):
        if "lat" in el and "lon" in el:
            tags = el.get("tags", {})
            name = tags.get("name", "")
            poi_type = next(
                (f"{k}={v}" for k, v in tags.items() if f"{k}={v}" in poi_types),
                "unknown"
            )
            results.append({
                "lat": el["lat"],
                "lng": el["lon"],
                "name": name or poi_type.split("=")[1].replace("_", " ").title(),
                "type": poi_type,
            })

    return results


def get_strava_segments(lat: float, lng: float, radius_km: float, access_token: str) -> list[dict]:
    """Query Strava API for popular cycling segments near a location.
    Returns list of {lat, lng, name, athlete_count}.
    """
    # Bounding box from center + radius
    dlat = radius_km / 111.0
    dlng = radius_km / (111.0 * 0.75)  # rough cos correction
    bounds = f"{lat - dlat},{lng - dlng},{lat + dlat},{lng + dlng}"

    try:
        resp = requests.get(
            "https://www.strava.com/api/v3/segments/explore",
            params={"bounds": bounds, "activity_type": "riding"},
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
    except (requests.RequestException, ValueError) as e:
        print(f"  [intelligence] Strava segments error: {e}")
        return []

    results = []
    for seg in data.get("segments", []):
        start = seg.get("start_latlng", [0, 0])
        results.append({
            "lat": start[0],
            "lng": start[1],
            "name": seg.get("name", ""),
            "athlete_count": seg.get("athlete_count", 0),
        })

    # Sort by popularity
    results.sort(key=lambda s: s["athlete_count"], reverse=True)
    return results


def get_ride_density(lat: float, lng: float, radius_km: float, days: int = 30, conn=None) -> dict:
    """Build a grid-based density map of recently ridden roads from GPS history.

    Returns dict mapping (grid_lat, grid_lng) → ride_count.
    Grid resolution: ~500m cells.
    """
    if conn is None:
        try:
            from veloai.db import get_connection
            conn = get_connection()
        except Exception:
            return {}

    if not conn:
        return {}

    grid_size = 0.005  # ~500m grid cells
    density = {}

    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT s.lat, s.lng
                FROM activity_streams s
                JOIN activities a ON a.id = s.activity_id
                WHERE a.date >= CURRENT_DATE - interval '%s days'
                  AND s.lat IS NOT NULL AND s.lng IS NOT NULL
                  AND s.time_offset %% 30 = 0
                  AND ABS(s.lat - %s) < %s AND ABS(s.lng - %s) < %s
            """, (days, lat, radius_km / 111.0 * 1.5, lng, radius_km / 80.0 * 1.5))
            for row in cur.fetchall():
                grid_key = (round(row[0] / grid_size) * grid_size, round(row[1] / grid_size) * grid_size)
                density[grid_key] = density.get(grid_key, 0) + 1
        print(f"  [intelligence] Ride density: {len(density)} grid cells from last {days} days")
    except Exception as e:
        print(f"  [intelligence] Ride density failed: {e}")

    return density


def _density_at(density: dict, lat: float, lng: float, grid_size: float = 0.005) -> float:
    """Get ride density score (0-1) at a location. Higher = more ridden."""
    grid_key = (round(lat / grid_size) * grid_size, round(lng / grid_size) * grid_size)
    count = density.get(grid_key, 0)
    # Normalize: 10+ visits = max density
    return min(1.0, count / 10.0)


def smart_waypoints(
    lat: float, lng: float, target_km: float, surface: str,
    max_waypoints: int = 3,
    strava_token: str | None = None,
    preference: str = "variety",
) -> list[dict]:
    """Generate intelligent waypoints using POIs, Strava segments, and ride history.

    Combines OSM POIs, Strava popular segments, and ride history density to
    place waypoints at interesting locations. Preference controls history bias:
    - "variety": penalize recently ridden areas (explore new roads)
    - "comfort": boost recently ridden areas (stick to known roads)

    Returns list of {lat, lng, name, reason} for use as Valhalla waypoints.
    """
    import math

    radius_km = (target_km / (2 * math.pi)) * 1.2  # slightly larger than route radius

    # Get POIs from OSM
    pois = get_pois(lat, lng, radius_km)
    print(f"  [intelligence] Found {len(pois)} POIs from OSM")

    # Get Strava segments if token available
    segments = []
    if strava_token:
        segments = get_strava_segments(lat, lng, radius_km, strava_token)
        print(f"  [intelligence] Found {len(segments)} Strava segments")

    # Get ride history density
    density = get_ride_density(lat, lng, radius_km)

    # Combine and score candidates
    candidates = []

    for poi in pois:
        dist = math.sqrt((poi["lat"] - lat) ** 2 + (poi["lng"] - lng) ** 2) * 111
        # Prefer POIs that are roughly at the route radius (not too close, not too far)
        ideal_dist = radius_km * 0.7
        dist_score = max(0, 1 - abs(dist - ideal_dist) / radius_km)

        # Type scoring: viewpoints and peaks > cafes > water > bike shops
        type_scores = {
            "tourism=viewpoint": 1.0,
            "natural=peak": 0.9,
            "amenity=cafe": 0.7,
            "amenity=drinking_water": 0.4,
            "amenity=bicycle_repair_station": 0.3,
        }
        type_score = type_scores.get(poi["type"], 0.5)

        # Ride history adjustment
        hist_density = _density_at(density, poi["lat"], poi["lng"])
        if preference == "variety":
            hist_score = 1.0 - hist_density  # penalize recently ridden
        else:
            hist_score = hist_density  # boost familiar areas

        base_score = dist_score * 0.4 + type_score * 0.3
        final_score = base_score + hist_score * 0.3 if density else dist_score * 0.6 + type_score * 0.4

        candidates.append({
            "lat": poi["lat"],
            "lng": poi["lng"],
            "name": poi["name"],
            "reason": f"POI: {poi['type'].split('=')[1]}",
            "score": final_score,
            "angle": math.atan2(poi["lat"] - lat, poi["lng"] - lng),
        })

    for seg in segments[:10]:  # top 10 by popularity
        dist = math.sqrt((seg["lat"] - lat) ** 2 + (seg["lng"] - lng) ** 2) * 111
        ideal_dist = radius_km * 0.7
        dist_score = max(0, 1 - abs(dist - ideal_dist) / radius_km)
        pop_score = min(1.0, seg["athlete_count"] / 500)  # normalize

        hist_density = _density_at(density, seg["lat"], seg["lng"])
        if preference == "variety":
            hist_score = 1.0 - hist_density
        else:
            hist_score = hist_density

        base_score = dist_score * 0.3 + pop_score * 0.4
        final_score = base_score + hist_score * 0.3 if density else dist_score * 0.4 + pop_score * 0.6

        candidates.append({
            "lat": seg["lat"],
            "lng": seg["lng"],
            "name": seg["name"],
            "reason": f"Popular segment ({seg['athlete_count']} cyclists)",
            "score": final_score,
            "angle": math.atan2(seg["lat"] - lat, seg["lng"] - lng),
        })

    if not candidates:
        return []

    # Select waypoints spread around the circle (avoid clustering)
    candidates.sort(key=lambda c: c["score"], reverse=True)
    selected = []
    used_angles = []

    for c in candidates:
        if len(selected) >= max_waypoints:
            break
        # Check angular separation (at least 45 degrees from any selected)
        angle = c["angle"]
        too_close = any(abs(angle - ua) < math.radians(45) for ua in used_angles)
        if too_close:
            continue
        selected.append({
            "lat": c["lat"],
            "lng": c["lng"],
            "name": c["name"],
            "reason": c["reason"],
        })
        used_angles.append(angle)

    # Sort by angle for sensible route ordering (clockwise)
    selected.sort(key=lambda w: math.atan2(w["lat"] - lat, w["lng"] - lng))

    return selected
