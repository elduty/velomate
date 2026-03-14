"""PostgreSQL connection, schema creation, and upsert helpers."""

import os
from datetime import datetime, timezone

import psycopg2
import psycopg2.extras


def get_connection():
    """Connect to PostgreSQL using DATABASE_URL env var."""
    url = os.environ["DATABASE_URL"]
    conn = psycopg2.connect(url)
    conn.autocommit = True
    return conn


def create_schema(conn):
    """Create all tables if they don't exist."""
    with conn.cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS activities (
                id              SERIAL PRIMARY KEY,
                strava_id       BIGINT UNIQUE,
                komoot_tour_id  BIGINT,
                name            TEXT,
                date            TIMESTAMPTZ,
                distance_m      FLOAT,
                duration_s      INTEGER,
                elevation_m     FLOAT,
                avg_hr          INTEGER,
                max_hr          INTEGER,
                avg_power       INTEGER,
                max_power       INTEGER,
                avg_cadence     INTEGER,
                avg_speed_kmh   FLOAT,
                calories        INTEGER,
                suffer_score    INTEGER,
                device          TEXT,
                synced_at       TIMESTAMPTZ
            );

            CREATE TABLE IF NOT EXISTS activity_streams (
                id              SERIAL PRIMARY KEY,
                activity_id     INTEGER REFERENCES activities(id) ON DELETE CASCADE,
                time_offset     INTEGER,
                hr              INTEGER,
                power           INTEGER,
                cadence         INTEGER,
                speed_kmh       FLOAT,
                altitude_m      FLOAT,
                lat             FLOAT,
                lng             FLOAT
            );

            CREATE TABLE IF NOT EXISTS athlete_stats (
                date            DATE PRIMARY KEY,
                ctl             FLOAT,
                atl             FLOAT,
                tsb             FLOAT,
                resting_hr      INTEGER,
                vo2max          FLOAT,
                weekly_distance_m  FLOAT,
                weekly_elevation_m FLOAT
            );

            CREATE TABLE IF NOT EXISTS routes (
                id              SERIAL PRIMARY KEY,
                komoot_id       BIGINT UNIQUE,
                name            TEXT,
                distance_m      FLOAT,
                elevation_m     FLOAT,
                sport           TEXT,
                last_ridden_at  DATE,
                ride_count      INTEGER
            );

            CREATE TABLE IF NOT EXISTS sync_state (
                key             TEXT PRIMARY KEY,
                last_synced_at  TIMESTAMPTZ,
                value           TEXT
            );

            ALTER TABLE activities ADD COLUMN IF NOT EXISTS is_indoor BOOLEAN;
            ALTER TABLE activities ADD COLUMN IF NOT EXISTS sport_type TEXT;
            ALTER TABLE activities ADD COLUMN IF NOT EXISTS tss FLOAT;

            CREATE INDEX IF NOT EXISTS idx_activities_date ON activities(date);
            CREATE INDEX IF NOT EXISTS idx_activity_streams_activity_id ON activity_streams(activity_id);
            CREATE UNIQUE INDEX IF NOT EXISTS idx_activities_komoot_tour_id
                ON activities(komoot_tour_id) WHERE komoot_tour_id IS NOT NULL;
        """)


def classify_activity(data: dict) -> dict:
    """Add is_indoor and sport_type fields based on device/distance/name."""
    device = data.get("device", "")
    distance_m = data.get("distance_m") or 0
    name = (data.get("name") or "").lower()

    if device == "zwift":
        is_indoor, sport_type = True, "zwift"
    elif any(k in name for k in ("weight", "train", "gym", "strength", "yoga", "pilates")):
        is_indoor, sport_type = True, "strength"
    elif distance_m > 0:
        is_indoor, sport_type = False, "cycling_outdoor"
    else:
        is_indoor, sport_type = True, "cycling_indoor"

    return {**data, "is_indoor": is_indoor, "sport_type": sport_type}


def find_duplicate_by_distance(conn, date_str: str, distance_m: float, tolerance_pct: float = 0.10):
    """Find an existing activity on the same calendar day with similar distance (±10%).
    More reliable than duration-based dedup for cross-platform matching (Strava moving time
    vs Komoot elapsed time differ significantly for the same ride).
    Returns (id, strava_id, device, distance_m, avg_hr, avg_power) or None.
    """
    if not distance_m or distance_m <= 0:
        return None
    with conn.cursor() as cur:
        cur.execute("""
            SELECT id, strava_id, device, distance_m, avg_hr, avg_power
            FROM activities
            WHERE date::date = %s::date
              AND distance_m > 0
              AND ABS(distance_m - %s) / %s < %s
        """, (date_str, distance_m, distance_m, tolerance_pct))
        return cur.fetchone()


def find_duplicate(conn, date_str: str, duration_s: int, tolerance_seconds: int = 300) -> int:
    """Find an existing activity that started within tolerance of date_str
    and has a similar duration. Returns activity id or None.
    Used to detect cross-device duplicates (Zwift + Watch recording same session).
    """
    with conn.cursor() as cur:
        cur.execute("""
            SELECT id, strava_id, device, distance_m, avg_hr, avg_power
            FROM activities
            WHERE ABS(EXTRACT(EPOCH FROM (date - %s::timestamptz))) < %s
              AND ABS(duration_s - %s) < 300
        """, (date_str, tolerance_seconds, duration_s))
        return cur.fetchone()


def merge_activity_data(existing: tuple, new_data: dict) -> dict:
    """Merge two activity records, preferring richer data.
    existing = (id, strava_id, device, distance_m, avg_hr, avg_power)
    Priority: zwift > gps/outdoor > watch
    """
    ex_id, ex_strava_id, ex_device, ex_distance, ex_hr, ex_power = existing
    device_priority = {"karoo": 4, "unknown": 3, "zwift": 3, "watch": 1}
    new_priority = device_priority.get(new_data.get("device", ""), 1)
    ex_priority = device_priority.get(ex_device or "", 1)

    # Keep richer record as base, fill gaps from the other
    if new_priority >= ex_priority:
        merged = dict(new_data)
        # Fill any missing fields from existing record
        if not merged.get("avg_hr") and ex_hr:
            merged["avg_hr"] = ex_hr
        if not merged.get("avg_power") and ex_power:
            merged["avg_power"] = ex_power
        if not merged.get("distance_m") and ex_distance:
            merged["distance_m"] = ex_distance
    else:
        # Existing is richer — just fill gaps, don't replace base
        merged = dict(new_data)
        merged["_skip_insert"] = True  # signal caller to skip this activity

    return merged


def _do_insert(conn, data: dict, now) -> int:
    """Execute the INSERT ... ON CONFLICT for an activity. Returns activity id."""
    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO activities (
                strava_id, name, date, distance_m, duration_s, elevation_m,
                avg_hr, max_hr, avg_power, max_power, avg_cadence,
                avg_speed_kmh, calories, suffer_score, device,
                is_indoor, sport_type, synced_at
            ) VALUES (
                %(strava_id)s, %(name)s, %(date)s, %(distance_m)s, %(duration_s)s, %(elevation_m)s,
                %(avg_hr)s, %(max_hr)s, %(avg_power)s, %(max_power)s, %(avg_cadence)s,
                %(avg_speed_kmh)s, %(calories)s, %(suffer_score)s, %(device)s,
                %(is_indoor)s, %(sport_type)s, %(synced_at)s
            )
            ON CONFLICT (strava_id) DO UPDATE SET
                name = EXCLUDED.name,
                distance_m = EXCLUDED.distance_m,
                duration_s = EXCLUDED.duration_s,
                elevation_m = EXCLUDED.elevation_m,
                avg_hr = EXCLUDED.avg_hr,
                max_hr = EXCLUDED.max_hr,
                avg_power = EXCLUDED.avg_power,
                max_power = EXCLUDED.max_power,
                avg_cadence = EXCLUDED.avg_cadence,
                avg_speed_kmh = EXCLUDED.avg_speed_kmh,
                calories = EXCLUDED.calories,
                suffer_score = EXCLUDED.suffer_score,
                device = EXCLUDED.device,
                is_indoor = EXCLUDED.is_indoor,
                sport_type = EXCLUDED.sport_type,
                synced_at = EXCLUDED.synced_at
            RETURNING id
        """, {**data, "synced_at": now})
        return cur.fetchone()[0]


def upsert_activity(conn, data: dict) -> int:
    """Insert or update an activity. Returns the activity id.
    Detects cross-device duplicates and merges data instead of creating duplicates.
    """
    now = datetime.now(timezone.utc)
    data = classify_activity(data)

    # Duplicate detection: check if another activity started within 5 min with similar duration
    if data.get("date") and data.get("duration_s"):
        duplicate = find_duplicate(conn, data["date"], data["duration_s"])
        if duplicate and duplicate[1] != data.get("strava_id"):
            ex_id = duplicate[0]
            merged = merge_activity_data(duplicate, data)
            if merged.get("_skip_insert"):
                print(f"  [dedup] Skipping {data['name']} — weaker duplicate of existing activity {ex_id}")
                return ex_id
            else:
                # Atomic merge: disable autocommit so DELETE + INSERT are one transaction
                print(f"  [dedup] Merging {data['name']} with existing activity {ex_id} (device priority)")
                conn.autocommit = False
                try:
                    with conn.cursor() as cur:
                        cur.execute("DELETE FROM activities WHERE id = %s", (ex_id,))
                    data = merged
                    activity_id = _do_insert(conn, data, now)
                    conn.commit()
                    return activity_id
                except Exception:
                    conn.rollback()
                    raise
                finally:
                    conn.autocommit = True

    return _do_insert(conn, data, now)


def upsert_streams(conn, activity_id: int, streams: list):
    """Replace streams for an activity."""
    with conn.cursor() as cur:
        cur.execute("DELETE FROM activity_streams WHERE activity_id = %s", (activity_id,))
        if not streams:
            return
        psycopg2.extras.execute_values(
            cur,
            """INSERT INTO activity_streams
                (activity_id, time_offset, hr, power, cadence, speed_kmh, altitude_m, lat, lng)
                VALUES %s""",
            [(activity_id, s.get("time_offset"), s.get("hr"), s.get("power"),
              s.get("cadence"), s.get("speed_kmh"), s.get("altitude_m"),
              s.get("lat"), s.get("lng")) for s in streams]
        )


def upsert_komoot_activity(conn, data: dict) -> int:
    """Insert or update a Komoot-only activity (no strava_id).
    Uses komoot_tour_id as the conflict key.
    """
    now = datetime.now(timezone.utc)
    data = classify_activity(data)
    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO activities (
                komoot_tour_id, name, date, distance_m, duration_s, elevation_m,
                avg_hr, max_hr, avg_power, max_power, avg_cadence,
                avg_speed_kmh, calories, suffer_score, device,
                is_indoor, sport_type, synced_at
            ) VALUES (
                %(komoot_tour_id)s, %(name)s, %(date)s, %(distance_m)s, %(duration_s)s, %(elevation_m)s,
                %(avg_hr)s, %(max_hr)s, %(avg_power)s, %(max_power)s, %(avg_cadence)s,
                %(avg_speed_kmh)s, %(calories)s, %(suffer_score)s, %(device)s,
                %(is_indoor)s, %(sport_type)s, %(synced_at)s
            )
            ON CONFLICT (komoot_tour_id) WHERE komoot_tour_id IS NOT NULL DO UPDATE SET
                name = EXCLUDED.name,
                distance_m = EXCLUDED.distance_m,
                duration_s = EXCLUDED.duration_s,
                elevation_m = EXCLUDED.elevation_m,
                avg_speed_kmh = EXCLUDED.avg_speed_kmh,
                device = EXCLUDED.device,
                is_indoor = EXCLUDED.is_indoor,
                sport_type = EXCLUDED.sport_type,
                synced_at = EXCLUDED.synced_at
            RETURNING id
        """, {**data, "synced_at": now})
        return cur.fetchone()[0]


def upsert_route(conn, data: dict):
    """Insert or update a route."""
    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO routes (komoot_id, name, distance_m, elevation_m, sport, last_ridden_at, ride_count)
            VALUES (%(komoot_id)s, %(name)s, %(distance_m)s, %(elevation_m)s, %(sport)s, %(last_ridden_at)s, %(ride_count)s)
            ON CONFLICT (komoot_id) DO UPDATE SET
                name = EXCLUDED.name,
                distance_m = EXCLUDED.distance_m,
                elevation_m = EXCLUDED.elevation_m,
                sport = EXCLUDED.sport,
                last_ridden_at = EXCLUDED.last_ridden_at,
                ride_count = EXCLUDED.ride_count
        """, data)


def upsert_athlete_stats(conn, date, stats: dict):
    """Insert or update athlete stats for a date."""
    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO athlete_stats (date, ctl, atl, tsb, resting_hr, vo2max, weekly_distance_m, weekly_elevation_m)
            VALUES (%(date)s, %(ctl)s, %(atl)s, %(tsb)s, %(resting_hr)s, %(vo2max)s, %(weekly_distance_m)s, %(weekly_elevation_m)s)
            ON CONFLICT (date) DO UPDATE SET
                ctl = EXCLUDED.ctl,
                atl = EXCLUDED.atl,
                tsb = EXCLUDED.tsb,
                resting_hr = EXCLUDED.resting_hr,
                vo2max = EXCLUDED.vo2max,
                weekly_distance_m = EXCLUDED.weekly_distance_m,
                weekly_elevation_m = EXCLUDED.weekly_elevation_m
        """, {"date": date, **stats})


def get_sync_state(conn, key: str):
    """Get the value for a sync state key, or None."""
    with conn.cursor() as cur:
        cur.execute("SELECT value FROM sync_state WHERE key = %s", (key,))
        row = cur.fetchone()
        return row[0] if row else None


def set_sync_state(conn, key: str, value: str):
    """Set a sync state key/value."""
    now = datetime.now(timezone.utc)
    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO sync_state (key, value, last_synced_at)
            VALUES (%s, %s, %s)
            ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, last_synced_at = EXCLUDED.last_synced_at
        """, (key, value, now))
