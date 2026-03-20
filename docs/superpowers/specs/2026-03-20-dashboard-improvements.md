# Dashboard Improvements — All Three Dashboards

**Date:** 2026-03-20
**Status:** Draft

## Goal

Apply 20 improvements across all 3 VeloAI Grafana dashboards: bug fixes, consistency fixes, new panel types (heatmap, histogram, gauge, pie, XY chart, state timeline, candlestick, bar gauge), Grafana features (annotations, fill-between, thresholds as regions, time comparison, config from query), and ingestor pre-calculation of NP/EF/Work.

## Scope

### Group 1: Bug Fixes & Consistency

**1. Overview stat cards missing sport_type filter**
- File: `overview.json`
- Panels: Rides (id 1), Distance (id 2), Elevation (id 3), Duration (id 4)
- Fix: Add `AND (('${sport_type}' = 'all') OR sport_type = '${sport_type}')` to each stat query
- Also affects: TSS, Avg Power, Avg HR, Avg Speed, Avg Cadence, Calories stat cards — audit all

**2. "Days Since Ride" hard-coded sport list**
- File: `overview.json`
- Fix: Replace `WHERE sport_type IN ('cycling_outdoor', ...)` with `WHERE (('${sport_type}' = 'all') OR sport_type = '${sport_type}')`

**3. Missing units on Overview timeseries**
- File: `overview.json`
- Fix: Add `"unit": "lengthkm"` to Distance timeseries defaults, `"unit": "h"` to Duration timeseries defaults

**4. Inconsistent noValue**
- All 3 files
- Fix: Add `"noValue": "No data"` to all panels missing it (primarily Progression dashboard panels)

**5. Speed filtering documentation**
- Files: `overview.json`, `all-time-progression.json`
- Fix: Add `description` tooltip to Speed panels explaining the `>5km outdoor only` filter

### Group 2: Overview Dashboard Enhancements

**6. Enable manual annotations**
- File: `overview.json`
- Add to dashboard root:
```json
"annotations": {
  "list": [
    {
      "builtIn": 1,
      "datasource": { "type": "grafana", "uid": "-- Grafana --" },
      "enable": true,
      "hide": true,
      "iconColor": "rgba(0, 211, 255, 1)",
      "name": "Annotations & Alerts",
      "type": "dashboard"
    }
  ]
}
```
- This enables click-to-annotate on any timeseries panel (CTL/ATL/TSB, daily charts)

**7. Fill between CTL and ATL**
- File: `overview.json`, panel CTL/ATL/TSB (id 201)
- Add override on "Fatigue (ATL)" series:
```json
{"id": "custom.fillBelowTo", "value": "Fitness (CTL)"}
```
- Fill color inherits from ATL series (#ff9830) at low opacity — visually shows TSB as the gap width
- Also apply to Progression dashboard panel 40

**8. Training frequency heatmap**
- File: `overview.json`
- New panel after fitness section, full width (w=24, h=8)
- Type: `heatmap`
- Query:
```sql
SELECT
  EXTRACT(ISODOW FROM date)::int AS "Day",
  EXTRACT(HOUR FROM date)::int AS "Hour",
  COUNT(*) AS "Rides"
FROM activities
WHERE $__timeFilter(date)
  AND (('${sport_type}' = 'all') OR sport_type = '${sport_type}')
GROUP BY 1, 2;
```
- Format: `table`
- Y-axis: Day of week (1=Mon, 7=Sun)
- X-axis: Hour (0-23)
- Color scheme: blues (matching volume theme)
- Title: "When You Ride"

**13. TSB Gauge**
- File: `overview.json`
- New gauge panel alongside TSB stat card (w=6, h=3)
- Type: `gauge`
- Query: same as existing TSB stat (`SELECT ROUND(tsb::numeric, 1) FROM athlete_stats ORDER BY date DESC LIMIT 1`)
- Thresholds: red (<-10), orange (-10 to 0), green (0 to 15), blue (>15)
- Show threshold labels and markers
- Min: -30, Max: 30

**14. Ride type donut**
- File: `overview.json`
- New pie chart panel (w=6, h=6)
- Type: `piechart`
- Query:
```sql
SELECT
  CASE sport_type
    WHEN 'cycling_outdoor' THEN 'Outdoor'
    WHEN 'zwift' THEN 'Zwift'
    WHEN 'ebike' THEN 'E-Bike'
    WHEN 'cycling_indoor' THEN 'Indoor'
  END AS "Type",
  COUNT(*) AS "Rides"
FROM activities
WHERE $__timeFilter(date)
  AND (('${sport_type}' = 'all') OR sport_type = '${sport_type}')
GROUP BY 1;
```
- Format: `table`
- Pie mode: `donut`
- Color overrides: Outdoor #33658a, Zwift #fc4c02, E-Bike #73bf69, Indoor #8b5cf6
- Title: "Ride Types"

**20. Per-panel time override: 6-week fitness stat**
- File: `overview.json`
- New stat card (w=6, h=3) showing CTL change over 6 weeks
- Query: `SELECT ROUND((a.ctl - b.ctl)::numeric, 1) AS "6w CTL Δ" FROM (SELECT ctl FROM athlete_stats ORDER BY date DESC LIMIT 1) a, (SELECT ctl FROM athlete_stats WHERE date <= CURRENT_DATE - 42 ORDER BY date DESC LIMIT 1) b`
- Panel time override: not needed since query is self-contained
- Color: green if positive (fitness gaining), red if negative
- Thresholds: red (<0), green (>=0)

### Group 3: Activity Detail Enhancements

**10. Zone state timeline**
- File: `activity.json`
- New panel below route map (w=24, h=4)
- Type: `state-timeline`
- Query:
```sql
SELECT
  (a.date + s.time_offset * interval '1 second') AS time,
  CASE
    WHEN s.hr < mhr.val * 0.60 THEN 'Z1 Recovery'
    WHEN s.hr < mhr.val * 0.70 THEN 'Z2 Endurance'
    WHEN s.hr < mhr.val * 0.80 THEN 'Z3 Tempo'
    WHEN s.hr < mhr.val * 0.90 THEN 'Z4 Threshold'
    ELSE 'Z5 VO2max'
  END AS "HR Zone"
FROM activity_streams s
JOIN activities a ON a.id = s.activity_id
CROSS JOIN (
  SELECT COALESCE(
    NULLIF((SELECT value::numeric FROM sync_state WHERE key = 'configured_max_hr'), 0),
    185
  ) AS val
) mhr
WHERE s.activity_id = ${activity_id}
  AND s.hr IS NOT NULL AND s.hr > 0
ORDER BY s.time_offset;
```
- Format: `table`
- Value mappings with zone colors: Z1 #6b7280, Z2 #3b82f6, Z3 #22c55e, Z4 #eab308, Z5 #ef4444
- Title: "HR Zone Timeline"
- Description: "Which heart rate zone you were in at each moment of the ride."

**11. Power histogram**
- File: `activity.json`
- New panel (w=12, h=8)
- Type: `histogram`
- Query:
```sql
SELECT power AS "Power (W)"
FROM activity_streams
WHERE activity_id = ${activity_id}
  AND power IS NOT NULL AND power > 0;
```
- Format: `table`
- Bucket size: 20 (watts)
- Color: #8b5cf6 (purple, matching power theme)
- Fill opacity: 80
- Title: "Power Distribution"

**12. Power zone bands on HR & Power chart**
- File: `activity.json`
- Existing HR & Power trend panel
- Add thresholds on the power axis with `thresholdStyle: "area"`:
  - Z1 < 55% FTP: #6b728020
  - Z2 < 75% FTP: #3b82f620
  - Z3 < 90% FTP: #22c55e20
  - Z4 < 105% FTP: #eab30820
  - Z5 < 120% FTP: #f9731620
  - Z6 > 120% FTP: #ef444420
- Note: FTP value needs to be known. Use absolute thresholds based on configured FTP (165W default): 91, 124, 149, 173, 198.
- Low opacity (alpha 20) so bands don't obscure data

**9. Power vs HR scatter (XY Chart)**
- File: `activity.json`
- New panel (w=12, h=8)
- Type: `xychart`
- Query:
```sql
SELECT
  s.hr AS "Heart Rate",
  s.power AS "Power (W)"
FROM activity_streams s
WHERE s.activity_id = ${activity_id}
  AND s.hr IS NOT NULL AND s.hr > 0
  AND s.power IS NOT NULL AND s.power > 0
  AND s.time_offset % 5 = 0
ORDER BY s.time_offset;
```
- Format: `table`
- X field: Heart Rate, Y field: Power
- Point size: 3, opacity: 0.3
- Color: #8b5cf6
- Title: "Power vs Heart Rate"
- Description: "Scatter plot showing the relationship between power output and heart rate.\n\nA tight cluster = good aerobic efficiency.\nPoints drifting right over time = cardiac drift (fatigue)."

### Group 4: Progression Dashboard Enhancements

**17. Native time comparison for YoY**
- File: `all-time-progression.json`
- Replace panel 70 (YoY Monthly Distance) with a simpler approach:
  - Single query: `SELECT date_trunc('month', date) AS time, ROUND((SUM(distance_m)/1000.0)::numeric, 1) AS "Distance (km)" FROM activities WHERE distance_m > 0 AND (('${sport_type}' = 'all') OR sport_type = '${sport_type}') GROUP BY 1 ORDER BY 1;`
  - Add `timeShift: "1y"` to create a second panel copy showing last year
  - OR: keep current two-query approach if native time comparison requires feature toggles
- Decision: keep current approach — it works and Grafana 12 time comparison may require feature flag enablement. Not worth the risk.

**18. Weekly power ranges (candlestick-style)**
- File: `all-time-progression.json`
- New panel in the Performance Progression section (w=12, h=8)
- Type: `candlestick`
- Query:
```sql
SELECT
  date_trunc('week', date) AS time,
  MIN(avg_power) AS "open",
  MAX(avg_power) AS "high",
  MIN(avg_power) AS "low",
  ROUND(AVG(avg_power)::numeric, 0) AS "close"
FROM activities
WHERE avg_power IS NOT NULL AND avg_power > 0
  AND (('${sport_type}' = 'all') OR sport_type = '${sport_type}')
  AND $__timeFilter(date)
GROUP BY 1
ORDER BY 1;
```
- Format: `table`
- Color: up=#73bf69, down=#F2495C
- Title: "Weekly Power Range"
- Description: "Min/avg/max power per week.\n\nGreen = avg power rising. Red = avg power falling."

### Group 5: Cross-Dashboard & Ingestor

**15. Pre-calculate NP, EF, Work in ingestor**
- File: `ingestor/db.py` — add columns: `ALTER TABLE activities ADD COLUMN IF NOT EXISTS np FLOAT; ALTER TABLE activities ADD COLUMN IF NOT EXISTS ef FLOAT; ALTER TABLE activities ADD COLUMN IF NOT EXISTS work_kj FLOAT;`
- File: `ingestor/fitness.py` — after TSS calculation, compute NP/EF/Work per activity:
  - NP: 30s rolling avg → 4th power → mean → 4th root (from stream data)
  - EF: NP / avg_hr (if both available)
  - Work: avg_power * duration_s / 1000 (kJ)
- Update `activities` table with computed values
- Dashboard queries can then use `a.np`, `a.ef`, `a.work_kj` instead of CTE stream scans

**16. Config from query results for dynamic thresholds**
- Deferred — the current CROSS JOIN approach works. Config from query results requires restructuring panel queries significantly. Lower priority.

**19. Bar gauge for zones**
- Deferred — existing barchart panels work well. Retro-LCD mode is cosmetic. Lower priority.

## Implementation Order

1. Bug fixes (items 1-5) — all 3 dashboards
2. Overview enhancements (items 6, 7, 8, 13, 14, 20)
3. Activity Detail enhancements (items 9, 10, 11, 12)
4. Progression enhancements (item 18, keep 17 as-is)
5. Ingestor changes (item 15)

Items 16 and 19 deferred.

## What Changed

| Item | Dashboard | Type |
|---|---|---|
| 1-5 | All 3 | Bug fix |
| 6 | Overview | Annotations enabled |
| 7 | Overview + Progression | Fill between CTL/ATL |
| 8 | Overview | New heatmap panel |
| 9 | Activity | New XY chart panel |
| 10 | Activity | New state timeline panel |
| 11 | Activity | New histogram panel |
| 12 | Activity | Thresholds on existing panel |
| 13 | Overview | New gauge panel |
| 14 | Overview | New pie chart panel |
| 15 | Ingestor | Schema + code change |
| 17 | Progression | No change (keep current) |
| 18 | Progression | New candlestick panel |
| 20 | Overview | New stat card with self-contained query |
