"""Route planner — generates a cycling GPX route with weather and intelligence enrichment."""

import re
import sys
from datetime import datetime, timedelta


# Default avg speeds (km/h) when no ride history available
DEFAULT_SPEEDS = {"road": 27, "gravel": 22, "mtb": 17}

# Surface multipliers against overall outdoor avg speed

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
        if hour > 23:
            return None
        return f"{hour:02d}:00"

    return None


def estimate_distance(duration_min: int, surface: str, avg_speed: float | None) -> float:
    """Estimate route distance (km) from duration and speed.
    avg_speed is already surface-specific from DB, so no multiplier needed.
    Falls back to DEFAULT_SPEEDS when no ride history is available.
    """
    if avg_speed:
        speed = float(avg_speed)
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


def _analyze_wind(coords: list, wind_dir: float, wind_speed: float) -> str | None:
    """Analyze route exposure to wind. Returns a warning string or None.

    wind_dir: degrees (0=N, 90=E, 180=S, 270=W) — direction wind comes FROM.
    """
    import math
    if not coords or len(coords) < 10 or wind_speed < 15:
        return None

    # Calculate bearing of each segment
    headwind_count = 0
    crosswind_count = 0
    total = 0
    step = max(1, len(coords) // 50)

    for i in range(0, len(coords) - step, step):
        lat1, lng1 = coords[i]
        lat2, lng2 = coords[i + step]
        # Route bearing (direction of travel)
        dlng = lng2 - lng1
        dlat = lat2 - lat1
        if abs(dlng) < 1e-7 and abs(dlat) < 1e-7:
            continue
        bearing = math.degrees(math.atan2(dlng, dlat)) % 360

        # Angle between wind direction and route bearing
        # Wind comes FROM wind_dir, so headwind = riding INTO the wind
        angle_diff = abs((bearing - wind_dir + 180) % 360 - 180)

        total += 1
        if angle_diff < 45:
            headwind_count += 1
        elif angle_diff > 135:
            pass  # tailwind, good
        else:
            crosswind_count += 1

    if total == 0:
        return None

    headwind_pct = headwind_count / total * 100
    crosswind_pct = crosswind_count / total * 100

    parts = []
    if headwind_pct > 40 and wind_speed > 25:
        parts.append(f"strong headwind on {headwind_pct:.0f}% of route")
    elif headwind_pct > 30 and wind_speed > 20:
        parts.append(f"headwind on {headwind_pct:.0f}% of route")
    if crosswind_pct > 50 and wind_speed > 25:
        parts.append(f"exposed crosswind on {crosswind_pct:.0f}%")

    if parts:
        wind_from = ["N", "NE", "E", "SE", "S", "SW", "W", "NW"][int((wind_dir + 22.5) % 360 / 45)]
        return f"Wind from {wind_from} at {wind_speed:.0f} km/h — {', '.join(parts)}"
    return None


def format_weather(day: dict) -> str:
    """Format weather for a single day."""
    parts = [day["weather"]]
    parts.append(f"{day['temp_min']:.0f}-{day['temp_max']:.0f}°C")
    parts.append(f"wind {day['wind']:.0f} km/h")
    if day.get("uv_max", 0) >= 6:
        parts.append(f"UV {day['uv_max']:.0f}")
    if day["precip"] > 0:
        parts.append(f"rain {day['precip']:.1f}mm")
    return ", ".join(parts)


def _get_strava_token() -> str | None:
    """Get a Strava access token using refresh token from config. Returns None if not configured."""
    try:
        from velomate.config import load as load_config
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


def parse_distance(distance_str: str) -> float | None:
    """Parse distance string to km. Supports '30', '50km', '25.5'."""
    if not distance_str:
        return None
    s = distance_str.lower().strip().rstrip("km").strip()
    try:
        return float(s)
    except ValueError:
        return None


def plan(duration_str: str = None, distance_str: str = None,
         surface: str = "road", loop: bool = True,
         waypoints_str: str = None, date_str: str = "tomorrow",
         time_str: str = None,
         home_lat: float = None, home_lng: float = None,
         preference: str = "variety",
         safety: float = 0.5,
         output_dir: str = None) -> str:
    """Generate a cycling route. Accepts either duration or distance."""
    safety = max(0.0, min(1.0, safety))

    # Parse duration or distance
    duration_min = parse_duration(duration_str) if duration_str else None
    target_distance = parse_distance(distance_str) if distance_str else None

    if not duration_min and not target_distance:
        return "Error: provide --duration (e.g. 2h) or --distance (e.g. 30km)"

    ride_date = resolve_date(date_str)
    ride_time = parse_time(time_str)

    # Get fitness + avg speed from DB
    avg_speed = None
    fitness = {}
    try:
        from velomate.db import get_connection, get_latest_fitness, get_avg_speed
        conn = get_connection()
        if conn:
            try:
                avg_speed = get_avg_speed(conn, surface=surface)
                fitness = get_latest_fitness(conn)
            finally:
                conn.close()
    except Exception as e:
        print(f"  DB unavailable ({e}), using defaults", file=sys.stderr)

    # Determine target distance
    if target_distance:
        distance_km = target_distance
        # Estimate duration for display
        speed = float(avg_speed) if avg_speed else DEFAULT_SPEEDS.get(surface, 22)
        duration_min = int(target_distance / speed * 60) if speed > 0 else 60
    else:
        distance_km = estimate_distance(duration_min, surface, avg_speed)
    distance_km, fitness_note = adjust_for_fitness(distance_km, fitness.get("tsb"))

    # Weather check
    weather_day = None
    if ride_date:
        try:
            from velomate import weather
            forecast = weather.fetch_forecast(home_lat, home_lng)
            for day in forecast:
                if day["date"] == ride_date:
                    weather_day = day
                    break
        except Exception as e:
            print(f"  [route] {e}", file=sys.stderr)


    # Geocode explicit waypoints, or use smart waypoints from route intelligence
    waypoint_names = []
    valhalla_waypoints = []
    preview_waypoints = []  # full data for map preview
    if waypoints_str:
        from velomate.geocode import geocode_many
        places = [p.strip() for p in waypoints_str.split(",")]
        geocoded = geocode_many(places, home_lat, home_lng)
        waypoint_names = [g["display_name"].split(",")[0] for g in geocoded]
        valhalla_waypoints = [{"lat": g["lat"], "lon": g["lng"]} for g in geocoded]
        preview_waypoints = [{"lat": g["lat"], "lng": g["lng"], "name": g["display_name"].split(",")[0], "reason": "user waypoint"} for g in geocoded]
    else:
        # No explicit waypoints — use route intelligence for smart placement
        try:
            from velomate.route_intelligence import smart_waypoints
            strava_token = _get_strava_token()
            smart = smart_waypoints(home_lat, home_lng, distance_km, surface, max_waypoints=3, strava_token=strava_token, preference=preference)
            if smart:
                waypoint_names = [w["name"] for w in smart]
                valhalla_waypoints = [{"lat": w["lat"], "lon": w["lng"]} for w in smart]
                preview_waypoints = smart  # already has lat, lng, name, reason
                desc = ', '.join(w["name"] + ' (' + w["reason"] + ')' for w in smart)
                print(f"  Smart waypoints: {desc}", file=sys.stderr)
        except Exception as e:
            print(f"  [intelligence] Skipped: {e}", file=sys.stderr)

    # Generate real GPX route via Valhalla
    if target_distance:
        route_name = f"VeloMate {target_distance:.0f}km {surface.title()}"
    else:
        route_name = f"VeloMate {duration_min // 60}h{duration_min % 60:02d}m {surface.title()}"
    if waypoint_names:
        route_name += " via " + ", ".join(waypoint_names)

    print(f"  Generating {distance_km:.0f}km {surface} route via Valhalla...", file=sys.stderr)

    from velomate.route_generator import generate
    result = generate(
        start_lat=home_lat,
        start_lng=home_lng,
        target_km=distance_km,
        surface=surface,
        name=route_name,
        waypoints=valhalla_waypoints if valhalla_waypoints else None,
        safety=safety,
    )

    if "error" in result:
        return f"Route generation failed: {result['error']}"

    gpx_path = result["gpx_path"]
    actual_km = result["actual_km"]
    print(f"  GPX generated: {actual_km:.1f}km, {len(result['coords'])} points", file=sys.stderr)

    # Verify surface matches requested type
    surface_check = {}
    try:
        from velomate.route_intelligence import verify_surface
        surface_check = verify_surface(result["coords"], surface)
        if surface_check["surfaces"]:
            breakdown = ', '.join(f'{s} {p}%' for s, p in list(surface_check["surfaces"].items())[:4])
            print(f"  Surface: {breakdown} (match: {surface_check['match_pct']}%)", file=sys.stderr)
        if surface_check["warning"]:
            print(f"  ⚠️ {surface_check['warning']}", file=sys.stderr)
    except Exception as e:
        print(f"  [surface] Skipped: {e}", file=sys.stderr)

    # Score scenic value, elevation profile, cycling trails
    scenic_info = {}
    elevation_info = {}
    trails = []
    try:
        from velomate.route_intelligence import score_scenic, get_elevation_profile, find_cycling_trails
        scenic_info = score_scenic(result["coords"])
        if scenic_info.get("features"):
            print(f"  Scenic: {', '.join(scenic_info['features'])} (score: {scenic_info['scenic_score']}/100)", file=sys.stderr)
        elevation_info = get_elevation_profile(result["coords"])
        if elevation_info.get("total_climb"):
            print(f"  Elevation: +{elevation_info['total_climb']}m / -{elevation_info['total_descent']}m, max gradient {elevation_info['max_gradient']}%", file=sys.stderr)
        trails = find_cycling_trails(result["coords"])
        if trails:
            print(f"  Trails: {', '.join(trails)}", file=sys.stderr)
    except Exception as e:
        print(f"  [enrichment] {e}", file=sys.stderr)

    # Score cycling safety infrastructure
    safety_info = {}
    try:
        from velomate.route_intelligence import score_cycling_safety
        safety_info = score_cycling_safety(result["coords"])
        if safety_info.get("details"):
            print(f"  Cycling safety: {safety_info['details']} (score: {safety_info['safety_score']}/100)", file=sys.stderr)
    except Exception as e:
        print(f"  [safety] Skipped: {e}", file=sys.stderr)

    # Compute best ride hours once (used for preview, wind analysis, and output)
    best_hours = None
    if ride_date and weather_day and weather_day.get("hourly"):
        try:
            from velomate.weather import best_ride_hours
            best_hours = best_ride_hours(weather_day["hourly"], ride_date)
        except Exception:
            pass

    # Show route preview in browser
    html_path = None
    try:
        from velomate.map_preview import preview
        wp_for_preview = preview_waypoints if preview_waypoints else None

        # Collect best time info
        best_time_info = None
        if best_hours:
            top = best_hours[0]
            best_time_info = {"hour": top["time"][11:16], "temp": top["temp"], "wind": top["wind"], "uv": top["uv"]}

        # Collect sun info
        sun_info = None
        if ride_date and home_lat:
            try:
                from velomate.weather import fetch_sunrise_sunset
                sun_info = fetch_sunrise_sunset(home_lat, home_lng, ride_date)
            except Exception:
                pass

        html_path = preview(result["coords"], route_name, wp_for_preview, route_info={
            "distance_km": actual_km,
            "duration_min": duration_min,
            "elevation": elevation_info,
            "scenic": scenic_info,
            "surface": surface_check,
            "safety": safety_info,
            "weather": weather_day,
            "fitness": fitness_note,
            "best_time": best_time_info,
            "sun": sun_info,
            "trails": trails,
            "gpx_path": gpx_path,
        }, output_dir=output_dir)
        if not output_dir:
            print(f"  Route preview opened in browser", file=sys.stderr)
    except Exception as e:
        html_path = None
        print(f"  [preview] Skipped: {e}", file=sys.stderr)

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

    if surface_check.get("surfaces"):
        breakdown = ', '.join(f'{s} {p}%' for s, p in list(surface_check["surfaces"].items())[:4])
        lines.append(f"  🛤 Surface: {breakdown}")
    if surface_check.get("warning"):
        lines.append(f"  ⚠️ {surface_check['warning']}")

    if elevation_info.get("total_climb"):
        lines.append(f"  ⛰ Climb: +{elevation_info['total_climb']}m / -{elevation_info['total_descent']}m (max gradient {elevation_info['max_gradient']}%)")

    if scenic_info.get("features"):
        lines.append(f"  🌿 Scenic: {', '.join(scenic_info['features'])} ({scenic_info['scenic_score']}/100)")

    if safety_info.get("details"):
        lines.append(f"  🛡 Safety: {safety_info['details']} ({safety_info['safety_score']}/100)")

    if trails:
        lines.append(f"  🚲 Trails: {', '.join(trails)}")

    if weather_day:
        lines.append(f"  🌤 {format_weather(weather_day)}")
        if weather_day["wind"] > 30:
            lines.append(f"  ⚠️ High wind — consider a sheltered route")

        # Wind direction analysis against route
        if best_hours and result.get("coords"):
            wind_warning = _analyze_wind(result["coords"], best_hours[0]["wind_dir"], best_hours[0]["wind"])
            if wind_warning:
                lines.append(f"  💨 {wind_warning}")

        if weather_day["precip"] > 5:
            lines.append(f"  ⚠️ Rain expected — check conditions")
        if weather_day.get("uv_max", 0) >= 8:
            lines.append(f"  ⚠️ Very high UV ({weather_day['uv_max']:.0f}) — wear sunscreen, ride early or late")
        elif weather_day.get("uv_max", 0) >= 6:
            lines.append(f"  ☀️ High UV ({weather_day['uv_max']:.0f}) — sunscreen recommended")
        if weather_day["temp_max"] > 35:
            lines.append(f"  ⚠️ Extreme heat — ride before 9am or after 6pm")
        elif weather_day["temp_max"] > 30:
            lines.append(f"  ☀️ Hot day — consider an early morning ride")

        # Suggest best ride time
        if best_hours:
            top = best_hours[0]
            hour = top["time"][11:16]
            lines.append(f"  🕐 Best time: {hour} ({top['temp']:.0f}°C, wind {top['wind']:.0f} km/h, UV {top['uv']:.0f})")


    # Air quality
    if ride_date and home_lat:
        try:
            from velomate.weather import fetch_air_quality
            aqi = fetch_air_quality(home_lat, home_lng, ride_date)
            if aqi and aqi.get("aqi"):
                aqi_val = aqi["aqi"]
                if aqi_val > 100:
                    lines.append(f"  ⚠️ Poor air quality (AQI {aqi_val}) — consider indoor ride")
                elif aqi_val > 50:
                    lines.append(f"  😷 Moderate air quality (AQI {aqi_val})")
        except Exception as e:
            print(f"  [aqi] {e}", file=sys.stderr)

    # Sunrise/sunset display
    # Note: sunrise-sunset.org returns UTC times; ride_time is local.
    # We display times but don't compare them — timezone mismatch would give wrong warnings.
    if ride_date and home_lat:
        try:
            from velomate.weather import fetch_sunrise_sunset
            sun = fetch_sunrise_sunset(home_lat, home_lng, ride_date)
            if sun:
                tz_label = sun.get("tz_label", "UTC")
                lines.append(f"  🌅 Sunrise {sun['sunrise']}, sunset {sun['sunset']} ({tz_label})")
        except Exception as e:
            print(f"  [sun] {e}", file=sys.stderr)

    if fitness_note:
        lines.append(f"  💪 {fitness_note}")

    lines.append(f"  💾 GPX: {gpx_path}")
    if output_dir and html_path:
        lines.append(f"  🌐 Preview: {html_path}")

    return "\n".join(lines)
