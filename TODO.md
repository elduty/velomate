# VeloAI — TODO

Prioritised backlog. Full rationale for all items lives in `docs/features-analysis-06apr26.md`. Detailed implementation plans live in `docs/superpowers/plans/`.

## 🔴 In Progress

(none)

## 📋 Backlog

### High — Cluster A: Ride Analytics Depth
Plan: `docs/superpowers/plans/2026-04-06-ride-analytics-depth.md`
- [ ] Implement cardiac drift trend — stored `aerobic_decoupling` column + All Time Progression trend panel + Overview period stat (gap #1, plan Tasks 2–9)
- [ ] Implement auto interval detection — `ingestor/intervals.py` + `ride_intervals` table + Activity Details interval table + monthly distribution chart (gap #6, plan Tasks 10–15)

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
