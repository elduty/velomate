# Changelog

## v1.3.0 — 2026-04-10

Major release: ride analytics depth, dashboard overhaul, new metrics.

### New Features

- **Aerobic decoupling** — stored per ride, trended on All Time Progression. Measures cardiac drift (first-half vs second-half EF) to track aerobic fitness (#91)
- **Auto interval detection** — Coggan-style classification (sprint / anaerobic / vo2 / threshold / sweetspot / tempo) from power streams, stored in `ride_intervals` table, displayed on Activity Details + monthly distribution on All Time Progression (#91)
- **VI-aware TSS** — rides with Variability Index > 1.30 (urban stop-and-go) now use avg_power instead of NP for TSS/IF calculation, preventing overestimation on high-variability rides. `METRICS_VERSION` 9→10 (#97)
- **HR TSS uses LTHR** — the HR-only TSS fallback path now derives LTHR (~89% of max HR per Friel) instead of using max HR directly, fixing a ~21% underestimation on rides without power (#99)
- **Estimated FTP preserved as diagnostic** — `sync_state.estimated_ftp` always holds the algorithmic estimate, even when `VELOMATE_FTP` is configured. Overview shows Configured + Estimated FTP side-by-side (#95, #96)
- **Calories** — total and delta on Overview, filling the Period Summary grid to 10 stats (#104)
- **W/kg (NP-based)** — per-ride NP/weight on Activity Details + NP/kg Trend on All Time Progression. Uses per-ride `ride_weight` column so historical values are preserved if weight changes (#104)
- **`VELOMATE_WEIGHT` env var** — rider weight in kg, stored per ride like `ride_ftp`. Enables W/kg panels. Weight changes preserve historical rides (#104)
- **`VELOMATE_BACKFILL_MONTHS` env var** — configurable backfill window. Default `12` months, `0` for full Strava history (#89)
- **Auto-backfill on window extension** — increasing `VELOMATE_BACKFILL_MONTHS` triggers re-backfill on next restart (#90)

### Dashboard Overhaul

- **Overview redesigned** — single comprehensive dashboard with progressive disclosure. Period Summary uses compact 2×5 grid at full width. vs Previous Period, Trends (6 charts), and Ride Patterns sections expanded by default. Default time range changed to 7 days. Outdoor Records and Ride Map removed (already on All Time Progression) (#92, #102, #103)
- **All Time Progression** — added Aerobic Decoupling Trend, NP/kg Trend, Monthly Interval Distribution. Rebuilt layout with no gaps or overlaps, full-width utilisation (#91, #104)
- **Activity Details** — added Power Distribution histogram (25W buckets, 7-zone coloured), Detected Intervals table, W/kg panel. Advanced metrics row expanded to 8 (added aerobic decoupling, W/kg) (#91, #104)
- **Tooltip consistency** — all colour-coded panels now have matching emoji icons in tooltips. Dashboard Conventions rule documented in CLAUDE.md with unified 7-emoji palette (#98, #100)
- **Panel count**: 43 + 41 + 44 = 128 panels (up from 98)

### Fixes

- Δ Avg cards NULL-safe CTE pattern replacing COALESCE→0 (#93, #94)
- Power Distribution Z7 missing `unit: min` override causing separate Y-axis (#101)
- Power Distribution buckets widened 10W→25W for cleaner histograms (#101)
- Δ Avg HR missing `unit: bpm` (#103)
- Outdoor Records + Ride Map gained `sport_type` filtering (#102)
- Rolling Weekly Volume description notes it's always all-sport (#102)

### Docs

- Feature gap analysis: 8 top gaps, 12 secondary, 13 non-gaps (`docs/features-analysis-06apr26.md`)
- Dashboard Conventions section in CLAUDE.md: tooltip formatting, colour icon palette, compressed palette rules
- W/kg metric + weight-preservation design documented in CLAUDE.md and README

### Stats

- 443 tests (up from 370)
- 46 commits, 16 PRs (#89–#104)

## v1.2.0 — 2026-03-27

### New Features

- **`--destination` flag** — plan point-to-point routes to a named place or coordinates (`--destination Cascais` or `--destination "38.69,-9.42"`)
- **Unified location parsing** — `--start`, `--waypoints`, and `--destination` all accept both place names and `lat,lng` coordinates
- **Corridor waypoints** — when `--destination` + `--distance` is set and the direct route is shorter than target, smart waypoints are added in a corridor to pad the distance
- **There-and-back routing** — `--destination Cascais --loop` routes to the destination and back home
- **Coordinate bounds validation** — `parse_location` rejects out-of-range lat/lng values before they hit Valhalla

### Changes

- **Waypoints separator** changed from comma to semicolon (`--waypoints "Cascais;Estoril"`) to avoid ambiguity with coordinate notation
- **`--duration`/`--distance` now optional** when `--destination` is set
- **`--loop` auto-disables** when `--destination` is set (override with explicit `--loop`)
- **Log warnings** for flag clashes: baseline exceeds target distance, explicit waypoints skip padding

### Fixes

- CI venv pip bootstrap on macOS runner (stale `/tmp` venv, broken pip RECORD)
- Push-to-github script: auto-generated commit messages, graceful first-push, MESSAGE override

### Stats

- 370 tests (up from 331)
- 10 files changed, 716 insertions

## v1.1.0 — 2026-03-25

Metric accuracy overhaul, per-ride FTP, user feedback fixes.

- NP reverted to 30s SMA (Coggan standard, matches GoldenCheetah)
- Per-ride FTP with 90-day rolling backfill
- IF, TRIMP, VI computed as single source of truth in ingestor
- Default passwords in `.env.example` (zero-edit `docker compose up`)
- Venv setup documented in README
- Windows emoji encoding fix in map preview

## v1.0.0 — 2026-03-21

Initial release. Strava ingestion, 3 Grafana dashboards (98 panels), CLI route planner with 10 data sources.
