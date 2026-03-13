# VeloAI v2 — Architecture Design Spec

**Date:** 2026-03-13
**Status:** Approved
**Supersedes:** 2026-03-13-veloai-architecture.md (v1)

---

## Problem

VeloAI v1 pulls live data at runtime with no history, no fitness trends, and no dashboards. It answers "should I ride Saturday?" but can't answer "am I getting fitter?", "what's my best route for my current form?", or "how does my fitness compare to last month?".

---

## Goal

A TeslaMATE-inspired self-hosted cycling data platform:
- **Automatic ingestion** of every ride from Karoo 3 (via Strava polling)
- **Local PostgreSQL** as single source of truth — your data, your machine
- **Grafana dashboards** for fitness trends, ride analytics, route stats
- **VeloAI planner** upgraded to use DB for fitness-aware recommendations

---

## Data Sources & Roles

| Source | Role | Unique Data |
|---|---|---|
| **Karoo 3 → Strava** | Ride recorder | HR, power, cadence, speed, GPS, calories per ride |
| **Apple Watch → Health → Strava** | Recovery context | Resting HR, HRV, VO2max, daily activity |
| **Komoot** | Route library | Named routes, elevation profiles, ride history |
| **Open-Meteo** | Weather | 7-day forecast for ride planning |

---

## Architecture

```
Karoo 3 ──auto-sync──▶ Strava API ◀── polling (every 10 min)
Apple Watch ──────────▶ Strava API          │
                                            ▼
Komoot API ◀── hourly poll ──────▶  [ ingestor service ]
                                            │
                                            ▼
                                    [ PostgreSQL DB ]
                                       (homelab)
                                       /        \
                               [ Grafana ]   [ VeloAI CLI ]
                               dashboards    Mac mini → WhatsApp
```

**No public endpoints.** All ingestion is outbound polling — zero exposure.

---

## Services (Docker Compose — homelab)

| Service | Image | Port | Purpose |
|---|---|---|---|
| `postgres` | postgres:15 | 5432 | Primary database |
| `ingestor` | custom Python | — | Strava poller + Komoot poller |
| `grafana` | grafana/grafana | 3000 | Dashboards (behind homelab reverse proxy) |

VeloAI CLI runs on Mac mini, connects to homelab PostgreSQL over LAN (10.7.40.x).

---

## Database Schema

### `activities`
```sql
id              SERIAL PRIMARY KEY
strava_id       BIGINT UNIQUE
komoot_tour_id  BIGINT
name            TEXT
date            TIMESTAMPTZ
distance_m      FLOAT
duration_s      INTEGER
elevation_m     FLOAT
avg_hr          INTEGER
max_hr          INTEGER
avg_power       INTEGER
max_power       INTEGER
avg_cadence     INTEGER
avg_speed_kmh   FLOAT
calories        INTEGER
suffer_score    INTEGER
device          TEXT        -- 'karoo' | 'watch'
synced_at       TIMESTAMPTZ
```

### `activity_streams`
```sql
id              SERIAL PRIMARY KEY
activity_id     INTEGER REFERENCES activities(id)
time_offset     INTEGER     -- seconds from start
hr              INTEGER
power           INTEGER
cadence         INTEGER
speed_kmh       FLOAT
altitude_m      FLOAT
lat             FLOAT
lng             FLOAT
```

### `athlete_stats`
```sql
date            DATE PRIMARY KEY
ctl             FLOAT       -- Chronic Training Load (fitness, 42d)
atl             FLOAT       -- Acute Training Load (fatigue, 7d)
tsb             FLOAT       -- Training Stress Balance (form = CTL-ATL)
resting_hr      INTEGER
vo2max          FLOAT
weekly_distance_m  FLOAT
weekly_elevation_m FLOAT
```

### `routes`
```sql
id              SERIAL PRIMARY KEY
komoot_id       BIGINT UNIQUE
name            TEXT
distance_m      FLOAT
elevation_m     FLOAT
sport           TEXT
last_ridden_at  DATE
ride_count      INTEGER
```

### `sync_state`
```sql
key             TEXT PRIMARY KEY
last_synced_at  TIMESTAMPTZ
value           TEXT
```

---

## Fitness Calculation (Local — No Strava Premium)

CTL/ATL/TSB calculated from raw ride data:

```
TSS (per ride) = (duration_s / 3600) × (avg_hr / threshold_hr)² × 100
CTL            = 42-day exponential moving average of daily TSS
ATL            = 7-day exponential moving average of daily TSS
TSB (form)     = CTL − ATL
```

Threshold HR calibrated from ride history (estimated at 95th percentile of max HRs, or set manually).

Interpretation:
- TSB > +10: Fresh — good day to push hard
- TSB -10 to +10: Neutral — normal ride
- TSB < -10: Fatigued — easy ride or rest

---

## Grafana Dashboards

| Dashboard | Panels |
|---|---|
| **Overview** | This week's rides, current CTL/ATL/TSB, form gauge |
| **Ride Detail** | HR zones, power curve, elevation profile, map |
| **Fitness Trends** | CTL/ATL/TSB over time, resting HR, VO2max |
| **Route Stats** | Per-route ride count, best time, elevation |
| **Weekly Summary** | Distance, elevation, calories bar charts |

---

## VeloAI Planner (updated)

Reads from DB instead of live APIs:
- Fitness state from `athlete_stats` (latest TSB → adjust route difficulty)
- Routes from `routes` table
- Weather from Open-Meteo (still live — always fresh)
- Recommendation includes form note: "You're fresh (+12 TSB) — good day for a hard effort"

---

## Repo Structure

```
veloai/
├── docker-compose.yml
├── .env.example              # DB creds, Strava client_id/secret (no values)
├── ingestor/
│   ├── Dockerfile
│   ├── requirements.txt
│   ├── main.py               # scheduler: poll Strava + Komoot
│   ├── strava.py             # OAuth refresh, activity + stream fetch
│   ├── komoot.py             # route fetch → DB
│   ├── fitness.py            # CTL/ATL/TSB calculator
│   └── db.py                 # PostgreSQL connection + upserts
├── grafana/
│   ├── provisioning/
│   │   ├── datasources/      # PostgreSQL datasource config
│   │   └── dashboards/       # auto-load dashboard JSONs
│   └── dashboards/
│       ├── overview.json
│       ├── ride-detail.json
│       ├── fitness-trends.json
│       ├── route-stats.json
│       └── weekly-summary.json
├── veloai/                   # existing CLI package
│   ├── cli.py                # updated to read from DB
│   ├── planner.py            # fitness-aware recommendations
│   ├── db.py                 # NEW: DB reader for CLI
│   └── ...                   # weather.py, keychain.py unchanged
└── docs/
    ├── specs/
    └── plans/
```

---

## Key Risks

- 🟡 **Strava API rate limits** — 100 req/15min, 1000/day. Streams = 1 req per ride. Backfill of 100 rides = 100 stream requests. Fine with throttling.
- 🟡 **CTL accuracy** — without a power meter, HR-based TSS is an approximation. Good enough for ride planning, not for pro training.
- 🟢 **komPYoot stability** — unofficial API, low risk since we're just reading.
- 🟢 **PostgreSQL on homelab** — persistent volume, standard ops.

---

*Last updated: 2026-03-13*
