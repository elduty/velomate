"""Strava OAuth + activity fetching."""

import os
import pathlib
import tempfile
import time
from datetime import datetime, timezone, timedelta

import requests

TOKEN_URL = "https://www.strava.com/oauth/token"
API_BASE = "https://www.strava.com/api/v3"

CYCLING_STRAVA_TYPES = {"Ride", "VirtualRide", "EBikeRide", "Handcycle", "Velomobile"}


def _request_with_retry(method, url, max_retries=3, **kwargs):
    """Make an HTTP request with exponential backoff on 429."""
    kwargs.setdefault("timeout", 15)
    for attempt in range(max_retries + 1):
        resp = method(url, **kwargs)
        if resp.status_code == 429:
            wait = min(60 * (2 ** attempt), 900)  # max 15 min
            print(f"[strava] Rate limited (429), waiting {wait}s (attempt {attempt + 1})")
            time.sleep(wait)
            continue
        return resp
    return resp  # return last response even if still 429


# Module-level token cache
_access_token = None
_token_expires_at = 0
_current_refresh_token = None

# Where the rotated refresh token is written when the DB write fails. Lives on
# the velomate-token-data volume so it survives a container restart. Overridable
# via VELOMATE_TOKEN_FALLBACK so tests can exercise a real write instead of
# mocking one — a mocked write passed even while /app/data was root-owned and
# unwritable by the app user, which made the fallback silently dead.
DEFAULT_TOKEN_FALLBACK = "/app/data/.strava_refresh_token"


def _token_fallback_path():
    """Path for the on-disk refresh-token fallback. Read per call so the
    environment can change between calls (and so tests can redirect it)."""
    return pathlib.Path(os.environ.get("VELOMATE_TOKEN_FALLBACK", DEFAULT_TOKEN_FALLBACK))


def _write_secret(path, value: str):
    """Write a credential owner-readable only (0600).

    Path.write_text would create the file with the process umask, typically
    0644 — a world-readable refresh token.

    Writes to a fresh temp file and atomically renames it into place, rather
    than opening the destination and tightening it. Opening an existing file
    cannot undo its permissions for anyone who already holds a descriptor on
    it, so a token written into a previously-0644 inode is exposed to that
    holder no matter how quickly it is chmod'ed. mkstemp creates with O_EXCL
    at 0600, so the secret only ever occupies an inode no other process has
    ever had open, and os.replace swaps it in atomically — a reader either
    sees the whole old file or the whole new one, never a partial write.
    """
    path = pathlib.Path(path)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".strava-token-")
    try:
        try:
            fh = os.fdopen(fd, "w")
        except Exception:
            # fdopen only takes ownership of the fd once it succeeds, so this
            # is the one case where the raw fd still has to be closed by hand.
            os.close(fd)
            raise
        with fh:
            fh.write(value)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def refresh_access_token(client_id: str, client_secret: str, refresh_token: str) -> str:
    """POST to Strava token endpoint, return fresh access_token.
    Also stores the new refresh_token from the response (Strava rotates it).
    """
    global _access_token, _token_expires_at, _current_refresh_token

    if _access_token and time.time() < _token_expires_at - 60:
        return _access_token

    resp = _request_with_retry(requests.post, TOKEN_URL, data={
        "client_id": client_id,
        "client_secret": client_secret,
        "refresh_token": refresh_token,
        "grant_type": "refresh_token",
    })
    resp.raise_for_status()
    data = resp.json()
    _access_token = data["access_token"]
    _token_expires_at = data["expires_at"]

    # Strava rotates refresh tokens — persist the new one
    new_refresh = data.get("refresh_token")
    if new_refresh and new_refresh != refresh_token:
        try:
            from db import get_connection, set_sync_state
            conn = get_connection()
            try:
                set_sync_state(conn, "strava_refresh_token", new_refresh)
                _current_refresh_token = new_refresh  # set AFTER successful DB write
                print(f"[strava] Refresh token rotated and persisted")
            finally:
                conn.close()
        except Exception as e:
            # DB write failed — update in-memory token so the current process doesn't
            # reuse the old (now-invalid) token. Token will be lost on restart.
            _current_refresh_token = new_refresh
            print(f"[strava] WARNING: Could not persist new refresh token: {e}")
            # Fallback: write to file so token survives restart
            path = _token_fallback_path()
            try:
                _write_secret(path, new_refresh)
                print(f"[strava] Fallback: token written to {path}")
            except Exception as file_err:
                # Never swallow this. Both persistence paths have now failed, so
                # the rotated token exists only in memory and is lost on restart —
                # Strava auth then breaks until the OAuth flow is re-run.
                print(
                    f"[strava] ERROR: Could not write token fallback to {path}: {file_err}. "
                    "The rotated refresh token is in memory only and will be lost on restart — "
                    "check that the directory exists and is writable by the container user."
                )

    return _access_token


def _get_token() -> str:
    """Get a valid access token, using persisted refresh token if available."""
    global _current_refresh_token

    # Check file fallback before env var
    if not _current_refresh_token:
        try:
            token_path = _token_fallback_path()
            if token_path.exists():
                _current_refresh_token = token_path.read_text().strip()
                print("[strava] Loaded refresh token from file fallback")
        except Exception as e:
            print(f"[strava] Could not read token fallback: {e}")

    # Prefer DB-stored refresh token over env var (Strava rotates them)
    refresh_token = _current_refresh_token or os.environ["STRAVA_REFRESH_TOKEN"]
    if not _current_refresh_token:
        try:
            from db import get_connection, get_sync_state
            conn = get_connection()
            try:
                stored = get_sync_state(conn, "strava_refresh_token")
                if stored:
                    refresh_token = stored
                    _current_refresh_token = stored
            finally:
                conn.close()
        except Exception as e:
            print(f"[strava] Could not load stored refresh token: {e}")

    return refresh_access_token(
        os.environ["STRAVA_CLIENT_ID"],
        os.environ["STRAVA_CLIENT_SECRET"],
        refresh_token,
    )


def fetch_recent_activities(access_token: str, after_epoch: int) -> list:
    """GET /athlete/activities?after=<epoch>&per_page=50. Handle pagination."""
    headers = {"Authorization": f"Bearer {access_token}"}
    all_activities = []
    page = 1

    while True:
        resp = _request_with_retry(
            requests.get,
            f"{API_BASE}/athlete/activities",
            headers=headers,
            params={"after": after_epoch, "per_page": 50, "page": page},
        )
        resp.raise_for_status()
        batch = resp.json()
        if not batch:
            break
        all_activities.extend(batch)
        if len(batch) < 50:
            break
        page += 1
        time.sleep(1)  # rate limit courtesy

    return all_activities


def parse_segment_climbs(detail: dict) -> list[dict]:
    """Extract uphill segment efforts from a Strava activity detail response.

    Returns a list of climb dicts compatible with ride_climbs schema,
    filtered to uphill segments (average_grade > 0).
    """
    from climbs import classify_climb

    efforts = detail.get("segment_efforts", [])
    climbs = []
    for e in efforts:
        seg = e.get("segment", {})
        grade = seg.get("average_grade", 0)
        if grade <= 0:
            continue

        dist = seg.get("distance", 0)
        elev_high = seg.get("elevation_high", 0)
        elev_low = seg.get("elevation_low", 0)
        gain = elev_high - elev_low
        name = seg.get("name", "")
        elapsed = e.get("elapsed_time", 0)

        if dist <= 0 or gain <= 0:
            continue

        category = classify_climb(dist, grade)
        score = round(dist * grade)

        climbs.append({
            "gain_m": round(gain),
            "length_m": round(dist),
            "avg_grade": round(grade, 1),
            "start_alt": round(elev_low),
            "peak_alt": round(elev_high),
            "duration_s": elapsed,
            "category": category,
            "score": score,
            "source": "strava",
            "segment_name": name,
            # Strava doesn't give us stream offsets for segments,
            # so start_offset/end_offset will be NULL
            "start_offset": None,
            "end_offset": None,
        })

    return climbs


def _store_strava_climbs(conn, activity_id: int, climbs: list[dict]) -> int:
    """Store Strava segment climbs in ride_climbs.

    Skips segments that are already stored (idempotent on re-sync).
    """
    stored = 0
    with conn.cursor() as cur:
        for c in climbs:
            # Check if this segment is already stored (by name + activity)
            cur.execute("""
                SELECT 1 FROM ride_climbs
                WHERE activity_id = %s AND segment_name = %s AND source = 'strava'
            """, (activity_id, c["segment_name"]))
            if cur.fetchone():
                continue

            cur.execute("""
                INSERT INTO ride_climbs
                    (activity_id, start_offset, end_offset, gain_m, length_m,
                     avg_grade, start_alt, peak_alt, duration_s, category,
                     score, source, segment_name)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """, (
                activity_id, c["start_offset"], c["end_offset"], c["gain_m"],
                c["length_m"], c["avg_grade"], c["start_alt"], c["peak_alt"],
                c["duration_s"], c["category"], c["score"], "strava",
                c["segment_name"],
            ))
            stored += 1

    if stored:
        print(f"  [segments] Stored {stored} Strava climb segments")
    return stored


def _store_strava_segment_sentinel(conn, activity_id: int) -> None:
    """Mark a ride as having no Strava segments to fetch.

    Inserts a source='strava' sentinel row (category='none', gain_m=0) so the
    NOT EXISTS selector in backfill_strava_segments stops re-fetching this ride
    from the Strava API on every recalculation. Rides that legitimately have no
    uphill segments (indoor/Zwift, flat routes) or 404 on detail otherwise never
    gain a source='strava' row and are re-fetched forever — burning API quota and
    stretching each recalc. category='none' keeps the sentinel out of dashboards
    (their climbs query filters `category != 'none'`); NULL start_alt/peak_alt are
    ignored by the overlap check in fitness.detect_climbs_for_rides. Mirrors the
    detected-climbs 'none' sentinel there.
    """
    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO ride_climbs (activity_id, gain_m, category, source)
            VALUES (%s, 0, 'none', 'strava')
        """, (activity_id,))


def backfill_strava_segments(conn) -> int:
    """Re-fetch Strava activity details for rides missing Strava segment data.

    Finds rides that have a strava_id but no source='strava' rows in
    ride_climbs, fetches their detail from the Strava API, and stores
    any uphill segment efforts. Rides with no storable segments get a
    source='strava' sentinel so they are not re-fetched next cycle.

    Rate-limited: 1 second between API calls.
    """
    import time as _time

    # Find rides with Strava IDs that don't have Strava segments yet
    with conn.cursor() as cur:
        cur.execute("""
            SELECT a.id, a.strava_id FROM activities a
            WHERE a.strava_id IS NOT NULL
              AND NOT EXISTS (
                  SELECT 1 FROM ride_climbs rc
                  WHERE rc.activity_id = a.id AND rc.source = 'strava'
              )
            ORDER BY a.id
        """)
        rides = cur.fetchall()

    if not rides:
        return 0

    token = _get_token()
    count = 0
    for act_id, strava_id in rides:
        try:
            _time.sleep(1.0)  # rate limit
            detail = fetch_activity_detail(token, strava_id)
            if not detail:
                # detail is falsy ONLY on a genuine 404 (deleted on Strava):
                # fetch_activity_detail returns {} for 404 but RAISES (via
                # raise_for_status) on 429/5xx/network errors. Those raises are
                # caught by the except below and retried next cycle — they never
                # reach here — so sentineling a 404 can't mis-fire on a transient
                # rate-limit. (Regression-tested: test_transient_error_does_not_sentinel.)
                _store_strava_segment_sentinel(conn, act_id)
                continue
            strava_climbs = parse_segment_climbs(detail)
            stored = _store_strava_climbs(conn, act_id, strava_climbs) if strava_climbs else 0
            if stored:
                count += 1
                print(f"[segments] Activity {act_id} (strava {strava_id}): {stored} segments")
            else:
                # No uphill segments to store — sentinel this ride so the
                # NOT EXISTS selector skips it on every later recalculation.
                _store_strava_segment_sentinel(conn, act_id)
        except Exception as e:
            print(f"[segments] Failed for activity {act_id}: {e}")
            continue

    return count


def fetch_activity_detail(access_token: str, activity_id: int) -> dict:
    """GET /activities/{id} — returns full detail including calories, HR, suffer_score."""
    headers = {"Authorization": f"Bearer {access_token}"}
    resp = _request_with_retry(
        requests.get,
        f"{API_BASE}/activities/{activity_id}",
        headers=headers,
    )
    if resp.status_code == 404:
        return {}
    resp.raise_for_status()
    return resp.json()


def fetch_activity_streams(access_token: str, activity_id: int) -> dict:
    """GET /activities/{id}/streams for HR, power, cadence, speed, altitude, latlng."""
    headers = {"Authorization": f"Bearer {access_token}"}
    keys = "time,heartrate,watts,cadence,velocity_smooth,altitude,latlng"

    resp = _request_with_retry(
        requests.get,
        f"{API_BASE}/activities/{activity_id}/streams",
        headers=headers,
        params={"keys": keys, "key_type": "time"},
        timeout=30,
    )
    if resp.status_code == 404:
        return {}
    resp.raise_for_status()

    # Convert Strava stream format to dict of lists
    streams = {}
    for stream in resp.json():
        streams[stream["type"]] = stream["data"]
    return streams


def _detect_device(raw: dict) -> str:
    """Detect recording device from Strava activity metadata."""
    device_name = (raw.get("device_name") or "").lower()
    if "karoo" in device_name:
        return "karoo"
    elif "watch" in device_name or "apple" in device_name:
        return "watch"
    elif raw.get("trainer", False) or "zwift" in raw.get("name", "").lower():
        return "zwift"
    return "unknown"


def _parse_activity(raw: dict) -> dict:
    """Convert Strava API activity (summary or detail) to our DB format."""
    device = _detect_device(raw)
    return {
        "strava_id": raw["id"],
        "name": raw.get("name", ""),
        "date": raw.get("start_date"),
        "distance_m": raw.get("distance", 0),
        "duration_s": raw.get("moving_time", 0),
        "elevation_m": raw.get("total_elevation_gain", 0),
        "avg_hr": raw.get("average_heartrate"),
        "max_hr": raw.get("max_heartrate"),
        "avg_power": raw.get("average_watts"),
        "max_power": raw.get("max_watts"),
        "avg_cadence": raw.get("average_cadence"),
        "avg_speed_kmh": round((raw.get("average_speed") or 0) * 3.6, 2),
        "calories": raw.get("calories"),
        "suffer_score": raw.get("suffer_score"),
        "device": device,
        "strava_type": raw.get("type", ""),
        "trainer": raw.get("trainer", False),
    }


def _merge_detail(summary: dict, detail: dict) -> dict:
    """Enrich summary data with detail fields.
    Karoo calories always win. For other devices, fill gaps only.
    """
    if not detail:
        return summary
    merged = dict(summary)
    device = summary.get("device", "unknown")

    # Calories: Karoo data is from FIT file (accurate) — always prefer it.
    # For other devices, use detail calories only if summary had none.
    detail_calories = detail.get("calories")
    if detail_calories:
        if device == "karoo" or not merged.get("calories"):
            merged["calories"] = detail_calories

    # Fill other gaps from detail
    for field in ("average_heartrate", "max_heartrate", "suffer_score"):
        detail_val = detail.get(field)
        db_field = {
            "average_heartrate": "avg_hr",
            "max_heartrate": "max_hr",
            "suffer_score": "suffer_score",
        }[field]
        if detail_val and not merged.get(db_field):
            merged[db_field] = detail_val

    return merged


def _parse_streams(raw_streams: dict) -> list:
    """Convert Strava stream format to list of point dicts."""
    if not raw_streams or "time" not in raw_streams:
        return []

    points = []
    time_arr = raw_streams["time"]
    length = len(time_arr)
    hr_arr = raw_streams.get("heartrate", [])
    power_arr = raw_streams.get("watts", [])
    cadence_arr = raw_streams.get("cadence", [])
    speed_arr = raw_streams.get("velocity_smooth", [])
    alt_arr = raw_streams.get("altitude", [])
    latlngs = raw_streams.get("latlng", [])

    for i in range(length):
        speed_val = speed_arr[i] if i < len(speed_arr) else None
        point = {
            "time_offset": time_arr[i],
            "hr": hr_arr[i] if i < len(hr_arr) else None,
            "power": power_arr[i] if i < len(power_arr) else None,
            "cadence": cadence_arr[i] if i < len(cadence_arr) else None,
            "speed_kmh": round(speed_val * 3.6, 2) if speed_val is not None else None,
            "altitude_m": alt_arr[i] if i < len(alt_arr) else None,
            "lat": latlngs[i][0] if i < len(latlngs) and latlngs[i] else None,
            "lng": latlngs[i][1] if i < len(latlngs) and latlngs[i] else None,
        }
        points.append(point)
    return points


def sync_activities(conn, after_epoch: int = None):
    """Fetch recent activities from Strava, store with streams.

    Activities are processed oldest-first and the cursor
    (strava_last_activity_epoch) is checkpointed after each one completes, so a
    transient failure mid-pass keeps all progress up to the last finished
    activity instead of discarding the whole pass and restarting from
    after_epoch. This lets a large/full-history backfill make durable forward
    progress and converge within Strava's rate limits rather than re-fetching
    everything every cycle until a (statistically unlikely) zero-failure pass.
    Ascending order is imposed here — not assumed from the API response — so the
    checkpoint is always safe. Mirrors the RWGPS sync checkpoint.

    Per-activity failures are classified rather than allowed to abort the pass:
    a deterministic data error (KeyError/TypeError from a malformed payload) is
    skipped and checkpointed past, because it would fail identically on every
    retry and would otherwise block every newer activity permanently; anything
    else is treated as transient and stops the pass with the cursor left at the
    last fully-completed activity, so the next cycle retries it. Same contract
    as the RWGPS sync loop.
    """
    from db import upsert_activity, upsert_streams, get_sync_state, set_sync_state

    token = _get_token()

    if after_epoch is None:
        last = get_sync_state(conn, "strava_last_activity_epoch")
        after_epoch = int(last) if last else 0

    activities = fetch_recent_activities(token, after_epoch)
    print(f"[strava] Fetched {len(activities)} activities since epoch {after_epoch}")

    def _epoch(raw):
        """Parse a Strava start_date to a UTC epoch int, or None if unparseable."""
        try:
            return int(datetime.fromisoformat(
                (raw.get("start_date") or "").replace("Z", "+00:00")).timestamp())
        except (ValueError, TypeError, AttributeError):
            return None

    # Oldest-first so checkpointing after each activity is safe (all earlier
    # epochs are already done). Undated activities (no parseable start_date —
    # anomalous for Strava) sort last and never advance the cursor.
    ordered = sorted(
        ((_epoch(raw), raw) for raw in activities),
        key=lambda t: (t[0] is None, t[0] or 0),
    )

    ingested = 0
    latest_epoch = after_epoch

    def _checkpoint(epoch):
        """Advance the persisted cursor to epoch (monotonic; skips None).

        Durability relies on conn.autocommit=True (set in db.get_connection):
        set_sync_state and the preceding upsert_activity/upsert_streams commit
        immediately, so a later exception in the same pass cannot roll back an
        activity that was already checkpointed. (Same mechanism the RWGPS sync
        checkpoint depends on.)
        """
        nonlocal latest_epoch
        if epoch is not None and epoch > latest_epoch:
            latest_epoch = epoch
            set_sync_state(conn, "strava_last_activity_epoch", str(latest_epoch))

    for epoch, raw in ordered:
        # Skip non-cycling activities, but advance the cursor past them so they
        # aren't re-fetched.
        strava_type = raw.get("type", "")
        if strava_type and strava_type not in CYCLING_STRAVA_TYPES:
            print(f"  ⏭ Skipping {raw.get('name', '?')} ({strava_type})")
            _checkpoint(epoch)
            continue

        try:
            data = _parse_activity(raw)

            # Fetch detailed activity for calories, HR, suffer_score
            time.sleep(1.0)
            detail = fetch_activity_detail(token, raw["id"])
            data = _merge_detail(data, detail)

            activity_id, streams_preserved = upsert_activity(conn, data)

            # Store Strava segment efforts as climb data
            try:
                strava_climbs = parse_segment_climbs(detail)
                if strava_climbs:
                    _store_strava_climbs(conn, activity_id, strava_climbs)
            except Exception as e:
                print(f"  [segments] {e}")

            # Fetch streams with rate limiting
            time.sleep(1.5)
            raw_streams = fetch_activity_streams(token, raw["id"])
            streams = _parse_streams(raw_streams)
            if streams and not streams_preserved:
                upsert_streams(conn, activity_id, streams)
        except (KeyError, TypeError) as e:
            # Deterministic per-activity data error — malformed payload, missing
            # field, wrong type. It will fail identically on every retry, so
            # skip it and checkpoint PAST it; otherwise this one ride blocks
            # every newer ride forever. Mirrors the RWGPS sync classification.
            print(f"  [strava] Skipping activity {raw.get('id')} "
                  f"({raw.get('name', '?')}) — unprocessable ({type(e).__name__}: {e})")
            _checkpoint(epoch)
            continue
        except Exception as e:
            # Transient — network, 5xx after retries, DB blip. The ride is fine
            # and retryable, so stop the pass WITHOUT advancing the cursor. The
            # next cycle resumes from the last fully-completed activity.
            # (ValueError lands here on purpose: a truncated-response
            # JSONDecodeError is a ValueError subclass and is retryable.)
            print(f"  [strava] Transient failure on activity {raw.get('id')} "
                  f"({raw.get('name', '?')}): {e} — stopping pass, retrying next cycle")
            break

        ingested += 1
        print(f"  → {data['name']} ({(data.get('date') or '')[:10]}) — {(data.get('distance_m') or 0)/1000:.1f}km")

        # Activity fully stored — checkpoint so this progress survives a later
        # failure in the same pass.
        _checkpoint(epoch)

    return ingested


def backfill(conn, months: int = 12):
    """Fetch activities from Strava and store with streams.

    months > 0: only rides newer than N months ago (fast, bounded).
    months == 0: full history back to the athlete's first activity
                 (slow — may take hours and span multiple days because
                 of Strava's 200 req / 15 min and 2000 req / day limits).
    """
    if months == 0:
        print("[strava] Backfilling FULL history — this may take hours and hit Strava rate limits")
        return sync_activities(conn, after_epoch=0)
    cutoff = datetime.now(timezone.utc) - timedelta(days=months * 30)
    after_epoch = int(cutoff.timestamp())
    print(f"[strava] Backfilling {months} months (since {cutoff.date()})")
    return sync_activities(conn, after_epoch)


def reclassify_activities(conn):
    """Re-fetch Strava type for all activities. Reclassify cycling, delete non-cycling."""
    from db import classify_activity

    token = _get_token()

    with conn.cursor() as cur:
        cur.execute("SELECT id, strava_id FROM activities WHERE strava_id IS NOT NULL ORDER BY date")
        db_activities = cur.fetchall()

    print(f"[reclassify] {len(db_activities)} activities to check")

    updated = 0
    deleted = 0
    skipped = 0
    conn.autocommit = False
    try:
        for db_id, strava_id in db_activities:
            time.sleep(0.5)
            try:
                resp = _request_with_retry(
                    requests.get,
                    f"{API_BASE}/activities/{strava_id}",
                    headers={"Authorization": f"Bearer {token}"},
                )
                if resp.status_code == 404:
                    print(f"  [reclassify] Activity {strava_id} not found on Strava, skipping")
                    skipped += 1
                    continue
                resp.raise_for_status()
                raw = resp.json()
            except Exception as e:
                print(f"  [reclassify] Error fetching {strava_id}: {e}")
                skipped += 1
                continue

            strava_type = raw.get("type", "")
            name = raw.get("name", "")

            # Delete non-cycling activities
            if strava_type and strava_type not in CYCLING_STRAVA_TYPES:
                with conn.cursor() as cur:
                    cur.execute("DELETE FROM activities WHERE id = %s", (db_id,))
                deleted += 1
                print(f"  ✕ {name[:40]:40s} ({strava_type}) — deleted")
                continue

            # Reclassify cycling activities
            trainer = raw.get("trainer", False)
            with conn.cursor() as cur:
                cur.execute("SELECT name, distance_m, device FROM activities WHERE id = %s", (db_id,))
                row = cur.fetchone()
                if not row:
                    continue

            classified = classify_activity({
                "strava_type": strava_type, "trainer": trainer,
                "name": row[0], "distance_m": row[1], "device": row[2],
            })

            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE activities SET sport_type = %s, is_indoor = %s WHERE id = %s",
                    (classified["sport_type"], classified["is_indoor"], db_id),
                )
            updated += 1
            print(f"  → {row[0][:40]:40s} {strava_type:20s} → {classified['sport_type']}")

        conn.commit()
        print(f"[reclassify] Done: {deleted} deleted, {updated} updated, {skipped} skipped")
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.autocommit = True
