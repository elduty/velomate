"""Auto interval detection from a per-second power stream.

Finds sustained efforts and classifies each by the %FTP band its mean power
falls in. The bands are contiguous and open at the top, so every effort at or
above the detection floor gets a class:

  - surge:      under 60 s, at any intensity
  - tempo:      60 s or more at 75–88% FTP
  - sweetspot:  60 s or more at 88–95% FTP
  - threshold:  60 s or more at 95–105% FTP
  - vo2:        60 s or more at 105–120% FTP
  - anaerobic:  60 s or more at >= 120% FTP

Each band includes its lower edge. Duration only decides surge versus band: a
short effort is a stop-and-go acceleration or a blip around the threshold
whatever its intensity, and a deliberate effort of a minute or more keeps its
band wherever it starts from.

There is no sprint class. The detector's 30 s minimum sits exactly where
neuromuscular work ends, so nothing it reports can be a sprint.

Detection runs in two passes so the 75% floor cannot fuse a ride. A region is
a stretch at or above a threshold, with dips of up to gap_tolerance_s bridged.
The first pass, at 85%, takes every region whose mean is harder than tempo out
of the ride. The second pass runs at the floor over what is left. Reps with
recoveries still above 75% therefore stay separate reps, with tempo between
them, instead of one long effort averaging out to something neither was.

A bridged region can clear the threshold sample by sample and still average
below the floor. That is not a sustained effort at any level: classify_interval
returns None and the detector leaves it out.

Note on avg_hr: the interval dicts emitted by detect_intervals do not
include avg_hr. That field is computed by the caller from the parallel
HR stream slice when persisting to the ride_intervals table — keeps this
module pure and decoupled from HR stream handling.
"""

from __future__ import annotations

SURGE_CEILING_S = 60

# Lower edge of each band as a fraction of FTP, hardest first.
BANDS = (
    (1.20, "anaerobic"),
    (1.05, "vo2"),
    (0.95, "threshold"),
    (0.88, "sweetspot"),
    (0.75, "tempo"),
)
INTERVAL_CLASSES = ("surge",) + tuple(name for _, name in BANDS)

DETECTION_FLOOR_PCT = BANDS[-1][0]
# First pass: regions at this level are kept apart when their mean is harder
# than tempo. Sitting between the two keeps a tempo ride that wanders across
# 85% in one piece.
CORE_THRESHOLD_PCT = 0.85
CORE_MIN_MEAN_PCT = BANDS[-2][0]


def classify_interval(duration_s: int, avg_power: float, ftp: float) -> str | None:
    """Return the class of an effort, or None if it is below the detection floor.

    ftp may be int or float (per-ride FTP is stored as FLOAT in the schema).
    Edges are compared as ftp * edge, the same product the detector thresholds
    samples with, so a mean exactly on an edge cannot round to the wrong side.
    """
    if ftp <= 0 or avg_power <= 0 or duration_s <= 0:
        return None
    if avg_power < ftp * DETECTION_FLOOR_PCT:
        return None
    if duration_s < SURGE_CEILING_S:
        return "surge"
    for lower_edge, name in BANDS:
        if avg_power >= ftp * lower_edge:
            return name
    return None


def _find_regions(samples: list, threshold: float, min_duration_s: int, gap_tolerance_s: int) -> list[tuple[int, int]]:
    """Return (start, end) slices at or above threshold, end exclusive.

    Runs of up to gap_tolerance_s below-threshold samples are bridged. Trailing
    below-threshold samples are never part of a region.
    """
    regions: list[tuple[int, int]] = []
    in_region = False
    start = 0
    below_run = 0  # consecutive below-threshold samples while inside a region

    def close(effective_end: int):
        nonlocal in_region
        if effective_end - start >= min_duration_s:
            regions.append((start, effective_end))
        in_region = False

    for i, watts in enumerate(samples):
        if watts >= threshold:
            if not in_region:
                in_region = True
                start = i
            below_run = 0
        elif in_region:
            below_run += 1
            if below_run > gap_tolerance_s:
                # The run of bad samples occupies indices (i - below_run + 1) .. i.
                # Last good sample index = i - below_run. Exclusive slice end = i - below_run + 1.
                close(i - below_run + 1)

    if in_region:
        # End of stream: trim any trailing below-threshold samples inside the region.
        close(len(samples) - below_run)

    return regions


def _summarise(samples: list, start: int, end: int, ftp: float) -> dict | None:
    """Build the interval dict for a region, or None if it has no class."""
    segment = samples[start:end]
    avg_p = sum(segment) / len(segment)
    classification = classify_interval(len(segment), avg_p, ftp)
    if classification is None:
        return None
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
    return {
        "start_offset_s": start,
        "duration_s": len(segment),
        "avg_power": round(avg_p, 1),
        "np": round(np_val, 1),
        "max_power": float(max(segment)),
        "classification": classification,
    }


def detect_intervals(
    power_samples: list,
    ftp: float,
    threshold_pct: float = DETECTION_FLOOR_PCT,
    min_duration_s: int = 30,
    gap_tolerance_s: int = 10,
) -> list[dict]:
    """Walk a 1-Hz power stream and return its intervals in stream order.

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
        threshold_pct: fraction of FTP at or above which a sample counts as
            "in-interval" in the second pass
        min_duration_s: minimum duration for a region to be reported as an interval
        gap_tolerance_s: consecutive below-threshold samples up to this many are
            bridged (do not split an interval)
    """
    if not power_samples or ftp <= 0:
        return []

    # Replace None with 0 for detection math
    samples = [p if p is not None else 0 for p in power_samples]

    harder_than_tempo = [
        (start, end)
        for start, end in _find_regions(samples, ftp * CORE_THRESHOLD_PCT, min_duration_s, gap_tolerance_s)
        if sum(samples[start:end]) / (end - start) >= ftp * CORE_MIN_MEAN_PCT
    ]

    regions = list(harder_than_tempo)
    cursor = 0
    for start, end in harder_than_tempo + [(len(samples), len(samples))]:
        between = _find_regions(samples[cursor:start], ftp * threshold_pct, min_duration_s, gap_tolerance_s)
        regions += [(cursor + s, cursor + e) for s, e in between]
        cursor = end

    summaries = (_summarise(samples, start, end, ftp) for start, end in sorted(regions))
    return [interval for interval in summaries if interval is not None]
