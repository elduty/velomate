# Progression Dashboard Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace `grafana/dashboards/all-time-progression.json` with a deep analytics dashboard: stat cards, progression charts with rolling averages, FTP tracking, best efforts, training zone polarization, personal records, and YoY comparison.

**Architecture:** Single Grafana JSON file replacement. All SQL queries hit existing PostgreSQL tables (`activities`, `activity_streams`, `athlete_stats`, `sync_state`). No schema changes, no Python code changes. Deploy by restarting the Grafana container.

**Tech Stack:** Grafana 12.4 dashboard JSON, PostgreSQL raw SQL queries

**Spec:** `docs/superpowers/specs/2026-03-20-progression-dashboard-redesign.md`

---

### Task 1: Dashboard scaffold + stat cards

**Files:**
- Modify: `grafana/dashboards/all-time-progression.json`

Build the dashboard shell and first section. This replaces the entire file.

- [ ] **Step 1: Write the dashboard JSON with scaffold + stat cards**

Create `grafana/dashboards/all-time-progression.json` with:
- Dashboard metadata: uid `veloai-progression`, title `All Time Progression`, `graphTooltip: 2`, time range `now-1y` to `now`, no refresh
- Nav links: Overview (`/d/veloai-main/overview`) and Activity Detail (`/d/veloai-activity/activity-details`)
- Template variable: `sport_type` (custom, same definition as Overview dashboard — copy from `overview.json` lines 62-96)
- 6 stat panels (id 1-6) in a row at y=0, each w=4, h=3:
  - Total Distance: `SELECT ROUND((COALESCE(SUM(distance_m), 0) / 1000.0)::numeric, 1) AS "Distance" FROM activities WHERE (('${sport_type}' = 'all') OR sport_type = '${sport_type}');` — dark-blue, unit `lengthkm`
  - Total Elevation: `SUM(elevation_m)` — dark-blue, unit `lengthm`
  - Total Rides: `COUNT(*)` — dark-blue, no unit
  - Total Hours: `SUM(duration_s)` — dark-blue, unit `dthms`
  - Current FTP: copy exact query from Overview dashboard (id 223, lines 440-441 of overview.json) — dark-purple, unit `watt`
  - Peak CTL: `SELECT ROUND(MAX(ctl)::numeric, 1) AS "Peak CTL" FROM athlete_stats;` — dark-purple, no unit

Stat card pattern (use for all 6):
```json
{
  "options": {"colorMode": "background", "graphMode": "none", "textMode": "auto", "reduceOptions": {"calcs": ["lastNotNull"]}},
  "fieldConfig": {"defaults": {"noValue": "No data", "color": {"mode": "fixed", "fixedColor": "dark-blue"}, "thresholds": {"mode": "absolute", "steps": [{"color": "dark-blue", "value": null}]}}}
}
```
For purple cards: replace all `dark-blue` with `dark-purple`.

- [ ] **Step 2: Verify JSON is valid**

Run: `python3 -c "import json; json.load(open('grafana/dashboards/all-time-progression.json'))"`
Expected: no output (valid JSON)

- [ ] **Step 3: Commit**

```bash
git add grafana/dashboards/all-time-progression.json
git commit -m "feat(dashboard): progression scaffold + stat cards"
```

---

### Task 2: Performance progression — simple charts (speed, power, HR, distance)

**Files:**
- Modify: `grafana/dashboards/all-time-progression.json`

Add 4 timeseries panels with scatter + 10-ride rolling average + regression. These use only the `activities` table (no stream queries).

- [ ] **Step 1: Add row header + 4 progression panels**

Add row panel at y=3 (id 100, title "Performance Progression").

Add 4 timeseries panels in a 2×2 grid starting at y=4, each h=8, w=12:

**Avg Speed** (id 10, x=0, y=4):
```sql
SELECT date::date AS "time",
  ROUND(avg_speed_kmh::numeric, 1) AS "Avg Speed",
  ROUND(AVG(avg_speed_kmh) OVER (ORDER BY date ROWS BETWEEN 9 PRECEDING AND CURRENT ROW)::numeric, 1) AS "10-ride avg"
FROM activities
WHERE avg_speed_kmh > 0 AND distance_m > 5000 AND is_indoor IS NOT TRUE
  AND (('${sport_type}' = 'all') OR sport_type = '${sport_type}')
  AND $__timeFilter(date)
ORDER BY date;
```
Color `#33658a`, unit `velocitykmh`.

**Avg Power** (id 11, x=12, y=4):
```sql
SELECT date::date AS "time",
  ROUND(avg_power::numeric, 0) AS "Avg Power",
  ROUND(AVG(avg_power) OVER (ORDER BY date ROWS BETWEEN 9 PRECEDING AND CURRENT ROW)::numeric, 0) AS "10-ride avg"
FROM activities
WHERE avg_power IS NOT NULL AND avg_power > 0 AND duration_s > 1200
  AND (('${sport_type}' = 'all') OR sport_type = '${sport_type}')
  AND $__timeFilter(date)
ORDER BY date;
```
Color `#8b5cf6`, unit `watt`.

**Avg HR** (id 12, x=0, y=12):
```sql
SELECT date::date AS "time",
  avg_hr AS "Avg HR",
  ROUND(AVG(avg_hr) OVER (ORDER BY date ROWS BETWEEN 9 PRECEDING AND CURRENT ROW)::numeric, 0) AS "10-ride avg"
FROM activities
WHERE avg_hr IS NOT NULL AND avg_hr > 0
  AND (('${sport_type}' = 'all') OR sport_type = '${sport_type}')
  AND $__timeFilter(date)
ORDER BY date;
```
Color `#F2495C`, no unit.

**Avg Distance** (id 13, x=12, y=12):
```sql
SELECT date::date AS "time",
  ROUND((distance_m / 1000.0)::numeric, 1) AS "Distance",
  ROUND(AVG(distance_m / 1000.0) OVER (ORDER BY date ROWS BETWEEN 9 PRECEDING AND CURRENT ROW)::numeric, 1) AS "10-ride avg"
FROM activities
WHERE distance_m > 0
  AND (('${sport_type}' = 'all') OR sport_type = '${sport_type}')
  AND $__timeFilter(date)
ORDER BY date;
```
Color `#33658a`, unit `lengthkm`.

All 4 charts share this field config pattern:
```json
{
  "fieldConfig": {
    "defaults": {
      "unit": "THE_UNIT",
      "color": {"fixedColor": "THE_COLOR", "mode": "fixed"},
      "custom": {"drawStyle": "points", "pointSize": 5}
    },
    "overrides": [{
      "matcher": {"id": "byName", "options": "10-ride avg"},
      "properties": [
        {"id": "custom.drawStyle", "value": "line"},
        {"id": "custom.lineWidth", "value": 2},
        {"id": "custom.pointSize", "value": 0},
        {"id": "custom.fillOpacity", "value": 0}
      ]
    }]
  },
  "transformations": [{"id": "regression", "options": {"modelType": "linear"}}]
}
```

**Note on regression:** The Grafana regression transformation applies to all numeric series. It will generate regression lines for both the scatter and rolling avg series. During verification (Task 9), check if the extra regression line on "10-ride avg" is distracting. If so, split into two queries — scatter in query A (with regression), rolling avg in query B (no regression) — and remove the `overrides` approach.

- [ ] **Step 2: Verify JSON is valid**

Run: `python3 -c "import json; json.load(open('grafana/dashboards/all-time-progression.json'))"`

- [ ] **Step 3: Commit**

```bash
git add grafana/dashboards/all-time-progression.json
git commit -m "feat(dashboard): progression scatter plots with rolling averages"
```

---

### Task 3: Performance progression — stream charts (NP, EF)

**Files:**
- Modify: `grafana/dashboards/all-time-progression.json`

Add NP and EF progression charts that query `activity_streams`.

- [ ] **Step 1: Add NP and EF panels**

**Normalized Power** (id 14, x=0, y=20, h=8, w=12):
Use the NP query from spec lines 94-118. Color `#8b5cf6`, unit `watt`. Same field config pattern as Task 2 (scatter + rolling avg override + regression).

**Efficiency Factor** (id 15, x=12, y=20, h=8, w=12):
Use the NP CTE from the same query, but final SELECT:
```sql
SELECT a.date::date AS "time",
  ROUND((n.np / a.avg_hr)::numeric, 2) AS "EF",
  ROUND(AVG(n.np / a.avg_hr) OVER (ORDER BY a.date ROWS BETWEEN 9 PRECEDING AND CURRENT ROW)::numeric, 2) AS "10-ride avg"
FROM np_per_ride n
JOIN activities a ON a.id = n.activity_id
WHERE a.avg_hr > 0
ORDER BY a.date;
```
Color `#73bf69`, no unit. Same field config pattern.

- [ ] **Step 2: Verify JSON + commit**

```bash
python3 -c "import json; json.load(open('grafana/dashboards/all-time-progression.json'))"
git add grafana/dashboards/all-time-progression.json
git commit -m "feat(dashboard): NP and efficiency factor progression"
```

---

### Task 4: FTP progression + best efforts

**Files:**
- Modify: `grafana/dashboards/all-time-progression.json`

- [ ] **Step 1: Add row header + FTP and best efforts panels**

Add row panel at y=28 (id 101, title "FTP & Best Efforts").

**FTP Progression** (id 20, x=0, y=29, h=8, w=12):
Two targets:
- Query A: FTP estimation query from spec lines 129-150. Format `time_series`.
- Query B: Configured FTP reference line from spec lines 158-163. Format `time_series`.

Field config:
```json
{
  "defaults": {
    "unit": "watt",
    "color": {"fixedColor": "#8b5cf6", "mode": "fixed"},
    "custom": {"drawStyle": "line", "lineWidth": 2, "fillOpacity": 15}
  },
  "overrides": [{
    "matcher": {"id": "byName", "options": "Configured FTP"},
    "properties": [
      {"id": "color", "value": {"fixedColor": "#8b5cf680", "mode": "fixed"}},
      {"id": "custom.lineStyle", "value": {"fill": "dash", "dash": [6, 4]}},
      {"id": "custom.fillOpacity", "value": 0},
      {"id": "custom.lineWidth", "value": 1}
    ]
  }]
}
```

**Best Efforts** (id 21, x=12, y=29, h=8, w=12):
Three targets (A/B/C) — one per duration. Use the 1-min query from spec lines 171-189 as template. Query B changes `59` to `299` and alias to `"5-min"`. Query C changes `59` to `1199` and alias to `"20-min"`.

Field config:
```json
{
  "defaults": {
    "unit": "watt",
    "custom": {"drawStyle": "line", "pointSize": 4, "lineWidth": 1}
  },
  "overrides": [
    {"matcher": {"id": "byName", "options": "1-min"}, "properties": [{"id": "color", "value": {"fixedColor": "#F2495C", "mode": "fixed"}}]},
    {"matcher": {"id": "byName", "options": "5-min"}, "properties": [{"id": "color", "value": {"fixedColor": "#ff9830", "mode": "fixed"}}]},
    {"matcher": {"id": "byName", "options": "20-min"}, "properties": [{"id": "color", "value": {"fixedColor": "#8b5cf6", "mode": "fixed"}}]}
  ]
}
```

- [ ] **Step 2: Verify JSON + commit**

```bash
python3 -c "import json; json.load(open('grafana/dashboards/all-time-progression.json'))"
git add grafana/dashboards/all-time-progression.json
git commit -m "feat(dashboard): FTP progression and best efforts"
```

---

### Task 5: Training zone polarization

**Files:**
- Modify: `grafana/dashboards/all-time-progression.json`

- [ ] **Step 1: Add row header + zone distribution panels**

Add row panel at y=37 (id 102, title "Training Zones Over Time").

**Monthly Power Zone Distribution** (id 30, x=0, y=38, h=8, w=12):
Use the power zone query from spec lines 202-231. Format `time_series`.

Field config — stacked bars with Coggan zone colors:
```json
{
  "defaults": {
    "unit": "percent",
    "custom": {"drawStyle": "bars", "fillOpacity": 80, "stacking": {"mode": "normal"}, "barWidthFactor": 0.6}
  }
}
```

**Note:** Stacking mode is `normal` (not `percent`) because the SQL already computes percentage values. Using `percent` mode would re-normalize and distort the data.

Zone color overrides:
```json
{
  "overrides": [
    {"matcher": {"id": "byName", "options": "Z1 Recovery"}, "properties": [{"id": "color", "value": {"fixedColor": "#6b7280", "mode": "fixed"}}]},
    {"matcher": {"id": "byName", "options": "Z2 Endurance"}, "properties": [{"id": "color", "value": {"fixedColor": "#3b82f6", "mode": "fixed"}}]},
    {"matcher": {"id": "byName", "options": "Z3 Tempo"}, "properties": [{"id": "color", "value": {"fixedColor": "#22c55e", "mode": "fixed"}}]},
    {"matcher": {"id": "byName", "options": "Z4 Threshold"}, "properties": [{"id": "color", "value": {"fixedColor": "#eab308", "mode": "fixed"}}]},
    {"matcher": {"id": "byName", "options": "Z5 VO2max"}, "properties": [{"id": "color", "value": {"fixedColor": "#f97316", "mode": "fixed"}}]},
    {"matcher": {"id": "byName", "options": "Z6 Anaerobic"}, "properties": [{"id": "color", "value": {"fixedColor": "#ef4444", "mode": "fixed"}}]}
  ]
}
```

**Monthly HR Zone Distribution** (id 31, x=12, y=38, h=8, w=12):
Use the HR zone query from spec lines 238-265. Same field config pattern but only Z1-Z5 overrides (no Z6).

- [ ] **Step 2: Verify JSON + commit**

```bash
python3 -c "import json; json.load(open('grafana/dashboards/all-time-progression.json'))"
git add grafana/dashboards/all-time-progression.json
git commit -m "feat(dashboard): training zone polarization charts"
```

---

### Task 6: Fitness history + cumulative totals

**Files:**
- Modify: `grafana/dashboards/all-time-progression.json`

- [ ] **Step 1: Add fitness history section**

Add row panel at y=46 (id 103, title "Fitness History").

**CTL/ATL/TSB** (id 40, x=0, y=47, h=8, w=24):
Use the fitness query from spec lines 276-282. Copy the exact field config (overrides for CTL/ATL/TSB colors and TSB dashed style) from the current `all-time-progression.json` panel id 9 (lines 458-534 of the original file, saved at start of conversation).

- [ ] **Step 2: Add cumulative totals section**

Add row panel at y=55 (id 104, title "Cumulative Totals").

6 timeseries panels in a 3×2 grid starting at y=56, each h=7, w=8:

| id | Panel | x | y | SQL window function | Color | Unit |
|---|---|---|---|---|---|---|
| 50 | Cumulative Distance | 0 | 56 | `SUM(distance_m) OVER (ORDER BY date, id) / 1000.0` | #33658a | lengthkm |
| 51 | Cumulative Elevation | 8 | 56 | `SUM(elevation_m) OVER (ORDER BY date, id)` | #ff9830 | lengthm |
| 52 | Cumulative Duration | 16 | 56 | `SUM(duration_s) OVER (ORDER BY date, id) / 3600.0` | #6ed0ff | h |
| 53 | Cumulative Rides | 0 | 63 | `ROW_NUMBER() OVER (ORDER BY date, id)` | #6ed0ff | none |
| 54 | Cumulative TSS | 8 | 63 | `SUM(COALESCE(tss, 0)) OVER (ORDER BY date, id)` | #8b5cf6 | none |
| 55 | Cumulative Calories | 16 | 63 | `SUM(COALESCE(calories, 0)) OVER (ORDER BY date, id)` | #ff9830 | none |

All cumulative queries: `SELECT date::date AS "time", ROUND((<window_fn>)::numeric, 0) AS "Total" FROM activities WHERE <appropriate_filter> AND (('${sport_type}' = 'all') OR sport_type = '${sport_type}') ORDER BY date, id;`

No `$__timeFilter` — cumulative always starts from first ride.

Shared field config:
```json
{
  "defaults": {
    "color": {"fixedColor": "THE_COLOR", "mode": "fixed"},
    "custom": {"drawStyle": "line", "fillOpacity": 15, "lineWidth": 2}
  }
}
```

- [ ] **Step 3: Verify JSON + commit**

```bash
python3 -c "import json; json.load(open('grafana/dashboards/all-time-progression.json'))"
git add grafana/dashboards/all-time-progression.json
git commit -m "feat(dashboard): fitness history and cumulative totals"
```

---

### Task 7: Monthly trends (stacked by ride type)

**Files:**
- Modify: `grafana/dashboards/all-time-progression.json`

- [ ] **Step 1: Add monthly trends section**

Add row panel at y=70 (id 105, title "Monthly Trends").

4 timeseries panels in a 2×2 grid starting at y=71, each h=8, w=12.

Each chart has 4 queries (A/B/C/D) — one per sport type. Example for Monthly Distance (id 60, x=0, y=71):

Query A (Outdoor):
```sql
SELECT date_trunc('month', date) AS time,
  ROUND((SUM(distance_m)/1000.0)::numeric, 0) AS "Outdoor"
FROM activities
WHERE distance_m > 0 AND sport_type = 'cycling_outdoor'
  AND (('${sport_type}' = 'all') OR sport_type = '${sport_type}')
GROUP BY 1 ORDER BY 1;
```
Query B: same but `sport_type = 'zwift'`, alias `"Zwift"`.
Query C: same but `sport_type = 'ebike'`, alias `"E-Bike"`.
Query D: same but `sport_type = 'cycling_indoor'`, alias `"Indoor"`.

Field config for all 4 monthly charts:
```json
{
  "defaults": {
    "custom": {"drawStyle": "bars", "fillOpacity": 80, "stacking": {"mode": "normal"}, "barWidthFactor": 0.6}
  },
  "overrides": [
    {"matcher": {"id": "byName", "options": "Outdoor"}, "properties": [{"id": "color", "value": {"fixedColor": "#33658a", "mode": "fixed"}}]},
    {"matcher": {"id": "byName", "options": "Zwift"}, "properties": [{"id": "color", "value": {"fixedColor": "#fc4c02", "mode": "fixed"}}]},
    {"matcher": {"id": "byName", "options": "E-Bike"}, "properties": [{"id": "color", "value": {"fixedColor": "#73bf69", "mode": "fixed"}}]},
    {"matcher": {"id": "byName", "options": "Indoor"}, "properties": [{"id": "color", "value": {"fixedColor": "#8b5cf6", "mode": "fixed"}}]}
  ]
}
```

| id | Panel | x | y | Metric |
|---|---|---|---|---|
| 60 | Monthly Distance | 0 | 71 | `SUM(distance_m)/1000` (km) |
| 61 | Monthly Elevation | 12 | 71 | `SUM(elevation_m)` (m) |
| 62 | Monthly Rides | 0 | 79 | `COUNT(*)` |
| 63 | Monthly Hours | 12 | 79 | `SUM(duration_s)/3600` (h) |

- [ ] **Step 2: Verify JSON + commit**

```bash
python3 -c "import json; json.load(open('grafana/dashboards/all-time-progression.json'))"
git add grafana/dashboards/all-time-progression.json
git commit -m "feat(dashboard): monthly trends stacked by ride type"
```

---

### Task 8: Year-over-year + personal records

**Files:**
- Modify: `grafana/dashboards/all-time-progression.json`

- [ ] **Step 1: Add YoY section**

Add row panel at y=87 (id 106, title "Year-over-Year").

**YoY Monthly Distance** (id 70, x=0, y=88, h=8, w=12, type `timeseries`):
Two queries from spec lines 325-342. Style: bars with `barWidthFactor: 0.6`, `stacking: {"mode": "none"}` (grouped, not stacked). Colors: This Year `#33658a`, Last Year `#33658a80`.

**Annual Totals** (id 71, x=12, y=88, h=8, w=12, type `table`):
Query from spec lines 349-360. Format `table`. `showHeader: true`.

- [ ] **Step 2: Add personal records section**

Add row panel at y=96 (id 107, title "Personal Records").

**All-Time Records** (id 80, x=0, y=97, h=8, w=24, type `table`):
Use the UNION ALL query from spec lines 368-426. Format `table`.

Table options: `"filterable": false`, `"showHeader": true`.

Field config overrides:
- Hide "ord" column if present
- "Ride" column: set `custom.displayMode` to `auto` (Grafana auto-detects HTML links in table cells)

**Important:** Grafana's table panel renders `<a href>` tags if the column data type is detected as string containing HTML. Test this in the deployed dashboard — if links don't render, we may need to use data links instead.

- [ ] **Step 3: Verify JSON + commit**

```bash
python3 -c "import json; json.load(open('grafana/dashboards/all-time-progression.json'))"
git add grafana/dashboards/all-time-progression.json
git commit -m "feat(dashboard): year-over-year comparison and personal records"
```

---

### Task 9: Deploy and verify

- [ ] **Step 1: Restart Grafana to load the new dashboard**

```bash
docker compose restart veloai-grafana
```

Wait ~10 seconds for Grafana to start.

- [ ] **Step 2: Verify each section visually**

Open the dashboard in a browser at the Grafana URL (`/d/veloai-progression/all-time-progression`).

Check each section:
1. Stat cards show values (not "No data" unless DB is empty)
2. Progression charts show scatter points + rolling avg line + regression line
3. NP and EF charts render (stream queries execute without error)
4. FTP chart shows monthly line
5. Best efforts show 3 colored series
6. Zone polarization shows stacked bars with correct colors
7. CTL/ATL/TSB renders with 3 lines
8. Cumulative charts show upward curves
9. Monthly trends show stacked bars split by ride type
10. YoY shows grouped bars + annual table
11. Personal records table shows drill-down links
12. Sport type filter works (switching filters all panels)
13. Time range picker works on appropriate panels
14. Shared crosshair works across charts
15. Nav links work (Overview + Activity Detail)

- [ ] **Step 3: Fix any query errors**

If any panel shows "Error" instead of data, check Grafana's panel inspect (Edit → Query Inspector) for the SQL error. Common fixes:
- Missing column: check column name against `ingestor/db.py:create_schema()`
- Type cast errors: ensure `::numeric` casts on aggregates
- Time filter issues: verify `$__timeFilter(date)` syntax

- [ ] **Step 4: Final commit if fixes were needed**

```bash
git add grafana/dashboards/all-time-progression.json
git commit -m "fix(dashboard): address query issues from visual verification"
```
