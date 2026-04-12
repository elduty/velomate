# VeloAI — TODO

Prioritised backlog. Full rationale for all items lives in `docs/features-analysis-06apr26.md`. Detailed implementation plans live in `docs/superpowers/plans/`.

## 🔴 In Progress

(none)

## 📋 Backlog

### High — Cluster B: Performance Modeling
- [x] CP/W' foundation (#108) — Monod-Scherrer 2-parameter fit via numpy.polyfit (no scipy). Quality gate R² >= 0.9 AND >= 4/5 durations. Graceful fallback 90d → 180d → rolling 20-min × 0.95. New `ingestor/critical_power.py` pure-function module, `cp_estimates` table, CP/W' Progression + Power-Duration Curve panels on All Time Progression. Replaces rolling 20-min as the source of `sync_state.estimated_ftp` when fit quality is good. No TSS impact (configured FTP still wins).
- [x] W'bal time series per ride (#111) — Skiba differential model with GoldenCheetah tau. Per-second w_bal column on activity_streams. Uses latest CP/W' from cp_estimates, defaults W' to 20kJ on fallback. W'bal timeseries + Min W'bal + Time below 25% panels on Activity Details. Per-ride error isolation. COALESCE(power, 0) for coasting.
- [x] Durability Profile + Durability Index (#112) — within-ride 1st-half vs 2nd-half best efforts at 5 durations + Durability Index stat (5-min power ratio, threshold-coloured). Pure Grafana SQL, no ingestor changes. CTL-segmented cross-ride version deferred until dataset grows.

### High — Cluster C: Recovery & Wellness
- [ ] Wellness diary schema + CLI entry (`velomate wellness today --rhr ... --sleep ...`) (gap #2 phase 1)
- [ ] Daily readiness score composite from HRV + sleep + load balance (gap #2 phase 2)
- [ ] HRV ingestion from wearables (HealthFit/Apple Health CSV first, then Oura/WHOOP/Garmin) (gap #2 phase 3)

### Medium
- [ ] Daily "ride today" recommendation extending `cmd_recommend` with form-zone annotation (gap #8)
- [ ] Equipment tracking — bikes + components + mileage + wear alerts (gap #7)
- [x] Climb categorisation (HC/Cat 1–4) from GPS elevation — Detected Climbs table on Activity Details with category/gain/length/grade/duration. 30s smoothed altitude, 50m minimum gain. Pure Grafana SQL.
- [x] Training Monotony & Strain (Foster) — Monotony + Strain stat cards on Overview after CTL chart. Monotony = mean/stdev of daily TSS over 7 days. Strain = weekly TSS × Monotony. Threshold-coloured (green/yellow/red). Pure Grafana SQL.
- [x] OAuth browser flow for Strava (#117) — `velomate auth` CLI command. Manual paste flow, no port forwarding needed.
- [ ] Direct FIT file import — bypass Strava for offline rides (gap #13)

### Low
- [ ] Athlete type classification from CP/W'/Pmax (gap #17 — nearly free after CP/W')
- [ ] eFTP auto-update from single maximal efforts (gap #11) — CP covers the algorithmic estimate; this is for athletes doing deliberate FTP tests where a single breakthrough effort should update the estimate immediately
- [ ] PR notifications + durability PRs (best power after ≥1000kJ) (gap #14)
- [ ] VO2max estimate from HR + power (populates unused `athlete_stats.vo2max` column) (gap #15)
- [ ] User-defined computed fields via YAML config (gap #16)
- [ ] Form-zone auto-annotation on fitness timeline (peak/productive/overreaching)
- [ ] Route library with metadata (favourite routes, tags, repeat analysis)
- [ ] Strava webhook subscriptions — push instead of 10-min polling
- [ ] Interval detector: lower `threshold_pct` from 0.85 → 0.78 so tempo (75-85% FTP) and sweetspot-floor (83%) efforts become detectable. Currently the detection threshold sits inside the sweetspot band so sustained 83-85% FTP rides can't be classified. Not blocking for urban-surge-dominant riders (power profile doesn't touch that band anyway), but needed once structured training enters the mix. One-line default + METRICS_VERSION bump.
- [ ] Consider "surge vs interval" heuristic: urban riders generate many 30-120s anaerobic classifications from traffic light accelerations (verified in prod data: 21 anaerobic intervals with max/avg ratios 1.4-2.1 = classic spike-then-decay traffic surges). Not a bug — classification is mathematically correct — but represents "traffic physics" rather than training intent. Possible heuristics: require max/avg ratio < 1.6 for "real" anaerobic class, or require ≥3 similar efforts within a 30-min window, or add a "surge" class for spike-pattern efforts. Deferred pending more thought about the right abstraction.

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
- [x] Tooltip one-range-per-line formatting + Dashboard Conventions rule (#98) — normalised 6 panel descriptions that had jammed range lists onto consecutive lines without blank separators (TRIMP, Aerobic Decoupling, HR Zones, Detected Intervals, Monthly Power/HR Zone Distribution). Added a new **Dashboard Conventions** section to `CLAUDE.md` documenting the tooltip format (one range per line, blank separator, em-dash between range and label). Raven found stale "Z6 > 120%" with Z7 missing on Power Zones — fixed as part of the same PR.
- [x] HR TSS uses LTHR, not max HR (#99) — latent bug in the HR-only TSS fallback path. `calculate_tss` was being passed max HR directly as `threshold_hr`, but the Coggan formula expects LTHR (~0.89 × max HR), underestimating HR TSS by ~21% on any ride without power. Fix derives LTHR at the call site. TDD verified with RED confirming `got 77.9` (buggy) vs expected ~99 (LTHR-based).
- [x] Tooltip color icons + unified palette + rule update (#100) — 10 panels had color-coded ranges but no emoji icons in tooltips, plus 2 panels had wrong content (TRIMP bands didn't match panel thresholds, Power Zones Z7 used ⚡ when chart uses purple). Fixed all. Rewrote TRIMP bands to `50/75/100/125/150` matching the panel's actual threshold steps. Added a "Tooltip color icons MUST match the panel's actual chart colors" rule to Dashboard Conventions with a unified 7-emoji palette table (🔘 grey → 🟣 purple) and a "Compressed palettes" note explaining why HR Z5 uses red in 5-zone systems.
- [x] Power Distribution Z7 scale + bucket size (#101) — Z7 Neuromuscular was rendered on a separate Y-axis because its override was missing `unit: min` while Z1-Z6 had it. Added the missing unit so all 7 series share the primary axis. Also widened buckets from 10W to 25W, reducing visual noise from ~55 sparse bars to ~22 cleaner bars across 0-550W range, matching GoldenCheetah's default histogram resolution.
- [x] Overview + Training Report dashboard split (#102) — split Overview into two surfaces, then reversed in #103 (see below).
- [x] Merge Training Report back into Overview (#103) — the #102 split introduced arbitrary boundaries and duplicated stats across both dashboards. Merged back into a single Overview with progressive disclosure: all 9 period stats in compact 2×5 at w=4 (always visible), vs Previous Period + Trends + Ride Patterns as collapsed rows (expand on click), Outdoor Records and Ride Map removed (already on All Time Progression as Personal Records and All-Time Rides Map). Result: 3 dashboards (Overview 41 panels / 22 visible, All Time Progression, Activity Details). Zero duplication. Also fixed Δ Avg HR missing `unit: bpm`.
- [x] Calories + W/kg (#104) — Calories fills the 10th Period Summary slot (both stat rows now 5+5, no dead space) with matching Δ Calories. Per-ride `ride_weight` column + `VELOMATE_WEIGHT` env var follows ride_ftp pattern — stored per ride so historical W/kg is preserved if weight changes. W/kg (NP-based) panel on Activity Details. NP/kg Trend chart on All Time Progression alongside Aerobic Decoupling. Weight change handled independently from TSS/IF/TRIMP config — only resets ride_weight, no expensive recalc.
