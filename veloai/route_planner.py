"""Route planner — generates a real cycling GPX route and uploads it to Komoot."""

import re
import sys
from datetime import datetime, timedelta


# Default avg speeds (km/h) when no ride history available
DEFAULT_SPEEDS = {"road": 27, "gravel": 22, "mtb": 17}

# Surface multipliers against overall outdoor avg speed
SURFACE_MULTIPLIERS = {"road": 1.1, "gravel": 0.85, "mtb": 0.7}


def parse_duration(duration_str: str) -> int | None:
    """Parse duration string to minutes. Supports '2h', '1h30m', '90min', '1:30'."""
    if not duration_str:
        return None
    s = duration_str.lower().strip()

    match = re.match(r'^(?:(\d+)h)?(?:(\d+)m(?:in)?)?$', s)
    if match and (match.group(1) or match.group(2)):
        hours = int(match.group(1) or 0)
        minutes = int(match.group(2) or 0)
        return hours * 60 + minutes

    match = re.match(r'^(\d+)min$', s)
    if match:
        return int(match.group(1))

    match = re.match(r'^(\d+):(\d{2})$', s)
    if match:
        return int(match.group(1)) * 60 + int(match.group(2))

    return None


def resolve_date(date_str: str) -> str | None:
    """Resolve date string to YYYY-MM-DD."""
    if not date_str:
        return None
    s = date_str.lower().strip()
    today = datetime.now().date()

    if s == "today":
        return today.isoformat()
    elif s == "tomorrow":
        return (today + timedelta(days=1)).isoformat()

    day_names = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
    if s in day_names:
        target_day = day_names.index(s)
        days_ahead = (target_day - today.weekday()) % 7
        if days_ahead == 0:
            days_ahead = 7
        return (today + timedelta(days=days_ahead)).isoformat()

    try:
        datetime.strptime(s, "%Y-%m-%d")
        return s
    except ValueError:
        pass

    return None


def parse_time(time_str: str) -> str | None:
    """Parse time string to HH:MM. Supports '14:00', '2pm', '9am', '14h'."""
    if not time_str:
        return None
    s = time_str.lower().strip()

    match = re.match(r'^(\d{1,2}):(\d{2})$', s)
    if match:
        return f"{int(match.group(1)):02d}:{match.group(2)}"

    match = re.match(r'^(\d{1,2})\s*(am|pm|h)$', s)
    if match:
        hour = int(match.group(1))
        suffix = match.group(2)
        if suffix == "pm" and hour < 12:
            hour += 12
        elif suffix == "am" and hour == 12:
            hour = 0
        return f"{hour:02d}:00"

    return None


def estimate_distance(duration_min: int, surface: str, avg_speed: object) -> float:
    """Estimate route distance (km) from duration and speed."""
    if avg_speed:
        speed = float(avg_speed) * SURFACE_MULTIPLIERS.get(surface, 0.85)
    else:
        speed = DEFAULT_SPEEDS.get(surface, 22)
    return round(duration_min / 60 * speed, 1)


def adjust_for_fitness(distance_km: float, tsb: float | None) -> tuple[float, str | None]:
    """Adjust distance based on TSB. Returns (adjusted_distance, note)."""
    if tsb is None:
        return distance_km, None
    tsb = float(tsb)
    if tsb > 10:
        return distance_km, "fresh (TSB +{:.0f}) — good day to push".format(tsb)
    elif tsb > -10:
        return distance_km, "neutral (TSB {:+.0f})".format(tsb)
    else:
        adjusted = round(distance_km * 0.8, 1)
        return adjusted, "fatigued (TSB {:.0f}) — reduced to {}km".format(tsb, adjusted)


def format_weather(day: dict) -> str:
    """Format weather for a single day."""
    parts = [day["weather"]]
    parts.append(f"{day['temp_min']:.0f}-{day['temp_max']:.0f}°C")
    parts.append(f"wind {day['wind']:.0f} km/h")
    if day["precip"] > 0:
        parts.append(f"rain {day['precip']:.1f}mm")
    return ", ".join(parts)


def _get_strava_token() -> str | None:
    """Get a Strava access token using refresh token from config. Returns None if not configured."""
    try:
        from veloai.config import load as load_config
        import requests
        cfg = load_config()
        strava = cfg.get("strava", {})
        client_id = strava.get("client_id", "")
        client_secret = strava.get("client_secret", "")
        refresh_token = strava.get("refresh_token", "")
        if not all([client_id, client_secret, refresh_token]):
            return None
        resp = requests.post("https://www.strava.com/oauth/token", data={
            "client_id": client_id,
            "client_secret": client_secret,
            "refresh_token": refresh_token,
            "grant_type": "refresh_token",
        }, timeout=10)
        resp.raise_for_status()
        return resp.json().get("access_token")
    except Exception as e:
        print(f"  [strava] Token refresh failed: {e}", file=sys.stderr)
        return None


def _upload_to_komoot(gpx_path: str, surface: str, name: str) -> str | None:
    """Upload GPX to Komoot as a planned route (not recorded). Returns tour URL or None."""
    try:
        import requests as _requests
        from komPYoot.api import API, Sport
        from veloai.config import load as load_config

        cfg = load_config()
        komoot_cfg = cfg["komoot"]
        if not komoot_cfg.get("email") or not komoot_cfg.get("password"):
            print("  Komoot credentials not configured — skipping upload", file=sys.stderr)
            return None
        api = API()
        api.login(komoot_cfg["email"], komoot_cfg["password"])

        sport_map = {
            "road":   "racebike",
            "gravel": "touringbicycle",
            "mtb":    "mtb",
        }
        sport_flag = sport_map.get(surface, "touringbicycle")

        # Upload as planned route (type=tour_planned) instead of recorded activity
        with open(gpx_path, "rb") as f:
            gpx_data = f.read()
        resp = _requests.post(
            "https://api.komoot.de/v007/tours/",
            params={"data_type": "gpx", "sport": sport_flag, "type": "tour_planned"},
            headers={"User-Agent": "VeloAI"},
            data=gpx_data,
            auth=(api.user_details["user_id"], api.user_details["token"]),
            timeout=30,
        )
        ok = resp.status_code in (200, 201, 202)
        if ok:
            tour_id = resp.json().get("id")
            print(f"  Planned route created (ID: {tour_id})", file=sys.stderr)
            if tour_id:
                return f"https://www.komoot.com/tour/{tour_id}"
            return "https://www.komoot.com/user/tours"
        else:
            print(f"  Komoot upload failed: HTTP {resp.status_code}", file=sys.stderr)
    except Exception as e:
        print(f"  Komoot upload failed: {e}", file=sys.stderr)
    return None


def plan(duration_str: str, surface: str = "gravel", loop: bool = True,
         waypoints_str: str = None, date_str: str = "tomorrow",
         time_str: str = None,
         home_lat: float = None, home_lng: float = None,
         upload: bool = True, preference: str = "variety") -> str:
    """Generate a real cycling route, upload to Komoot, return summary."""

    # Parse duration
    duration_min = parse_duration(duration_str)
    if not duration_min:
        return f"Error: could not parse duration '{duration_str}'. Use format like '2h', '1h30m', '90min'"

    ride_date = resolve_date(date_str)
    ride_time = parse_time(time_str)

    # Get fitness + avg speed from DB
    avg_speed = None
    fitness = {}
    try:
        from veloai.db import get_connection, get_latest_fitness, get_avg_speed
        conn = get_connection()
        if conn:
            try:
                avg_speed = get_avg_speed(conn)
                fitness = get_latest_fitness(conn)
            finally:
                conn.close()
    except Exception as e:
        print(f"  DB unavailable ({e}), using defaults", file=sys.stderr)

    # Estimate target distance
    distance_km = estimate_distance(duration_min, surface, avg_speed)
    distance_km, fitness_note = adjust_for_fitness(distance_km, fitness.get("tsb"))

    # Weather check
    weather_day = None
    if ride_date:
        try:
            from veloai import weather
            forecast = weather.fetch_forecast(home_lat, home_lng)
            for day in forecast:
                if day["date"] == ride_date:
                    weather_day = day
                    break
        except Exception:
            pass

    # Geocode explicit waypoints, or use smart waypoints from route intelligence
    waypoint_names = []
    valhalla_waypoints = []
    if waypoints_str:
        from veloai.geocode import geocode_many
        places = [p.strip() for p in waypoints_str.split(",")]
        geocoded = geocode_many(places, home_lat, home_lng)
        waypoint_names = [g["display_name"].split(",")[0] for g in geocoded]
        valhalla_waypoints = [{"lat": g["lat"], "lon": g["lng"]} for g in geocoded]
    else:
        # No explicit waypoints — use route intelligence for smart placement
        try:
            from veloai.route_intelligence import smart_waypoints
            strava_token = _get_strava_token()
            smart = smart_waypoints(home_lat, home_lng, distance_km, surface, max_waypoints=3, strava_token=strava_token, preference=preference)
            if smart:
                waypoint_names = [w["name"] for w in smart]
                valhalla_waypoints = [{"lat": w["lat"], "lon": w["lng"]} for w in smart]
                desc = ', '.join(w["name"] + ' (' + w["reason"] + ')' for w in smart)
                print(f"  Smart waypoints: {desc}", file=sys.stderr)
        except Exception as e:
            print(f"  [intelligence] Skipped: {e}", file=sys.stderr)

    # Generate real GPX route via Valhalla
    route_name = f"VeloAI {duration_min // 60}h{duration_min % 60:02d}m {surface.title()}"
    if waypoint_names:
        route_name += " via " + ", ".join(waypoint_names)

    print(f"  Generating {distance_km:.0f}km {surface} route via Valhalla...", file=sys.stderr)

    from veloai.route_generator import generate
    result = generate(
        start_lat=home_lat,
        start_lng=home_lng,
        target_km=distance_km,
        surface=surface,
        name=route_name,
        waypoints=valhalla_waypoints if valhalla_waypoints else None,
    )

    if "error" in result:
        return f"Route generation failed: {result['error']}"

    gpx_path = result["gpx_path"]
    actual_km = result["actual_km"]
    print(f"  GPX generated: {actual_km:.1f}km, {len(result['coords'])} points", file=sys.stderr)

    # Show route preview in browser
    try:
        from veloai.map_preview import preview
        wp_for_preview = [{"lat": w["lat"], "lng": w.get("lng", w.get("lon")), "name": w.get("name", ""), "reason": w.get("reason", "")} for w in (valhalla_waypoints if valhalla_waypoints else [])] if waypoint_names else None
        preview(result["coords"], route_name, wp_for_preview)
        print(f"  Route preview opened in browser", file=sys.stderr)
    except Exception as e:
        print(f"  [preview] Skipped: {e}", file=sys.stderr)

    # Upload to Komoot (optional)
    komoot_url = None
    if upload:
        print(f"  Uploading to Komoot...", file=sys.stderr)
        komoot_url = _upload_to_komoot(gpx_path, surface, route_name)
    else:
        print(f"  Skipping Komoot upload (--no-upload)", file=sys.stderr)

    # Build output
    lines = []
    lines.append(f"🗺 *{route_name}*")
    lines.append(f"  📏 {actual_km:.0f} km")

    if ride_date or ride_time:
        when_parts = []
        if ride_date:
            when_parts.append(ride_date)
        if ride_time:
            when_parts.append(ride_time)
        lines.append(f"  📅 {' at '.join(when_parts)}")

    if weather_day:
        lines.append(f"  🌤 {format_weather(weather_day)}")
        if weather_day["wind"] > 30:
            lines.append(f"  ⚠️ High wind — route goes inland where possible")
        if weather_day["precip"] > 5:
            lines.append(f"  ⚠️ Rain expected — check conditions")
        if weather_day["temp_max"] > 35:
            lines.append(f"  ⚠️ Heat warning — ride early")

    if fitness_note:
        lines.append(f"  💪 {fitness_note}")

    if komoot_url:
        lines.append(f"  🔗 {komoot_url}")
        lines.append(f"  Uploaded to Komoot — change to 'Planned' in the app if needed")
    else:
        lines.append(f"  💾 GPX saved: {gpx_path}")
        if not upload:
            lines.append(f"  Preview only — use --no-upload to skip, or remove flag to upload")
        else:
            lines.append(f"  Import manually: Komoot → + → Import GPX")

    return "\n".join(lines)
