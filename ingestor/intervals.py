"""Auto interval detection from a per-second power stream.

Detects sustained efforts above a threshold and classifies them by
duration and intensity using Coggan-style buckets:

  - sprint:     < 30s  at > 150% FTP
  - anaerobic:  30s–2min at 120–150% FTP
  - vo2:        2–5 min at 105–120% FTP
  - threshold:  5–20 min at 95–105% FTP
  - sweetspot:  15–60 min at 83–94% FTP
  - tempo:      > 20 min at 75–85% FTP

The detector walks the power stream, finds contiguous regions where power
exceeds threshold_pct × FTP for at least min_duration_s seconds, and merges
sub-gap_tolerance_s gaps so brief dips (bad samples, quick coasts) don't
split a single effort.

Note on boundary semantics: the classifier buckets above are not a full
partition of the (duration, %FTP) plane — there are narrow dead zones at
the strict edges (e.g. exactly 120s at exactly 150% FTP falls between
anaerobic and vo2). Inputs that land in a dead zone return None and are
filtered out by the detector. This is acceptable for real-world rides
because the threshold-based detection rarely produces segments that hit
those exact boundaries, but worth knowing when comparing hand-crafted
workouts against the output.

Note on avg_hr: the interval dicts emitted by detect_intervals do not
include avg_hr. That field is computed by the caller from the parallel
HR stream slice when persisting to the ride_intervals table — keeps this
module pure and decoupled from HR stream handling.
"""

from __future__ import annotations


def classify_interval(duration_s: int, avg_power: float, ftp: float) -> str | None:
    """Classify an interval by duration and % of FTP. Returns a bucket name or None.

    ftp may be int or float (per-ride FTP is stored as FLOAT in the schema).
    """
    if ftp <= 0 or avg_power <= 0 or duration_s <= 0:
        return None
    pct = avg_power / ftp

    # Sprint: < 30s at > 150% FTP
    if duration_s < 30 and pct > 1.50:
        return "sprint"
    # Anaerobic: 30s – under 2 min at 120–150% FTP
    if 30 <= duration_s < 120 and 1.20 <= pct <= 1.50:
        return "anaerobic"
    # VO2max: 2–5 min at 105–120% FTP (inclusive of the 2 min boundary)
    if 120 <= duration_s <= 300 and 1.05 <= pct <= 1.20:
        return "vo2"
    # Threshold: 5–20 min at 95–105% FTP
    if 300 < duration_s <= 1200 and 0.95 <= pct <= 1.05:
        return "threshold"
    # Sweet spot: 15–60 min at 83–94% FTP
    if 900 <= duration_s <= 3600 and 0.83 <= pct <= 0.94:
        return "sweetspot"
    # Tempo: > 20 min at 75–85% FTP
    if duration_s > 1200 and 0.75 <= pct <= 0.85:
        return "tempo"
    return None


def detect_intervals(
    power_samples: list,
    ftp: float,
    threshold_pct: float = 0.85,
    min_duration_s: int = 30,
    gap_tolerance_s: int = 10,
) -> list[dict]:
    """Walk a 1-Hz power stream and return a list of detected intervals.

    Each interval is a dict:
        {
            "start_offset_s": int,
            "duration_s": int,
            "avg_power": float,
            "np": float,
            "max_power": float,
            "classification": str,
        }

    Args:
        power_samples: list of per-second power values (may contain None)
        ftp: rider's FTP in watts
        threshold_pct: fraction of FTP above which a sample counts as "in-interval"
        min_duration_s: minimum duration for a region to be reported as an interval
        gap_tolerance_s: consecutive below-threshold samples shorter than this are
            bridged (do not split an interval)
    """
    if not power_samples or ftp <= 0:
        return []

    threshold = ftp * threshold_pct
    # Replace None with 0 for detection math
    samples = [p if p is not None else 0 for p in power_samples]

    intervals: list[dict] = []
    in_interval = False
    start = 0
    below_run = 0  # consecutive below-threshold samples while inside an interval

    def close(effective_end: int):
        """Close the current interval at effective_end (exclusive). Trailing below-threshold samples already trimmed by caller."""
        nonlocal in_interval
        duration = effective_end - start
        if duration >= min_duration_s:
            segment = samples[start:effective_end]
            if segment:
                avg_p = sum(segment) / len(segment)
                max_p = max(segment)
                # Simple NP approximation: 30s SMA → 4th power → mean → 4th root
                # For short segments (< 30s) fall back to avg_p
                if len(segment) >= 30:
                    window = 30
                    buf = [0.0] * window
                    idx = 0
                    rolling_sum = 0.0
                    total = 0.0
                    for w in segment:
                        rolling_sum += w - buf[idx]
                        buf[idx] = w
                        idx = (idx + 1) % window
                        total += (rolling_sum / window) ** 4
                    np_val = (total / len(segment)) ** 0.25
                else:
                    np_val = avg_p
                classification = classify_interval(duration, avg_p, ftp)
                if classification is not None:
                    intervals.append({
                        "start_offset_s": start,
                        "duration_s": duration,
                        "avg_power": round(avg_p, 1),
                        "np": round(np_val, 1),
                        "max_power": float(max_p),
                        "classification": classification,
                    })
        in_interval = False

    for i, watts in enumerate(samples):
        if watts >= threshold:
            if not in_interval:
                in_interval = True
                start = i
                below_run = 0
            else:
                below_run = 0
        else:
            if in_interval:
                below_run += 1
                if below_run > gap_tolerance_s:
                    # The run of bad samples occupies indices (i - below_run + 1) .. i.
                    # Last good sample index = i - below_run. Exclusive slice end = i - below_run + 1.
                    close(i - below_run + 1)

    if in_interval:
        # End of stream: trim any trailing below-threshold samples inside the interval.
        close(len(samples) - below_run)

    return intervals
