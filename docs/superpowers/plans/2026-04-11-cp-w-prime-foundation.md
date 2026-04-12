# CP / W' Foundation — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add Critical Power (CP) and W' modeling as the new authoritative algorithmic FTP estimate, with graceful fallback to the existing rolling 20-min × 0.95 calculation when fit quality is poor.

**Architecture:** New pure-function module `ingestor/critical_power.py` for the math (Monod-Scherrer linear regression via numpy.polyfit, plus quality assessment). DB-touching helpers `fit_period` and `compute_cp_estimate` live in `ingestor/fitness.py` and call the existing `estimate_ftp()` as the fallback path. New `cp_estimates` table stores per-day estimates with both the chosen value and the rejected fallback for diagnostic comparison. Two new ATP panels (CP/W' Progression timeseries + Power-Duration Curve scatter).

**Tech Stack:** Python 3.11, numpy (already a dependency), psycopg2, PostgreSQL 15, Grafana 12.4. No new package dependencies.

**Spec:** `docs/superpowers/specs/2026-04-11-cp-w-prime-foundation-design.md`

---

## File Structure

| File | Action | Responsibility |
|---|---|---|
| `ingestor/critical_power.py` | **Create** | Pure functions: `compute_mean_maximal_power`, `fit_monod_scherrer`, `assess_fit_quality` |
| `ingestor/db.py` | Modify | Add `cp_estimates` table to schema DDL |
| `ingestor/fitness.py` | Modify | Add `fit_period` (DB helper), `compute_cp_estimate` (orchestrator), wire into `recalculate_fitness` |
| `tests/test_critical_power.py` | **Create** | Pure-function tests with synthetic data |
| `tests/test_fitness_recalc.py` | Modify | Update mock cursor sequence for the new compute_cp_estimate step |
| `grafana/dashboards/all-time-progression.json` | Modify | Add CP/W' Progression panel + Power-Duration Curve panel |
| `grafana/dashboards/overview.json` | Modify | Update `Est. FTP` panel description |
| `CLAUDE.md` | Modify | Add CP/W' to Metrics section + design decisions |
| `README.md` | Modify | Add CP/W' to the metrics list |

---

## Task 1: Schema — `cp_estimates` table

**Files:**
- Modify: `ingestor/db.py:111` (after the existing `aerobic_decoupling` ALTER, before `CREATE INDEX`)

- [ ] **Step 1: Add the table DDL**

Find this line in `ingestor/db.py`:
```python
            ALTER TABLE activities ADD COLUMN IF NOT EXISTS ride_weight FLOAT;
```

After it (and before `CREATE INDEX IF NOT EXISTS idx_activities_date`), add:

```python

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
```

CP-specific columns are nullable; for `source='20min_fallback'` rows, only `source` and `fallback_ftp` are populated.

- [ ] **Step 2: Commit**

```bash
git checkout -b feat/cp-w-prime-foundation
git add ingestor/db.py
git commit -m "feat(db): add cp_estimates table for CP/W' modeling"
```

---

## Task 2: Pure function — `compute_mean_maximal_power`

**Files:**
- Create: `ingestor/critical_power.py`
- Create: `tests/test_critical_power.py`

- [ ] **Step 1: Create the test file with the failing test**

Create `tests/test_critical_power.py`:

```python
"""Tests for ingestor/critical_power.py — pure-function module."""

import sys
from pathlib import Path

import pytest

_ingestor_dir = Path(__file__).resolve().parent.parent / "ingestor"
if str(_ingestor_dir) not in sys.path:
    sys.path.insert(0, str(_ingestor_dir))

from critical_power import (
    compute_mean_maximal_power,
    fit_monod_scherrer,
    assess_fit_quality,
)


class TestComputeMeanMaximalPower:
    def test_flat_stream_returns_flat_value(self):
        """A constant 200W stream should return 200W for any window."""
        stream = [200.0] * 600
        assert compute_mean_maximal_power(stream, 60) == 200.0
        assert compute_mean_maximal_power(stream, 300) == 200.0
        assert compute_mean_maximal_power(stream, 600) == 200.0

    def test_ramping_stream_returns_highest_window(self):
        """A ramp from 100→300W should return the average of the highest window."""
        stream = [float(p) for p in range(100, 400)]  # 100..399, 300 samples
        # Best 60s window is the last 60 samples: avg(340..399) = 369.5
        result = compute_mean_maximal_power(stream, 60)
        assert result == pytest.approx(369.5, abs=0.1)

    def test_stream_shorter_than_duration_returns_none(self):
        """If the stream is shorter than the window, return None."""
        stream = [200.0] * 30
        assert compute_mean_maximal_power(stream, 60) is None

    def test_empty_stream_returns_none(self):
        assert compute_mean_maximal_power([], 60) is None
```

- [ ] **Step 2: Run the test to confirm it fails**

Run: `python3 -m pytest tests/test_critical_power.py::TestComputeMeanMaximalPower -v`
Expected: `ModuleNotFoundError: No module named 'critical_power'`

- [ ] **Step 3: Create `ingestor/critical_power.py` with the minimal implementation**

```python
"""Critical Power (CP) and W' modeling — pure-function module.

Implements the Monod-Scherrer 2-parameter hyperbolic model:
    P = W'/t + CP

This is linear in (1/t, P), so numpy.polyfit handles the fit without
needing scipy. See docs/superpowers/specs/2026-04-11-cp-w-prime-foundation-design.md
for the full design rationale and quality gating logic.
"""

from __future__ import annotations

import numpy as np


def compute_mean_maximal_power(
    stream_powers: list[float], duration_s: int
) -> float | None:
    """Best mean power over a sliding window of `duration_s` seconds.

    Returns the highest rolling average across the entire stream.
    Returns None when the stream is shorter than the requested window.
    """
    if not stream_powers or duration_s <= 0:
        return None
    if len(stream_powers) < duration_s:
        return None

    arr = np.array(stream_powers, dtype=float)
    # Rolling mean via cumulative sum (faster than convolve for our sizes)
    cumsum = np.concatenate(([0.0], np.cumsum(arr)))
    window_sums = cumsum[duration_s:] - cumsum[:-duration_s]
    window_means = window_sums / duration_s
    return float(window_means.max())
```

- [ ] **Step 4: Run the test to confirm it passes**

Run: `python3 -m pytest tests/test_critical_power.py::TestComputeMeanMaximalPower -v`
Expected: 4 passed.

- [ ] **Step 5: Commit**

```bash
git add ingestor/critical_power.py tests/test_critical_power.py
git commit -m "feat(cp): pure function compute_mean_maximal_power"
```

---

## Task 3: Pure function — `fit_monod_scherrer` (with sanity check)

**Files:**
- Modify: `ingestor/critical_power.py`
- Modify: `tests/test_critical_power.py`

- [ ] **Step 1: Add failing tests for the fit function**

Append to `tests/test_critical_power.py`:

```python
class TestFitMonodScherrer:
    def test_recovers_known_parameters_from_clean_data(self):
        """Synthetic data generated from CP=200, W'=15kJ should round-trip."""
        cp_true = 200.0
        w_prime_true_j = 15000.0  # 15 kJ
        durations = [60, 120, 300, 600, 1200]
        # P(t) = W'/t + CP
        efforts = [(t, w_prime_true_j / t + cp_true) for t in durations]
        cp, w_prime_kj, r2 = fit_monod_scherrer(efforts)
        assert cp == pytest.approx(200.0, abs=0.5)
        assert w_prime_kj == pytest.approx(15.0, abs=0.1)
        assert r2 == pytest.approx(1.0, abs=0.001)

    def test_noisy_data_still_recovers_within_tolerance(self):
        """Add small Gaussian noise and check we still get a reasonable fit."""
        rng = np.random.default_rng(42)
        cp_true = 200.0
        w_prime_true_j = 15000.0
        durations = [60, 120, 300, 600, 1200]
        efforts = [
            (t, w_prime_true_j / t + cp_true + rng.normal(0, 5))
            for t in durations
        ]
        cp, w_prime_kj, r2 = fit_monod_scherrer(efforts)
        assert cp == pytest.approx(200.0, abs=20.0)
        assert w_prime_kj == pytest.approx(15.0, abs=5.0)
        assert r2 > 0.85

    def test_fewer_than_two_efforts_returns_none(self):
        assert fit_monod_scherrer([]) == (None, None, None)
        assert fit_monod_scherrer([(60, 250.0)]) == (None, None, None)

    def test_negative_cp_rejected_as_failed(self):
        """Degenerate data that fits to CP <= 0 must return None (sanity check).

        Constructed by reverse-engineering the line P = W'/t + CP with
        CP = -10 W and W' = 12 kJ:
            t=60s   -> P = 12000/60 - 10  = 190
            t=120s  -> P = 12000/120 - 10 = 90
            t=300s  -> P = 12000/300 - 10 = 30
            t=600s  -> P = 12000/600 - 10 = 10
        polyfit recovers (slope ~ 12000, intercept ~ -10), so cp_watts = -10
        and the sanity check rejects.
        """
        efforts = [(60, 190.0), (120, 90.0), (300, 30.0), (600, 10.0)]
        cp, w_prime_kj, r2 = fit_monod_scherrer(efforts)
        assert cp is None and w_prime_kj is None and r2 is None

    def test_negative_w_prime_rejected_as_failed(self):
        """Inverted relationship (P INCREASES with longer duration) → negative slope.

        Physically impossible: holding more power for longer than for shorter.
        polyfit on (1/t, P) gives a negative slope, so w_prime <= 0 and the
        sanity check rejects.
        """
        efforts = [(60, 100.0), (120, 200.0), (300, 400.0), (600, 700.0)]
        cp, w_prime_kj, r2 = fit_monod_scherrer(efforts)
        assert cp is None and w_prime_kj is None and r2 is None
```

You'll need numpy imported at the top of the test file. Add to imports:

```python
import numpy as np
```

- [ ] **Step 2: Run the tests to confirm they fail**

Run: `python3 -m pytest tests/test_critical_power.py::TestFitMonodScherrer -v`
Expected: 5 errors with `ImportError: cannot import name 'fit_monod_scherrer'`

- [ ] **Step 3: Implement `fit_monod_scherrer` with the sanity check**

Append to `ingestor/critical_power.py`:

```python
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
    x = 1.0 / durations  # independent variable for the linear form

    # polyfit(x, y, 1) returns [slope, intercept]
    slope, intercept = np.polyfit(x, powers, 1)
    cp_watts = float(intercept)
    w_prime_joules = float(slope)

    # Physiological sanity check — reject impossible results
    if cp_watts <= 0 or w_prime_joules <= 0:
        return (None, None, None)

    # R² = 1 - SS_res / SS_tot
    predicted = slope * x + intercept
    ss_res = float(np.sum((powers - predicted) ** 2))
    ss_tot = float(np.sum((powers - powers.mean()) ** 2))
    r_squared = 1.0 - (ss_res / ss_tot) if ss_tot > 0 else 0.0

    w_prime_kj = w_prime_joules / 1000.0
    return (cp_watts, w_prime_kj, r_squared)
```

- [ ] **Step 4: Run the tests to confirm they pass**

Run: `python3 -m pytest tests/test_critical_power.py::TestFitMonodScherrer -v`
Expected: 5 passed.

- [ ] **Step 5: Commit**

```bash
git add ingestor/critical_power.py tests/test_critical_power.py
git commit -m "feat(cp): fit_monod_scherrer with physiological sanity check"
```

---

## Task 4: Pure function — `assess_fit_quality`

**Files:**
- Modify: `ingestor/critical_power.py`
- Modify: `tests/test_critical_power.py`

- [ ] **Step 1: Add failing tests**

Append to `tests/test_critical_power.py`:

```python
class TestAssessFitQuality:
    def test_high_r_squared_and_enough_durations_passes(self):
        assert assess_fit_quality(0.95, 5) is True
        assert assess_fit_quality(0.90, 4) is True

    def test_r_squared_below_threshold_fails(self):
        assert assess_fit_quality(0.89, 5) is False
        assert assess_fit_quality(0.5, 5) is False

    def test_too_few_durations_fails(self):
        assert assess_fit_quality(0.95, 3) is False
        assert assess_fit_quality(0.95, 0) is False

    def test_none_r_squared_returns_false(self):
        """Defensive None handling — fit_monod_scherrer can return None r_squared."""
        assert assess_fit_quality(None, 5) is False
        assert assess_fit_quality(None, 0) is False
```

- [ ] **Step 2: Run the tests to confirm they fail**

Run: `python3 -m pytest tests/test_critical_power.py::TestAssessFitQuality -v`
Expected: 4 errors with `ImportError: cannot import name 'assess_fit_quality'`

- [ ] **Step 3: Implement `assess_fit_quality`**

Append to `ingestor/critical_power.py`:

```python
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
```

- [ ] **Step 4: Run the tests to confirm they pass**

Run: `python3 -m pytest tests/test_critical_power.py -v`
Expected: 13 passed (4 mean_maximal + 5 fit + 4 quality).

- [ ] **Step 5: Commit**

```bash
git add ingestor/critical_power.py tests/test_critical_power.py
git commit -m "feat(cp): assess_fit_quality gate with None handling"
```

---

## Task 5: DB helper — `fit_period` in `fitness.py`

**Files:**
- Modify: `ingestor/fitness.py` (add new function)

`fit_period` is the bridge between the pure functions in `critical_power.py` and the `activity_streams` table. It queries the DB once per ride per duration bucket, finds the period maximum at each duration, and calls `fit_monod_scherrer`.

This task does not have its own unit tests — it will be exercised by the integration tests in Task 7 against `recalculate_fitness`. The pure functions it calls already have full unit coverage.

**Performance note:** The implementation uses a per-ride loop (one query per activity to fetch its stream). This is an N+1 pattern but acceptable for personal-app scale (~50–100 rides per 90-day window) running once per day in a batch recalc. If profiling later shows this is slow, the optimization is a single query with `WHERE activity_id = ANY(%s)` plus client-side grouping. Deferred until measured.

- [ ] **Step 1: Add `fit_period` and helper imports to `fitness.py`**

At the top of `ingestor/fitness.py`, find the imports section and verify it imports `os` and `numpy as np` (numpy may need adding). Then find a logical place to insert `fit_period` — after `estimate_ftp` (around line 230) is a good spot, before `recalculate_fitness`.

Add this function:

```python
# CP/W' standard duration buckets (seconds) — sweet spot for the
# Monod-Scherrer model. Sub-60s is dominated by neuromuscular factors,
# >20min has insufficient density in most riders' data.
CP_DURATIONS = [60, 120, 300, 600, 1200]


def fit_period(conn, days: int) -> tuple[float | None, float | None, float | None, list[int]]:
    """Fit Monod-Scherrer for activities in the last `days` days.

    Returns (cp_watts, w_prime_kj, r_squared, durations_present) where
    durations_present is the list of CP_DURATIONS buckets that had at
    least one ride contributing a max effort.

    Returns (None, None, None, []) when:
    - No power-stream rides in the window
    - fit_monod_scherrer rejects the fit (degenerate or non-physiological)
    """
    from critical_power import compute_mean_maximal_power, fit_monod_scherrer

    # Find activities with power streams in the window.
    # Note: parameter goes outside the string literal — `interval '%s days'`
    # would only work for integer substitution and breaks under psycopg3.
    with conn.cursor() as cur:
        cur.execute("""
            SELECT a.id FROM activities a
            WHERE a.date >= CURRENT_DATE - %s * interval '1 day'
              AND EXISTS (
                  SELECT 1 FROM activity_streams s
                  WHERE s.activity_id = a.id AND s.power IS NOT NULL
              )
        """, (days,))
        activity_ids = [row[0] for row in cur.fetchall()]

    if not activity_ids:
        return (None, None, None, [])

    # For each duration bucket, find the period maximum across all rides
    period_max = {}  # {duration: max_power}
    for act_id in activity_ids:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT power FROM activity_streams
                WHERE activity_id = %s AND power IS NOT NULL
                ORDER BY time_offset
            """, (act_id,))
            powers = [float(row[0]) for row in cur.fetchall()]

        for duration in CP_DURATIONS:
            mmp = compute_mean_maximal_power(powers, duration)
            if mmp is None:
                continue
            if duration not in period_max or mmp > period_max[duration]:
                period_max[duration] = mmp

    if len(period_max) < 2:
        # Not enough data for any kind of fit
        return (None, None, None, list(period_max.keys()))

    efforts = sorted(period_max.items())  # [(duration, max_power), ...]
    cp, w_prime_kj, r2 = fit_monod_scherrer(efforts)
    return (cp, w_prime_kj, r2, sorted(period_max.keys()))
```

- [ ] **Step 2: Verify the imports are clean by syntax-checking the file**

Run: `python3 -c "import ast; ast.parse(open('ingestor/fitness.py').read()); print('OK')"`
Expected: `OK`

- [ ] **Step 3: Commit**

```bash
git add ingestor/fitness.py
git commit -m "feat(cp): fit_period DB helper for CP fit aggregation"
```

---

## Task 6: Orchestrator — `compute_cp_estimate` in `fitness.py`

**Files:**
- Modify: `ingestor/fitness.py` (add new function)

- [ ] **Step 1: Add `compute_cp_estimate` after `fit_period`**

Append to `ingestor/fitness.py`, immediately after `fit_period`:

```python
def compute_cp_estimate(
    conn,
    fallback_ftp: int | None = None,
) -> tuple[str, float, float | None, float | None, int | None] | None:
    """Compute today's CP estimate and persist to cp_estimates + sync_state.

    Tries 90-day window first, then 180-day fallback, then falls back to
    the existing rolling 20-min × 0.95 estimate (estimate_ftp). Always
    populates cp_estimates.fallback_ftp regardless of which source wins,
    so the user can compare directly.

    Args:
        conn: psycopg2 connection.
        fallback_ftp: precomputed rolling 20-min × 0.95 value to avoid
            redundant DB queries. If None, computes via estimate_ftp(conn).
            Pass the auto_ftp variable from recalculate_fitness here so the
            existing call site is reused.

    Returns the chosen tuple (source, value, w_prime_kj, r_squared, period_days)
    or None when there is no data at all to act on.
    """
    from critical_power import assess_fit_quality
    import db as _db

    # Short-circuit: nothing to do if there are no power-stream rides at all
    with conn.cursor() as cur:
        cur.execute("""
            SELECT 1 FROM activity_streams WHERE power IS NOT NULL LIMIT 1
        """)
        if cur.fetchone() is None:
            print("[fitness] No power streams — skipping CP estimate")
            return None

    # Use the precomputed value when caller passed it (avoids redundant DB
    # round-trip when recalculate_fitness already computed estimate_ftp).
    if fallback_ftp is None:
        fallback_ftp = estimate_ftp(conn)
    fallback = fallback_ftp

    # Try 90-day window first
    cp_90, wp_90, r2_90, durations_90 = fit_period(conn, days=90)
    if assess_fit_quality(r2_90, len(durations_90)):
        result = ("cp", cp_90, wp_90, r2_90, 90)
        chosen_duration_count = len(durations_90)
    else:
        # Try 180-day fallback window
        cp_180, wp_180, r2_180, durations_180 = fit_period(conn, days=180)
        if assess_fit_quality(r2_180, len(durations_180)):
            result = ("cp", cp_180, wp_180, r2_180, 180)
            chosen_duration_count = len(durations_180)
        elif fallback is not None:
            # Both CP fits failed quality gate — use rolling 20-min × 0.95
            result = ("20min_fallback", float(fallback), None, None, None)
            chosen_duration_count = None
        else:
            # No data even for the fallback — leave sync_state alone
            print("[fitness] CP fit failed and fallback FTP unavailable")
            return None

    source, value, w_prime_kj, r_squared, period_days = result

    # Upsert the row in cp_estimates
    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO cp_estimates
                (date, cp_watts, w_prime_kj, r_squared, period_days,
                 duration_count, source, fallback_ftp, updated_at)
            VALUES (CURRENT_DATE, %s, %s, %s, %s, %s, %s, %s, NOW())
            ON CONFLICT (date) DO UPDATE SET
                cp_watts = EXCLUDED.cp_watts,
                w_prime_kj = EXCLUDED.w_prime_kj,
                r_squared = EXCLUDED.r_squared,
                period_days = EXCLUDED.period_days,
                duration_count = EXCLUDED.duration_count,
                source = EXCLUDED.source,
                fallback_ftp = EXCLUDED.fallback_ftp,
                updated_at = NOW()
        """, (
            value if source == "cp" else None,
            w_prime_kj,
            r_squared,
            period_days,
            chosen_duration_count,
            source,
            fallback,
        ))

    # Update sync_state for Grafana
    _db.set_sync_state(conn, "estimated_ftp", str(int(round(value))))
    _db.set_sync_state(conn, "estimated_ftp_source", source)
    if w_prime_kj is not None:
        _db.set_sync_state(conn, "estimated_cp_w_prime_kj", f"{w_prime_kj:.2f}")
    if r_squared is not None:
        _db.set_sync_state(conn, "estimated_cp_quality", f"{r_squared:.3f}")

    r2_display = f"{r_squared:.3f}" if r_squared is not None else "n/a"
    print(f"[fitness] CP estimate: {value:.0f}W (source={source}, R²={r2_display})")
    return result
```

- [ ] **Step 2: Verify the file syntax**

Run: `python3 -c "import ast; ast.parse(open('ingestor/fitness.py').read()); print('OK')"`
Expected: `OK`

- [ ] **Step 3: Commit**

```bash
git add ingestor/fitness.py
git commit -m "feat(cp): compute_cp_estimate orchestrator with quality gating"
```

---

## Task 7: Wire `compute_cp_estimate` into `recalculate_fitness`

**Files:**
- Modify: `ingestor/fitness.py` (call site at end of `recalculate_fitness`)

- [ ] **Step 1: Find the end of `recalculate_fitness`**

Search for the final `print` statement in `recalculate_fitness`. It looks like:

```python
    print(f"[fitness] Calculated {len(daily_stats)} days of fitness data ...")
```

- [ ] **Step 2: Add the CP estimate call before the final print**

Immediately before the final print statement, add:

```python
    # Step 6: Compute CP/W' estimate (graceful fallback to existing rolling
    # 20-min × 0.95 when fit quality is poor). Pass auto_ftp through so
    # compute_cp_estimate doesn't redundantly recompute it — the value is
    # already in scope from the FTP resolution at line 274 of this function
    # (unconditional assignment: auto_ftp = estimate_ftp(conn)).
    print("[fitness] Computing CP / W' estimate...")
    try:
        compute_cp_estimate(conn, fallback_ftp=auto_ftp)
    except Exception as e:
        print(f"[fitness] CP estimate failed (non-fatal): {e}")
```

The try/except wrapper protects the rest of the recalc — even if CP fitting blows up, the existing fitness pipeline still completes. The fallback path handles intentional failures (poor data); the except handles unexpected ones (programming errors, DB issues).

- [ ] **Step 3: Verify the file syntax**

Run: `python3 -c "import ast; ast.parse(open('ingestor/fitness.py').read()); print('OK')"`
Expected: `OK`

- [ ] **Step 4: Commit**

```bash
git add ingestor/fitness.py
git commit -m "feat(cp): wire compute_cp_estimate into recalculate_fitness"
```

---

## Task 8: Update `test_fitness_recalc.py` mock cursor sequence

**Files:**
- Modify: `tests/test_fitness_recalc.py` (mock cursor sequence in `_make_conn`)

The new `compute_cp_estimate` adds DB cursors at the end of `recalculate_fitness`. The mock needs to handle them without failing existing tests.

The simplest approach: make the mock return empty results for the new CP cursors, which causes `compute_cp_estimate` to short-circuit on the "no power streams" check and return None. The CP path is exercised in dedicated unit tests on the pure functions, not in the integration test.

- [ ] **Step 1: Open `tests/test_fitness_recalc.py` and find `_make_conn`**

Look for the `make_cursor` inner function. It currently dispatches by cursor index. After all existing branches, the new CP estimate step will request additional cursors.

- [ ] **Step 2: Add a catch-all for cursors after the documented sequence**

In the `make_cursor` function, find the last `elif` branch (`elif idx == readback_idx:` or similar). After it, add:

```python
        else:
            # Cursors after the documented sequence belong to compute_cp_estimate.
            # Return empty/None so the CP path short-circuits cleanly.
            cur.fetchone.return_value = None
            cur.fetchall.return_value = []
```

If there's already an `else` branch, change it to handle the new case the same way.

- [ ] **Step 3: Run the existing fitness recalc tests**

Run: `python3 -m pytest tests/test_fitness_recalc.py -q`
Expected: 23 passed (or whatever the current count is — should be unchanged from main).

If any test fails because the mock cursor count check is now wrong, the test was asserting on `cursor_call_count[0]`. Update the assertion to account for one extra CP cursor.

- [ ] **Step 4: Commit**

```bash
git add tests/test_fitness_recalc.py
git commit -m "test(fitness): handle compute_cp_estimate cursors in mock"
```

---

## Task 9: Add ATP dashboard panels

**Files:**
- Modify: `grafana/dashboards/all-time-progression.json`

Two new panels in the Performance Progression section, immediately after the Aerobic Decoupling Trend / NP/kg Trend pair (currently at y=28). Both at w=12, paired layout.

- [ ] **Step 1: Inventory current ATP layout to confirm placement**

```bash
python3 -c "
import json
with open('grafana/dashboards/all-time-progression.json') as f:
    d = json.load(f)
for p in d['panels']:
    gp = p.get('gridPos', {})
    if 24 <= gp.get('y', 0) <= 50:
        print(f'  id={p[\"id\"]:4} x={gp[\"x\"]:2} y={gp[\"y\"]:3} w={gp[\"w\"]:2} h={gp[\"h\"]:2}  {p.get(\"title\",\"\")}')
"
```

Confirm Aerobic Decoupling Trend (id=1001) and NP/kg Trend (id=1002) are at y=28 with w=12 each, and the next row (Weekly Power Range id=500) is at y=36.

- [ ] **Step 2: Run a Python script to insert the two new panels and shift everything below them by +8**

```bash
python3 <<'PYEOF'
import json, copy

with open('grafana/dashboards/all-time-progression.json') as f:
    d = json.load(f)

# Find the Aerobic Decoupling Trend panel to use as a style template
template = None
for p in d['panels']:
    if p.get('id') == 1001:
        template = p
        break

# CP / W' Progression panel (id=1003)
# Note: explicit fieldConfig is set below — do NOT inherit Decoupling
# template's units (which are percent) or colors. CP and W' have
# different units (watts vs kJ) and different scales (~150-400 vs ~5-30),
# so they need a dual Y-axis layout: CP on the left in watts, W' on the
# right in kJ. Without the override, W' would render as a flat line near
# zero because the auto-scale picks the larger CP series.
cp_progression = copy.deepcopy(template)
cp_progression['id'] = 1003
cp_progression['type'] = 'timeseries'
cp_progression['title'] = 'CP / W'"'"' Progression'
cp_progression['description'] = (
    "Critical Power and W' (anaerobic work capacity) over time.\n\n"
    "CP rising = aerobic ceiling improving.\n\n"
    "W' rising = anaerobic capacity improving.\n\n"
    "Filtered to source='cp' rows only — fallback days are skipped."
)
cp_progression['gridPos'] = {"x": 0, "y": 36, "w": 12, "h": 8}
cp_progression['targets'] = [{
    "rawSql": (
        "SELECT date::timestamptz AS time,\n"
        "  cp_watts AS \"CP (W)\",\n"
        "  w_prime_kj AS \"W' (kJ)\"\n"
        "FROM cp_estimates\n"
        "WHERE source = 'cp' AND $__timeFilter(date)\n"
        "ORDER BY date;"
    ),
    "format": "time_series",
    "refId": "A"
}]
# Override inherited template config — CP defaults to watt, W' goes on
# secondary axis with kJ unit
cp_progression['fieldConfig'] = {
    "defaults": {
        "unit": "watt",
        "color": {"mode": "fixed", "fixedColor": "#3498db"},
        "custom": {
            "drawStyle": "line",
            "lineWidth": 2,
            "pointSize": 5,
            "axisPlacement": "auto"
        },
        "noValue": "No data"
    },
    "overrides": [
        {
            "matcher": {"id": "byName", "options": "W' (kJ)"},
            "properties": [
                {"id": "unit", "value": "kj"},
                {"id": "custom.axisPlacement", "value": "right"},
                {"id": "color", "value": {"mode": "fixed", "fixedColor": "#e67e22"}}
            ]
        }
    ]
}
# Reset options too — template panel may have inherited mode-specific options
cp_progression['options'] = {
    "tooltip": {"mode": "multi", "sort": "none"},
    "legend": {"displayMode": "list", "placement": "bottom", "showLegend": True}
}

# Power-Duration Curve panel (id=1004) — shows the latest fit's input data
pd_curve = copy.deepcopy(template)
pd_curve['id'] = 1004
pd_curve['title'] = 'Power-Duration Curve'
pd_curve['description'] = (
    "Mean maximal power at standard durations (1, 2, 5, 10, 20 min)\n"
    "from the last 90 days, plotted against the fitted Monod-Scherrer\n"
    "curve.\n\n"
    "Data points well above the curve = strong efforts.\n"
    "Curve far below points = poor fit (check R² in Est. FTP description).\n\n"
    "Note: always uses a fixed 90-day window regardless of the dashboard\n"
    "time picker. This shows your current power profile, not a historical\n"
    "one. The CP Progression panel respects the time picker for trends."
)
pd_curve['gridPos'] = {"x": 12, "y": 36, "w": 12, "h": 8}
pd_curve['targets'] = [{
    "rawSql": (
        "WITH recent AS (\n"
        "  SELECT a.id FROM activities a\n"
        "  WHERE a.date >= CURRENT_DATE - interval '90 days'\n"
        "    AND EXISTS (SELECT 1 FROM activity_streams s WHERE s.activity_id = a.id AND s.power IS NOT NULL)\n"
        "),\n"
        "rolling AS (\n"
        "  SELECT s.activity_id,\n"
        "    ROW_NUMBER() OVER (PARTITION BY s.activity_id ORDER BY s.time_offset) AS rn,\n"
        "    AVG(s.power) OVER w60   AS p60,\n"
        "    AVG(s.power) OVER w120  AS p120,\n"
        "    AVG(s.power) OVER w300  AS p300,\n"
        "    AVG(s.power) OVER w600  AS p600,\n"
        "    AVG(s.power) OVER w1200 AS p1200\n"
        "  FROM activity_streams s\n"
        "  JOIN recent r ON r.id = s.activity_id\n"
        "  WHERE s.power IS NOT NULL\n"
        "  WINDOW w60   AS (PARTITION BY s.activity_id ORDER BY s.time_offset ROWS BETWEEN 59   PRECEDING AND CURRENT ROW),\n"
        "         w120  AS (PARTITION BY s.activity_id ORDER BY s.time_offset ROWS BETWEEN 119  PRECEDING AND CURRENT ROW),\n"
        "         w300  AS (PARTITION BY s.activity_id ORDER BY s.time_offset ROWS BETWEEN 299  PRECEDING AND CURRENT ROW),\n"
        "         w600  AS (PARTITION BY s.activity_id ORDER BY s.time_offset ROWS BETWEEN 599  PRECEDING AND CURRENT ROW),\n"
        "         w1200 AS (PARTITION BY s.activity_id ORDER BY s.time_offset ROWS BETWEEN 1199 PRECEDING AND CURRENT ROW)\n"
        ")\n"
        "-- Filter rn >= duration so partial windows at the start of each\n"
        "-- ride are excluded. Without this, the first 59 samples produce\n"
        "-- a 60s avg over fewer than 60 rows, which can inflate MAX().\n"
        "-- ROWS-based windows count rows, not seconds, so this assumes 1Hz\n"
        "-- stream data with no gaps — same assumption as the Python\n"
        "-- compute_mean_maximal_power. Strava streams are 1Hz so this is safe.\n"
        "SELECT 60   AS \"duration_s\", MAX(p60)   AS \"power\" FROM rolling WHERE rn >= 60   UNION ALL\n"
        "SELECT 120,  MAX(p120)  FROM rolling WHERE rn >= 120  UNION ALL\n"
        "SELECT 300,  MAX(p300)  FROM rolling WHERE rn >= 300  UNION ALL\n"
        "SELECT 600,  MAX(p600)  FROM rolling WHERE rn >= 600  UNION ALL\n"
        "SELECT 1200, MAX(p1200) FROM rolling WHERE rn >= 1200\n"
        "ORDER BY 1;"
    ),
    "format": "table",
    "refId": "A"
}]
# Power-Duration Curve uses xychart not timeseries — override the type
pd_curve['type'] = 'xychart'
pd_curve['fieldConfig'] = {
    "defaults": {
        "custom": {
            "pointSize": 8,
            "show": "points"
        },
        "color": {"mode": "fixed", "fixedColor": "#3498db"}
    },
    "overrides": []
}
pd_curve['options'] = {
    "series": [{
        "x": {"matcher": {"id": "byName", "options": "duration_s"}},
        "y": {"matcher": {"id": "byName", "options": "power"}}
    }]
}

# Insert the new panels
d['panels'].append(cp_progression)
d['panels'].append(pd_curve)

# Shift all panels currently at y >= 36 down by 8 (to make room)
# But first restore the new panels' Y by appending after the shift
# So: shift FIRST, then re-set the new panels' Y
for p in d['panels']:
    if p.get('id') in (1003, 1004):
        continue  # don't shift the new ones (they're already placed at y=36)
    if p.get('gridPos', {}).get('y', 0) >= 36:
        p['gridPos']['y'] += 8

# Re-set new panels to y=36 (they may have been incorrectly shifted)
for p in d['panels']:
    if p.get('id') in (1003, 1004):
        p['gridPos']['y'] = 36

# Sort panels by (y, x) for clean rendering
d['panels'].sort(key=lambda p: (p['gridPos']['y'], p['gridPos']['x']))

with open('grafana/dashboards/all-time-progression.json', 'w') as f:
    json.dump(d, f, indent=2)
    f.write("\n")

# Sanity
ids = [p['id'] for p in d['panels']]
dupes = {i for i in ids if ids.count(i) > 1}
print(f"Panels: {len(ids)}, dupes: {dupes or 'none'}")
PYEOF
```

- [ ] **Step 3: Run dashboard tests**

Run: `python3 -m pytest tests/test_dashboards.py -q`
Expected: 24 passed (8 tests × 3 dashboards).

- [ ] **Step 4: Commit**

```bash
git add grafana/dashboards/all-time-progression.json
git commit -m "feat(dashboards): add CP/W' Progression + Power-Duration Curve panels"
```

---

## Task 10: Update Overview FTP panel description

**Files:**
- Modify: `grafana/dashboards/overview.json`

- [ ] **Step 1: Run a Python script to update the Est. FTP panel description**

```bash
python3 <<'PYEOF'
import json

with open('grafana/dashboards/overview.json') as f:
    d = json.load(f)

for p in d['panels']:
    if p.get('id') == 1020:  # Est. FTP panel
        p['description'] = (
            "Algorithmic FTP estimate.\n\n"
            "Source: Critical Power model fit when quality is good\n"
            "(R² ≥ 0.9 and ≥ 4 of 5 standard durations contributing).\n\n"
            "Falls back to rolling 90-day best 20-min × 0.95 when the\n"
            "CP fit quality gate fails.\n\n"
            "Both values are stored in the cp_estimates table for\n"
            "comparison — see CP / W' Progression on All Time Progression."
        )
        print("Updated Est. FTP panel description")
        break

with open('grafana/dashboards/overview.json', 'w') as f:
    json.dump(d, f, indent=2)
    f.write("\n")
PYEOF
```

- [ ] **Step 2: Run dashboard tests**

Run: `python3 -m pytest tests/test_dashboards.py -q`
Expected: 24 passed.

- [ ] **Step 3: Commit**

```bash
git add grafana/dashboards/overview.json
git commit -m "docs(dashboards): explain CP source on Est. FTP panel description"
```

---

## Task 11: Documentation updates

**Files:**
- Modify: `CLAUDE.md`
- Modify: `README.md`

- [ ] **Step 1: Add CP/W' to the CLAUDE.md Metrics section**

Find the metrics list in `CLAUDE.md` (around the "Metrics (Validated)" section). After the **W/kg** line, add:

```markdown
- **CP / W'**: Critical Power and W' (anaerobic work capacity) modeled via Monod-Scherrer 2-parameter fit (`P = W'/t + CP`) on mean maximal power at 5 standard durations (60s/120s/300s/600s/1200s). Stored daily in `cp_estimates`. Quality gate: R² ≥ 0.9 AND ≥ 4 of 5 durations contributing. Graceful fallback to rolling 20-min × 0.95 when the gate fails. Replaces the rolling 20-min calculation as the source of `sync_state.estimated_ftp`
```

- [ ] **Step 2: Add CP/W' to the Important Design Decisions section in CLAUDE.md**

After the **Per-ride weight** line, add:

```markdown
- **CP/W' modeling**: Monod-Scherrer linear fit via `numpy.polyfit` (no scipy dependency). Quality gate (R² ≥ 0.9 AND ≥ 4/5 durations) with 90d → 180d → existing `estimate_ftp()` fallback. CP replaces only the auto-estimate path — `VELOMATE_FTP` (when configured) still wins for TSS calculation. Pure functions in `ingestor/critical_power.py`, DB-touching helpers in `ingestor/fitness.py`. Physiological sanity check rejects fits with CP ≤ 0 or W' ≤ 0
```

- [ ] **Step 3: Add CP/W' to the README.md metrics list**

Find the metrics bullet list in `README.md` (around line 280, after the **W/kg** entry). Add:

```markdown
- **CP / W'**: Critical Power and W' modeled via Monod-Scherrer fit on mean maximal power at standard durations. Replaces rolling 20-min × 0.95 as the algorithmic FTP estimate when fit quality is good (R² ≥ 0.9). Graceful fallback to the old method when data is sparse. CP is the aerobic ceiling, W' is the anaerobic reservoir
```

- [ ] **Step 4: Commit**

```bash
git add CLAUDE.md README.md
git commit -m "docs: document CP / W' modeling in CLAUDE and README"
```

---

## Task 12: Final test run + push + open PR

- [ ] **Step 1: Run the full relevant test suite**

```bash
python3 -m pytest tests/test_critical_power.py tests/test_fitness.py tests/test_fitness_recalc.py tests/test_dashboards.py tests/test_intervals.py -q
```

Expected: all pass (no regressions, new CP tests included).

- [ ] **Step 2: Verify panel ID uniqueness on ATP**

```bash
python3 -c "
import json
with open('grafana/dashboards/all-time-progression.json') as f:
    d = json.load(f)
ids = [p['id'] for p in d['panels']]
dupes = {i for i in ids if ids.count(i) > 1}
print(f'Panels: {len(ids)}, dupes: {dupes or \"none\"}')
assert not dupes
"
```

Expected: no duplicates.

- [ ] **Step 3: Push the branch**

```bash
git push -u origin feat/cp-w-prime-foundation
```

- [ ] **Step 4: Open a PR via tea**

```bash
tea pr create --title "feat(cp): CP / W' foundation — Cluster B phase 1" --description "$(cat <<'EOF'
## Summary

Implements the CP / W' foundation per the design spec at `docs/superpowers/specs/2026-04-11-cp-w-prime-foundation-design.md`.

**What's new:**
- New `ingestor/critical_power.py` pure-function module: `compute_mean_maximal_power`, `fit_monod_scherrer` (with physiological sanity check), `assess_fit_quality`
- New `cp_estimates` table for daily CP estimates
- New `compute_cp_estimate` orchestrator in `fitness.py` with graceful fallback (90d → 180d → existing `estimate_ftp()`)
- Two new panels on All Time Progression: CP / W' Progression timeseries + Power-Duration Curve scatter
- Updated Overview Est. FTP panel description to explain the new source

**No new dependencies** — Monod-Scherrer is linear in 1/t so `numpy.polyfit` is sufficient.

**No TSS impact** — CP replaces only the auto-estimate path. `VELOMATE_FTP` still wins for TSS calculation.

## Test plan

- [x] Pure-function unit tests for all of `critical_power.py` (13 tests)
- [x] Existing fitness recalc tests still pass (mock updated for new cursors)
- [x] Dashboard structure tests pass (24 tests across 3 dashboards)
- [x] No new dependencies in `requirements.txt`

Server-side (post-merge):
- [ ] Restart ingestor, verify `cp_estimates` table populated
- [ ] Check `sync_state.estimated_ftp_source` value
- [ ] Confirm new ATP panels render
EOF
)" --base main
```

- [ ] **Step 5: Wait for Raven, address findings, merge**

Follow the standard pr-review-workflow: analyse Raven review, fix or push back per project policy, merge when stable.

---

## Self-Review Checklist

After implementing all tasks:

- [ ] All 13 pure-function tests pass
- [ ] `recalculate_fitness` integration test passes (mock cursor update)
- [ ] Dashboard tests pass (3 dashboards × 8 tests = 24)
- [ ] `cp_estimates` table is created on first run via `CREATE TABLE IF NOT EXISTS`
- [ ] No new entries in `requirements.txt`
- [ ] Spec coverage: every section in the design spec maps to a task
  - Schema (Components #2) → Task 1
  - Pure functions (Components #1) → Tasks 2, 3, 4
  - `fit_period` helper (Quality Gating) → Task 5
  - `compute_cp_estimate` (Components #3) → Task 6
  - Wire into `recalculate_fitness` → Task 7
  - Sync_state keys (Components #4) → embedded in Task 6
  - Dashboard panels (Components #5) → Tasks 9, 10
  - Documentation → Task 11

---

## Out of Scope (per spec)

- W'bal time series per ride (Cluster B phase 2)
- Fresh vs fatigued power-duration curves (Cluster B phase 3)
- Athlete type classification (gap #17, deferred)
- Morton 3-parameter model
- Replacing `ride_ftp` with CP
