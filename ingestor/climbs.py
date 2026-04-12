"""Climb detection from GPS elevation profiles.

Uses the Ramer-Douglas-Peucker (RDP) algorithm to simplify the
elevation profile, then identifies uphill segments between the
simplified inflection points. Strava scoring for categorisation.

This approach is mathematically grounded (RDP is a proven line
simplification algorithm) with ONE tuning parameter (epsilon)
instead of multiple hand-tuned thresholds.

References:
- ActivityLog2 climb analysis (RDP-based detection)
- Strava climb categories (length × gradient scoring)
"""

from __future__ import annotations

import math


def smooth_altitude(altitudes: list[float], window: int = 20) -> list[float]:
    """Simple moving average smoothing for altitude data.

    Args:
        altitudes: per-second altitude values (metres).
        window: smoothing window in seconds (default 20, matches Grade panel).

    Returns:
        Smoothed altitude list, same length as input.
    """
    if not altitudes or window <= 0:
        return list(altitudes)

    n = len(altitudes)
    result = []
    half = window // 2
    for i in range(n):
        lo = max(0, i - half)
        hi = min(n, i + half + 1)
        result.append(sum(altitudes[lo:hi]) / (hi - lo))
    return result



def _rdp_with_original_indices(
    points: list[tuple[float, float]], epsilon: float
) -> list[tuple[float, float, int]]:
    """RDP line simplification preserving original indices.

    Iterative implementation using an explicit stack to avoid
    RecursionError on long rides (>1000 points).
    """
    if len(points) <= 2:
        return [(x, y, i) for i, (x, y) in enumerate(points)]

    n = len(points)
    # Track which points to keep
    keep = [False] * n
    keep[0] = True
    keep[-1] = True

    # Explicit stack: each entry is (start_index, end_index)
    stack = [(0, n - 1)]

    while stack:
        start, end = stack.pop()
        if end - start <= 1:
            continue

        sx, sy = points[start]
        ex, ey = points[end]
        dx = ex - sx
        dy = ey - sy
        line_len = math.sqrt(dx * dx + dy * dy)

        max_dist = 0.0
        max_idx = start

        for i in range(start + 1, end):
            px, py = points[i]
            if line_len > 0:
                dist = abs(dy * px - dx * py + ex * sy - ey * sx) / line_len
            else:
                dist = math.sqrt((px - sx) ** 2 + (py - sy) ** 2)

            if dist > max_dist:
                max_dist = dist
                max_idx = i

        if max_dist > epsilon:
            keep[max_idx] = True
            stack.append((start, max_idx))
            stack.append((max_idx, end))

    return [(points[i][0], points[i][1], i) for i in range(n) if keep[i]]


def classify_climb(length_m: float, avg_grade: float) -> str:
    """Classify a climb using Strava's scoring formula.

    score = length_m × gradient_%
    """
    score = length_m * avg_grade
    if score >= 80000:
        return "HC"
    elif score >= 64000:
        return "Cat 1"
    elif score >= 32000:
        return "Cat 2"
    elif score >= 16000:
        return "Cat 3"
    elif score >= 8000:
        return "Cat 4"
    else:
        return "Climb"


def detect_climbs(
    altitudes: list[float],
    distances_m: list[float],
    min_distance_m: float = 200.0,
    min_gradient: float = 2.0,
    epsilon: float = 10.0,
    time_offsets: list[int] | None = None,
) -> list[dict]:
    """Detect climbs using RDP simplification of the elevation profile.

    1. Builds a (distance, altitude) curve from the smoothed data
    2. Applies RDP to find significant inflection points
    3. Walks the simplified curve: each uphill segment is a potential climb
    4. Merges consecutive uphill segments (small dips absorbed by RDP)
    5. Filters by minimum distance and gradient
    6. Classifies using Strava scoring

    Args:
        altitudes: smoothed altitude values (metres), one per sample.
        distances_m: cumulative distance in metres, same length as altitudes.
        epsilon: RDP sensitivity. Lower = more detail, higher = smoother.
            10m works well for urban/rolling terrain.
        min_distance_m: minimum climb length (metres). Default 200m.
        min_gradient: minimum average gradient (%) to qualify.
        time_offsets: actual time_offset values from the stream for duration.

    Returns:
        List of dicts with keys: start_idx, end_idx, gain_m, length_m,
        avg_grade, start_alt, peak_alt, duration_s, category, score
    """
    if len(altitudes) < 2 or len(distances_m) < 2:
        return []

    # Build the (distance, altitude) curve
    points = [(distances_m[i], altitudes[i]) for i in range(len(altitudes))]

    # Apply RDP to simplify
    simplified = _rdp_with_original_indices(points, epsilon)

    # Walk the simplified points: find uphill segments
    climbs = []
    i = 0
    while i < len(simplified) - 1:
        dist_i, alt_i, idx_i = simplified[i]
        dist_j, alt_j, idx_j = simplified[i + 1]

        if alt_j > alt_i:
            # Uphill segment — extend through consecutive uphills
            climb_start = i
            climb_end = i + 1
            while climb_end < len(simplified) - 1:
                _, alt_next, _ = simplified[climb_end + 1]
                _, alt_curr, _ = simplified[climb_end]
                if alt_next > alt_curr:
                    climb_end += 1
                else:
                    break

            # Compute climb metrics
            s_dist, s_alt, s_idx = simplified[climb_start]
            e_dist, e_alt, e_idx = simplified[climb_end]

            gain = e_alt - s_alt
            length = e_dist - s_dist

            if length >= min_distance_m and gain > 0:
                avg_grade = (gain / length) * 100
                if avg_grade >= min_gradient:
                    if time_offsets is not None:
                        duration_s = time_offsets[e_idx] - time_offsets[s_idx]
                    else:
                        duration_s = e_idx - s_idx

                    category = classify_climb(length, avg_grade)
                    score = round(length * avg_grade)

                    climbs.append({
                        "start_idx": s_idx,
                        "end_idx": e_idx,
                        "gain_m": round(gain),
                        "length_m": round(length),
                        "avg_grade": round(avg_grade, 1),
                        "start_alt": round(s_alt),
                        "peak_alt": round(e_alt),
                        "duration_s": duration_s,
                        "category": category,
                        "score": score,
                    })

            i = climb_end + 1
        else:
            i += 1

    return climbs
