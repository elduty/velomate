"""Polling scheduler for Strava ingestion."""

import os
import sys
import time
import traceback

import schedule

from db import get_connection, create_schema, get_sync_state
from strava import sync_activities, backfill, reclassify_activities
from fitness import recalculate_fitness


def _get_healthy_conn():
    """Get a healthy DB connection, reconnecting if needed."""
    conn = None
    try:
        conn = get_connection()
        # Verify connection is alive
        with conn.cursor() as cur:
            cur.execute("SELECT 1")
        return conn
    except Exception as e:
        print(f"[main] DB connection failed, reconnecting: {e}")
        if conn:
            try:
                conn.close()
            except Exception:
                pass
        try:
            return get_connection()
        except Exception as e2:
            print(f"[main] DB reconnect failed: {e2}")
            return None


def _daily_fitness_recalc():
    """Recalculate fitness at the start of each day so CTL/ATL/TSB decay on rest days."""
    conn = None
    try:
        conn = _get_healthy_conn()
        if conn:
            recalculate_fitness(conn)
            print("[daily] Fitness recalculated through today")
    except Exception as e:
        print(f"[daily] Fitness recalc error: {e}")
    finally:
        if conn:
            conn.close()


def poll_strava():
    """Fetch activities since last sync, store streams, recalculate fitness."""
    conn = None
    try:
        conn = _get_healthy_conn()
        if not conn:
            print("[poll] Strava: skipped — no DB connection")
            return
        count = sync_activities(conn)
        if count > 0:
            recalculate_fitness(conn)
        print(f"[poll] Strava: {count} new activities")
    except Exception as e:
        print(f"[poll] Strava error: {e}")
        traceback.print_exc()
    finally:
        if conn:
            conn.close()


def run_backfill():
    """One-time backfill — call manually or on first run."""
    conn = get_connection()
    try:
        create_schema(conn)
        count = backfill(conn, months=12)
        recalculate_fitness(conn)
        print(f"[backfill] Complete — {count} Strava activities ingested")
        return count
    finally:
        conn.close()


def run():
    """Main loop: schema init, optional backfill, then poll forever."""
    # Retry loop for initial DB connection — common in Docker Compose startup ordering
    max_attempts = 10
    retry_delay = 5
    conn = None
    for attempt in range(1, max_attempts + 1):
        try:
            conn = get_connection()
            create_schema(conn)
            print("[main] Schema ready")
            has_data = get_sync_state(conn, "strava_last_activity_epoch")
            break
        except Exception as e:
            print(f"[main] DB not ready (attempt {attempt}/{max_attempts}): {e}")
            if conn:
                try:
                    conn.close()
                except Exception:
                    pass
                conn = None
            if attempt == max_attempts:
                print("[main] DB unavailable after max retries — exiting")
                sys.exit(1)
            time.sleep(retry_delay)
    if conn:
        conn.close()

    # Persist configured FTP/HR to sync_state so dashboards can read them.
    # If either value changed (added, removed, or updated), reset all derived metrics.
    from db import set_sync_state
    try:
        env_ftp = os.environ.get("VELOMATE_FTP", "")
        env_hr = os.environ.get("VELOMATE_MAX_HR", "")
        env_rhr = os.environ.get("VELOMATE_RESTING_HR", "")
        conn = get_connection()
        try:
            ftp = int(env_ftp) if env_ftp else 0
            hr = int(env_hr) if env_hr else 0
            rhr = int(env_rhr) if env_rhr else 0
            ftp_str = str(ftp) if ftp > 0 else "0"
            hr_str = str(hr) if hr > 0 else "0"
            rhr_str = str(rhr) if rhr > 0 else "0"

            # Check if values changed
            old_ftp = get_sync_state(conn, "configured_ftp") or "0"
            old_hr = get_sync_state(conn, "configured_max_hr") or "0"
            old_rhr = get_sync_state(conn, "configured_resting_hr") or "0"
            # FTP/max HR affect TSS, IF, CTL/ATL/TSB. Resting HR affects TRIMP.
            ftp_changed = (ftp_str != old_ftp)
            config_changed = ftp_changed or (hr_str != old_hr) or (rhr_str != old_rhr)

            # If thresholds changed, reset all derived metrics BEFORE persisting new values.
            # This ensures a crash between reset and persist triggers reset again on restart.
            if config_changed:
                print("[main] FTP/HR/RHR config changed — resetting derived metrics for recalculation")
                with conn.cursor() as cur:
                    # Reset TSS, IF, TRIMP and fitness stats (they depend on thresholds)
                    cur.execute("UPDATE activities SET tss = NULL, intensity_factor = NULL, trimp = NULL")
                    # When FTP changes, also reset per-ride FTP so it gets re-backfilled
                    # with the new configured FTP as fallback
                    if ftp_changed:
                        cur.execute("UPDATE activities SET ride_ftp = NULL")
                        print("[main] ride_ftp reset — will be re-backfilled with new FTP")
                    cur.execute("DELETE FROM athlete_stats")
                print("[main] TSS/IF/TRIMP and CTL/ATL/TSB will be recalculated")

            # Opt-in: reset per-ride FTP so all rides use configured FTP.
            # Set VELOMATE_RESET_RIDE_FTP=1 once, restart, then remove the flag.
            if os.environ.get("VELOMATE_RESET_RIDE_FTP", "") == "1":
                print("[main] VELOMATE_RESET_RIDE_FTP=1 — resetting all ride_ftp and derived metrics")
                with conn.cursor() as cur:
                    cur.execute("UPDATE activities SET ride_ftp = NULL, tss = NULL, intensity_factor = NULL")
                    cur.execute("DELETE FROM athlete_stats")

            # Persist current values (0 = auto-estimate, dashboard queries use value > 0)
            set_sync_state(conn, "configured_ftp", ftp_str)
            set_sync_state(conn, "configured_max_hr", hr_str)
            set_sync_state(conn, "configured_resting_hr", rhr_str)
            print(f"[main] FTP: {ftp}W {'(configured)' if ftp > 0 else '(auto-estimate)'}")
            print(f"[main] Max HR: {hr} {'(configured)' if hr > 0 else '(auto-estimate)'}")
            print(f"[main] Resting HR: {rhr if rhr > 0 else 50} {'(configured)' if rhr > 0 else '(default 50 bpm)'}")
        finally:
            conn.close()
    except (ValueError, TypeError) as e:
        print(f"[main] Invalid FTP/HR env var (skipping): {e}")
    except Exception as e:
        print(f"[main] Could not persist FTP/HR to sync_state (skipping): {e}")

    # Backfill on first run if no activities yet
    if not has_data:
        print("[main] No previous sync — running backfill")
        run_backfill()
    else:
        # Recalculate fitness on startup to extend CTL/ATL/TSB decay through today
        conn = get_connection()
        try:
            recalculate_fitness(conn)
            print("[main] Fitness recalculated through today")
        finally:
            conn.close()

    interval = int(os.environ.get("POLL_INTERVAL_MINUTES", 10))
    schedule.every(interval).minutes.do(poll_strava)
    schedule.every().day.at("00:05").do(_daily_fitness_recalc)

    print(f"[main] Polling Strava every {interval}min, fitness recalc daily at 00:05")

    # Run once immediately
    poll_strava()

    while True:
        schedule.run_pending()
        time.sleep(30)


def run_reclassify():
    """One-time reclassification of all activities using Strava's type field."""
    conn = get_connection()
    try:
        reclassify_activities(conn)
        recalculate_fitness(conn)
        print("[reclassify] Fitness metrics recalculated")
    finally:
        conn.close()


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "reclassify":
        run_reclassify()
    else:
        run()
