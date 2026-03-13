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

            CREATE INDEX IF NOT EXISTS idx_activities_date ON activities(date);
            CREATE INDEX IF NOT EXISTS idx_activities_strava_id ON activities(strava_id);
            CREATE INDEX IF NOT EXISTS idx_activity_streams_activity_id ON activity_streams(activity_id);
        """)


def upsert_activity(conn, data: dict) -> int:
    """Insert or update an activity. Returns the activity id."""
    now = datetime.now(timezone.utc)
    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO activities (
                strava_id, name, date, distance_m, duration_s, elevation_m,
                avg_hr, max_hr, avg_power, max_power, avg_cadence,
                avg_speed_kmh, calories, suffer_score, device, synced_at
            ) VALUES (
                %(strava_id)s, %(name)s, %(date)s, %(distance_m)s, %(duration_s)s, %(elevation_m)s,
                %(avg_hr)s, %(max_hr)s, %(avg_power)s, %(max_power)s, %(avg_cadence)s,
                %(avg_speed_kmh)s, %(calories)s, %(suffer_score)s, %(device)s, %(synced_at)s
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
                synced_at = EXCLUDED.synced_at
            RETURNING id
        """, {**data, "synced_at": now})
        return cur.fetchone()[0]


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
