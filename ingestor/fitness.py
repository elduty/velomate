"""CTL/ATL/TSB fitness calculator."""

import os
from datetime import timedelta


DEFAULT_THRESHOLD_HR = 170
DEFAULT_FTP = 150  # Estimated FTP (watts) — fallback only


def calculate_tss(duration_s: int, avg_hr: int, threshold_hr: int) -> float:
    """HR-based TSS = (duration_h) × (avg_hr / threshold_hr)² × 100"""
    if not duration_s or not avg_hr or not threshold_hr:
        return 0.0
    duration_h = duration_s / 3600
    intensity = avg_hr / threshold_hr
    return duration_h * (intensity ** 2) * 100


def calculate_tss_power(duration_s: int, avg_power: int, ftp: int) -> float:
    """Power-based TSS = (duration_s × avg_power × IF) / (FTP × 3600) × 100
    where IF (Intensity Factor) = avg_power / FTP"""
    if not duration_s or not avg_power or not ftp:
        return 0.0
    intensity = avg_power / ftp
    return (duration_s * avg_power * intensity) / (ftp * 3600) * 100


def estimate_threshold_hr(conn) -> int:
    """Return 95th percentile of max_hr from activities, or default."""
    with conn.cursor() as cur:
        cur.execute("""
            SELECT percentile_cont(0.95) WITHIN GROUP (ORDER BY max_hr)
            FROM activities
            WHERE max_hr IS NOT NULL AND max_hr > 0
        """)
        row = cur.fetchone()
        if row and row[0]:
            return int(row[0])
    return DEFAULT_THRESHOLD_HR


def estimate_ftp(conn) -> int:
    """Return estimated FTP from 95th percentile of avg_power, or default."""
    with conn.cursor() as cur:
        cur.execute("""
            SELECT percentile_cont(0.95) WITHIN GROUP (ORDER BY avg_power)
            FROM activities
            WHERE avg_power IS NOT NULL AND avg_power > 0
        """)
        row = cur.fetchone()
        if row and row[0]:
            return int(row[0])
    return DEFAULT_FTP


def recalculate_fitness(conn):
    """
    Walk day-by-day from earliest activity, applying EMA:
      CTL = CTL_prev × (1 - 1/42) + tss × (1/42)
      ATL = ATL_prev × (1 - 1/7)  + tss × (1/7)
      TSB = CTL - ATL
    Uses power-based TSS when available, HR-based as fallback.
    Upsert into athlete_stats.
    """
    from db import upsert_athlete_stats

    # Use configured values if set, otherwise auto-estimate from data
    env_max_hr = os.environ.get("VELOAI_MAX_HR", "")
    env_ftp = os.environ.get("VELOAI_FTP", "")

    if env_max_hr:
        threshold_hr = int(env_max_hr)
        print(f"[fitness] Using configured max HR: {threshold_hr}")
    else:
        threshold_hr = estimate_threshold_hr(conn)
        print(f"[fitness] Auto-estimated threshold HR: {threshold_hr}")

    if env_ftp:
        ftp = int(env_ftp)
        print(f"[fitness] Using configured FTP: {ftp}W")
    else:
        ftp = estimate_ftp(conn)
        print(f"[fitness] Auto-estimated FTP: {ftp}W")

    # Store per-activity TSS
    with conn.cursor() as cur:
        cur.execute("""
            SELECT id, duration_s, avg_hr, avg_power
            FROM activities
            WHERE date IS NOT NULL
        """)
        activity_rows = cur.fetchall()

    for act_id, duration_s, avg_hr, avg_power in activity_rows:
        if avg_power and avg_power > 0:
            tss = calculate_tss_power(duration_s, avg_power, ftp)
        elif avg_hr and avg_hr > 0:
            tss = calculate_tss(duration_s, avg_hr, threshold_hr)
        else:
            tss = 0
        with conn.cursor() as cur:
            cur.execute("UPDATE activities SET tss = %s WHERE id = %s", (round(tss, 1), act_id))

    # Get all activities ordered by date (include power data)
    with conn.cursor() as cur:
        cur.execute("""
            SELECT date::date, duration_s, avg_hr, avg_power, distance_m, elevation_m
            FROM activities
            WHERE date IS NOT NULL
            ORDER BY date
        """)
        rows = cur.fetchall()

    if not rows:
        print("[fitness] No activities found, skipping")
        return

    # Build daily TSS map — prefer power-based TSS, fall back to HR-based
    daily_tss = {}
    daily_distance = {}
    daily_elevation = {}
    for date, duration_s, avg_hr, avg_power, distance_m, elevation_m in rows:
        if avg_power and avg_power > 0:
            tss = calculate_tss_power(duration_s, avg_power, ftp)
        elif avg_hr and avg_hr > 0:
            tss = calculate_tss(duration_s, avg_hr, threshold_hr)
        else:
            tss = 0
        daily_tss[date] = daily_tss.get(date, 0) + tss
        daily_distance[date] = daily_distance.get(date, 0) + (distance_m or 0)
        daily_elevation[date] = daily_elevation.get(date, 0) + (elevation_m or 0)

    # Walk from first to last date
    first_date = min(daily_tss.keys())
    last_date = max(daily_tss.keys())

    ctl = 0.0
    atl = 0.0
    current = first_date
    count = 0

    while current <= last_date:
        tss = daily_tss.get(current, 0)
        ctl = ctl * (1 - 1/42) + tss * (1/42)
        atl = atl * (1 - 1/7) + tss * (1/7)
        tsb = ctl - atl

        # Calculate rolling weekly totals
        week_start = current - timedelta(days=6)
        weekly_dist = sum(v for k, v in daily_distance.items() if week_start <= k <= current)
        weekly_elev = sum(v for k, v in daily_elevation.items() if week_start <= k <= current)

        upsert_athlete_stats(conn, current, {
            "ctl": round(ctl, 2),
            "atl": round(atl, 2),
            "tsb": round(tsb, 2),
            "resting_hr": None,
            "vo2max": None,
            "weekly_distance_m": round(weekly_dist, 1),
            "weekly_elevation_m": round(weekly_elev, 1),
        })
        count += 1
        current += timedelta(days=1)

    print(f"[fitness] Calculated {count} days of fitness data (CTL={ctl:.1f}, ATL={atl:.1f}, TSB={ctl-atl:.1f})")
