from datetime import datetime, timedelta
from typing import Dict

import requests

from veloai import keychain


def _fetch_activities():
    """Fetch recent Strava activities (last 4 weeks)."""
    creds = keychain.get("openclaw/strava")

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


def get_fitness_level() -> Dict:
    """Return {level: str, rides_last_4w: int, avg_distance_km: float, description: str}"""
    activities = _fetch_activities()

    rides = [a for a in activities if a.get("type") in ("Ride", "VirtualRide")]
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
        "rides_last_4w": ride_count,
        "avg_distance_km": round(total_distance / ride_count, 1) if ride_count else 0,
        "description": f"{ride_count} rides in last 4 weeks, {round(total_distance, 1)} km total",
    }
