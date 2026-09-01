"""PostgreSQL connection, schema creation, and upsert helpers."""

import os
from datetime import datetime, timezone

import psycopg2
import psycopg2.extras


def get_connection():
    """Connect to PostgreSQL using DATABASE_URL env var."""
    url = os.environ["DATABASE_URL"]
    conn = psycopg2.connect(url)
    conn.autocommit = True
    return conn


def create_schema(conn):
    """Create all tables if they don't exist."""
    with conn.cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS activities (
                id              SERIAL PRIMARY KEY,
                strava_id       BIGINT UNIQUE,
                komoot_tour_id  BIGINT,
                name            TEXT,
                date            TIMESTAMPTZ,
                distance_m      FLOAT,
                duration_s      INTEGER,
                elevation_m     FLOAT,
                avg_hr          INTEGER,
                max_hr          INTEGER,
                avg_power       INTEGER,
                max_power       INTEGER,
                avg_cadence     INTEGER,
                avg_speed_kmh   FLOAT,
                calories        INTEGER,
                suffer_score    INTEGER,
                device          TEXT,
                synced_at       TIMESTAMPTZ
            );

            CREATE TABLE IF NOT EXISTS activity_streams (
                id              SERIAL PRIMARY KEY,
                activity_id     INTEGER REFERENCES activities(id) ON DELETE CASCADE,
                time_offset     INTEGER,
                hr              INTEGER,
                power           INTEGER,
                cadence         INTEGER,
                speed_kmh       FLOAT,
                altitude_m      FLOAT,
                lat             FLOAT,
                lng             FLOAT
            );

            CREATE TABLE IF NOT EXISTS athlete_stats (
                date            DATE PRIMARY KEY,
                ctl             FLOAT,
                atl             FLOAT,
                tsb             FLOAT,
                resting_hr      INTEGER,
                vo2max          FLOAT,
                weekly_distance_m  FLOAT,
                weekly_elevation_m FLOAT
            );

            CREATE TABLE IF NOT EXISTS routes (
                id              SERIAL PRIMARY KEY,
                komoot_id       BIGINT UNIQUE,
                name            TEXT,
                distance_m      FLOAT,
                elevation_m     FLOAT,
                sport           TEXT,
                last_ridden_at  DATE,
                ride_count      INTEGER
            );

            CREATE TABLE IF NOT EXISTS sync_state (
                key             TEXT PRIMARY KEY,
                last_synced_at  TIMESTAMPTZ,
                value           TEXT
            );

            CREATE TABLE IF NOT EXISTS ride_intervals (
                id              SERIAL PRIMARY KEY,
                activity_id     INTEGER REFERENCES activities(id) ON DELETE CASCADE,
                start_offset_s  INTEGER NOT NULL,
                duration_s      INTEGER NOT NULL,
                avg_power       FLOAT,
                np              FLOAT,
                max_power       FLOAT,
                avg_hr          INTEGER,
                classification  TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_ride_intervals_activity_id
                ON ride_intervals(activity_id);
            CREATE INDEX IF NOT EXISTS idx_ride_intervals_classification
                ON ride_intervals(classification);

            ALTER TABLE activities ADD COLUMN IF NOT EXISTS is_indoor BOOLEAN;
            ALTER TABLE activities ADD COLUMN IF NOT EXISTS sport_type TEXT;
            ALTER TABLE activities ADD COLUMN IF NOT EXISTS tss FLOAT;
            ALTER TABLE activities ADD COLUMN IF NOT EXISTS np FLOAT;
            ALTER TABLE activities ADD COLUMN IF NOT EXISTS ef FLOAT;
            ALTER TABLE activities ADD COLUMN IF NOT EXISTS work_kj FLOAT;
            ALTER TABLE activities ADD COLUMN IF NOT EXISTS ride_ftp FLOAT;
            ALTER TABLE activities ADD COLUMN IF NOT EXISTS intensity_factor FLOAT;
            ALTER TABLE activities ADD COLUMN IF NOT EXISTS trimp FLOAT;
            ALTER TABLE activities ADD COLUMN IF NOT EXISTS variability_index FLOAT;
            ALTER TABLE activities ADD COLUMN IF NOT EXISTS aerobic_decoupling FLOAT;
            ALTER TABLE activities ADD COLUMN IF NOT EXISTS ride_weight FLOAT;
            -- Provider-independent ride metrics: computed by the ingestor from
            -- our own streams, so every ride has them whichever source
            -- delivered it. Never imported from a provider.
            ALTER TABLE activities ADD COLUMN IF NOT EXISTS coasting_time_s INTEGER;
            ALTER TABLE activities ADD COLUMN IF NOT EXISTS kj_above_ftp FLOAT;
            ALTER TABLE activities ADD COLUMN IF NOT EXISTS polarization_index FLOAT;

            CREATE TABLE IF NOT EXISTS cp_estimates (
                date            DATE PRIMARY KEY,
                cp_watts        FLOAT,
                w_prime_kj      FLOAT,
                r_squared       FLOAT,
                period_days     INTEGER,
                duration_count  INTEGER,
                source          TEXT NOT NULL,
                fallback_ftp    FLOAT,
                updated_at      TIMESTAMPTZ DEFAULT NOW()
            );

            CREATE INDEX IF NOT EXISTS idx_activities_date ON activities(date);
            CREATE INDEX IF NOT EXISTS idx_activity_streams_activity_id ON activity_streams(activity_id);
            CREATE UNIQUE INDEX IF NOT EXISTS idx_activities_komoot_tour_id
                ON activities(komoot_tour_id) WHERE komoot_tour_id IS NOT NULL;

            ALTER TABLE activity_streams ADD COLUMN IF NOT EXISTS w_bal FLOAT;

            ALTER TABLE activities ADD COLUMN IF NOT EXISTS rwgps_id BIGINT;
            CREATE UNIQUE INDEX IF NOT EXISTS idx_activities_rwgps_id
                ON activities(rwgps_id) WHERE rwgps_id IS NOT NULL;

            ALTER TABLE activities ADD COLUMN IF NOT EXISTS intervals_icu_id TEXT;
            CREATE UNIQUE INDEX IF NOT EXISTS idx_activities_intervals_icu_id
                ON activities(intervals_icu_id) WHERE intervals_icu_id IS NOT NULL;
            -- Stores the activity's `analyzed` timestamp so streams are re-fetched
            -- only when it advances. Without it every poll re-downloads every
            -- stream in the window.
            ALTER TABLE activities ADD COLUMN IF NOT EXISTS intervals_icu_analyzed TIMESTAMPTZ;

            CREATE TABLE IF NOT EXISTS ride_climbs (
                id              SERIAL PRIMARY KEY,
                activity_id     INTEGER REFERENCES activities(id) ON DELETE CASCADE,
                start_offset    INTEGER,
                end_offset      INTEGER,
                gain_m          INTEGER,
                length_m        INTEGER,
                avg_grade       FLOAT,
                start_alt       INTEGER,
                peak_alt        INTEGER,
                duration_s      INTEGER,
                category        TEXT,
                score           INTEGER,
                source          TEXT DEFAULT 'detected',
                segment_name    TEXT
            );

            ALTER TABLE ride_climbs ADD COLUMN IF NOT EXISTS source TEXT DEFAULT 'detected';
            ALTER TABLE ride_climbs ADD COLUMN IF NOT EXISTS segment_name TEXT;

            ALTER TABLE ride_climbs ADD COLUMN IF NOT EXISTS score INTEGER;

            CREATE INDEX IF NOT EXISTS idx_ride_climbs_activity_id ON ride_climbs(activity_id);

            CREATE INDEX IF NOT EXISTS idx_streams_power ON activity_streams(activity_id, time_offset) WHERE power IS NOT NULL;
        """)


def classify_activity(data: dict) -> dict:
    """Add is_indoor and sport_type fields. Only cycling activities are ingested.
    Uses Strava type, device, trainer flag, and distance to classify.
    """
    strava_type = (data.get("strava_type") or "").lower()
    device = data.get("device", "")
    distance_m = data.get("distance_m") or 0
    trainer = data.get("trainer", False)

    if device == "zwift" or strava_type == "virtualride":
        is_indoor, sport_type = True, "zwift"
    elif trainer:
        is_indoor, sport_type = True, "cycling_indoor"
    elif strava_type == "ebikeride":
        is_indoor, sport_type = False, "ebike"
    elif distance_m > 0:
        is_indoor, sport_type = False, "cycling_outdoor"
    else:
        is_indoor, sport_type = True, "cycling_indoor"

    return {**data, "is_indoor": is_indoor, "sport_type": sport_type}


def find_duplicate(conn, date_str: str, duration_s: int, distance_m: float = 0,
                   tolerance_seconds: int = 300) -> tuple | None:
    """Find an existing activity that started within tolerance of date_str
    and has a similar duration OR similar distance. Returns activity id or None.
    Used to detect cross-device duplicates (e.g., Karoo + Watch recording same ride).

    Matches if start time is close AND either:
    - Duration within 15% (handles moving_time vs elapsed_time differences)
    - Distance within 10% (handles different GPS sampling / measurement)

    Returns a 10-column tuple:
        (id, strava_id, device, distance_m, avg_hr, avg_power, rwgps_id,
         suffer_score, intervals_icu_id, intervals_icu_analyzed)

    Unpacked positionally in three places, all in this module
    (_is_same_source_activity, merge_activity_data, upsert_activity), so every
    site must change together — a partial change silently misaligns columns.
    """
    with conn.cursor() as cur:
        cur.execute("""
            SELECT id, strava_id, device, distance_m, avg_hr, avg_power, rwgps_id,
                   suffer_score, intervals_icu_id, intervals_icu_analyzed
            FROM activities
            WHERE ABS(EXTRACT(EPOCH FROM (date - %s::timestamptz))) < %s
              AND (
                ABS(duration_s - %s) < GREATEST(300, duration_s * 0.15)
                OR (distance_m > 0 AND %s > 0 AND ABS(distance_m - %s) < distance_m * 0.10)
              )
        """, (date_str, tolerance_seconds, duration_s, distance_m, distance_m))
        return cur.fetchone()


def _is_same_source_activity(duplicate: tuple, data: dict) -> bool:
    """True when the duplicate row IS this activity (same source ID).

    Same-source identity means upsert should go straight to the ON CONFLICT
    path instead of dedup-merging — without this, every re-synced RWGPS trip
    ('updated' action) would re-enter the dedup merge.
    duplicate = (id, strava_id, device, distance_m, avg_hr, avg_power,
                 rwgps_id, suffer_score, intervals_icu_id, intervals_icu_analyzed)
    """
    dup_strava_id, dup_rwgps_id, dup_icu_id = duplicate[1], duplicate[6], duplicate[8]
    if data.get("strava_id") is not None:
        return dup_strava_id == data["strava_id"]
    if data.get("rwgps_id") is not None:
        return dup_rwgps_id == data["rwgps_id"]
    if data.get("intervals_icu_id") is not None:
        return dup_icu_id == data["intervals_icu_id"]
    return False


# Explicit source precedence. intervals.icu is VeloMate's primary source, so it
# wins the merge base regardless of richness — otherwise primacy would be an
# emergent property of scoring rather than an invariant that can be tested.
# Per-column COALESCE in _do_insert still backfills sensor gaps from the loser,
# so choosing a base by source never discards a sensor value only one source had.
#
# Strava and RWGPS sit at the SAME tier deliberately. Ranking them against each
# other was not asked for and loses data: the skip-path UPDATE carries sensor
# columns but not elevation_m or distance_m, so a thinner Strava row outranking
# a richer RWGPS copy would drop that copy's elevation and distance. Between
# equals, richness still decides, exactly as before this change.
SOURCE_PRIORITY = {"intervals_icu": 2, "strava": 1, "rwgps": 1}


def _source_tier(record) -> int:
    """Highest source priority among the IDs a record carries.

    MAX, not first-match or bottom-tier: an already-merged row carries several
    IDs and is always the existing DB row. Dropping it to the bottom would let
    an incoming lower-priority source outrank it and demote intervals.icu data
    — inverting the invariant this exists to establish.
    """
    return max((p for s, p in SOURCE_PRIORITY.items() if record.get(f"{s}_id")),
               default=0)


def _data_richness(data: dict) -> int:
    """Score an activity record by data richness. Higher = more useful data."""
    score = 0
    if data.get("avg_power"):
        score += 3  # power is the most valuable metric
    if data.get("avg_hr"):
        score += 2
    if data.get("distance_m") and data["distance_m"] > 0:
        score += 1
    if data.get("avg_cadence"):
        score += 1
    if data.get("calories"):
        score += 1
    if data.get("elevation_m") and data["elevation_m"] > 0:
        score += 1
    return score


def merge_activity_data(existing: tuple, new_data: dict) -> dict:
    """Merge two activity records, preferring the one with richer data.
    existing = (id, strava_id, device, distance_m, avg_hr, avg_power, rwgps_id, suffer_score)
    Uses data richness scoring — whichever record has more useful fields wins.
    """
    (ex_id, ex_strava_id, ex_device, ex_distance, ex_hr, ex_power,
     ex_rwgps_id, ex_suffer, ex_icu_id, ex_icu_analyzed) = existing
    ex_data = {"avg_power": ex_power, "avg_hr": ex_hr, "distance_m": ex_distance}

    # Source precedence decides the base; richness only breaks ties within a
    # tier. Two rows from the SAME source never reach here —
    # _is_same_source_activity diverts them to the ON CONFLICT path.
    ex_record = {"strava_id": ex_strava_id, "rwgps_id": ex_rwgps_id,
                 "intervals_icu_id": ex_icu_id}
    ex_tier, new_tier = _source_tier(ex_record), _source_tier(new_data)
    if new_tier != ex_tier:
        new_wins = new_tier > ex_tier
    else:
        new_wins = _data_richness(new_data) >= _data_richness(ex_data)

    if new_wins:
        merged = dict(new_data)
        # Fill any missing fields from existing record
        if not merged.get("avg_hr") and ex_hr:
            merged["avg_hr"] = ex_hr
        if not merged.get("avg_power") and ex_power:
            merged["avg_power"] = ex_power
        if not merged.get("distance_m") and ex_distance:
            merged["distance_m"] = ex_distance
        # Carry source IDs from the existing row so the merged record keeps both
        if not merged.get("strava_id") and ex_strava_id:
            merged["strava_id"] = ex_strava_id
        if not merged.get("rwgps_id") and ex_rwgps_id:
            merged["rwgps_id"] = ex_rwgps_id
        # Both intervals.icu columns must survive: this path delete-and-
        # reinserts, so a lost analyzed makes the sync gate fire on every poll
        # for that ride, forever.
        if not merged.get("intervals_icu_id") and ex_icu_id:
            merged["intervals_icu_id"] = ex_icu_id
        if not merged.get("intervals_icu_analyzed") and ex_icu_analyzed:
            merged["intervals_icu_analyzed"] = ex_icu_analyzed
        # Carry suffer_score — only Strava supplies it, and the merge path
        # delete-and-reinserts the row, so an unfilled gap would lose it
        if merged.get("suffer_score") is None and ex_suffer is not None:
            merged["suffer_score"] = ex_suffer
    else:
        # Existing is richer — just fill gaps, don't replace base
        merged = dict(new_data)
        merged["_skip_insert"] = True  # signal caller to skip this activity

    return merged


def _do_insert(conn, data: dict, now) -> int:
    """Execute the INSERT ... ON CONFLICT for an activity. Returns activity id.

    The conflict target is chosen by source: Strava activities upsert on
    strava_id, RWGPS activities on the partial-unique rwgps_id index. The
    RWGPS update list omits suffer_score — RWGPS never supplies it, and the
    standard list would null out a Strava-provided value on a dual-source row.
    Sensor-derived columns are NULL-protected with COALESCE on both paths so
    a re-sync from the weaker source of a dual-ID row cannot wipe sensor data
    merged in from the richer source. distance_m and elevation_m use
    NULLIF(EXCLUDED.col, 0)+COALESCE: RWGPS parsing coerces an absent
    distance/elevation to 0 (not NULL), so a plain EXCLUDED overwrite on an
    'updated' event would zero a richer source's value — NULLIF maps that 0
    back to NULL so COALESCE keeps the existing value, while a real edit (any
    non-zero value) still propagates.
    """
    params = {"strava_id": None, "rwgps_id": None, "intervals_icu_id": None,
              "intervals_icu_analyzed": None, **data, "synced_at": now}
    if params["strava_id"] is not None:
        conflict_target = "(strava_id)"
        suffer_set = ",\n                suffer_score = EXCLUDED.suffer_score"
    elif params["rwgps_id"] is not None:
        conflict_target = "(rwgps_id) WHERE rwgps_id IS NOT NULL"
        suffer_set = ""
    else:
        conflict_target = "(intervals_icu_id) WHERE intervals_icu_id IS NOT NULL"
        suffer_set = ""
    with conn.cursor() as cur:
        cur.execute(f"""
            INSERT INTO activities (
                strava_id, rwgps_id, intervals_icu_id, intervals_icu_analyzed,
                name, date, distance_m, duration_s, elevation_m,
                avg_hr, max_hr, avg_power, max_power, avg_cadence,
                avg_speed_kmh, calories, suffer_score, device,
                is_indoor, sport_type, synced_at
            ) VALUES (
                %(strava_id)s, %(rwgps_id)s, %(intervals_icu_id)s, %(intervals_icu_analyzed)s,
                %(name)s, %(date)s, %(distance_m)s, %(duration_s)s, %(elevation_m)s,
                %(avg_hr)s, %(max_hr)s, %(avg_power)s, %(max_power)s, %(avg_cadence)s,
                %(avg_speed_kmh)s, %(calories)s, %(suffer_score)s, %(device)s,
                %(is_indoor)s, %(sport_type)s, %(synced_at)s
            )
            ON CONFLICT {conflict_target} DO UPDATE SET
                name = EXCLUDED.name,
                distance_m = COALESCE(NULLIF(EXCLUDED.distance_m, 0), activities.distance_m),
                duration_s = EXCLUDED.duration_s,
                elevation_m = COALESCE(NULLIF(EXCLUDED.elevation_m, 0), activities.elevation_m),
                avg_hr = COALESCE(EXCLUDED.avg_hr, activities.avg_hr),
                max_hr = COALESCE(EXCLUDED.max_hr, activities.max_hr),
                avg_power = COALESCE(EXCLUDED.avg_power, activities.avg_power),
                max_power = COALESCE(EXCLUDED.max_power, activities.max_power),
                avg_cadence = COALESCE(EXCLUDED.avg_cadence, activities.avg_cadence),
                avg_speed_kmh = EXCLUDED.avg_speed_kmh,
                calories = COALESCE(EXCLUDED.calories, activities.calories),
                device = EXCLUDED.device,
                is_indoor = EXCLUDED.is_indoor,
                sport_type = EXCLUDED.sport_type,
                intervals_icu_id = COALESCE(EXCLUDED.intervals_icu_id, activities.intervals_icu_id),
                intervals_icu_analyzed = COALESCE(EXCLUDED.intervals_icu_analyzed, activities.intervals_icu_analyzed),
                synced_at = EXCLUDED.synced_at{suffer_set}
            RETURNING id
        """, params)
        return cur.fetchone()[0]


def upsert_activity(conn, data: dict) -> tuple[int, bool]:
    """Insert or update an activity. Returns (activity_id, streams_preserved).
    streams_preserved=True means the caller must NOT overwrite streams — the
    surviving row already holds the streams we want to keep.

    - Skip path (existing row richer): returns True. The incoming weaker copy's
      streams must not clobber the richer existing row's streams.
    - Merge path (incoming copy richer): returns False. The merge keeps the
      incoming record, so the caller's freshly fetched (richer) streams should
      win. The old duplicate's streams are restored only as a fallback for when
      the caller has none of its own.
    """
    now = datetime.now(timezone.utc)
    data = classify_activity(data)

    # Duplicate detection: check if another activity started within 5 min with similar duration
    if data.get("date") and data.get("duration_s"):
        duplicate = find_duplicate(conn, data["date"], data["duration_s"], data.get("distance_m", 0))
        if duplicate and not _is_same_source_activity(duplicate, data):
            ex_id = duplicate[0]
            merged = merge_activity_data(duplicate, data)
            if merged.get("_skip_insert"):
                print(f"  [dedup] Skipping {data['name']} — weaker duplicate of existing activity {ex_id}")
                # Still update the existing record with any new fields from the incoming data
                # (e.g. suffer_score, tss, calories that may arrive on a later sync)
                now = datetime.now(timezone.utc)
                with conn.cursor() as cur:
                    cur.execute("""
                        UPDATE activities SET
                            suffer_score = COALESCE(suffer_score, %(suffer_score)s),
                            tss          = COALESCE(tss, %(tss)s),
                            calories     = COALESCE(calories, %(calories)s),
                            avg_hr       = COALESCE(avg_hr, %(avg_hr)s),
                            avg_power    = COALESCE(avg_power, %(avg_power)s),
                            max_hr       = COALESCE(max_hr, %(max_hr)s),
                            max_power    = COALESCE(max_power, %(max_power)s),
                            avg_cadence  = COALESCE(avg_cadence, %(avg_cadence)s),
                            -- Closes the one gap the skip path used to leave: a
                            -- richer copy that loses the merge base on source
                            -- precedence had its elevation and distance dropped
                            -- entirely. NULLIF mirrors _do_insert, because RWGPS
                            -- coerces an absent value to 0 rather than NULL, so a
                            -- plain COALESCE would treat 0 as a real measurement.
                            elevation_m  = COALESCE(NULLIF(elevation_m, 0),
                                                    NULLIF(%(elevation_m)s, 0), elevation_m),
                            distance_m   = COALESCE(NULLIF(distance_m, 0),
                                                    NULLIF(%(distance_m)s, 0), distance_m),
                            strava_id    = COALESCE(strava_id, %(strava_id)s),
                            rwgps_id     = COALESCE(rwgps_id, %(rwgps_id)s),
                            intervals_icu_id = COALESCE(intervals_icu_id, %(intervals_icu_id)s),
                            intervals_icu_analyzed = COALESCE(intervals_icu_analyzed, %(intervals_icu_analyzed)s),
                            synced_at    = %(synced_at)s
                        WHERE id = %(ex_id)s
                    """, {
                        "suffer_score": data.get("suffer_score"),
                        "tss":          data.get("tss"),
                        "calories":     data.get("calories"),
                        "avg_hr":       data.get("avg_hr"),
                        "avg_power":    data.get("avg_power"),
                        "max_hr":       data.get("max_hr"),
                        "max_power":    data.get("max_power"),
                        "avg_cadence":  data.get("avg_cadence"),
                        "elevation_m":  data.get("elevation_m"),
                        "distance_m":   data.get("distance_m"),
                        "strava_id":    data.get("strava_id"),
                        "rwgps_id":     data.get("rwgps_id"),
                        "intervals_icu_id": data.get("intervals_icu_id"),
                        "intervals_icu_analyzed": data.get("intervals_icu_analyzed"),
                        "synced_at":    now,
                        "ex_id":        ex_id,
                    })
                return ex_id, True
            else:
                # Atomic merge: save streams, delete old, insert new, restore streams
                print(f"  [dedup] Merging {data['name']} with existing activity {ex_id}")
                conn.autocommit = False
                try:
                    with conn.cursor() as cur:
                        # Save existing streams in memory before CASCADE deletes them
                        cur.execute("""
                            SELECT time_offset, hr, power, cadence, speed_kmh, altitude_m, lat, lng
                            FROM activity_streams WHERE activity_id = %s
                        """, (ex_id,))
                        saved_streams = cur.fetchall()
                        cur.execute("DELETE FROM activities WHERE id = %s", (ex_id,))
                    data = merged
                    activity_id = _do_insert(conn, data, now)
                    # Restore the old duplicate's streams as a FALLBACK only —
                    # if the caller has freshly fetched streams for this richer
                    # record it will overwrite them (streams_preserved=False).
                    # This restore just avoids leaving the merged ride with no
                    # streams when the caller brought none of its own.
                    if saved_streams:
                        with conn.cursor() as cur:
                            cur.execute("SELECT COUNT(*) FROM activity_streams WHERE activity_id = %s", (activity_id,))
                            if cur.fetchone()[0] == 0:
                                psycopg2.extras.execute_values(
                                    cur,
                                    """INSERT INTO activity_streams
                                        (activity_id, time_offset, hr, power, cadence, speed_kmh, altitude_m, lat, lng)
                                        VALUES %s""",
                                    [(activity_id, *row) for row in saved_streams],
                                )
                    conn.commit()
                    # False: the merged record is the incoming (richer) copy, so
                    # the caller's freshly fetched streams should replace the
                    # restored fallback. Returning True here would discard them.
                    return activity_id, False
                except Exception:
                    conn.rollback()
                    raise
                finally:
                    conn.autocommit = True

    return _do_insert(conn, data, now), False


def upsert_streams(conn, activity_id: int, streams: list):
    """Replace streams for an activity. Wrapped in a transaction so a crash
    between DELETE and INSERT doesn't leave the activity with no streams.
    """
    conn.autocommit = False
    try:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM activity_streams WHERE activity_id = %s", (activity_id,))
            if streams:
                psycopg2.extras.execute_values(
                    cur,
                    """INSERT INTO activity_streams
                        (activity_id, time_offset, hr, power, cadence, speed_kmh, altitude_m, lat, lng)
                        VALUES %s""",
                    [(activity_id, s.get("time_offset"), s.get("hr"), s.get("power"),
                      s.get("cadence"), s.get("speed_kmh"), s.get("altitude_m"),
                      s.get("lat"), s.get("lng")) for s in streams]
                )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.autocommit = True


def handle_rwgps_deletion(conn, rwgps_id: int) -> str:
    """Process a 'deleted' sync item from RWGPS.

    Returns "deleted" (RWGPS-only activity removed — CASCADE wipes streams,
    intervals, climbs), "unlinked" (dual-source activity kept, rwgps_id
    cleared because the ride still exists on Strava), or "not_found".
    """
    with conn.cursor() as cur:
        cur.execute("SELECT id, strava_id FROM activities WHERE rwgps_id = %s", (rwgps_id,))
        row = cur.fetchone()
        if not row:
            return "not_found"
        act_id, strava_id = row
        if strava_id is not None:
            cur.execute("UPDATE activities SET rwgps_id = NULL WHERE id = %s", (act_id,))
            return "unlinked"
        cur.execute("DELETE FROM activities WHERE id = %s", (act_id,))
        return "deleted"


def handle_intervals_icu_deletion(conn, intervals_icu_id: str) -> str:
    """Process an activity that has vanished from intervals.icu.

    Returns "deleted" (intervals.icu-only row removed — CASCADE wipes streams,
    intervals, climbs), "unlinked" (multi-source row kept, intervals_icu_id
    cleared), or "not_found". Same rule as handle_rwgps_deletion.
    """
    with conn.cursor() as cur:
        cur.execute("SELECT id, strava_id, rwgps_id FROM activities "
                    "WHERE intervals_icu_id = %s", (intervals_icu_id,))
        row = cur.fetchone()
        if not row:
            return "not_found"
        act_id, strava_id, rwgps_id = row
        if strava_id is not None or rwgps_id is not None:
            cur.execute("UPDATE activities SET intervals_icu_id = NULL WHERE id = %s", (act_id,))
            return "unlinked"
        cur.execute("DELETE FROM activities WHERE id = %s", (act_id,))
        return "deleted"


def upsert_athlete_stats(conn, date, stats: dict):
    """Insert or update athlete stats for a date."""
    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO athlete_stats (date, ctl, atl, tsb, resting_hr, vo2max, weekly_distance_m, weekly_elevation_m)
            VALUES (%(date)s, %(ctl)s, %(atl)s, %(tsb)s, %(resting_hr)s, %(vo2max)s, %(weekly_distance_m)s, %(weekly_elevation_m)s)
            ON CONFLICT (date) DO UPDATE SET
                ctl = EXCLUDED.ctl,
                atl = EXCLUDED.atl,
                tsb = EXCLUDED.tsb,
                resting_hr = EXCLUDED.resting_hr,
                vo2max = EXCLUDED.vo2max,
                weekly_distance_m = EXCLUDED.weekly_distance_m,
                weekly_elevation_m = EXCLUDED.weekly_elevation_m
        """, {"date": date, **stats})


def get_sync_state(conn, key: str):
    """Get the value for a sync state key, or None."""
    with conn.cursor() as cur:
        cur.execute("SELECT value FROM sync_state WHERE key = %s", (key,))
        row = cur.fetchone()
        return row[0] if row else None


# Distinguishes "we have never stored this activity" from "we stored it and its
# analyzed value is NULL". Collapsing both to None makes an activity whose
# intervals.icu `analyzed` is null — a still-processing upload, a manual entry,
# a de-analysed ride — look changed on every single poll: its streams are
# re-downloaded and it is counted as ingested each cycle, which forces a full
# fitness recalculation. A sentinel keeps the two cases apart.
ACTIVITY_ABSENT = object()


def get_intervals_icu_analyzed(conn, intervals_icu_id: str):
    """The stored `analyzed` timestamp for an activity, as an ISO string.

    Returns ACTIVITY_ABSENT when no row carries this id — the caller must
    ingest it. Returns None when the row exists but has no analyzed value,
    which compares equal to an incoming None and so does not re-trigger.
    """
    with conn.cursor() as cur:
        cur.execute("SELECT intervals_icu_analyzed FROM activities "
                    "WHERE intervals_icu_id = %s", (intervals_icu_id,))
        row = cur.fetchone()
        if not row:
            return ACTIVITY_ABSENT
        if row[0] is None:
            return None
        return row[0].isoformat() if hasattr(row[0], "isoformat") else str(row[0])


def set_sync_state(conn, key: str, value: str):
    """Set a sync state key/value."""
    now = datetime.now(timezone.utc)
    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO sync_state (key, value, last_synced_at)
            VALUES (%s, %s, %s)
            ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, last_synced_at = EXCLUDED.last_synced_at
        """, (key, value, now))
