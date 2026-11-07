"""Polling scheduler: the cloud sources and the watched import folder."""

import os
import signal
import sys
import time
import traceback
from datetime import datetime, timezone

import schedule

from db import (backfill_missing_speed, create_schema, get_connection, get_sync_state,
                has_activities, set_sync_state)
from strava import sync_activities, backfill, reclassify_activities
import rwgps
import intervals_icu
import file_import
from fitness import configured_threshold, recalculate_fitness

# Ingest health, read by the Overview stats and the Ingestion Stalled alert. Those
# queries use the row's typed last_synced_at, so `value` is only for a human
# reading sync_state and a bad one cannot fail them.
LAST_SUCCESSFUL_POLL_KEY = "last_successful_poll"
SKIPPED_STUBS_KEY = "intervals_icu_skipped_stubs"

# Health sources that have delivered since last_successful_poll last advanced.
_delivered = set()

# Rides are committed before recalculation runs, and the next poll sees them as
# unchanged, so a failed recalculation has to be remembered: otherwise it is never
# retried, and it stops the poll counting as a success for one cycle only.
_recalc_pending = False


def _recalculate_if_needed(conn, changed: bool):
    """Recalculate if this poll changed anything or an earlier recalculation failed."""
    global _recalc_pending
    _recalc_pending = _recalc_pending or changed
    if _recalc_pending:
        recalculate_fitness(conn)
        _recalc_pending = False


def _recalculate_at_startup(conn):
    """Recalculate before the poll loop starts. A raise here would restart the
    container into the same failure, so it stays pending for the next poll."""
    try:
        _recalculate_if_needed(conn, True)
        print("[main] Fitness recalculated through today")
    except Exception as e:
        print(f"[main] Fitness recalculation failed, the next poll retries it: {e}")
        traceback.print_exc()


def _backfill_speed_at_startup(conn):
    """Give stored GPS rides with no speed one. Failing only leaves them as they were,
    so it never stops the ingestor."""
    try:
        filled = backfill_missing_speed(conn)
        if filled:
            print(f"[main] Derived speed from GPS for {filled} stored rides")
    except Exception as e:
        print(f"[main] Deriving speed for stored rides failed, the next start retries it: {e}")
        traceback.print_exc()


def _write_health(conn, key: str, value: str):
    """Write a health key. Health only reports on the poll, so a failure here is
    logged and never takes the poll down with it."""
    try:
        set_sync_state(conn, key, value)
    except Exception as e:
        print(f"[health] Could not write {key}: {e}")


def _record_poll(conn, source: str, problems: list):
    """Advance last_successful_poll once every health source has delivered.

    The key is one row, so a healthy source must not hide a dead one: it moves
    only when the whole health set has come good since the last move.

    `problems` is what the sync swallowed. Without it a poll where every ride
    failed, or where every ride is an unreadable stub, reaches here exactly like
    a quiet week with nothing new, and only the quiet week is healthy.
    """
    if problems:
        print(f"[health] {source} poll not counted as a success: {'; '.join(problems)}")
        return
    _delivered.add(source)
    if _delivered.issuperset(_health_sources()):
        _delivered.clear()
        _write_health(conn, LAST_SUCCESSFUL_POLL_KEY, datetime.now(timezone.utc).isoformat())


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


def _health_sources() -> list:
    """What has to deliver before last_successful_poll moves: the cloud sources,
    plus the folder when it is configured. An unconfigured folder is the empty
    default volume, which can't stall, so an install that never set
    VELOMATE_IMPORT_DIR keeps the health set it had."""
    sources = _enabled_sources()
    if file_import.folder_configured():
        sources.append("file")
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
        problems = []
        count = sync_activities(conn, problems=problems)
        _recalculate_if_needed(conn, count > 0)
        _record_poll(conn, "strava", problems)
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
        problems = []
        if get_sync_state(conn, "rwgps_last_sync_datetime"):
            # incremental from cursor
            ingested, deleted = rwgps.sync_activities(conn, problems=problems)
        else:
            # No cursor yet — the initial backfill never completed cleanly (e.g. a
            # transient failure withheld the cursor). Re-run the BOUNDED backfill
            # rather than falling through to sync_activities(), whose no-cursor
            # default is since=1970 with no departed_after window — that would
            # silently ingest the user's entire history and ignore the configured
            # VELOMATE_BACKFILL_MONTHS. The bounded retry stays within the window
            # and seeds the cursor once it completes a clean pass.
            print("[poll] RWGPS: no cursor yet — running bounded backfill")
            ingested = rwgps.backfill(conn, months=_backfill_months(), problems=problems)
            deleted = 0
        _recalculate_if_needed(conn, ingested > 0 or deleted > 0)
        _record_poll(conn, "rwgps", problems)
        print(f"[poll] RWGPS: {ingested} new, {deleted} deleted")
    except Exception as e:
        print(f"[poll] RWGPS error: {e}")
        traceback.print_exc()
    finally:
        if conn:
            conn.close()


def poll_folder():
    """Import new or changed files from the watched folder, then recalculate.

    With no health source at all (no cloud source and no configured folder, so
    rides arrive only through the upload page) nothing can stall, so the tick
    itself advances last_successful_poll: the stall alert then means the
    ingestor stopped.
    """
    conn = None
    try:
        conn = _get_healthy_conn()
        if not conn:
            print("[poll] Folder: skipped — no DB connection")
            return
        changed, problems = file_import.scan_folder(conn)
        _recalculate_if_needed(conn, changed > 0)
        configured = file_import.folder_configured()
        if configured:
            _record_poll(conn, "file", problems)
        elif problems:
            print(f"[health] folder pass not counted as a success: {'; '.join(problems)}")
        elif not _health_sources():
            _write_health(conn, LAST_SUCCESSFUL_POLL_KEY, datetime.now(timezone.utc).isoformat())
        if configured or changed or problems:
            print(f"[poll] Folder: {changed} rides imported or merged")
    except Exception as e:
        print(f"[poll] Folder error: {e}")
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
        problems = []
        ingested, skipped = intervals_icu.sync_activities(
            conn, window_days=_positive_int_env("INTERVALS_ICU_SYNC_WINDOW_DAYS", 14),
            problems=problems)
        # Written whatever the verdict: a window of nothing but stubs is not a
        # success, and it is the state this count exists to show. Zero is written
        # too, or stubs that left the window would stay on the dashboard.
        _write_health(conn, SKIPPED_STUBS_KEY, str(skipped))
        # Only new or re-analysed rides count, so an unchanged window does not
        # trigger a full recalculation every poll.
        _recalculate_if_needed(conn, ingested > 0)
        _record_poll(conn, "intervals_icu", problems)
        print(f"[poll] intervals.icu: {ingested} ingested, {skipped} unavailable")
    except Exception as e:
        print(f"[poll] intervals.icu error: {e}")
        traceback.print_exc()
    finally:
        if conn:
            conn.close()


def _backfill_intervals_icu(conn, months: int) -> int:
    """One backfill pass. Never raises: this runs at startup, where an exception
    restarts the container into the same failure. An unfinished pass leaves its
    pending key set, so the next start or daily sweep picks it up."""
    try:
        return intervals_icu.backfill(conn, months=months)
    except Exception as e:
        print(f"[backfill] intervals.icu error: {e}")
        traceback.print_exc()
        return 0


def _reconcile_intervals_icu():
    """Daily bidirectional sweep — mirrors deletions, ingests remote-only rides,
    and resumes a backfill an earlier pass left unfinished."""
    conn = None
    try:
        conn = _get_healthy_conn()
        if conn:
            deleted, recovered = intervals_icu.reconcile(
                conn, sweep_days=_positive_int_env("INTERVALS_ICU_SWEEP_DAYS", 90))
            print(f"[daily] intervals.icu reconcile: {deleted} deleted, "
                  f"{recovered} recovered")
            resumed = 0
            if get_sync_state(conn, intervals_icu.BACKFILL_PENDING_KEY):
                resumed = _backfill_intervals_icu(conn, _backfill_months())
            _recalculate_if_needed(conn, recovered > 0 or resumed > 0)
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
        # Written before any source runs: their rows make the next start read as an
        # existing install, so this is all that says intervals.icu is still owed.
        if "intervals_icu" in sources:
            set_sync_state(conn, intervals_icu.BACKFILL_PENDING_KEY, str(months))
        if "strava" in sources:
            total += backfill(conn, months=months)
        if "rwgps" in sources:
            total += rwgps.backfill(conn, months=months)
        if "intervals_icu" in sources:
            total += _backfill_intervals_icu(conn, months)
        _recalculate_at_startup(conn)
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
            icu_backfill_pending = get_sync_state(conn, intervals_icu.BACKFILL_PENDING_KEY)
            # intervals.icu has no cursor to read, so the rows themselves answer
            # for it. Any stored ride counts, whoever delivered it: only an empty
            # install backfills on its own, and an existing one asks for more
            # history by extending VELOMATE_BACKFILL_MONTHS.
            has_data = strava_has_data or rwgps_has_data or has_activities(conn)
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

    # Never exit for want of a source: the upload page is a separate container
    # this one can't see, so an exit would crash-loop an upload-only install.
    enabled = _enabled_sources()
    if enabled:
        print(f"[main] Enabled sources: {', '.join(enabled)}")
    else:
        print("[main] No cloud source configured — rides arrive only through the "
              "import folder or the upload page")

    # Persist configured FTP/HR to sync_state so dashboards can read them.
    # If either value changed (added, removed, or updated), reset all derived metrics.
    try:
        env_weight = os.environ.get("VELOMATE_WEIGHT", "")
        conn = get_connection()
        try:
            ftp = configured_threshold(conn, "VELOMATE_FTP", "configured_ftp")
            hr = configured_threshold(conn, "VELOMATE_MAX_HR", "configured_max_hr")
            rhr = configured_threshold(conn, "VELOMATE_RESTING_HR", "configured_resting_hr")
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
        if "intervals_icu" in enabled and (not has_data or icu_backfill_pending):
            sources_to_backfill.append("intervals_icu")
        if sources_to_backfill:
            print(f"[main] No previous sync for: {', '.join(sources_to_backfill)} — running backfill")
    if sources_to_backfill:
        run_backfill(sources_to_backfill)

    if not sources_to_backfill:
        # Recalculate fitness on startup to extend CTL/ATL/TSB decay through today
        conn = get_connection()
        try:
            _backfill_speed_at_startup(conn)
            _recalculate_at_startup(conn)
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
    # Always, configured or not: unconfigured it scans the empty default volume,
    # and with no health source at all its tick is what keeps health moving.
    schedule.every(interval).minutes.do(poll_folder)
    schedule.every().day.at("00:05").do(_daily_fitness_recalc)

    print(f"[main] Polling {', '.join(enabled + ['file'])} every {interval}min, "
          f"fitness recalc daily at 00:05")

    # Run once immediately
    if "strava" in enabled:
        poll_strava()
    if "rwgps" in enabled:
        poll_rwgps()
    if "intervals_icu" in enabled:
        poll_intervals_icu()
    poll_folder()

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


def run_upload():
    """Serve the upload page until stopped. Behind Grafana's own logins; the
    pending fitness recalculation is flushed on Ctrl-C or SIGTERM (docker stop)
    so a just-uploaded ride is never left without it."""
    import upload_auth
    import upload_server
    grafana_url = os.environ.get("GRAFANA_URL") or "http://velomate-grafana:3000"
    root_url = os.environ.get("GRAFANA_ROOT_URL") or "http://localhost:3021/"
    port = int(os.environ.get("UPLOAD_LISTEN_PORT") or 8080)
    auth = upload_auth.GrafanaAuth(grafana_url)
    server = upload_server.make_server("0.0.0.0", port, auth=auth,
                                       grafana_root_url=root_url)

    def _term(signum, frame):
        raise SystemExit(0)

    signal.signal(signal.SIGTERM, _term)
    print(f"[upload] Listening on http://0.0.0.0:{port} "
          f"(signing in against {grafana_url})", flush=True)
    try:
        server.serve_forever()
    except (KeyboardInterrupt, SystemExit):
        pass
    finally:
        server.server_close()
        server.debouncer.flush()
        print("[upload] Stopped", flush=True)


def dispatch(argv):
    command = argv[1] if len(argv) > 1 else ""
    if command == "reclassify":
        run_reclassify()
    elif command == "upload":
        run_upload()
    else:
        run()


if __name__ == "__main__":
    dispatch(sys.argv)
