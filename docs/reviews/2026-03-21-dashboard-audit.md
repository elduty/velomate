# Dashboard Audit — 2026-03-21

Results from UX layout review + functionality/value review.

## UX Layout Issues

### Must Fix

**A1+A2 (Activity): Overlapping panels + 4-row gap** — DONE (PR #37)
- ~~Panels id=31, 32, 42, 41 overlap with id=403 at y=34-48~~
- ~~4-row empty gap at y=24-28~~
- Fixed: full relayout with correct y-positions, no overlaps

### Should Fix

**O1 (Overview): Stat cards before first chart** — WON'T FIX
- 8 summary + 8 delta + 4 fitness stat cards before the first chart
- User prefers all sections expanded — no collapsing

**A5 (Activity): No row headers** — DONE (PR #37)
- ~~100+ grid unit dashboard with no visual section separators~~
- Added 7 row headers: Route, Power Quality, Zone Analysis, Power Duration, Splits, Ride Telemetry, Distributions

**P2 (Progression): Long dashboard, no collapsed rows** — WON'T FIX
- 132 grid units total, all sections expanded
- User prefers all sections expanded — no collapsing

### Nice to Fix

**O3 (Overview): 6 stacked full-width trend charts**
- 48 grid units of stacked timeseries — scroll fatigue
- Option: pair side-by-side at w=12 (3 rows instead of 6)

**A4 (Activity): Duplicate "Power Distribution"**
- id=41 (zone-colored barchart) and id=401 (plain histogram) show same data
- Remove id=401 or rename to "Power Histogram"

**A6 (Activity): Oversized route map** — DONE (PR #37)
- ~~Activity Route h=12~~ → trimmed to h=10

**O4 (Overview): Oversized ride map**
- Overview Ride Map h=14 — trim to h=12

## Panel Removal Candidates

From functionality review — panels scored value 1-2:

| Dashboard | Panel ID | Title | Score | Why Remove |
|---|---|---|---|---|
| Progression | 55 | Cumulative Calories | 1 | Strava estimate, not accurate, not actionable |
| Progression | 53 | Cumulative Rides | 2 | Monotonic line, total in stat card already |
| Progression | 52 | Cumulative Duration | 2 | Same — total hours in stat card |
| Activity | 401 | Power Distribution (histogram) | 2 | Redundant with id=41 zone-colored barchart |
| Overview | 604 | Avg Speed & Cadence | 2 | Speed = route/wind noise, cadence barely changes |
| Overview | 605 | Calories & Rides | 2 | Calories = Strava estimate, rides in stat card |
| Overview | 308 | Delta Avg Speed | 2 | Period avg speed delta too noisy |
| Overview | 401 | Form Gauge | 3 | Redundant with id=222 TSB stat (has text labels) |
