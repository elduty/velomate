# Ride Analytics Depth — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Surface two latent per-ride insights — aerobic decoupling stored and trended over time, and automatically detected intervals classified by intensity — without adding any new data source or dependency.

**Architecture:** Both features follow the existing "compute once in ingestor, store in Postgres, read in Grafana" pattern. Feature 1 promotes the decoupling computation that currently lives in Grafana SQL into `ingestor/fitness.py`, stores it as a new `activities.aerobic_decoupling` column, and surfaces a steady-state-filtered trend on All Time Progression + a period-delta stat on Overview. Feature 2 adds a new `ingestor/intervals.py` module that walks each ride's power stream detecting sustained efforts ≥ 30s above configurable thresholds, classifies them into Coggan-style buckets (sprint/anaerobic/VO2/threshold/sweet spot/tempo), persists them in a new `ride_intervals` table, and adds an Activity Details interval table + an All Time Progression monthly distribution bar chart. Both features bump `METRICS_VERSION` to trigger full historical backfill on the next ingestor start. Tests are pure-function style following `tests/test_fitness.py` patterns — no database needed for computation logic.

**Tech Stack:** Python 3.10+, psycopg2, PostgreSQL 15, Grafana 12.4, pytest 8. No new dependencies.

**Source references (from baseline read):**
- `ingestor/fitness.py:15` — `METRICS_VERSION = "7"` (will bump to `"8"`)
- `ingestor/fitness.py:214` — reset query lists derived columns to NULL on version mismatch
- `ingestor/db.py:84-93` — `ALTER TABLE activities ADD COLUMN IF NOT EXISTS` pattern
- `grafana/dashboards/activity.json:1180-1238` — existing Decoupling stat card (will be rewritten to read stored column)
- `grafana/dashboards/all-time-progression.json:787-862` — Efficiency Factor timeseries panel (pattern to copy for decoupling trend)
- `grafana/dashboards/overview.json:1068-1118` — Δ Rides delta stat card (pattern to copy for Δ Decoupling)
- Max panel IDs: activity.json 906, all-time-progression.json 500, overview.json 610 — new IDs safe above 1000

**Branch + PR workflow:** All work on a single branch `feat/ride-analytics-depth`, pushed to Gitea `origin`, opened as one PR against `main`. Raven bot reviews. Address real findings, skip theoretical ones per project PR protocol. Squash merge. Delete remote+local branch.

---

## Task 1: Create the feature branch

**Files:** none (git operation)

- [ ] **Step 1: Create and switch to branch**

```bash
git checkout main
git pull
git checkout -b feat/ride-analytics-depth
```

Expected: `Switched to a new branch 'feat/ride-analytics-depth'`

---

## Task 2: Add `aerobic_decoupling` column to activities schema

**Files:**
- Modify: `ingestor/db.py:93` (add one line to the `create_schema` DDL block)

- [ ] **Step 1: Add the column DDL**

Locate the `ALTER TABLE activities ADD COLUMN IF NOT EXISTS variability_index FLOAT;` line at `ingestor/db.py:93`. Insert a new line immediately after it:

```python
            ALTER TABLE activities ADD COLUMN IF NOT EXISTS aerobic_decoupling FLOAT;
```

- [ ] **Step 2: Commit**

```bash
git add ingestor/db.py
git commit -m "feat(db): add aerobic_decoupling column to activities"
```

---

## Task 3: Write the failing test for `compute_decoupling`

**Files:**
- Modify: `tests/test_fitness.py` (add a new test class at the end of the file)

Aerobic decoupling = `(first_half_EF / second_half_EF - 1) × 100`, where EF = avg_power / avg_hr computed over each half of the power+HR stream.

- [ ] **Step 1: Add the test class**

Append to `tests/test_fitness.py`:

```python
# --- compute_decoupling (Friel) ---

class TestComputeDecoupling:
    """Aerobic decoupling: (first_half_EF / second_half_EF - 1) * 100.
    EF = avg_power / avg_hr. Positive = cardiac drift (HR rising relative to power).
    """

    def test_no_drift(self):
        """Constant power and HR across both halves -> 0% decoupling."""
        power = [200] * 200
        hr = [150] * 200
        assert compute_decoupling(power, hr) == 0.0

    def test_cardiac_drift(self):
        """HR rising in second half while power constant -> positive decoupling."""
        power = [200] * 200
        hr = [140] * 100 + [160] * 100
        # first_ef = 200/140 = 1.4286; second_ef = 200/160 = 1.25
        # decoupling = (1.4286/1.25 - 1) * 100 = 14.29
        result = compute_decoupling(power, hr)
        assert result == pytest.approx(14.29, abs=0.1)

    def test_negative_drift(self):
        """HR falling in second half -> negative decoupling (rare but valid)."""
        power = [200] * 200
        hr = [160] * 100 + [140] * 100
        # first_ef = 1.25; second_ef = 1.4286; decoupling = (1.25/1.4286 - 1) * 100 = -12.5
        result = compute_decoupling(power, hr)
        assert result == pytest.approx(-12.5, abs=0.1)

    def test_empty(self):
        assert compute_decoupling([], []) is None

    def test_mismatched_lengths(self):
        assert compute_decoupling([200, 200], [150]) is None

    def test_too_few_samples(self):
        """Fewer than 2 samples per half -> None."""
        assert compute_decoupling([200, 200], [150, 150]) is None

    def test_zero_hr_in_first_half(self):
        """Zero HR samples in first half should not produce infinity."""
        power = [200] * 200
        hr = [0] * 100 + [150] * 100
        # first half has no valid HR -> cannot compute first_ef -> None
        assert compute_decoupling(power, hr) is None

    def test_none_samples_filtered(self):
        """None values in the stream should be filtered, not cause TypeError."""
        power = [200, None, 200] * 100
        hr = [140, None, 140] * 100
        result = compute_decoupling(power, hr)
        assert result is not None
```

Also update the import at the top of the file to include `compute_decoupling`:

```python
from fitness import (
    calculate_tss, calculate_tss_power,
    compute_np, compute_trimp, compute_if, compute_vi,
    compute_decoupling,
)
```

- [ ] **Step 2: Run the test to verify it fails**

```bash
python3 -m pytest tests/test_fitness.py::TestComputeDecoupling -v
```

Expected: `ImportError: cannot import name 'compute_decoupling' from 'fitness'` — this is the RED phase.

---

## Task 4: Implement `compute_decoupling`

**Files:**
- Modify: `ingestor/fitness.py` (add a new pure function after `compute_vi` at line 88)

- [ ] **Step 1: Add the function**

Insert after line 88 (after `compute_vi`):

```python
def compute_decoupling(power_samples: list, hr_samples: list) -> float | None:
    """Aerobic decoupling (Friel) from matched power + HR 1-second streams.
    Splits the ride into two halves, computes EF (avg_power/avg_hr) for each,
    returns (first_EF / second_EF - 1) * 100 as a percentage.

    Positive values = cardiac drift (HR rising relative to power in the second half),
    which is a leading indicator of aerobic fatigue or insufficient base fitness.

    Returns None when streams are missing, mismatched, or too short for a
    meaningful split. None values inside the streams are filtered pair-wise.
    """
    if not power_samples or not hr_samples:
        return None
    if len(power_samples) != len(hr_samples):
        return None

    # Filter pair-wise, dropping any index where either value is missing
    pairs = [(p, h) for p, h in zip(power_samples, hr_samples)
             if p is not None and h is not None and h > 0]
    if len(pairs) < 4:  # need at least 2 samples per half
        return None

    mid = len(pairs) // 2
    first = pairs[:mid]
    second = pairs[mid:]

    def ef(half):
        if not half:
            return None
        avg_p = sum(p for p, _ in half) / len(half)
        avg_h = sum(h for _, h in half) / len(half)
        if avg_h <= 0:
            return None
        return avg_p / avg_h

    first_ef = ef(first)
    second_ef = ef(second)
    if first_ef is None or second_ef is None or second_ef == 0:
        return None

    return round((first_ef / second_ef - 1) * 100, 2)
```

- [ ] **Step 2: Run the test to verify it passes**

```bash
python3 -m pytest tests/test_fitness.py::TestComputeDecoupling -v
```

Expected: all 8 tests PASS.

- [ ] **Step 3: Run the full fitness test suite to check no regressions**

```bash
python3 -m pytest tests/test_fitness.py tests/test_np_ef.py -q
```

Expected: all pre-existing tests + 8 new tests PASS.

- [ ] **Step 4: Commit**

```bash
git add ingestor/fitness.py tests/test_fitness.py
git commit -m "feat(fitness): add compute_decoupling pure function with tests"
```

---

## Task 5: Wire `compute_decoupling` into `recalculate_fitness`

**Files:**
- Modify: `ingestor/fitness.py` (add decoupling to Step 1 NP/EF/Work loop at lines 218–256, and to the METRICS_VERSION reset query at line 214)

The existing "Step 1: Compute NP, EF, Work" block already loops through activities that have a power stream. We fold decoupling computation into the same loop — for each ride, we need to fetch power AND hr samples together (the current loop only fetches power).

- [ ] **Step 1: Update the METRICS_VERSION reset query**

Find line 214:

```python
            cur.execute("UPDATE activities SET tss = NULL, np = NULL, ef = NULL, work_kj = NULL, ride_ftp = NULL, intensity_factor = NULL, trimp = NULL, variability_index = NULL")
```

Replace with:

```python
            cur.execute("UPDATE activities SET tss = NULL, np = NULL, ef = NULL, work_kj = NULL, ride_ftp = NULL, intensity_factor = NULL, trimp = NULL, variability_index = NULL, aerobic_decoupling = NULL")
```

- [ ] **Step 2: Update the "Step 1: Compute NP, EF, Work" query predicate**

Find the activity-selection SQL at lines 222–231:

```python
        cur.execute("""
            SELECT a.id, a.avg_hr, a.avg_power
            FROM activities a
            WHERE a.np IS NULL AND a.date IS NOT NULL
              AND EXISTS (
                  SELECT 1 FROM activity_streams s
                  WHERE s.activity_id = a.id AND s.power IS NOT NULL
                  GROUP BY s.activity_id HAVING COUNT(*) > 30
              )
        """)
```

Replace the `WHERE a.np IS NULL` clause with `WHERE (a.np IS NULL OR a.aerobic_decoupling IS NULL)` so rides missing only decoupling are also picked up:

```python
        cur.execute("""
            SELECT a.id, a.avg_hr, a.avg_power, a.np, a.aerobic_decoupling
            FROM activities a
            WHERE (a.np IS NULL OR a.aerobic_decoupling IS NULL) AND a.date IS NOT NULL
              AND EXISTS (
                  SELECT 1 FROM activity_streams s
                  WHERE s.activity_id = a.id AND s.power IS NOT NULL
                  GROUP BY s.activity_id HAVING COUNT(*) > 30
              )
        """)
```

- [ ] **Step 3: Update the stream-fetch loop to pull HR alongside power**

Find the loop starting at line 235:

```python
    np_count = 0
    for act_id, avg_hr, avg_power in power_activities:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT power FROM activity_streams
                WHERE activity_id = %s AND power IS NOT NULL
                ORDER BY time_offset
            """, (act_id,))
            power_samples = [r[0] for r in cur.fetchall()]
            work_val = round(sum(power_samples) / 1000.0, 1)

        np_val = compute_np(power_samples)

        if np_val:
            ef_val = compute_ef(np_val, avg_hr)
            vi_val = compute_vi(np_val, avg_power)
            with conn.cursor() as cur:
                cur.execute("""
                    UPDATE activities SET np = %s, ef = %s, work_kj = %s, variability_index = %s WHERE id = %s
                """, (np_val, ef_val, work_val, vi_val, act_id))
            np_count += 1

    print(f"[fitness] Computed NP/EF/Work for {np_count} activities")
```

Replace the entire loop with:

```python
    np_count = 0
    decoupling_count = 0
    for act_id, avg_hr, avg_power, existing_np, existing_decoupling in power_activities:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT power, hr FROM activity_streams
                WHERE activity_id = %s AND power IS NOT NULL
                ORDER BY time_offset
            """, (act_id,))
            rows = cur.fetchall()
            power_samples = [r[0] for r in rows]
            hr_samples = [r[1] for r in rows]
            work_val = round(sum(power_samples) / 1000.0, 1)

        # Only compute NP if missing (avoids redundant work)
        np_val = existing_np
        if existing_np is None:
            np_val = compute_np(power_samples)
            if np_val:
                ef_val = compute_ef(np_val, avg_hr)
                vi_val = compute_vi(np_val, avg_power)
                with conn.cursor() as cur:
                    cur.execute("""
                        UPDATE activities SET np = %s, ef = %s, work_kj = %s, variability_index = %s WHERE id = %s
                    """, (np_val, ef_val, work_val, vi_val, act_id))
                np_count += 1

        # Compute decoupling if missing and HR stream has any data
        if existing_decoupling is None and any(h is not None and h > 0 for h in hr_samples):
            dec_val = compute_decoupling(power_samples, hr_samples)
            if dec_val is not None:
                with conn.cursor() as cur:
                    cur.execute("UPDATE activities SET aerobic_decoupling = %s WHERE id = %s", (dec_val, act_id))
                decoupling_count += 1

    print(f"[fitness] Computed NP/EF/Work for {np_count} activities")
    print(f"[fitness] Computed aerobic decoupling for {decoupling_count} activities")
```

- [ ] **Step 4: Run fitness-related tests (no DB — pure-function layer only)**

```bash
python3 -m pytest tests/test_fitness.py tests/test_fitness_recalc.py tests/test_np_ef.py -q
```

Expected: all pass. `test_fitness_recalc.py` will exercise the integration if it uses an in-memory or fixture DB.

- [ ] **Step 5: Commit**

```bash
git add ingestor/fitness.py
git commit -m "feat(fitness): wire compute_decoupling into recalculate_fitness loop"
```

---

## Task 6: Bump METRICS_VERSION to trigger historical backfill

**Files:**
- Modify: `ingestor/fitness.py:15`

- [ ] **Step 1: Bump the version**

Change line 15 from:

```python
METRICS_VERSION = "7"  # v7: NP uses 30s SMA (Coggan standard, matches GoldenCheetah)
```

to:

```python
METRICS_VERSION = "8"  # v8: store aerobic_decoupling on activities for trend analysis
```

- [ ] **Step 2: Commit**

```bash
git add ingestor/fitness.py
git commit -m "feat(fitness): bump METRICS_VERSION to 8 for decoupling backfill"
```

---

## Task 7: Update existing Activity Details Decoupling panel to read stored column

**Files:**
- Modify: `grafana/dashboards/activity.json:1182-1200` (the Aerobic Decoupling stat panel)

- [ ] **Step 1: Replace the raw SQL**

Find the panel at `id: 411, title: "Aerobic Decoupling"` (starts at line 1180). Its current `rawSql` computes decoupling on the fly from `activity_streams`. Replace the `rawSql` field at line 1196 with a direct read of the stored column:

Current (line 1196):
```json
"rawSql": "WITH half AS (\n  SELECT MAX(time_offset) / 2 AS mid\n  FROM activity_streams\n  WHERE activity_id = ${activity_id}\n),\nhalves AS (\n  SELECT\n    CASE WHEN s.time_offset <= h.mid THEN 'first' ELSE 'second' END AS half,\n    s.power, s.hr\n  FROM activity_streams s\n  CROSS JOIN half h\n  WHERE s.activity_id = ${activity_id}\n    AND s.power IS NOT NULL\n    AND s.hr IS NOT NULL AND s.hr > 0\n)\nSELECT ROUND(((\n  (SELECT AVG(power)::float / NULLIF(AVG(hr), 0) FROM halves WHERE half = 'first') /\n  NULLIF((SELECT AVG(power)::float / NULLIF(AVG(hr), 0) FROM halves WHERE half = 'second'), 0)\n  - 1) * 100)::numeric, 1) AS \"Decoupling %\";",
```

Replace with:
```json
"rawSql": "SELECT ROUND(aerobic_decoupling::numeric, 1) AS \"Decoupling %\" FROM activities WHERE id = ${activity_id};",
```

- [ ] **Step 2: Validate JSON structure with the dashboard test suite**

```bash
python3 -m pytest tests/test_dashboards.py -q
```

Expected: all tests pass. If `test_valid_json` fails, fix the JSON quoting.

- [ ] **Step 3: Commit**

```bash
git add grafana/dashboards/activity.json
git commit -m "feat(dashboards): read aerobic_decoupling from stored column on activity page"
```

---

## Task 8: Add "Aerobic Decoupling Trend" panel to All Time Progression dashboard

**Files:**
- Modify: `grafana/dashboards/all-time-progression.json` — add a new panel under the "Performance Progression" row

The trend must only include steady-state rides (VI < 1.05, duration > 3600s, power + HR both present) so drift on interval workouts doesn't pollute the signal. Pattern copied from the Efficiency Factor panel (line 787–862) which has the 10-ride rolling average and linear regression we want.

- [ ] **Step 1: Insert the new panel**

Find the panel array. After the panel with `title: "Efficiency Factor"` (ends around line 862), insert the following panel object. Preserve JSON comma syntax — the new panel goes between the EF panel's closing `}` and the next panel (`Weekly Power Range`):

```json
    {
      "id": 1001,
      "title": "Aerobic Decoupling Trend",
      "type": "timeseries",
      "gridPos": {
        "h": 8,
        "w": 24,
        "x": 0,
        "y": 36
      },
      "datasource": {
        "type": "postgres",
        "uid": "velomate"
      },
      "targets": [
        {
          "rawSql": "SELECT a.date::date AS time,\n  ROUND(a.aerobic_decoupling::numeric, 2) AS \"Decoupling %\",\n  ROUND(AVG(a.aerobic_decoupling) OVER (ORDER BY a.date ROWS BETWEEN 9 PRECEDING AND CURRENT ROW)::numeric, 2) AS \"10-ride avg\"\nFROM activities a\nWHERE a.aerobic_decoupling IS NOT NULL\n  AND a.variability_index IS NOT NULL AND a.variability_index < 1.05\n  AND a.duration_s > 3600\n  AND $__timeFilter(a.date)\n  AND (('${sport_type}' = 'all') OR a.sport_type = '${sport_type}')\nORDER BY a.date;",
          "format": "table",
          "refId": "A"
        }
      ],
      "fieldConfig": {
        "defaults": {
          "color": {
            "fixedColor": "#fade2a",
            "mode": "fixed"
          },
          "custom": {
            "drawStyle": "line",
            "lineWidth": 0,
            "pointSize": 5
          },
          "unit": "percent",
          "noValue": "No steady-state rides in period"
        },
        "overrides": [
          {
            "matcher": {
              "id": "byName",
              "options": "10-ride avg"
            },
            "properties": [
              {
                "id": "custom.lineWidth",
                "value": 2
              },
              {
                "id": "custom.pointSize",
                "value": 0
              },
              {
                "id": "custom.fillOpacity",
                "value": 0
              },
              {
                "id": "custom.spanNulls",
                "value": true
              },
              {
                "id": "color",
                "value": {
                  "fixedColor": "#f2cc0c",
                  "mode": "fixed"
                }
              }
            ]
          }
        ]
      },
      "description": "Aerobic Decoupling (Friel) — cardiac drift on steady rides (VI < 1.05, duration > 60 min).\n\nFalling trend = aerobic base improving. Rising trend = aerobic fitness degrading or fatigue accumulating.\n\nRecommend looking at the 10-ride rolling average — the raw per-ride value is noisy.",
      "transformations": [
        {
          "id": "regression",
          "options": {
            "modelType": "linear"
          }
        }
      ]
    },
```

- [ ] **Step 2: Run dashboard structural tests**

```bash
python3 -m pytest tests/test_dashboards.py -q
```

Expected: all tests pass (panel 1001 has unique id, valid gridPos, valid JSON).

- [ ] **Step 3: Commit**

```bash
git add grafana/dashboards/all-time-progression.json
git commit -m "feat(dashboards): add Aerobic Decoupling Trend panel to All Time Progression"
```

---

## Task 9: Add "Avg Decoupling (steady rides)" period stat to Overview

**Files:**
- Modify: `grafana/dashboards/overview.json` — add a new stat panel alongside the delta cards

Place this alongside the existing fitness section. Using pattern from the `Δ Rides` delta stat (line 1068–1118) for the Δ card, and a simple period aggregate for the value card.

- [ ] **Step 1: Insert the new stat panel**

Find a location near the period stat cards (search for `"title": "TSS"` around line 864). Insert this panel right after the `"TSS"` stat panel's closing `}`, keeping JSON comma syntax correct:

```json
    {
      "id": 1010,
      "title": "Avg Decoupling (steady)",
      "type": "stat",
      "gridPos": {
        "h": 3,
        "w": 6,
        "x": 18,
        "y": 11
      },
      "datasource": {
        "type": "postgres",
        "uid": "velomate"
      },
      "targets": [
        {
          "rawSql": "SELECT ROUND(AVG(aerobic_decoupling)::numeric, 2) AS \"Avg Decoupling\"\nFROM activities\nWHERE aerobic_decoupling IS NOT NULL\n  AND variability_index IS NOT NULL AND variability_index < 1.05\n  AND duration_s > 3600\n  AND $__timeFilter(date)\n  AND (('${sport_type}' = 'all') OR sport_type = '${sport_type}');",
          "format": "table",
          "refId": "A"
        }
      ],
      "options": {
        "colorMode": "value",
        "graphMode": "none",
        "textMode": "auto",
        "reduceOptions": {
          "calcs": [
            "lastNotNull"
          ]
        }
      },
      "fieldConfig": {
        "defaults": {
          "noValue": "N/A",
          "unit": "percent",
          "color": {
            "mode": "thresholds"
          },
          "thresholds": {
            "mode": "absolute",
            "steps": [
              {
                "color": "green",
                "value": null
              },
              {
                "color": "yellow",
                "value": 5
              },
              {
                "color": "red",
                "value": 10
              }
            ]
          }
        }
      },
      "description": "Average aerobic decoupling on steady-state rides in the selected period. Lower = better aerobic fitness."
    },
```

- [ ] **Step 2: Run dashboard structural tests**

```bash
python3 -m pytest tests/test_dashboards.py -q
```

Expected: all tests pass.

- [ ] **Step 3: Commit**

```bash
git add grafana/dashboards/overview.json
git commit -m "feat(dashboards): add Avg Decoupling period stat to Overview"
```

---

## Task 10: Add `ride_intervals` table to schema

**Files:**
- Modify: `ingestor/db.py` — add new table DDL inside `create_schema(conn)`

- [ ] **Step 1: Add the CREATE TABLE statement**

Inside the `create_schema` function in `ingestor/db.py`, add the following table definition. Insert it immediately after the `CREATE TABLE IF NOT EXISTS sync_state (...)` block (line 78–82) and before the `ALTER TABLE` section (line 84):

```python
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
```

- [ ] **Step 2: Commit**

```bash
git add ingestor/db.py
git commit -m "feat(db): add ride_intervals table for auto-detected intervals"
```

---

## Task 11: Write failing tests for `detect_intervals`

**Files:**
- Create: `tests/test_intervals.py` (new file)

- [ ] **Step 1: Write the test module**

Create `tests/test_intervals.py` with:

```python
"""Tests for auto interval detection in ingestor/intervals.py."""

import pytest

from intervals import detect_intervals, classify_interval


# --- classify_interval ---

class TestClassifyInterval:
    """Coggan-style classification from duration + avg power relative to FTP."""

    def test_sprint(self):
        # 20s at 200% FTP
        assert classify_interval(20, 400, ftp=200) == "sprint"

    def test_anaerobic(self):
        # 60s at 140% FTP
        assert classify_interval(60, 280, ftp=200) == "anaerobic"

    def test_vo2(self):
        # 4 min at 115% FTP
        assert classify_interval(240, 230, ftp=200) == "vo2"

    def test_threshold(self):
        # 10 min at 100% FTP
        assert classify_interval(600, 200, ftp=200) == "threshold"

    def test_sweetspot(self):
        # 20 min at 90% FTP
        assert classify_interval(1200, 180, ftp=200) == "sweetspot"

    def test_tempo(self):
        # 30 min at 80% FTP
        assert classify_interval(1800, 160, ftp=200) == "tempo"

    def test_unclassified_short_easy(self):
        """Short easy effort below tempo threshold -> None (not an interval)."""
        assert classify_interval(30, 100, ftp=200) is None

    def test_unclassified_long_easy(self):
        """Long effort below tempo threshold -> None."""
        assert classify_interval(2400, 120, ftp=200) is None


# --- detect_intervals ---

class TestDetectIntervals:
    """Detection: find contiguous sustained efforts ≥ 30s above threshold_pct × FTP."""

    def test_empty(self):
        assert detect_intervals([], ftp=200) == []

    def test_no_effort(self):
        """All zone 2 — no intervals detected."""
        samples = [120] * 600  # 10 min at 60% FTP
        assert detect_intervals(samples, ftp=200) == []

    def test_single_threshold_interval(self):
        """10 min warmup, 8 min threshold, 10 min cooldown."""
        samples = [120] * 600 + [210] * 480 + [120] * 600
        intervals = detect_intervals(samples, ftp=200, threshold_pct=0.85)
        assert len(intervals) == 1
        iv = intervals[0]
        assert iv["start_offset_s"] == 600
        assert iv["duration_s"] == 480
        assert 200 <= iv["avg_power"] <= 220
        assert iv["classification"] == "threshold"

    def test_multiple_intervals(self):
        """Four 2-min VO2 reps with 1-min recovery."""
        samples = [100] * 300  # 5 min warmup
        for _ in range(4):
            samples += [240] * 120  # 2 min at 120% FTP
            samples += [100] * 60   # 1 min recovery
        samples += [100] * 300  # 5 min cooldown
        intervals = detect_intervals(samples, ftp=200, threshold_pct=0.85)
        assert len(intervals) == 4
        assert all(iv["classification"] == "vo2" for iv in intervals)
        assert all(115 <= iv["duration_s"] <= 125 for iv in intervals)

    def test_minimum_duration_filter(self):
        """A 20-second surge (< 30s) should not be detected as an interval."""
        samples = [100] * 300 + [250] * 20 + [100] * 300
        intervals = detect_intervals(samples, ftp=200, min_duration_s=30)
        assert intervals == []

    def test_spike_bridges_gap(self):
        """A 5-second dip in the middle of a 5-min threshold effort should not split it."""
        samples = [100] * 300 + [210] * 150 + [100] * 5 + [210] * 150 + [100] * 300
        intervals = detect_intervals(samples, ftp=200, threshold_pct=0.85, gap_tolerance_s=10)
        assert len(intervals) == 1
        assert intervals[0]["duration_s"] >= 300  # bridged

    def test_filters_none_samples(self):
        """None in the stream should be treated as zero, not cause TypeError."""
        samples = [100] * 300 + [None] * 60 + [210] * 300 + [100] * 300
        # Should still find the 5-min threshold effort
        intervals = detect_intervals(samples, ftp=200, threshold_pct=0.85)
        assert len(intervals) >= 1
```

- [ ] **Step 2: Run the tests to verify they fail**

```bash
python3 -m pytest tests/test_intervals.py -v
```

Expected: `ModuleNotFoundError: No module named 'intervals'` — RED phase.

---

## Task 12: Implement `classify_interval` and `detect_intervals`

**Files:**
- Create: `ingestor/intervals.py` (new module)

- [ ] **Step 1: Write the module**

Create `ingestor/intervals.py` with:

```python
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
"""

from __future__ import annotations


def classify_interval(duration_s: int, avg_power: float, ftp: int) -> str | None:
    """Classify an interval by duration and % of FTP. Returns a bucket name or None."""
    if ftp <= 0 or avg_power <= 0 or duration_s <= 0:
        return None
    pct = avg_power / ftp

    # Sprint: < 30s at > 150% FTP
    if duration_s < 30 and pct > 1.50:
        return "sprint"
    # Anaerobic: 30s–2 min at 120–150% FTP
    if 30 <= duration_s <= 120 and 1.20 <= pct <= 1.50:
        return "anaerobic"
    # VO2max: 2–5 min at 105–120% FTP
    if 120 < duration_s <= 300 and 1.05 <= pct < 1.20:
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
    ftp: int,
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

    def close(end_exclusive: int):
        """Close the current interval at end_exclusive (non-inclusive), add to list if long enough."""
        nonlocal in_interval
        # Trim the trailing below-threshold samples that were tolerated
        effective_end = end_exclusive - below_run
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
                    close(i)

    if in_interval:
        close(len(samples))

    return intervals
```

- [ ] **Step 2: Run the tests to verify they pass**

```bash
python3 -m pytest tests/test_intervals.py -v
```

Expected: all 14 tests PASS.

- [ ] **Step 3: Run the full test suite to check no regressions**

```bash
python3 -m pytest tests/ -q
```

Expected: all tests pass (new + pre-existing).

- [ ] **Step 4: Commit**

```bash
git add ingestor/intervals.py tests/test_intervals.py
git commit -m "feat(intervals): add interval detection + classification module with tests"
```

---

## Task 13: Wire `detect_intervals` into the ingestor recalc loop

**Files:**
- Modify: `ingestor/fitness.py` — add an interval-compute step after the existing Step 1 NP/EF/Work/Decoupling block

The existing loop already fetches `power_samples` per ride. We fold interval detection into the same loop, using the ride's own `ride_ftp` (if set) or the global FTP as the scaling FTP.

- [ ] **Step 1: Add import**

At the top of `ingestor/fitness.py`, next to the existing internal imports, add:

```python
from intervals import detect_intervals
```

(Place it with the other top-level imports, e.g. after `import psycopg2.extras`.)

- [ ] **Step 2: Extend the fetch query with ride_ftp**

Find the updated SQL from Task 5 Step 2 — the one that selects `a.id, a.avg_hr, a.avg_power, a.np, a.aerobic_decoupling`. Extend it to also return `ride_ftp`:

```python
        cur.execute("""
            SELECT a.id, a.avg_hr, a.avg_power, a.np, a.aerobic_decoupling, a.ride_ftp
            FROM activities a
            WHERE (a.np IS NULL OR a.aerobic_decoupling IS NULL) AND a.date IS NOT NULL
              AND EXISTS (
                  SELECT 1 FROM activity_streams s
                  WHERE s.activity_id = a.id AND s.power IS NOT NULL
                  GROUP BY s.activity_id HAVING COUNT(*) > 30
              )
        """)
```

Update the loop tuple unpacking to match:

```python
    for act_id, avg_hr, avg_power, existing_np, existing_decoupling, ride_ftp_val in power_activities:
```

- [ ] **Step 3: Compute and persist intervals in the same loop**

After the decoupling block inside the loop (which ends with `decoupling_count += 1`), add:

```python
        # Detect and persist intervals for rides that don't have any yet
        with conn.cursor() as cur:
            cur.execute("SELECT 1 FROM ride_intervals WHERE activity_id = %s LIMIT 1", (act_id,))
            has_intervals = cur.fetchone() is not None
        if not has_intervals:
            act_ftp = ride_ftp_val if ride_ftp_val and ride_ftp_val > 0 else ftp
            detected = detect_intervals(power_samples, ftp=int(act_ftp))
            if detected:
                with conn.cursor() as cur:
                    psycopg2.extras.execute_values(
                        cur,
                        """INSERT INTO ride_intervals
                            (activity_id, start_offset_s, duration_s, avg_power, np, max_power, avg_hr, classification)
                           VALUES %s""",
                        [(act_id, d["start_offset_s"], d["duration_s"], d["avg_power"],
                          d["np"], d["max_power"], None, d["classification"])
                         for d in detected]
                    )
```

Note: `ftp` is defined earlier in `recalculate_fitness` (line 194). The `int(act_ftp)` cast ensures classification thresholds work on an integer FTP.

- [ ] **Step 4: Update the METRICS_VERSION reset block to also clear ride_intervals**

Find the reset block (line ~212–216, after the METRICS_VERSION mismatch check). Currently:

```python
        with conn.cursor() as cur:
            cur.execute("UPDATE activities SET tss = NULL, np = NULL, ef = NULL, work_kj = NULL, ride_ftp = NULL, intensity_factor = NULL, trimp = NULL, variability_index = NULL, aerobic_decoupling = NULL")
            cur.execute("DELETE FROM athlete_stats")
        _db.set_sync_state(conn, "metrics_version", METRICS_VERSION)
```

Add a `DELETE FROM ride_intervals` line so bumping the version re-detects:

```python
        with conn.cursor() as cur:
            cur.execute("UPDATE activities SET tss = NULL, np = NULL, ef = NULL, work_kj = NULL, ride_ftp = NULL, intensity_factor = NULL, trimp = NULL, variability_index = NULL, aerobic_decoupling = NULL")
            cur.execute("DELETE FROM athlete_stats")
            cur.execute("DELETE FROM ride_intervals")
        _db.set_sync_state(conn, "metrics_version", METRICS_VERSION)
```

- [ ] **Step 5: Bump METRICS_VERSION to 9**

Change line 15 from `METRICS_VERSION = "8"` to:

```python
METRICS_VERSION = "9"  # v9: auto-detect and store ride_intervals
```

- [ ] **Step 6: Run tests**

```bash
python3 -m pytest tests/ -q
```

Expected: all pass.

- [ ] **Step 7: Commit**

```bash
git add ingestor/fitness.py
git commit -m "feat(fitness): detect and persist intervals during recalculate_fitness"
```

---

## Task 14: Add "Detected Intervals" table panel to Activity Details

**Files:**
- Modify: `grafana/dashboards/activity.json` — add a new table panel after the Aerobic Decoupling stat card

- [ ] **Step 1: Insert the new panel**

Find the Aerobic Decoupling panel (`id: 411`, around line 1180). After its closing `}` at line 1238, and before the row divider at line 1239 (`id: 902, "title": "Zone Analysis"`), insert this new panel:

```json
    {
      "id": 1100,
      "title": "Detected Intervals",
      "type": "table",
      "gridPos": {
        "h": 8,
        "w": 24,
        "x": 0,
        "y": 24
      },
      "datasource": {
        "type": "postgres",
        "uid": "velomate"
      },
      "targets": [
        {
          "rawSql": "SELECT\n  ROW_NUMBER() OVER (ORDER BY start_offset_s) AS \"#\",\n  TO_CHAR((start_offset_s || ' seconds')::interval, 'HH24:MI:SS') AS \"Start\",\n  TO_CHAR((duration_s || ' seconds')::interval, 'MI:SS') AS \"Duration\",\n  ROUND(avg_power::numeric, 0) AS \"Avg W\",\n  ROUND(np::numeric, 0) AS \"NP\",\n  ROUND(max_power::numeric, 0) AS \"Max W\",\n  classification AS \"Type\"\nFROM ride_intervals\nWHERE activity_id = ${activity_id}\nORDER BY start_offset_s;",
          "format": "table",
          "refId": "A"
        }
      ],
      "fieldConfig": {
        "defaults": {
          "noValue": "No intervals detected"
        },
        "overrides": [
          {
            "matcher": {
              "id": "byName",
              "options": "Type"
            },
            "properties": [
              {
                "id": "custom.cellOptions",
                "value": {
                  "type": "color-background"
                }
              },
              {
                "id": "mappings",
                "value": [
                  {
                    "type": "value",
                    "options": {
                      "sprint":    { "color": "purple", "index": 0 },
                      "anaerobic": { "color": "red",    "index": 1 },
                      "vo2":       { "color": "orange", "index": 2 },
                      "threshold": { "color": "yellow", "index": 3 },
                      "sweetspot": { "color": "green",  "index": 4 },
                      "tempo":     { "color": "blue",   "index": 5 }
                    }
                  }
                ]
              }
            ]
          }
        ]
      },
      "description": "Automatically detected sustained efforts in this ride, classified by duration and intensity (% of ride FTP). Thresholds: sprint >150% <30s, anaerobic 120–150% 30s–2min, VO2 105–120% 2–5min, threshold 95–105% 5–20min, sweet spot 83–94% 15–60min, tempo 75–85% >20min."
    },
```

- [ ] **Step 2: Run dashboard structural tests**

```bash
python3 -m pytest tests/test_dashboards.py -q
```

Expected: all tests pass.

- [ ] **Step 3: Commit**

```bash
git add grafana/dashboards/activity.json
git commit -m "feat(dashboards): add Detected Intervals table to Activity Details"
```

---

## Task 15: Add "Monthly Interval Distribution" panel to All Time Progression

**Files:**
- Modify: `grafana/dashboards/all-time-progression.json` — add new panel under the "Training Zones Over Time" row

- [ ] **Step 1: Insert the new panel**

Find the row `"title": "Training Zones Over Time"` (line 1041 area). Locate the "Monthly HR Zone Distribution" panel that ends before the "Fitness History" row (line 1312). Insert the following panel immediately after the HR zone panel's closing `}` and before the Fitness History row:

```json
    {
      "id": 1110,
      "title": "Monthly Interval Distribution",
      "type": "barchart",
      "gridPos": {
        "h": 8,
        "w": 24,
        "x": 0,
        "y": 96
      },
      "datasource": {
        "type": "postgres",
        "uid": "velomate"
      },
      "targets": [
        {
          "rawSql": "SELECT\n  date_trunc('month', a.date)::date AS time,\n  COUNT(*) FILTER (WHERE ri.classification = 'sprint')    AS \"sprint\",\n  COUNT(*) FILTER (WHERE ri.classification = 'anaerobic') AS \"anaerobic\",\n  COUNT(*) FILTER (WHERE ri.classification = 'vo2')       AS \"vo2\",\n  COUNT(*) FILTER (WHERE ri.classification = 'threshold') AS \"threshold\",\n  COUNT(*) FILTER (WHERE ri.classification = 'sweetspot') AS \"sweetspot\",\n  COUNT(*) FILTER (WHERE ri.classification = 'tempo')     AS \"tempo\"\nFROM ride_intervals ri\nJOIN activities a ON a.id = ri.activity_id\nWHERE $__timeFilter(a.date)\n  AND (('${sport_type}' = 'all') OR a.sport_type = '${sport_type}')\nGROUP BY 1\nORDER BY 1;",
          "format": "table",
          "refId": "A"
        }
      ],
      "fieldConfig": {
        "defaults": {
          "noValue": "No intervals in period",
          "custom": {
            "stacking": {
              "mode": "normal"
            }
          }
        },
        "overrides": [
          { "matcher": { "id": "byName", "options": "sprint" },    "properties": [{ "id": "color", "value": { "mode": "fixed", "fixedColor": "purple" } }] },
          { "matcher": { "id": "byName", "options": "anaerobic" }, "properties": [{ "id": "color", "value": { "mode": "fixed", "fixedColor": "red"    } }] },
          { "matcher": { "id": "byName", "options": "vo2" },       "properties": [{ "id": "color", "value": { "mode": "fixed", "fixedColor": "orange" } }] },
          { "matcher": { "id": "byName", "options": "threshold" }, "properties": [{ "id": "color", "value": { "mode": "fixed", "fixedColor": "yellow" } }] },
          { "matcher": { "id": "byName", "options": "sweetspot" }, "properties": [{ "id": "color", "value": { "mode": "fixed", "fixedColor": "green"  } }] },
          { "matcher": { "id": "byName", "options": "tempo" },     "properties": [{ "id": "color", "value": { "mode": "fixed", "fixedColor": "blue"   } }] }
        ]
      },
      "description": "Count of detected intervals per month, stacked by type. Useful for verifying your training polarisation and seeing whether you're actually doing the VO2 / threshold work you plan."
    },
```

- [ ] **Step 2: Run dashboard structural tests**

```bash
python3 -m pytest tests/test_dashboards.py -q
```

Expected: all tests pass.

- [ ] **Step 3: Commit**

```bash
git add grafana/dashboards/all-time-progression.json
git commit -m "feat(dashboards): add Monthly Interval Distribution panel to All Time Progression"
```

---

## Task 16: Integration check — rebuild and verify

**Files:** none (verification only)

- [ ] **Step 1: Run the full test suite**

```bash
python3 -m pytest tests/ -q
```

Expected: all tests pass, no warnings about the new modules.

- [ ] **Step 2: Rebuild the ingestor container**

```bash
docker compose -f docker-compose.yml build velomate-ingestor
docker compose -f docker-compose.yml up -d velomate-ingestor
docker compose -f docker-compose.yml logs -f velomate-ingestor
```

Expected log lines (within the first minute after startup):
- `[fitness] Metrics version changed (7 → 9), recalculating everything...`
- `[fitness] Computed NP/EF/Work for N activities`
- `[fitness] Computed aerobic decoupling for M activities`
- A short delay while intervals are detected per ride
- `[fitness] Calculated K days of fitness data (CTL=..., ATL=..., TSB=...)`

Exit the log tail with Ctrl+C once the recalculation finishes cleanly.

- [ ] **Step 3: Database sanity checks**

```bash
docker compose exec velomate-postgres psql -U velomate -d velomate -c \
  "SELECT COUNT(*) AS n, COUNT(aerobic_decoupling) AS n_with_decoupling FROM activities;"
```

Expected: `n_with_decoupling` should be > 0 and proportional to rides with both power and HR streams.

```bash
docker compose exec velomate-postgres psql -U velomate -d velomate -c \
  "SELECT classification, COUNT(*) FROM ride_intervals GROUP BY 1 ORDER BY 2 DESC;"
```

Expected: a distribution of interval classes (sprint, anaerobic, vo2, threshold, sweetspot, tempo) counts. At least one bucket should be non-zero if you have any non-recovery rides in the dataset.

- [ ] **Step 4: Visual check of Grafana**

Open `http://localhost:3021` in a browser:
1. **Overview** dashboard — confirm "Avg Decoupling (steady)" stat card is present and shows a value (or "N/A" if no steady rides in the selected period).
2. **All Time Progression** dashboard — confirm "Aerobic Decoupling Trend" timeseries renders with a 10-ride rolling average and regression line. Confirm "Monthly Interval Distribution" stacked bar chart shows monthly interval counts by type.
3. **Activity Details** dashboard — open any recent activity with power and HR. Confirm the Aerobic Decoupling stat card still works (now reading from stored column, should show the same value as before). Confirm "Detected Intervals" table appears with at least some rows classified by type.

Screenshot each of the new panels for the PR description.

---

## Task 17: Push branch and open PR

**Files:** none (git/Gitea operation)

- [ ] **Step 1: Push the branch**

```bash
git push -u origin feat/ride-analytics-depth
```

- [ ] **Step 2: Open the PR**

```bash
tea pr create \
  --title "feat: ride analytics depth — stored decoupling + auto interval detection" \
  --description "$(cat <<'EOF'
## Summary

Implements Cluster A of the VeloMate features analysis (docs/features-analysis-06apr26.md):

- **Gap #1 — Cardiac drift trend over time**: promotes aerobic decoupling from per-activity Grafana SQL into a stored `activities.aerobic_decoupling` column; adds a steady-state-filtered trend panel to All Time Progression and a period-delta stat card to Overview.
- **Gap #6 — Auto interval detection**: new `ingestor/intervals.py` walks each ride's power stream, detects sustained efforts ≥ 30s, classifies them using Coggan-style buckets (sprint/anaerobic/vo2/threshold/sweetspot/tempo), and persists into a new `ride_intervals` table. Adds a detected-intervals table panel to Activity Details and a monthly-distribution bar chart to All Time Progression.

Both features follow the existing "compute in ingestor, store, read in Grafana" pattern. METRICS_VERSION bumped 7 → 9 to trigger full historical backfill.

## Schema changes

- New column: `activities.aerobic_decoupling FLOAT`
- New table: `ride_intervals (id, activity_id, start_offset_s, duration_s, avg_power, np, max_power, avg_hr, classification)` with indexes

## Test plan

- [x] Pure-function tests for `compute_decoupling` (8 cases in `tests/test_fitness.py`)
- [x] Pure-function tests for `classify_interval` and `detect_intervals` (14 cases in `tests/test_intervals.py`)
- [x] Dashboard structural tests (`tests/test_dashboards.py`) still pass
- [x] Full historical recalc completes cleanly on ingestor rebuild
- [x] Visual check of all new Grafana panels

## Rollback

Setting `METRICS_VERSION` back to `"7"` will not restore the old state cleanly — the column and table will remain. To fully roll back:

```sql
ALTER TABLE activities DROP COLUMN aerobic_decoupling;
DROP TABLE ride_intervals;
DELETE FROM sync_state WHERE key = 'metrics_version';
```

Then revert the branch.
EOF
)"
```

Expected: PR URL printed. Note it for the next step.

- [ ] **Step 3: Wait for Raven review**

Raven bot will post review comments on the PR. Apply the project PR protocol from `CLAUDE.md`:
- Fix findings that catch real bugs or security issues
- Skip theoretical / premature-optimisation findings
- If stabilised with no new real issues: proceed to merge

---

## Task 18: Merge and clean up

**Files:** none (git/Gitea operation)

- [ ] **Step 1: Squash merge on Gitea**

Via `tea`:

```bash
tea pr merge --style squash <PR-number>
```

Or via the Gitea web UI → "Squash and merge".

- [ ] **Step 2: Delete remote + local branch**

```bash
git checkout main
git pull
git branch -D feat/ride-analytics-depth
git push origin --delete feat/ride-analytics-depth
```

Expected: working tree clean, on main, feature branch gone.

- [ ] **Step 3: Sanity check prod ingestor**

```bash
docker compose logs velomate-ingestor --tail 50
```

Expected: no errors. Next scheduled `poll_strava` runs cleanly.

---

## Appendix — Self-review checklist

- [x] **Spec coverage:** Gap #1 (cardiac drift trend) covered by Tasks 2–9. Gap #6 (auto interval detection) covered by Tasks 10–15. Integration by Task 16. PR flow by Tasks 17–18.
- [x] **Placeholder scan:** Every SQL query, Python function body, and Grafana panel JSON is complete and copy-pasteable.
- [x] **Type consistency:** `compute_decoupling(power_samples, hr_samples) → float | None` used consistently; `detect_intervals(power_samples, ftp, ...) → list[dict]` with a stable dict shape referenced in Task 13 insert and Task 14 SQL.
- [x] **Commit granularity:** 13 commits across 16 implementation tasks (some tasks bundle a change + its test). Matches "frequent commits" guidance.
- [x] **TDD order:** Feature 1 test-first (Task 3 before Task 4). Feature 2 test-first (Task 11 before Task 12). Schema tasks precede the wiring tasks that reference the columns.
- [x] **No dependencies added:** scipy was suggested in the features analysis for a future CP/W' fit, but is not needed for Cluster A. `requirements.txt` remains unchanged.
- [x] **PR workflow respected:** Single branch `feat/ride-analytics-depth`, single PR, Raven review, squash merge, branch cleanup — matches the project's stated workflow in `CLAUDE.md`.
