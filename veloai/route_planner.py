"""Route planner — builds Komoot route from structured parameters."""

import re
import sys
import webbrowser
from datetime import datetime, timedelta


# Surface type → Komoot sport mapping
KOMOOT_SPORTS = {
    "road": "racebike",
    "gravel": "touringbicycle",
    "mtb": "mtb",
}

# Default avg speeds (km/h) when no ride history available
DEFAULT_SPEEDS = {"road": 27, "gravel": 22, "mtb": 17}

# Surface multipliers against overall outdoor avg speed
SURFACE_MULTIPLIERS = {"road": 1.1, "gravel": 0.85, "mtb": 0.7}


def parse_duration(duration_str: str) -> int | None:
    """Parse duration string to minutes. Supports '2h', '1h30m', '90min', '1:30'."""
    if not duration_str:
        return None
    s = duration_str.lower().strip()

    # Match "1h30m", "2h", "30m"
    match = re.match(r'^(?:(\d+)h)?(?:(\d+)m(?:in)?)?$', s)
    if match and (match.group(1) or match.group(2)):
        hours = int(match.group(1) or 0)
        minutes = int(match.group(2) or 0)
        return hours * 60 + minutes

    # Match "90min"
    match = re.match(r'^(\d+)min$', s)
    if match:
        return int(match.group(1))

    # Match "1:30"
    match = re.match(r'^(\d+):(\d{2})$', s)
    if match:
        return int(match.group(1)) * 60 + int(match.group(2))

    return None


def resolve_date(date_str: str) -> str | None:
    """Resolve date string to YYYY-MM-DD. Supports 'tomorrow', day names, ISO dates."""
    if not date_str:
        return None
    s = date_str.lower().strip()
    today = datetime.now().date()

    if s == "today":
        return today.isoformat()
    elif s == "tomorrow":
        return (today + timedelta(days=1)).isoformat()

    # Day name (next occurrence)
    day_names = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
    if s in day_names:
        target_day = day_names.index(s)
        days_ahead = (target_day - today.weekday()) % 7
        if days_ahead == 0:
            days_ahead = 7  # next week if today
        return (today + timedelta(days=days_ahead)).isoformat()

    # ISO date
    try:
        datetime.strptime(s, "%Y-%m-%d")
        return s
    except ValueError:
        pass

    return None


def estimate_distance(duration_min: int, surface: str, avg_speed: float | None) -> float:
    """Estimate route distance (km) from duration and speed.
    Uses ride history avg_speed with surface multiplier, or defaults.
    """
    if avg_speed:
        speed = avg_speed * SURFACE_MULTIPLIERS.get(surface, 0.85)
    else:
        speed = DEFAULT_SPEEDS.get(surface, 22)
    return round(duration_min / 60 * speed, 1)


def adjust_for_fitness(distance_km: float, tsb: float | None) -> tuple:
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


def build_komoot_url(lat: float, lng: float, sport: str, waypoints: list = None) -> str:
    """Build Komoot planner URL with start point, sport type, and optional waypoints."""
    base = f"https://www.komoot.com/plan/@{lat},{lng},12/{sport}"
    if waypoints:
        wp_params = "&".join(f"wp={w['lat']},{w['lng']}" for w in waypoints)
        return f"{base}?{wp_params}"
    return base


def format_weather(day: dict) -> str:
    """Format weather for a single day."""
    parts = [day["weather"]]
    parts.append(f"{day['temp_min']:.0f}-{day['temp_max']:.0f}°C")
    parts.append(f"wind {day['wind']:.0f} km/h")
    if day["precip"] > 0:
        parts.append(f"rain {day['precip']:.1f}mm")
    return ", ".join(parts)


def format_output(duration_min: int, surface: str, distance_km: float,
                  fitness_note: str | None, weather_day: dict | None,
                  komoot_url: str, waypoint_names: list = None) -> str:
    """Format the route plan output."""
    lines = []

    title = f"{duration_min // 60}h{duration_min % 60:02d}m {surface.title()}"
    if waypoint_names:
        title += " via " + ", ".join(waypoint_names)
    lines.append(f"🗺 Route Plan: {title}")
    lines.append(f"  📏 ~{distance_km:.0f} km (estimated)")

    if weather_day:
        weather_str = format_weather(weather_day)
        lines.append(f"  🌤 {weather_str}")
        if weather_day["wind"] > 30:
            lines.append(f"  ⚠️ High wind — consider a sheltered route")
        if weather_day["precip"] > 5:
            lines.append(f"  ⚠️ Rain expected — check conditions")
        if weather_day["temp_max"] > 35:
            lines.append(f"  ⚠️ Heat warning — ride early or late")

    if fitness_note:
        lines.append(f"  💪 {fitness_note}")

    lines.append(f"  🔗 {komoot_url}")
    lines.append(f"")
    lines.append(f"  Save the route in Komoot → syncs to Karoo automatically")

    return "\n".join(lines)


def plan(duration_str: str, surface: str = "gravel", loop: bool = True,
         waypoints_str: str = None, date_str: str = "tomorrow",
         home_lat: float = 38.69, home_lng: float = -9.32) -> str:
    """Main entry point: plan a route and return formatted output.
    Opens Komoot planner URL in browser.
    """
    # Parse duration
    duration_min = parse_duration(duration_str)
    if not duration_min:
        return f"Error: could not parse duration '{duration_str}'. Use format like '2h', '1h30m', '90min'"

    # Resolve date
    ride_date = resolve_date(date_str)

    # Get fitness + avg speed from DB (graceful degradation)
    avg_speed = None
    fitness = {}
    try:
        from veloai.db import get_connection, get_latest_fitness, get_avg_speed
        conn = get_connection()
        if conn:
            try:
                avg_speed = get_avg_speed(conn)
                fitness = get_latest_fitness(conn)
                if avg_speed:
                    print(f"  → Avg outdoor speed: {avg_speed} km/h", file=sys.stderr)
            finally:
                conn.close()
    except Exception as e:
        print(f"  DB unavailable ({e}), using defaults", file=sys.stderr)

    # Estimate distance
    distance_km = estimate_distance(duration_min, surface, avg_speed)

    # Fitness adjustment
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

    # Geocode waypoints
    geocoded_waypoints = []
    waypoint_names = []
    if waypoints_str:
        from veloai.geocode import geocode_many
        places = [p.strip() for p in waypoints_str.split(",")]
        geocoded_waypoints = geocode_many(places, home_lat, home_lng)
        waypoint_names = [g["display_name"].split(",")[0] for g in geocoded_waypoints]
        if len(geocoded_waypoints) < len(places):
            skipped = len(places) - len(geocoded_waypoints)
            print(f"  ⚠️ {skipped} waypoint(s) could not be geocoded", file=sys.stderr)

    # Build Komoot URL
    komoot_sport = KOMOOT_SPORTS.get(surface, "touringbicycle")
    komoot_url = build_komoot_url(home_lat, home_lng, komoot_sport, geocoded_waypoints)

    # Open in browser
    print("  Opening Komoot planner...", file=sys.stderr)
    webbrowser.open(komoot_url)

    # Format output
    return format_output(
        duration_min, surface, distance_km,
        fitness_note, weather_day, komoot_url, waypoint_names,
    )
