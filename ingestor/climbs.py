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


def _rdp_simplify(
    points: list[tuple[float, float]], epsilon: float
) -> list[tuple[float, float, int]]:
    """Ramer-Douglas-Peucker line simplification.

    Args:
        points: list of (x, y) points defining the curve.
        epsilon: maximum perpendicular distance for a point to be
            considered insignificant and removed.

    Returns:
        Simplified list of (x, y, original_index) tuples preserving
        the significant inflection points.
    """
    if len(points) <= 2:
        return [(x, y, i) for i, (x, y) in enumerate(points)]

    # Find the point farthest from the line connecting start and end
    start_x, start_y = points[0]
    end_x, end_y = points[-1]

    dx = end_x - start_x
    dy = end_y - start_y
    line_len = math.sqrt(dx * dx + dy * dy)

    max_dist = 0.0
    max_idx = 0

    for i in range(1, len(points) - 1):
        px, py = points[i]
        if line_len > 0:
            # Perpendicular distance from point to line
            dist = abs(dy * px - dx * py + end_x * start_y - end_y * start_x) / line_len
        else:
            dist = math.sqrt((px - start_x) ** 2 + (py - start_y) ** 2)

        if dist > max_dist:
            max_dist = dist
            max_idx = i

    if max_dist > epsilon:
        # Recursively simplify each half
        left = _rdp_simplify(points[:max_idx + 1], epsilon)
        right = _rdp_simplify(points[max_idx:], epsilon)
        # Adjust right indices (they're relative to the sub-array)
        offset = max_idx
        right_adjusted = [(x, y, idx + offset) for x, y, idx in right]
        # Merge (avoid duplicating the split point)
        return left + right_adjusted[1:]
    else:
        # All intermediate points are insignificant
        return [
            (points[0][0], points[0][1], 0),
            (points[-1][0], points[-1][1], len(points) - 1),
        ]


def _rdp_with_original_indices(
    points: list[tuple[float, float]], epsilon: float
) -> list[tuple[float, float, int]]:
    """RDP that tracks original indices through recursion."""
    indexed = [(x, y, i) for i, (x, y) in enumerate(points)]
    return _rdp_indexed(indexed, epsilon)


def _rdp_indexed(
    points: list[tuple[float, float, int]], epsilon: float
) -> list[tuple[float, float, int]]:
    """RDP on indexed points, preserving original indices."""
    if len(points) <= 2:
        return list(points)

    start_x, start_y, _ = points[0]
    end_x, end_y, _ = points[-1]

    dx = end_x - start_x
    dy = end_y - start_y
    line_len = math.sqrt(dx * dx + dy * dy)

    max_dist = 0.0
    max_idx = 0

    for i in range(1, len(points) - 1):
        px, py, _ = points[i]
        if line_len > 0:
            dist = abs(dy * px - dx * py + end_x * start_y - end_y * start_x) / line_len
        else:
            dist = math.sqrt((px - start_x) ** 2 + (py - start_y) ** 2)

        if dist > max_dist:
            max_dist = dist
            max_idx = i

    if max_dist > epsilon:
        left = _rdp_indexed(points[:max_idx + 1], epsilon)
        right = _rdp_indexed(points[max_idx:], epsilon)
        return left + right[1:]
    else:
        return [points[0], points[-1]]


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
    epsilon: float = 10.0,
    min_distance_m: float = 200.0,
    min_gradient: float = 2.0,
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
