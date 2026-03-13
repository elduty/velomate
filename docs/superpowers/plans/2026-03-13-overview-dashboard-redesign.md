# Overview Dashboard Redesign — Implementation Plan

> **For agentic workers:** REQUIRED: Use superpowers:subagent-driven-development (if subagents available) or superpowers:executing-plans to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Rebuild the VeloAI overview Grafana dashboard as a TeslaMate-style hub with dynamic time filtering, collapsible sections, and cross-dashboard navigation.

**Architecture:** Full rewrite of `overview.json`. All panels driven by Grafana time range picker. A `$group_by` template variable controls bar chart grouping (day/week/month). Collapsible rows for Volume, Training Load, Records, Monthly Comparison. Activities table with drill-down links.

**Tech Stack:** Grafana 12 dashboard JSON, PostgreSQL 15 SQL queries

**Spec:** `docs/superpowers/specs/2026-03-13-overview-dashboard-redesign.md`

---

## File Map

| File | Action | Responsibility |
|------|--------|----------------|
| `ingestor/db.py` | Modify | Add `tss` column to schema |
| `ingestor/fitness.py` | Modify | Store per-activity TSS during recalculation |
| `grafana/dashboards/overview.json` | Full rewrite | All new panels |
| `grafana/dashboards/fitness-trends.json` | Modify | Add dashboard nav links |
| `grafana/dashboards/activity.json` | Modify | Add dashboard nav links |

Task 0 (schema) is independent. Tasks 1-7 modify the same file (`overview.json`) and must run sequentially.

---

## Chunk 0: Schema Enhancement

### Task 0: Add `tss` column to activities and backfill

Store per-activity TSS at ingest time using the auto-estimated thresholds, so the dashboard can just `SUM(tss)` instead of recalculating with hardcoded defaults.

**Files:**
- Modify: `ingestor/db.py:84-85` (schema migration)
- Modify: `ingestor/fitness.py:56-132` (store TSS per activity during recalculation)

- [ ] **Step 1: Add `tss` column to schema**

In `ingestor/db.py`, after the existing `ALTER TABLE` lines (line 85), add:

```python
            ALTER TABLE activities ADD COLUMN IF NOT EXISTS tss FLOAT;
```

- [ ] **Step 2: Update `recalculate_fitness()` to store per-activity TSS**

In `ingestor/fitness.py`, modify `recalculate_fitness()` to update each activity's `tss` column. After estimating thresholds (line 68-69), add a pass that writes TSS back to each activity:

```python
    # Store per-activity TSS
    with conn.cursor() as cur:
        cur.execute("""
            SELECT id, duration_s, avg_hr, avg_power
            FROM activities
            WHERE date IS NOT NULL
        """)
        activity_rows = cur.fetchall()

    for act_id, duration_s, avg_hr, avg_power in activity_rows:
        if avg_power and avg_power > 0:
            tss = calculate_tss_power(duration_s, avg_power, ftp)
        elif avg_hr and avg_hr > 0:
            tss = calculate_tss(duration_s, avg_hr, threshold_hr)
        else:
            tss = 0
        with conn.cursor() as cur:
            cur.execute("UPDATE activities SET tss = %s WHERE id = %s", (round(tss, 1), act_id))
```

Insert this block after line 69 (`print(f"[fitness] Threshold HR..."`), before the existing daily TSS aggregation loop.

- [ ] **Step 3: Verify schema migration and backfill**

Run:
```bash
docker compose up -d --build
docker compose exec veloai-ingestor python3 -c "
from db import get_connection, create_schema
from fitness import recalculate_fitness
conn = get_connection()
create_schema(conn)
recalculate_fitness(conn)
"
docker compose exec veloai-postgres psql -U veloai -c "SELECT id, name, tss FROM activities WHERE tss > 0 LIMIT 5;"
```
Expected: Activities now have `tss` values populated.

- [ ] **Step 4: Commit**

```bash
git add ingestor/db.py ingestor/fitness.py
git commit -m "feat: add tss column to activities, compute at fitness recalculation time"
```

---

## Chunk 1: Dashboard Foundation

### Task 1: Create dashboard skeleton with hero stats

Build the complete dashboard shell (metadata, template variables, links) and the first 6 stat panels.

**Files:**
- Rewrite: `grafana/dashboards/overview.json`

- [ ] **Step 1: Write the new overview.json with skeleton + hero stats**

**This is a FULL FILE REPLACEMENT** — delete all existing content and write from scratch.

Create the full dashboard JSON with:
- `uid: "veloai-main"`, `title: "VeloAI Overview"`, `schemaVersion: 39`, `version: 1`
- `time: { from: "now-7d", to: "now" }`, `refresh: "5m"`
- `"templating": { "list": [ ...group_by variable... ] }` (must be wrapped in this structure)
- Dashboard `links` array pointing to Fitness Trends (`veloai-fitness`) and Activity Detail (`veloai-activity`)
- 6 hero stat panels at y:0

Datasource block for all panels:
```json
"datasource": { "type": "postgres", "uid": "veloai" }
```

Hero stat panel SQL queries:

**Rides (id:1, w:4 h:4 x:0 y:0):**
```sql
SELECT COUNT(*) AS "Rides" FROM activities WHERE $__timeFilter(date);
```

**Distance (id:2, w:4 h:4 x:4 y:0):**
```sql
SELECT ROUND((COALESCE(SUM(distance_m), 0) / 1000.0)::numeric, 1) AS "Distance" FROM activities WHERE $__timeFilter(date);
```

**Elevation (id:3, w:4 h:4 x:8 y:0):**
```sql
SELECT ROUND(COALESCE(SUM(elevation_m), 0)::numeric, 0) AS "Elevation" FROM activities WHERE $__timeFilter(date);
```

**Duration (id:4, w:4 h:4 x:12 y:0):**
```sql
SELECT COALESCE(SUM(duration_s), 0) AS "Duration" FROM activities WHERE $__timeFilter(date);
```
Unit: `"dthms"` (Grafana displays as days/hours/minutes/seconds)

**Calories (id:5, w:4 h:4 x:16 y:0):**
```sql
SELECT COALESCE(SUM(calories), 0) AS "Calories" FROM activities WHERE $__timeFilter(date) AND calories IS NOT NULL;
```

**Form/TSB (id:6, w:4 h:4 x:20 y:0):**
```sql
SELECT tsb AS "TSB" FROM athlete_stats WHERE $__timeFilter(date) ORDER BY date DESC LIMIT 1;
```
Thresholds: null→red, -10→yellow, 10→green. Decimals: 0. Display sign: `+`.

All stat panels use `"format": "table"` in their target, and this options + fieldConfig pattern:
```json
"options": {
  "colorMode": "value",
  "graphMode": "none",
  "textMode": "auto",
  "reduceOptions": { "calcs": ["lastNotNull"] }
},
"fieldConfig": {
  "defaults": {
    "color": { "mode": "thresholds" },
    "thresholds": {
      "mode": "absolute",
      "steps": [
        { "color": "green", "value": null }
      ]
    }
  }
}
```
Except TSB which uses the red/yellow/green thresholds above.

Template variable JSON:
```json
{
  "name": "group_by",
  "type": "custom",
  "label": "Group by",
  "query": "day,week,month",
  "current": { "text": "day", "value": "day" },
  "options": [
    { "text": "day", "value": "day", "selected": true },
    { "text": "week", "value": "week", "selected": false },
    { "text": "month", "value": "month", "selected": false }
  ]
}
```

Dashboard links JSON:
```json
"links": [
  {
    "title": "Fitness Trends",
    "url": "/d/veloai-fitness/fitness-trends",
    "type": "link",
    "icon": "dashboard",
    "keepTime": true
  },
  {
    "title": "Activity Detail",
    "url": "/d/veloai-activity/activity-detail",
    "type": "link",
    "icon": "dashboard"
  }
]
```

- [ ] **Step 2: Verify dashboard loads in Grafana**

Run: `docker compose restart veloai-grafana && sleep 5 && echo "Check http://10.7.40.15:3021/d/veloai-main"`
Expected: Dashboard loads with 6 stat panels showing data for the selected time range. The `group_by` variable dropdown appears at the top.

- [ ] **Step 3: Commit**

```bash
git add grafana/dashboards/overview.json
git commit -m "feat(grafana): rebuild overview dashboard with hero stats and template variables"
```

---

### Task 2: Add comparison row

Add 4 stat panels below the hero stats.

**Files:**
- Modify: `grafana/dashboards/overview.json`

- [ ] **Step 1: Add 4 comparison stat panels to the panels array**

**vs Previous Period (id:7, w:6 h:3 x:0 y:4):**
```sql
SELECT
  ROUND((COALESCE(curr.dist, 0) - COALESCE(prev.dist, 0))::numeric, 1) AS "Delta (km)"
FROM
  (SELECT SUM(distance_m)/1000.0 AS dist FROM activities WHERE $__timeFilter(date)) curr,
  (SELECT SUM(distance_m)/1000.0 AS dist FROM activities
   WHERE date >= $__timeFrom()::timestamptz - ($__timeTo()::timestamptz - $__timeFrom()::timestamptz)
     AND date < $__timeFrom()::timestamptz) prev;
```
Title: "vs Previous Period". Thresholds: null→red, 0→green. Decimals: 1. Unit: `km`. Prefix with sign.

**Avg per Ride (id:8, w:6 h:3 x:6 y:4):**
```sql
SELECT ROUND((COALESCE(SUM(distance_m), 0) / GREATEST(COUNT(*), 1) / 1000.0)::numeric, 1) AS "Avg (km)"
FROM activities WHERE $__timeFilter(date);
```

**Weekly Streak (id:9, w:6 h:3 x:12 y:4):**
```sql
WITH weeks AS (
  SELECT DISTINCT date_trunc('week', date)::date AS week_start
  FROM activities
  WHERE date IS NOT NULL
),
numbered AS (
  SELECT week_start,
         ROW_NUMBER() OVER (ORDER BY week_start DESC) AS rn,
         (date_trunc('week', CURRENT_DATE)::date - ((ROW_NUMBER() OVER (ORDER BY week_start DESC) - 1) * 7)::int) AS expected
  FROM weeks
)
SELECT COUNT(*) AS "Weeks" FROM numbered WHERE week_start = expected;
```
Color: blue (#6ed0ff). Unit: `weeks` (suffix).

**Days Since Ride (id:10, w:6 h:3 x:18 y:4):**
```sql
SELECT CURRENT_DATE - MAX(date::date) AS "Days" FROM activities;
```
Thresholds: null→green, 4→yellow, 7→red. Unit: `days` (suffix).

- [ ] **Step 2: Verify comparison row appears**

Run: `docker compose restart veloai-grafana`
Expected: 4 stat panels visible below hero stats.

- [ ] **Step 3: Commit**

```bash
git add grafana/dashboards/overview.json
git commit -m "feat(grafana): add comparison row (vs previous period, avg/ride, streak, days since)"
```

---

### Task 3: Add volume row with stacked bars

Add a collapsible row with distance and elevation bar charts.

**Files:**
- Modify: `grafana/dashboards/overview.json`

- [ ] **Step 1: Add collapsible row panel + 2 timeseries panels**

**Row panel (id:100, w:24 h:1 x:0 y:7):**
```json
{
  "id": 100,
  "type": "row",
  "title": "Volume",
  "collapsed": false,
  "gridPos": { "h": 1, "w": 24, "x": 0, "y": 7 },
  "panels": []
}
```

**Distance bars (id:11, w:12 h:8 x:0 y:8):**
Type: `timeseries`. Two queries (refId A = outdoor, refId B = indoor):

Query A (Outdoor):
```sql
SELECT
  gs AS "time",
  COALESCE(SUM(a.distance_m) / 1000.0, 0) AS "Outdoor (km)"
FROM generate_series(
  date_trunc('${group_by}', $__timeFrom()::timestamptz),
  date_trunc('${group_by}', $__timeTo()::timestamptz),
  '1 ${group_by}'::interval
) gs
LEFT JOIN activities a
  ON date_trunc('${group_by}', a.date) = gs
  AND (a.is_indoor = false OR a.is_indoor IS NULL)
GROUP BY gs
ORDER BY gs;
```
Format: `time_series`

Query B (Indoor):
```sql
SELECT
  gs AS "time",
  COALESCE(SUM(a.distance_m) / 1000.0, 0) AS "Indoor (km)"
FROM generate_series(
  date_trunc('${group_by}', $__timeFrom()::timestamptz),
  date_trunc('${group_by}', $__timeTo()::timestamptz),
  '1 ${group_by}'::interval
) gs
LEFT JOIN activities a
  ON date_trunc('${group_by}', a.date) = gs
  AND a.is_indoor = true
GROUP BY gs
ORDER BY gs;
```
Format: `time_series`

Field config for stacking:
```json
"fieldConfig": {
  "defaults": {
    "custom": {
      "drawStyle": "bars",
      "fillOpacity": 80,
      "stacking": { "mode": "normal", "group": "A" }
    }
  },
  "overrides": [
    {
      "matcher": { "id": "byName", "options": "Outdoor (km)" },
      "properties": [{ "id": "color", "value": { "fixedColor": "#33658a", "mode": "fixed" } }]
    },
    {
      "matcher": { "id": "byName", "options": "Indoor (km)" },
      "properties": [{ "id": "color", "value": { "fixedColor": "#5a3d1e", "mode": "fixed" } }]
    }
  ]
}
```

**Elevation bars (id:12, w:12 h:8 x:12 y:8):**
Same generate_series pattern, single query:
```sql
SELECT
  gs AS "time",
  COALESCE(SUM(a.elevation_m), 0) AS "Elevation (m)"
FROM generate_series(
  date_trunc('${group_by}', $__timeFrom()::timestamptz),
  date_trunc('${group_by}', $__timeTo()::timestamptz),
  '1 ${group_by}'::interval
) gs
LEFT JOIN activities a
  ON date_trunc('${group_by}', a.date) = gs
GROUP BY gs
ORDER BY gs;
```
Color: fixed orange `#ff9830`. Same `drawStyle: bars`, `fillOpacity: 80`.

- [ ] **Step 2: Verify volume charts**

Run: `docker compose restart veloai-grafana`
Expected: Collapsible "Volume" row with two bar charts. Changing `group_by` dropdown to "week" or "month" re-groups the bars. Indoor/outdoor distance bars are stacked.

- [ ] **Step 3: Commit**

```bash
git add grafana/dashboards/overview.json
git commit -m "feat(grafana): add volume row with stacked distance and elevation bars"
```

---

## Chunk 2: Training Load, Records, Table

### Task 4: Add training load row

**Files:**
- Modify: `grafana/dashboards/overview.json`

- [ ] **Step 1: Add collapsible row + 2 panels**

**Row panel (id:101, w:24 h:1 x:0 y:16):**
Same pattern as volume row, `title: "Training Load"`, `collapsed: false`.

**TSS bars (id:13, w:12 h:8 x:0 y:17):**
Type: `timeseries`, drawStyle: bars.
```sql
SELECT
  gs AS "time",
  COALESCE(SUM(a.tss), 0) AS "TSS"
FROM generate_series(
  date_trunc('${group_by}', $__timeFrom()::timestamptz),
  date_trunc('${group_by}', $__timeTo()::timestamptz),
  '1 ${group_by}'::interval
) gs
LEFT JOIN activities a
  ON date_trunc('${group_by}', a.date) = gs
GROUP BY gs
ORDER BY gs;
```
Uses the pre-computed `tss` column from Task 0 — no CASE expression needed.
Color: fixed purple `#8b5cf6`.

**CTL/ATL/TSB lines (id:14, w:12 h:8 x:12 y:17):**
Type: `timeseries`, drawStyle: line.
```sql
SELECT
  date AS "time",
  ctl AS "Fitness (CTL)",
  atl AS "Fatigue (ATL)",
  tsb AS "Form (TSB)"
FROM athlete_stats
WHERE $__timeFilter(date)
ORDER BY date;
```
Format: `time_series`

Color overrides:
- "Fitness (CTL)" → `#6ed0ff`
- "Fatigue (ATL)" → `#ff9830`
- "Form (TSB)" → `#73bf69`, override properties: `{ "id": "custom.lineStyle", "value": { "fill": "dash", "dash": [6, 4] } }`, `{ "id": "custom.fillOpacity", "value": 10 }`

- [ ] **Step 2: Verify training load row**

Run: `docker compose restart veloai-grafana`
Expected: Purple TSS bars and CTL/ATL/TSB line chart in collapsible "Training Load" row.

- [ ] **Step 3: Commit**

```bash
git add grafana/dashboards/overview.json
git commit -m "feat(grafana): add training load row with TSS bars and CTL/ATL/TSB lines"
```

---

### Task 5: Add records and monthly comparison rows

**Files:**
- Modify: `grafana/dashboards/overview.json`

- [ ] **Step 1: Add Personal Records collapsible row (collapsed) + 4 stat panels**

**Row panel (id:102, w:24 h:1 x:0 y:25):**
`title: "Personal Records"`, `collapsed: true`. When collapsed, child panels go inside the row's `panels` array.

**Longest Ride (id:15, w:6 h:4 x:0 y:26):**
```sql
SELECT
  ROUND((a.distance_m / 1000.0)::numeric, 1) AS "Longest (km)",
  a.name || ' (' || TO_CHAR(a.date, 'YYYY-MM-DD') || ')' AS "description"
FROM activities a
WHERE a.distance_m = (SELECT MAX(distance_m) FROM activities)
LIMIT 1;
```

**Most Elevation (id:16, w:6 h:4 x:6 y:26):**
```sql
SELECT
  ROUND(a.elevation_m::numeric, 0) AS "Most Elev (m)",
  a.name || ' (' || TO_CHAR(a.date, 'YYYY-MM-DD') || ')' AS "description"
FROM activities a
WHERE a.elevation_m = (SELECT MAX(elevation_m) FROM activities)
LIMIT 1;
```

**Fastest Avg Speed (id:17, w:6 h:4 x:12 y:26):**
```sql
SELECT
  ROUND(a.avg_speed_kmh::numeric, 1) AS "Fastest (km/h)",
  a.name || ' (' || TO_CHAR(a.date, 'YYYY-MM-DD') || ')' AS "description"
FROM activities a
WHERE a.distance_m > 5000
  AND a.avg_speed_kmh = (SELECT MAX(avg_speed_kmh) FROM activities WHERE distance_m > 5000)
LIMIT 1;
```

**Best Avg Power (id:18, w:6 h:4 x:18 y:26):**
```sql
SELECT
  a.avg_power AS "Best Power (W)",
  a.name || ' (' || TO_CHAR(a.date, 'YYYY-MM-DD') || ')' AS "description"
FROM activities a
WHERE a.duration_s > 1200 AND a.avg_power IS NOT NULL
  AND a.avg_power = (SELECT MAX(avg_power) FROM activities WHERE duration_s > 1200 AND avg_power IS NOT NULL)
LIMIT 1;
```

For all record panels: use `options.textMode: "value_and_name"` or display the description as secondary text via field overrides.

- [ ] **Step 2: Add Monthly Comparison collapsible row (collapsed) + 1 table panel**

**Row panel (id:103, w:24 h:1 x:0 y:30):**
`title: "Monthly Comparison"`, `collapsed: true`.

**Year-over-year table (id:19, w:24 h:8 x:0 y:31):**
Type: `table`.
```sql
SELECT
  TO_CHAR(date, 'Mon') AS "Month",
  EXTRACT(YEAR FROM date)::int AS "Year",
  COUNT(*) AS "Rides",
  ROUND((SUM(distance_m) / 1000)::numeric, 0) AS "Distance (km)",
  ROUND(SUM(elevation_m)::numeric, 0) AS "Elevation (m)",
  ROUND((SUM(duration_s) / 3600.0)::numeric, 1) AS "Hours"
FROM activities
WHERE date >= date_trunc('year', CURRENT_DATE) - interval '1 year'
GROUP BY TO_CHAR(date, 'Mon'), EXTRACT(YEAR FROM date), EXTRACT(MONTH FROM date)
ORDER BY EXTRACT(MONTH FROM date), EXTRACT(YEAR FROM date);
```
Format: `table`.

- [ ] **Step 3: Verify collapsed rows expand correctly**

Run: `docker compose restart veloai-grafana`
Expected: "Personal Records" and "Monthly Comparison" rows appear collapsed. Clicking expands them to reveal panels.

- [ ] **Step 4: Commit**

```bash
git add grafana/dashboards/overview.json
git commit -m "feat(grafana): add personal records and monthly comparison rows (collapsed)"
```

---

### Task 6: Add activities table and finalize

**Files:**
- Modify: `grafana/dashboards/overview.json`

- [ ] **Step 1: Add activities table panel**

**Row panel (id:104, w:24 h:1 x:0 y:39):**
`title: "Recent Activities"`, `collapsed: false`.

**Activities table (id:20, w:24 h:10 x:0 y:40):**
Type: `table`.
```sql
SELECT
  id,
  name AS "Name",
  TO_CHAR(date, 'YYYY-MM-DD') AS "Date",
  ROUND((distance_m / 1000.0)::numeric, 1) AS "Distance (km)",
  ROUND(elevation_m::numeric, 0) AS "Elevation (m)",
  TO_CHAR(duration_s * interval '1 second', 'HH24:MI') AS "Duration",
  COALESCE(avg_hr::text, '—') AS "Avg HR",
  COALESCE(avg_power::text, '—') AS "Avg Power",
  CASE sport_type
    WHEN 'cycling_outdoor' THEN 'Outdoor'
    WHEN 'cycling_indoor' THEN 'Indoor'
    WHEN 'zwift' THEN 'Zwift'
    WHEN 'strength' THEN 'Strength'
    ELSE COALESCE(sport_type, '—')
  END AS "Type",
  COALESCE(device, '—') AS "Device",
  EXTRACT(EPOCH FROM date - interval '1 hour')::bigint * 1000 AS from_ts,
  EXTRACT(EPOCH FROM date + duration_s * interval '1 second' + interval '1 hour')::bigint * 1000 AS to_ts
FROM activities
WHERE $__timeFilter(date)
ORDER BY date DESC
LIMIT 50;
```
Format: `table`.

Hide columns `id`, `from_ts`, `to_ts` via field overrides (they're used for linking only).

Data link on "Name" column:
```json
{
  "title": "View Activity",
  "url": "/d/veloai-activity/activity-detail?var-activity_id=${__data.fields.id}&from=${__data.fields.from_ts}&to=${__data.fields.to_ts}"
}
```

- [ ] **Step 2: Verify full dashboard**

Run: `docker compose restart veloai-grafana`
Expected: Complete dashboard with all 8 sections. Activity names are clickable and open Activity Detail. Time picker changes update all panels. `group_by` variable changes bar chart grouping.

- [ ] **Step 3: Commit**

```bash
git add grafana/dashboards/overview.json
git commit -m "feat(grafana): add activities table with drill-down links"
```

---

### Task 7: Add navigation links to other dashboards

**Files:**
- Modify: `grafana/dashboards/fitness-trends.json`
- Modify: `grafana/dashboards/activity.json`

- [ ] **Step 1: Add `links` array to fitness-trends.json**

Add at the top level of the JSON (alongside `uid`, `title`, etc.):
```json
"links": [
  {
    "title": "Overview",
    "url": "/d/veloai-main/veloai-overview",
    "type": "link",
    "icon": "dashboard",
    "keepTime": true
  },
  {
    "title": "Activity Detail",
    "url": "/d/veloai-activity/activity-detail",
    "type": "link",
    "icon": "dashboard"
  }
]
```

- [ ] **Step 2: Add `links` array to activity.json**

```json
"links": [
  {
    "title": "Overview",
    "url": "/d/veloai-main/veloai-overview",
    "type": "link",
    "icon": "dashboard",
    "keepTime": true
  },
  {
    "title": "Fitness Trends",
    "url": "/d/veloai-fitness/fitness-trends",
    "type": "link",
    "icon": "dashboard",
    "keepTime": true
  }
]
```

- [ ] **Step 3: Verify navigation links on all dashboards**

Run: `docker compose restart veloai-grafana`
Expected: All three dashboards show navigation links at the top. Clicking links navigates between dashboards, preserving time range where `keepTime: true`.

- [ ] **Step 4: Commit**

```bash
git add grafana/dashboards/fitness-trends.json grafana/dashboards/activity.json
git commit -m "feat(grafana): add cross-dashboard navigation links"
```
