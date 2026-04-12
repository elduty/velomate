# W'bal Time Series Per Ride — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Compute per-second W'bal (remaining anaerobic work capacity) for every ride with power data and display it on Activity Details.

**Architecture:** New pure function `compute_wbal` in `ingestor/critical_power.py` implements the Skiba differential model with GoldenCheetah tau. New orchestrator `compute_wbal_for_rides` in `ingestor/fitness.py` reads the latest CP/W' from `cp_estimates`, iterates rides missing `w_bal`, and writes per-second values back to `activity_streams`. New `w_bal` column on `activity_streams`. W'bal timeseries panel + two stat cards on Activity Details.

**Tech Stack:** Python 3.11, numpy, psycopg2, PostgreSQL 15, Grafana 12.4. No new dependencies.

**Spec:** `docs/superpowers/specs/2026-04-12-wbal-time-series-design.md`

---

## File Structure

| File | Action | Responsibility |
|---|---|---|
| `ingestor/critical_power.py` | Modify | Add `compute_wbal` pure function |
| `ingestor/fitness.py` | Modify | Add `compute_wbal_for_rides` orchestrator, wire into `recalculate_fitness` |
| `ingestor/db.py` | Modify | Add `w_bal FLOAT` column to `activity_streams` |
| `tests/test_critical_power.py` | Modify | Add tests for `compute_wbal` |
| `tests/test_fitness_recalc.py` | Modify | Update mock cursor sequence |
| `grafana/dashboards/activity.json` | Modify | Add W'bal timeseries + Min W'bal stat + Time below 25% stat |
| `CLAUDE.md` | Modify | Add W'bal to Metrics section |
| `README.md` | Modify | Add W'bal to metrics list |

---

## Task 1: Schema — `w_bal` column on `activity_streams`

**Files:**
- Modify: `ingestor/db.py`

- [ ] **Step 1: Add the column DDL**

Find the existing `activity_streams` index line in `ingestor/db.py`:
```python
            CREATE INDEX IF NOT EXISTS idx_streams_power ON activity_streams(activity_id, time_offset) WHERE power IS NOT NULL;
```

Before that line, add:

```python
            ALTER TABLE activity_streams ADD COLUMN IF NOT EXISTS w_bal FLOAT;
```

- [ ] **Step 2: Commit**

```bash
git add ingestor/db.py
git commit -m "feat(db): add w_bal column to activity_streams"
```

---

## Task 2: Pure function — `compute_wbal` (TDD)

**Files:**
- Modify: `ingestor/critical_power.py`
- Modify: `tests/test_critical_power.py`

- [ ] **Step 1: Add failing tests**

Add `compute_wbal` to the **existing** import at the top of `tests/test_critical_power.py` (do NOT create a duplicate import block):

```python
from critical_power import (
    compute_mean_maximal_power,
    fit_monod_scherrer,
    assess_fit_quality,
    compute_wbal,  # ← add this line to the existing import
)
```

Then append the test class at the bottom of the file:

```python
class TestComputeWbal:
    CP = 200.0       # watts
    W_PRIME = 15000.0  # joules (15 kJ)

    def test_constant_power_below_cp_no_drain(self):
        """Riding at CP-50W for 60s should leave W'bal at W'."""
        powers = [150.0] * 60
        wbal = compute_wbal(powers, self.CP, self.W_PRIME)
        assert len(wbal) == 60
        # W'bal should stay very close to W' (slight recovery overshoot is clamped)
        assert wbal[-1] == pytest.approx(self.W_PRIME, rel=0.01)

    def test_constant_power_above_cp_drains(self):
        """Riding at CP+100W for 60s should drain 6000J from W'."""
        powers = [300.0] * 60  # 100W above CP for 60s = 6000J drained
        wbal = compute_wbal(powers, self.CP, self.W_PRIME)
        assert len(wbal) == 60
        # W'bal should be approximately W' - 6000 = 9000J
        assert wbal[-1] == pytest.approx(9000.0, abs=1.0)

    def test_drain_then_recovery(self):
        """30s above CP then 30s below CP should show drain then partial recovery."""
        powers = [300.0] * 30 + [100.0] * 30
        wbal = compute_wbal(powers, self.CP, self.W_PRIME)
        assert len(wbal) == 60
        # At second 30: drained by 30 * 100 = 3000J → ~12000J
        mid = wbal[29]
        assert mid == pytest.approx(12000.0, abs=1.0)
        # At second 60: should have recovered somewhat above mid
        assert wbal[-1] > mid

    def test_wbal_never_below_zero(self):
        """Sustained hard effort should drain to 0 but not go negative."""
        powers = [400.0] * 200  # 200W above CP for 200s = 40000J >> W'=15000
        wbal = compute_wbal(powers, self.CP, self.W_PRIME)
        assert min(wbal) >= 0.0

    def test_wbal_never_above_w_prime(self):
        """Recovery should not push W'bal above W'."""
        powers = [300.0] * 30 + [50.0] * 300  # drain then long recovery
        wbal = compute_wbal(powers, self.CP, self.W_PRIME)
        assert max(wbal) <= self.W_PRIME + 0.01  # float tolerance

    def test_empty_stream(self):
        wbal = compute_wbal([], 200.0, 15000.0)
        assert wbal == []

    def test_known_values(self):
        """Hand-computed example: 3 seconds above CP then 2 below.
        CP=200, W'=10000J.
        t=0: W'bal = 10000 (start)
        t=1: P=250 → drain 50J → W'bal = 9950
        t=2: P=250 → drain 50J → W'bal = 9900
        t=3: P=250 → drain 50J → W'bal = 9850
        t=4: P=100 → recover. tau = 546*exp(-0.01*(200-100))+316 = 546*exp(-1)+316 ≈ 546*0.3679+316 ≈ 516.9
              W'bal = 10000 - (10000 - 9850) * exp(-1/516.9) ≈ 10000 - 150*0.99807 ≈ 9850.3
        t=5: P=100 → same tau. W'bal ≈ 10000 - (10000-9850.3)*exp(-1/516.9) ≈ 9850.6
        """
        powers = [250.0, 250.0, 250.0, 100.0, 100.0]
        wbal = compute_wbal(powers, 200.0, 10000.0)
        assert len(wbal) == 5
        assert wbal[0] == pytest.approx(10000.0 - 50.0, abs=1.0)  # 9950
        assert wbal[2] == pytest.approx(10000.0 - 150.0, abs=1.0)  # 9850
        # After recovery, should be slightly above 9850
        assert wbal[4] > 9850.0
```

- [ ] **Step 2: Run tests to confirm they fail**

Run: `python3 -m pytest tests/test_critical_power.py::TestComputeWbal -v`
Expected: ImportError (compute_wbal not yet defined)

- [ ] **Step 3: Implement `compute_wbal`**

Append to `ingestor/critical_power.py`:

```python
import math


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
            # Draining: lose (P - CP) joules this second
            current = current - (p - cp)
        else:
            # Recovering: exponential refill toward W' with Skiba tau
            tau = 546.0 * math.exp(-0.01 * (cp - p)) + 316.0
            current = w_prime_j - (w_prime_j - current) * math.exp(-1.0 / tau)

        # Clamp to [0, W']
        current = max(0.0, min(current, w_prime_j))
        wbal.append(current)

    return wbal
```

Note: the `import math` should go at the top of the file alongside the existing `import numpy as np`. If it's already there, skip.

- [ ] **Step 4: Run tests to confirm they pass**

Run: `python3 -m pytest tests/test_critical_power.py -v`
Expected: All tests pass (13 existing + 7 new = 20).

- [ ] **Step 5: Commit**

```bash
git add ingestor/critical_power.py tests/test_critical_power.py
git commit -m "feat(cp): compute_wbal — Skiba differential W'bal per second"
```

---

## Task 3: Orchestrator — `compute_wbal_for_rides` in `fitness.py`

**Files:**
- Modify: `ingestor/fitness.py`

- [ ] **Step 1: Add `compute_wbal_for_rides` after `compute_cp_estimate`**

Append to `ingestor/fitness.py`, immediately after `compute_cp_estimate`:

```python
def compute_wbal_for_rides(conn) -> int:
    """Compute W'bal for rides that don't have it yet.

    Reads CP/W' from the latest cp_estimates row. For rides with power
    streams where w_bal IS NULL, computes per-second W'bal via Skiba
    differential and writes it back to activity_streams.

    Returns the number of rides processed. Returns 0 if no CP estimate
    is available or no rides need processing.
    """
    from critical_power import compute_wbal
    import psycopg2.extras

    # Get latest CP estimate
    with conn.cursor() as cur:
        cur.execute("""
            SELECT cp_watts, w_prime_kj, fallback_ftp, source
            FROM cp_estimates ORDER BY date DESC LIMIT 1
        """)
        row = cur.fetchone()

    if row is None:
        print("[fitness] No CP estimates — skipping W'bal")
        return 0

    cp_watts, w_prime_kj, fallback_ftp, source = row

    # Determine CP and W' to use
    if source == "cp" and cp_watts is not None:
        cp = cp_watts
    elif fallback_ftp is not None:
        cp = float(fallback_ftp)
    else:
        print("[fitness] No usable CP value — skipping W'bal")
        return 0

    # W' in joules — use fitted value or 20kJ default (Skiba standard)
    w_prime_j = (w_prime_kj * 1000.0) if w_prime_kj is not None else 20000.0

    # Find rides with power streams that need W'bal
    with conn.cursor() as cur:
        cur.execute("""
            SELECT DISTINCT s.activity_id
            FROM activity_streams s
            WHERE s.power IS NOT NULL
              AND s.w_bal IS NULL
            ORDER BY s.activity_id
        """)
        ride_ids = [row[0] for row in cur.fetchall()]

    if not ride_ids:
        return 0

    count = 0
    for act_id in ride_ids:
        try:
            # Read power stream — COALESCE NULL power to 0 so coasting seconds
            # are modeled as recovery (0W < CP) rather than creating time gaps.
            # Matches the project convention: "Includes zero-power (coasting)".
            # Note: assumes consecutive 1-second samples (same assumption as NP
            # computation). Gaps in time_offset are not detected — if a sensor
            # drops samples, drain/recovery duration is under-counted for that
            # interval. This is consistent with the rest of the pipeline.
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT time_offset, COALESCE(power, 0) AS power
                    FROM activity_streams
                    WHERE activity_id = %s
                    ORDER BY time_offset
                """, (act_id,))
                rows = cur.fetchall()

            if not rows:
                continue

            offsets = [r[0] for r in rows]
            powers = [float(r[1]) for r in rows]

            # Compute W'bal
            wbal = compute_wbal(powers, cp, w_prime_j)

            # Batch update w_bal for each time_offset
            updates = [(wbal[i], act_id, offsets[i]) for i in range(len(wbal))]
            with conn.cursor() as cur:
                psycopg2.extras.execute_batch(cur, """
                    UPDATE activity_streams SET w_bal = %s
                    WHERE activity_id = %s AND time_offset = %s
                """, updates, page_size=1000)
        except Exception as e:
            print(f"[fitness] W'bal failed for activity {act_id} (skipping): {e}")
            continue

        count += 1

    return count
```

- [ ] **Step 2: Wire into `recalculate_fitness`**

Find the CP estimate call in `recalculate_fitness`:
```python
    print("[fitness] Computing CP / W' estimate...")
    try:
        compute_cp_estimate(conn, fallback_ftp=auto_ftp)
    except Exception as e:
        print(f"[fitness] CP estimate failed (non-fatal): {e}")
```

Immediately after it, add:

```python
    # Step 7: Compute W'bal for rides missing it
    print("[fitness] Computing W'bal...")
    try:
        wbal_count = compute_wbal_for_rides(conn)
        if wbal_count > 0:
            print(f"[fitness] Computed W'bal for {wbal_count} rides")
    except Exception as e:
        print(f"[fitness] W'bal computation failed (non-fatal): {e}")
```

- [ ] **Step 3: Verify syntax**

Run: `python3 -c "import ast; ast.parse(open('ingestor/fitness.py').read()); print('OK')"`
Expected: `OK`

- [ ] **Step 4: Commit**

```bash
git add ingestor/fitness.py
git commit -m "feat(cp): compute_wbal_for_rides orchestrator + wire into recalculate_fitness"
```

---

## Task 4: Update `test_fitness_recalc.py` mock cursors

**Files:**
- Modify: `tests/test_fitness_recalc.py`

The new `compute_wbal_for_rides` adds cursors after `compute_cp_estimate`. The existing catch-all `else` branch in `make_cursor` (added in PR #108) already returns `None`/`[]` for unknown cursors, which will cause `compute_wbal_for_rides` to short-circuit on the "no CP estimates" check (`cur.fetchone()` returns `None`).

- [ ] **Step 1: Verify existing tests still pass**

Run: `python3 -m pytest tests/test_fitness_recalc.py -q`
Expected: 23 passed. If the catch-all handles it, no changes needed.

- [ ] **Step 2: If tests pass, commit (no-op commit if no changes)**

If tests pass without changes, skip this commit. If any test fails, update the catch-all to handle the new cursor pattern.

---

## Task 5: Activity Details dashboard panels

**Files:**
- Modify: `grafana/dashboards/activity.json`

Three new panels:
1. **Min W'bal** stat card — new stat row at y=24 (shift everything below by +3)
2. **Time below 25%** stat card — same row, x=6
3. **W'bal timeseries** — in Ride Telemetry section after Cadence & Grade

- [ ] **Step 1: Run a Python script to add all three panels**

```bash
python3 <<'PYEOF'
import json, copy

with open('grafana/dashboards/activity.json') as f:
    d = json.load(f)

# --- Find insertion points by panel ID, not hardcoded Y ---
# Stat row goes immediately after the W/kg panel (id=37)
wkg_panel = next(p for p in d['panels'] if p.get('id') == 37)
stat_insert_y = wkg_panel['gridPos']['y'] + wkg_panel['gridPos']['h']

# Shift everything from stat_insert_y down by 3 to make room for stat row
for p in d['panels']:
    gp = p.get('gridPos', {})
    if gp.get('y', 0) >= stat_insert_y:
        gp['y'] += 3
    if p.get('type') == 'row':
        for c in p.get('panels', []) or []:
            cgp = c.get('gridPos', {})
            if cgp.get('y', 0) >= stat_insert_y:
                cgp['y'] += 3

# --- Min W'bal stat (id=1200) — built from scratch, not deepcopy ---
# Avoids inheriting stale config from template panels (datasource,
# pluginVersion, links, maxDataPoints, transformations).
min_wbal = {
    "id": 1200,
    "type": "stat",
    "datasource": {"type": "postgres", "uid": "velomate"},
}
min_wbal['title'] = "Min W'bal"
min_wbal['description'] = (
    "Lowest W'bal during the ride — how close to empty your\n"
    "anaerobic battery got.\n\n"
    "Computed via Skiba differential model using the latest CP/W'\n"
    "estimate. Requires power data."
)
min_wbal['gridPos'] = {"x": 0, "y": stat_insert_y, "w": 6, "h": 3}
min_wbal['targets'] = [{
    "rawSql": "SELECT ROUND((MIN(s.w_bal) / 1000.0)::numeric, 1) AS \"Min W'bal (kJ)\" FROM activity_streams s WHERE s.activity_id = ${activity_id} AND s.w_bal IS NOT NULL;",
    "format": "table",
    "refId": "A"
}]
min_wbal['fieldConfig'] = {
    "defaults": {
        "noValue": "No W'bal",
        "color": {"mode": "fixed", "fixedColor": "dark-red"},
        "unit": "kj",
        "decimals": 1,
        "thresholds": {
            "mode": "absolute",
            "steps": [{"color": "dark-red", "value": None}]
        }
    }
}
min_wbal['options'] = {
    "colorMode": "background",
    "graphMode": "none",
    "textMode": "auto",
    "reduceOptions": {"calcs": ["lastNotNull"]}
}

# --- Time below 25% stat (id=1201) ---
time_below = copy.deepcopy(min_wbal)
time_below['id'] = 1201
time_below['title'] = "Time W'bal < 25%"
time_below['description'] = (
    "Percentage of ride time where W'bal was below 25% of W'.\n"
    "Shows how much of the ride was 'in the red.'\n\n"
    "Note: uses the latest W' estimate for the 25% threshold —\n"
    "a known approximation (see spec)."
)
time_below['gridPos'] = {"x": 6, "y": stat_insert_y, "w": 6, "h": 3}
time_below['targets'] = [{
    "rawSql": (
        "SELECT ROUND(\n"
        "  (COUNT(*) FILTER (WHERE s.w_bal < (\n"
        "    SELECT COALESCE(w_prime_kj, 20.0) * 1000.0 * 0.25\n"
        "    FROM cp_estimates ORDER BY date DESC LIMIT 1\n"
        "  )) * 100.0 / NULLIF(COUNT(*), 0))::numeric, 1\n"
        ") AS \"Time < 25% (%)\"\n"
        "FROM activity_streams s\n"
        "WHERE s.activity_id = ${activity_id}\n"
        "  AND s.w_bal IS NOT NULL;"
    ),
    "format": "table",
    "refId": "A"
}]
time_below['fieldConfig'] = {
    "defaults": {
        "noValue": "No W'bal",
        "color": {"mode": "fixed", "fixedColor": "dark-orange"},
        "unit": "percent",
        "decimals": 1,
        "thresholds": {
            "mode": "absolute",
            "steps": [{"color": "dark-orange", "value": None}]
        }
    }
}

d['panels'].append(min_wbal)
d['panels'].append(time_below)

# --- W'bal timeseries (id=1202) ---
# Find the Distributions row (id=906) by ID — W'bal goes before it.
dist_row = next(p for p in d['panels'] if p.get('id') == 906)
wbal_insert_y = dist_row['gridPos']['y']

# Shift Distributions and everything below by +10 to make room
for p in d['panels']:
    gp = p.get('gridPos', {})
    if gp.get('y', 0) >= wbal_insert_y:
        gp['y'] += 10
    if p.get('type') == 'row':
        for c in p.get('panels', []) or []:
            cgp = c.get('gridPos', {})
            if cgp.get('y', 0) >= wbal_insert_y:
                cgp['y'] += 10

wbal_panel = {
    "id": 1202,
    "title": "W'bal (Anaerobic Battery)",
    "description": (
        "Per-second remaining anaerobic work capacity.\n\n"
        "Starts at W' (full battery). Drains when power exceeds CP.\n"
        "Refills exponentially when power is below CP.\n\n"
        "Skiba differential model with GoldenCheetah tau.\n"
        "Computed from the latest CP/W' estimate."
    ),
    "type": "timeseries",
    "gridPos": {"x": 0, "y": wbal_insert_y, "w": 24, "h": 10},
    "datasource": {"type": "postgres", "uid": "velomate"},
    "targets": [{
        "rawSql": (
            "WITH deltas AS (\n"
            "  SELECT time_offset,\n"
            "    COALESCE(speed_kmh, 0) / 3600.0 *\n"
            "      (time_offset - LAG(time_offset, 1, time_offset) OVER (ORDER BY time_offset)) AS dist_delta,\n"
            "    w_bal, power\n"
            "  FROM activity_streams\n"
            "  WHERE activity_id = ${activity_id}\n"
            ")\n"
            "SELECT\n"
            "  ROUND(SUM(dist_delta) OVER (ORDER BY time_offset)::numeric, 2) AS \"Distance (km)\",\n"
            "  ROUND((w_bal / 1000.0)::numeric, 1) AS \"W'bal (kJ)\",\n"
            "  power AS \"Power (W)\"\n"
            "FROM deltas\n"
            "WHERE w_bal IS NOT NULL\n"
            "ORDER BY time_offset;"
        ),
        "format": "table",
        "refId": "A"
    }],
    "fieldConfig": {
        "defaults": {
            "unit": "kj",
            "color": {"mode": "fixed", "fixedColor": "#2ecc71"},
            "custom": {
                "drawStyle": "line",
                "lineWidth": 2,
                "fillOpacity": 10,
                "pointSize": 0
            },
            "noValue": "No W'bal data"
        },
        "overrides": [
            {
                "matcher": {"id": "byName", "options": "Power (W)"},
                "properties": [
                    {"id": "unit", "value": "watt"},
                    {"id": "custom.axisPlacement", "value": "right"},
                    {"id": "custom.lineWidth", "value": 1},
                    {"id": "custom.fillOpacity", "value": 0},
                    {"id": "color", "value": {"mode": "fixed", "fixedColor": "#666666"}}
                ]
            }
        ]
    },
    "options": {
        "tooltip": {"mode": "multi", "sort": "none"},
        "legend": {"displayMode": "list", "placement": "bottom", "showLegend": True}
    }
}
d['panels'].append(wbal_panel)

# Sort panels by (y, x) for clean JSON
d['panels'].sort(key=lambda p: (p['gridPos']['y'], p['gridPos']['x']))

with open('grafana/dashboards/activity.json', 'w') as f:
    json.dump(d, f, indent=2)
    f.write("\n")

# Sanity
ids = []
for p in d['panels']:
    ids.append(p['id'])
    if p.get('type') == 'row':
        for c in p.get('panels', []) or []:
            ids.append(c['id'])
dupes = {i for i in ids if ids.count(i) > 1}
print(f"Panels: {len(ids)}, dupes: {dupes or 'none'}")
PYEOF
```

- [ ] **Step 2: Run dashboard tests**

Run: `python3 -m pytest tests/test_dashboards.py -q`
Expected: 24 passed.

- [ ] **Step 3: Commit**

```bash
git add grafana/dashboards/activity.json
git commit -m "feat(dashboards): W'bal timeseries + Min W'bal + Time below 25% on Activity Details"
```

---

## Task 6: Documentation updates

**Files:**
- Modify: `CLAUDE.md`
- Modify: `README.md`

- [ ] **Step 1: Add W'bal to CLAUDE.md Metrics section**

After the **CP / W'** line, add:

```markdown
- **W'bal**: Per-second remaining anaerobic work capacity. Skiba differential model with GoldenCheetah tau (`546 * exp(-0.01 * (CP - P)) + 316`). Uses latest CP/W' from `cp_estimates`; defaults W' to 20 kJ when CP fit falls back. Stored per second on `activity_streams.w_bal`. Displayed on Activity Details as timeseries + Min W'bal + Time below 25% stats
```

- [ ] **Step 2: Add W'bal to README.md metrics list**

After the **CP / W'** line, add:

```markdown
- **W'bal**: Per-second anaerobic battery gauge computed via Skiba differential model. Shows when you drained your reservoir, how close to empty you got, and where it refilled. Displayed on Activity Details alongside the power trace
```

- [ ] **Step 3: Commit**

```bash
git add CLAUDE.md README.md
git commit -m "docs: document W'bal in CLAUDE and README"
```

---

## Task 7: Final test + push + open PR

- [ ] **Step 1: Run the full relevant test suite**

```bash
python3 -m pytest tests/test_critical_power.py tests/test_fitness_recalc.py tests/test_dashboards.py -q
```

Expected: all pass (20 CP tests + 23 recalc + 24 dashboards).

- [ ] **Step 2: Push the branch**

```bash
git push -u origin feat/wbal-time-series
```

- [ ] **Step 3: Open a PR via tea**

```bash
tea pr create --title "feat(cp): W'bal time series per ride — Cluster B phase 2" --description "$(cat <<'EOF'
## Summary

Per-second W'bal (remaining anaerobic work capacity) for every ride with power data. Skiba differential model with GoldenCheetah tau, using the latest CP/W' from cp_estimates (PR #108). Defaults W' to 20 kJ when CP fit falls back.

**New:** `compute_wbal` pure function in `critical_power.py`, `compute_wbal_for_rides` orchestrator in `fitness.py`, `w_bal` column on `activity_streams`.

**Activity Details panels:**
- W'bal timeseries (green line, power overlay in grey, full width in Ride Telemetry)
- Min W'bal stat card (how close to empty)
- Time below 25% stat card (how much time in the red)

No new dependencies. No METRICS_VERSION bump (w_bal IS NULL filter handles backfill).

## Test plan

- [x] 7 new pure-function tests for compute_wbal (drain, recovery, clamping, known values)
- [x] Existing fitness recalc tests pass (mock catch-all handles new cursors)
- [x] Dashboard tests pass

Server-side:
- [ ] Restart ingestor, verify w_bal populated on activity_streams
- [ ] Activity Details shows W'bal chart alongside power trace
- [ ] Min W'bal and Time below 25% stats show values
EOF
)" --base main
```

---

## Self-Review Checklist

- [ ] All 20 CP tests pass (13 existing + 7 new)
- [ ] 23 fitness recalc tests pass
- [ ] 24 dashboard tests pass
- [ ] `w_bal` column created via `ALTER TABLE ... ADD COLUMN IF NOT EXISTS`
- [ ] `compute_wbal` pure function tested with known values
- [ ] `compute_wbal_for_rides` reads from latest `cp_estimates` row (not per-ride-date)
- [ ] W' defaults to 20 kJ when `w_prime_kj IS NULL` (fallback rows)
- [ ] W'bal timeseries panel has power overlay on secondary axis
- [ ] Both stat cards have descriptive tooltips
- [ ] CLAUDE.md and README.md document W'bal

## Out of Scope (per spec)

- W'bal-based pacing recommendations
- Custom tau parameters
- Recalculating W'bal when CP changes (manual reset via SQL if needed)
- W'bal on Overview (per-ride metric, belongs on Activity Details)
