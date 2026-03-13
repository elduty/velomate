# ARCHIVED — superseded by the veloai/ package
#!/usr/bin/env python3
"""
Ride Planner — recommends the best days and routes for Marcin's cycling.
Pulls data from Komoot (past routes), Strava (recent fitness), and Open-Meteo (weather).
"""

import json
import subprocess
import sys
import warnings
from datetime import datetime, timedelta
from collections import defaultdict

import requests

warnings.filterwarnings("ignore")

# ── Config ──────────────────────────────────────────────────────────────
LOCATION = {"lat": 38.69, "lon": -9.32, "name": "São Domingos de Rana"}
STRAVA_ATHLETE_ID = 204728438
WEATHER_URL = (
    "https://api.open-meteo.com/v1/forecast"
    "?latitude={lat}&longitude={lon}"
    "&daily=precipitation_sum,windspeed_10m_max,temperature_2m_max,temperature_2m_min,weathercode"
    "&timezone=Europe/Lisbon&forecast_days=7"
)

# WMO weather codes → human labels
WMO_CODES = {
    0: "Clear sky", 1: "Mainly clear", 2: "Partly cloudy", 3: "Overcast",
    45: "Foggy", 48: "Depositing rime fog",
    51: "Light drizzle", 53: "Moderate drizzle", 55: "Dense drizzle",
    56: "Light freezing drizzle", 57: "Dense freezing drizzle",
    61: "Slight rain", 63: "Moderate rain", 65: "Heavy rain",
    66: "Light freezing rain", 67: "Heavy freezing rain",
    71: "Slight snow", 73: "Moderate snow", 75: "Heavy snow",
    77: "Snow grains", 80: "Slight rain showers", 81: "Moderate rain showers",
    82: "Violent rain showers", 85: "Slight snow showers", 86: "Heavy snow showers",
    95: "Thunderstorm", 96: "Thunderstorm + slight hail", 99: "Thunderstorm + heavy hail",
}

DAY_NAMES = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]


# ── Helpers ─────────────────────────────────────────────────────────────

def get_keychain(service):
    """Retrieve JSON credentials from macOS Keychain."""
    raw = subprocess.check_output(
        ["security", "find-generic-password", "-a", "openclaw", "-s", service, "-w"]
    ).decode().strip()
    return json.loads(raw)


def score_weather(precip, wind, temp_max, code):
    """Score a day for cycling (0–100, higher = better)."""
    score = 100

    # Rain penalty
    if precip > 10:
        score -= 50
    elif precip > 5:
        score -= 35
    elif precip > 1:
        score -= 20
    elif precip > 0:
        score -= 5

    # Wind penalty (km/h)
    if wind > 40:
        score -= 40
    elif wind > 30:
        score -= 25
    elif wind > 20:
        score -= 10

    # Temperature comfort (ideal: 15–25°C)
    if temp_max < 5 or temp_max > 38:
        score -= 30
    elif temp_max < 10 or temp_max > 35:
        score -= 15
    elif temp_max < 13:
        score -= 5

    # Bad weather codes
    if code >= 61:  # rain or worse
        score -= 15
    elif code >= 45:  # fog
        score -= 10

    return max(0, score)


# ── Data Fetchers ───────────────────────────────────────────────────────

def fetch_komoot_tours():
    """Fetch all recorded cycling tours from Komoot."""
    creds = get_keychain("openclaw/komoot")
    from komPYoot import API
    api = API()
    if not api.login(creds["email"], creds["password"]):
        print("⚠️  Komoot login failed")
        return []

    all_tours = api.get_user_tours_list()

    # Filter to cycling only
    cycling_sports = {"touringbicycle", "mtb", "racebike", "roadcycling", "gravelbike", "e_touringbicycle"}
    cycling = [t for t in all_tours if t.get("sport") in cycling_sports]

    # Deduplicate by grouping similar routes (same date, similar distance)
    seen = set()
    unique = []
    for t in cycling:
        date = t["date"][:10]
        dist_bucket = round(t["distance"] / 1000)  # km, rounded
        key = (date, dist_bucket)
        if key not in seen:
            seen.add(key)
            unique.append(t)

    return unique


def fetch_weather():
    """Fetch 7-day weather forecast."""
    url = WEATHER_URL.format(lat=LOCATION["lat"], lon=LOCATION["lon"])
    r = requests.get(url, timeout=10)
    r.raise_for_status()
    data = r.json()["daily"]

    forecast = []
    for i, date_str in enumerate(data["time"]):
        dt = datetime.strptime(date_str, "%Y-%m-%d")
        forecast.append({
            "date": date_str,
            "day_name": DAY_NAMES[dt.weekday()],
            "temp_max": data["temperature_2m_max"][i],
            "temp_min": data["temperature_2m_min"][i],
            "precip": data["precipitation_sum"][i],
            "wind": data["windspeed_10m_max"][i],
            "code": data["weathercode"][i],
            "weather": WMO_CODES.get(data["weathercode"][i], "Unknown"),
            "score": score_weather(
                data["precipitation_sum"][i],
                data["windspeed_10m_max"][i],
                data["temperature_2m_max"][i],
                data["weathercode"][i],
            ),
        })
    return forecast


def fetch_strava_activities():
    """Fetch recent Strava activities (last 4 weeks)."""
    creds = get_keychain("openclaw/strava")

    # Refresh token
    r = requests.post("https://www.strava.com/oauth/token", data={
        "client_id": creds["client_id"],
        "client_secret": creds["client_secret"],
        "refresh_token": creds["refresh_token"],
        "grant_type": "refresh_token",
    }, timeout=10)
    r.raise_for_status()
    access_token = r.json()["access_token"]

    # Fetch activities from last 4 weeks
    after = int((datetime.utcnow() - timedelta(weeks=4)).timestamp())
    headers = {"Authorization": f"Bearer {access_token}"}
    r2 = requests.get(
        "https://www.strava.com/api/v3/athlete/activities",
        headers=headers,
        params={"per_page": 50, "after": after},
        timeout=10,
    )
    r2.raise_for_status()
    return r2.json()


# ── Analysis ────────────────────────────────────────────────────────────

def analyze_fitness(strava_activities):
    """Analyze recent activity to gauge fitness level."""
    rides = [a for a in strava_activities if a.get("type") in ("Ride", "VirtualRide")]
    real_rides = [a for a in rides if a.get("type") == "Ride" and a.get("distance", 0) > 1000]
    virtual_rides = [a for a in rides if a.get("type") == "VirtualRide"]

    total_distance = sum(a.get("distance", 0) for a in rides) / 1000
    total_elevation = sum(a.get("total_elevation_gain", 0) for a in rides)
    ride_count = len(rides)

    # Determine fitness level
    if ride_count >= 6 and total_distance > 100:
        level = "active"
        max_distance = 50
        max_elevation = 600
    elif ride_count >= 3 and total_distance > 40:
        level = "moderate"
        max_distance = 35
        max_elevation = 450
    elif ride_count >= 1:
        level = "getting back into it"
        max_distance = 25
        max_elevation = 350
    else:
        level = "fresh start"
        max_distance = 20
        max_elevation = 250

    return {
        "level": level,
        "ride_count": ride_count,
        "real_rides": len(real_rides),
        "virtual_rides": len(virtual_rides),
        "total_distance_km": round(total_distance, 1),
        "total_elevation_m": round(total_elevation),
        "max_recommended_distance": max_distance,
        "max_recommended_elevation": max_elevation,
        "last_ride": rides[0]["start_date"][:10] if rides else "N/A",
    }


def recommend_routes(tours, fitness, weather_best_days):
    """Pick routes that match fitness and conditions."""
    if not tours:
        return []

    max_dist = fitness["max_recommended_distance"]
    max_elev = fitness["max_recommended_elevation"]

    # Score routes
    scored = []
    for t in tours:
        dist_km = t["distance"] / 1000
        elev = t.get("elevation_up", 0)

        # Skip if too hard
        if dist_km > max_dist * 1.3 or elev > max_elev * 1.3:
            continue

        # Prefer routes within comfort zone
        dist_fit = 1 - min(abs(dist_km - max_dist * 0.8) / max_dist, 1)
        elev_fit = 1 - min(abs(elev - max_elev * 0.6) / max_elev, 1) if max_elev > 0 else 0.5

        score = dist_fit * 0.6 + elev_fit * 0.4
        scored.append((score, t))

    scored.sort(key=lambda x: -x[0])

    # Return top 3 distinct routes (by distance bucket since names are often identical)
    seen = set()
    results = []
    for score, t in scored:
        dist_bucket = round(t["distance"] / 1000)
        elev_bucket = round(t.get("elevation_up", 0) / 50) * 50
        key = (dist_bucket, elev_bucket)
        if key in seen:
            continue
        seen.add(key)
        results.append(t)
        if len(results) >= 3:
            break

    return results


# ── Output ──────────────────────────────────────────────────────────────

def format_output(weather, fitness, routes):
    """Format the recommendation for WhatsApp."""
    lines = []
    lines.append("🚴 *Ride Planner — This Week*")
    lines.append("")

    # Weather overview
    lines.append("*📅 Weather Forecast*")
    best_days = []
    for day in weather:
        emoji = "☀️" if day["score"] >= 80 else "🌤" if day["score"] >= 60 else "🌥" if day["score"] >= 40 else "🌧"
        stars = "⭐" * (day["score"] // 20)
        line = f"  • {day['day_name'][:3]} {day['date'][5:]}: {emoji} {day['weather']}, {day['temp_min']:.0f}–{day['temp_max']:.0f}°C, wind {day['wind']:.0f} km/h"
        if day["precip"] > 0:
            line += f", rain {day['precip']:.1f}mm"
        line += f" [{stars}]"
        lines.append(line)
        if day["score"] >= 60:
            best_days.append(day)

    lines.append("")

    # Fitness summary
    lines.append("*🏋️ Recent Fitness (4 weeks)*")
    lines.append(f"  • Level: {fitness['level']}")
    lines.append(f"  • Rides: {fitness['real_rides']} outdoor + {fitness['virtual_rides']} indoor")
    lines.append(f"  • Total: {fitness['total_distance_km']} km, {fitness['total_elevation_m']}m elevation")
    if fitness["last_ride"] != "N/A":
        lines.append(f"  • Last ride: {fitness['last_ride']}")
    lines.append(f"  • Suggested max: ~{fitness['max_recommended_distance']}km / ~{fitness['max_recommended_elevation']}m gain")
    lines.append("")

    # Best days
    lines.append("*🎯 Recommendation*")
    if best_days:
        day_list = ", ".join(f"*{d['day_name']}*" for d in best_days[:3])
        lines.append(f"  Best day(s) to ride: {day_list}")
    else:
        lines.append("  ⚠️ No great days this week — all have rain or heavy wind")
        # Find least bad
        sorted_days = sorted(weather, key=lambda d: -d["score"])
        if sorted_days:
            lines.append(f"  Least bad option: *{sorted_days[0]['day_name']}* (score {sorted_days[0]['score']}/100)")
    lines.append("")

    # Route suggestions
    if routes:
        lines.append("*🗺 Route Suggestions*")
        for i, r in enumerate(routes, 1):
            dist = r["distance"] / 1000
            elev = r.get("elevation_up", 0)
            sport = r.get("sport", "cycling").replace("touringbicycle", "touring bike")
            date = r.get("date", "")[:10]
            url = f"https://www.komoot.com/tour/{r['id']}"
            lines.append(f"  {i}. *{r['name']}* — {dist:.1f}km, +{elev:.0f}m ({sport})")
            lines.append(f"     Last done: {date}")
            lines.append(f"     {url}")
    else:
        lines.append("  No matching routes found in Komoot history")

    lines.append("")
    lines.append(f"_Generated {datetime.now().strftime('%Y-%m-%d %H:%M')}_")

    return "\n".join(lines)


# ── Main ────────────────────────────────────────────────────────────────

def main():
    print("Fetching Komoot tours...", file=sys.stderr)
    tours = fetch_komoot_tours()
    print(f"  → {len(tours)} cycling tours", file=sys.stderr)

    print("Fetching weather forecast...", file=sys.stderr)
    weather = fetch_weather()

    print("Fetching Strava activities...", file=sys.stderr)
    strava = fetch_strava_activities()

    fitness = analyze_fitness(strava)
    print(f"  → Fitness level: {fitness['level']}", file=sys.stderr)

    best_days = [d for d in weather if d["score"] >= 60]
    routes = recommend_routes(tours, fitness, best_days)

    output = format_output(weather, fitness, routes)
    print(output)


if __name__ == "__main__":
    main()
