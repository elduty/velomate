# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Documentation

Obsidian vault: `~/Library/Mobile Documents/iCloud~md~obsidian/Documents/Personal/4 - Projects/Active/VeloAI/`

- `VeloAI.md` — project overview, tech stack, roadmap
- `TODO.md` — blockers, next tasks, someday list
- `Progress Log.md` — chronological development history

## What This Is

VeloAI is a self-hosted cycling data platform (inspired by TeslaMate) for Marcin, based in São Domingos de Rana, Portugal. Data flows: Karoo 3 → Strava (ride data), Apple Watch → Health → Strava (recovery metrics), Komoot (route library). The ingestor polls Strava/Komoot, stores activities + per-second telemetry in PostgreSQL, calculates fitness metrics (CTL/ATL/TSB via EMA) locally (no Strava Premium needed), and serves Grafana dashboards. A CLI generates WhatsApp-formatted ride recommendations using fitness + weather data.

## Architecture

Three Docker Compose services on a homelab:

- **veloai-postgres** (PostgreSQL 15, port 5423) — five tables: `activities`, `activity_streams`, `athlete_stats`, `routes`, `sync_state`
- **veloai-ingestor** (Python 3.11) — polls Strava every 10min, Komoot every 1h; auto-backfills 12 months on first run; handles cross-device deduplication (Karoo > unknown/Zwift > Watch) by matching same-day activities within ±10% distance
- **veloai-grafana** (Grafana 12.0, port 3021) — dashboards provisioned from JSON files in `grafana/dashboards/`

Separate from Docker:

- **veloai CLI** (`veloai/`) — runs on Mac mini, connects to homelab DB, merges fitness + Komoot routes + Open-Meteo weather into ride recommendations

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

# Query DB directly
docker compose exec veloai-postgres psql -U veloai -c "SELECT COUNT(*) FROM activities;"

# Grafana dashboard changes: edit JSON in grafana/dashboards/, then:
docker compose restart veloai-grafana

# CLI (Mac mini only)
pip install -r requirements.txt
python3 -m veloai.cli
```

## Code Layout

- `ingestor/` — Dockerized polling service. `main.py` is the scheduler; `strava.py` and `komoot.py` handle API calls; `db.py` owns schema DDL + all upserts; `fitness.py` does EMA-based CTL/ATL/TSB calculation
- `veloai/` — CLI package. `cli.py` is the entry point; `planner.py` formats WhatsApp output; `weather.py` calls Open-Meteo; `db.py` is a read-only DB client; `keychain.py` wraps macOS Keychain
- `grafana/dashboards/` — Three dashboard JSON files (overview, fitness-trends, activity). Provisioned automatically on container start
- `grafana/provisioning/` — Grafana datasource + dashboard provider YAML configs

## Important Patterns

- **Schema lives in code**: `ingestor/db.py:create_schema()` is the source of truth for DDL. No migration tool — schema changes go there with `IF NOT EXISTS` / `ADD COLUMN IF NOT EXISTS`
- **Dedup logic**: Two strategies in `ingestor/db.py` — `find_duplicate()` (time-window + duration) for cross-device like Zwift+Watch, `find_duplicate_by_distance()` (same-day ±10% distance) for Strava-Komoot matching
- **Activity classification**: `classify_activity()` in `ingestor/db.py` infers `is_indoor` and `sport_type` from device, distance, and activity name
- **Fitness TSS**: Power-based TSS preferred over HR-based; thresholds auto-estimated from 95th percentile of historical data
- **Grafana dashboards**: Hand-edited JSON. The activity detail dashboard uses `__data.fields.id` variable to link from overview. Charts use `trend` panel type with distance-based x-axis

## Environment

Secrets in `.env` (see `.env.example`): `POSTGRES_PASSWORD`, `STRAVA_CLIENT_ID`, `STRAVA_CLIENT_SECRET`, `STRAVA_REFRESH_TOKEN`, `KOMOOT_EMAIL`, `KOMOOT_PASSWORD`, `GRAFANA_PASSWORD`.

Credentials in macOS Keychain: `openclaw/strava` (Strava OAuth), `openclaw/komoot` (Komoot login), `openclaw/veloai-db` (CLI DB password).

CLI connects to the homelab DB using `VELOAI_DB_*` env vars (defaults: host `10.7.40.15`, port `5423`, db/user `veloai`).

No test suite, no CI/CD, no linter config. Deployment is manual git pull + `docker compose up -d`.

## Known Limitations

- Komoot only exposes recorded rides, not saved/planned routes — route suggestions are limited to past activities
- All Komoot rides default to name "Ride" — dedup uses distance/elevation buckets (fragile)
- komPYoot is an unofficial Komoot API wrapper — may break if Komoot changes their internal API
- Strava API credentials stored in macOS Keychain (`openclaw/strava`), Komoot in (`openclaw/komoot`)

## Roadmap

See `TODO.md` in Obsidian vault. Key next items: calendar integration (skip days with events), weekly cron via OpenClaw (Friday morning WhatsApp), natural language ride requests, heat/UV awareness for Portuguese summers.
