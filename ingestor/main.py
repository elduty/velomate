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
    try:
        conn = get_connection()
        # Verify connection is alive
        with conn.cursor() as cur:
            cur.execute("SELECT 1")
        return conn
    except Exception as e:
        print(f"[main] DB connection failed, reconnecting: {e}")
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
    conn = get_connection()
    try:
        create_schema(conn)
        print("[main] Schema ready")
        has_data = get_sync_state(conn, "strava_last_activity_epoch")
    finally:
        conn.close()

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
