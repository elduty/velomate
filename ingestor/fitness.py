"""CTL/ATL/TSB fitness calculator."""

import math
import os
from datetime import timedelta

import psycopg2.extras


DEFAULT_THRESHOLD_HR = 170
DEFAULT_FTP = 150  # Estimated FTP (watts) — fallback only

# Bump this when NP/EF/Work calculation logic changes.
# On startup, if the stored version differs, all values are recalculated.
METRICS_VERSION = "5"  # v5: store IF, TRIMP, VI; single source of truth


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


def compute_trimp(hr_samples: list, max_hr: int, resting_hr: int) -> float:
    """Banister TRIMP from 1-second HR samples.
    TRIMP = SUM((1/60) * HRR * 0.64 * exp(1.92 * HRR))
    HRR = (HR - resting) / (max - resting), capped at 1.0.
    Male coefficients (k=0.64, c=1.92).
    """
    if not hr_samples or not max_hr or max_hr <= resting_hr:
        return 0.0
    hr_range = max_hr - resting_hr
    total = 0.0
    for hr in hr_samples:
        if hr <= resting_hr:
            continue
        hrr = min((hr - resting_hr) / hr_range, 1.0)
        total += (1 / 60) * hrr * 0.64 * math.exp(1.92 * hrr)
    return round(total, 1)


def compute_if(np: float, ftp: int) -> float | None:
    """Intensity Factor = NP / FTP."""
    if not np or not ftp or ftp <= 0:
        return None
    return round(np / ftp, 2)


def compute_vi(np: float, avg_power: int) -> float | None:
    """Variability Index = NP / avg_power."""
    if not np or not avg_power or avg_power <= 0:
        return None
    return round(np / avg_power, 2)


def calculate_tss_power(duration_s: int, np: float, ftp: int) -> float:
    """Power-based TSS = (duration_s × NP × IF) / (FTP × 3600) × 100
    where IF (Intensity Factor) = NP / FTP.
    Uses Normalized Power (not avg power) per Coggan standard."""
    if not duration_s or not np or not ftp:
        return 0.0
    intensity = np / ftp
    return (duration_s * np * intensity) / (ftp * 3600) * 100


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
                        ROWS BETWEEN 1199 PRECEDING AND CURRENT ROW
                    ) AS avg_20min
                FROM activity_streams s
                JOIN recent_activities a ON a.id = s.activity_id
                WHERE s.power IS NOT NULL
            )
            SELECT ROUND(MAX(avg_20min) * 0.95) FROM rolling
            WHERE avg_20min IS NOT NULL
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
    env_max_hr = os.environ.get("VELOMATE_MAX_HR", "")
    env_ftp = os.environ.get("VELOMATE_FTP", "")

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

    env_rhr = os.environ.get("VELOMATE_RESTING_HR", "")
    try:
        rhr_val = int(env_rhr) if env_rhr else 0
    except ValueError:
        rhr_val = 0
    resting_hr = rhr_val if rhr_val > 0 else 50
    print(f"[fitness] Resting HR: {resting_hr} {'(configured)' if rhr_val > 0 else '(default 50 bpm)'}")

    # Persist estimated FTP so Grafana can read it directly from sync_state
    import db as _db
    _db.set_sync_state(conn, "estimated_ftp", str(ftp))

    # Check metrics version — reset all derived metrics if calculation logic changed
    stored_version = _db.get_sync_state(conn, "metrics_version")
    if stored_version != METRICS_VERSION:
        print(f"[fitness] Metrics version changed ({stored_version} → {METRICS_VERSION}), recalculating everything...")
        with conn.cursor() as cur:
            cur.execute("UPDATE activities SET tss = NULL, np = NULL, ef = NULL, work_kj = NULL, ride_ftp = NULL, intensity_factor = NULL, trimp = NULL, variability_index = NULL")
            cur.execute("DELETE FROM athlete_stats")
        _db.set_sync_state(conn, "metrics_version", METRICS_VERSION)

    # Step 1: Compute NP, EF, Work for activities with power stream data
    # NP must be computed BEFORE TSS because TSS uses NP (Coggan standard)
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
            cur.execute("""
                WITH rolling AS (
                    SELECT
                        AVG(power) OVER (
                            ORDER BY time_offset
                            ROWS BETWEEN 29 PRECEDING AND CURRENT ROW
                        ) AS rolling_30s,
                        power
                    FROM activity_streams
                    WHERE activity_id = %s AND power IS NOT NULL
                )
                SELECT
                    POWER(AVG(POWER(rolling_30s, 4)), 0.25),
                    ROUND((SUM(power) / 1000.0)::numeric, 1)
                FROM rolling
                WHERE rolling_30s IS NOT NULL
            """, (act_id,))
            row = cur.fetchone()
            np_val = round(row[0], 1) if row and row[0] else None
            work_val = float(row[1]) if row and row[1] else None

        if np_val:
            ef_val = compute_ef(np_val, avg_hr)
            vi_val = compute_vi(np_val, avg_power)
            with conn.cursor() as cur:
                cur.execute("""
                    UPDATE activities SET np = %s, ef = %s, work_kj = %s, variability_index = %s WHERE id = %s
                """, (np_val, ef_val, work_val, vi_val, act_id))
            np_count += 1

    print(f"[fitness] Computed NP/EF/Work for {np_count} activities")

    # Step 2: Backfill ride_ftp for historical rides that don't have one.
    # Uses the best 20-min power from the 90 days before each ride's date.
    with conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM activities WHERE ride_ftp IS NULL AND date IS NOT NULL")
        unfilled = cur.fetchone()[0]

    if unfilled > 0:
        print(f"[fitness] Backfilling ride_ftp for {unfilled} activities...")
        with conn.cursor() as cur:
            # For rides with power stream data: estimate FTP from prior 90 days
            cur.execute("""
                UPDATE activities a SET ride_ftp = sub.est_ftp
                FROM (
                    SELECT a2.id,
                        COALESCE(
                            (SELECT ROUND(MAX(rolling_avg) * 0.95)
                             FROM (
                                SELECT AVG(s.power) OVER w AS rolling_avg,
                                    COUNT(*) OVER w AS window_size
                                FROM activity_streams s
                                JOIN activities a3 ON a3.id = s.activity_id
                                WHERE a3.date BETWEEN a2.date - interval '90 days' AND a2.date - interval '1 day'
                                  AND s.power IS NOT NULL
                                WINDOW w AS (PARTITION BY s.activity_id ORDER BY s.time_offset ROWS BETWEEN 1199 PRECEDING AND CURRENT ROW)
                            ) t WHERE rolling_avg IS NOT NULL AND window_size >= 1200),
                            %s
                        ) AS est_ftp
                    FROM activities a2
                    WHERE a2.ride_ftp IS NULL AND a2.date IS NOT NULL
                ) sub
                WHERE a.id = sub.id
            """, (ftp,))  # fallback to current FTP if no prior stream data
            backfilled = cur.rowcount
        print(f"[fitness] Backfilled ride_ftp for {backfilled} activities")

    # For new rides (just ingested), stamp current FTP if not set
    # Stamp current FTP on any rides still missing ride_ftp (new rides, or backfill gaps)
    with conn.cursor() as cur:
        cur.execute("""
            UPDATE activities SET ride_ftp = %s
            WHERE ride_ftp IS NULL AND date IS NOT NULL
        """, (ftp,))
        if cur.rowcount > 0:
            print(f"[fitness] Stamped current FTP ({ftp}W) on {cur.rowcount} rides without historical FTP")

    # Step 3: Compute TSS using per-ride FTP (ride_ftp), NP preferred, fallbacks
    # Standard Coggan TSS = (duration × NP × IF) / (FTP × 3600) × 100 where IF = NP/FTP
    with conn.cursor() as cur:
        cur.execute("""
            SELECT id, duration_s, avg_hr, avg_power, np, ride_ftp
            FROM activities
            WHERE date IS NOT NULL
        """)
        activity_rows = cur.fetchall()

    tss_updates = []
    for act_id, duration_s, avg_hr, avg_power, np_val, ride_ftp_val in activity_rows:
        act_ftp = ride_ftp_val if ride_ftp_val and ride_ftp_val > 0 else ftp
        if np_val and np_val > 0:
            tss = calculate_tss_power(duration_s, np_val, act_ftp)
        elif avg_power and avg_power > 0:
            tss = calculate_tss_power(duration_s, avg_power, act_ftp)
        elif avg_hr and avg_hr > 0:
            tss = calculate_tss(duration_s, avg_hr, threshold_hr)
        else:
            tss = 0
        if_val = compute_if(np_val, act_ftp) if np_val and np_val > 0 else None
        tss_updates.append((round(tss, 1), if_val, act_id))

    with conn.cursor() as cur:
        psycopg2.extras.execute_batch(
            cur, "UPDATE activities SET tss = %s, intensity_factor = %s WHERE id = %s", tss_updates
        )

    # Step 4: Compute TRIMP for activities that don't have it yet
    print("[fitness] Computing TRIMP...")
    with conn.cursor() as cur:
        cur.execute("""
            SELECT a.id FROM activities a
            WHERE a.trimp IS NULL AND a.date IS NOT NULL
              AND EXISTS (SELECT 1 FROM activity_streams s WHERE s.activity_id = a.id AND s.hr IS NOT NULL)
        """)
        trimp_ids = [row[0] for row in cur.fetchall()]

    trimp_count = 0
    for act_id in trimp_ids:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT hr FROM activity_streams
                WHERE activity_id = %s AND hr IS NOT NULL
                ORDER BY time_offset
            """, (act_id,))
            hr_samples = [row[0] for row in cur.fetchall()]

        trimp_val = compute_trimp(hr_samples, threshold_hr, resting_hr)
        with conn.cursor() as cur:
            cur.execute("UPDATE activities SET trimp = %s WHERE id = %s", (trimp_val, act_id))
        trimp_count += 1

    print(f"[fitness] Computed TRIMP for {trimp_count} activities")

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

    conn.autocommit = False
    try:
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
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.autocommit = True

    print(f"[fitness] Calculated {count} days of fitness data (CTL={ctl:.1f}, ATL={atl:.1f}, TSB={ctl-atl:.1f})")
