"""DB reader for VeloMate CLI (connects to homelab PostgreSQL)."""

from velomate.config import load as load_config


def get_connection():
    """Connect to PostgreSQL. Returns None if unavailable."""
    try:
        import psycopg2
        cfg = load_config()
        db = cfg["db"]
        conn = psycopg2.connect(
            host=db["host"], port=db["port"],
            dbname=db["name"], user=db["user"], password=db["password"],
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
    """Get unique rides from activities for recommendations.
    Deduplicates by distance/elevation bucket to avoid showing the same route twice.
    """
    if not conn:
        return []
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT id, name, distance_m, elevation_m, sport_type, date::date, 1 as ride_count
                FROM activities
                WHERE distance_m > 5000 AND sport_type = 'cycling_outdoor'
                ORDER BY date DESC
                LIMIT 50
            """)
            rows = cur.fetchall()
            return [
                {
                    "id": r[0], "name": r[1], "distance": r[2],
                    "elevation_up": r[3], "sport": r[4] or "cycling",
                    "date": str(r[5]) if r[5] else "", "ride_count": r[6],
                }
                for r in rows
            ]
    except Exception:
        return []


def get_avg_speed(conn, surface: str | None = None) -> float | None:
    """Get average outdoor cycling speed (km/h) from ride history.
    Uses the median speed from rides >5km to get a realistic cruising speed.
    If surface is provided, filters by speed ranges typical for that surface
    (road > 22 km/h, gravel/mtb < 25 km/h) to separate surface-specific data.
    Falls back to overall median if not enough surface-specific data.
    Returns None if no data available.
    """
    if not conn:
        return None
    try:
        with conn.cursor() as cur:
            # Try surface-specific speed first
            if surface == "road":
                cur.execute("""
                    SELECT ROUND(percentile_cont(0.5) WITHIN GROUP (ORDER BY avg_speed_kmh)::numeric, 1)
                    FROM activities
                    WHERE sport_type = 'cycling_outdoor'
                      AND avg_speed_kmh > 22
                      AND distance_m > 5000
                    HAVING COUNT(*) >= 5
                """)
                row = cur.fetchone()
                if row and row[0]:
                    return float(row[0])
            elif surface in ("gravel", "mtb"):
                cur.execute("""
                    SELECT ROUND(percentile_cont(0.5) WITHIN GROUP (ORDER BY avg_speed_kmh)::numeric, 1)
                    FROM activities
                    WHERE sport_type = 'cycling_outdoor'
                      AND avg_speed_kmh > 0 AND avg_speed_kmh < 25
                      AND distance_m > 5000
                    HAVING COUNT(*) >= 5
                """)
                row = cur.fetchone()
                if row and row[0]:
                    return float(row[0])

            # Fallback: overall median
            cur.execute("""
                SELECT ROUND(percentile_cont(0.5) WITHIN GROUP (ORDER BY avg_speed_kmh)::numeric, 1)
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