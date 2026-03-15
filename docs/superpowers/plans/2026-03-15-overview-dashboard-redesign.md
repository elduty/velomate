# Overview Dashboard Redesign — Implementation Plan

> **For agentic workers:** REQUIRED: Use superpowers:subagent-driven-development (if subagents available) or superpowers:executing-plans to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Rebuild the overview dashboard with 9 stat cards + 8 daily charts as hero content, all secondary panels collapsed, shared crosshair enabled.

**Architecture:** Single Grafana dashboard JSON file rewrite. Extract collapsed section panels from the current file, build new hero panels, reassemble. Also rename Year in Review dashboard title.

**Tech Stack:** Grafana 12.4 dashboard JSON, PostgreSQL queries.

---

## Chunk 1: Build the new overview dashboard

### Task 1: Read current dashboard, extract collapsed sections

**Files:**
- Read: `grafana/dashboards/overview.json`

- [ ] **Step 1: Read the current overview.json and identify all panels that belong to collapsed sections**

The collapsed sections to preserve are:
- Fitness row (id=101) + CTL/ATL/TSB chart (id=14)
- Outdoor Records & Progress row (id=103) + all record/progression panels inside it
- Ride Map row (id=105) + geomap panel (id=30)
- Year in Review row (id=106) + all YiR panels inside it
- Activities row (id=104) + activities table (id=20)

Note their exact JSON. These will be copied verbatim into the new file.

- [ ] **Step 2: Note the current templating, links, and dashboard-level settings for reference**

### Task 2: Write the new overview.json

**Files:**
- Modify: `grafana/dashboards/overview.json`

- [ ] **Step 3: Write the complete new dashboard JSON**

Structure:
```
Dashboard settings:
  title: "Overview"
  uid: "veloai-main"
  graphTooltip: 2
  time: now-7d to now
  templating: group_by variable only
  links: 5 dashboard links

Panels:
  1. Stats row (9 stat panels, y=0, h=3)
  2. Daily charts (8 timeseries panels, y=3, 4 rows of 2, h=8 each)
  3. Collapsed: Fitness (y=35)
  4. Collapsed: Outdoor Records & Progress (y=36)
  5. Collapsed: Ride Map (y=37)
  6. Collapsed: Year in Review (y=38)
  7. Collapsed: Activities (y=39)
```

Stat panel SQL patterns:
- Totals: `SELECT ROUND(COALESCE(SUM(col), 0)::numeric, N) AS "Label" FROM activities WHERE $__timeFilter(date);`
- Averages: `SELECT ROUND(AVG(col)::numeric, N) AS "Label" FROM activities WHERE $__timeFilter(date) AND col > 0;`
- Avg Speed adds: `AND distance_m > 5000`
- Count: `SELECT COUNT(*) AS "Rides" FROM activities WHERE $__timeFilter(date);`

Chart SQL pattern (single series):
```sql
SELECT gs AS "time", COALESCE(AGG(a.col), 0) AS "Label"
FROM generate_series(
  date_trunc('${group_by}', $__timeFrom()::timestamptz),
  date_trunc('${group_by}', $__timeTo()::timestamptz),
  '1 ${group_by}'::interval
) gs
LEFT JOIN activities a ON date_trunc('${group_by}', a.date) = gs [AND filters]
GROUP BY gs ORDER BY gs;
```

Chart SQL pattern (stacked by type — Distance, Elevation):
Same pattern but with `AND a.sport_type = 'cycling_outdoor'` etc. as separate queries (refId A/B/C/D).

Stat card widths (total = 24):
- Rides: w=2, x=0
- Distance: w=3, x=2
- Elevation: w=3, x=5
- Duration: w=3, x=8
- Calories: w=3, x=11
- TSS: w=3, x=14
- Avg Power: w=2, x=17
- Avg Speed: w=3, x=19
- Avg HR: w=2, x=22

- [ ] **Step 4: Validate JSON**

Run: `python3 -c "import json; json.load(open('grafana/dashboards/overview.json')); print('valid')"`
Expected: `valid`

- [ ] **Step 5: Commit**

```bash
git add grafana/dashboards/overview.json
git commit -m "feat(grafana): redesign overview — 9 stats, 8 daily charts, shared crosshair"
```

### Task 3: Rename Year in Review dashboard

**Files:**
- Modify: `grafana/dashboards/year-in-review.json`

- [ ] **Step 6: Change title from "VeloAI Year in Review" to "Year in Review"**

- [ ] **Step 7: Validate JSON**

Run: `python3 -c "import json; json.load(open('grafana/dashboards/year-in-review.json')); print('valid')"`
Expected: `valid`

- [ ] **Step 8: Commit**

```bash
git add grafana/dashboards/year-in-review.json
git commit -m "chore(grafana): drop VeloAI prefix from Year in Review title"
```

### Task 4: Push and verify

- [ ] **Step 9: Push all commits**

```bash
git push
```

- [ ] **Step 10: Verify on server**

Pull on server, restart Grafana, check:
1. Overview loads with 9 stat cards across the top
2. 8 daily charts below in 2-column layout
3. Hovering one chart shows crosshair on all others
4. Collapsed sections expand correctly
5. Time picker changes update all panels
6. group_by dropdown switches between day/week/month
