# VeloAI 🚴

A self-hosted cycling data platform — automatic ride ingestion, Grafana dashboards, and fitness-aware ride planning.

Inspired by TeslaMate. Built for cyclists using Strava.

---

## What it does

- **Ingestor** — polls Strava every 10 min, pulls every ride with full per-second streams (HR, power, cadence, speed, altitude, GPS), calculates CTL/ATL/TSB fitness metrics, stores everything in PostgreSQL. Strava is the single source of truth — any device that syncs to Strava works. Automatically deduplicates when multiple devices record the same ride by keeping the richer data source.
- **Grafana** — 5 dashboards: overview hub, activity detail (with GPS map, zones, splits), fitness trends, weekly report, training log
- **VeloAI CLI** — ride recommendations based on fitness + weather, and route planning via Valhalla GPX generation → Komoot upload

---

## Architecture

```
Any device → Strava API ←── polling (10 min)
Komoot              ←── route upload (CLI)
                              │
                              ▼
                      [ ingestor (Docker) ]
                              │
                              ▼
                      [ PostgreSQL 15 ]  ←── VeloAI CLI (local machine)
                              │
                              ▼
                      [ Grafana 12 ]
```

All services run on server via Docker Compose. VeloAI CLI runs on local machine, connects over LAN.

---

## Services

| Service | Image | Host Port | URL |
|---|---|---|---|
| PostgreSQL | postgres:15 | 5423 | configured in `.env` |
| Ingestor | custom Python | — | — |
| Grafana | grafana/grafana:12.4 | 3021 | `http://localhost:3021` |

---

## Setup

### 1. Clone and configure

```bash
git clone https://github.com/your-user/veloai.git
cd veloai
cp .env.example .env
# Edit .env — fill in all values
```

### 2. `.env` values needed

```
POSTGRES_PASSWORD=       # DB password
STRAVA_CLIENT_ID=        # from https://www.strava.com/settings/api
STRAVA_CLIENT_SECRET=    # from Strava API settings
STRAVA_REFRESH_TOKEN=    # obtained via OAuth (see below)
GRAFANA_PASSWORD=        # Grafana admin password
VELOAI_DB_HOST=your-db-host  # PostgreSQL host (for CLI — ingestor uses Docker DNS)
VELOAI_DB_PORT=5432          # PostgreSQL port (for CLI — ingestor uses Docker DNS)
```

#### Getting a Strava refresh token

```bash
# 1. Open in browser (replace YOUR_CLIENT_ID):
https://www.strava.com/oauth/authorize?client_id=YOUR_CLIENT_ID&response_type=code&redirect_uri=http://localhost&approval_prompt=force&scope=activity:read_all

# 2. After authorizing, copy the `code=` param from the redirect URL
# 3. Exchange for refresh token:
curl -X POST https://www.strava.com/oauth/token \
  -d client_id=YOUR_CLIENT_ID \
  -d client_secret=YOUR_CLIENT_SECRET \
  -d code=CODE_FROM_STEP_2 \
  -d grant_type=authorization_code
# → use refresh_token from the response
```

### 3. Start services

```bash
docker compose up -d
```

On first run, the ingestor backfills the last 12 months of Strava activities + streams.

### 4. VeloAI CLI (local machine only)

```bash
pip install -r requirements.txt
python3 -m veloai.cli
```

Requires a config file with credentials:

```bash
cp config.example.yaml ~/.config/veloai/config.yaml
# Edit with your home coordinates, DB host, Strava/Komoot credentials
```

Supports env var overrides and `password_cmd` for secret managers (Keychain, 1Password, etc.).

---

## Database

**Host:** configured in `.env` or `config.yaml`
**DB:** `veloai` | **User:** `veloai`

### Tables

| Table | Contents |
|---|---|
| `activities` | Every ride — distance, duration, HR, power, cadence, elevation, calories, `is_indoor`, `sport_type`, `device` |
| `activity_streams` | Per-second stream data — HR, power, cadence, speed, altitude, GPS |
| `athlete_stats` | Daily CTL/ATL/TSB fitness metrics |
| `routes` | Route library (historical rides used for recommendations) |
| `sync_state` | Ingestor bookmarks (last synced timestamps) |

### Fitness metrics

```
Power TSS      = (duration_s × avg_power × IF) / (FTP × 3600) × 100   (preferred)
HR TSS         = (duration_h) × (avg_hr / threshold_hr)² × 100         (fallback)
CTL            = 42-day EMA of daily TSS  (fitness)
ATL            = 7-day EMA of daily TSS   (fatigue)
TSB            = CTL − ATL               (form)
```

Power-based TSS is preferred when power data is available; HR-based is the fallback.
Threshold HR = 95th percentile of max HRs; FTP estimated from 95th percentile of avg power.

Interpretation: TSB > +10 = fresh (push hard) · TSB -10..+10 = neutral · TSB < -10 = fatigued (easy or rest)

---

## Grafana Dashboards

Access: `http://localhost:3021` (or configure `GRAFANA_ROOT_URL` in `.env` for remote access)

| Dashboard | Description |
|---|---|
| **Overview** | All activities table, weekly distance/elevation bars, HR trend |
| **Fitness Trends** | CTL/ATL/TSB over time, weekly load |
| **Activity Detail** | Per-activity stats + speed/elevation/HR/power/cadence vs distance |

### Activity Detail dashboard

Stat row: Name · Distance · Duration · Elevation · Avg Speed · Calories · Avg HR · Max HR · Avg Power · Sport · Device · Date

Charts (x-axis = distance in km, computed from speed × time delta):
- **Speed & Elevation** — dual y-axis, smooth line
- **Heart Rate & Power** — dual y-axis
- **Cadence**

Navigate to an activity via the Overview dashboard (click activity name → opens Activity Detail).

---

## Repo Structure

```
veloai/
├── docker-compose.yml
├── .env                      # gitignored — local secrets
├── .env.example              # template — safe to commit
├── ingestor/
│   ├── Dockerfile
│   ├── requirements.txt
│   ├── main.py               # scheduler: Strava polling
│   ├── strava.py             # OAuth refresh, activity + stream fetch
│   ├── fitness.py            # CTL/ATL/TSB calculator
│   └── db.py                 # PostgreSQL connection + upserts + schema
├── grafana/
│   ├── provisioning/
│   │   ├── datasources/      # PostgreSQL auto-provisioned
│   │   └── dashboards/       # auto-load from grafana/dashboards/
│   └── dashboards/
│       ├── overview.json
│       ├── activity.json     # Activity Detail
│       └── fitness-trends.json
├── veloai/                   # CLI package
│   ├── __main__.py
│   ├── cli.py                # entry point (argparse subcommands)
│   ├── config.py             # YAML + env var config loader
│   ├── route_planner.py      # route planning pipeline
│   ├── route_generator.py    # Valhalla GPX loop generation
│   ├── geocode.py            # Nominatim geocoder
│   ├── planner.py            # weekly ride recommendations
│   ├── db.py                 # DB reader
│   ├── route_planner.py      # route planning pipeline
│   ├── route_generator.py    # Valhalla GPX generation
│   ├── route_intelligence.py # smart waypoint selection (OSM + Strava + Komoot highlights)
│   └── weather.py            # Open-Meteo forecast
├── config.example.yaml       # CLI config template
├── tests/                    # pytest (70 pure function tests)
└── docs/
    ├── specs/
    │   ├── 2026-03-13-veloai-architecture.md      # v1 spec
    │   └── 2026-03-13-veloai-v2-architecture.md   # v2 spec (current)
    └── plans/
        ├── 2026-03-13-v1-restructure.md
        └── 2026-03-13-v2-pipeline.md
```

---

## Common Operations

### Check ingestor logs
```bash
docker compose logs -f ingestor
```

### Force re-sync fitness metrics
```bash
docker compose exec ingestor python3 -c "
from db import get_connection
from fitness import recalculate_fitness
recalculate_fitness(get_connection())
"
```

### Check activity count
```bash
docker compose exec veloai-postgres psql -U veloai -c "SELECT COUNT(*) FROM activities;"
```

### Restart after config change
```bash
docker compose up -d --force-recreate
```

### Update Grafana dashboards (after editing JSON)
```bash
git pull  # on server
docker compose restart grafana
```

---

## Known Issues / Pending

- Activity Detail charts use `trend` panel type for distance x-axis — requires Grafana 9+
- Grafana dashboard provisioning reloads on container restart (not hot-reload)
- Strava rate limits: 100 req/15min — stream backfill throttles automatically
- Valhalla route generator creates loops mathematically — doesn't consider preferred roads or scenic preferences yet
- Requires Python 3.10+ (uses `X | None` union type syntax)

---

*Last updated: 2026-03-15*
