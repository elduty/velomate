# VeloAI — TODO

Prioritised backlog. Full rationale for all items lives in `docs/features-analysis-06apr26.md`. Detailed implementation plans live in `docs/superpowers/plans/`.

## 🔴 In Progress

(none)

## 📋 Backlog

### High — Dashboard restructure: Overview + Training Report split
Plan: `docs/superpowers/plans/2026-04-06-overview-training-report-split.md`
- [ ] Create new `Training Report` dashboard with Period Summary (2×5) + vs Previous Period (2×5) + Trends (6 charts expanded) + Ride Patterns + Outdoor Records + Ride Map
- [ ] Trim Overview to daily-glance view — keep Fitness + 4-stat "This Period" row (Rides/Distance/Hours/TSS) + Activities; remove delta section, Trends, Ride Patterns, Outdoor Records, Ride Map, Avg Decoupling panels
- [ ] Update cross-dashboard nav links + provisioning for the new dashboard

### High — Cluster B: Performance Modeling
- [ ] CP/W' model with Monod-Scherrer + Morton fits — new `ingestor/critical_power.py` + `cp_estimates` table + CP/W' panel on All Time Progression (gap #3)
- [ ] W'bal time series per ride — Skiba differential on stream + new Activity Details panel (gap #4, depends on CP/W')
- [ ] Fresh vs fatigued PD curves — Grafana SQL segmenting by CTL bucket + Durability Index stat (gap #5)

### High — Cluster C: Recovery & Wellness
- [ ] Wellness diary schema + CLI entry (`velomate wellness today --rhr ... --sleep ...`) (gap #2 phase 1)
- [ ] Daily readiness score composite from HRV + sleep + load balance (gap #2 phase 2)
- [ ] HRV ingestion from wearables (HealthFit/Apple Health CSV first, then Oura/WHOOP/Garmin) (gap #2 phase 3)

### Medium
- [ ] Daily "ride today" recommendation extending `cmd_recommend` with form-zone annotation (gap #8)
- [ ] Equipment tracking — bikes + components + mileage + wear alerts (gap #7)
- [ ] Climb categorisation (HC/Cat 1–4) from GPS elevation (gap #10)
- [ ] Training Monotony & Strain (Foster) on `athlete_stats` + overreaching warning panel (gap #9)
- [ ] eFTP auto-update from single maximal efforts (gap #11)
- [ ] OAuth browser flow for Strava — `velomate auth` CLI command (gap #12)
- [ ] Direct FIT file import — bypass Strava for offline rides (gap #13)

### Low
- [ ] Athlete type classification from CP/W'/Pmax (gap #17 — nearly free after CP/W')
- [ ] PR notifications + durability PRs (best power after ≥1000kJ) (gap #14)
- [ ] VO2max estimate from HR + power (populates unused `athlete_stats.vo2max` column) (gap #15)
- [ ] User-defined computed fields via YAML config (gap #16)
- [ ] Form-zone auto-annotation on fitness timeline (peak/productive/overreaching)
- [ ] Route library with metadata (favourite routes, tags, repeat analysis)
- [ ] Strava webhook subscriptions — push instead of 10-min polling

## ✅ Done
- [x] Configurable backfill window via `VELOMATE_BACKFILL_MONTHS` (#89)
- [x] Auto-backfill when `VELOMATE_BACKFILL_MONTHS` is extended (#90)
- [x] Feature gap analysis covering 8 canonical competitor platforms (`docs/features-analysis-06apr26.md`)
- [x] Cluster A implementation plan (`docs/superpowers/plans/2026-04-06-ride-analytics-depth.md`)
- [x] `velomate-features-designer` project skill for ongoing gap evaluation
- [x] Cluster A — Ride Analytics Depth (#91) — stored `aerobic_decoupling` column + trend panel + period stats, `ride_intervals` table + detection module + Activity Details interval table + monthly distribution chart
- [x] Overview polish (#92) — decoupling collision fix, Δ Avg Decoupling, loosened steady-state filter, `now-30d` default, collapsible rows for secondary sections
- [x] Overview + Training Report split design spec (`docs/superpowers/plans/2026-04-06-overview-training-report-split.md`)
- [x] Δ Avg cards bug fixes (#93) — NULL-safe via CTE pattern + sample-size threshold (≥3 rides per period); fixes Bug A (COALESCE→0 misleading delta when prev period empty) and Bug B (tiny-sample noise) on all 4 average-based delta cards (Power, HR, Speed, Decoupling)
