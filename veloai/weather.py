import sys
from datetime import datetime
from typing import Dict, List

import requests

WEATHER_URL = (
    "https://api.open-meteo.com/v1/forecast"
    "?latitude={lat}&longitude={lon}"
    "&daily=precipitation_sum,windspeed_10m_max,temperature_2m_max,temperature_2m_min,weathercode,uv_index_max"
    "&hourly=temperature_2m,windspeed_10m,winddirection_10m,precipitation,uv_index"
    "&timezone=auto&forecast_days=7"
)

AIR_QUALITY_URL = (
    "https://air-quality-api.open-meteo.com/v1/air-quality"
    "?latitude={lat}&longitude={lon}"
    "&hourly=european_aqi,pm2_5,pm10"
    "&timezone=auto&forecast_days=2"
)

SUNRISE_URL = "https://api.sunrise-sunset.org/json?lat={lat}&lng={lng}&date={date}&formatted=0"

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


def _score_weather(precip: float, wind: float, temp_max: float, code: int, uv_max: float = 0) -> int:
    """Score a day for cycling (0-100, higher = better)."""
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

    # Temperature comfort (ideal: 15-25C)
    if temp_max < 5 or temp_max > 38:
        score -= 30
    elif temp_max < 10 or temp_max > 35:
        score -= 15
    elif temp_max < 13:
        score -= 5

    # UV penalty (high exposure)
    if uv_max >= 11:
        score -= 20
    elif uv_max >= 8:
        score -= 10
    elif uv_max >= 6:
        score -= 5

    # Bad weather codes
    if code >= 61:
        score -= 15
    elif code >= 45:
        score -= 10

    return max(0, score)


def best_ride_hours(hourly_data: list[dict], date_str: str) -> list[dict]:
    """Find the best hours to ride on a given date.
    Returns list of {hour, temp, wind, wind_dir, uv, precip, score} sorted by score.
    """
    day_hours = [h for h in hourly_data if h["time"].startswith(date_str)]
    # Only consider daylight hours (6am-9pm)
    day_hours = [h for h in day_hours if 6 <= int(h["time"][11:13]) <= 20]

    scored = []
    for h in day_hours:
        score = 100
        # Temperature comfort
        temp = h["temp"]
        if temp < 5 or temp > 38:
            score -= 30
        elif temp < 10 or temp > 35:
            score -= 20
        elif temp < 13 or temp > 30:
            score -= 10
        # Wind
        if h["wind"] > 40:
            score -= 40
        elif h["wind"] > 30:
            score -= 25
        elif h["wind"] > 20:
            score -= 10
        # UV
        if h["uv"] >= 11:
            score -= 25
        elif h["uv"] >= 8:
            score -= 15
        elif h["uv"] >= 6:
            score -= 5
        # Rain
        if h["precip"] > 2:
            score -= 40
        elif h["precip"] > 0.5:
            score -= 20
        elif h["precip"] > 0:
            score -= 5

        scored.append({**h, "score": max(0, score)})

    scored.sort(key=lambda x: x["score"], reverse=True)
    return scored


def fetch_forecast(lat: float, lon: float) -> List[Dict]:
    """Return list of 7 day dicts. Returns empty list if API unavailable."""
    url = WEATHER_URL.format(lat=lat, lon=lon)
    try:
        r = requests.get(url, timeout=10)
        r.raise_for_status()
    except requests.RequestException as e:
        print(f"[weather] Open-Meteo API error: {e}", file=sys.stderr)
        return []
    try:
        full_data = r.json()
        data = full_data["daily"]
    except (ValueError, KeyError) as e:
        print(f"[weather] Invalid API response: {e}", file=sys.stderr)
        return []

    # Parse hourly data
    hourly = []
    hourly_data = full_data.get("hourly", {})
    if hourly_data.get("time"):
        for i, time_str in enumerate(hourly_data["time"]):
            hourly.append({
                "time": time_str,
                "temp": hourly_data.get("temperature_2m", [0])[i] if i < len(hourly_data.get("temperature_2m", [])) else 0,
                "wind": hourly_data.get("windspeed_10m", [0])[i] if i < len(hourly_data.get("windspeed_10m", [])) else 0,
                "wind_dir": hourly_data.get("winddirection_10m", [0])[i] if i < len(hourly_data.get("winddirection_10m", [])) else 0,
                "precip": hourly_data.get("precipitation", [0])[i] if i < len(hourly_data.get("precipitation", [])) else 0,
                "uv": hourly_data.get("uv_index", [0])[i] if i < len(hourly_data.get("uv_index", [])) else 0,
            })

    forecast = []
    for i, date_str in enumerate(data["time"]):
        dt = datetime.strptime(date_str, "%Y-%m-%d")
        code = data["weathercode"][i]
        precip = data["precipitation_sum"][i]
        wind = data["windspeed_10m_max"][i]
        temp_max = data["temperature_2m_max"][i]
        temp_min = data["temperature_2m_min"][i]
        uv_max = data.get("uv_index_max", [0] * 7)[i] if i < len(data.get("uv_index_max", [])) else 0
        forecast.append({
            "date": date_str,
            "day_name": DAY_NAMES[dt.weekday()],
            "temp_max": temp_max,
            "temp_min": temp_min,
            "precip": precip,
            "wind": wind,
            "uv_max": uv_max,
            "code": code,
            "weather": WMO_CODES.get(code, "Unknown"),
            "score": _score_weather(precip, wind, temp_max, code, uv_max),
            "hourly": hourly,
        })
    return forecast


def fetch_air_quality(lat: float, lon: float, date_str: str) -> dict | None:
    """Fetch air quality for a specific date. Returns {aqi, pm25, pm10} or None."""
    url = AIR_QUALITY_URL.format(lat=lat, lon=lon)
    try:
        r = requests.get(url, timeout=10)
        r.raise_for_status()
        data = r.json().get("hourly", {})
        if not data.get("time"):
            return None
        # Find midday hour for the requested date
        for i, t in enumerate(data["time"]):
            if t.startswith(date_str) and "T12:" in t:
                return {
                    "aqi": data.get("european_aqi", [None])[i],
                    "pm25": data.get("pm2_5", [None])[i],
                    "pm10": data.get("pm10", [None])[i],
                }
    except Exception:
        pass
    return None


def fetch_sunrise_sunset(lat: float, lon: float, date_str: str) -> dict | None:
    """Fetch sunrise/sunset times. Returns {sunrise, sunset, golden_hour_end} or None."""
    url = SUNRISE_URL.format(lat=lat, lng=lon, date=date_str)
    try:
        r = requests.get(url, timeout=10)
        r.raise_for_status()
        data = r.json()
        if data.get("status") != "OK":
            return None
        results = data["results"]
        return {
            "sunrise": results["sunrise"][11:16],   # HH:MM
            "sunset": results["sunset"][11:16],
            "civil_twilight_end": results.get("civil_twilight_end", "")[11:16],
        }
    except Exception:
        pass
    return None
