"""Polling scheduler for Strava + Komoot ingestion."""

import os
import time
import traceback

import schedule

from db import get_connection, create_schema, get_sync_state
from strava import sync_activities, backfill
from komoot import sync_routes
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


def poll_strava():
    """Fetch activities since last sync, store streams, recalculate fitness."""
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


def poll_komoot():
    """Sync routes to DB."""
    try:
        conn = _get_healthy_conn()
        if not conn:
            print("[poll] Komoot: skipped — no DB connection")
            return
        count = sync_routes(conn)
        print(f"[poll] Komoot: {count} routes synced")
    except Exception as e:
        print(f"[poll] Komoot error: {e}")
        traceback.print_exc()


def run_backfill():
    """One-time backfill — call manually or on first run."""
    conn = get_connection()
    create_schema(conn)
    count = backfill(conn, months=12)
    recalculate_fitness(conn)
    sync_routes(conn)
    print(f"[backfill] Complete — {count} activities ingested")
    return count


def run():
    """Main loop: schema init, optional backfill, then poll forever."""
    conn = get_connection()
    create_schema(conn)
    print("[main] Schema ready")

    # Backfill on first run if no activities yet
    has_data = get_sync_state(conn, "strava_last_activity_epoch")
    if not has_data:
        print("[main] No previous sync — running backfill")
        run_backfill()

    interval = int(os.environ.get("POLL_INTERVAL_MINUTES", 10))
    schedule.every(interval).minutes.do(poll_strava)
    schedule.every(1).hours.do(poll_komoot)

    print(f"[main] Polling Strava every {interval}min, Komoot every 1h")

    # Run once immediately
    poll_strava()
    poll_komoot()

    while True:
        schedule.run_pending()
        time.sleep(30)


if __name__ == "__main__":
    run()
