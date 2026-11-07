"""Climb segments — recurring climbs matched across rides, Strava-style.

Detected climbs (ride_climbs) are per-ride and their RDP boundaries drift from
ride to ride, so the same road climb is reported as 1.2 km on one ride and
1.8 km on another — not comparable. This module turns them into fixed
segments and times every ride over the same start/end points:

1. **Discovery** — detected climbs whose summits (END_CLUSTER_RADIUS_M) and
   starts (START_CLUSTER_RADIUS_M) roughly coincide on at least MIN_RIDES
   rides form a cluster. The segment's start/end are the member climb points
   that the most member rides actually pass (tie → the longest climb), so the
   segment is the stretch of road those rides share.
2. **Matching** — any ride passing within PASS_RADIUS_M of the start and then
   of the end, over a path no longer than MAX_PATH_RATIO × the segment, is an
   effort. Matching runs on GPS, not on detected climbs, so it also catches
   rides where detection split or missed the climb, and works across routes.

Segments are persistent: once created they keep their id (and any name the
user gave them) and are never moved; discovery only adds segments for climbs
no existing segment covers. Each (segment, ride) pair is scanned once
(climb_segment_scans); replacing a ride's streams clears its scans so it is
re-matched.
"""

from __future__ import annotations

from climbs import classify_climb
from route_match import haversine_m

MIN_CLIMB_GAIN_M = 40          # detected climbs smaller than this don't seed segments
MIN_CLIMB_LENGTH_M = 500
START_CLUSTER_RADIUS_M = 600   # climb starts drift a lot (RDP picks different valley points)
END_CLUSTER_RADIUS_M = 150     # summits are stable
MIN_RIDES = 2                  # distinct rides needed to create a segment
PASS_RADIUS_M = 40             # how close a ride must pass the start/end point
MAX_PATH_RATIO = 1.3           # effort path vs segment length — rejects detours
MOVING_SPEED_KMH = 2.0         # below this the rider is stopped (moving time)


# ── Matching ────────────────────────────────────────────────────────────────

def _passes(track: list, lat: float, lng: float, radius_m: float) -> list[int]:
    """Indices of closest approach for each separate pass within radius_m."""
    passes = []
    best_i, best_d = None, None
    for i, pt in enumerate(track):
        d = haversine_m(pt["lat"], pt["lng"], lat, lng)
        if d <= radius_m:
            if best_d is None or d < best_d:
                best_i, best_d = i, d
        elif best_i is not None:
            passes.append(best_i)
            best_i, best_d = None, None
    if best_i is not None:
        passes.append(best_i)
    return passes


def find_efforts(track: list, start: tuple, end: tuple,
                 max_path_m: float | None = None,
                 radius_m: float = PASS_RADIUS_M) -> list[dict]:
    """Every traversal of start → end in a ride.

    track: GPS points ordered by time, each a dict with time_offset, lat, lng
    and optionally altitude_m, hr, power. Points without a fix must be
    filtered out by the caller.

    A traversal runs from the last start pass before an end pass, so circling
    at the bottom doesn't count against the effort; one ride can yield several
    (repeats/laps). Traversals longer than max_path_m are detours, not efforts.
    """
    events = sorted([(i, "s") for i in _passes(track, *start, radius_m)]
                    + [(i, "e") for i in _passes(track, *end, radius_m)])
    efforts = []
    last_start = None
    for idx, kind in events:
        if kind == "s":
            last_start = idx
            continue
        if last_start is None or idx <= last_start:
            continue
        effort = _effort_metrics(track, last_start, idx)
        if max_path_m is None or effort["path_m"] <= max_path_m:
            efforts.append(effort)
        last_start = None
    return efforts


def _effort_metrics(track: list, s: int, e: int) -> dict:
    path_m = 0.0
    moving_s = 0
    hr_sum = hr_t = pw_sum = pw_t = 0
    for prev, pt in zip(track[s:e], track[s + 1:e + 1]):
        dt = pt["time_offset"] - prev["time_offset"]
        if dt <= 0:
            continue
        d = haversine_m(prev["lat"], prev["lng"], pt["lat"], pt["lng"])
        path_m += d
        if d / dt * 3.6 >= MOVING_SPEED_KMH:
            moving_s += dt
        if pt.get("hr"):
            hr_sum += pt["hr"] * dt
            hr_t += dt
        if pt.get("power") is not None:
            pw_sum += pt["power"] * dt
            pw_t += dt
    elapsed_s = track[e]["time_offset"] - track[s]["time_offset"]
    alt_s, alt_e = track[s].get("altitude_m"), track[e].get("altitude_m")
    gain_m = (alt_e - alt_s) if alt_s is not None and alt_e is not None else None
    return {
        "start_idx": s,
        "end_idx": e,
        "start_offset": track[s]["time_offset"],
        "end_offset": track[e]["time_offset"],
        "elapsed_s": elapsed_s,
        "moving_s": moving_s,
        "path_m": path_m,
        "gain_m": gain_m,
        "start_alt": alt_s,
        "avg_speed_kmh": round(path_m / elapsed_s * 3.6, 2) if elapsed_s > 0 else None,
        "vam_mh": round(gain_m / elapsed_s * 3600) if elapsed_s > 0 and gain_m is not None else None,
        "avg_hr": round(hr_sum / hr_t) if hr_t else None,
        "avg_power": round(pw_sum / pw_t) if pw_t else None,
    }


# ── Discovery ───────────────────────────────────────────────────────────────

def cluster_climbs(climbs: list) -> list[list]:
    """Greedy clustering of detected climbs on (start, end) proximity.

    climbs: dicts with activity_id, start (lat, lng), end (lat, lng),
    start_alt, peak_alt, gain_m. Biggest climbs seed clusters first. Only
    clusters spanning MIN_RIDES distinct rides are returned.
    """
    clusters = []
    for c in sorted(climbs, key=lambda c: -c["gain_m"]):
        for cl in clusters:
            seed = cl[0]
            if (haversine_m(*c["end"], *seed["end"]) <= END_CLUSTER_RADIUS_M
                    and haversine_m(*c["start"], *seed["start"]) <= START_CLUSTER_RADIUS_M):
                cl.append(c)
                break
        else:
            clusters.append([c])
    return [cl for cl in clusters if len({c["activity_id"] for c in cl}) >= MIN_RIDES]


def is_covered(seed: dict, segments: list) -> bool:
    """True if an existing segment already represents this cluster seed."""
    return any(
        haversine_m(*seed["end"], seg["end_lat"], seg["end_lng"]) <= END_CLUSTER_RADIUS_M
        and haversine_m(*seed["start"], seg["start_lat"], seg["start_lng"]) <= START_CLUSTER_RADIUS_M
        for seg in segments
    )


def build_segment(cluster: list, tracks: dict) -> dict | None:
    """Pick the consensus start/end for a cluster and validate it.

    tracks: activity_id → GPS track of every member ride. Returns the
    segment geometry measured on the shortest member effort, or None when
    fewer than MIN_RIDES rides actually ride the consensus stretch.
    """
    def best(points, prefer_low):
        scored = []
        for c, pt in points:
            n = sum(1 for t in tracks.values() if _passes(t, *pt, PASS_RADIUS_M))
            alt = c["start_alt"] if prefer_low else -c["peak_alt"]
            scored.append((-n, alt, pt))
        return min(scored)[2]

    start = best([(c, c["start"]) for c in cluster], prefer_low=True)
    end = best([(c, c["end"]) for c in cluster], prefer_low=False)
    if haversine_m(*start, *end) < PASS_RADIUS_M * 2:
        return None

    efforts = []
    for track in tracks.values():
        found = find_efforts(track, start, end)
        if found:
            efforts.append(min(found, key=lambda e: e["path_m"]))
    if len(efforts) < MIN_RIDES:
        return None

    ref = min(efforts, key=lambda e: e["path_m"])
    gains = sorted(e["gain_m"] for e in efforts if e["gain_m"] is not None)
    if not gains:
        return None
    gain = gains[len(gains) // 2]
    length = ref["path_m"]
    if gain < MIN_CLIMB_GAIN_M or length < MIN_CLIMB_LENGTH_M:
        return None
    grade = gain / length * 100
    start_alt = ref["start_alt"] if ref["start_alt"] is not None \
        else min(c["start_alt"] for c in cluster)
    return {
        "start_lat": start[0], "start_lng": start[1],
        "end_lat": end[0], "end_lng": end[1],
        "start_alt": round(start_alt),
        "end_alt": round(start_alt + gain),
        "length_m": round(length),
        "gain_m": round(gain),
        "avg_grade": round(grade, 1),
        "category": classify_climb(length, grade),
    }


def auto_name(seg: dict) -> str:
    return (f"{seg['start_alt']}→{seg['end_alt']} m · "
            f"{seg['length_m'] / 1000:.1f} km · {seg['avg_grade']:.1f}%")


# ── DB steps ────────────────────────────────────────────────────────────────

def _load_track(cur, activity_id: int) -> list:
    cur.execute("""
        SELECT time_offset, lat, lng, altitude_m, hr, power
        FROM activity_streams
        WHERE activity_id = %s AND lat IS NOT NULL AND lng IS NOT NULL
        ORDER BY time_offset
    """, (activity_id,))
    return [{"time_offset": r[0], "lat": r[1], "lng": r[2], "altitude_m": r[3],
             "hr": r[4], "power": r[5]} for r in cur.fetchall()]


def _load_segments(cur) -> list:
    cur.execute("""
        SELECT id, start_lat, start_lng, end_lat, end_lng, length_m
        FROM climb_segments
    """)
    return [{"id": r[0], "start_lat": r[1], "start_lng": r[2], "end_lat": r[3],
             "end_lng": r[4], "length_m": r[5]} for r in cur.fetchall()]


def discover_segments(conn) -> int:
    """Create segments for recurring detected climbs. Returns segments created."""
    with conn.cursor() as cur:
        cur.execute("""
            SELECT c.activity_id, c.start_alt, c.peak_alt, c.gain_m,
                   s0.lat, s0.lng, s1.lat, s1.lng
            FROM ride_climbs c
            JOIN LATERAL (
                SELECT lat, lng FROM activity_streams s
                WHERE s.activity_id = c.activity_id AND s.time_offset >= c.start_offset
                  AND s.lat IS NOT NULL
                ORDER BY s.time_offset LIMIT 1
            ) s0 ON TRUE
            JOIN LATERAL (
                SELECT lat, lng FROM activity_streams s
                WHERE s.activity_id = c.activity_id AND s.time_offset >= c.end_offset
                  AND s.lat IS NOT NULL
                ORDER BY s.time_offset LIMIT 1
            ) s1 ON TRUE
            WHERE c.source = 'detected'
              AND c.gain_m >= %s AND c.length_m >= %s
              AND c.start_alt IS NOT NULL AND c.peak_alt IS NOT NULL
        """, (MIN_CLIMB_GAIN_M, MIN_CLIMB_LENGTH_M))
        climbs = [{"activity_id": r[0], "start_alt": r[1], "peak_alt": r[2], "gain_m": r[3],
                   "start": (r[4], r[5]), "end": (r[6], r[7])} for r in cur.fetchall()]

        segments = _load_segments(cur)
        created = 0
        track_cache = {}
        for cluster in cluster_climbs(climbs):
            if is_covered(cluster[0], segments):
                continue
            tracks = {}
            for c in cluster:
                aid = c["activity_id"]
                if aid not in track_cache:
                    track_cache[aid] = _load_track(cur, aid)
                tracks[aid] = track_cache[aid]
            seg = build_segment(cluster, tracks)
            if seg is None or is_covered(
                    {"start": (seg["start_lat"], seg["start_lng"]),
                     "end": (seg["end_lat"], seg["end_lng"])}, segments):
                continue
            cur.execute("""
                INSERT INTO climb_segments
                    (auto_name, start_lat, start_lng, end_lat, end_lng, start_alt,
                     end_alt, length_m, gain_m, avg_grade, category)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING id
            """, (auto_name(seg), seg["start_lat"], seg["start_lng"], seg["end_lat"],
                  seg["end_lng"], seg["start_alt"], seg["end_alt"], seg["length_m"],
                  seg["gain_m"], seg["avg_grade"], seg["category"]))
            seg["id"] = cur.fetchone()[0]
            segments.append(seg)
            created += 1
    return created


def match_efforts(conn) -> int:
    """Time every not-yet-scanned ride on every segment. Returns efforts stored."""
    # ~1 km bounding-box margin for the cheap SQL prefilter
    margin = 0.01
    stored = 0
    with conn.cursor() as cur:
        segments = _load_segments(cur)
        for seg in segments:
            cur.execute("""
                SELECT a.id FROM activities a
                WHERE NOT EXISTS (
                    SELECT 1 FROM climb_segment_scans sc
                    WHERE sc.segment_id = %s AND sc.activity_id = a.id)
            """, (seg["id"],))
            unscanned = [r[0] for r in cur.fetchall()]
            if not unscanned:
                continue
            cur.execute("""
                SELECT DISTINCT activity_id FROM activity_streams
                WHERE activity_id = ANY(%s)
                  AND lat BETWEEN %s AND %s AND lng BETWEEN %s AND %s
            """, (unscanned, seg["start_lat"] - margin, seg["start_lat"] + margin,
                  seg["start_lng"] - margin, seg["start_lng"] + margin))
            nearby = [r[0] for r in cur.fetchall()]
            max_path = seg["length_m"] * MAX_PATH_RATIO
            for aid in nearby:
                track = _load_track(cur, aid)
                for e in find_efforts(track, (seg["start_lat"], seg["start_lng"]),
                                      (seg["end_lat"], seg["end_lng"]), max_path):
                    cur.execute("""
                        INSERT INTO climb_efforts
                            (segment_id, activity_id, start_offset, end_offset, elapsed_s,
                             moving_s, avg_speed_kmh, vam_mh, avg_hr, avg_power)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                        ON CONFLICT (segment_id, activity_id, start_offset) DO NOTHING
                    """, (seg["id"], aid, e["start_offset"], e["end_offset"], e["elapsed_s"],
                          e["moving_s"], e["avg_speed_kmh"], e["vam_mh"], e["avg_hr"],
                          e["avg_power"]))
                    stored += 1
            cur.execute("""
                INSERT INTO climb_segment_scans (segment_id, activity_id)
                SELECT %s, unnest(%s::int[])
                ON CONFLICT DO NOTHING
            """, (seg["id"], unscanned))
    return stored


# ── Management (run inside the ingestor container) ──────────────────────────
#   docker exec velomate-ingestor python3 segments.py list
#   docker exec velomate-ingestor python3 segments.py rename 4 "Serra climb"
#   docker exec velomate-ingestor python3 segments.py rename 4 ""   # back to auto name

def list_segments(conn) -> list:
    with conn.cursor() as cur:
        cur.execute("""
            SELECT s.id, COALESCE(s.name, s.auto_name), s.category,
                   COUNT(e.id), MIN(e.elapsed_s)
            FROM climb_segments s
            LEFT JOIN climb_efforts e ON e.segment_id = s.id
            GROUP BY s.id ORDER BY s.id
        """)
        return cur.fetchall()


def rename_segment(conn, segment_id: int, name: str) -> bool:
    """Set a segment's display name; an empty name restores the auto name."""
    with conn.cursor() as cur:
        cur.execute("UPDATE climb_segments SET name = %s WHERE id = %s",
                    (name.strip() or None, segment_id))
        return cur.rowcount == 1


def main(argv=None) -> int:
    import argparse
    from db import get_connection

    parser = argparse.ArgumentParser(prog="segments.py", description="Manage climb segments")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("list", help="List climb segments")
    rename = sub.add_parser("rename", help="Rename a segment (empty name = auto name)")
    rename.add_argument("id", type=int)
    rename.add_argument("name")
    args = parser.parse_args(argv)

    conn = get_connection()
    conn.autocommit = True
    if args.command == "list":
        for seg_id, name, category, efforts, best in list_segments(conn):
            record = f"{best // 60}:{best % 60:02d}" if best is not None else "—"
            print(f"{seg_id:>4}  {name:<40} {category or '':<6} {efforts:>3} efforts  record {record}")
        return 0
    if not rename_segment(conn, args.id, args.name):
        print(f"No segment with id {args.id}")
        return 1
    print(f"Segment {args.id} renamed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
