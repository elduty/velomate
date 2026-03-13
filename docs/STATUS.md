# VeloAI — Current Status

**Last updated:** 2026-03-13  
**Branch:** `main` — all changes pushed to `ssh://git@10.7.40.20:2222/MrMartian/veloai.git`

---

## What's Running

| Component | Status | Notes |
|---|---|---|
| PostgreSQL | ✅ Running | `10.7.40.15:5423` |
| Ingestor | ✅ Running | Polling Strava every 10 min |
| Grafana | ✅ Running | `https://veloai.mrmartian.in` |
| VeloAI CLI | ✅ Working | `python3 -m veloai.cli` on Mac mini |

---

## Data

- Activities ingested from Strava (Karoo 3 + Komoot backfill)
- Stream data (HR, power, cadence, speed, altitude) per activity
- CTL/ATL/TSB fitness metrics calculated daily
- Komoot routes synced to `routes` table

---

## Grafana Dashboards

### Overview (`overview.json`)
- Activities table with clickable names → Activity Detail
- Weekly distance/elevation bar charts
- HR trend timeseries

### Fitness Trends (`fitness-trends.json`)
- CTL/ATL/TSB over time
- Weekly training load

### Activity Detail (`activity.json`) — last worked on 2026-03-13
Stats row (top):
- Name, Distance, Duration, Elevation, Avg Speed, Calories
- Avg HR, Max HR, Avg Power
- Sport (Indoor/Outdoor), Device, Date

Charts (distance on x-axis via `trend` panel + CTE-based distance calculation):
- Speed & Elevation (dual y-axis)
- Heart Rate & Power (dual y-axis)
- Cadence

---

## Recent Fixes (2026-03-13)

1. **`$__timeFilter` bug** — Grafana's macro mangled computed expressions; replaced with explicit `BETWEEN $__timeFrom() AND $__timeTo()`
2. **Sport label** — was showing raw Strava sport type (`cycling_outdoor`); now shows "Indoor" / "Outdoor"
3. **Distance x-axis** — all stream charts now show distance (km) instead of time; computed via SQL window function CTE
4. **Panel loading** — `xychart` was broken for PostgreSQL table format; switched to `trend` panel type
5. **Unified text sizes** — stat panels were auto-sizing differently due to varying widths; set explicit `valueSize=28 / titleSize=14`
6. **Empty labels** — nullable fields (Avg HR, Max HR, Avg Power, Calories, Device) now show `-` instead of blank
7. **Hidden obvious labels** — Sport, Date, Device panels show value only (no redundant label underneath)

---

## Known Issues / Next Steps

- [ ] **Map panel** — GPS data in `activity_streams.lat/lng`, no Grafana map dashboard yet
- [ ] **Route Stats dashboard** — `routes` table exists but no dedicated Grafana dashboard
- [ ] **Trend panel x-axis label** — shows "Distance (km)" but axis tick format may need tuning depending on Grafana version
- [ ] **Activity Detail: no time zoom** — removed `$__timeFilter` + time range dependency; the dashboard is now activity-scoped only (no time scrubbing)

---

## Key File Locations

| File | Purpose |
|---|---|
| `grafana/dashboards/activity.json` | Activity Detail dashboard |
| `grafana/dashboards/overview.json` | Overview dashboard |
| `grafana/dashboards/fitness-trends.json` | Fitness Trends dashboard |
| `ingestor/main.py` | Polling scheduler |
| `ingestor/strava.py` | Strava OAuth + activity/stream fetch |
| `ingestor/fitness.py` | CTL/ATL/TSB calculation |
| `veloai/cli.py` | WhatsApp recommendation CLI |
| `.env` | All secrets (gitignored) |

---

## Credentials (stored in `.env`)

- **DB password:** `veloai_secret_2026`
- **Grafana admin:** `admin` / `veloai_grafana_2026`
- **Strava:** Client ID `200823`, credentials in `.env`
- **Komoot:** `marcin.forsetvice@gmail.com`, password in `.env`
