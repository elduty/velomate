# Metric Consistency — Single Source of Truth

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the ingestor the sole authority for all derived metrics; Grafana only reads stored values. Fix all cross-dashboard inconsistencies.

**Architecture:** Add `intensity_factor`, `trimp`, and `variability_index` columns to activities. Compute them in `recalculate_fitness()` alongside existing NP/EF/TSS. Simplify every Grafana panel that currently recomputes or uses inconsistent fallback chains to read from activities/sync_state instead. Bump METRICS_VERSION to trigger full recalc.

**Tech Stack:** Python (ingestor), PostgreSQL DDL, Grafana dashboard JSON, pytest

---

## File Map

| File | Action | Responsibility |
|------|--------|----------------|
| `ingestor/db.py:84-98` | Modify | Add `intensity_factor`, `trimp`, `variability_index` columns |
| `ingestor/fitness.py` | Modify | Add `compute_trimp()`, `compute_if()`, `compute_vi()`; store all in `recalculate_fitness()` |
| `ingestor/main.py:29-30` | Modify | Read VELOMATE_RESTING_HR env var, persist to sync_state |
| `docker-compose.yml:29-30` | Modify | Forward VELOMATE_RESTING_HR env var |
| `grafana/dashboards/activity.json` | Modify | NP/EF/IF/VI/TRIMP panels read stored values; standardise FTP/HR fallbacks; add Z7; fix decoupling |
| `grafana/dashboards/all-time-progression.json` | Modify | Standardise monthly power zone FTP source; add Z7; standardise HR fallback |
| `grafana/dashboards/overview.json` | No change needed | Already reads stored values |
| `tests/test_fitness.py` | Modify | Add tests for compute_trimp, compute_if, compute_vi |
| `tests/test_fitness_recalc.py` | Modify | Update mock cursor sequence for new columns |

---

### Task 1: Add new columns to schema

**Files:**
- Modify: `ingestor/db.py:84-98` (ALTER TABLE block)

- [ ] **Step 1: Add three ALTER TABLE statements**

After the existing `ride_ftp` ALTER, add:

```python
            ALTER TABLE activities ADD COLUMN IF NOT EXISTS intensity_factor FLOAT;
            ALTER TABLE activities ADD COLUMN IF NOT EXISTS trimp FLOAT;
            ALTER TABLE activities ADD COLUMN IF NOT EXISTS variability_index FLOAT;
```

- [ ] **Step 2: Run tests**

Run: `pytest tests/test_velomate_db.py -v`
Expected: PASS (schema tests don't hit real DB)

- [ ] **Step 3: Commit**

```bash
git add ingestor/db.py
git commit -m "feat: add intensity_factor, trimp, variability_index columns"
```

---

### Task 2: Add pure compute functions for TRIMP, IF, VI

**Files:**
- Modify: `ingestor/fitness.py` (add after `compute_ef`)
- Modify: `tests/test_fitness.py` (add test classes)

- [ ] **Step 1: Write failing tests**

In `tests/test_fitness.py`, add:

```python
from fitness import calculate_tss, calculate_tss_power, compute_trimp, compute_if, compute_vi


class TestComputeTrimp:
    """Banister TRIMP with HRR capped at 1.0."""

    def test_normal(self):
        """6985 1-sec samples at constant 144bpm, max=175, rest=50."""
        import math
        hrr = (144 - 50) / (175 - 50)  # 0.752
        expected_per_sample = (1 / 60) * hrr * 0.64 * math.exp(1.92 * hrr)
        expected = round(expected_per_sample * 6985, 1)
        result = compute_trimp([144] * 6985, max_hr=175, resting_hr=50)
        assert result == expected

    def test_hrr_capped_at_one(self):
        """HR above max_hr should be capped at HRR=1.0."""
        import math
        capped = (1 / 60) * 1.0 * 0.64 * math.exp(1.92 * 1.0)
        expected = round(capped * 60, 1)  # 60 samples
        result = compute_trimp([200] * 60, max_hr=175, resting_hr=50)
        assert result == expected

    def test_hr_below_resting_excluded(self):
        """Samples at or below resting HR contribute 0."""
        result = compute_trimp([40, 45, 50] * 20, max_hr=175, resting_hr=50)
        assert result == 0.0

    def test_empty(self):
        assert compute_trimp([], max_hr=175, resting_hr=50) == 0.0

    def test_none_max_hr(self):
        assert compute_trimp([144] * 60, max_hr=0, resting_hr=50) == 0.0


class TestComputeIF:
    """IF = NP / FTP."""

    def test_normal(self):
        assert compute_if(118, 250) == 0.47

    def test_high_intensity(self):
        assert compute_if(300, 250) == 1.2

    def test_zero_ftp(self):
        assert compute_if(200, 0) is None

    def test_none_np(self):
        assert compute_if(None, 250) is None


class TestComputeVI:
    """VI = NP / avg_power."""

    def test_normal(self):
        assert compute_vi(118, 109) == 1.08

    def test_steady_ride(self):
        assert compute_vi(200, 200) == 1.0

    def test_zero_avg(self):
        assert compute_vi(200, 0) is None

    def test_none_np(self):
        assert compute_vi(None, 150) is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_fitness.py -v`
Expected: FAIL — `compute_trimp`, `compute_if`, `compute_vi` not defined

- [ ] **Step 3: Implement the functions**

In `ingestor/fitness.py`, add after `compute_ef`:

```python
import math

def compute_trimp(hr_samples: list, max_hr: int, resting_hr: int) -> float:
    """Banister TRIMP from 1-second HR samples.
    TRIMP = SUM((1/60) * HRR * 0.64 * exp(1.92 * HRR))
    HRR = (HR - resting) / (max - resting), capped at 1.0.
    Male coefficients (k=0.64, c=1.92).
    """
    if not hr_samples or not max_hr or max_hr <= resting_hr:
        return 0.0
    hr_range = max_hr - resting_hr
    total = 0.0
    for hr in hr_samples:
        if hr <= resting_hr:
            continue
        hrr = min((hr - resting_hr) / hr_range, 1.0)
        total += (1 / 60) * hrr * 0.64 * math.exp(1.92 * hrr)
    return round(total, 1)


def compute_if(np: float, ftp: int) -> float | None:
    """Intensity Factor = NP / FTP."""
    if not np or not ftp or ftp <= 0:
        return None
    return round(np / ftp, 2)


def compute_vi(np: float, avg_power: int) -> float | None:
    """Variability Index = NP / avg_power."""
    if not np or not avg_power or avg_power <= 0:
        return None
    return round(np / avg_power, 2)
```

- [ ] **Step 4: Run tests**

Run: `pytest tests/test_fitness.py -v`
Expected: ALL PASS

- [ ] **Step 5: Commit**

```bash
git add ingestor/fitness.py tests/test_fitness.py
git commit -m "feat: add compute_trimp, compute_if, compute_vi functions"
```

---

### Task 3: Store IF/TRIMP/VI in recalculate_fitness

**Files:**
- Modify: `ingestor/fitness.py:139-150` (metrics version), `ingestor/fitness.py:191-197` (NP update), `ingestor/fitness.py:246-272` (TSS loop)
- Modify: `ingestor/main.py:120-121` (resting HR env var)
- Modify: `docker-compose.yml:29-30` (env var)

- [ ] **Step 1: Bump METRICS_VERSION to "5" and add new columns to reset**

In `ingestor/fitness.py`, change:

```python
METRICS_VERSION = "5"  # v5: store IF, TRIMP, VI; single source of truth
```

In the metrics version reset block (`cur.execute("UPDATE activities SET ..."`), add the new columns:

```python
cur.execute("UPDATE activities SET tss = NULL, np = NULL, ef = NULL, work_kj = NULL, ride_ftp = NULL, intensity_factor = NULL, trimp = NULL, variability_index = NULL")
```

- [ ] **Step 2: Store VI and IF alongside NP/EF**

In the NP computation loop (after `ef_val = compute_ef(np_val, avg_hr)`), add:

```python
            vi_val = compute_vi(np_val, avg_power)
            # IF requires ride_ftp which is set later — defer to TSS step
```

Update the UPDATE statement to include VI:

```python
                cur.execute("""
                    UPDATE activities SET np = %s, ef = %s, work_kj = %s, variability_index = %s WHERE id = %s
                """, (np_val, ef_val, work_val, vi_val, act_id))
```

- [ ] **Step 3: Add resting_hr + max_hr to recalculate_fitness for TRIMP**

At the top of `recalculate_fitness`, after the existing `threshold_hr` and `ftp` resolution, add resting HR resolution:

```python
    env_rhr = os.environ.get("VELOMATE_RESTING_HR", "")
    try:
        rhr_val = int(env_rhr) if env_rhr else 0
    except ValueError:
        rhr_val = 0
    resting_hr = rhr_val if rhr_val > 0 else 50
    print(f"[fitness] Resting HR: {resting_hr} {'(configured)' if rhr_val > 0 else '(default 50 bpm)'}")
```

- [ ] **Step 4: Compute IF, TRIMP in the TSS loop**

In the TSS computation loop (after `tss = calculate_tss_power(...)` / fallbacks), add IF:

```python
        if_val = compute_if(np_val, act_ftp) if np_val and np_val > 0 else None
```

Change `tss_updates` to include IF, and update the batch SQL.

After the TSS batch update, add a TRIMP computation pass that reads HR streams:

```python
    # Step 4: Compute TRIMP for activities that don't have it yet
    print("[fitness] Computing TRIMP...")
    with conn.cursor() as cur:
        cur.execute("""
            SELECT a.id FROM activities a
            WHERE a.trimp IS NULL AND a.date IS NOT NULL
              AND EXISTS (SELECT 1 FROM activity_streams s WHERE s.activity_id = a.id AND s.hr IS NOT NULL)
        """)
        trimp_ids = [row[0] for row in cur.fetchall()]

    trimp_count = 0
    for act_id in trimp_ids:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT hr FROM activity_streams
                WHERE activity_id = %s AND hr IS NOT NULL
                ORDER BY time_offset
            """, (act_id,))
            hr_samples = [row[0] for row in cur.fetchall()]

        trimp_val = compute_trimp(hr_samples, threshold_hr, resting_hr)
        with conn.cursor() as cur:
            cur.execute("UPDATE activities SET trimp = %s WHERE id = %s", (trimp_val, act_id))
        trimp_count += 1

    print(f"[fitness] Computed TRIMP for {trimp_count} activities")
```

Note: TRIMP uses `threshold_hr` (max HR) not `ftp`. The `resting_hr` is the new parameter.

- [ ] **Step 5: Update TSS batch to also store IF**

Change the batch update:

```python
    tss_updates = []
    for act_id, duration_s, avg_hr, avg_power, np_val, ride_ftp_val in activity_rows:
        act_ftp = ride_ftp_val if ride_ftp_val and ride_ftp_val > 0 else ftp
        if np_val and np_val > 0:
            tss = calculate_tss_power(duration_s, np_val, act_ftp)
        elif avg_power and avg_power > 0:
            tss = calculate_tss_power(duration_s, avg_power, act_ftp)
        elif avg_hr and avg_hr > 0:
            tss = calculate_tss(duration_s, avg_hr, threshold_hr)
        else:
            tss = 0
        if_val = compute_if(np_val, act_ftp) if np_val and np_val > 0 else None
        tss_updates.append((round(tss, 1), if_val, act_id))

    with conn.cursor() as cur:
        psycopg2.extras.execute_batch(
            cur, "UPDATE activities SET tss = %s, intensity_factor = %s WHERE id = %s", tss_updates
        )
```

- [ ] **Step 6: Forward VELOMATE_RESTING_HR in docker-compose.yml**

Add after the VELOMATE_FTP line:

```yaml
      VELOMATE_RESTING_HR: ${VELOMATE_RESTING_HR:-0}
```

- [ ] **Step 7: Run tests and fix test_fitness_recalc.py**

The mock cursor sequence in `_make_conn` needs updating to account for:
- New columns in the UPDATE statements
- The TRIMP computation pass (new cursor calls)

Run: `pytest tests/ -v`
Fix any failures from the changed cursor sequence.

- [ ] **Step 8: Commit**

```bash
git add ingestor/fitness.py ingestor/main.py docker-compose.yml tests/test_fitness_recalc.py
git commit -m "feat: compute IF, TRIMP, VI in ingestor — single source of truth"
```

---

### Task 4: Grafana activity panels — read stored values

**Files:**
- Modify: `grafana/dashboards/activity.json`

Replace these 5 panels' rawSql to read from the activities table:

- [ ] **Step 1: NP panel**

Replace the rolling window CTE with:

```sql
SELECT ROUND(np::numeric, 0) AS "NP (W)" FROM activities WHERE id = ${activity_id} AND np IS NOT NULL;
```

- [ ] **Step 2: IF panel**

Replace the entire FTP CTE + NP recomputation with:

```sql
SELECT ROUND(intensity_factor::numeric, 2) AS "IF" FROM activities WHERE id = ${activity_id} AND intensity_factor IS NOT NULL;
```

- [ ] **Step 3: VI panel**

Replace with:

```sql
SELECT ROUND(variability_index::numeric, 2) AS "VI" FROM activities WHERE id = ${activity_id} AND variability_index IS NOT NULL;
```

- [ ] **Step 4: EF panel**

Replace with:

```sql
SELECT ROUND(ef::numeric, 2) AS "EF" FROM activities WHERE id = ${activity_id} AND ef IS NOT NULL;
```

- [ ] **Step 5: TRIMP panel**

Replace the entire Banister CTE with:

```sql
SELECT ROUND(trimp::numeric, 0) AS "TRIMP" FROM activities WHERE id = ${activity_id} AND trimp IS NOT NULL;
```

- [ ] **Step 6: Run dashboard tests**

Run: `pytest tests/test_dashboards.py -v`
Expected: PASS (structural checks only)

- [ ] **Step 7: Commit**

```bash
git add grafana/dashboards/activity.json
git commit -m "fix: activity panels read stored NP/EF/IF/VI/TRIMP — no recomputation"
```

---

### Task 5: Standardise FTP references in Grafana

**Files:**
- Modify: `grafana/dashboards/activity.json` (Power Zones, Power Zones by KM, Power Distribution)
- Modify: `grafana/dashboards/all-time-progression.json` (Monthly Power Zones)

All power zone panels should use the same simple FTP source:

```sql
SELECT COALESCE(
  (SELECT value::numeric FROM sync_state WHERE key = 'configured_ftp' AND value::numeric > 0),
  (SELECT value::numeric FROM sync_state WHERE key = 'estimated_ftp' AND value::numeric > 0),
  150
) AS val
```

No inline 90-day recalculation. No percentile fallback. The ingestor already computed and stored the right value.

- [ ] **Step 1: Power Zones bar chart (activity.json)**

Replace the FTP CTE. Keep the 6-zone → 7-zone fix (add Z7 Neuromuscular, see Task 7).

- [ ] **Step 2: Power Zones by KM (activity.json)**

Replace the FTP CTE. Also add Z7.

- [ ] **Step 3: Power Distribution (activity.json)**

Replace the FTP CTE. Z7 already exists here — keep it.

- [ ] **Step 4: Monthly Power Zone Distribution (all-time-progression.json)**

Replace the FTP subquery to include `configured_ftp` check first. Also add Z7.

The `CROSS JOIN` becomes:

```sql
CROSS JOIN (
  SELECT COALESCE(
    (SELECT value::numeric FROM sync_state WHERE key = 'configured_ftp' AND value::numeric > 0),
    (SELECT value::numeric FROM sync_state WHERE key = 'estimated_ftp' AND value::numeric > 0),
    150
  ) AS val
) ftp
```

- [ ] **Step 5: Run dashboard tests**

Run: `pytest tests/test_dashboards.py -v`

- [ ] **Step 6: Commit**

```bash
git add grafana/dashboards/activity.json grafana/dashboards/all-time-progression.json
git commit -m "fix: standardise FTP source — configured → estimated → 150 everywhere"
```

---

### Task 6: Standardise HR fallback chains

**Files:**
- Modify: `grafana/dashboards/activity.json` (HR Zones bar chart, HR Distribution)

These two panels are missing the safe default of 185.

- [ ] **Step 1: HR Zones bar chart**

Change the `lthr` CTE from:

```sql
SELECT COALESCE(
  (SELECT value::numeric FROM sync_state WHERE key = 'configured_max_hr' AND value::numeric > 0),
  (SELECT percentile_cont(0.95) WITHIN GROUP (ORDER BY max_hr) FROM activities WHERE max_hr IS NOT NULL AND max_hr > 0)
) AS thr
```

to:

```sql
SELECT COALESCE(
  (SELECT value::numeric FROM sync_state WHERE key = 'configured_max_hr' AND value::numeric > 0),
  (SELECT percentile_cont(0.95) WITHIN GROUP (ORDER BY max_hr) FROM activities WHERE max_hr IS NOT NULL AND max_hr > 0),
  185
) AS thr
```

- [ ] **Step 2: HR Distribution**

Same change — add `, 185` to the COALESCE.

- [ ] **Step 3: Commit**

```bash
git add grafana/dashboards/activity.json
git commit -m "fix: add default 185 fallback to HR Zones and HR Distribution panels"
```

---

### Task 7: Add Z7 Neuromuscular to all power zone panels

**Files:**
- Modify: `grafana/dashboards/activity.json` (Power Zones bar chart, Power Zones by KM)
- Modify: `grafana/dashboards/all-time-progression.json` (Monthly Power Zones)

The Coggan 7-zone model:
- Z1-Z5: unchanged
- Z6 Anaerobic: 120-150% FTP
- Z7 Neuromuscular: >150% FTP

Currently Z6 is `ELSE` (>120%). Change to split at 150%.

- [ ] **Step 1: Power Zones bar chart (activity.json)**

Change the CASE:
```sql
WHEN s.power < ftp.ftp_val * 1.20 THEN 'z5'
WHEN s.power < ftp.ftp_val * 1.50 THEN 'z6'
ELSE 'z7'
```

Add to the SELECT:
```sql
COALESCE((SELECT COUNT(*) FROM data WHERE z='z7'), 0) AS "Z7 Neuromuscular"
```

- [ ] **Step 2: Power Zones by KM (activity.json)**

Same CASE change and add Z7 to SELECT columns.

- [ ] **Step 3: Monthly Power Zones (all-time-progression.json)**

Same CASE change and add Z7 to SELECT columns.

- [ ] **Step 4: Commit**

```bash
git add grafana/dashboards/activity.json grafana/dashboards/all-time-progression.json
git commit -m "fix: add Z7 Neuromuscular (>150% FTP) to all power zone panels"
```

---

### Task 8: Fix Aerobic Decoupling to include coasting

**Files:**
- Modify: `grafana/dashboards/activity.json` (Aerobic Decoupling panel)

The current query filters `s.power > 0`, excluding coasting samples. This skews EF per half when coasting distribution is uneven. Standard decoupling splits by time and includes all samples.

- [ ] **Step 1: Update the decoupling query**

Change the WHERE clause from:
```sql
AND s.power IS NOT NULL AND s.power > 0
AND s.hr IS NOT NULL AND s.hr > 0
```

to:
```sql
AND s.hr IS NOT NULL AND s.hr > 0
AND s.power IS NOT NULL
```

This includes zero-power (coasting) in the avg_power per half, matching the standard time-based decoupling definition. HR > 0 filter stays (need valid HR for EF).

- [ ] **Step 2: Commit**

```bash
git add grafana/dashboards/activity.json
git commit -m "fix: aerobic decoupling includes coasting samples"
```

---

### Task 9: Update test_fitness_recalc.py for new cursor sequence

**Files:**
- Modify: `tests/test_fitness_recalc.py`

The `_make_conn` mock needs to account for:
1. New columns in UPDATE SQL (variability_index in NP step, intensity_factor in TSS batch)
2. New TRIMP computation pass (SELECT ids, then per-activity SELECT hr + UPDATE trimp)
3. METRICS_VERSION now "5"

- [ ] **Step 1: Update `_make_conn` docstring and cursor sequence**

The new cursor sequence after the TSS batch:
```
  3+2*N+3+B: execute_batch TSS+IF update
  3+2*N+4+B: SELECT activities needing TRIMP
  3+2*N+5+B..+5+B+2*T: per-TRIMP-activity (SELECT hr, UPDATE trimp)
  3+2*N+6+B+2*T: final TSS readback
  ...
```

Add `trimp_activity_ids=[]` parameter to `_make_conn`. Wire up the new cursors.

- [ ] **Step 2: Update METRICS_VERSION reference**

Change `from fitness import recalculate_fitness, compute_ef, METRICS_VERSION` — still works since we just changed the value.

- [ ] **Step 3: Run all tests**

Run: `pytest tests/ -v`
Expected: ALL PASS

- [ ] **Step 4: Commit**

```bash
git add tests/test_fitness_recalc.py
git commit -m "test: update recalc mocks for IF/TRIMP/VI cursor sequence"
```

---

### Task 10: Update CLAUDE.md

**Files:**
- Modify: `CLAUDE.md`

- [ ] **Step 1: Update Metrics section**

Add IF, TRIMP, VI to the documented metrics. Note single-source-of-truth architecture. Update METRICS_VERSION to "5".

- [ ] **Step 2: Commit**

```bash
git add CLAUDE.md
git commit -m "docs: update CLAUDE.md for metric consistency changes"
```
