"""Critical Power (CP) and W' modeling — pure-function module.

Implements the Monod-Scherrer 2-parameter hyperbolic model:
    P = W'/t + CP

This is linear in (1/t, P), so numpy.polyfit handles the fit without
needing scipy. See docs/superpowers/specs/2026-04-11-cp-w-prime-foundation-design.md
for the full design rationale and quality gating logic.
"""

from __future__ import annotations

import math

import numpy as np


def compute_mean_maximal_power(
    stream_powers: list[float], duration_s: int
) -> float | None:
    """Best mean power over a sliding window of duration_s seconds.

    Returns the highest rolling average across the entire stream.
    Returns None when the stream is shorter than the requested window.
    """
    if not stream_powers or duration_s <= 0:
        return None
    if len(stream_powers) < duration_s:
        return None

    arr = np.array(stream_powers, dtype=float)
    cumsum = np.concatenate(([0.0], np.cumsum(arr)))
    window_sums = cumsum[duration_s:] - cumsum[:-duration_s]
    window_means = window_sums / duration_s
    return float(window_means.max())


def fit_monod_scherrer(
    efforts: list[tuple[int, float]],
) -> tuple[float | None, float | None, float | None]:
    """Fit P = W'/t + CP via linear regression on (1/t, P) points.

    Returns (cp_watts, w_prime_kj, r_squared) on success.

    Returns (None, None, None) when:
    - Fewer than 2 efforts provided (degenerate, can't fit a line)
    - Fit produces a physiologically impossible result (CP <= 0 or W' <= 0).
      numpy.polyfit on degenerate or near-collinear input can yield negative
      intercept/slope with high R²; rejecting these prevents nonsense values
      from leaking into sync_state.
    """
    if len(efforts) < 2:
        return (None, None, None)

    durations = np.array([d for d, _ in efforts], dtype=float)
    powers = np.array([p for _, p in efforts], dtype=float)
    x = 1.0 / durations

    slope, intercept = np.polyfit(x, powers, 1)
    cp_watts = float(intercept)
    w_prime_joules = float(slope)

    if cp_watts <= 0 or w_prime_joules <= 0:
        return (None, None, None)

    predicted = slope * x + intercept
    ss_res = float(np.sum((powers - predicted) ** 2))
    ss_tot = float(np.sum((powers - powers.mean()) ** 2))
    r_squared = 1.0 - (ss_res / ss_tot) if ss_tot > 0 else 0.0

    w_prime_kj = w_prime_joules / 1000.0
    return (cp_watts, w_prime_kj, r_squared)


def assess_fit_quality(
    r_squared: float | None, duration_count: int
) -> bool:
    """Quality gate for CP fits.

    Returns True iff:
    - r_squared is not None
    - r_squared >= 0.9
    - duration_count >= 4 (at least 4 of 5 standard duration buckets contributed)

    The 4-of-5 threshold allows for one missing bucket (e.g., a rider who
    never holds 20-min max efforts) without rejecting the fit entirely.
    """
    if r_squared is None:
        return False
    return r_squared >= 0.9 and duration_count >= 4


def compute_wbal(
    powers: list[float], cp: float, w_prime_j: float
) -> list[float]:
    """Compute per-second W'bal using Skiba differential model.

    Uses the GoldenCheetah tau formulation:
        tau = 546 * exp(-0.01 * (CP - P)) + 316

    Args:
        powers: per-second power values (watts).
        cp: Critical Power (watts).
        w_prime_j: W' in joules (NOT kJ).

    Returns:
        list of W'bal values (joules), same length as powers.
        W'bal starts at w_prime_j and is clamped to [0, w_prime_j].
    """
    if not powers:
        return []

    wbal = []
    current = w_prime_j

    for p in powers:
        if p > cp:
            current = current - (p - cp)
        else:
            tau = 546.0 * math.exp(-0.01 * (cp - p)) + 316.0
            current = w_prime_j - (w_prime_j - current) * math.exp(-1.0 / tau)

        current = max(0.0, min(current, w_prime_j))
        wbal.append(current)

    return wbal
