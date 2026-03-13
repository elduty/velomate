# VeloAI 🚴

A self-hosted cycling data platform — automatic ride ingestion, Grafana dashboards, and fitness-aware ride planning.

Inspired by TeslaMate. Built for Marcin's Karoo 3 + Strava setup.

---

## What it does

- **Ingestor** — polls Strava every 10 min, pulls every ride with full streams (HR, power, cadence, speed, altitude, GPS), calculates CTL/ATL/TSB fitness metrics, stores everything in PostgreSQL. Handles cross-device deduplication (Karoo > unknown/Zwift > Watch) and Strava-Komoot matching (same-day ±10% distance)
- **Grafana** — dashboards for activity history, fitness trends, and per-activity detail (speed, elevation, HR, power, cadence vs distance)
- **VeloAI CLI** — reads from DB to produce WhatsApp-friendly ride recommendations based on current fitness (TSB) + weather forecast + Komoot routes

---

## Architecture

```
Karoo 3 → Strava API ←── polling (10 min)
Komoot API          ←── polling (1 hr)
                              │
                              ▼
                      [ ingestor (Docker) ]
                              │
                              ▼
                      [ PostgreSQL 15 ]  ←── VeloAI CLI (Mac mini)
                              │
                              ▼
                      [ Grafana 12 ]
```

All services run on homelab via Docker Compose. VeloAI CLI runs on Mac mini, connects over LAN.

---

## Services

| Service | Image | Host Port | URL |
|---|---|---|---|
| PostgreSQL | postgres:15 | 5423 | `10.7.40.15:5423` |
| Ingestor | custom Python | — | — |
| Grafana | grafana/grafana:12 | 3021 | `https://veloai.mrmartian.in` |

---

## Setup

### 1. Clone and configure

```bash
git clone ssh://git@10.7.40.20:2222/MrMartian/veloai.git
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
KOMOOT_EMAIL=            # Komoot account email
KOMOOT_PASSWORD=         # Komoot account password
GRAFANA_PASSWORD=        # Grafana admin password
VELOAI_DB_HOST=10.7.40.15  # homelab IP (CLI only)
VELOAI_DB_PORT=5423        # host-mapped port (CLI only)
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

### 4. VeloAI CLI (Mac mini only)

```bash
pip install -r requirements.txt
python3 -m veloai.cli
```

Requires Mac Keychain entries (Komoot credentials for route library):

```bash
security add-generic-password -a openclaw -s openclaw/komoot \
  -w '{"email":"...","password":"..."}'
```

DB credentials are read from `.env` or env vars (`VELOAI_DB_HOST`, `VELOAI_DB_PORT`, etc.).

---

## Database

**Host:** `10.7.40.15:5423`  
**DB:** `veloai` | **User:** `veloai`

### Tables

| Table | Contents |
|---|---|
| `activities` | Every ride — distance, duration, HR, power, cadence, elevation, calories, `is_indoor`, `sport_type`, `device` |
| `activity_streams` | Per-second stream data — HR, power, cadence, speed, altitude, GPS |
| `athlete_stats` | Daily CTL/ATL/TSB fitness metrics |
| `routes` | Komoot route library with ride counts |
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

Access: `https://veloai.mrmartian.in` (or `http://10.7.40.15:3021` on LAN)

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
│   ├── main.py               # scheduler: Strava + Komoot polling
│   ├── strava.py             # OAuth refresh, activity + stream fetch
│   ├── komoot.py             # Komoot route sync
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
├── veloai/                   # CLI package (Mac mini)
│   ├── __main__.py
│   ├── cli.py                # entry point
│   ├── planner.py            # fitness-aware recommendations
│   ├── db.py                 # DB reader
│   ├── komoot.py             # Komoot integration
│   ├── weather.py            # Open-Meteo forecast
│   └── keychain.py           # macOS Keychain helper
├── scripts/
│   └── ride-planner-v0.py    # archived v0 script
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
git pull  # on homelab
docker compose restart grafana
```

---

## Known Issues / Pending

- Activity Detail charts use `trend` panel type for distance x-axis — requires Grafana 9+
- Grafana dashboard provisioning reloads on container restart (not hot-reload)
- No map panel yet (GPS data is in `activity_streams.lat/lng` but not visualised)
- No route stats dashboard yet (routes table populated but no dedicated dashboard)
- Strava rate limits: 100 req/15min — stream backfill throttles automatically

---

*Last updated: 2026-03-13 — VeloAI v2*
