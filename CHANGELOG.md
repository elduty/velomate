# Changelog

## v1.4.0 — 2026-04-13

Performance modeling, climb detection, and quality-of-life improvements.

### New Features

- **Critical Power / W' modeling** — Monod-Scherrer 2-parameter fit from mean maximal power at 5 standard durations (1-20 min). Quality gate (R² >= 0.9, >= 4 durations) with graceful fallback to rolling 20-min x 0.95. Replaces the old FTP estimate as the algorithmic diagnostic. CP/W' Progression chart on All Time Progression
- **W'bal time series** — per-second anaerobic battery gauge on Activity Details. Skiba differential model with GoldenCheetah tau. Shows when you drained, how close to empty, where you recovered. Min W'bal and Time below 25% stat cards
- **Durability Profile** — 1st half vs 2nd half power comparison on Activity Details. Ratio-based bars showing how much power you retained at each duration. Durability Index stat card (threshold-coloured green/yellow/red)
- **Training Monotony & Strain** — Foster overreaching warning on Overview. Monotony = mean/stdev of daily TSS over 7 days, Strain = weekly TSS x Monotony. Includes rest days via generate_series
- **Climb detection** — RDP (Ramer-Douglas-Peucker) algorithm for elevation profile simplification + Strava segment enrichment. Named Strava segments where available, RDP detection fills gaps. Strava scoring for categorisation (length x gradient). Detected Climbs table on Activity Details
- **Strava OAuth** — `velomate auth` CLI command. Manual paste flow, works from any machine without port forwarding

### Fixes

- numpy added to container requirements (was missing, broke CP estimation)
- Power-Duration Curve panel type fixed (xychart → barchart)
- W'bal panel type fixed (timeseries → trend, matching existing telemetry)
- Overview Monotony/Strain layout (alongside CTL chart)
- All Time Progression layout cleanup (no gaps, full-width utilisation)
- METRICS_VERSION reset now preserves Strava segment data
- Climb detection: multiple iterations to handle rolling terrain (dynamic thresholds, boundary trimming, RDP rewrite)

### Migration from v1.3.0

1. **Pull and rebuild:**
   ```bash
   git pull && docker compose build && docker compose up -d
   ```

2. **New env var (optional):**
   ```bash
   VELOMATE_WEIGHT=75   # enables W/kg (if not set in v1.3.0)
   ```

3. **Automatic on first restart:**
   - CP/W' estimate computed from existing power stream data
   - W'bal computed for all rides with power data
   - Climbs detected from elevation profiles + Strava segments backfilled
   - New tables (`cp_estimates`, `ride_climbs`) and columns (`w_bal`) created automatically
   - No METRICS_VERSION bump — existing TSS/NP/IF unchanged

4. **Strava OAuth (new users):**
   ```bash
   python3 -m velomate.cli auth
   ```
   Replaces the manual curl flow for getting a refresh token.

5. **Breaking changes:** None.

### Stats

- 483 tests (up from 443)
- 48 commits since v1.3.0

## v1.3.0 — 2026-04-10

Major release: ride analytics depth, dashboard overhaul, new metrics.

### New Features

- **Aerobic decoupling** — stored per ride, trended on All Time Progression. Measures cardiac drift (first-half vs second-half EF) to track aerobic fitness
- **Auto interval detection** — Coggan-style classification (sprint / anaerobic / vo2 / threshold / sweetspot / tempo) from power streams, stored in `ride_intervals` table, displayed on Activity Details + monthly distribution on All Time Progression
- **VI-aware TSS** — rides with Variability Index > 1.30 (urban stop-and-go) now use avg_power instead of NP for TSS/IF calculation, preventing overestimation on high-variability rides
- **HR TSS uses LTHR** — the HR-only TSS fallback path now derives LTHR (~89% of max HR per Friel) instead of using max HR directly, fixing a ~21% underestimation on rides without power
- **Estimated FTP preserved as diagnostic** — `sync_state.estimated_ftp` always holds the algorithmic estimate, even when `VELOMATE_FTP` is configured. Overview shows Configured + Estimated FTP side-by-side
- **Calories** — total and delta on Overview, filling the Period Summary grid to 10 stats
- **W/kg (NP-based)** — per-ride NP/weight on Activity Details + NP/kg Trend on All Time Progression. Uses per-ride `ride_weight` column so historical values are preserved if weight changes
- **`VELOMATE_WEIGHT` env var** — rider weight in kg, stored per ride like `ride_ftp`. Enables W/kg panels. Weight changes preserve historical rides
- **`VELOMATE_BACKFILL_MONTHS` env var** — configurable backfill window. Default `12` months, `0` for full Strava history
- **Auto-backfill on window extension** — increasing `VELOMATE_BACKFILL_MONTHS` triggers re-backfill on next restart

### Dashboard Overhaul

- **Overview redesigned** — single comprehensive dashboard with progressive disclosure. Period Summary uses compact 2×5 grid at full width. vs Previous Period, Trends (6 charts), and Ride Patterns sections expanded by default. Default time range changed to 7 days. Outdoor Records and Ride Map removed (already on All Time Progression)
- **All Time Progression** — added Aerobic Decoupling Trend, NP/kg Trend, Monthly Interval Distribution. Rebuilt layout with no gaps or overlaps, full-width utilisation
- **Activity Details** — added Power Distribution histogram (25W buckets, 7-zone coloured), Detected Intervals table, W/kg panel. Advanced metrics row expanded to 8 (added aerobic decoupling, W/kg)
- **Tooltip consistency** — all colour-coded panels now have matching emoji icons in tooltips
- **Panel count**: 43 + 41 + 44 = 128 panels (up from 98)

### Fixes

- Δ Avg cards NULL-safe CTE pattern replacing COALESCE→0
- Power Distribution Z7 missing `unit: min` override causing separate Y-axis
- Power Distribution buckets widened 10W→25W for cleaner histograms
- Δ Avg HR missing `unit: bpm`
- Outdoor Records + Ride Map gained `sport_type` filtering
- Rolling Weekly Volume description notes it's always all-sport

### Migration from v1.2.0

1. **Pull and rebuild:**
   ```bash
   git pull && docker compose build && docker compose up -d
   ```

2. **New env vars (optional):**
   ```bash
   # Add to .env if desired:
   VELOMATE_WEIGHT=75          # your weight in kg — enables W/kg panels
   VELOMATE_BACKFILL_MONTHS=12 # default, increase for more history
   ```

3. **Automatic on first restart:**
   - `METRICS_VERSION` 9→10 triggers full recalculation of TSS/IF (VI-aware routing) and TRIMP (LTHR fix). This runs once and takes a few minutes depending on ride count.
   - New DB columns (`aerobic_decoupling`, `ride_weight`, `ride_intervals` table) are created automatically via `ALTER TABLE ... ADD COLUMN IF NOT EXISTS`.
   - NP, EF, aerobic decoupling, and intervals are computed for all rides with power stream data.
   - If `VELOMATE_WEIGHT` is set, all rides are stamped with `ride_weight` for W/kg.

4. **Dashboard changes load automatically** — Grafana provisioning picks up the updated JSON files. No manual import needed. Clear browser cache if panels look stale.

5. **Breaking changes:** None. All changes are additive. Existing data is preserved; derived metrics are recalculated with corrected formulas.

### Stats

- 443 tests (up from 370)

## v1.2.0 — 2026-03-27

Point-to-point route planning.

### New Features

- **`--destination` flag** — plan point-to-point routes to a named place or coordinates (`--destination Cascais` or `--destination "38.69,-9.42"`)
- **Unified location parsing** — `--start`, `--waypoints`, and `--destination` all accept both place names and `lat,lng` coordinates
- **Corridor waypoints** — when `--destination` + `--distance` is set and the direct route is shorter than target, smart waypoints are added in a corridor to pad the distance
- **There-and-back routing** — `--destination Cascais --loop` routes to the destination and back home
- **Coordinate bounds validation** — `parse_location` rejects out-of-range lat/lng values before they hit Valhalla

### Breaking Changes

- **Waypoints separator** changed from comma to semicolon (`--waypoints "Cascais;Estoril"`) to avoid ambiguity with coordinate notation

### Changes

- **`--duration`/`--distance` now optional** when `--destination` is set
- **`--loop` auto-disables** when `--destination` is set (override with explicit `--loop`)
- **Log warnings** for flag clashes: baseline exceeds target distance, explicit waypoints skip padding

### Fixes

- CI venv pip bootstrap on macOS runner (stale `/tmp` venv, broken pip RECORD)
- Push-to-github script: auto-generated commit messages, graceful first-push, MESSAGE override

### Usage

```bash
python3 -m velomate.cli plan --destination Cascais
python3 -m velomate.cli plan --destination Cascais --waypoints "Oeiras;Estoril"
python3 -m velomate.cli plan --destination Cascais --loop
python3 -m velomate.cli plan --destination Cascais --distance 50km
```

### Stats

- 370 tests (up from 331)

## v1.1.0 — 2026-03-25

Metric accuracy overhaul, per-ride FTP, user feedback fixes.

### New Features

- **IF, VI, TRIMP** stored per ride (previously only existed as Grafana SQL)
- **NP** computed in Python using Coggan 30s SMA (matches GoldenCheetah)
- **Per-ride FTP** — historical rides preserve their TSS/IF accuracy via `ride_ftp` column + 90-day rolling backfill
- **Z7 Neuromuscular** (>150% FTP) added to all power zone panels
- **`VELOMATE_RESTING_HR`** — configure resting heart rate for TRIMP
- **`VELOMATE_RESET_RIDE_FTP=1`** — one-shot flag to reset all per-ride FTP values

### Fixes

- TRIMP: HRR capped at 1.0 (no more exponential blowup when HR exceeds configured max)
- TSS: uses per-ride FTP, not current global FTP
- Configured FTP stamps all rides directly (no more stream re-estimation)
- Decoupling includes coasting samples
- FTP/HR fallbacks standardised across all Grafana panels
- Config changes trigger automatic recalculation
- Default passwords in `.env.example` (zero-edit `docker compose up`)
- Windows emoji encoding fix in map preview

### Breaking Changes

- **`METRICS_VERSION` 6→7** — first startup recalculates all metrics (automatic, may take a minute)
- **Configured FTP overrides estimation** — `VELOMATE_FTP` now applies to all rides directly. Previously it was only a fallback when stream-based estimation returned no result
- **Resting HR changes reset TRIMP** — previously had no server-side effect

### Upgrade

```bash
git pull && docker compose up -d --build
# Wait for "Calculated N days of fitness data" in logs
```

Optional `.env` additions:
```bash
VELOMATE_RESTING_HR=60
VELOMATE_FTP=175
```

## v1.0.0 — 2026-03-21

Initial release. Strava ingestion, 3 Grafana dashboards (98 panels), CLI route planner with 10 data sources.
