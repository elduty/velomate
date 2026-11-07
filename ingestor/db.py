"""PostgreSQL connection, schema creation, and upsert helpers."""

import contextlib
import os
from datetime import datetime, timezone

import psycopg2
import psycopg2.extras

from route_match import haversine_m

# Above this a GPS-derived speed is a jump in the fix, not riding
MAX_DERIVED_SPEED_KMH = 120.0
# One GPS step is mostly noise: 3 m of error at 1 Hz reads as 10 km/h
SPEED_WINDOW_S = 5


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

            -- Route identity. Computed by the ingestor from stored GPS, so a
            -- ride groups the same way whichever source delivered it.
            CREATE TABLE IF NOT EXISTS ride_routes (
                id            SERIAL PRIMARY KEY,
                label         TEXT NOT NULL,
                distance_m    DOUBLE PRECISION,
                elevation_m   DOUBLE PRECISION,
                ride_count    INTEGER NOT NULL DEFAULT 0,
                first_ridden  DATE,
                last_ridden   DATE
            );

            -- Route identity must survive a rebuild. Step 2.7 deletes and
            -- re-inserts every route on each recalculation, and drawing a
            -- fresh serial id each time renumbered every route whenever a ride
            -- landed -- which silently emptied the Route Comparison dashboard
            -- for anyone holding a saved var-route=<id> URL, since its panels
            -- filter WHERE route_id = <id>. The founding ride is the stable
            -- natural key: activity ids never change and founding order is
            -- deterministic, so it hands a rebuilt route its previous id.
            ALTER TABLE ride_routes
                ADD COLUMN IF NOT EXISTS founding_activity_id INTEGER;
            CREATE UNIQUE INDEX IF NOT EXISTS idx_ride_routes_founding_activity
                ON ride_routes(founding_activity_id)
                WHERE founding_activity_id IS NOT NULL;

            -- Nullable on purpose: an indoor ride, or one with too little GPS,
            -- has no route and that is a normal state rather than a failure.
            ALTER TABLE activities ADD COLUMN IF NOT EXISTS route_id INTEGER
                REFERENCES ride_routes(id);
            ALTER TABLE activities ADD COLUMN IF NOT EXISTS route_signature TEXT;

            CREATE INDEX IF NOT EXISTS idx_activities_route
                ON activities(route_id);

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
            -- The CP (watts) and W' (joules) this ride's w_bal was modelled with.
            -- Stored by the W'bal step rather than looked up again, because
            -- estimates change after a ride is modelled and w_bal does not.
            ALTER TABLE activities ADD COLUMN IF NOT EXISTS wbal_cp FLOAT;
            ALTER TABLE activities ADD COLUMN IF NOT EXISTS wbal_w_prime_j FLOAT;

            ALTER TABLE activities ADD COLUMN IF NOT EXISTS rwgps_id BIGINT;
            CREATE UNIQUE INDEX IF NOT EXISTS idx_activities_rwgps_id
                ON activities(rwgps_id) WHERE rwgps_id IS NOT NULL;

            -- A FIT/GPX file's identity is the SHA-256 of its bytes. It is the
            -- file source's id, the way rwgps_id is RWGPS's.
            ALTER TABLE activities ADD COLUMN IF NOT EXISTS file_sha256 TEXT;
            CREATE UNIQUE INDEX IF NOT EXISTS idx_activities_file_sha256
                ON activities(file_sha256) WHERE file_sha256 IS NOT NULL;

            -- Ledger of every file seen, whatever came of it.
            CREATE TABLE IF NOT EXISTS imported_files (
                sha256      TEXT PRIMARY KEY,
                name        TEXT NOT NULL,
                size_bytes  BIGINT NOT NULL,
                first_seen  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                last_seen   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                via         TEXT NOT NULL,          -- 'folder' | 'upload'
                outcome     TEXT NOT NULL,          -- imported|merged|duplicate|not_cycling|failed
                detail      TEXT,
                activity_id INTEGER REFERENCES activities(id) ON DELETE SET NULL
            );
            CREATE TABLE IF NOT EXISTS imported_paths (
                path        TEXT PRIMARY KEY,       -- path under /app/import
                size_bytes  BIGINT NOT NULL,
                mtime_ns    BIGINT NOT NULL,
                done_at     TIMESTAMPTZ NOT NULL DEFAULT NOW()
            );

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

            -- Recurring climbs matched across rides (see ingestor/segments.py).
            -- name is set by hand (segments.py rename) and wins over auto_name.
            CREATE TABLE IF NOT EXISTS climb_segments (
                id          SERIAL PRIMARY KEY,
                name        TEXT,
                auto_name   TEXT NOT NULL,
                start_lat   DOUBLE PRECISION NOT NULL,
                start_lng   DOUBLE PRECISION NOT NULL,
                end_lat     DOUBLE PRECISION NOT NULL,
                end_lng     DOUBLE PRECISION NOT NULL,
                start_alt   INTEGER,
                end_alt     INTEGER,
                length_m    INTEGER,
                gain_m      INTEGER,
                avg_grade   FLOAT,
                category    TEXT,
                created_at  TIMESTAMPTZ DEFAULT NOW()
            );

            CREATE TABLE IF NOT EXISTS climb_efforts (
                id              SERIAL PRIMARY KEY,
                segment_id      INTEGER NOT NULL REFERENCES climb_segments(id) ON DELETE CASCADE,
                activity_id     INTEGER NOT NULL REFERENCES activities(id) ON DELETE CASCADE,
                start_offset    INTEGER NOT NULL,
                end_offset      INTEGER NOT NULL,
                elapsed_s       INTEGER NOT NULL,
                moving_s        INTEGER,
                avg_speed_kmh   FLOAT,
                vam_mh          INTEGER,
                avg_hr          INTEGER,
                avg_power       INTEGER,
                UNIQUE (segment_id, activity_id, start_offset)
            );
            CREATE INDEX IF NOT EXISTS idx_climb_efforts_activity_id ON climb_efforts(activity_id);

            -- (segment, ride) pairs already matched, so each ride is scanned once.
            CREATE TABLE IF NOT EXISTS climb_segment_scans (
                segment_id  INTEGER NOT NULL REFERENCES climb_segments(id) ON DELETE CASCADE,
                activity_id INTEGER NOT NULL REFERENCES activities(id) ON DELETE CASCADE,
                PRIMARY KEY (segment_id, activity_id)
            );

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

    Returns an 11-column tuple:
        (id, strava_id, device, distance_m, avg_hr, avg_power, rwgps_id,
         suffer_score, intervals_icu_id, intervals_icu_analyzed, file_sha256)

    Unpacked positionally in three places, all in this module
    (_is_same_source_activity, merge_activity_data, upsert_activity), so every
    site must change together — a partial change silently misaligns columns.
    """
    with conn.cursor() as cur:
        cur.execute("""
            SELECT id, strava_id, device, distance_m, avg_hr, avg_power, rwgps_id,
                   suffer_score, intervals_icu_id, intervals_icu_analyzed, file_sha256
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
                 rwgps_id, suffer_score, intervals_icu_id, intervals_icu_analyzed,
                 file_sha256)
    """
    dup_strava_id, dup_rwgps_id, dup_icu_id = duplicate[1], duplicate[6], duplicate[8]
    if data.get("strava_id") is not None:
        return dup_strava_id == data["strava_id"]
    if data.get("rwgps_id") is not None:
        return dup_rwgps_id == data["rwgps_id"]
    if data.get("intervals_icu_id") is not None:
        return dup_icu_id == data["intervals_icu_id"]
    if data.get("file_sha256") is not None:
        return duplicate[10] == data["file_sha256"]
    return False


# Explicit source precedence. intervals.icu is VeloMate's primary source, so it
# wins the merge base regardless of richness — otherwise primacy would be an
# emergent property of scoring rather than an invariant that can be tested.
# Per-column COALESCE in _merge_in_place still backfills gaps from the loser, so
# choosing a base by source never discards a value only one source had.
#
# Strava and RWGPS sit at the SAME tier deliberately. Ranking them against each
# other was not asked for and loses data: the skip-path UPDATE carries sensor
# columns but not elevation_m or distance_m, so a thinner Strava row outranking
# a richer RWGPS copy would drop that copy's elevation and distance. Between
# equals, richness still decides, exactly as before this change.
#
# A file is tier 0, below every cloud source: a cloud copy of the same ride is
# the better base. _source_tier keeps default=0 because every stored row carries
# some source id, so "no id" and "file only" never need telling apart (the entry
# documents the tier; no "file_id" key is ever looked up).
SOURCE_PRIORITY = {"intervals_icu": 2, "strava": 1, "rwgps": 1, "file": 0}


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
     ex_rwgps_id, ex_suffer, ex_icu_id, ex_icu_analyzed, ex_file_sha256) = existing
    ex_data = {"avg_power": ex_power, "avg_hr": ex_hr, "distance_m": ex_distance}

    # Source precedence decides the base; richness only breaks ties within a
    # tier. Two rows from the SAME source never reach here —
    # _is_same_source_activity diverts them to the ON CONFLICT path.
    ex_record = {"strava_id": ex_strava_id, "rwgps_id": ex_rwgps_id,
                 "intervals_icu_id": ex_icu_id, "file_sha256": ex_file_sha256}
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
        if not merged.get("file_sha256") and ex_file_sha256:
            merged["file_sha256"] = ex_file_sha256
        # Both intervals.icu columns must survive: a lost analyzed makes the
        # sync gate fire on every poll for that ride, forever.
        if not merged.get("intervals_icu_id") and ex_icu_id:
            merged["intervals_icu_id"] = ex_icu_id
        if not merged.get("intervals_icu_analyzed") and ex_icu_analyzed:
            merged["intervals_icu_analyzed"] = ex_icu_analyzed
        # Carry suffer_score — only Strava supplies it, and Strava never
        # re-fetches an old activity, so a value lost here does not come back
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

    intervals_icu_analyzed is written as sent on the intervals.icu target, so
    stored and incoming compare equal afterwards even when the remote value went
    back to null. Keeping the old timestamp there would leave the sync gate open
    on every poll. The other two targets never carry a value for it and must not
    clear the one a dual-source row holds.
    """
    params = {"strava_id": None, "rwgps_id": None, "intervals_icu_id": None,
              "intervals_icu_analyzed": None, "file_sha256": None,
              **data, "synced_at": now}
    analyzed_set = ("COALESCE(EXCLUDED.intervals_icu_analyzed, "
                    "activities.intervals_icu_analyzed)")
    if params["strava_id"] is not None:
        conflict_target = "(strava_id)"
        suffer_set = ",\n                suffer_score = EXCLUDED.suffer_score"
    elif params["rwgps_id"] is not None:
        conflict_target = "(rwgps_id) WHERE rwgps_id IS NOT NULL"
        suffer_set = ""
    elif params["file_sha256"] is not None:
        conflict_target = "(file_sha256) WHERE file_sha256 IS NOT NULL"
        suffer_set = ""
    else:
        conflict_target = "(intervals_icu_id) WHERE intervals_icu_id IS NOT NULL"
        suffer_set = ""
        analyzed_set = "EXCLUDED.intervals_icu_analyzed"
    with conn.cursor() as cur:
        cur.execute(f"""
            INSERT INTO activities (
                strava_id, rwgps_id, intervals_icu_id, intervals_icu_analyzed,
                file_sha256,
                name, date, distance_m, duration_s, elevation_m,
                avg_hr, max_hr, avg_power, max_power, avg_cadence,
                avg_speed_kmh, calories, suffer_score, device,
                is_indoor, sport_type, synced_at
            ) VALUES (
                %(strava_id)s, %(rwgps_id)s, %(intervals_icu_id)s, %(intervals_icu_analyzed)s,
                %(file_sha256)s,
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
                intervals_icu_analyzed = {analyzed_set},
                synced_at = EXCLUDED.synced_at{suffer_set}
            RETURNING id
        """, params)
        return cur.fetchone()[0]


def _source_id_on_another_row(conn, ex_id: int, data: dict) -> int | None:
    """The id of a row other than ex_id that already holds the incoming source id.

    find_duplicate matches on time and takes the first row it finds, so it can
    return a different row from the one that owns this id, for instance after a
    ride's date is edited upstream. That is a re-sync of the owner rather than a
    new copy of ex_id. Merging anyway violates the unique index on the source
    id, which the Strava and RWGPS loops treat as transient, so one such ride
    would stop every pass.
    """
    with conn.cursor() as cur:
        cur.execute("""
            SELECT id FROM activities
            WHERE id <> %(ex_id)s
              AND (strava_id = %(strava_id)s
                   OR rwgps_id = %(rwgps_id)s
                   OR intervals_icu_id = %(intervals_icu_id)s
                   OR file_sha256 = %(file_sha256)s)
            LIMIT 1
        """, {"ex_id": ex_id, "strava_id": data.get("strava_id"),
              "rwgps_id": data.get("rwgps_id"),
              "intervals_icu_id": data.get("intervals_icu_id"),
              "file_sha256": data.get("file_sha256")})
        row = cur.fetchone()
        return row[0] if row else None


def _clear_stream_derived(cur, activity_id: int) -> None:
    """Reset what was read from a ride's streams, for when they are replaced.

    Recalculation only fills gaps, so anything left here would go on describing
    streams that are gone: the METRICS_VERSION reset's column list minus ride_ftp,
    plus this ride's intervals and detected climbs. Strava climbs stay. They are
    external data that may be impossible to re-fetch, and they hold no stream
    offsets, so replacing the stream cannot invalidate them.
    """
    cur.execute("""
        UPDATE activities SET
            tss = NULL, np = NULL, ef = NULL, work_kj = NULL,
            intensity_factor = NULL, trimp = NULL,
            variability_index = NULL, aerobic_decoupling = NULL,
            coasting_time_s = NULL, kj_above_ftp = NULL,
            polarization_index = NULL, route_signature = NULL,
            wbal_cp = NULL, wbal_w_prime_j = NULL
        WHERE id = %s
    """, (activity_id,))
    cur.execute("DELETE FROM ride_intervals WHERE activity_id = %s", (activity_id,))
    cur.execute("DELETE FROM ride_climbs WHERE activity_id = %s AND source != 'strava'",
                (activity_id,))


def _merge_in_place(conn, ex_id: int, merged: dict, streams: list | None, now) -> None:
    """Merge the winning copy into the existing row, keeping its id.

    Only reached when the incoming copy won the base, so its values go first and
    the row's own values fill the gaps. A 0 counts as a gap: RWGPS and
    intervals.icu coerce an absent value to 0, which is never a real reading for
    any of these columns. suffer_score is the exception, where 0 is real. So does
    an "unknown" device, which is what every source reports when it can't tell.

    The row keeps what the merge does not invalidate: its id (Grafana links and
    ride_routes.founding_activity_id both hold it), route_id, komoot_tour_id and
    ride_weight (a weight change never resets a historical ride). ride_ftp stays too. It comes
    from earlier rides or the configured FTP, never from this ride's own
    streams, and re-deriving it under a since-changed VELOMATE_FTP would rewrite
    a historical TSS.

    Everything derived from the streams is reset, see _clear_stream_derived.

    Without `streams` the existing ones stay until the caller writes any. With
    them, the whole merge is one transaction: committing the row first would let
    a failed stream write strand the new base over the old streams, and for
    intervals.icu that is permanent, because the committed `analyzed` tells every
    later poll the ride is unchanged.
    """
    params = {"strava_id": None, "rwgps_id": None, "intervals_icu_id": None,
              "intervals_icu_analyzed": None, "file_sha256": None, **merged,
              "synced_at": now, "ex_id": ex_id}
    conn.autocommit = False
    try:
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE activities SET
                    strava_id = COALESCE(%(strava_id)s, strava_id),
                    rwgps_id = COALESCE(%(rwgps_id)s, rwgps_id),
                    file_sha256 = COALESCE(%(file_sha256)s, file_sha256),
                    intervals_icu_id = COALESCE(%(intervals_icu_id)s, intervals_icu_id),
                    intervals_icu_analyzed = COALESCE(%(intervals_icu_analyzed)s,
                                                      intervals_icu_analyzed),
                    name = COALESCE(NULLIF(%(name)s, ''), name),
                    date = %(date)s,
                    duration_s = %(duration_s)s,
                    distance_m = COALESCE(NULLIF(%(distance_m)s, 0), distance_m),
                    elevation_m = COALESCE(NULLIF(%(elevation_m)s, 0), elevation_m),
                    avg_hr = COALESCE(NULLIF(%(avg_hr)s, 0), avg_hr),
                    max_hr = COALESCE(NULLIF(%(max_hr)s, 0), max_hr),
                    avg_power = COALESCE(NULLIF(%(avg_power)s, 0), avg_power),
                    max_power = COALESCE(NULLIF(%(max_power)s, 0), max_power),
                    avg_cadence = COALESCE(NULLIF(%(avg_cadence)s, 0), avg_cadence),
                    avg_speed_kmh = COALESCE(NULLIF(%(avg_speed_kmh)s, 0), avg_speed_kmh),
                    calories = COALESCE(NULLIF(%(calories)s, 0), calories),
                    suffer_score = COALESCE(%(suffer_score)s, suffer_score),
                    device = COALESCE(NULLIF(NULLIF(%(device)s, ''), 'unknown'),
                                      device, %(device)s),
                    is_indoor = %(is_indoor)s,
                    sport_type = %(sport_type)s,
                    synced_at = %(synced_at)s
                WHERE id = %(ex_id)s
            """, params)
            _clear_stream_derived(cur, ex_id)
            if streams:
                _replace_streams(cur, ex_id, streams)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.autocommit = True


def _upsert_row(conn, data: dict, streams: list | None, now) -> tuple[int, bool]:
    """The plain insert or re-sync path. Returns (activity_id, streams_written).

    intervals.icu writes the row and its streams in one transaction. The stored
    `analyzed` is what tells every later poll the streams are current, so
    committing it first would let one failed stream write strand the ride for
    good. Strava and RWGPS have a cursor that holds a failed ride back, and their
    callers write the streams themselves.

    Streams here mean the ride is new or intervals.icu re-analysed it, so what was
    derived from the old ones is reset in the same transaction.
    """
    if not streams or data.get("intervals_icu_id") is None:
        return _do_insert(conn, data, now), False
    conn.autocommit = False
    try:
        activity_id = _do_insert(conn, data, now)
        with conn.cursor() as cur:
            _clear_stream_derived(cur, activity_id)
            _replace_streams(cur, activity_id, streams)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.autocommit = True
    return activity_id, True


# Any fixed bigint. One session lock serialises every writer of the dedup path
# (the polling sources and the file importer), so two copies of one ride that
# arrive at once cannot both miss each other in find_duplicate.
INGEST_LOCK_KEY = 7_365_696_973


@contextlib.contextmanager
def ingest_lock(conn):
    """Hold the ingest advisory lock for the block. Session locks nest in
    PostgreSQL, so a caller may hold it around upsert_activity, which takes it too."""
    with conn.cursor() as cur:
        cur.execute("SELECT pg_advisory_lock(%s)", (INGEST_LOCK_KEY,))
    try:
        yield
    finally:
        with conn.cursor() as cur:
            cur.execute("SELECT pg_advisory_unlock(%s)", (INGEST_LOCK_KEY,))


def upsert_activity(conn, data: dict, streams: list | None = None) -> tuple[int, bool]:
    """Insert or update an activity. Returns (activity_id, streams_preserved).
    streams_preserved=True means the caller must NOT overwrite streams — the
    surviving row already holds the streams we want to keep.

    Pass `streams` when the caller already has them, so a merge can write them
    in its own transaction, and so can an intervals.icu insert or re-sync (see
    _upsert_row). On a Strava or RWGPS insert or re-sync they are ignored and the
    caller writes them with upsert_streams, as it does when this returns False.

    - Skip path (existing row richer): returns True. The incoming weaker copy's
      streams must not clobber the richer existing row's streams.
    - Merge path (incoming copy richer): updates the existing row in place, see
      _merge_in_place. Returns True when `streams` were given and written.
      Returns False without them: the existing streams stay, and a caller that
      fetches its streams later overwrites them.
    """
    with ingest_lock(conn):
        return _upsert_activity_locked(conn, data, streams)


def _upsert_activity_locked(conn, data: dict, streams: list | None) -> tuple[int, bool]:
    now = datetime.now(timezone.utc)
    data = classify_activity(data)

    # The same file again, after it was merged onto a cloud ride. Its hash is on
    # that row, so the ON CONFLICT (file_sha256) path would overwrite the cloud
    # ride's name, duration, device and sensor columns with the file's, and the
    # caller would replace its streams. A file never replaces a cloud ride, so
    # nothing changes. A file-only row re-upserted with its own hash does not
    # match and keeps the plain re-sync.
    if data.get("file_sha256") is not None and not any(
            data.get(k) is not None for k in ("strava_id", "rwgps_id", "intervals_icu_id")):
        with conn.cursor() as cur:
            cur.execute("""
                SELECT id FROM activities
                WHERE file_sha256 = %s
                  AND (strava_id IS NOT NULL OR rwgps_id IS NOT NULL
                       OR intervals_icu_id IS NOT NULL)
            """, (data["file_sha256"],))
            row = cur.fetchone()
        if row:
            return row[0], True

    # Duplicate detection: check if another activity started within 5 min with similar duration
    if data.get("date") and data.get("duration_s"):
        duplicate = find_duplicate(conn, data["date"], data["duration_s"], data.get("distance_m", 0))
        if duplicate and not _is_same_source_activity(duplicate, data):
            ex_id = duplicate[0]
            holder = _source_id_on_another_row(conn, ex_id, data)
            if holder:
                print(f"  [dedup] {data['name']} matched activity {ex_id} but its source "
                      f"id is on activity {holder} — re-syncing {holder}, keeping both")
                return _upsert_row(conn, data, streams, now)
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
                            file_sha256  = COALESCE(file_sha256, %(file_sha256)s),
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
                        "file_sha256":  data.get("file_sha256"),
                        "intervals_icu_id": data.get("intervals_icu_id"),
                        "intervals_icu_analyzed": data.get("intervals_icu_analyzed"),
                        "synced_at":    now,
                        "ex_id":        ex_id,
                    })
                return ex_id, True
            else:
                print(f"  [dedup] Merging {data['name']} with existing activity {ex_id}")
                _merge_in_place(conn, ex_id, merged, streams, now)
                return ex_id, bool(streams)

    return _upsert_row(conn, data, streams, now)


def fill_missing_speed(streams: list) -> list:
    """Derive speed_kmh from GPS wherever a point has none, in place.

    Ride with GPS phone recordings carry position but no speed. Speed is the straight
    line back to the fix SPEED_WINDOW_S earlier, because a single step is mostly GPS
    noise and would decide Max Speed. Recorded speed is never overwritten. A point
    without a full window behind it, or reading above MAX_DERIVED_SPEED_KMH, stays empty.

    It runs inside the stream write, and missing speed must never be what fails one,
    so a point it can't read is skipped and left for the database to judge.
    """
    fixes = []
    first = 0
    for point in streams:
        lat, lng, offset = point.get("lat"), point.get("lng"), point.get("time_offset")
        if not all(isinstance(value, (int, float)) for value in (lat, lng, offset)):
            continue
        if fixes and offset <= fixes[-1][0]:
            continue
        fixes.append((offset, lat, lng))
        if point.get("speed_kmh") is not None or len(fixes) < 2:
            continue
        while offset - fixes[first + 1][0] >= SPEED_WINDOW_S:
            first += 1
        from_offset, from_lat, from_lng = fixes[first]
        if offset - from_offset < SPEED_WINDOW_S:
            continue
        kmh = haversine_m(from_lat, from_lng, lat, lng) / (offset - from_offset) * 3.6
        if kmh <= MAX_DERIVED_SPEED_KMH:
            point["speed_kmh"] = round(kmh, 2)
    return streams


def backfill_missing_speed(conn) -> int:
    """Derive speed for stored rides with GPS and no speed at all. Returns how many.

    A ride with any recorded speed is left as it was stored. One whose fixes give no
    usable speed is looked at again on the next start, which costs one read.
    """
    with conn.cursor() as cur:
        cur.execute("""SELECT activity_id FROM activity_streams GROUP BY activity_id
                       HAVING COUNT(lat) > 0 AND COUNT(speed_kmh) = 0""")
        activity_ids = [row[0] for row in cur.fetchall()]

    filled = 0
    for activity_id in activity_ids:
        with conn.cursor() as cur:
            cur.execute("""SELECT id, time_offset, lat, lng FROM activity_streams
                           WHERE activity_id = %s ORDER BY time_offset""", (activity_id,))
            points = [{"id": row[0], "time_offset": row[1], "lat": row[2], "lng": row[3],
                       "speed_kmh": None} for row in cur.fetchall()]
            derived = [(p["id"], p["speed_kmh"]) for p in fill_missing_speed(points)
                       if p["speed_kmh"] is not None]
            if derived:
                psycopg2.extras.execute_values(
                    cur,
                    """UPDATE activity_streams AS s SET speed_kmh = v.speed
                       FROM (VALUES %s) AS v(id, speed) WHERE s.id = v.id""",
                    derived,
                    template="(%s::integer, %s::double precision)",
                )
                filled += 1
    return filled


def _replace_streams(cur, activity_id: int, streams: list) -> None:
    """DELETE then INSERT an activity's streams, in the caller's transaction.

    The W' stored on the ride describes the stream going away, and if the new one
    has no power nothing will ever come back to replace it. Segment efforts were
    timed on it too, so the ride is matched again.
    """
    cur.execute("DELETE FROM activity_streams WHERE activity_id = %s", (activity_id,))
    cur.execute("DELETE FROM climb_efforts WHERE activity_id = %s", (activity_id,))
    cur.execute("DELETE FROM climb_segment_scans WHERE activity_id = %s", (activity_id,))
    cur.execute("UPDATE activities SET wbal_cp = NULL, wbal_w_prime_j = NULL WHERE id = %s",
                (activity_id,))
    if streams:
        fill_missing_speed(streams)
        psycopg2.extras.execute_values(
            cur,
            """INSERT INTO activity_streams
                (activity_id, time_offset, hr, power, cadence, speed_kmh, altitude_m, lat, lng)
                VALUES %s""",
            [(activity_id, s.get("time_offset"), s.get("hr"), s.get("power"),
              s.get("cadence"), s.get("speed_kmh"), s.get("altitude_m"),
              s.get("lat"), s.get("lng")) for s in streams]
        )


def upsert_streams(conn, activity_id: int, streams: list):
    """Replace streams for an activity. Wrapped in a transaction so a crash
    between DELETE and INSERT doesn't leave the activity with no streams.
    """
    conn.autocommit = False
    try:
        with conn.cursor() as cur:
            _replace_streams(cur, activity_id, streams)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.autocommit = True


def stream_channels(streams: list) -> set:
    """The channels a list of stream points carries.

    A zero is a reading, so presence means "not None". GPS needs both lat and
    lng on one point. Speed counts when GPS is present, because the stream write
    derives it from the fixes.
    """
    found = set()
    for key, channel in (("hr", "hr"), ("power", "power"), ("cadence", "cadence"),
                         ("altitude_m", "altitude"), ("speed_kmh", "speed")):
        if any(s.get(key) is not None for s in streams):
            found.add(channel)
    if any(s.get("lat") is not None and s.get("lng") is not None for s in streams):
        found.update(("gps", "speed"))
    return found


def replace_streams_if_richer(conn, activity_id: int, streams: list) -> bool:
    """Swap in a file's streams when they carry every stored channel and more.

    A matched cloud ride keeps its streams, except that a file bringing a channel
    they lack entirely (power on a phone-recorded ride) replaces them. Only a
    strict superset swaps, so no channel is ever dropped and no timestamps need
    aligning. Returns True when it swapped.
    """
    with conn.cursor() as cur:
        cur.execute("""
            SELECT bool_or(hr IS NOT NULL), bool_or(power IS NOT NULL),
                   bool_or(cadence IS NOT NULL), bool_or(altitude_m IS NOT NULL),
                   bool_or(lat IS NOT NULL AND lng IS NOT NULL),
                   bool_or(speed_kmh IS NOT NULL)
            FROM activity_streams WHERE activity_id = %s
        """, (activity_id,))
        flags = cur.fetchone()
    stored = {name for name, flag in zip(
        ("hr", "power", "cadence", "altitude", "gps", "speed"), flags) if flag}
    if "gps" in stored:
        stored.add("speed")
    if not stream_channels(streams) > stored:
        return False
    conn.autocommit = False
    try:
        with conn.cursor() as cur:
            _clear_stream_derived(cur, activity_id)
            _replace_streams(cur, activity_id, streams)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.autocommit = True
    return True


def handle_rwgps_deletion(conn, rwgps_id: int) -> str:
    """Process a 'deleted' sync item from RWGPS.

    Returns "deleted" (RWGPS-only activity removed — CASCADE wipes streams,
    intervals, climbs), "unlinked" (dual-source activity kept, rwgps_id
    cleared because the ride still exists on Strava or in a file), or "not_found".
    """
    with conn.cursor() as cur:
        cur.execute("SELECT id, strava_id, file_sha256 FROM activities "
            "WHERE rwgps_id = %s", (rwgps_id,))
        row = cur.fetchone()
        if not row:
            return "not_found"
        act_id, strava_id, file_sha256 = row
        if strava_id is not None or file_sha256 is not None:
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
        cur.execute("SELECT id, strava_id, rwgps_id, file_sha256 FROM activities "
                    "WHERE intervals_icu_id = %s", (intervals_icu_id,))
        row = cur.fetchone()
        if not row:
            return "not_found"
        act_id, strava_id, rwgps_id, file_sha256 = row
        if strava_id is not None or rwgps_id is not None or file_sha256 is not None:
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


def has_activities(conn) -> bool:
    """True when any ride is stored, from any source."""
    with conn.cursor() as cur:
        cur.execute("SELECT EXISTS (SELECT 1 FROM activities)")
        return cur.fetchone()[0]


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


def count_intervals_icu_rides_since(conn, date: str) -> int:
    """How many stored rides carry an intervals.icu id and start on or after `date`."""
    with conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM activities "
                    "WHERE intervals_icu_id IS NOT NULL AND date >= %s::date", (date,))
        return cur.fetchone()[0]


def set_sync_state(conn, key: str, value: str):
    """Set a sync state key/value."""
    now = datetime.now(timezone.utc)
    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO sync_state (key, value, last_synced_at)
            VALUES (%s, %s, %s)
            ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, last_synced_at = EXCLUDED.last_synced_at
        """, (key, value, now))
