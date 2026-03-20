# Progression Dashboard Redesign — Deep Analytics

**Date:** 2026-03-20
**Status:** Draft
**Replaces:** Current `all-time-progression.json` (5 scatter plots, 5 cumulative charts, 1 fitness chart, 4 monthly bars, 1 annual table)

## Goal

Redesign the All Time Progression dashboard to answer "Am I getting better?" across every dimension — speed, power, efficiency, fitness, training intensity — with coaching-grade analytics. Add FTP progression, best effort tracking, training zone polarization, personal records, and rolling averages on all progression charts.

## Data Sources

All queries use existing tables — no schema changes required.

| Table | Used For |
|---|---|
| `activities` | Stat cards, progression scatter plots, cumulative charts, monthly trends, YoY, personal records |
| `activity_streams` | FTP estimation, best efforts (1/5/20min power), NP computation, zone polarization |
| `athlete_stats` | CTL/ATL/TSB fitness history |
| `sync_state` | Configured FTP reference line |

## Template Variables

| Variable | Type | Values | Purpose |
|---|---|---|---|
| `sport_type` | custom | All, Outdoor, Zwift, E-Bike, Indoor | Filter all panels (matches Overview) |

Sport type filter SQL pattern (same as Overview):
```sql
AND (('${sport_type}' = 'all') OR sport_type = '${sport_type}')
```

## Dashboard Settings

- **UID:** `veloai-progression`
- **Default time range:** `now-1y` to `now`
- **graphTooltip:** 2 (shared crosshair)
- **Nav links:** Overview, Activity Detail
- **Refresh:** none (historical data, no auto-refresh needed)

## Layout — 9 Sections, ~28 Panels

### Section 1: All-Time Stats (row y=0)

6 stat cards across full width. Semantic background colors matching Overview.

| Card | SQL | Color | Unit |
|---|---|---|---|
| Total Distance | `SUM(distance_m)/1000` | dark-blue | km |
| Total Elevation | `SUM(elevation_m)` | dark-blue | m |
| Total Rides | `COUNT(*)` | dark-blue | none |
| Total Hours | `SUM(duration_s)` | dark-blue | dthms |
| Current FTP | Same query as Overview (sync_state → stream est. → p95 fallback) | dark-purple | watt |
| Peak CTL | `MAX(ctl) FROM athlete_stats` | dark-purple | none |

All stat cards: `colorMode: "background"`, `graphMode: "none"`, `noValue: "No data"`.

Stats are **not** filtered by time range — they show all-time totals. Sport type filter applies.

### Section 2: Performance Progression (row y=4)

6 timeseries charts in a 2×3 grid. Each chart shows:
- **Scatter points** (per-ride values, pointSize 5)
- **10-ride rolling average** (line, lineWidth 2) — computed via SQL window function
- **Linear regression** (Grafana `regression` transformation)

All charts respect `$__timeFilter(date)` and sport type filter.

| Chart | Query Filter | Color | Unit |
|---|---|---|---|
| Avg Speed | `avg_speed_kmh > 0 AND distance_m > 5000 AND is_indoor IS NOT TRUE` | #33658a | velocitykmh |
| Avg Power | `avg_power > 0 AND duration_s > 1200` | #8b5cf6 | watt |
| Normalized Power | Computed from streams (30s rolling avg → 4th power → root) | #8b5cf6 | watt |
| Efficiency Factor | NP / avg_hr (stream-computed NP, activity avg_hr) | #73bf69 | none |
| Avg HR | `avg_hr > 0` (all rides) | #F2495C | none |
| Avg Distance | `distance_m > 0` (all rides, excluding zero-distance) | #33658a | lengthkm |

**Rolling average SQL pattern** (example for speed):
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

The scatter series uses `drawStyle: "points"`. The rolling avg series uses `drawStyle: "line"`, `lineWidth: 2`, `pointSize: 0`. Regression transformation applies to the scatter series.

**NP query** (per ride, from streams):
```sql
WITH rolling AS (
  SELECT s.activity_id,
    AVG(s.power) OVER (
      PARTITION BY s.activity_id
      ORDER BY s.time_offset
      RANGE BETWEEN 29 PRECEDING AND CURRENT ROW
    ) AS rolling_30s
  FROM activity_streams s
  JOIN activities a ON a.id = s.activity_id
  WHERE s.power IS NOT NULL AND s.power > 0
    AND $__timeFilter(a.date)
    AND (('${sport_type}' = 'all') OR a.sport_type = '${sport_type}')
),
np_per_ride AS (
  SELECT activity_id,
    POWER(AVG(POWER(rolling_30s, 4)), 0.25) AS np
  FROM rolling WHERE rolling_30s IS NOT NULL
  GROUP BY activity_id
)
SELECT a.date::date AS "time",
  ROUND(n.np::numeric, 0) AS "NP",
  ROUND(AVG(n.np) OVER (ORDER BY a.date ROWS BETWEEN 9 PRECEDING AND CURRENT ROW)::numeric, 0) AS "10-ride avg"
FROM np_per_ride n
JOIN activities a ON a.id = n.activity_id
ORDER BY a.date;
```

**EF query:** Same NP CTE, then `ROUND((n.np / a.avg_hr)::numeric, 2)` with `WHERE a.avg_hr > 0`.

### Section 3: FTP & Best Efforts (row y=28)

Two half-width timeseries charts.

**FTP Progression** — line chart with area fill:
```sql
WITH monthly_ftp AS (
  SELECT date_trunc('month', a.date) AS month,
    ROUND(MAX(avg_20min) * 0.95) AS ftp_est
  FROM (
    SELECT s.activity_id,
      AVG(s.power) OVER (
        PARTITION BY s.activity_id
        ORDER BY s.time_offset
        RANGE BETWEEN 1199 PRECEDING AND CURRENT ROW
      ) AS avg_20min
    FROM activity_streams s
    JOIN activities a ON a.id = s.activity_id
    WHERE s.power IS NOT NULL AND s.power > 0
      AND (('${sport_type}' = 'all') OR a.sport_type = '${sport_type}')
  ) sub
  JOIN activities a ON a.id = sub.activity_id
  WHERE avg_20min IS NOT NULL
  GROUP BY 1
)
SELECT month AS "time", ftp_est AS "Est. FTP (W)"
FROM monthly_ftp
ORDER BY month;
```

Style: `drawStyle: "line"`, `lineWidth: 2`, `fillOpacity: 15`, color `#8b5cf6`. Unit: watt.

Configured FTP reference: second query returning horizontal line from `sync_state`:
```sql
SELECT $__timeFrom() AS "time", value::numeric AS "Configured FTP"
FROM sync_state WHERE key = 'configured_ftp' AND value::numeric > 0
UNION ALL
SELECT $__timeTo() AS "time", value::numeric
FROM sync_state WHERE key = 'configured_ftp' AND value::numeric > 0;
```
Override: dashed line, color `#8b5cf680`.

**Best Efforts Progression** — 3 series on one chart:

Per ride, compute rolling max power at 1min (59s), 5min (299s), 20min (1199s):
```sql
WITH efforts AS (
  SELECT a.date, a.id AS activity_id,
    MAX(CASE WHEN dur = 59 THEN avg_p END) AS best_1min,
    MAX(CASE WHEN dur = 299 THEN avg_p END) AS best_5min,
    MAX(CASE WHEN dur = 1199 THEN avg_p END) AS best_20min
  FROM activities a
  JOIN LATERAL (
    SELECT
      w.dur,
      AVG(s.power) OVER (
        PARTITION BY s.activity_id
        ORDER BY s.time_offset
        RANGE BETWEEN w.dur PRECEDING AND CURRENT ROW
      ) AS avg_p
    FROM activity_streams s
    CROSS JOIN (VALUES (59),(299),(1199)) AS w(dur)
    WHERE s.activity_id = a.id
      AND s.power IS NOT NULL AND s.power > 0
  ) sub ON true
  WHERE a.avg_power IS NOT NULL AND a.avg_power > 0
    AND $__timeFilter(a.date)
    AND (('${sport_type}' = 'all') OR a.sport_type = '${sport_type}')
  GROUP BY a.date, a.id
)
SELECT date::date AS "time",
  ROUND(best_1min::numeric, 0) AS "1-min",
  ROUND(best_5min::numeric, 0) AS "5-min",
  ROUND(best_20min::numeric, 0) AS "20-min"
FROM efforts
ORDER BY date;
```

Colors: 1-min `#F2495C`, 5-min `#ff9830`, 20-min `#8b5cf6`. Unit: watt. Style: points + line, `lineWidth: 1`, `pointSize: 4`.

**Note:** The LATERAL + CROSS JOIN approach may be slow with many activities. If query performance is poor, fall back to 3 separate queries (one per duration) or pre-compute in the ingestor. Test with actual data first.

### Section 4: Training Zones Over Time (row y=36)

Two half-width stacked bar charts (100% stacked).

**Monthly Power Zone Distribution:**
```sql
WITH zones AS (
  SELECT date_trunc('month', a.date) AS month,
    CASE
      WHEN s.power < ftp.val * 0.55 THEN 'Z1 Recovery'
      WHEN s.power < ftp.val * 0.75 THEN 'Z2 Endurance'
      WHEN s.power < ftp.val * 0.90 THEN 'Z3 Tempo'
      WHEN s.power < ftp.val * 1.05 THEN 'Z4 Threshold'
      WHEN s.power < ftp.val * 1.20 THEN 'Z5 VO2max'
      ELSE 'Z6 Anaerobic'
    END AS zone
  FROM activity_streams s
  JOIN activities a ON a.id = s.activity_id
  CROSS JOIN (
    SELECT COALESCE(
      NULLIF((SELECT value::numeric FROM sync_state WHERE key = 'configured_ftp'), 0),
      150
    ) AS val
  ) ftp
  WHERE s.power IS NOT NULL AND s.power > 0
    AND (('${sport_type}' = 'all') OR a.sport_type = '${sport_type}')
)
SELECT month AS "time",
  ROUND(100.0 * SUM(CASE WHEN zone = 'Z1 Recovery' THEN 1 ELSE 0 END) / COUNT(*), 1) AS "Z1 Recovery",
  ROUND(100.0 * SUM(CASE WHEN zone = 'Z2 Endurance' THEN 1 ELSE 0 END) / COUNT(*), 1) AS "Z2 Endurance",
  ROUND(100.0 * SUM(CASE WHEN zone = 'Z3 Tempo' THEN 1 ELSE 0 END) / COUNT(*), 1) AS "Z3 Tempo",
  ROUND(100.0 * SUM(CASE WHEN zone = 'Z4 Threshold' THEN 1 ELSE 0 END) / COUNT(*), 1) AS "Z4 Threshold",
  ROUND(100.0 * SUM(CASE WHEN zone = 'Z5 VO2max' THEN 1 ELSE 0 END) / COUNT(*), 1) AS "Z5 VO2max",
  ROUND(100.0 * SUM(CASE WHEN zone = 'Z6 Anaerobic' THEN 1 ELSE 0 END) / COUNT(*), 1) AS "Z6 Anaerobic"
FROM zones
GROUP BY 1 ORDER BY 1;
```

Stacked bars, each zone in Coggan color: Z1 `#6b7280`, Z2 `#3b82f6`, Z3 `#22c55e`, Z4 `#eab308`, Z5 `#f97316`, Z6 `#ef4444`. Unit: percent.

**Monthly HR Zone Distribution:** Same pattern but with HR thresholds (5-zone model):
- Z1 < 60% max HR, Z2 60-70%, Z3 70-80%, Z4 80-90%, Z5 90-100%.
- Max HR from `sync_state` key `configured_max_hr`, fallback 185.
- Colors: Z1 `#6b7280`, Z2 `#3b82f6`, Z3 `#22c55e`, Z4 `#eab308`, Z5 `#ef4444`.

### Section 5: Fitness History (row y=44)

Full-width CTL/ATL/TSB chart — same as current but with `$__timeFilter` applied:
```sql
SELECT date AS "time",
  ctl AS "Fitness (CTL)",
  atl AS "Fatigue (ATL)",
  tsb AS "Form (TSB)"
FROM athlete_stats
WHERE $__timeFilter(date)
ORDER BY date;
```

Colors: CTL `#6ed0ff`, ATL `#ff9830`, TSB `#73bf69` dashed with `fillOpacity: 10`. Same overrides as current.

### Section 6: Cumulative Totals (row y=53)

6 area charts in a 3×2 grid. All show running totals across all time (no `$__timeFilter` — cumulative should always start from first ride). Sport type filter applies.

| Chart | SQL | Color |
|---|---|---|
| Cumulative Distance | `SUM(distance_m) OVER (ORDER BY date, id) / 1000` | #33658a |
| Cumulative Elevation | `SUM(elevation_m) OVER (ORDER BY date, id)` | #ff9830 |
| Cumulative Duration | `SUM(duration_s) OVER (ORDER BY date, id) / 3600` | #6ed0ff |
| Cumulative Rides | `ROW_NUMBER() OVER (ORDER BY date, id)` | #6ed0ff |
| Cumulative TSS | `SUM(COALESCE(tss, 0)) OVER (ORDER BY date, id)` | #8b5cf6 |
| Cumulative Calories | `SUM(COALESCE(calories, 0)) OVER (ORDER BY date, id)` | #ff9830 |

Style: `drawStyle: "line"`, `fillOpacity: 15`, `lineWidth: 2`.

### Section 7: Monthly Trends (row y=69)

4 stacked bar charts in a 2×2 grid. Stacked by ride type using the established color palette: Outdoor `#33658a`, Zwift `#fc4c02`, E-Bike `#73bf69`, Indoor `#8b5cf6`.

Each chart has 4 queries (one per sport type) with `GROUP BY date_trunc('month', date)`.

| Chart | Metric | Unit |
|---|---|---|
| Monthly Distance | `SUM(distance_m)/1000` | km |
| Monthly Elevation | `SUM(elevation_m)` | m |
| Monthly Rides | `COUNT(*)` | none |
| Monthly Hours | `SUM(duration_s)/3600` | hours |

Sport type filter still applies to all queries (when filtered to e.g. "Outdoor", only Outdoor bars show).

### Section 8: Year-over-Year (row y=85)

Two panels side by side.

**YoY Monthly Distance** — grouped bar chart comparing current year vs previous year:
```sql
SELECT date_trunc('month', date) AS "time",
  ROUND(SUM(CASE WHEN EXTRACT(YEAR FROM date) = EXTRACT(YEAR FROM CURRENT_DATE) THEN distance_m ELSE 0 END) / 1000::numeric, 0) AS "This Year",
  ROUND(SUM(CASE WHEN EXTRACT(YEAR FROM date) = EXTRACT(YEAR FROM CURRENT_DATE) - 1 THEN distance_m ELSE 0 END) / 1000::numeric, 0) AS "Last Year"
FROM activities
WHERE distance_m > 0
  AND date >= date_trunc('year', CURRENT_DATE - interval '1 year')
  AND (('${sport_type}' = 'all') OR sport_type = '${sport_type}')
GROUP BY 1 ORDER BY 1;
```

Colors: This Year `#33658a`, Last Year `#33658a40` (same hue, lower opacity).

**Annual Totals** — table (same as current but with sport filter):
```sql
SELECT EXTRACT(YEAR FROM date)::int AS "Year",
  COUNT(*) AS "Rides",
  ROUND((SUM(distance_m)/1000)::numeric, 0) AS "Distance (km)",
  ROUND(SUM(elevation_m)::numeric, 0) AS "Elevation (m)",
  ROUND((SUM(duration_s)/3600.0)::numeric, 1) AS "Hours",
  ROUND((AVG(distance_m)/1000.0)::numeric, 1) AS "Avg Ride (km)",
  ROUND(AVG(avg_speed_kmh)::numeric, 1) AS "Avg Speed",
  ROUND(SUM(tss)::numeric, 0) AS "Total TSS"
FROM activities
WHERE date IS NOT NULL
  AND (('${sport_type}' = 'all') OR sport_type = '${sport_type}')
GROUP BY 1 ORDER BY 1 DESC;
```

### Section 9: Personal Records (row y=93)

Single full-width table showing all-time bests with drill-down links.

```sql
SELECT * FROM (
  SELECT 'Longest Ride' AS "Record",
    ROUND((distance_m/1000)::numeric, 1) || ' km' AS "Value",
    TO_CHAR(date, 'YYYY-MM-DD') AS "Date",
    '<a href="/d/veloai-activity/activity-details?var-activity_id=' || id || '">' || name || '</a>' AS "Ride"
  FROM activities WHERE distance_m IS NOT NULL
    AND (('${sport_type}' = 'all') OR sport_type = '${sport_type}')
  ORDER BY distance_m DESC LIMIT 1
) a
UNION ALL
SELECT * FROM (
  SELECT 'Most Elevation',
    ROUND(elevation_m::numeric, 0) || ' m',
    TO_CHAR(date, 'YYYY-MM-DD'),
    '<a href="/d/veloai-activity/activity-details?var-activity_id=' || id || '">' || name || '</a>'
  FROM activities WHERE elevation_m IS NOT NULL
    AND (('${sport_type}' = 'all') OR sport_type = '${sport_type}')
  ORDER BY elevation_m DESC LIMIT 1
) b
UNION ALL
SELECT * FROM (
  SELECT 'Fastest Avg Speed',
    ROUND(avg_speed_kmh::numeric, 1) || ' km/h',
    TO_CHAR(date, 'YYYY-MM-DD'),
    '<a href="/d/veloai-activity/activity-details?var-activity_id=' || id || '">' || name || '</a>'
  FROM activities WHERE avg_speed_kmh IS NOT NULL AND is_indoor IS NOT TRUE AND distance_m > 5000
    AND (('${sport_type}' = 'all') OR sport_type = '${sport_type}')
  ORDER BY avg_speed_kmh DESC LIMIT 1
) c
UNION ALL
SELECT * FROM (
  SELECT 'Highest Avg Power',
    ROUND(avg_power::numeric, 0) || ' W',
    TO_CHAR(date, 'YYYY-MM-DD'),
    '<a href="/d/veloai-activity/activity-details?var-activity_id=' || id || '">' || name || '</a>'
  FROM activities WHERE avg_power IS NOT NULL AND avg_power > 0
    AND (('${sport_type}' = 'all') OR sport_type = '${sport_type}')
  ORDER BY avg_power DESC LIMIT 1
) d
UNION ALL
SELECT * FROM (
  SELECT 'Longest Duration',
    ROUND(duration_s / 3600.0::numeric, 1) || ' h',
    TO_CHAR(date, 'YYYY-MM-DD'),
    '<a href="/d/veloai-activity/activity-details?var-activity_id=' || id || '">' || name || '</a>'
  FROM activities WHERE duration_s IS NOT NULL
    AND (('${sport_type}' = 'all') OR sport_type = '${sport_type}')
  ORDER BY duration_s DESC LIMIT 1
) e
UNION ALL
SELECT * FROM (
  SELECT 'Highest TSS',
    ROUND(tss::numeric, 0) || '',
    TO_CHAR(date, 'YYYY-MM-DD'),
    '<a href="/d/veloai-activity/activity-details?var-activity_id=' || id || '">' || name || '</a>'
  FROM activities WHERE tss IS NOT NULL
    AND (('${sport_type}' = 'all') OR sport_type = '${sport_type}')
  ORDER BY tss DESC LIMIT 1
) f;
```

Table config: `filterable: false`, enable HTML sanitization for links. Column overrides: "Ride" column allows HTML rendering.

## Performance Considerations

Stream-heavy queries (NP, EF, best efforts, zone polarization, FTP) scan `activity_streams` which can be large. Mitigations:

1. **NP/EF progression** — already works in current dashboard (EF panel). Scoping to `$__timeFilter` limits data scanned.
2. **Best efforts** — the LATERAL + CROSS JOIN may be slow. If so, simplify to 3 separate queries or pre-compute best efforts in the ingestor.
3. **Zone polarization** — aggregates to monthly buckets, so even large stream tables produce small result sets. Should be fast.
4. **FTP progression** — one scan per month. Bounded by number of months with data.

## What Changed vs. Current Dashboard

| Current | Redesigned |
|---|---|
| No stat cards | 6 stat cards (volume + power themed) |
| No sport filter | Sport type variable (All/Outdoor/Zwift/E-Bike/Indoor) |
| Nav link: Overview only | Nav links: Overview + Activity Detail |
| 5 scatter plots (regression only) | 6 scatter plots with 10-ride rolling avg + regression |
| No NP chart | NP progression chart |
| No FTP tracking | FTP progression (monthly, line + area) |
| No best efforts | Best 1/5/20min power per ride |
| No zone tracking | Monthly power + HR zone polarization |
| CTL/ATL/TSB (no time filter) | CTL/ATL/TSB with $__timeFilter |
| 5 cumulative charts | 6 cumulative charts (+calories) |
| 4 monthly bars (no ride type split) | 4 monthly bars stacked by ride type |
| Annual table (no filter) | Annual table + YoY grouped bar chart |
| No records | Personal records table with drill-down |
| EF ignores time filter | EF respects $__timeFilter |
| HR filtered to outdoor >5km | HR includes all rides |
| Distance filtered to outdoor >5km | Distance includes all rides with distance > 0 |
