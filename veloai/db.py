"""DB reader for VeloAI CLI (connects to homelab PostgreSQL)."""

import os

DB_HOST = os.environ.get("VELOAI_DB_HOST", "10.7.40.15")
DB_PORT = os.environ.get("VELOAI_DB_PORT", "5423")
DB_NAME = os.environ.get("VELOAI_DB_NAME", "veloai")
DB_USER = os.environ.get("VELOAI_DB_USER", "veloai")
DB_PASS = os.environ.get("VELOAI_DB_PASS", "")


def _get_password():
    """Get DB password from env or keychain."""
    if DB_PASS:
        return DB_PASS
    try:
        from veloai.keychain import get
        creds = get("openclaw/veloai-db")
        return creds.get("password", "")
    except Exception:
        return ""


def get_connection():
    """Connect to PostgreSQL on homelab. Returns None if unavailable."""
    try:
        import psycopg2
        conn = psycopg2.connect(
            host=DB_HOST, port=DB_PORT,
            dbname=DB_NAME, user=DB_USER, password=_get_password(),
            connect_timeout=5,
        )
        conn.autocommit = True
        return conn
    except Exception:
        return None


def get_latest_fitness(conn) -> dict:
    """Get most recent fitness stats."""
    if not conn:
        return {}
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT date, ctl, atl, tsb FROM athlete_stats ORDER BY date DESC LIMIT 1")
            row = cur.fetchone()
            if row:
                return {"date": row[0], "ctl": row[1], "atl": row[2], "tsb": row[3]}
    except Exception:
        pass
    return {}


def get_routes(conn) -> list:
    """Get routes from DB."""
    if not conn:
        return []
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT komoot_id, name, distance_m, elevation_m, sport, last_ridden_at, ride_count
                FROM routes ORDER BY last_ridden_at DESC NULLS LAST
            """)
            rows = cur.fetchall()
            return [
                {
                    "id": r[0], "name": r[1], "distance": r[2],
                    "elevation_up": r[3], "sport": r[4],
                    "date": str(r[5]) if r[5] else "", "ride_count": r[6],
                }
                for r in rows
            ]
    except Exception:
        return []


def get_avg_speed(conn) -> float | None:
    """Get average outdoor cycling speed (km/h) from ride history.
    Only considers rides > 5km to exclude warm-ups and errands.
    Returns None if no data available.
    """
    if not conn:
        return None
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT ROUND(AVG(avg_speed_kmh)::numeric, 1)
                FROM activities
                WHERE sport_type = 'cycling_outdoor'
                  AND avg_speed_kmh > 0
                  AND distance_m > 5000
            """)
            row = cur.fetchone()
            if row and row[0]:
                return float(row[0])
    except Exception:
        pass
    return None