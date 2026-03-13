# Overview Dashboard Redesign — Design Spec

**Goal:** Rebuild the VeloAI overview dashboard as a TeslaMate-style hub driven entirely by Grafana's time range picker, with collapsible sections for volume, training load, records, and monthly comparisons.

**Approach:** Enhanced Hub (Approach A) — keep the existing 3-dashboard structure, rebuild `overview.json` with dynamic time-aware panels and collapsible rows.

---

## Design Decisions

1. **All panels driven by `$__timeFilter(date)`** — hero stats, charts, comparisons, and table all respond to the Grafana time range picker. No hardcoded "this week" or "this month."

2. **Auto-grouped bar charts via `$group_by` variable** — a Grafana template variable dropdown (`day`/`week`/`month`) controls `date_trunc()` grouping. Defaults to `day`. User can override, or we set a sensible default per time range via variable query.

3. **"vs Previous Period" comparison** — uses SQL to compare the selected range against the equivalent previous range (e.g., last 7 days vs the 7 days before that).

4. **Collapsible rows** — Grafana's native row panel with collapse. Volume and Training Load expanded by default; Records and Monthly Comparison collapsed.

5. **Dashboard navigation links** — Grafana `links` array in the dashboard JSON for cross-dashboard navigation (Overview, Fitness Trends, Activity Detail).

6. **Indoor/outdoor stacked bars** — volume charts use stacked bar series split by `is_indoor`.

---

## Dashboard Sections

### 1. Navigation Bar

Grafana dashboard-level `links` to Fitness Trends and Activity Detail dashboards. Appears as a row of links at the top of every dashboard.

### 2. Hero Stats Row (6 stat panels)

Totals for the selected time range. All queries use `$__timeFilter(date)`.

| Panel | SQL | Unit | Color |
|-------|-----|------|-------|
| Rides | `COUNT(*)` from activities | count | green |
| Distance | `SUM(distance_m) / 1000` | km | green |
| Elevation | `SUM(elevation_m)` | m | green |
| Duration | `SUM(duration_s)` | h:m (unit: `dthms`) | green |
| Calories | `SUM(calories)` | kcal | green |
| Form (TSB) | Latest `tsb` from `athlete_stats` within range | signed number | thresholds: >10 green, -10..10 yellow, <-10 red |

Grid: 6 panels, each `w:4 h:4`, on row y:0.

### 3. Comparison Row (4 stat panels)

| Panel | SQL Logic | Display |
|-------|-----------|---------|
| vs Previous Period | Distance in selected range minus distance in equivalent previous range | `▲ +22 km` or `▼ -15 km`, green/red |
| Avg per Ride | `SUM(distance_m) / COUNT(*)` for selected range | `35.5 km` |
| Weekly Streak | Count consecutive calendar weeks (ending with current) that have at least 1 ride | `3 weeks` |
| Days Since Ride | `CURRENT_DATE - MAX(date::date)` from activities | `1` (green if ≤3, yellow if 4-6, red if 7+) |

Grid: 4 panels, each `w:6 h:3`, on row y:4.

**vs Previous Period SQL (single query):**
```sql
SELECT
  COALESCE(curr.dist, 0) - COALESCE(prev.dist, 0) AS "Delta (km)"
FROM
  (SELECT SUM(distance_m)/1000 AS dist FROM activities WHERE $__timeFilter(date)) curr,
  (SELECT SUM(distance_m)/1000 AS dist FROM activities
   WHERE date >= $__timeFrom()::timestamptz - ($__timeTo()::timestamptz - $__timeFrom()::timestamptz)
     AND date < $__timeFrom()::timestamptz) prev
```

Display with value mapping: positive → `▲ +X km` (green), negative → `▼ X km` (red).

**Weekly Streak SQL:**
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
SELECT COUNT(*) AS "Weeks"
FROM numbered
WHERE week_start = expected
```

### 4. Volume Row (collapsible, expanded by default)

Two timeseries bar chart panels side by side.

**Template variable `$group_by`:** Custom variable with values `day`, `week`, `month`. Default `day`.

**Distance (km):**
- Two queries: outdoor (`is_indoor = false OR is_indoor IS NULL`) and indoor (`is_indoor = true`)
- Groups by `date_trunc('${group_by}', date)` — uses the template variable, not `$__interval`
- Uses `generate_series($__timeFrom()::timestamptz, $__timeTo()::timestamptz, '1 ${group_by}'::interval)` for zero-fill
- Stacked bars via `"stacking": {"mode": "normal", "group": "A"}` in field overrides
- Outdoor blue (#33658a), indoor brown (#5a3d1e)

Example SQL (outdoor):
```sql
SELECT
  gs AS "time",
  COALESCE(SUM(distance_m) / 1000.0, 0) AS "Outdoor (km)"
FROM generate_series(
  date_trunc('${group_by}', $__timeFrom()::timestamptz),
  date_trunc('${group_by}', $__timeTo()::timestamptz),
  '1 ${group_by}'::interval
) gs
LEFT JOIN activities a
  ON date_trunc('${group_by}', a.date) = gs
  AND (a.is_indoor = false OR a.is_indoor IS NULL)
GROUP BY gs
ORDER BY gs
```

**Elevation (m):**
- Same `generate_series` + `date_trunc('${group_by}', ...)` pattern
- Single series (all activities)
- Orange (#ff9830) bars

Grid: 2 panels, each `w:12 h:8`, in a collapsible row at y:7.

All timeseries panels use `"format": "time_series"` in target config.

### 5. Training Load Row (collapsible, expanded by default)

Two panels side by side.

**TSS per Interval (bar chart):**
- Calculates TSS per activity using the formula below, then groups by `date_trunc('${group_by}', date)`
- Uses same `generate_series` zero-fill pattern as volume charts
- Purple (#8b5cf6) bars

**CTL / ATL / TSB (timeseries line chart):**
- Three lines from `athlete_stats` table
- CTL blue (#6ed0ff), ATL orange (#ff9830), TSB green (#73bf69) dashed
- Fill below TSB line with opacity for visual weight

Grid: 2 panels, each `w:12 h:8`, in a collapsible row at y:16.

### 6. Personal Records Row (collapsible, collapsed by default)

Four stat panels showing all-time records with the date achieved.

| Record | SQL |
|--------|-----|
| Longest Ride | `MAX(distance_m) / 1000` + date subquery |
| Most Elevation | `MAX(elevation_m)` + date subquery |
| Fastest Avg Speed | `MAX(avg_speed_kmh)` where `distance_m > 5000` (ignore short rides) |
| Best Avg Power | `MAX(avg_power)` where `duration_s > 1200` (ignore sub-20min) |

Each stat panel shows the value as the main number and the date + activity name as the description text. These are all-time records, not filtered by time range.

Grid: 4 panels, each `w:6 h:4`, in a collapsible row (collapsed).

### 7. Monthly Comparison Row (collapsible, collapsed by default)

One table panel showing month-by-month totals for the current year vs previous year. **Intentionally not filtered by `$__timeFilter`** — this always shows the full year-over-year view regardless of the time picker.

```sql
SELECT
  TO_CHAR(date, 'Mon') AS "Month",
  EXTRACT(YEAR FROM date) AS "Year",
  COUNT(*) AS "Rides",
  ROUND((SUM(distance_m) / 1000)::numeric, 0) AS "Distance (km)",
  ROUND(SUM(elevation_m)::numeric, 0) AS "Elevation (m)",
  ROUND((SUM(duration_s) / 3600.0)::numeric, 1) AS "Hours"
FROM activities
WHERE date >= date_trunc('year', CURRENT_DATE) - interval '1 year'
GROUP BY TO_CHAR(date, 'Mon'), EXTRACT(YEAR FROM date), EXTRACT(MONTH FROM date)
ORDER BY EXTRACT(MONTH FROM date), EXTRACT(YEAR FROM date)
```

Grid: 1 panel, `w:24 h:8`, in a collapsible row (collapsed).

### 8. Recent Activities Table (always visible)

Full-width table with clickable activity names linking to Activity Detail dashboard.

| Column | Source | Format |
|--------|--------|--------|
| Name | `name` | data link to `/d/veloai-activity/activity-detail?var-activity_id=${__data.fields.id}&from=${__data.fields.from_ts}&to=${__data.fields.to_ts}` |
| Date | `date` | `YYYY-MM-DD` |
| Distance | `distance_m / 1000` | `XX.X km` |
| Elevation | `elevation_m` | `XXX m` |
| Duration | `duration_s` | `h:mm` |
| Avg HR | `avg_hr` | bpm, COALESCE to `—` |
| Avg Power | `avg_power` | W, COALESCE to `—` |
| Type | `sport_type` | mapped to display name |
| Device | `device` | as-is |

Filtered by `$__timeFilter(date)`, ordered by `date DESC`, limit 50.

Grid: 1 panel, `w:24 h:10`, at the bottom.

---

## Panel Grid Layout Summary

```
y:0   [Rides w:4] [Dist w:4] [Elev w:4] [Dur w:4] [Cal w:4] [TSB w:4]    Hero Stats
y:4   [vs Prev w:6] [Avg/Ride w:6] [Streak w:6] [Days Since w:6]          Comparison
y:7   ▼ Volume (collapsible row, expanded)
y:8     [Distance bars w:12] [Elevation bars w:12]
y:16  ▼ Training Load (collapsible row, expanded)
y:17    [TSS bars w:12] [CTL/ATL/TSB lines w:12]
y:25  ▶ Personal Records (collapsible row, collapsed)
y:26    [Longest w:6] [Most Elev w:6] [Fastest w:6] [Best Power w:6]
y:30  ▶ Monthly Comparison (collapsible row, collapsed)
y:31    [Year-over-year table w:24]
y:39  ▼ Recent Activities
y:40    [Activities table w:24]
```

---

## Files Changed

| File | Action |
|------|--------|
| `grafana/dashboards/overview.json` | Full rewrite |
| `grafana/dashboards/fitness-trends.json` | Add dashboard links |
| `grafana/dashboards/activity.json` | Add dashboard links |

No ingestor or schema changes needed — all data already exists in the database.

---

## TSS Calculation in SQL

The training load section needs TSS calculated in SQL (currently only done in Python). Uses hardcoded defaults matching `fitness.py` (`DEFAULT_FTP = 150`, `DEFAULT_THRESHOLD_HR = 170`):

```sql
-- Per-activity TSS: power-based preferred, HR-based fallback
CASE
  WHEN avg_power > 0 AND avg_power IS NOT NULL THEN
    (duration_s * avg_power * (avg_power::float / 150)) / (150 * 3600) * 100
  WHEN avg_hr > 0 AND avg_hr IS NOT NULL THEN
    (duration_s / 3600.0) * POWER(avg_hr::float / 170, 2) * 100
  ELSE 0
END AS tss
```

These defaults (FTP=150W, threshold HR=170bpm) match the Python fallbacks in `ingestor/fitness.py:7-8`. The Python code auto-estimates from 95th percentile of historical data, but for dashboard SQL, hardcoded values are simpler and sufficient.

---

## Out of Scope (Future Work)

- GPS route map (separate sub-project)
- Activity detail enhancements (HR/power zones, splits)
- Additional dashboards (Weekly Summary, Equipment)
- km/mi unit toggle variable
