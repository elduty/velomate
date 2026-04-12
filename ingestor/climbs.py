"""Climb detection from GPS elevation profiles.

Walks the smoothed elevation profile to detect climbs, merging
segments separated by small dips. Uses the same 20-second smoothing
window as the Cadence & Grade panel for consistency.

Classification (standard cycling categories):
    Cat 4: 100-250m gain
    Cat 3: 250-500m gain
    Cat 2: 500-1000m gain
    Cat 1: 1000-1500m gain
    HC:    > 1500m gain

Minimum 30m gain and 3% average gradient to qualify.
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


def detect_climbs(
    altitudes: list[float],
    distances_m: list[float],
    min_gain: float = 30.0,
    min_gradient: float = 3.0,
    merge_tolerance: float = 30.0,
) -> list[dict]:
    """Detect climbs from a smoothed elevation profile.

    Walks the profile tracking local lows and highs. A climb starts
    when altitude rises from a local low. A climb ends when altitude
    drops more than `merge_tolerance` metres below the peak. Small dips
    (< merge_tolerance) mid-climb are absorbed — nearby segments merge
    into one climb.

    Args:
        altitudes: smoothed altitude values (metres), one per second.
        distances_m: cumulative distance in metres, same length as altitudes.
        min_gain: minimum elevation gain (metres) to qualify as a climb.
        min_gradient: minimum average gradient (%) to qualify.
        merge_tolerance: maximum descent (metres) before ending a climb.

    Returns:
        List of dicts, each with keys:
            start_idx, end_idx, gain_m, length_m, avg_grade,
            start_alt, peak_alt, duration_s, category
    """
    if len(altitudes) < 2 or len(distances_m) < 2:
        return []

    climbs = []
    n = len(altitudes)

    # State tracking
    in_climb = False
    climb_start_idx = 0
    local_low = altitudes[0]
    local_low_idx = 0
    peak = altitudes[0]
    peak_idx = 0

    for i in range(1, n):
        alt = altitudes[i]

        if not in_climb:
            # Looking for a climb to start
            if alt < local_low:
                local_low = alt
                local_low_idx = i
            elif alt - local_low >= 5.0:
                # Started climbing — 5m above the local low
                in_climb = True
                climb_start_idx = local_low_idx
                peak = alt
                peak_idx = i
        else:
            # In a climb — track the peak
            if alt > peak:
                peak = alt
                peak_idx = i

            # Check if we've descended enough to end the climb
            if peak - alt > merge_tolerance:
                # End the climb at the peak
                _maybe_add_climb(
                    climbs, altitudes, distances_m,
                    climb_start_idx, peak_idx,
                    min_gain, min_gradient,
                )
                # Reset — start looking for next climb from current position
                in_climb = False
                local_low = alt
                local_low_idx = i
                peak = alt
                peak_idx = i

    # Handle climb that extends to end of ride
    if in_climb:
        _maybe_add_climb(
            climbs, altitudes, distances_m,
            climb_start_idx, peak_idx,
            min_gain, min_gradient,
        )

    return climbs


def _maybe_add_climb(
    climbs: list[dict],
    altitudes: list[float],
    distances_m: list[float],
    start_idx: int,
    end_idx: int,
    min_gain: float,
    min_gradient: float,
) -> None:
    """Add a climb to the list if it meets the minimum gain and gradient."""
    gain = altitudes[end_idx] - altitudes[start_idx]
    if gain < min_gain:
        return

    length_m = distances_m[end_idx] - distances_m[start_idx]
    if length_m <= 0:
        return

    avg_grade = (gain / length_m) * 100
    if avg_grade < min_gradient:
        return

    duration_s = end_idx - start_idx

    if gain >= 1500:
        category = "HC"
    elif gain >= 1000:
        category = "Cat 1"
    elif gain >= 500:
        category = "Cat 2"
    elif gain >= 250:
        category = "Cat 3"
    elif gain >= 100:
        category = "Cat 4"
    else:
        category = "Climb"

    climbs.append({
        "start_idx": start_idx,
        "end_idx": end_idx,
        "gain_m": round(gain),
        "length_m": round(length_m),
        "avg_grade": round(avg_grade, 1),
        "start_alt": round(altitudes[start_idx]),
        "peak_alt": round(altitudes[end_idx]),
        "duration_s": duration_s,
        "category": category,
    })
