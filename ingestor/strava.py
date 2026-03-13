"""Strava OAuth + activity fetching."""

import os
import time
from datetime import datetime, timezone, timedelta

import requests

TOKEN_URL = "https://www.strava.com/oauth/token"
API_BASE = "https://www.strava.com/api/v3"

# Module-level token cache
_access_token = None
_token_expires_at = 0


def refresh_access_token(client_id: str, client_secret: str, refresh_token: str) -> str:
    """POST to Strava token endpoint, return fresh access_token."""
    global _access_token, _token_expires_at

    if _access_token and time.time() < _token_expires_at - 60:
        return _access_token

    resp = requests.post(TOKEN_URL, data={
        "client_id": client_id,
        "client_secret": client_secret,
        "refresh_token": refresh_token,
        "grant_type": "refresh_token",
    }, timeout=15)
    resp.raise_for_status()
    data = resp.json()
    _access_token = data["access_token"]
    _token_expires_at = data["expires_at"]
    return _access_token


def _get_token() -> str:
    """Get a valid access token from env vars."""
    return refresh_access_token(
        os.environ["STRAVA_CLIENT_ID"],
        os.environ["STRAVA_CLIENT_SECRET"],
        os.environ["STRAVA_REFRESH_TOKEN"],
    )


def _headers():
    return {"Authorization": f"Bearer {_get_token()}"}


def fetch_recent_activities(access_token: str, after_epoch: int) -> list:
    """GET /athlete/activities?after=<epoch>&per_page=50. Handle pagination."""
    headers = {"Authorization": f"Bearer {access_token}"}
    all_activities = []
    page = 1

    while True:
        resp = requests.get(
            f"{API_BASE}/athlete/activities",
            headers=headers,
            params={"after": after_epoch, "per_page": 50, "page": page},
            timeout=15,
        )
        resp.raise_for_status()
        batch = resp.json()
        if not batch:
            break
        all_activities.extend(batch)
        if len(batch) < 50:
            break
        page += 1
        time.sleep(1)  # rate limit courtesy

    return all_activities


def fetch_activity_streams(access_token: str, activity_id: int) -> dict:
    """GET /activities/{id}/streams for HR, power, cadence, speed, altitude, latlng."""
    headers = {"Authorization": f"Bearer {access_token}"}
    keys = "time,heartrate,watts,cadence,velocity_smooth,altitude,latlng"

    resp = requests.get(
        f"{API_BASE}/activities/{activity_id}/streams",
        headers=headers,
        params={"keys": keys, "key_type": "time"},
        timeout=30,
    )
    if resp.status_code == 404:
        return {}
    resp.raise_for_status()

    # Convert Strava stream format to dict of lists
    streams = {}
    for stream in resp.json():
        streams[stream["type"]] = stream["data"]
    return streams


def _parse_activity(raw: dict) -> dict:
    """Convert Strava API activity to our DB format."""
    # Detect device from device_name or type
    device_name = raw.get("device_name", "").lower()
    if "karoo" in device_name:
        device = "karoo"
    elif "watch" in device_name or "apple" in device_name:
        device = "watch"
    elif raw.get("trainer", False) or "zwift" in raw.get("name", "").lower():
        device = "zwift"
    else:
        device = "unknown"

    return {
        "strava_id": raw["id"],
        "name": raw.get("name", ""),
        "date": raw.get("start_date"),
        "distance_m": raw.get("distance", 0),
        "duration_s": raw.get("moving_time", 0),
        "elevation_m": raw.get("total_elevation_gain", 0),
        "avg_hr": raw.get("average_heartrate"),
        "max_hr": raw.get("max_heartrate"),
        "avg_power": raw.get("average_watts"),
        "max_power": raw.get("max_watts"),
        "avg_cadence": raw.get("average_cadence"),
        "avg_speed_kmh": round(raw.get("average_speed", 0) * 3.6, 2),
        "calories": raw.get("calories"),
        "suffer_score": raw.get("suffer_score"),
        "device": device,
    }


def _parse_streams(raw_streams: dict) -> list:
    """Convert Strava stream format to list of point dicts."""
    if not raw_streams or "time" not in raw_streams:
        return []

    points = []
    length = len(raw_streams["time"])
    latlngs = raw_streams.get("latlng", [])

    for i in range(length):
        point = {
            "time_offset": raw_streams["time"][i],
            "hr": raw_streams.get("heartrate", [None] * length)[i] if i < len(raw_streams.get("heartrate", [])) else None,
            "power": raw_streams.get("watts", [None] * length)[i] if i < len(raw_streams.get("watts", [])) else None,
            "cadence": raw_streams.get("cadence", [None] * length)[i] if i < len(raw_streams.get("cadence", [])) else None,
            "speed_kmh": round(raw_streams.get("velocity_smooth", [0] * length)[i] * 3.6, 2) if i < len(raw_streams.get("velocity_smooth", [])) else None,
            "altitude_m": raw_streams.get("altitude", [None] * length)[i] if i < len(raw_streams.get("altitude", [])) else None,
            "lat": latlngs[i][0] if i < len(latlngs) and latlngs[i] else None,
            "lng": latlngs[i][1] if i < len(latlngs) and latlngs[i] else None,
        }
        points.append(point)
    return points


def sync_activities(conn, after_epoch: int = None):
    """Fetch recent activities from Strava, store with streams."""
    from db import upsert_activity, upsert_streams, get_sync_state, set_sync_state

    token = _get_token()

    if after_epoch is None:
        last = get_sync_state(conn, "strava_last_activity_epoch")
        after_epoch = int(last) if last else 0

    activities = fetch_recent_activities(token, after_epoch)
    print(f"[strava] Fetched {len(activities)} activities since epoch {after_epoch}")

    latest_epoch = after_epoch
    for raw in activities:
        data = _parse_activity(raw)
        activity_id = upsert_activity(conn, data)

        # Fetch streams with rate limiting
        time.sleep(1.5)
        raw_streams = fetch_activity_streams(token, raw["id"])
        streams = _parse_streams(raw_streams)
        upsert_streams(conn, activity_id, streams)

        # Track latest activity time
        start = raw.get("start_date_local", raw.get("start_date", ""))
        if start:
            try:
                dt = datetime.fromisoformat(start.replace("Z", "+00:00"))
                epoch = int(dt.timestamp())
                if epoch > latest_epoch:
                    latest_epoch = epoch
            except (ValueError, TypeError):
                pass

        print(f"  → {data['name']} ({data['date'][:10]}) — {data['distance_m']/1000:.1f}km")

    if latest_epoch > after_epoch:
        set_sync_state(conn, "strava_last_activity_epoch", str(latest_epoch))

    return len(activities)


def backfill(conn, months: int = 12):
    """Fetch all activities in last N months, store with streams."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=months * 30)
    after_epoch = int(cutoff.timestamp())
    print(f"[strava] Backfilling {months} months (since {cutoff.date()})")
    return sync_activities(conn, after_epoch)
