"""CTL/ATL/TSB fitness calculator."""

import math
import os
from datetime import timedelta

import psycopg2.extras

from intervals import detect_intervals


DEFAULT_THRESHOLD_HR = 170
DEFAULT_FTP = 150  # Estimated FTP (watts) — fallback only

# VI boundary above which the Coggan NP-based TSS overestimates physiological
# load. Stop-and-go urban rides (coasting + surges) drive NP's 4th-power
# weighting way above the sustained effort the rider actually produced. Above
# this threshold, VeloMate computes TSS from avg_power instead of NP. Below
# (the typical 1.0–1.3 range) NP remains the right input for the Coggan model.
HIGH_VI_THRESHOLD = 1.30

# Bump this when NP/EF/Work calculation logic changes.
# On startup, if the stored version differs, all values are recalculated.
METRICS_VERSION = "10"  # v10: VI-aware TSS uses avg_power when VI > 1.30


def calculate_tss(duration_s: int, avg_hr: int, threshold_hr: int) -> float:
    """HR-based TSS = (duration_h) × (avg_hr / threshold_hr)² × 100"""
    if not duration_s or not avg_hr or not threshold_hr:
        return 0.0
    duration_h = duration_s / 3600
    intensity = avg_hr / threshold_hr
    return duration_h * (intensity ** 2) * 100


def compute_np(power_samples: list) -> float | None:
    """Normalized Power from 1-second power samples using 30-second SMA.
    Coggan standard (matches GoldenCheetah IsoPower):
      1. Compute 30-second simple moving average (circular buffer, always divide by 30)
      2. Raise each to the 4th power
      3. Take the mean
      4. Take the 4th root
    """
    if not power_samples or len(power_samples) < 30:
        return None
    window = 30
    buf = [0.0] * window
    idx = 0
    rolling_sum = 0.0
    total = 0.0
    count = len(power_samples)
    for watts in power_samples:
        rolling_sum += watts - buf[idx]
        buf[idx] = watts
        idx = (idx + 1) % window
        total += (rolling_sum / window) ** 4
    np_val = (total / count) ** 0.25
    return round(np_val, 1) if np_val > 0 else None


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


def select_power_for_tss(np, avg_power):
    """Pick which power value to feed into Coggan TSS given the ride's VI.

    The Coggan NP-based TSS formula assumes steady or near-steady effort
    (VI ≈ 1.0–1.2). On rides dominated by coasting + surges (VI > 1.30) —
    urban commutes, crit-style bunch rides, technical MTB — the NP 4th-power
    weighting overestimates sustained physiological load, which in turn
    inflates TSS, ATL, and pushes TSB unnaturally negative.

    Rule:
      - Both present and VI > HIGH_VI_THRESHOLD → use avg_power
      - Both present and VI ≤ HIGH_VI_THRESHOLD → use NP (Coggan standard)
      - Only one usable (non-None, > 0) → use whichever is usable
      - Neither usable → return None (caller falls back to HR-based TSS or 0)

    Returns the chosen power value (watts) or None.
    """
    np_ok = bool(np and np > 0)
    avg_ok = bool(avg_power and avg_power > 0)
    if np_ok and avg_ok:
        vi = np / avg_power
        return avg_power if vi > HIGH_VI_THRESHOLD else np
    if np_ok:
        return np
    if avg_ok:
        return avg_power
    return None


def compute_decoupling(power_samples: list, hr_samples: list) -> float | None:
    """Aerobic decoupling (Friel) from matched power + HR 1-second streams.
    Splits the ride into two halves by time index, computes EF (avg_power/avg_hr)
    for each half from its valid samples, returns (first_EF / second_EF - 1) * 100
    as a percentage.

    Positive values = cardiac drift (HR rising relative to power in the second half),
    which is a leading indicator of aerobic fatigue or insufficient base fitness.

    Returns None when streams are missing, mismatched, too short for a
    meaningful split, or when either half lacks valid HR data. None values
    inside the streams are filtered per-half.
    """
    if not power_samples or not hr_samples:
        return None
    if len(power_samples) != len(hr_samples):
        return None
    if len(power_samples) < 4:  # need at least 2 samples per half
        return None

    mid = len(power_samples) // 2
    first_power, first_hr = power_samples[:mid], hr_samples[:mid]
    second_power, second_hr = power_samples[mid:], hr_samples[mid:]

    def ef(power_half, hr_half):
        pairs = [(p, h) for p, h in zip(power_half, hr_half)
                 if p is not None and h is not None and h > 0]
        if len(pairs) < 2:
            return None
        avg_p = sum(p for p, _ in pairs) / len(pairs)
        avg_h = sum(h for _, h in pairs) / len(pairs)
        if avg_h <= 0:
            return None
        return avg_p / avg_h

    first_ef = ef(first_power, first_hr)
    second_ef = ef(second_power, second_hr)
    if first_ef is None or second_ef is None or second_ef == 0:
        return None

    return round((first_ef / second_ef - 1) * 100, 2)


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

    # Always compute the auto-estimate so it can be persisted as a diagnostic
    # value alongside the configured FTP, regardless of which one is in use.
    auto_ftp = estimate_ftp(conn)
    if ftp_val > 0:
        ftp = ftp_val
        print(f"[fitness] Using configured FTP: {ftp}W (algorithmic estimate: {auto_ftp}W)")
    else:
        ftp = auto_ftp
        print(f"[fitness] Auto-estimated FTP: {ftp}W (rolling 90-day best 20min × 0.95)")

    env_rhr = os.environ.get("VELOMATE_RESTING_HR", "")
    try:
        rhr_val = int(env_rhr) if env_rhr else 0
    except ValueError:
        rhr_val = 0
    resting_hr = rhr_val if rhr_val > 0 else 50
    print(f"[fitness] Resting HR: {resting_hr} {'(configured)' if rhr_val > 0 else '(default 50 bpm)'}")

    env_weight = os.environ.get("VELOMATE_WEIGHT", "")
    try:
        weight = float(env_weight) if env_weight else 0.0
    except ValueError:
        weight = 0.0

    # Persist the algorithmic estimate so Grafana can read it directly from
    # sync_state. This is the auto-computed value, NOT the currently-active
    # FTP — when configured_ftp is set, the two diverge and the difference is
    # the diagnostic signal ("recalibrate?").
    import db as _db
    _db.set_sync_state(conn, "estimated_ftp", str(auto_ftp))

    # Check metrics version — reset all derived metrics if calculation logic changed
    stored_version = _db.get_sync_state(conn, "metrics_version")
    if stored_version != METRICS_VERSION:
        print(f"[fitness] Metrics version changed ({stored_version} → {METRICS_VERSION}), recalculating everything...")
        with conn.cursor() as cur:
            # ride_weight intentionally excluded — it's user-configured, not derived.
            # Historical rides preserve their stamped weight across version bumps.
            cur.execute("UPDATE activities SET tss = NULL, np = NULL, ef = NULL, work_kj = NULL, ride_ftp = NULL, intensity_factor = NULL, trimp = NULL, variability_index = NULL, aerobic_decoupling = NULL")
            cur.execute("DELETE FROM athlete_stats")
            cur.execute("DELETE FROM ride_intervals")
        _db.set_sync_state(conn, "metrics_version", METRICS_VERSION)

    # Step 1: Compute NP, EF, Work for activities with power stream data
    # NP uses 30-second SMA (Coggan standard), computed in Python
    print("[fitness] Computing NP/EF/Work...")
    with conn.cursor() as cur:
        cur.execute("""
            SELECT a.id, a.avg_hr, a.avg_power, a.np, a.aerobic_decoupling
            FROM activities a
            WHERE (a.np IS NULL OR a.aerobic_decoupling IS NULL) AND a.date IS NOT NULL
              AND EXISTS (
                  SELECT 1 FROM activity_streams s
                  WHERE s.activity_id = a.id AND s.power IS NOT NULL
                  GROUP BY s.activity_id HAVING COUNT(*) > 30
              )
        """)
        power_activities = cur.fetchall()

    np_count = 0
    decoupling_count = 0
    for act_id, avg_hr, avg_power, existing_np, existing_decoupling in power_activities:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT power, hr FROM activity_streams
                WHERE activity_id = %s AND power IS NOT NULL
                ORDER BY time_offset
            """, (act_id,))
            rows = cur.fetchall()
            power_samples = [r[0] for r in rows]
            hr_samples = [r[1] for r in rows]
            work_val = round(sum(power_samples) / 1000.0, 1)

        # Only compute NP/EF/VI/Work if missing (avoids redundant work)
        np_val = existing_np
        if existing_np is None:
            np_val = compute_np(power_samples)
            if np_val:
                ef_val = compute_ef(np_val, avg_hr)
                vi_val = compute_vi(np_val, avg_power)
                with conn.cursor() as cur:
                    cur.execute("""
                        UPDATE activities SET np = %s, ef = %s, work_kj = %s, variability_index = %s WHERE id = %s
                    """, (np_val, ef_val, work_val, vi_val, act_id))
                np_count += 1

        # Compute decoupling if missing and HR stream has any data
        if existing_decoupling is None and any(h is not None and h > 0 for h in hr_samples):
            dec_val = compute_decoupling(power_samples, hr_samples)
            if dec_val is not None:
                with conn.cursor() as cur:
                    cur.execute("UPDATE activities SET aerobic_decoupling = %s WHERE id = %s", (dec_val, act_id))
                decoupling_count += 1

    print(f"[fitness] Computed NP/EF/Work for {np_count} activities")
    print(f"[fitness] Computed aerobic decoupling for {decoupling_count} activities")

    # Step 2: Backfill ride_ftp for historical rides that don't have one.
    # Uses the best 20-min power from the 90 days before each ride's date.
    with conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM activities WHERE ride_ftp IS NULL AND date IS NOT NULL")
        unfilled = cur.fetchone()[0]

    if unfilled > 0:
        if ftp_val > 0:
            # FTP explicitly configured — use it for all rides, skip stream-based estimation
            with conn.cursor() as cur:
                cur.execute("UPDATE activities SET ride_ftp = %s WHERE ride_ftp IS NULL AND date IS NOT NULL", (ftp,))
                stamped = cur.rowcount
            print(f"[fitness] Stamped configured FTP ({ftp}W) on {stamped} rides")
        else:
            # Auto-estimate: backfill from rolling 90-day best 20-min power per ride
            print(f"[fitness] Backfilling ride_ftp for {unfilled} activities...")
            with conn.cursor() as cur:
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
                """, (ftp,))
                backfilled = cur.rowcount
            print(f"[fitness] Backfilled ride_ftp for {backfilled} activities")

            # Stamp auto-estimated FTP on any remaining rides without enough prior data
            with conn.cursor() as cur:
                cur.execute("""
                    UPDATE activities SET ride_ftp = %s
                    WHERE ride_ftp IS NULL AND date IS NOT NULL
                """, (ftp,))
                if cur.rowcount > 0:
                    print(f"[fitness] Stamped estimated FTP ({ftp}W) on {cur.rowcount} rides without historical data")

    # Step 2.1: Backfill ride_weight for rides that don't have one.
    # Unlike FTP, weight can't be auto-estimated — purely user-configured.
    if weight > 0:
        with conn.cursor() as cur:
            cur.execute("UPDATE activities SET ride_weight = %s WHERE ride_weight IS NULL AND date IS NOT NULL", (weight,))
            if cur.rowcount > 0:
                print(f"[fitness] Stamped weight ({weight}kg) on {cur.rowcount} rides")

    # Step 2.5: Detect intervals for rides with no ride_intervals rows yet.
    # Runs AFTER ride_ftp backfill (Step 2) so classification uses per-ride historical
    # FTP, not the current global FTP — critical for correct classification on
    # METRICS_VERSION bumps that reset ride_ftp for every activity.
    print("[fitness] Detecting intervals...")
    with conn.cursor() as cur:
        cur.execute("""
            SELECT a.id, a.ride_ftp
            FROM activities a
            WHERE a.date IS NOT NULL
              AND NOT EXISTS (SELECT 1 FROM ride_intervals ri WHERE ri.activity_id = a.id)
              AND EXISTS (
                  SELECT 1 FROM activity_streams s
                  WHERE s.activity_id = a.id AND s.power IS NOT NULL
                  GROUP BY s.activity_id HAVING COUNT(*) > 30
              )
        """)
        interval_activities = cur.fetchall()

    interval_activity_count = 0
    interval_row_count = 0
    for act_id, act_ride_ftp in interval_activities:
        with conn.cursor() as cur:
            # Fetch time_offset alongside power+hr so detected sample indices can be
            # translated to real time offsets (handles rare power-meter dropout gaps).
            cur.execute("""
                SELECT time_offset, power, hr FROM activity_streams
                WHERE activity_id = %s AND power IS NOT NULL
                ORDER BY time_offset
            """, (act_id,))
            rows = cur.fetchall()
            time_offsets = [r[0] for r in rows]
            power_samples = [r[1] for r in rows]
            hr_samples = [r[2] for r in rows]

        act_ftp = act_ride_ftp if act_ride_ftp and act_ride_ftp > 0 else ftp
        detected = detect_intervals(power_samples, ftp=act_ftp)
        if not detected:
            continue

        insert_rows = []
        for d in detected:
            start_idx = d["start_offset_s"]
            end_idx = start_idx + d["duration_s"]
            # Map sample index → real time_offset from the stream (unfiltered second
            # count). In streams without NULL-power gaps these are identical; with
            # gaps the sample-index based start would otherwise display wrong in
            # the Activity Details "Start" column.
            real_start = time_offsets[start_idx] if start_idx < len(time_offsets) else start_idx
            hr_slice = [h for h in hr_samples[start_idx:end_idx] if h is not None and h > 0]
            avg_hr_val = int(round(sum(hr_slice) / len(hr_slice))) if hr_slice else None
            insert_rows.append((
                act_id, real_start, d["duration_s"], d["avg_power"],
                d["np"], d["max_power"], avg_hr_val, d["classification"],
            ))
        with conn.cursor() as cur:
            psycopg2.extras.execute_values(
                cur,
                """INSERT INTO ride_intervals
                    (activity_id, start_offset_s, duration_s, avg_power, np, max_power, avg_hr, classification)
                   VALUES %s""",
                insert_rows,
            )
        interval_activity_count += 1
        interval_row_count += len(insert_rows)

    print(f"[fitness] Detected {interval_row_count} intervals across {interval_activity_count} activities")

    # Step 3: Compute TSS using per-ride FTP (ride_ftp). VI-aware input:
    # NP for standard-variability rides (VI ≤ 1.30), avg_power for high-VI
    # rides (urban stop-and-go) where NP overestimates sustained load. HR
    # fallback when no power stream exists. IF is computed from the SAME
    # power input used for TSS so IF² × duration_h × 100 ≈ TSS holds.
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
        tss_power = select_power_for_tss(np_val, avg_power)
        if tss_power is not None:
            tss = calculate_tss_power(duration_s, tss_power, act_ftp)
            if_val = compute_if(tss_power, act_ftp)
        elif avg_hr and avg_hr > 0:
            # Coggan HR TSS = duration_h × (avg_hr / LTHR)² × 100.
            # LTHR (Lactate Threshold HR) ≈ 89% of max HR per Friel convention.
            # threshold_hr at this point holds max HR (from VELOMATE_MAX_HR or
            # the estimate_threshold_hr auto-estimate), so derive LTHR here
            # before feeding it into the HR TSS formula.
            lthr = int(round(threshold_hr * 0.89))
            tss = calculate_tss(duration_s, avg_hr, lthr)
            if_val = None
        else:
            tss = 0
            if_val = None
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
