# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Documentation

Obsidian vault: `~/Library/Mobile Documents/iCloud~md~obsidian/Documents/Personal/4 - Projects/Active/VeloAI/`

- `VeloAI.md` — project overview, tech stack, roadmap
- `TODO.md` — blockers, next tasks, someday list
- `Progress Log.md` — chronological development history

## What This Is

VeloAI is a self-hosted cycling data platform (inspired by TeslaMate). Any device that syncs to Strava works (Karoo, Garmin, Wahoo, Apple Watch, Zwift). The ingestor polls Strava, stores activities + per-second telemetry in PostgreSQL, calculates fitness metrics (CTL/ATL/TSB via EMA) locally (no Strava Premium needed), and serves Grafana dashboards. A CLI generates ride recommendations and creates Valhalla GPX routes.

## Architecture

Three Docker Compose services on a server:

- **veloai-postgres** (PostgreSQL 15, port 5423) — five tables: `activities`, `activity_streams`, `athlete_stats`, `routes`, `sync_state`
- **veloai-ingestor** (Python 3.11) — polls Strava every 10min; auto-backfills 12 months on first run; handles cross-device deduplication when multiple devices record the same ride (keeps the record with the richest data — power > HR > distance) by matching same-day activities within ±10% distance
- **veloai-grafana** (Grafana 12.4, port 3021) — dashboards provisioned from JSON files in `grafana/dashboards/`

Separate from Docker:

- **veloai CLI** (`veloai/`) — runs on local machine, connects to server DB, generates ride recommendations + Valhalla GPX routes with smart waypoint selection

## Key Commands

```bash
# Deploy / rebuild all services
docker compose up -d
docker compose up -d --build          # after ingestor code changes

# Logs
docker compose logs -f ingestor

# Force fitness recalculation
docker compose exec veloai-ingestor python3 -c \
  "from db import get_connection; from fitness import recalculate_fitness; recalculate_fitness(get_connection())"

# Reclassify all activities using Strava's type field (one-time migration)
docker compose exec veloai-ingestor python3 main.py reclassify

# Query DB directly
docker compose exec veloai-postgres psql -U veloai -c "SELECT COUNT(*) FROM activities;"

# Grafana dashboard changes: edit JSON in grafana/dashboards/, then:
docker compose restart veloai-grafana

# CLI (local machine only)
pip install -r requirements.txt
python3 -m veloai.cli
```

## Code Layout

- `ingestor/` — Dockerized polling service. `main.py` is the scheduler; `strava.py` handles Strava API calls; `db.py` owns schema DDL + all upserts; `fitness.py` does EMA-based CTL/ATL/TSB calculation + NP/EF/Work pre-calculation per activity
- `veloai/` — CLI package. `cli.py` is the entry point; `planner.py` formats WhatsApp output; `weather.py` calls Open-Meteo; `db.py` is a read-only DB client; `config.py` loads YAML config + env vars; `route_planner.py` + `route_generator.py` handle Valhalla route creation
- `grafana/dashboards/` — Three dashboard JSON files (overview, activity, all-time-progression). Provisioned automatically on container start
- `grafana/provisioning/` — Grafana datasource + dashboard provider YAML configs

## Important Patterns

- **Schema lives in code**: `ingestor/db.py:create_schema()` is the source of truth for DDL. No migration tool — schema changes go there with `IF NOT EXISTS` / `ADD COLUMN IF NOT EXISTS`
- **Dedup logic**: `find_duplicate()` in `ingestor/db.py` — time-window + duration matching for cross-device duplicates (e.g., Zwift + Watch recording the same session). Data richness scoring determines which record to keep
- **Activity classification**: Only cycling activities are ingested (Ride, VirtualRide, EBikeRide filtered at Strava sync). `classify_activity()` in `ingestor/db.py` classifies into: `cycling_outdoor`, `cycling_indoor`, `zwift`, `ebike` based on device/trainer/distance
- **Fitness TSS**: Power-based TSS preferred over HR-based; thresholds auto-estimated from 95th percentile of historical data. `VELOAI_FTP` and `VELOAI_MAX_HR` env vars override auto-estimation and are persisted to `sync_state` for dashboard queries
- **Fitness recalculation**: Runs on startup, after each sync, and daily at 00:05 — ensures rest days show CTL/ATL decay through today. Also pre-calculates NP, EF, Work (kJ) per activity from stream data (skips already-computed activities)
- **Grafana dashboards**: Three dashboards (Overview, Activity Details, All Time Progression). Hand-edited JSON. All use `graphTooltip: 2` for shared crosshair. Activity Details uses `trend` panel type with distance-based x-axis, `barchart` for HR/power zones by kilometer, `xychart` for power vs HR scatter. All Time Progression uses `candlestick` for weekly power ranges. Stat cards use semantic background colors (dark-blue = volume, dark-purple = power, dark-orange = body metrics)
- **Dashboard panel types**: `stat`, `timeseries`, `table`, `geomap`, `barchart`, `trend`, `piechart`, `gauge`, `xychart`, `candlestick`, `row`
- **Dashboard color palette**: Outdoor=#33658a, Zwift=#fc4c02, E-Bike=#73bf69, Indoor=#8b5cf6. Zone colors follow Coggan standard (gray→blue→green→yellow→orange→red). Green/red reserved for delta comparisons only

## Environment

**Ingestor (Docker):** Secrets in `.env` (see `.env.example`): `POSTGRES_PASSWORD`, `STRAVA_CLIENT_ID`, `STRAVA_CLIENT_SECRET`, `STRAVA_REFRESH_TOKEN`, `GRAFANA_PASSWORD`, `VELOAI_MAX_HR`, `VELOAI_FTP`.

**CLI (local):** Configuration in `~/.config/veloai/config.yaml` (see `config.example.yaml`). Supports env var overrides and `password_cmd` for secret managers (Keychain, 1Password, Vault, etc.). No hardcoded credentials or personal data in codebase.

260 pytest tests covering pure functions, fitness calculations, and dashboard structure validation (requires Python 3.10+ for union type syntax). No CI/CD. Deployment is manual git pull + `docker compose up -d`.

## Known Limitations

- Komoot highlights API (vector tiles) is unofficial — may change if Komoot updates their tile server
- Overpass API has strict rate limits — multiple route intelligence queries in sequence can trigger 429 errors

## Roadmap

See `TODO.md` in Obsidian vault.
