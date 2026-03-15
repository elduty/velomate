# Overview Dashboard Redesign

## Goal

Replace the current overview dashboard with a cleaner layout focused on 9 key metrics with daily charts, driven entirely by Grafana's time picker. All existing secondary content moves to collapsed rows.

## Dashboard Settings

- **Title:** "Overview" (drop "VeloAI" prefix from all dashboards)
- **UID:** `veloai-main` (unchanged)
- **graphTooltip:** `2` — shared crosshair + tooltip across all charts (TeslaMate style)
- **Default time range:** `now-7d` to `now`
- **Variables:** `group_by` (day/week/month) — unchanged
- **Links:** Activity Detail, Fitness Trends, Weekly Report, Training Log, Year in Review

## Layout

### Row 1: Stats (y=0, h=3)

9 stat cards across the top. Each uses `$__timeFilter(date)`.

| Panel | Query | Unit | Color |
|-------|-------|------|-------|
| Rides | `COUNT(*)` | none | `#6ed0ff` |
| Distance | `SUM(distance_m) / 1000` | km | `#33658a` |
| Elevation | `SUM(elevation_m)` | m | `#ff9830` |
| Duration | `SUM(duration_s)` | seconds (Grafana formats) | `#6ed0ff` |
| Calories | `SUM(calories)` | kcal | `#F2495C` |
| TSS | `SUM(tss)` | none | `#8b5cf6` |
| Avg Power | `ROUND(AVG(avg_power))` where `avg_power > 0` | W | `#8b5cf6` |
| Avg Speed | `ROUND(AVG(avg_speed_kmh), 1)` where `avg_speed_kmh > 0 AND distance_m > 5000` | km/h | `#73bf69` |
| Avg HR | `ROUND(AVG(avg_hr))` where `avg_hr > 0` | bpm | `#F2495C` |

Width: each card is `24/9 ≈ 2.67` — use alternating 3 and 2 widths to fill 24 columns. Simplest: 6 cards at w=3 (18) + 3 cards at w=2 (6) = 24. Put the smaller cards on the simpler metrics (Rides, Avg Power, Avg HR).

### Row 2: Daily Charts (y=3, h=8, 4×2 grid)

8 timeseries panels in a 2-column layout. All use `generate_series(date_trunc('${group_by}', $__timeFrom()::timestamptz), date_trunc('${group_by}', $__timeTo()::timestamptz), '1 ${group_by}'::interval)` with LEFT JOIN.

| Panel | Position | Series | Color |
|-------|----------|--------|-------|
| Distance | w=12, x=0, y=3 | Outdoor, Zwift, E-Bike, Indoor (stacked) | `#33658a`, `#fc4c02`, `#73bf69`, `#8b5cf6` |
| Elevation | w=12, x=12, y=3 | Outdoor, Zwift, E-Bike (stacked) | `#33658a`, `#fc4c02`, `#73bf69` |
| Duration | w=12, x=0, y=11 | Single series: `SUM(duration_s)/3600.0` as hours | `#6ed0ff` |
| Calories | w=12, x=12, y=11 | Single series: `SUM(calories)` | `#F2495C` |
| Avg Power | w=12, x=0, y=19 | Single series: `AVG(avg_power)` where `avg_power > 0` | `#8b5cf6` |
| Avg Speed | w=12, x=12, y=19 | Single series: `AVG(avg_speed_kmh)` where `avg_speed_kmh > 0 AND distance_m > 5000` | `#73bf69` |
| Avg HR | w=12, x=0, y=27 | Single series: `AVG(avg_hr)` where `avg_hr > 0` | `#F2495C` |
| TSS | w=12, x=12, y=27 | Single series: `SUM(tss)` | `#8b5cf6` |

Chart style: `drawStyle: "bars"`, `fillOpacity: 80`, `barWidthFactor: 0.6`. Distance and Elevation use `stacking: { mode: "normal" }`.

### Collapsed Sections (below charts)

All existing secondary content moves to collapsed rows, unchanged except for title cleanup:

1. **Fitness** — CTL/ATL/TSB timeseries chart (from `athlete_stats`)
2. **Outdoor Records & Progress** — 8 record stat panels + 2 progression charts (all `is_indoor IS NOT TRUE`)
3. **Ride Map** — Lifetime heatmap (geomap, outdoor only)
4. **Year in Review** — 4 stat cards + YoY table
5. **Activities** — Table with drill-down links

## Dashboard Renaming

Remove "VeloAI" prefix from all dashboard titles:

| Current | New |
|---------|-----|
| VeloAI Overview | Overview |
| VeloAI Year in Review | Year in Review |

Activity Detail, Fitness Trends, Weekly Report, Training Log already don't have the prefix.

## Color Palette (unchanged)

| Metric | Hex |
|--------|-----|
| Outdoor cycling | `#33658a` |
| Zwift | `#fc4c02` |
| E-Bike | `#73bf69` |
| Indoor trainer | `#8b5cf6` |
| Distance | `#33658a` |
| Elevation | `#ff9830` |
| Duration / general | `#6ed0ff` |
| Power / TSS | `#8b5cf6` |
| Speed | `#73bf69` |
| HR / Calories | `#F2495C` |

## What Gets Removed

- Sport type dropdown variable (already removed)
- "Dist vs Prev Period" comparison panel
- "Avg per Ride" panel
- "Weekly Streak" panel
- "Days Since Ride" panel
- "This Year Distance/Elevation/Rides/Duration" stat cards (moved to Year in Review dashboard)

These metrics are either redundant with the new layout or better served by the Year in Review dashboard.

## What Stays (collapsed)

Everything in collapsed sections is preserved as-is. No query changes.
