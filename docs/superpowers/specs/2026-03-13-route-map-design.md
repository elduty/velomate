# Route Map — Design Spec

**Goal:** Add GPS route visualization to the Activity Detail dashboard (with metric color overlay) and a lifetime ride heatmap to the Overview dashboard.

**Approach:** Use Grafana's built-in Geomap panel — no plugins. Two new panels: one on activity detail, one on overview.

---

## Design Decisions

1. **Grafana Geomap panel** — built-in, supports route layers and heatmap layers natively from lat/lng data.
2. **Metric overlay via `$map_metric` variable** — user picks speed/HR/power from a dropdown; route points colored by that metric's value.
3. **Lifetime heatmap on overview** — collapsible row (collapsed by default), shows all-time ride density.
4. **Sampling for performance** — heatmap queries every 5th point (`time_offset % 5 = 0`) to keep ~1.8M rows manageable.
5. **Indoor activities excluded** — filtered by `lat IS NOT NULL`.

---

## Panel 1: Activity Route Map (Activity Detail Dashboard)

**Location:** Full width (`w:24 h:12`), inserted between the stat rows (y:6) and the existing Speed & Elevation chart. All existing charts shift down by +12.

**Template variable `$map_metric`:**
```json
{
  "name": "map_metric",
  "type": "custom",
  "label": "Map color",
  "query": "speed_kmh,hr,power",
  "current": { "text": "speed_kmh", "value": "speed_kmh" },
  "options": [
    { "text": "Speed", "value": "speed_kmh", "selected": true },
    { "text": "Heart Rate", "value": "hr", "selected": false },
    { "text": "Power", "value": "power", "selected": false }
  ]
}
```

**SQL query:**
```sql
SELECT
  lat AS "latitude",
  lng AS "longitude",
  CASE '${map_metric}' WHEN 'speed_kmh' THEN speed_kmh WHEN 'hr' THEN hr WHEN 'power' THEN power END AS "metric",
  time_offset
FROM activity_streams
WHERE activity_id = ${activity_id}
  AND lat IS NOT NULL
  AND lng IS NOT NULL
ORDER BY time_offset;
```
Format: `table`

**Geomap config:**
- Layer type: `markers` with size 2-3px (at per-second GPS density, points overlap to look like a continuous line — Grafana has no native route layer)
- Color field: `metric` with `color.mode: "continuous-GrYlRd"` (built-in green→yellow→red continuous scheme)
- Default zoom: auto-fit to route bounds
- Tooltip: show metric value + time_offset
- Base map: standard OpenStreetMap

---

## Panel 2: Lifetime Ride Heatmap (Overview Dashboard)

**Location:** New collapsible row on the overview dashboard, collapsed by default. Placed after Training Load row (y:25), before Personal Records. Personal Records and subsequent sections shift down.

**Row panel:** `title: "Ride Map"`, `collapsed: true`

**Geomap panel:** `w:24 h:14` (maps need vertical space to be useful)

**SQL query (sampled):**
```sql
SELECT
  s.lat AS "latitude",
  s.lng AS "longitude"
FROM activity_streams s
JOIN activities a ON a.id = s.activity_id
WHERE s.lat IS NOT NULL
  AND s.lng IS NOT NULL
  AND s.time_offset % 5 = 0
ORDER BY a.date, s.time_offset
LIMIT 500000;
```
Format: `table`

Note: No `$__timeFilter` — this is a lifetime view, always shows all rides.

**Geomap config:**
- Layer type: `heatmap` (point density visualization)
- Weight field: none (each point has equal weight)
- Radius: 4-6px
- Blur: 10-15px
- Default center: `{ lat: 38.69, lng: -9.32 }` (São Domingos de Rana)
- Default zoom: 11
- Base map: standard OpenStreetMap (dark variant if available)

---

## Grid Layout Changes

### Activity Detail Dashboard (`activity.json`)

Current layout:
```
y:0-5   Stats (2 rows of stat panels)
y:6     Speed & Elevation chart (h:12)
y:18    HR & Power chart (h:10)
y:28    Cadence chart (h:8)
```

New layout:
```
y:0-5   Stats (unchanged)
y:6     Route Map (h:12) ← NEW
y:18    Speed & Elevation chart (h:12, was y:6)
y:30    HR & Power chart (h:10, was y:18)
y:40    Cadence chart (h:8, was y:28)
```

### Overview Dashboard (`overview.json`)

Insert new collapsed row between Training Load (y:16) and Personal Records (y:25). Since the collapsed row takes h:1 and its child panel lives inside the row's `panels` array, subsequent sections shift by +1:

```
y:16    ▼ Training Load (unchanged)
y:25    ▶ Ride Map (collapsed, h:1) ← NEW
y:26      [Heatmap w:24 h:14]   (inside collapsed row's panels array)
y:26    ▶ Personal Records (was y:25, +1)
y:31    ▶ Monthly Comparison (was y:30, +1)
y:40    ▼ Recent Activities (was y:39, +1)
```

---

## Files Changed

| File | Action |
|------|--------|
| `grafana/dashboards/activity.json` | Add route map panel + `map_metric` variable, shift existing charts down |
| `grafana/dashboards/overview.json` | Add ride map collapsible row + heatmap panel, adjust y-positions |

No schema or ingestor changes — GPS data already exists in `activity_streams.lat/lng`.

---

## Performance Considerations

- **Activity route:** ~3600 points per ride (1 per second for a 1-hour ride). Geomap handles this easily.
- **Lifetime heatmap:** With sampling (`time_offset % 5 = 0`), ~360K points for 500 activities. Grafana Geomap should handle this, but if it's slow, increase sampling to `% 10` or add a `LIMIT 200000`.
- **No index on lat/lng needed** — queries filter by `activity_id` (indexed) or scan `activity_streams` once for the heatmap.
