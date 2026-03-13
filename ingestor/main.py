"""Polling scheduler for Strava + Komoot ingestion."""

import os
import time
import traceback

import schedule

from db import get_connection, create_schema, get_sync_state
from strava import sync_activities, backfill
from komoot import sync_routes
from fitness import recalculate_fitness


def poll_strava(conn):
    """Fetch activities since last sync, store streams, recalculate fitness."""
    try:
        count = sync_activities(conn)
        if count > 0:
            recalculate_fitness(conn)
        print(f"[poll] Strava: {count} new activities")
    except Exception as e:
        print(f"[poll] Strava error: {e}")
        traceback.print_exc()


def poll_komoot(conn):
    """Sync routes to DB."""
    try:
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
    schedule.every(interval).minutes.do(poll_strava, conn)
    schedule.every(1).hours.do(poll_komoot, conn)

    print(f"[main] Polling Strava every {interval}min, Komoot every 1h")

    # Run once immediately
    poll_strava(conn)
    poll_komoot(conn)

    while True:
        schedule.run_pending()
        time.sleep(30)


if __name__ == "__main__":
    run()
