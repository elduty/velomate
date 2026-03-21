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


def compute_ef(np: float, avg_hr: int) -> float | None:
    """Efficiency Factor = NP / avg HR."""
    if not np or not avg_hr or avg_hr <= 0:
        return None
    return round(np / avg_hr, 2)


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
    """Estimate FTP from best 20-minute rolling average power in last 90 days.
    FTP ≈ best 20-min power × 0.95 (standard protocol).
    Falls back to 95th percentile of avg_power if no stream data available.
    """
    # Try rolling 20-min best from stream data (last 90 days)
    with conn.cursor() as cur:
        cur.execute("""
            WITH recent_activities AS (
                SELECT id FROM activities
                WHERE date >= CURRENT_DATE - interval '90 days'
                  AND avg_power IS NOT NULL AND avg_power > 0
                ),
            rolling AS (
                SELECT
                    s.activity_id,
                    AVG(s.power) OVER (
                        PARTITION BY s.activity_id
                        ORDER BY s.time_offset
                        RANGE BETWEEN 1199 PRECEDING AND CURRENT ROW
                    ) AS avg_20min
                FROM activity_streams s
                JOIN recent_activities a ON a.id = s.activity_id
                WHERE s.power IS NOT NULL AND s.power > 0
            )
            SELECT ROUND(MAX(avg_20min) * 0.95) FROM rolling
        """)
        row = cur.fetchone()
        if row and row[0] and row[0] > 0:
            return int(row[0])

    # Fallback: 95th percentile of avg_power from activities
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

    try:
        hr_val = int(env_max_hr) if env_max_hr else 0
    except ValueError:
        hr_val = 0
    if hr_val > 0:
        threshold_hr = hr_val
        print(f"[fitness] Using configured max HR: {threshold_hr}")
    else:
        threshold_hr = estimate_threshold_hr(conn)
        print(f"[fitness] Auto-estimated threshold HR: {threshold_hr}")

    try:
        ftp_val = int(env_ftp) if env_ftp else 0
    except ValueError:
        ftp_val = 0
    if ftp_val > 0:
        ftp = ftp_val
        print(f"[fitness] Using configured FTP: {ftp}W")
    else:
        ftp = estimate_ftp(conn)
        print(f"[fitness] Auto-estimated FTP: {ftp}W (rolling 90-day best 20min × 0.95)")

    # Store per-activity TSS (cycling only — running/strength use different thresholds)
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

    # Compute NP, EF, Work for activities with power data
    print("[fitness] Computing NP/EF/Work...")
    with conn.cursor() as cur:
        cur.execute("""
            SELECT a.id, a.avg_hr, a.avg_power, a.duration_s
            FROM activities a
            JOIN activity_streams s ON s.activity_id = a.id
            WHERE s.power IS NOT NULL AND s.power > 0
              AND a.np IS NULL
            GROUP BY a.id, a.avg_hr, a.avg_power, a.duration_s
            HAVING COUNT(*) > 30
        """)
        power_activities = cur.fetchall()

    np_count = 0
    for act_id, avg_hr, avg_power, duration_s in power_activities:
        with conn.cursor() as cur:
            # NP: 30s rolling avg -> 4th power -> mean -> 4th root
            cur.execute("""
                WITH rolling AS (
                    SELECT AVG(power) OVER (
                        ORDER BY time_offset
                        RANGE BETWEEN 29 PRECEDING AND CURRENT ROW
                    ) AS rolling_30s
                    FROM activity_streams
                    WHERE activity_id = %s AND power IS NOT NULL
                )
                SELECT POWER(AVG(POWER(rolling_30s, 4)), 0.25)
                FROM rolling
                WHERE rolling_30s IS NOT NULL
            """, (act_id,))
            row = cur.fetchone()
            np_val = round(row[0], 1) if row and row[0] else None

        if np_val:
            ef_val = compute_ef(np_val, avg_hr)

            with conn.cursor() as cur:
                cur.execute("""
                    SELECT ROUND((SUM(power) / 1000.0)::numeric, 1)
                    FROM activity_streams
                    WHERE activity_id = %s AND power IS NOT NULL
                """, (act_id,))
                work_row = cur.fetchone()
                work_val = float(work_row[0]) if work_row and work_row[0] else None

            with conn.cursor() as cur:
                cur.execute("""
                    UPDATE activities SET np = %s, ef = %s, work_kj = %s WHERE id = %s
                """, (np_val, ef_val, work_val, act_id))
            np_count += 1

    print(f"[fitness] Computed NP/EF/Work for {np_count} activities")

    # Read back stored TSS + distance/elevation (cycling only)
    with conn.cursor() as cur:
        cur.execute("""
            SELECT date::date, COALESCE(tss, 0), distance_m, elevation_m
            FROM activities
            WHERE date IS NOT NULL
            ORDER BY date
        """)
        rows = cur.fetchall()

    if not rows:
        print("[fitness] No activities found, skipping")
        return

    # Build daily aggregates from stored TSS
    daily_tss = {}
    daily_distance = {}
    daily_elevation = {}
    for date, tss, distance_m, elevation_m in rows:
        daily_tss[date] = daily_tss.get(date, 0) + tss
        daily_distance[date] = daily_distance.get(date, 0) + (distance_m or 0)
        daily_elevation[date] = daily_elevation.get(date, 0) + (elevation_m or 0)

    # Walk from first activity to today (rest days still decay CTL/ATL)
    from datetime import date as date_type
    first_date = min(daily_tss.keys())
    last_date = max(max(daily_tss.keys()), date_type.today())

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
