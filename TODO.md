# VeloAI — TODO

Prioritised backlog. VeloMate is an **automatic analytics platform** — data flows in from APIs, metrics are computed, dashboards update. No manual entry, no daily chores. Features must fit this model.

## 🔴 In Progress

(none)

## 📋 Backlog

### High — Automatic Analytics
- [ ] Equipment tracking — auto-count distance/elevation per bike from Strava's `gear_id` field. Wear alerts based on component mileage. No manual entry (gap #7)
- [x] Athlete type classification — sprinter/pursuiter/rouleur/TT/climber from W'/CP ratio. Stat card on All Time Progression alongside CP/W' Progression chart

### Medium — Data Sources & Ingestion
- [x] Ride with GPS as activity source — standalone backfill + polling via /sync.json change feed, cross-source dedup with Strava, deletion mirroring. Spec: `docs/superpowers/specs/2026-06-10-rwgps-activity-source-design.md`
- [ ] Direct FIT file import — bypass Strava for offline rides or devices that don't sync. Automatic ingestion from a watched directory (gap #13)
- [ ] Strava webhook subscriptions — push instead of 10-min polling. Faster updates, fewer API calls (gap #18)
- [ ] HRV ingestion from wearable APIs — Garmin Connect, Oura, WHOOP. Automatic pull, no CSV upload. Only worth doing if the API is fully automatic (gap #2 phase 3, revised)

### Medium — Computed Metrics
- [x] VO2max estimate — computed from CP/weight via Storer formula. Stat card on All Time Progression
- [x] Form-zone auto-annotation — TSB threshold coloring on CTL/ATL/TSB chart (overreached/fatigued/neutral/optimal/fresh/detraining)

### Medium — Route Planning Safety
- [ ] Road safety filtering for route planner — hard-exclude roads that are illegal, unsafe, or uncomfortable for cycling. This includes motorways (autoestradas), vias rápidas, and high-speed national roads such as IC/IP routes where cycling is prohibited or dangerous (e.g. IC30 near Sintra). Valhalla supports `use_highways` and `exclude_` penalty weights — use hard exclusions (not just penalties) for roads tagged `highway=motorway`, `highway=trunk`, and `access=no` for bicycles, so the planner never routes through them regardless of detour cost. For roads that are legal but have no shoulder, high traffic, or speed limits ≥90 km/h, apply heavy penalties to force a detour. **The goal is that the rider should never accidentally end up on a dangerous road** — avoidance must be guaranteed, not best-effort. If a hard-excluded road makes a route geometrically impossible (e.g. no alternative crossing exists), fail the route generation and tell the user why, rather than silently falling back to the unsafe road. Warnings are a last resort for edge cases only — the primary contract is avoidance.

### Low — Polish & Refinement
- [ ] Imperial units support (USA) — `VELOMATE_UNITS=metric|imperial` env var, metric by default. Display-layer conversion only; the ingestor keeps SI storage as the single source of truth. Convert distance→mi, elevation→ft, speed→mph, weight→lb, temp→°F across Grafana dashboards + CLI output. Design note: Grafana can't switch units from an env var in place (unit labels are static per panel), so this needs a generated imperial dashboard variant provisioned when the flag is set — not a trivial add.
- [ ] eFTP auto-update from single maximal efforts — CP covers the algorithmic estimate; this is for athletes doing deliberate FTP tests where a single breakthrough effort should update immediately (gap #11)
- [ ] PR notifications + durability PRs (best power after ≥1000kJ) — automatic detection from ride data (gap #14)
- [ ] Route library with metadata — auto-detect repeat routes from GPS, tag favourites. Analysis of how performance changes on the same route over time
- [ ] Interval detector: lower `threshold_pct` from 0.85 → 0.78 so tempo and sweetspot-floor efforts become detectable. One-line default + METRICS_VERSION bump
- [ ] Surge vs interval heuristic — urban riders generate many 30-120s anaerobic classifications from traffic light accelerations. Consider max/avg ratio filter or a "surge" class

### Parked — Doesn't Fit the Automatic Model
- [ ] ~~Wellness diary (manual RHR/sleep/feel entry)~~ — requires daily user input, breaks the plug-and-forget model
- [ ] ~~Daily readiness score~~ — depends on manual wellness data
- [ ] ~~Daily ride recommendation~~ — prescriptive, not analytical
- [ ] ~~User-defined computed fields via YAML~~ — power-user feature with marginal value

## ✅ Done
- [x] Configurable backfill window via `VELOMATE_BACKFILL_MONTHS` (#89)
- [x] Auto-backfill when `VELOMATE_BACKFILL_MONTHS` is extended (#90)
- [x] Feature gap analysis covering 8 canonical competitor platforms (`docs/features-analysis-06apr26.md`)
- [x] Cluster A implementation plan (`docs/superpowers/plans/2026-04-06-ride-analytics-depth.md`)
- [x] `velomate-features-designer` project skill for ongoing gap evaluation
- [x] Cluster A — Ride Analytics Depth (#91) — stored `aerobic_decoupling` column + trend panel + period stats, `ride_intervals` table + detection module + Activity Details interval table + monthly distribution chart
- [x] Overview polish (#92) — decoupling collision fix, Δ Avg Decoupling, loosened steady-state filter, `now-30d` default, collapsible rows for secondary sections
- [x] Overview + Training Report split design spec (`docs/superpowers/plans/2026-04-06-overview-training-report-split.md`)
- [x] Δ Avg cards Bug A fix (#93) — NULL-safe CTE pattern
- [x] Drop sample-size threshold from Δ Avg cards (#94)
- [x] estimated_ftp preserved as algorithmic diagnostic (#95)
- [x] Overview FTP split into Configured + Est. side by side (#96)
- [x] VI-aware TSS uses avg_power when VI > 1.30 (#97)
- [x] Tooltip one-range-per-line formatting + Dashboard Conventions rule (#98)
- [x] HR TSS uses LTHR, not max HR (#99)
- [x] Tooltip color icons + unified palette + rule update (#100)
- [x] Power Distribution Z7 scale + bucket size (#101)
- [x] Overview + Training Report split (#102) → reversed in #103
- [x] Merge Training Report back into Overview (#103)
- [x] Calories + W/kg (#104)
- [x] Route wind/gust warnings (#105)
- [x] CP/W' foundation (#108)
- [x] W'bal time series (#111)
- [x] Durability Profile + Index (#112)
- [x] Training Monotony & Strain (#113)
- [x] Climb detection — RDP + Strava enrichment (#114, #115, #116)
- [x] Strava OAuth flow (#117)
- [x] Ride with GPS activity source — co-equal with Strava, cross-source dedup, deletion mirroring (#133)
- [x] RWGPS `is_stationary` trainer-detection fix from live-API audit (#134)
- [x] Parameterise all docker-compose env vars via `${...}` + complete `.env.example` (#135)
- [x] Overview averages blank-on-empty-prior-period fix — `COALESCE` the 4 average Δ cards so a NULL value doesn't crash the Grafana 12.4 dashboard scene (#140)
- [x] Pin image tags via `POSTGRES_VERSION` / `GRAFANA_VERSION` + v1.5.0 release-prep docs (README features, changelog, dry-run publish flag)
