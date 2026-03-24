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
            ALTER TABLE activities ADD COLUMN IF NOT EXISTS np FLOAT;
            ALTER TABLE activities ADD COLUMN IF NOT EXISTS ef FLOAT;
            ALTER TABLE activities ADD COLUMN IF NOT EXISTS work_kj FLOAT;
            ALTER TABLE activities ADD COLUMN IF NOT EXISTS ride_ftp FLOAT;

            CREATE INDEX IF NOT EXISTS idx_activities_date ON activities(date);
            CREATE INDEX IF NOT EXISTS idx_activity_streams_activity_id ON activity_streams(activity_id);
            CREATE UNIQUE INDEX IF NOT EXISTS idx_activities_komoot_tour_id
                ON activities(komoot_tour_id) WHERE komoot_tour_id IS NOT NULL;

            CREATE INDEX IF NOT EXISTS idx_streams_power ON activity_streams(activity_id, time_offset) WHERE power IS NOT NULL;
        """)


def classify_activity(data: dict) -> dict:
    """Add is_indoor and sport_type fields. Only cycling activities are ingested.
    Uses Strava type, device, trainer flag, and distance to classify.
    """
    strava_type = (data.get("strava_type") or "").lower()
    device = data.get("device", "")
    distance_m = data.get("distance_m") or 0
    trainer = data.get("trainer", False)

    if device == "zwift" or strava_type == "virtualride":
        is_indoor, sport_type = True, "zwift"
    elif trainer:
        is_indoor, sport_type = True, "cycling_indoor"
    elif strava_type == "ebikeride":
        is_indoor, sport_type = False, "ebike"
    elif distance_m > 0:
        is_indoor, sport_type = False, "cycling_outdoor"
    else:
        is_indoor, sport_type = True, "cycling_indoor"

    return {**data, "is_indoor": is_indoor, "sport_type": sport_type}


def find_duplicate(conn, date_str: str, duration_s: int, distance_m: float = 0,
                   tolerance_seconds: int = 300) -> tuple | None:
    """Find an existing activity that started within tolerance of date_str
    and has a similar duration OR similar distance. Returns activity id or None.
    Used to detect cross-device duplicates (e.g., Karoo + Watch recording same ride).

    Matches if start time is close AND either:
    - Duration within 15% (handles moving_time vs elapsed_time differences)
    - Distance within 10% (handles different GPS sampling / measurement)
    """
    with conn.cursor() as cur:
        cur.execute("""
            SELECT id, strava_id, device, distance_m, avg_hr, avg_power
            FROM activities
            WHERE ABS(EXTRACT(EPOCH FROM (date - %s::timestamptz))) < %s
              AND (
                ABS(duration_s - %s) < GREATEST(300, duration_s * 0.15)
                OR (distance_m > 0 AND %s > 0 AND ABS(distance_m - %s) < distance_m * 0.10)
              )
        """, (date_str, tolerance_seconds, duration_s, distance_m, distance_m))
        return cur.fetchone()


def _data_richness(data: dict) -> int:
    """Score an activity record by data richness. Higher = more useful data."""
    score = 0
    if data.get("avg_power"):
        score += 3  # power is the most valuable metric
    if data.get("avg_hr"):
        score += 2
    if data.get("distance_m") and data["distance_m"] > 0:
        score += 1
    if data.get("avg_cadence"):
        score += 1
    if data.get("calories"):
        score += 1
    if data.get("elevation_m") and data["elevation_m"] > 0:
        score += 1
    return score


def merge_activity_data(existing: tuple, new_data: dict) -> dict:
    """Merge two activity records, preferring the one with richer data.
    existing = (id, strava_id, device, distance_m, avg_hr, avg_power)
    Uses data richness scoring — whichever record has more useful fields wins.
    """
    ex_id, ex_strava_id, ex_device, ex_distance, ex_hr, ex_power = existing
    ex_data = {"avg_power": ex_power, "avg_hr": ex_hr, "distance_m": ex_distance}
    ex_richness = _data_richness(ex_data)
    new_richness = _data_richness(new_data)

    # Keep richer record as base, fill gaps from the other
    if new_richness >= ex_richness:
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


def upsert_activity(conn, data: dict) -> tuple[int, bool]:
    """Insert or update an activity. Returns (activity_id, streams_preserved).
    streams_preserved=True means dedup merge already handled streams — caller should not overwrite.
    """
    now = datetime.now(timezone.utc)
    data = classify_activity(data)

    # Duplicate detection: check if another activity started within 5 min with similar duration
    if data.get("date") and data.get("duration_s"):
        duplicate = find_duplicate(conn, data["date"], data["duration_s"], data.get("distance_m", 0))
        if duplicate and duplicate[1] != data.get("strava_id"):
            ex_id = duplicate[0]
            merged = merge_activity_data(duplicate, data)
            if merged.get("_skip_insert"):
                print(f"  [dedup] Skipping {data['name']} — weaker duplicate of existing activity {ex_id}")
                # Still update the existing record with any new fields from the incoming data
                # (e.g. suffer_score, tss, calories that may arrive on a later sync)
                now = datetime.now(timezone.utc)
                with conn.cursor() as cur:
                    cur.execute("""
                        UPDATE activities SET
                            suffer_score = COALESCE(suffer_score, %(suffer_score)s),
                            tss          = COALESCE(tss, %(tss)s),
                            calories     = COALESCE(calories, %(calories)s),
                            avg_hr       = COALESCE(avg_hr, %(avg_hr)s),
                            avg_power    = COALESCE(avg_power, %(avg_power)s),
                            max_hr       = COALESCE(max_hr, %(max_hr)s),
                            max_power    = COALESCE(max_power, %(max_power)s),
                            avg_cadence  = COALESCE(avg_cadence, %(avg_cadence)s),
                            synced_at    = %(synced_at)s
                        WHERE id = %(ex_id)s
                    """, {
                        "suffer_score": data.get("suffer_score"),
                        "tss":          data.get("tss"),
                        "calories":     data.get("calories"),
                        "avg_hr":       data.get("avg_hr"),
                        "avg_power":    data.get("avg_power"),
                        "max_hr":       data.get("max_hr"),
                        "max_power":    data.get("max_power"),
                        "avg_cadence":  data.get("avg_cadence"),
                        "synced_at":    now,
                        "ex_id":        ex_id,
                    })
                return ex_id, True
            else:
                # Atomic merge: save streams, delete old, insert new, restore streams
                print(f"  [dedup] Merging {data['name']} with existing activity {ex_id}")
                conn.autocommit = False
                try:
                    with conn.cursor() as cur:
                        # Save existing streams in memory before CASCADE deletes them
                        cur.execute("""
                            SELECT time_offset, hr, power, cadence, speed_kmh, altitude_m, lat, lng
                            FROM activity_streams WHERE activity_id = %s
                        """, (ex_id,))
                        saved_streams = cur.fetchall()
                        cur.execute("DELETE FROM activities WHERE id = %s", (ex_id,))
                    data = merged
                    activity_id = _do_insert(conn, data, now)
                    # Restore saved streams if new activity has none of its own
                    if saved_streams:
                        with conn.cursor() as cur:
                            cur.execute("SELECT COUNT(*) FROM activity_streams WHERE activity_id = %s", (activity_id,))
                            if cur.fetchone()[0] == 0:
                                psycopg2.extras.execute_values(
                                    cur,
                                    """INSERT INTO activity_streams
                                        (activity_id, time_offset, hr, power, cadence, speed_kmh, altitude_m, lat, lng)
                                        VALUES %s""",
                                    [(activity_id, *row) for row in saved_streams],
                                )
                    conn.commit()
                    return activity_id, True
                except Exception:
                    conn.rollback()
                    raise
                finally:
                    conn.autocommit = True

    return _do_insert(conn, data, now), False


def upsert_streams(conn, activity_id: int, streams: list):
    """Replace streams for an activity. Wrapped in a transaction so a crash
    between DELETE and INSERT doesn't leave the activity with no streams.
    """
    conn.autocommit = False
    try:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM activity_streams WHERE activity_id = %s", (activity_id,))
            if streams:
                psycopg2.extras.execute_values(
                    cur,
                    """INSERT INTO activity_streams
                        (activity_id, time_offset, hr, power, cadence, speed_kmh, altitude_m, lat, lng)
                        VALUES %s""",
                    [(activity_id, s.get("time_offset"), s.get("hr"), s.get("power"),
                      s.get("cadence"), s.get("speed_kmh"), s.get("altitude_m"),
                      s.get("lat"), s.get("lng")) for s in streams]
                )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.autocommit = True


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
