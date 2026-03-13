from datetime import datetime
from typing import Dict, List

import requests

WEATHER_URL = (
    "https://api.open-meteo.com/v1/forecast"
    "?latitude={lat}&longitude={lon}"
    "&daily=precipitation_sum,windspeed_10m_max,temperature_2m_max,temperature_2m_min,weathercode"
    "&timezone=Europe/Lisbon&forecast_days=7"
)

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


def _score_weather(precip: float, wind: float, temp_max: float, code: int) -> int:
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

    # Bad weather codes
    if code >= 61:
        score -= 15
    elif code >= 45:
        score -= 10

    return max(0, score)


def fetch_forecast(lat: float, lon: float) -> List[Dict]:
    """Return list of 7 day dicts: {date, day_name, temp_max, temp_min, precip, wind, code, label, score}"""
    url = WEATHER_URL.format(lat=lat, lon=lon)
    r = requests.get(url, timeout=10)
    r.raise_for_status()
    data = r.json()["daily"]

    forecast = []
    for i, date_str in enumerate(data["time"]):
        dt = datetime.strptime(date_str, "%Y-%m-%d")
        code = data["weathercode"][i]
        precip = data["precipitation_sum"][i]
        wind = data["windspeed_10m_max"][i]
        temp_max = data["temperature_2m_max"][i]
        temp_min = data["temperature_2m_min"][i]
        forecast.append({
            "date": date_str,
            "day_name": DAY_NAMES[dt.weekday()],
            "temp_max": temp_max,
            "temp_min": temp_min,
            "precip": precip,
            "wind": wind,
            "code": code,
            "weather": WMO_CODES.get(code, "Unknown"),
            "score": _score_weather(precip, wind, temp_max, code),
        })
    return forecast
