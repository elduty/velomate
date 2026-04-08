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
- [ ] Interval detector: lower `threshold_pct` from 0.85 → 0.78 so tempo (75-85% FTP) and sweetspot-floor (83%) efforts become detectable. Currently the detection threshold sits inside the sweetspot band so sustained 83-85% FTP rides can't be classified. Not blocking for urban-surge-dominant riders (power profile doesn't touch that band anyway), but needed once structured training enters the mix. One-line default + METRICS_VERSION bump.
- [ ] Consider "surge vs interval" heuristic: urban riders generate many 30-120s anaerobic classifications from traffic light accelerations (verified in prod data: 21 anaerobic intervals with max/avg ratios 1.4-2.1 = classic spike-then-decay traffic surges). Not a bug — classification is mathematically correct — but represents "traffic physics" rather than training intent. Possible heuristics: require max/avg ratio < 1.6 for "real" anaerobic class, or require ≥3 similar efforts within a 30-min window, or add a "surge" class for spike-pattern efforts. Deferred pending more thought about the right abstraction.

## 📬 External tooling feedback

Items that aren't VeloAI code changes but are worth tracking for the tools that interact with this repo. Raven is the code-review bot running against Gitea PRs on this project.

- [ ] **Raven**: add yourself as a reviewer when the PR is opened, not only when you have reviewed it. Currently Raven only appears in the Reviewers list after posting its first review, which means a newly-opened PR looks unreviewed until Raven gets to it. Adding itself at PR open time would make the pending review visible immediately in the PR list and in notifications.

## ✅ Done
- [x] Configurable backfill window via `VELOMATE_BACKFILL_MONTHS` (#89)
- [x] Auto-backfill when `VELOMATE_BACKFILL_MONTHS` is extended (#90)
- [x] Feature gap analysis covering 8 canonical competitor platforms (`docs/features-analysis-06apr26.md`)
- [x] Cluster A implementation plan (`docs/superpowers/plans/2026-04-06-ride-analytics-depth.md`)
- [x] `velomate-features-designer` project skill for ongoing gap evaluation
- [x] Cluster A — Ride Analytics Depth (#91) — stored `aerobic_decoupling` column + trend panel + period stats, `ride_intervals` table + detection module + Activity Details interval table + monthly distribution chart
- [x] Overview polish (#92) — decoupling collision fix, Δ Avg Decoupling, loosened steady-state filter, `now-30d` default, collapsible rows for secondary sections
- [x] Overview + Training Report split design spec (`docs/superpowers/plans/2026-04-06-overview-training-report-split.md`)
- [x] Δ Avg cards Bug A fix (#93) — NULL-safe CTE pattern replaces COALESCE→0 on the 4 average-based delta cards (Power, HR, Speed, Decoupling). Initially also added a `< 3` sample-size threshold (Bug B) which over-suppressed on sparse datasets — reversed in #94 below.
- [x] Drop sample-size threshold from Δ Avg cards (#94) — keeps Bug A NULL handling but removes the `< 3` suppression after user feedback; small-sample noise is acceptable, suppression isn't. Description on each card now points to the All Time Progression trend panel for smoothed direction.
- [x] estimated_ftp preserved as algorithmic diagnostic (#95) — `estimate_ftp()` is now always called, `sync_state.estimated_ftp` holds the auto-computed value regardless of whether `VELOMATE_FTP` is set. Startup logs show both numbers when they diverge.
- [x] Overview FTP split into Configured + Est. side by side (#96) — Fitness row now shows `Configured FTP` (from env) and `Est. FTP` (algorithmic) as two w=3 panels so a mismatch is visible at a glance. Recovered via cherry-pick after the original commit was dropped from #95's squash merge.
- [x] VI-aware TSS uses avg_power when VI > 1.30 (#97) — Coggan NP-based TSS overestimates load on high-VI urban rides (stop-and-go traffic, crit-style, technical MTB). New `HIGH_VI_THRESHOLD` constant + pure `select_power_for_tss()` helper routes high-VI rides through avg_power for both TSS and IF. METRICS_VERSION 9 → 10 triggered full recalc. User's 2026-04-03 ride dropped from TSS 145 (NP-based) to ~61 (avg-based) — matches perceived effort.
