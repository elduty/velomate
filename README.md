# VeloAI 🚴

A self-hosted cycling data platform — automatic ride ingestion from Strava, Grafana dashboards for analytics, and intelligent route planning.

Inspired by [TeslaMate](https://github.com/teslamate-org/teslamate). Works with any device that syncs to Strava.

## Features

### Data Ingestion
- Polls Strava every 10 minutes for new rides
- Stores full per-second telemetry (HR, power, cadence, speed, altitude, GPS)
- Calculates CTL/ATL/TSB fitness metrics locally (no Strava Premium needed)
- FTP auto-estimated from rolling 90-day best 20-minute power, or configured manually
- Smart deduplication when multiple devices record the same ride

### Grafana Dashboards (5 dashboards)
- **Overview** — hero stats, weekly/monthly volume, training load (TSS), records & progress trends, year in review, lifetime ride heatmap
- **Activity Detail** — GPS route map with speed/HR/power color overlay, HR and power zone distribution, per-km splits, normalized power, intensity factor, variability index
- **Fitness Trends** — CTL/ATL/TSB over time with PR annotations
- **Weekly Report** — week summary, week-over-week comparison, daily breakdown, HR zones
- **Training Log** — chronological ride table with drill-down, cumulative distance and TSS

### Intelligent Route Planning
- Generates real road-following GPX loops via [Valhalla](https://github.com/valhalla/valhalla) (free, OpenStreetMap-based)
- Saves GPX files for import into any bike computer or app (Komoot, Garmin, Wahoo, etc.)
- Smart waypoint selection from 10 data sources (see below)
- Weather-aware: best ride time, UV warnings, wind direction analysis
- Safety control: `--safety` flag adjusts preference for bike lanes vs main roads
- Configurable avoid zones for roads/areas you don't want to ride

## Route Intelligence — 10 Data Sources

When planning a route, VeloAI selects waypoints and enriches the output using:

| # | Source | Data | API |
|---|--------|------|-----|
| 1 | **OpenStreetMap POIs** | Viewpoints, cafes, peaks, water fountains, bike shops | Overpass (free) |
| 2 | **Strava segments** | Popular cycling roads near you | Strava API |
| 3 | **Komoot highlights** | Community-curated cycling POIs | Vector tiles (free, no auth) |
| 4 | **Your ride history** | 30-day GPS density grid — variety or comfort mode | Local DB |
| 5 | **OSM surface tags** | Road surface verification (asphalt, gravel, etc.) | Overpass (free) |
| 6 | **OSM cycling infrastructure** | Bike lanes, speed limits, traffic calming → safety score | Overpass (free) |
| 7 | **Open-Meteo weather** | Temperature, wind, UV, rain + hourly forecast | Open-Meteo (free) |
| 8 | **Open-Meteo air quality** | European AQI, PM2.5, PM10 | Open-Meteo (free) |
| 9 | **Open Topo Data** | Elevation profile, climb/descent, max gradient | Open Topo Data (free) |
| 10 | **Sunrise/Sunset** | Daylight safety, golden hour | sunrise-sunset.org (free) |

Additionally, the route planner detects **waymarked cycling trails** (EuroVelo, national routes) along the generated path.

## Deduplication — Data Richness Scoring

When multiple devices record the same ride (e.g., a bike computer and a watch both syncing to Strava), VeloAI keeps the record with the richest data:

| Field | Score |
|-------|-------|
| Power data | +3 |
| Heart rate | +2 |
| Distance > 0 | +1 |
| Cadence | +1 |
| Calories | +1 |
| Elevation > 0 | +1 |

The record with the higher total score wins. Missing fields from the losing record (e.g., HR from a watch when a bike computer wins on power) are merged into the winner. This works with any device brand — no hardcoded priorities.

## Architecture

```
Any device → Strava → [Ingestor] → PostgreSQL → Grafana dashboards
                                        ↑
                            VeloAI CLI (route planning + recommendations)
                                        ↓
                              Valhalla → GPX file
```

Three Docker Compose services:

| Service | Image | Port |
|---------|-------|------|
| PostgreSQL | `postgres:15` | 5423 |
| Ingestor | custom Python 3.11 | — |
| Grafana | `grafana/grafana:12.4` | 3021 |

The CLI runs locally and connects to the database over the network.

## Quick Start

### 1. Clone and configure

```bash
git clone https://github.com/your-user/veloai.git
cd veloai
cp .env.example .env
# Edit .env with your Strava API credentials and passwords
```

### 2. Get a Strava refresh token

```bash
# Open in browser (replace YOUR_CLIENT_ID):
# https://www.strava.com/oauth/authorize?client_id=YOUR_CLIENT_ID&response_type=code&redirect_uri=http://localhost&approval_prompt=force&scope=activity:read_all

# After authorizing, exchange the code:
curl -X POST https://www.strava.com/oauth/token \
  -d client_id=YOUR_CLIENT_ID \
  -d client_secret=YOUR_CLIENT_SECRET \
  -d code=CODE_FROM_REDIRECT \
  -d grant_type=authorization_code
# Use the refresh_token from the response
```

### 3. Start services

```bash
docker compose up -d
```

On first run, the ingestor backfills the last 12 months of Strava activities.

### 4. Set up the CLI

```bash
pip install -r requirements.txt
cp config.example.yaml ~/.config/veloai/config.yaml
# Edit with your home coordinates, DB host, and Strava credentials
```

Credentials support three methods: direct values, environment variables, or shell commands (`password_cmd`) for secret managers like Keychain, 1Password, or Vault.

## CLI Usage

```bash
# Weekly ride recommendation (fitness + weather + past routes)
python3 -m veloai.cli

# Plan a route
python3 -m veloai.cli plan --duration 2h --surface gravel
python3 -m veloai.cli plan --duration 3h --surface road --waypoints "Sintra,Cascais"
python3 -m veloai.cli plan --duration 1h --surface mtb --safety 1.0
python3 -m veloai.cli plan --duration 2h --preference comfort
python3 -m veloai.cli plan --duration 2h --preference comfort
```

### Plan flags

| Flag | Default | Description |
|------|---------|-------------|
| `--duration` | required | Ride time (`2h`, `1h30m`, `90min`) |
| `--surface` | `gravel` | `road`, `gravel`, or `mtb` |
| `--safety` | `0.5` | 0.0 = fastest, 1.0 = safest (prefers bike lanes) |
| `--preference` | `variety` | `variety` (new roads) or `comfort` (familiar) |
| `--waypoints` | — | Comma-separated place names |
| `--date` | `tomorrow` | When to ride (`today`, `saturday`, `2026-03-20`) |
| `--time` | — | Start time (`14:00`, `2pm`, `9am`) |
| `--start` | from config | Override start as `lat,lng` |
| `--loop` | true | Round-trip route |

### Example output

```
🗺 *VeloAI 2h00m Gravel via Miradouro de Aviões, Café mydream, Manique*
  📏 24 km
  📅 2026-03-16 at 09:00
  🛤 Surface: asphalt 55%, unknown 34%, gravel 11%
  ⛰ Climb: +239m / -264m (max gradient 9.8%)
  🌿 Scenic: wood (17), water (6), park (3) (78/100)
  🛡 Safety: bike lanes 17% (8/100)
  🌤 Mainly clear, 10-21°C, wind 12 km/h
  🕐 Best time: 09:00 (14°C, wind 10 km/h, UV 2)
  🌅 Sunrise 06:45, sunset 18:46
  💪 neutral (TSB -4)
  💾 GPX: /tmp/veloai_route_gravel_24km.gpx
```

## Fitness Metrics

```
Power TSS = (duration_s × avg_power × IF) / (FTP × 3600) × 100   (preferred)
HR TSS    = (duration_h) × (avg_hr / threshold_hr)² × 100         (fallback)
CTL       = 42-day EMA of daily TSS   (chronic training load / fitness)
ATL       = 7-day EMA of daily TSS    (acute training load / fatigue)
TSB       = CTL − ATL                 (training stress balance / form)
```

- **FTP**: auto-estimated from rolling 90-day best 20-minute power × 0.95, or configured via `VELOAI_FTP` / `config.yaml`
- **Threshold HR**: 95th percentile of your max HRs, or configured via `VELOAI_MAX_HR` / `config.yaml`
- **TSB interpretation**: > +10 fresh · -10 to +10 neutral · < -10 fatigued

## Database Schema

| Table | Contents |
|-------|----------|
| `activities` | Every ride — distance, duration, HR, power, cadence, elevation, calories, TSS, sport type, device |
| `activity_streams` | Per-second telemetry — HR, power, cadence, speed, altitude, lat/lng |
| `athlete_stats` | Daily fitness metrics — CTL, ATL, TSB, weekly volume |
| `routes` | Route library for recommendations |
| `sync_state` | Ingestor bookmarks (last synced timestamps) |

Schema is managed in code (`ingestor/db.py:create_schema()`) using `IF NOT EXISTS` / `ADD COLUMN IF NOT EXISTS`. No migration tool.

## Configuration

### Ingestor (Docker)

Configured via `.env` file:

| Variable | Required | Description |
|----------|----------|-------------|
| `POSTGRES_PASSWORD` | Yes | Database password |
| `STRAVA_CLIENT_ID` | Yes | From strava.com/settings/api |
| `STRAVA_CLIENT_SECRET` | Yes | From Strava API settings |
| `STRAVA_REFRESH_TOKEN` | Yes | OAuth refresh token |
| `GRAFANA_PASSWORD` | Yes | Grafana admin password |
| `VELOAI_MAX_HR` | No | Your max heart rate (0 = auto-estimate) |
| `VELOAI_FTP` | No | Your FTP in watts (0 = auto-estimate) |

### CLI (local)

Configured via `~/.config/veloai/config.yaml` (see `config.example.yaml`):

- Home coordinates (required for route planning)
- Database connection
- Strava credentials (for segment data in route intelligence)
- Avoid zones (lat/lng areas to exclude from routes)

## Requirements

- Docker + Docker Compose (for ingestor, PostgreSQL, Grafana)
- Python 3.10+ (for CLI)
- A Strava account with API access

## License

MIT
