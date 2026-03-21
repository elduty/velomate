# Dashboard Audit — 2026-03-21

Results from UX layout review + functionality/value review.

## UX Layout Issues

### Must Fix

**A1+A2 (Activity): Overlapping panels + 4-row gap**
- Panels id=31, 32, 42, 41 overlap with id=403 (Power Zones by Kilometer) at y=34-48
- 4-row empty gap at y=24-28 between stat cards and zone-by-km panels
- Full relayout needed — corrected y-positions:

| Panel | Title | Fixed gridPos |
|---|---|---|
| 400 | HR Zones by Km | h=6 w=24 x=0 y=24 |
| 403 | Power Zones by Km | h=6 w=24 x=0 y=30 |
| 31 | HR Zones | h=8 w=12 x=0 y=36 |
| 32 | Power Zones | h=8 w=12 x=12 y=36 |
| 42 | HR Distribution | h=8 w=12 x=0 y=44 |
| 41 | Power Distribution | h=8 w=12 x=12 y=44 |
| 40 | Power Duration Curve | h=8 w=24 x=0 y=52 |
| 33 | Per-km Splits | h=8 w=24 x=0 y=60 |
| 20 | Speed & Elevation | h=10 w=24 x=0 y=68 |
| 21 | HR & Power | h=10 w=24 x=0 y=78 |
| 22 | Cadence & Grade | h=10 w=24 x=0 y=88 |
| 401 | Power Distribution (histogram) | h=8 w=12 x=0 y=98 |
| 402 | Power vs HR | h=8 w=12 x=12 y=98 |

### Should Fix

**O1 (Overview): 20 stat cards before any chart**
- Wall of numbers — first chart doesn't appear until y=18
- Fix: Collapse "vs Previous Period" row by default (`collapsed: true`)

**A5 (Activity): No row headers**
- 100+ grid unit dashboard with no visual section separators
- Add row headers: Route, Power Quality, Zone Analysis, Splits, Ride Telemetry, Distributions

**P2 (Progression): No collapsed rows**
- 132 grid units total, all sections expanded
- Collapse lower-priority sections by default: Cumulative Totals, Monthly Trends, Year-over-Year, Ride Map

### Nice to Fix

**O3 (Overview): 6 stacked full-width trend charts**
- 48 grid units of stacked timeseries — scroll fatigue
- Option: pair side-by-side at w=12 (3 rows instead of 6)

**A4 (Activity): Duplicate "Power Distribution"**
- id=41 (zone-colored barchart) and id=401 (plain histogram) show same data
- Remove id=401 or rename to "Power Histogram"

**A6/O4 (Activity/Overview): Oversized maps**
- Activity Route h=12, Overview Ride Map h=14
- Trim to h=10/h=12

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
