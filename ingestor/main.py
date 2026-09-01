"""Polling scheduler for Strava ingestion."""

import os
import sys
import time
import traceback

import schedule

from db import get_connection, create_schema, get_sync_state, set_sync_state
from strava import sync_activities, backfill, reclassify_activities
import rwgps
import intervals_icu
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


def _enabled_sources() -> list:
    """Activity sources with complete credentials configured."""
    sources = []
    if (os.environ.get("STRAVA_CLIENT_ID") and os.environ.get("STRAVA_CLIENT_SECRET")
            and os.environ.get("STRAVA_REFRESH_TOKEN")):
        sources.append("strava")
    if os.environ.get("RWGPS_API_KEY") and os.environ.get("RWGPS_AUTH_TOKEN"):
        sources.append("rwgps")
    if os.environ.get("INTERVALS_ICU_ATHLETE_ID") and os.environ.get("INTERVALS_ICU_API_KEY"):
        sources.append("intervals_icu")
    return sources


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


def poll_rwgps():
    """Fetch RWGPS sync feed, store activities/streams, handle deletions."""
    conn = None
    try:
        conn = _get_healthy_conn()
        if not conn:
            print("[poll] RWGPS: skipped — no DB connection")
            return
        if get_sync_state(conn, "rwgps_last_sync_datetime"):
            ingested, deleted = rwgps.sync_activities(conn)   # incremental from cursor
        else:
            # No cursor yet — the initial backfill never completed cleanly (e.g. a
            # transient failure withheld the cursor). Re-run the BOUNDED backfill
            # rather than falling through to sync_activities(), whose no-cursor
            # default is since=1970 with no departed_after window — that would
            # silently ingest the user's entire history and ignore the configured
            # VELOMATE_BACKFILL_MONTHS. The bounded retry stays within the window
            # and seeds the cursor once it completes a clean pass.
            print("[poll] RWGPS: no cursor yet — running bounded backfill")
            ingested = rwgps.backfill(conn, months=_backfill_months())
            deleted = 0
        if ingested > 0 or deleted > 0:
            recalculate_fitness(conn)
        print(f"[poll] RWGPS: {ingested} new, {deleted} deleted")
    except Exception as e:
        print(f"[poll] RWGPS error: {e}")
        traceback.print_exc()
    finally:
        if conn:
            conn.close()


def _positive_int_env(name: str, default: int) -> int:
    """Read a positive integer env var, falling back on anything unusable.

    A zero or negative window would be actively dangerous here: the
    reconciliation sweep would compare against an empty remote range and treat
    the whole library as deleted.
    """
    raw = os.environ.get(name, "")
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        print(f"[main] Invalid {name}={raw!r} — falling back to {default}")
        return default
    if value <= 0:
        print(f"[main] Non-positive {name}={value} — falling back to {default}")
        return default
    return value


def poll_intervals_icu():
    """Re-query the intervals.icu sync window and store new/updated activities."""
    conn = None
    try:
        conn = _get_healthy_conn()
        if not conn:
            print("[poll] intervals.icu: skipped — no DB connection")
            return
        ingested, skipped = intervals_icu.sync_activities(
            conn, window_days=_positive_int_env("INTERVALS_ICU_SYNC_WINDOW_DAYS", 14))
        # Only new or re-analysed rides count, so an unchanged window does not
        # trigger a full recalculation every poll.
        if ingested > 0:
            recalculate_fitness(conn)
        print(f"[poll] intervals.icu: {ingested} ingested, {skipped} unavailable")
    except Exception as e:
        print(f"[poll] intervals.icu error: {e}")
        traceback.print_exc()
    finally:
        if conn:
            conn.close()


def _reconcile_intervals_icu():
    """Daily bidirectional sweep — mirrors deletions, reports local gaps."""
    conn = None
    try:
        conn = _get_healthy_conn()
        if conn:
            deleted, recovered = intervals_icu.reconcile(
                conn, sweep_days=_positive_int_env("INTERVALS_ICU_SWEEP_DAYS", 90))
            print(f"[daily] intervals.icu reconcile: {deleted} deleted, "
                  f"{recovered} remote-only")
    except Exception as e:
        print(f"[daily] intervals.icu reconcile error: {e}")
    finally:
        if conn:
            conn.close()


def _backfill_months() -> int:
    """Resolve VELOMATE_BACKFILL_MONTHS env var. Default 12. 0 = full history.
    Invalid values fall back to the default so a typo never blocks ingestion.
    """
    raw = os.environ.get("VELOMATE_BACKFILL_MONTHS", "")
    if not raw:
        return 12
    try:
        value = int(raw)
    except ValueError:
        print(f"[main] Invalid VELOMATE_BACKFILL_MONTHS={raw!r} — falling back to 12")
        return 12
    if value < 0:
        print(f"[main] Negative VELOMATE_BACKFILL_MONTHS={value} — falling back to 12")
        return 12
    return value


def _poll_interval_minutes() -> int:
    """Resolve POLL_INTERVAL_MINUTES env var. Default 10.
    A non-numeric or non-positive value falls back to the default so a typo
    never crash-loops the container after backfill/recalc have already run.
    """
    raw = os.environ.get("POLL_INTERVAL_MINUTES", "")
    if not raw:
        return 10
    try:
        value = int(raw)
    except ValueError:
        print(f"[main] Invalid POLL_INTERVAL_MINUTES={raw!r} — falling back to 10")
        return 10
    if value <= 0:
        print(f"[main] Non-positive POLL_INTERVAL_MINUTES={value} — falling back to 10")
        return 10
    return value


def _parse_backfill_months(value) -> int | None:
    """Parse a persisted backfill months value. Returns None if missing/invalid."""
    if value is None:
        return None
    try:
        return int(value)
    except (ValueError, TypeError):
        return None


def _describe_backfill_months(months: int) -> str:
    """Human-readable description. 0 == full history (infinite)."""
    return "FULL history" if months == 0 else f"{months} months"


def _backfill_window_extended(new_months: int, old_value, has_data: bool) -> bool:
    """True if the backfill window grew since the last persisted value.

    Semantics:
      - 0 means full history / infinite
      - First run (has_data falsy): False — normal first-run path handles it
      - Existing deployment with no persisted value: assume historical default 12
      - Same value: False
      - Extending (new > old, or new=0 when old was bounded): True
      - Shrinking (old > new, or bounded when old was 0): False
      - Corrupted persisted value: True (safer to refresh)
    """
    if not has_data:
        return False
    if old_value is None:
        old_months = 12  # historical default before this feature
    else:
        parsed = _parse_backfill_months(old_value)
        if parsed is None:
            return True  # corrupted — force refresh
        old_months = parsed
    if new_months == old_months:
        return False
    if new_months == 0 and old_months != 0:
        return True
    if old_months == 0:
        return False
    return new_months > old_months


def _backfill_window_shrunk(new_months: int, old_value, has_data: bool) -> bool:
    """True if the backfill window shrank vs the last persisted value.

    Only used for logging — never triggers action, since shrinking is
    non-destructive (the old data stays in the DB). Returns False for
    first runs, missing, or corrupted old values.
    """
    if not has_data or old_value is None:
        return False
    parsed = _parse_backfill_months(old_value)
    if parsed is None:
        return False  # corrupted — extended() handles it
    old_months = parsed
    if new_months == old_months:
        return False
    if old_months == 0 and new_months != 0:
        return True  # full history → bounded window
    if new_months == 0:
        return False  # bounded → full is extending, not shrinking
    return new_months < old_months


def run_backfill(sources: list = None):
    """Backfill the given sources (default: all enabled)."""
    sources = sources if sources is not None else _enabled_sources()
    conn = get_connection()
    try:
        create_schema(conn)
        months = _backfill_months()
        total = 0
        if "strava" in sources:
            total += backfill(conn, months=months)
        if "rwgps" in sources:
            total += rwgps.backfill(conn, months=months)
        recalculate_fitness(conn)
        print(f"[backfill] Complete — {total} activities ingested")
        return total
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
            strava_has_data = get_sync_state(conn, "strava_last_activity_epoch")
            rwgps_has_data = get_sync_state(conn, "rwgps_last_sync_datetime")
            has_data = strava_has_data or rwgps_has_data
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

    enabled = _enabled_sources()
    if not enabled:
        print("[main] No activity source configured — set STRAVA_CLIENT_ID/"
              "STRAVA_CLIENT_SECRET/STRAVA_REFRESH_TOKEN and/or "
              "RWGPS_API_KEY/RWGPS_AUTH_TOKEN")
        sys.exit(1)
    print(f"[main] Enabled sources: {', '.join(enabled)}")

    # Persist configured FTP/HR to sync_state so dashboards can read them.
    # If either value changed (added, removed, or updated), reset all derived metrics.
    try:
        env_ftp = os.environ.get("VELOMATE_FTP", "")
        env_hr = os.environ.get("VELOMATE_MAX_HR", "")
        env_rhr = os.environ.get("VELOMATE_RESTING_HR", "")
        env_weight = os.environ.get("VELOMATE_WEIGHT", "")
        conn = get_connection()
        try:
            ftp = int(env_ftp) if env_ftp else 0
            hr = int(env_hr) if env_hr else 0
            rhr = int(env_rhr) if env_rhr else 0
            ftp_str = str(ftp) if ftp > 0 else "0"
            hr_str = str(hr) if hr > 0 else "0"
            rhr_str = str(rhr) if rhr > 0 else "0"
            try:
                weight = float(env_weight) if env_weight else 0.0
            except ValueError:
                weight = 0.0
            weight_str = str(weight) if weight > 0 else "0"

            # Check if values changed
            old_ftp = get_sync_state(conn, "configured_ftp") or "0"
            old_hr = get_sync_state(conn, "configured_max_hr") or "0"
            old_rhr = get_sync_state(conn, "configured_resting_hr") or "0"
            old_weight = get_sync_state(conn, "configured_weight") or "0"
            # FTP/max HR affect TSS, IF, CTL/ATL/TSB. Resting HR affects TRIMP.
            # Weight only affects ride_weight (W/kg display) — handled separately.
            ftp_changed = (ftp_str != old_ftp)
            weight_changed = (weight_str != old_weight)
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

            # Weight change: only persist the new value. Don't reset existing
            # ride_weight — historical rides keep the weight they were stamped with.
            # New rides (ride_weight IS NULL) pick up the new weight via Step 2.1.
            if weight_changed:
                print(f"[main] Weight changed ({old_weight} → {weight_str}kg) — new rides will use the new value, historical rides preserved")

            # Persist current values (0 = auto-estimate, dashboard queries use value > 0)
            set_sync_state(conn, "configured_ftp", ftp_str)
            set_sync_state(conn, "configured_max_hr", hr_str)
            set_sync_state(conn, "configured_resting_hr", rhr_str)
            set_sync_state(conn, "configured_weight", weight_str)
            print(f"[main] FTP: {ftp}W {'(configured)' if ftp > 0 else '(auto-estimate)'}")
            print(f"[main] Max HR: {hr} {'(configured)' if hr > 0 else '(auto-estimate)'}")
            print(f"[main] Resting HR: {rhr if rhr > 0 else 50} {'(configured)' if rhr > 0 else '(default 50 bpm)'}")
            print(f"[main] Weight: {weight}kg" if weight > 0 else "[main] Weight: not configured (W/kg disabled)")
        finally:
            conn.close()
    except (ValueError, TypeError) as e:
        print(f"[main] Invalid FTP/HR env var (skipping): {e}")
    except Exception as e:
        print(f"[main] Could not persist FTP/HR to sync_state (skipping): {e}")

    # Detect VELOMATE_BACKFILL_MONTHS changes vs last persisted value so that
    # extending the window on a running deployment actually pulls older data.
    new_backfill_months = _backfill_months()
    force_backfill = False
    old_backfill_raw = None
    try:
        conn = get_connection()
        try:
            old_backfill_raw = get_sync_state(conn, "configured_backfill_months")
        finally:
            conn.close()
        force_backfill = _backfill_window_extended(
            new_backfill_months, old_backfill_raw, has_data=bool(has_data)
        )
        new_desc = _describe_backfill_months(new_backfill_months)
        old_parsed = _parse_backfill_months(old_backfill_raw)
        if force_backfill:
            if old_backfill_raw is None:
                # First rollout of this feature on an existing deployment — historical default was 12
                print(
                    f"[main] VELOMATE_BACKFILL_MONTHS extended (historical default 12 months → "
                    f"{new_desc}) — forcing re-backfill to pull older activities"
                )
            elif old_parsed is None:
                # Corrupted persisted value — safer to refresh
                print(
                    f"[main] configured_backfill_months in sync_state is corrupted "
                    f"({old_backfill_raw!r}) — forcing re-backfill"
                )
            else:
                old_desc = _describe_backfill_months(old_parsed)
                print(
                    f"[main] VELOMATE_BACKFILL_MONTHS extended ({old_desc} → {new_desc}) — "
                    f"forcing re-backfill to pull older activities"
                )
        elif _backfill_window_shrunk(
            new_backfill_months, old_backfill_raw, has_data=bool(has_data)
        ):
            # Shrink path only runs when old_parsed is a valid int (_backfill_window_shrunk
            # returns False for None/corrupted) so old_desc is always safe to compute.
            old_desc = _describe_backfill_months(old_parsed)
            print(
                f"[main] VELOMATE_BACKFILL_MONTHS reduced ({old_desc} → {new_desc}) — "
                f"existing older activities remain in the DB."
            )
            print(
                "[main] This variable controls the backfill horizon, not data retention."
            )
            if new_backfill_months > 0:
                print(
                    "[main] To delete older activities manually, run:"
                )
                print(
                    f"[main]   docker compose exec velomate-postgres psql -U velomate -d velomate \\"
                )
                print(
                    f"[main]     -c \"DELETE FROM activities "
                    f"WHERE date < NOW() - INTERVAL '{new_backfill_months} months';\""
                )
    except Exception as e:
        print(f"[main] Could not check backfill window state (skipping detection): {e}")

    # Backfill sources that never synced, OR all enabled sources when the
    # configured window grew
    sources_to_backfill = []
    if force_backfill:
        sources_to_backfill = list(enabled)
        print("[main] Running backfill for extended window")
    else:
        if "strava" in enabled and not strava_has_data:
            sources_to_backfill.append("strava")
        if "rwgps" in enabled and not rwgps_has_data:
            sources_to_backfill.append("rwgps")
        if sources_to_backfill:
            print(f"[main] No previous sync for: {', '.join(sources_to_backfill)} — running backfill")
    if sources_to_backfill:
        run_backfill(sources_to_backfill)

    if not sources_to_backfill:
        # Recalculate fitness on startup to extend CTL/ATL/TSB decay through today
        conn = get_connection()
        try:
            recalculate_fitness(conn)
            print("[main] Fitness recalculated through today")
        finally:
            conn.close()

    # Persist current backfill window so the next restart can detect changes.
    # Done after run_backfill() so a crash during backfill leaves the old value
    # in place and the next restart retries.
    try:
        conn = get_connection()
        try:
            set_sync_state(conn, "configured_backfill_months", str(new_backfill_months))
        finally:
            conn.close()
    except Exception as e:
        print(f"[main] Could not persist configured_backfill_months (non-fatal): {e}")

    interval = _poll_interval_minutes()
    if "strava" in enabled:
        schedule.every(interval).minutes.do(poll_strava)
    if "rwgps" in enabled:
        schedule.every(interval).minutes.do(poll_rwgps)
    if "intervals_icu" in enabled:
        schedule.every(interval).minutes.do(poll_intervals_icu)
        # After the daily fitness recalc, so a sweep-driven deletion is picked
        # up by the next one rather than sitting a day stale.
        schedule.every().day.at("00:20").do(_reconcile_intervals_icu)
    schedule.every().day.at("00:05").do(_daily_fitness_recalc)

    print(f"[main] Polling {', '.join(enabled)} every {interval}min, fitness recalc daily at 00:05")

    # Run once immediately
    if "strava" in enabled:
        poll_strava()
    if "rwgps" in enabled:
        poll_rwgps()
    if "intervals_icu" in enabled:
        poll_intervals_icu()

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
