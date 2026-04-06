# Changelog

## v1.2.1 — 2026-04-06

### New Features

- **`VELOMATE_BACKFILL_MONTHS` env var** — configurable backfill window. Default `12` months (previous hardcoded behaviour), `0` pulls full Strava history (slow, multi-day due to rate limits) (#89)
- **Auto-backfill on window extension** — increasing `VELOMATE_BACKFILL_MONTHS` on a running deployment now auto-triggers a re-backfill on the next restart to pull the additional history. Decreasing it logs a non-destructive note with a manual-prune SQL escape hatch; data retention is explicitly separated from the backfill horizon (#90)

### Docs

- Feature gap analysis comparing VeloMate against Strava Premium, GoldenCheetah, intervals.icu, TrainingPeaks, Xert, WKO5, Garmin, TrainerRoad, Wahoo SystM (`docs/features-analysis-06apr26.md`) — 8 top gaps, 12 secondary, 13 explicit non-gaps, grounded competitor cheat sheet
- Cluster A implementation plan: cardiac drift trend + auto interval detection (`docs/superpowers/plans/2026-04-06-ride-analytics-depth.md`)
- `TODO.md` populated with a prioritised backlog keyed to the features analysis
- `velomate-features-designer` project-local Claude skill for ongoing feature-gap evaluation
- Pruned obsolete docs: feedback-log, findings-metric-accuracy, golden-record, reddit-launch, fix-act-runner, old metric-consistency plan, Gitea runner setup
- Preserved the GoldenCheetah NP-vs-xPower metric naming lesson in `CLAUDE.md` for future validation comparisons

### Stats

- 405 tests (up from 370)

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
