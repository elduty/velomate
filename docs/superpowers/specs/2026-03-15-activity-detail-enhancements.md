# Activity Detail Enhancements — Design Spec

**Goal:** Add HR zones, power zones, per-km splits, and advanced derived metrics to the Activity Detail Grafana dashboard.

**Approach:** All new panels use SQL calculations against existing `activity_streams` data. No schema changes needed. Thresholds auto-calculated from ride history (same approach as fitness.py).

---

## Design Decisions

1. **Auto-calculated thresholds** — max HR from 95th percentile of `activities.max_hr`, FTP from 95th percentile of `activities.avg_power`. Same values used for TSS calculation.
2. **HR zones**: 5-zone model (Recovery/Endurance/Tempo/Threshold/VO2max) at 60/70/80/90% of max HR.
3. **Power zones**: 6-zone model (Recovery/Endurance/Tempo/Threshold/VO2max/Anaerobic) at 55/75/90/105/120% of FTP.
4. **Splits from stream data** — per-km splits computed by accumulating distance from speed × time delta in `activity_streams`.
5. **Advanced metrics collapsed** — normalized power, intensity factor, variability index, best efforts in a collapsed row.

---

## New Panels

### 1. HR Zones (id:31, w:12 h:8 x:0 y:18)

Type: `barchart` (horizontal bars showing time in each zone).

```sql
WITH max_hr AS (
  SELECT percentile_cont(0.95) WITHIN GROUP (ORDER BY max_hr) AS mhr
  FROM activities WHERE max_hr IS NOT NULL AND max_hr > 0
)
SELECT
  CASE
    WHEN s.hr < max_hr.mhr * 0.6 THEN 'Z1 Recovery'
    WHEN s.hr < max_hr.mhr * 0.7 THEN 'Z2 Endurance'
    WHEN s.hr < max_hr.mhr * 0.8 THEN 'Z3 Tempo'
    WHEN s.hr < max_hr.mhr * 0.9 THEN 'Z4 Threshold'
    ELSE 'Z5 VO2max'
  END AS "Zone",
  ROUND(COUNT(*)::numeric / 60, 1) AS "Minutes"
FROM activity_streams s, max_hr
WHERE s.activity_id = ${activity_id}
  AND s.hr IS NOT NULL AND s.hr > 0
GROUP BY "Zone"
ORDER BY "Zone";
```

Format: `table`. Orientation: horizontal. Color overrides per zone:
- Z1: `#73bf69` (green)
- Z2: `#6ed0ff` (blue)
- Z3: `#ffcc00` (yellow)
- Z4: `#ff9830` (orange)
- Z5: `#f2495c` (red)

### 2. Power Zones (id:32, w:12 h:8 x:12 y:18)

Type: `barchart` (horizontal bars). Only shows data if the activity has power data.

```sql
WITH ftp AS (
  SELECT percentile_cont(0.95) WITHIN GROUP (ORDER BY avg_power) AS ftp_val
  FROM activities WHERE avg_power IS NOT NULL AND avg_power > 0
)
SELECT
  CASE
    WHEN s.power < ftp.ftp_val * 0.55 THEN 'Z1 Recovery'
    WHEN s.power < ftp.ftp_val * 0.75 THEN 'Z2 Endurance'
    WHEN s.power < ftp.ftp_val * 0.90 THEN 'Z3 Tempo'
    WHEN s.power < ftp.ftp_val * 1.05 THEN 'Z4 Threshold'
    WHEN s.power < ftp.ftp_val * 1.20 THEN 'Z5 VO2max'
    ELSE 'Z6 Anaerobic'
  END AS "Zone",
  ROUND(COUNT(*)::numeric / 60, 1) AS "Minutes"
FROM activity_streams s, ftp
WHERE s.activity_id = ${activity_id}
  AND s.power IS NOT NULL AND s.power > 0
GROUP BY "Zone"
ORDER BY "Zone";
```

Same color scheme as HR zones (Z6 Anaerobic = `#b877d9` purple).

### 3. Per-km Splits (id:33, w:24 h:10 x:0 y:26)

Type: `table`. Shows each kilometer with avg speed, avg HR, avg power, elevation change.

```sql
WITH deltas AS (
  SELECT
    time_offset,
    hr, power, speed_kmh, altitude_m,
    COALESCE(speed_kmh, 0) / 3600.0 *
      (time_offset - LAG(time_offset, 1, time_offset) OVER (ORDER BY time_offset)) AS dist_delta
  FROM activity_streams
  WHERE activity_id = ${activity_id}
),
cumulative AS (
  SELECT
    time_offset, hr, power, speed_kmh, altitude_m,
    SUM(dist_delta) OVER (ORDER BY time_offset ROWS UNBOUNDED PRECEDING) AS dist_km
  FROM deltas
),
splits AS (
  SELECT
    FLOOR(dist_km)::int + 1 AS "KM",
    ROUND(AVG(speed_kmh)::numeric, 1) AS "Avg Speed",
    ROUND(AVG(hr)::numeric, 0) AS "Avg HR",
    ROUND(AVG(NULLIF(power, 0))::numeric, 0) AS "Avg Power",
    ROUND((MAX(altitude_m) - MIN(altitude_m))::numeric, 0) AS "Elev (m)",
    COUNT(*) AS seconds
  FROM cumulative
  WHERE dist_km >= 0
  GROUP BY FLOOR(dist_km)::int
)
SELECT
  "KM",
  "Avg Speed",
  "Avg HR",
  COALESCE("Avg Power"::text, '—') AS "Avg Power",
  "Elev (m)",
  TO_CHAR(seconds * interval '1 second', 'MI:SS') AS "Time"
FROM splits
ORDER BY "KM";
```

Format: `table`. Add color thresholds on "Avg HR" column to match zone colors.

### 4. Advanced Metrics Row (collapsed, id:106, y:66)

Collapsed row titled "Advanced Metrics". Four stat panels inside.

**Normalized Power (id:34, w:6 h:4 x:0 y:67):**

```sql
WITH rolling AS (
  SELECT
    time_offset,
    AVG(power) OVER (ORDER BY time_offset ROWS BETWEEN 29 PRECEDING AND CURRENT ROW) AS rolling_30s
  FROM activity_streams
  WHERE activity_id = ${activity_id}
    AND power IS NOT NULL AND power > 0
)
SELECT ROUND(POWER(AVG(POWER(rolling_30s, 4)), 0.25)::numeric, 0) AS "NP (W)"
FROM rolling
WHERE rolling_30s IS NOT NULL;
```

Unit: `watt`.

**Intensity Factor (id:35, w:6 h:4 x:6 y:67):**

```sql
WITH ftp AS (
  SELECT percentile_cont(0.95) WITHIN GROUP (ORDER BY avg_power) AS ftp_val
  FROM activities WHERE avg_power IS NOT NULL AND avg_power > 0
),
rolling AS (
  SELECT AVG(power) OVER (ORDER BY time_offset ROWS BETWEEN 29 PRECEDING AND CURRENT ROW) AS rolling_30s
  FROM activity_streams
  WHERE activity_id = ${activity_id}
    AND power IS NOT NULL AND power > 0
)
SELECT ROUND((POWER(AVG(POWER(rolling_30s, 4)), 0.25) / ftp.ftp_val)::numeric, 2) AS "IF"
FROM rolling, ftp
WHERE rolling_30s IS NOT NULL
GROUP BY ftp.ftp_val;
```

Thresholds: <0.75 green (easy), 0.75-0.90 yellow (moderate), >0.90 red (hard).

**Variability Index (id:36, w:6 h:4 x:12 y:67):**

```sql
WITH rolling AS (
  SELECT AVG(power) OVER (ORDER BY time_offset ROWS BETWEEN 29 PRECEDING AND CURRENT ROW) AS rolling_30s
  FROM activity_streams
  WHERE activity_id = ${activity_id}
    AND power IS NOT NULL AND power > 0
),
np AS (
  SELECT POWER(AVG(POWER(rolling_30s, 4)), 0.25) AS np_val FROM rolling WHERE rolling_30s IS NOT NULL
)
SELECT ROUND((np.np_val / a.avg_power)::numeric, 2) AS "VI"
FROM np, activities a
WHERE a.id = ${activity_id} AND a.avg_power > 0;
```

Thresholds: <1.05 green (steady), 1.05-1.15 yellow (variable), >1.15 red (very variable).

**Best Efforts (id:37, w:6 h:4 x:18 y:67):**

```sql
WITH rolling AS (
  SELECT
    AVG(power) OVER (ORDER BY time_offset ROWS BETWEEN 299 PRECEDING AND CURRENT ROW) AS avg_5min,
    AVG(power) OVER (ORDER BY time_offset ROWS BETWEEN 1199 PRECEDING AND CURRENT ROW) AS avg_20min
  FROM activity_streams
  WHERE activity_id = ${activity_id}
    AND power IS NOT NULL AND power > 0
)
SELECT
  ROUND(MAX(avg_5min)::numeric, 0) AS "Best 5min (W)",
  ROUND(MAX(avg_20min)::numeric, 0) AS "Best 20min (W)"
FROM rolling;
```

---

## Grid Layout

```
y:0-5   Stats (existing)
y:6     Route Map (existing, h:12)
y:18    HR Zones (h:8, w:12) + Power Zones (h:8, w:12)   ← NEW
y:26    Per-km Splits (h:10, w:24)                         ← NEW
y:36    Speed & Elevation (existing, shift from y:18)
y:48    HR & Power (existing, shift from y:30)
y:58    Cadence (existing, shift from y:40)
y:66    ▶ Advanced Metrics (collapsed)                     ← NEW
y:67      [NP w:6] [IF w:6] [VI w:6] [Best Efforts w:6]
```

Existing chart panels shift down by +18 (8 for zones + 10 for splits).

---

## Files Changed

| File | Action |
|------|--------|
| `grafana/dashboards/activity.json` | Add 7 new panels, shift existing charts down |

No schema or ingestor changes needed.

---

## Notes

- Power zones and advanced metrics will show "No data" for activities without power data — this is expected and correct.
- When the Assioma power meter arrives, these panels will automatically populate with real data.
- The Best Efforts query uses window functions that scan all stream rows — may be slow for very long activities (>4h). Acceptable for now.
- The per-km splits elevation calculation uses MAX-MIN per km, which is net elevation change, not total climbing. Good enough for v1.
