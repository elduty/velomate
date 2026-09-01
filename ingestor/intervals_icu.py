"""intervals.icu API client (read-only).

Auth is HTTP Basic with the literal username "API_KEY" and the athlete's API
key from intervals.icu -> Settings -> Developer Settings. See the design spec
at docs/design/specs/2026-08-05-intervals-icu-source-design.md.

This module is deliberately read-only. It never writes to intervals.icu, and
none of the icu_* computed fields it returns are ever stored as VeloMate
metrics — the ingestor remains the single source of truth.

Not to be confused with ingestor/intervals.py, which is the auto interval
detector that runs over power streams.
"""

import os
import time
from datetime import datetime, timedelta, timezone

import requests

API_BASE = "https://intervals.icu/api/v1"

# intervals.icu allows 10 calls/sec per IP. Stay comfortably under it.
_MIN_REQUEST_INTERVAL_S = 0.12

# Warn when the rolling 15-minute budget (2500 for API-key callers) gets low.
_LOW_BUDGET_RESERVE = 100

_last_request_at = 0.0


def _auth() -> tuple[str, str]:
    """HTTP Basic credentials. The username is the literal string API_KEY."""
    return ("API_KEY", os.environ["INTERVALS_ICU_API_KEY"])


def _athlete_id() -> str:
    return os.environ["INTERVALS_ICU_ATHLETE_ID"]


def _pace():
    """Sleep just enough to stay under the 10 requests/sec per-IP limit."""
    global _last_request_at
    elapsed = time.monotonic() - _last_request_at
    if elapsed < _MIN_REQUEST_INTERVAL_S:
        time.sleep(_MIN_REQUEST_INTERVAL_S - elapsed)
    _last_request_at = time.monotonic()


def _check_budget(resp):
    """Log when the rolling 15-minute request budget runs low.

    The header carries two comma-separated values: the 15-minute remaining
    count first, the daily remaining count second.
    """
    raw = resp.headers.get("X-RateLimit-Remaining", "")
    if not raw:
        return
    try:
        window_remaining = int(raw.split(",")[0])
    except (ValueError, IndexError):
        return
    if window_remaining < _LOW_BUDGET_RESERVE:
        print(f"[intervals_icu] rate budget low: {window_remaining} left in the 15-min window")


def _retry_after_seconds(resp, attempt: int) -> int:
    """Retry-After when the server sends a usable one, else exponential backoff."""
    raw = resp.headers.get("Retry-After", "")
    try:
        return min(int(raw), 900)
    except (TypeError, ValueError):
        return min(60 * (2 ** attempt), 900)


def _request_with_retry(method, url, max_retries=3, **kwargs):
    """HTTP request with per-IP pacing and Retry-After-aware 429 handling."""
    kwargs.setdefault("timeout", 30)
    kwargs.setdefault("auth", _auth())
    resp = None
    for attempt in range(max_retries + 1):
        _pace()
        resp = method(url, **kwargs)
        _check_budget(resp)
        # Only sleep+retry if there's an attempt left — sleeping on the final
        # 429 just blocks the caller before returning the 429 anyway.
        if resp.status_code == 429 and attempt < max_retries:
            wait = _retry_after_seconds(resp, attempt)
            print(f"[intervals_icu] Rate limited (429), waiting {wait}s (attempt {attempt + 1})")
            time.sleep(wait)
            continue
        return resp
    return resp


def _get(path: str, params: dict = None, missing_ok: bool = False):
    """GET an API path. Returns None for 404 when missing_ok is set."""
    resp = _request_with_retry(requests.get, f"{API_BASE}{path}", params=params or {})
    if resp.status_code in (401, 403):
        raise RuntimeError(
            "[intervals_icu] auth failed — check INTERVALS_ICU_ATHLETE_ID and regenerate "
            "the API key at intervals.icu -> Settings -> Developer Settings"
        )
    if missing_ok and resp.status_code == 404:
        return None
    resp.raise_for_status()
    return resp


def list_activities(oldest: str, newest: str = None, fields: list = None,
                    limit: int = None) -> list:
    """GET /athlete/{id}/activities — activities in a date range, newest first.

    `oldest` is required by the API and is a local ISO-8601 date or datetime.
    `fields` projects the response down to named fields, which matters because
    the full Activity object carries 183 of them.
    """
    params = {"oldest": oldest}
    if newest:
        params["newest"] = newest
    if fields:
        params["fields"] = ",".join(fields)
    if limit:
        params["limit"] = limit
    return _get(f"/athlete/{_athlete_id()}/activities", params).json()


def get_activity(activity_id: str, intervals: bool = False) -> dict:
    """GET /activity/{id}. Returns {} if the activity is gone."""
    params = {"intervals": "true"} if intervals else {}
    resp = _get(f"/activity/{activity_id}", params, missing_ok=True)
    return resp.json() if resp is not None else {}


def get_streams(activity_id: str, types: list = None,
                include_defaults: bool = True) -> list:
    """GET /activity/{id}/streams.json — per-second data for one activity.

    The extension is mandatory and must be at least one character. Only ".csv"
    switches to CSV; ".json" (or anything else) returns JSON.

    Returns [] if the activity is gone.
    """
    params = {"includeDefaults": "true" if include_defaults else "false"}
    if types:
        params["types"] = ",".join(types)
    resp = _get(f"/activity/{activity_id}/streams.json", params, missing_ok=True)
    return resp.json() if resp is not None else []


def get_wellness(oldest: str, newest: str = None) -> list:
    """GET /athlete/{id}/wellness.json — daily wellness records.

    Read-only reconnaissance for the comparison pass. Wellness ingestion is a
    separate backlog feature; nothing here is stored.
    """
    params = {"oldest": oldest}
    if newest:
        params["newest"] = newest
    return _get(f"/athlete/{_athlete_id()}/wellness.json", params).json()


def _is_unavailable_stub(activity: dict) -> bool:
    """True when intervals.icu cannot serve this activity's data.

    Strava's API terms forbid third parties from re-serving Strava data, so
    intervals.icu returns a 5-field placeholder for Strava-sourced activities
    instead of the usual ~115. Treating one as a normal activity would create a
    dateless, metric-less row or overwrite a good one. Permanent behaviour, not
    a transient state.
    """
    return bool(activity.get("_note")) or activity.get("source") == "STRAVA"


def _detect_device(activity: dict) -> str:
    """Same heuristics as strava._detect_device, on intervals.icu field names."""
    name = (activity.get("device_name") or "").lower()
    if "karoo" in name:
        return "karoo"
    if "watch" in name or "apple" in name:
        return "watch"
    if activity.get("trainer") or "zwift" in (activity.get("name") or "").lower():
        return "zwift"
    return "unknown"


def _parse_activity(activity: dict) -> dict:
    """Convert an intervals.icu activity to VeloMate's DB format.

    Deliberately maps only raw ride facts. Provider-computed metrics
    (polarization_index, coasting_time, icu_training_load, icu_intensity ...)
    are NOT imported: the ingestor computes those itself so every ride carries
    them whichever source delivered it: an imported field would be populated
    for one source and NULL for the others, so the same stat would take a
    different value depending on who delivered the ride.

    max_power has no field on their side (`max_watts` is absent), so it is left
    None here and derived from the watts stream by the caller. suffer_score
    stays None — only Strava supplies it, and the upsert must not null out a
    Strava-provided value on a dual-source row.
    """
    distance_m = activity.get("distance") or 0
    duration_s = activity.get("moving_time") or 0
    avg_speed_kmh = round(distance_m / duration_s * 3.6, 2) if duration_s else 0.0
    return {
        "intervals_icu_id": activity["id"],
        "name": activity.get("name", ""),
        "date": activity.get("start_date") or activity.get("start_date_local"),
        "distance_m": distance_m,
        "duration_s": duration_s,
        "elevation_m": activity.get("total_elevation_gain", 0),
        "avg_hr": activity.get("average_heartrate"),
        "max_hr": activity.get("max_heartrate"),
        "avg_power": activity.get("icu_average_watts"),
        "max_power": None,
        "avg_cadence": activity.get("average_cadence"),
        "avg_speed_kmh": avg_speed_kmh,
        "calories": activity.get("calories"),
        "suffer_score": None,
        "device": _detect_device(activity),
        "strava_type": activity.get("type", "Ride"),
        "trainer": bool(activity.get("trainer")),
        # Gates stream re-fetch: streams are only re-downloaded when this advances.
        "intervals_icu_analyzed": activity.get("analyzed"),
    }


# intervals.icu stream type -> (VeloMate key, transform)
_STREAM_MAP = {
    "watts": ("power", None),
    "heartrate": ("hr", None),
    "cadence": ("cadence", None),
    "altitude": ("altitude_m", None),
    "velocity_smooth": ("speed_kmh", lambda v: round(v * 3.6, 2)),
}


def _parse_streams(streams: list) -> list:
    """Convert intervals.icu parallel stream arrays to VeloMate point dicts.

    time_offset comes from the `time` channel, NOT the list index: real streams
    contain recording gaps (an observed ride jumps 1 -> 19), so the index is not
    the elapsed second. Channels shorter than `time` are padded with None.
    """
    channels = {s.get("type"): s for s in streams or []}
    by_type = {t: (c.get("data") or []) for t, c in channels.items()}
    times = by_type.get("time")
    if not times:
        return []

    # latlng is NOT a list of [lat, lng] pairs the way Strava sends it.
    # intervals.icu splits it across two parallel flat float arrays: `data`
    # holds latitudes and `data2` holds longitudes, either of which can be None
    # for a sample with no GPS fix. Indexing it as a pair raises
    # "'float' object is not subscriptable" on every real ride.
    latlng = channels.get("latlng") or {}
    lats = latlng.get("data") or []
    lngs = latlng.get("data2") or []

    points = []
    for i, offset in enumerate(times):
        point = {"time_offset": offset, "power": None, "hr": None, "cadence": None,
                 "speed_kmh": None, "altitude_m": None, "lat": None, "lng": None}
        for icu_type, (key, transform) in _STREAM_MAP.items():
            data = by_type.get(icu_type, [])
            if i < len(data) and data[i] is not None:
                point[key] = transform(data[i]) if transform else data[i]
        if i < len(lats) and i < len(lngs) and lats[i] is not None and lngs[i] is not None:
            point["lat"], point["lng"] = lats[i], lngs[i]
        points.append(point)
    return points


# Below this remote-to-local ratio the sweep refuses to delete anything. A
# degraded-but-successful response should never be able to empty the library.
MIN_REMOTE_RATIO = 0.5

# Explicit ceiling for the reconciliation fetch, so the sweep never inherits a
# server-side default page size.
RECONCILE_FETCH_LIMIT = 5000

# Hard ceiling on deletions per sweep. The ratio guard above only blocks a
# remote set below half of local, so a degraded-but-successful 200 returning
# 50-99% of the library still passes it and the missing remainder gets deleted
# — permanently, since rides outside the 14-day sync window never come back.
# A real library loses a handful of rides at a time, never a batch, so a low
# cap costs nothing legitimate and bounds the damage from a bad response.
MAX_DELETIONS_PER_SWEEP = 5

# sync_state key holding the missing-id set that exceeded the cap on the previous
# sweep, so a genuine bulk deletion can be confirmed across two sweeps rather
# than stranded indefinitely.
PENDING_DELETIONS_KEY = "intervals_icu_pending_deletions"


def _window_start(days: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%dT00:00:00")


def _parse_ts(value):
    """Parse an ISO-8601 timestamp to an aware UTC datetime, or None.

    Exists so the `analyzed` gate compares instants rather than strings. The
    API sends milliseconds; a TIMESTAMPTZ read back through psycopg2 renders
    microseconds and a session-dependent offset, so the two spellings of the
    same instant never match as text.
    """
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def sync_activities(conn, window_days: int = 14) -> tuple:
    """Re-query the last `window_days` and upsert. Returns (ingested, skipped_stubs).

    `ingested` counts only activities that were new or had been re-analysed —
    NOT everything in the window. The window necessarily contains the same
    recent rides every poll, so counting them all would make the caller
    recalculate fitness on every cycle for no reason.

    There is no cursor to withhold: the window is re-queried unconditionally,
    so a transient per-activity failure simply retries next poll and can never
    block newer rides the way a cursor-based loop can.
    """
    from db import (upsert_activity, upsert_streams,
                    get_intervals_icu_analyzed, ACTIVITY_ABSENT)

    activities = list_activities(oldest=_window_start(window_days))
    ingested = 0   # new or re-analysed — the recalculation trigger
    seen = 0       # upserted, changed or not — observability only
    skipped = 0

    for activity in activities:
        if _is_unavailable_stub(activity):
            skipped += 1
            continue
        try:
            data = _parse_activity(activity)

            # Re-fetch streams only when the ride has been (re-)analysed since
            # we last stored it. Skipping this would re-download every stream in
            # the window on every poll, which the rate budget will not absorb.
            #
            # Compared as datetimes, NOT strings: the API sends millisecond
            # precision ("...824+00:00") while the value read back from a
            # TIMESTAMPTZ column renders microseconds ("...824000+00:00"). A
            # string compare is never equal after the first store, so the gate
            # would always fire and defeat its own purpose.
            raw_stored = get_intervals_icu_analyzed(conn, activity["id"])
            incoming = _parse_ts(data["intervals_icu_analyzed"])
            if raw_stored is ACTIVITY_ABSENT:
                changed = True          # never stored — must ingest
            else:
                # A stored NULL compares equal to an incoming None, so a ride
                # intervals.icu never analyses is stored once and then left
                # alone instead of re-ingesting on every poll.
                changed = _parse_ts(raw_stored) != incoming

            streams = []
            if changed:
                streams = _parse_streams(get_streams(activity["id"]))
                watts = [p["power"] for p in streams if p["power"] is not None]
                if watts:
                    data["max_power"] = max(watts)

            # max_power stays None on the unchanged path, which is safe:
            # _do_insert COALESCEs every sensor column, so a NULL never
            # overwrites a stored value.
            activity_id, streams_preserved = upsert_activity(conn, data)
            if streams and not streams_preserved:
                upsert_streams(conn, activity_id, streams)

            seen += 1
            if changed:
                ingested += 1
                print(f"  → {data['name']} ({(data.get('date') or '')[:10]}) "
                      f"— {(data.get('distance_m') or 0)/1000:.1f}km")
        except Exception as e:
            # No cursor to protect, so a failure is logged and the batch
            # continues; the next poll re-queries this activity anyway.
            print(f"  [intervals_icu] Skipping {activity.get('id')} "
                  f"({type(e).__name__}: {e})")
        time.sleep(1.0)

    if skipped:
        print(f"[intervals_icu] {skipped} Strava-relayed activities unavailable via the API "
              f"(expected — they cannot be re-served)")
    print(f"[intervals_icu] {seen} activities in window, {ingested} new or re-analysed")
    return ingested, skipped


def reconcile(conn, sweep_days: int = 90) -> tuple:
    """Bidirectional sweep. Returns (deleted, recovered).

    A date-window API cannot signal deletion — a removed ride simply stops
    appearing — so the sweep compares ID sets over a wider window than the
    poll. Stub IDs are removed from BOTH sets: a local ride whose remote
    counterpart is an unavailable Strava stub is present remotely, merely
    unreadable, and must NOT be deleted.

    The deletion pass is gated on the remote set being plausible — a
    degraded-but-successful response must never be able to empty the library.
    """
    from db import handle_intervals_icu_deletion

    # `source` is requested explicitly rather than relying on stubs carrying it
    # under a field mask. (Verified against the live API that they do — a stub
    # returns _note/source even when masked — but the filter is load-bearing for
    # correctness, so it should not depend on an undocumented quirk.)
    # Explicit high limit so the destructive set-difference below can never
    # depend on a server-side default page size. Verified today that a two-year
    # window returns the complete library with or without it, but a truncated
    # result would delete real rides, so the guarantee is worth stating.
    remote = list_activities(oldest=_window_start(sweep_days),
                             fields=["id", "start_date_local", "source"],
                             limit=RECONCILE_FETCH_LIMIT)
    stub_ids = {a["id"] for a in remote if _is_unavailable_stub(a)}
    remote_ids = {a["id"] for a in remote if not _is_unavailable_stub(a)}

    # The local window is deliberately ONE DAY NARROWER than the remote one.
    # We store `date` as the activity's UTC start, while the API interprets
    # `oldest` against start_date_local, so the two windows do not coincide at
    # the boundary. Without the margin a ride whose UTC date is inside the local
    # window but whose local date falls just outside the remote one lands in
    # local_ids - remote_ids and gets deleted despite still existing remotely —
    # and, being outside the 14-day sync window, never comes back. A day of
    # margin absorbs any real UTC offset; the plausibility guards below would
    # not catch a single boundary ride.
    with conn.cursor() as cur:
        cur.execute("SELECT intervals_icu_id FROM activities "
                    "WHERE intervals_icu_id IS NOT NULL AND date >= %s::date",
                    (_window_start(max(sweep_days - 1, 1))[:10],))
        local_ids = {r[0] for r in cur.fetchall()}

    # Stubs are excluded from BOTH sides, not just the remote set. A stub is
    # present remotely but unreadable, so dropping it only from `remote_ids`
    # would make a local ride whose remote twin is a stub look deleted — and
    # delete a real ride.
    local_ids -= stub_ids

    # Sanity gate before anything destructive. A successful-but-degraded 200 —
    # an empty or truncated library, a permission change, partial pagination —
    # would otherwise make every local activity in the window look deleted, and
    # CASCADE takes its streams, intervals and climbs with it. A 5xx raises and
    # is caught upstream; a wrongly-empty 200 is not, so it is handled here.
    if local_ids and not remote_ids:
        print("[intervals_icu] reconcile: remote set is empty while "
              f"{len(local_ids)} local activities exist — skipping deletion pass")
        return (0, 0)
    if local_ids and len(remote_ids) < len(local_ids) * MIN_REMOTE_RATIO:
        print(f"[intervals_icu] reconcile: remote set ({len(remote_ids)}) is implausibly "
              f"small against local ({len(local_ids)}) — skipping deletion pass")
        return (0, 0)

    missing_ids = sorted(local_ids - remote_ids)
    if len(missing_ids) > MAX_DELETIONS_PER_SWEEP:
        # An over-cap batch is far more likely a partial response than that many
        # genuine deletions — but a real bulk delete must not be stranded
        # forever, leaving ghost rides feeding CTL/ATL/TSB. So the batch is
        # confirmed across sweeps instead of refused outright: a genuine
        # deletion reproduces the missing set exactly, while a TRANSIENT
        # degradation is unlikely to return the identical set twice. Sweeps are
        # daily, so a real bulk delete costs one day of delay.
        #
        # Residual, accepted: this does NOT defend against *persistent*
        # degradation. A permission or visibility change that consistently omits
        # the same activities for two consecutive sweeps reproduces the same
        # fingerprint and would be confirmed, then CASCADE-deleted — permanently,
        # since those rides sit outside the 14-day sync window. MIN_REMOTE_RATIO
        # bounds the blast radius to under half the local set, and the
        # alternative (refusing forever) leaves ghost rides skewing CTL/ATL/TSB
        # indefinitely, so this is the better of two imperfect options rather
        # than a safe one. Revisit if intervals.icu ever exposes an explicit
        # deletion signal.
        from db import get_sync_state, set_sync_state
        fingerprint = ",".join(missing_ids)
        if get_sync_state(conn, PENDING_DELETIONS_KEY) == fingerprint:
            print(f"[intervals_icu] reconcile: the same {len(missing_ids)} activities "
                  f"are missing for a second consecutive sweep — treating as a genuine "
                  f"bulk deletion and proceeding")
            set_sync_state(conn, PENDING_DELETIONS_KEY, "")
        else:
            set_sync_state(conn, PENDING_DELETIONS_KEY, fingerprint)
            print(f"[intervals_icu] reconcile: {len(missing_ids)} activities look "
                  f"deleted, above the {MAX_DELETIONS_PER_SWEEP} per-sweep cap. "
                  f"Deferring — if the next sweep reports the identical set it will "
                  f"be treated as a real bulk deletion and processed. A partial "
                  f"response is unlikely to repeat exactly.")
            return (0, len(remote_ids - local_ids))
    else:
        # A normal-sized batch clears any pending confirmation.
        from db import set_sync_state
        set_sync_state(conn, PENDING_DELETIONS_KEY, "")

    deleted = 0
    for missing in missing_ids:
        if handle_intervals_icu_deletion(conn, missing) == "deleted":
            deleted += 1
            print(f"  ✕ {missing} removed on intervals.icu — deleted")

    recovered = len(remote_ids - local_ids)
    if recovered:
        print(f"[intervals_icu] {recovered} remote activities not present locally "
              f"— next poll will ingest any inside the sync window")
    return deleted, recovered
