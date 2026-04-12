"""Climb detection from GPS elevation profiles.

Uses a GoldenCheetah-inspired state machine for detection with dynamic
downhill thresholds, and Strava's scoring formula for categorisation.

Detection: walks the smoothed elevation profile tracking local lows
and peaks. A climb starts when altitude rises consistently. A climb
ends when altitude drops more than 20% of the accumulated gain
(GoldenCheetah's approach — adapts naturally to climb size).

Categorisation (Strava formula):
    score = length_m × gradient_%
    Cat 4: 8000+, Cat 3: 16000+, Cat 2: 32000+, Cat 1: 64000+, HC: 80000+

Minimum 500m distance and 2% average gradient to qualify.

References:
- GoldenCheetah RideItem.cpp (climb detection state machine)
- Strava climb categories (length × gradient scoring)
"""

from __future__ import annotations


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
    min_distance_m: float = 500.0,
    min_gradient: float = 2.0,
    time_offsets: list[int] | None = None,
) -> list[dict]:
    """Detect climbs from a smoothed elevation profile.

    Uses a GoldenCheetah-inspired algorithm: tracks local lows and peaks.
    A climb ends when altitude drops more than 20% of the gain accumulated
    so far (dynamic threshold — adapts to climb size). This naturally
    handles rolling terrain: small dips mid-climb are absorbed, large
    descents split the climb.

    Args:
        altitudes: smoothed altitude values (metres), one per sample.
        distances_m: cumulative distance in metres, same length as altitudes.
        min_distance_m: minimum climb length (metres). Default 500m (GC standard).
        min_gradient: minimum average gradient (%) to qualify.
        time_offsets: actual time_offset values from the stream for duration.

    Returns:
        List of dicts with keys: start_idx, end_idx, gain_m, length_m,
        avg_grade, start_alt, peak_alt, duration_s, category, score
    """
    if len(altitudes) < 2 or len(distances_m) < 2:
        return []

    climbs = []
    n = len(altitudes)

    # State tracking
    in_climb = False
    climb_start_idx = 0
    low_point = altitudes[0]
    low_point_idx = 0
    peak = altitudes[0]
    peak_idx = 0

    for i in range(1, n):
        alt = altitudes[i]

        if not in_climb:
            # Track the lowest point
            if alt < low_point:
                low_point = alt
                low_point_idx = i
            # Start climbing when we've risen 5m above the low
            elif alt - low_point >= 5.0:
                in_climb = True
                climb_start_idx = low_point_idx
                peak = alt
                peak_idx = i
        else:
            # Track the peak
            if alt >= peak:
                peak = alt
                peak_idx = i

            # GoldenCheetah-style dynamic threshold:
            # End the climb when we've descended more than 20% of the
            # gain accumulated so far. This adapts naturally:
            # - 100m climb tolerates 20m dips
            # - 30m climb tolerates 6m dips
            # - 15m climb tolerates 3m dips
            gain_so_far = peak - altitudes[climb_start_idx]
            descent_threshold = max(gain_so_far * 0.20, 3.0)  # at least 3m

            if peak - alt > descent_threshold:
                # End the climb at the peak
                _maybe_add_climb(
                    climbs, altitudes, distances_m,
                    climb_start_idx, peak_idx,
                    min_distance_m, min_gradient, time_offsets,
                )
                # Reset
                in_climb = False
                low_point = alt
                low_point_idx = i
                peak = alt
                peak_idx = i

    # Handle climb that extends to end of ride
    if in_climb:
        _maybe_add_climb(
            climbs, altitudes, distances_m,
            climb_start_idx, peak_idx,
            min_distance_m, min_gradient, time_offsets,
        )

    return climbs


def _maybe_add_climb(
    climbs: list[dict],
    altitudes: list[float],
    distances_m: list[float],
    start_idx: int,
    end_idx: int,
    min_distance_m: float,
    min_gradient: float,
    time_offsets: list[int] | None = None,
) -> None:
    """Add a climb if it meets minimum distance and gradient requirements.

    Trims flat sections from the start by walking forward until the
    gradient over the next 30 samples exceeds 1%.
    """
    # Trim flat start: advance until gradient is positive
    trimmed_start = start_idx
    lookahead = min(30, (end_idx - start_idx) // 4)
    if lookahead > 5:
        for i in range(start_idx, end_idx - lookahead):
            alt_ahead = altitudes[i + lookahead] - altitudes[i]
            dist_ahead = distances_m[i + lookahead] - distances_m[i]
            if dist_ahead > 0 and (alt_ahead / dist_ahead) * 100 >= 1.0:
                trimmed_start = i
                break

    gain = altitudes[end_idx] - altitudes[trimmed_start]
    if gain < 5.0:  # absolute minimum gain
        return

    length_m = distances_m[end_idx] - distances_m[trimmed_start]
    if length_m < min_distance_m:
        return

    avg_grade = (gain / length_m) * 100
    if avg_grade < min_gradient:
        return

    if time_offsets is not None:
        duration_s = time_offsets[end_idx] - time_offsets[trimmed_start]
    else:
        duration_s = end_idx - trimmed_start

    category = classify_climb(length_m, avg_grade)
    score = round(length_m * avg_grade)

    climbs.append({
        "start_idx": trimmed_start,
        "end_idx": end_idx,
        "gain_m": round(gain),
        "length_m": round(length_m),
        "avg_grade": round(avg_grade, 1),
        "start_alt": round(altitudes[trimmed_start]),
        "peak_alt": round(altitudes[end_idx]),
        "duration_s": duration_s,
        "category": category,
        "score": score,
    })
