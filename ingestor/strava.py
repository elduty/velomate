"""Strava OAuth + activity fetching."""

import os
import time
from datetime import datetime, timezone, timedelta

import requests

TOKEN_URL = "https://www.strava.com/oauth/token"
API_BASE = "https://www.strava.com/api/v3"


def _request_with_retry(method, url, max_retries=3, **kwargs):
    """Make an HTTP request with exponential backoff on 429."""
    kwargs.setdefault("timeout", 15)
    for attempt in range(max_retries + 1):
        resp = method(url, **kwargs)
        if resp.status_code == 429:
            wait = min(60 * (2 ** attempt), 900)  # max 15 min
            print(f"[strava] Rate limited (429), waiting {wait}s (attempt {attempt + 1})")
            time.sleep(wait)
            continue
        return resp
    return resp  # return last response even if still 429


# Module-level token cache
_access_token = None
_token_expires_at = 0
_current_refresh_token = None


def refresh_access_token(client_id: str, client_secret: str, refresh_token: str) -> str:
    """POST to Strava token endpoint, return fresh access_token.
    Also stores the new refresh_token from the response (Strava rotates it).
    """
    global _access_token, _token_expires_at, _current_refresh_token

    if _access_token and time.time() < _token_expires_at - 60:
        return _access_token

    resp = _request_with_retry(requests.post, TOKEN_URL, data={
        "client_id": client_id,
        "client_secret": client_secret,
        "refresh_token": refresh_token,
        "grant_type": "refresh_token",
    })
    resp.raise_for_status()
    data = resp.json()
    _access_token = data["access_token"]
    _token_expires_at = data["expires_at"]

    # Strava rotates refresh tokens — persist the new one
    new_refresh = data.get("refresh_token")
    if new_refresh and new_refresh != refresh_token:
        try:
            from db import get_connection, set_sync_state
            conn = get_connection()
            try:
                set_sync_state(conn, "strava_refresh_token", new_refresh)
                _current_refresh_token = new_refresh  # set AFTER successful DB write
                print(f"[strava] Refresh token rotated and persisted")
            finally:
                conn.close()
        except Exception as e:
            print(f"[strava] WARNING: Could not persist new refresh token: {e}")

    return _access_token


def _get_token() -> str:
    """Get a valid access token, using persisted refresh token if available."""
    global _current_refresh_token

    # Prefer DB-stored refresh token over env var (Strava rotates them)
    refresh_token = _current_refresh_token or os.environ["STRAVA_REFRESH_TOKEN"]
    if not _current_refresh_token:
        try:
            from db import get_connection, get_sync_state
            conn = get_connection()
            try:
                stored = get_sync_state(conn, "strava_refresh_token")
                if stored:
                    refresh_token = stored
                    _current_refresh_token = stored
            finally:
                conn.close()
        except Exception as e:
            print(f"[strava] Could not load stored refresh token: {e}")

    return refresh_access_token(
        os.environ["STRAVA_CLIENT_ID"],
        os.environ["STRAVA_CLIENT_SECRET"],
        refresh_token,
    )


def fetch_recent_activities(access_token: str, after_epoch: int) -> list:
    """GET /athlete/activities?after=<epoch>&per_page=50. Handle pagination."""
    headers = {"Authorization": f"Bearer {access_token}"}
    all_activities = []
    page = 1

    while True:
        resp = _request_with_retry(
            requests.get,
            f"{API_BASE}/athlete/activities",
            headers=headers,
            params={"after": after_epoch, "per_page": 50, "page": page},
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


def fetch_activity_detail(access_token: str, activity_id: int) -> dict:
    """GET /activities/{id} — returns full detail including calories, HR, suffer_score."""
    headers = {"Authorization": f"Bearer {access_token}"}
    resp = _request_with_retry(
        requests.get,
        f"{API_BASE}/activities/{activity_id}",
        headers=headers,
    )
    if resp.status_code == 404:
        return {}
    resp.raise_for_status()
    return resp.json()


def fetch_activity_streams(access_token: str, activity_id: int) -> dict:
    """GET /activities/{id}/streams for HR, power, cadence, speed, altitude, latlng."""
    headers = {"Authorization": f"Bearer {access_token}"}
    keys = "time,heartrate,watts,cadence,velocity_smooth,altitude,latlng"

    resp = _request_with_retry(
        requests.get,
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


def _detect_device(raw: dict) -> str:
    """Detect recording device from Strava activity metadata."""
    device_name = raw.get("device_name", "").lower()
    if "karoo" in device_name:
        return "karoo"
    elif "watch" in device_name or "apple" in device_name:
        return "watch"
    elif raw.get("trainer", False) or "zwift" in raw.get("name", "").lower():
        return "zwift"
    return "unknown"


def _parse_activity(raw: dict) -> dict:
    """Convert Strava API activity (summary or detail) to our DB format."""
    device = _detect_device(raw)
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
        "avg_speed_kmh": round((raw.get("average_speed") or 0) * 3.6, 2),
        "calories": raw.get("calories"),
        "suffer_score": raw.get("suffer_score"),
        "device": device,
        "strava_type": raw.get("type", ""),
        "trainer": raw.get("trainer", False),
    }


def _merge_detail(summary: dict, detail: dict) -> dict:
    """Enrich summary data with detail fields.
    Karoo calories always win. For other devices, fill gaps only.
    """
    if not detail:
        return summary
    merged = dict(summary)
    device = summary.get("device", "unknown")

    # Calories: Karoo data is from FIT file (accurate) — always prefer it.
    # For other devices, use detail calories only if summary had none.
    detail_calories = detail.get("calories")
    if detail_calories:
        if device == "karoo" or not merged.get("calories"):
            merged["calories"] = detail_calories

    # Fill other gaps from detail
    for field in ("average_heartrate", "max_heartrate", "suffer_score"):
        detail_val = detail.get(field)
        db_field = {
            "average_heartrate": "avg_hr",
            "max_heartrate": "max_hr",
            "suffer_score": "suffer_score",
        }[field]
        if detail_val and not merged.get(db_field):
            merged[db_field] = detail_val

    return merged


def _parse_streams(raw_streams: dict) -> list:
    """Convert Strava stream format to list of point dicts."""
    if not raw_streams or "time" not in raw_streams:
        return []

    points = []
    time_arr = raw_streams["time"]
    length = len(time_arr)
    hr_arr = raw_streams.get("heartrate", [])
    power_arr = raw_streams.get("watts", [])
    cadence_arr = raw_streams.get("cadence", [])
    speed_arr = raw_streams.get("velocity_smooth", [])
    alt_arr = raw_streams.get("altitude", [])
    latlngs = raw_streams.get("latlng", [])

    for i in range(length):
        speed_val = speed_arr[i] if i < len(speed_arr) else None
        point = {
            "time_offset": time_arr[i],
            "hr": hr_arr[i] if i < len(hr_arr) else None,
            "power": power_arr[i] if i < len(power_arr) else None,
            "cadence": cadence_arr[i] if i < len(cadence_arr) else None,
            "speed_kmh": round(speed_val * 3.6, 2) if speed_val is not None else None,
            "altitude_m": alt_arr[i] if i < len(alt_arr) else None,
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

    CYCLING_STRAVA_TYPES = {"Ride", "VirtualRide", "EBikeRide", "Handcycle", "Velomobile"}

    latest_epoch = after_epoch
    for raw in activities:
        # Skip non-cycling activities
        strava_type = raw.get("type", "")
        if strava_type and strava_type not in CYCLING_STRAVA_TYPES:
            print(f"  ⏭ Skipping {raw.get('name', '?')} ({strava_type})")
            # Still track epoch so we don't re-fetch skipped activities
            start = raw.get("start_date", "")
            if start:
                try:
                    dt = datetime.fromisoformat(start.replace("Z", "+00:00"))
                    epoch = int(dt.timestamp())
                    if epoch > latest_epoch:
                        latest_epoch = epoch
                except (ValueError, TypeError):
                    pass
            continue

        data = _parse_activity(raw)

        # Fetch detailed activity for calories, HR, suffer_score
        time.sleep(1.0)
        detail = fetch_activity_detail(token, raw["id"])
        data = _merge_detail(data, detail)

        activity_id, streams_preserved = upsert_activity(conn, data)

        # Fetch streams with rate limiting
        time.sleep(1.5)
        raw_streams = fetch_activity_streams(token, raw["id"])
        streams = _parse_streams(raw_streams)
        if streams and not streams_preserved:
            upsert_streams(conn, activity_id, streams)

        # Track latest activity time (use UTC start_date, not local)
        start = raw.get("start_date", "")
        if start:
            try:
                dt = datetime.fromisoformat(start.replace("Z", "+00:00"))
                epoch = int(dt.timestamp())
                if epoch > latest_epoch:
                    latest_epoch = epoch
            except (ValueError, TypeError):
                pass

        print(f"  → {data['name']} ({(data.get('date') or '')[:10]}) — {(data.get('distance_m') or 0)/1000:.1f}km")

    if latest_epoch > after_epoch:
        set_sync_state(conn, "strava_last_activity_epoch", str(latest_epoch))

    return len(activities)


def backfill(conn, months: int = 12):
    """Fetch all activities in last N months, store with streams."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=months * 30)
    after_epoch = int(cutoff.timestamp())
    print(f"[strava] Backfilling {months} months (since {cutoff.date()})")
    return sync_activities(conn, after_epoch)


def reclassify_activities(conn):
    """Re-fetch Strava type for all activities. Reclassify cycling, delete non-cycling."""
    from db import classify_activity

    CYCLING_STRAVA_TYPES = {"Ride", "VirtualRide", "EBikeRide", "Handcycle", "Velomobile"}
    token = _get_token()

    with conn.cursor() as cur:
        cur.execute("SELECT id, strava_id FROM activities WHERE strava_id IS NOT NULL ORDER BY date")
        db_activities = cur.fetchall()

    print(f"[reclassify] {len(db_activities)} activities to check")

    updated = 0
    deleted = 0
    skipped = 0
    for db_id, strava_id in db_activities:
        time.sleep(0.5)
        try:
            resp = _request_with_retry(
                requests.get,
                f"{API_BASE}/activities/{strava_id}",
                headers={"Authorization": f"Bearer {token}"},
            )
            if resp.status_code == 404:
                print(f"  [reclassify] Activity {strava_id} not found on Strava, skipping")
                skipped += 1
                continue
            resp.raise_for_status()
            raw = resp.json()
        except Exception as e:
            print(f"  [reclassify] Error fetching {strava_id}: {e}")
            skipped += 1
            continue

        strava_type = raw.get("type", "")
        name = raw.get("name", "")

        # Delete non-cycling activities
        if strava_type and strava_type not in CYCLING_STRAVA_TYPES:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM activities WHERE id = %s", (db_id,))
            deleted += 1
            print(f"  ✕ {name[:40]:40s} ({strava_type}) — deleted")
            continue

        # Reclassify cycling activities
        trainer = raw.get("trainer", False)
        with conn.cursor() as cur:
            cur.execute("SELECT name, distance_m, device FROM activities WHERE id = %s", (db_id,))
            row = cur.fetchone()
            if not row:
                continue

        classified = classify_activity({
            "strava_type": strava_type, "trainer": trainer,
            "name": row[0], "distance_m": row[1], "device": row[2],
        })

        with conn.cursor() as cur:
            cur.execute(
                "UPDATE activities SET sport_type = %s, is_indoor = %s WHERE id = %s",
                (classified["sport_type"], classified["is_indoor"], db_id),
            )
        updated += 1
        print(f"  → {row[0][:40]:40s} {strava_type:20s} → {classified['sport_type']}")

    print(f"[reclassify] Done: {updated} updated, {deleted} deleted, {skipped} skipped")
