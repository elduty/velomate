# Reddit Launch Posts

Post r/selfhosted first, then others 1-2 days apart.

---

## r/selfhosted (primary)

**Title:** I built a TeslaMate-style cycling analytics platform — VeloMate

Hey r/selfhosted,

I've been running TeslaMate for my car and loved the approach — pull data from an API, store it locally, visualize with Grafana. I wanted the same thing for cycling, so I built VeloMate.

**What it does:**
- Polls Strava every 10 minutes for new rides (any device that syncs to Strava works)
- Stores full per-second telemetry in PostgreSQL
- Computes all the metrics Strava locks behind Premium — fitness (CTL/ATL/TSB), Normalized Power, training load, TRIMP, power zones — locally from raw data
- Serves 3 Grafana dashboards with 98 panels across 12 visualization types
- CLI generates GPX cycling routes using Valhalla + 10 data sources (OSM, weather, elevation, Strava segments, etc.)

**Stack:** Docker Compose (PostgreSQL 15 + Python ingestor + Grafana 12.4). 370 tests. AGPL-3.0 licensed.

**Screenshots:** [in the README]

It's a personal project that grew — started as a ride planner, turned into a full analytics platform. No account needed, no cloud, your data stays on your server.

GitHub: https://github.com/elduty/velomate

Happy to answer questions about the architecture or cycling-specific metrics.

---

## r/Velo

**Title:** Open-source cycling analytics — CTL/ATL/TSB, power zones, NP, EF without Strava Premium

Built an open-source platform that computes all the fitness and power metrics locally from Strava data — no Premium subscription needed. FTP auto-estimated from your 20-min best, zones calculated from stream data, aerobic decoupling per ride, the works.

Three Grafana dashboards: daily training overview, per-ride deep dive (zones by kilometer, power duration curve, cardiac drift), and long-term progression with rolling averages.

Self-hosted with Docker. Works with any device that syncs to Strava (Karoo, Garmin, Wahoo, Zwift).

GitHub: https://github.com/elduty/velomate

---

## r/cycling

**Title:** Free alternative to Strava Premium — self-hosted cycling dashboard with fitness tracking

Built a self-hosted cycling analytics tool that gives you everything Strava Premium offers (fitness trends, training load, power zones, relative effort) for free. It pulls your rides from Strava, computes the metrics locally, and serves Grafana dashboards.

Works with any device — Garmin, Wahoo, Karoo, Apple Watch, Zwift. Just needs Docker on a home server or NAS.

GitHub: https://github.com/elduty/velomate

---

## r/Zwift

**Title:** Self-hosted Zwift analytics — power zones, fitness tracking, NP/IF/TSS without Strava Premium

Built a self-hosted analytics platform that ingests your Zwift rides from Strava and computes all the training metrics locally. CTL/ATL/TSB fitness curves, power zones, Normalized Power, Efficiency Factor, training zone polarization — no subscription needed.

Three Grafana dashboards with 98 panels. Tracks indoor and outdoor rides separately with ride-type filtering.

GitHub: https://github.com/elduty/velomate

---

## r/grafana

**Title:** Cycling analytics dashboards — 98 panels across 3 dashboards (12 visualization types)

Built a cycling analytics platform with 3 Grafana dashboards using stat, timeseries, barchart, gauge, piechart, xychart, candlestick, trend, table, and geomap panels. PostgreSQL backend, provisioned from JSON.

Some highlights: fill-between on CTL/ATL for visual TSB, HR/power zones by kilometer (stacked bars), power vs HR scatter for cardiac drift, weekly power candlestick with LAG() for week-over-week comparison.

GitHub: https://github.com/elduty/velomate

---

## r/homelab

**Title:** VeloMate — self-hosted cycling analytics (Docker + PostgreSQL + Grafana)

Built a cycling data platform for my homelab — polls Strava, stores telemetry in PostgreSQL, serves Grafana dashboards. Computes fitness metrics (CTL/ATL/TSB) and power zones locally without needing Strava Premium.

Docker Compose, 3 containers, works with any cycling device that syncs to Strava.

GitHub: https://github.com/elduty/velomate
