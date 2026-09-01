# VeloAI — TODO

Prioritised backlog. VeloMate is an **automatic analytics platform** — data flows in from APIs, metrics are computed, dashboards update. No manual entry, no daily chores. Features must fit this model.

## 🔴 In Progress

(none)

## 📋 Backlog

### Reliability & Hardening — Technical Audit 2026-08-24
Remediation for the module-by-module audit. Full detail, sequencing, and verification steps in `docs/audit-remediation-plan.md`. Audit #1, #2, #3, #4, #9 and #11 are resolved — see the Done section (#3 was withdrawn as wrong, not implemented). Four remain:
- [ ] 🟠 Batch `reclassify_activities` commits instead of one library-wide transaction with per-row network I/O (audit #5; plan PR 5)
- [ ] 🟠 Decide + apply the Postgres network-exposure fix (bind to host IP / host firewall / TLS) without breaking the remote CLI — **needs a decision** (audit #6; plan PR 6)
- [ ] 🟡 Speed up recompute — bulk W'bal writeback + cache per-activity MMP in `fit_period` (audit #7; plan PR 7). No longer blocked: it was deferred behind PR 3, which has shipped
- [ ] 🟡 Harden the CLI — bounds-guard the weather daily arrays + use `tempfile` for the default GPX path (audit #8/#10; plan PR 8)

### High — Automatic Analytics
- [ ] 🔄 Alert when rides stop arriving — ingestion can fail silently and stay that way. A Strava-relayed activity arrives as an unavailable stub the API cannot re-serve, and the poll just logs `0 ingested, N unavailable` forever, which reads as normal. The 2026-08-27 Zwift rides sat stranded for three days that way, and the 2026-06→08 Strava outage went unnoticed for two months. Persist the unavailable-stub count and the last-successful-ingest time to `sync_state`, then surface both on Overview so a broken pipeline is visible without reading container logs. Independent of which source is configured
- [ ] 🔄 Ingest rides that fall outside the sync window — the reconciliation sweep *reports* remote-only activities but never ingests them, so any future outage longer than `INTERVALS_ICU_SYNC_WINDOW_DAYS` (14) needs a manual wide-window run, as the 2026-06→08 Strava-outage backlog did. Either widen the window automatically when the sweep finds remote-only rides, or have the sweep hand them to the sync
- [ ] Auto-attribute rides to a bike from power-meter serial / device name — intervals.icu exposes `power_meter_serial` and `device_name` per ride. (The Strava parser also read `device_name`, but Strava is no longer a source, so intervals.icu is the only live path.) Prerequisite that makes the equipment-tracking entry below work without manual tagging (findings §6)
- [ ] Equipment tracking — auto-count distance/elevation per bike. Wear alerts based on component mileage. No manual entry (gap #7)

### Medium — Data Sources & Ingestion
- [ ] Direct FIT file import — bypass any cloud service for offline rides or devices that don't sync. Automatic ingestion from a watched directory. The only path that depends on no third party at all, which makes it the permanent answer to the Strava-relay stub problem in High above — those rides are unreachable via any API but the FIT file is on the head unit (gap #13)
- [ ] HRV ingestion from wearable APIs — Garmin Connect, Oura, WHOOP. Automatic pull, no CSV upload. Only worth doing if the API is fully automatic. **Note:** syncing this from intervals.icu would import nothing — of 46 wellness fields only 11 are populated for this athlete, and every subjective/wearable one (`hrv`, `restingHR`, `sleepSecs`, `readiness`) is empty. This needs a wearable connector, not an intervals.icu sync (gap #2 phase 3, revised)

### Medium — Computed Metrics
- [ ] Quadrant analysis + pedal analytics — the Favero meter already records `torque`, left/right pedal smoothness and torque effectiveness, and the parser discards them. Force and cadence alone give the quadrants (`AEPF = P / (cadence · 2π/60 · crank_length)`), so the quadrant half is provider-independent and needs only a crank-length config; the pedal-quality channels are intervals.icu-only (findings §6)
- [ ] Pa:Hr ratio — power-to-HR ratio as an aerobic-fitness indicator, trended over the season alongside the existing EF and decoupling panels. The ride-level ratio is derivable from the stored `avg_power`/`avg_hr`, so that half is a dashboard expression with no schema change. The **Z2-only variant is a separate, larger job**: it needs average power and HR over Z2 samples only, which means stream-level zone-filtered computation in the ingestor and a stored column — neither the ride-level averages nor the stored time-in-zone can give it (findings §6)
- [ ] Heart-rate recovery (HRR) — drop in HR over the 60s after a hard effort ends, as a recovery-quality signal. Needs effort-boundary detection, which the interval detector already provides (findings §6)
- [ ] Heat-stress adjustment — ingest the temperature stream and flag or adjust rides ridden in high heat, where the same power costs more physiologically. All three sources carry a temperature channel, so it can be provider-independent (findings §6)
- [ ] hrTSS alongside TSS and TRIMP — a third load model shown side by side reveals measurement quality, which matters most on the VI 1.4+ rides where the models diverge
- [ ] Fresh-vs-fatigued power-duration curve — classify each ride's mean-maximal-power points by the TSB at the time of the ride (fresh > +5, neutral −15 to +5, loaded < −15) and overlay the three curves on All Time Progression. Exposes durability *across training states*, which is distinct from the completed Durability Profile (#112) — that one is within-ride and kJ-based. Provider-independent: both `athlete_stats.tsb` and the stream-derived MMP points are already stored

### Medium — Route Planning Safety
- [ ] Road safety filtering for route planner — hard-exclude roads that are illegal, unsafe, or uncomfortable for cycling. This includes motorways (autoestradas), vias rápidas, and high-speed national roads such as IC/IP routes where cycling is prohibited or dangerous (e.g. IC30 near Sintra). Valhalla supports `use_highways` and `exclude_` penalty weights — use hard exclusions (not just penalties) for roads tagged `highway=motorway`, `highway=trunk`, and `access=no` for bicycles, so the planner never routes through them regardless of detour cost. For roads that are legal but have no shoulder, high traffic, or speed limits ≥90 km/h, apply heavy penalties to force a detour. **The goal is that the rider should never accidentally end up on a dangerous road** — avoidance must be guaranteed, not best-effort. If a hard-excluded road makes a route geometrically impossible (e.g. no alternative crossing exists), fail the route generation and tell the user why, rather than silently falling back to the unsafe road. Warnings are a last resort for edge cases only — the primary contract is avoidance.

  **Start from the existing branch, don't rebuild:** `origin/feat/road-safety-filtering` (last touched 2026-04-19) already carries a complete implementation that never landed — `velomate/road_safety.py` (221 lines: bbox scoping, Overpass query with rate-limit handling, per-segment polygon buffering, 30-day on-disk cache), `exclude_polygons` wiring through `route_generator`/`route_planner`, and 387 lines of tests. Its commits say it already fails closed and names a `no_safe_route` error, which matches the contract above. It is 4 months behind main, so it needs a rebase and a review against this description before it can be proposed — but the work exists.

### Low — Polish & Refinement
- [ ] Graceful no-sensor (GPS-only) experience — a rider with no HR or power sensor gets no training-load analytics (TSS, CTL/ATL/TSB, NP, IF, EF, VI, TRIMP, CP/W', power/HR zones all read "N/A" by necessity), so most panels look empty with no in-product explanation. Add an in-product hint ("connect a heart-rate or power sensor to unlock training metrics") and/or distance/elevation-based progressions so GPS-only riders still get useful trends. Dashboards already degrade gracefully without crashing (#144) — this is about communicating the gap and offering sensor-independent value.
- [ ] eFTP auto-update from single maximal efforts — CP covers the algorithmic estimate; this is for athletes doing deliberate FTP tests where a single breakthrough effort should update immediately. intervals.icu exposes its own `eftp` (198W) which corroborated our CP estimate of 196W (gap #11)
- [ ] PR notifications + durability PRs (best power after ≥1000kJ) — automatic detection from ride data (gap #14)
- [ ] Route library with metadata — auto-detect repeat routes from GPS, tag favourites. Analysis of how performance changes on the same route over time
- [ ] Interval detector: lower `threshold_pct` from 0.85 → 0.78 so tempo and sweetspot-floor efforts become detectable. One-line default + METRICS_VERSION bump
- [ ] Surge vs interval heuristic — urban riders generate many 30-120s anaerobic classifications from traffic light accelerations. Consider max/avg ratio filter or a "surge" class

### Parked — Doesn't Fit the Automatic Model
- [ ] ~~Strava webhook subscriptions~~ — moot: Strava's API moved to a paid tier and Strava was removed as a source on 2026-08-27. Revisit only if a paid key is ever bought
- [ ] ~~Wellness diary (manual RHR/sleep/feel entry)~~ — requires daily user input, breaks the plug-and-forget model
- [ ] ~~Daily readiness score~~ — depends on manual wellness data
- [ ] ~~Daily ride recommendation~~ — prescriptive, not analytical
- [ ] ~~User-defined computed fields via YAML~~ — power-user feature with marginal value

## ✅ Done

### intervals.icu as primary source (2026-08-26/27)
- [x] Zwift rides reach VeloMate — Zwift only relays to Strava, whose stubs cannot be re-served, so Zwift rides could not arrive at all. intervals.icu's direct Zwift integration (Settings → Zwift → Connect) resolves it with nothing installed locally; verified ingesting, `0 unavailable` since. Relaying via Garmin or Wahoo was investigated and documented as a dead end — both refuse to forward third-party activities
- [x] 🐛 Aerobic decoupling on very short rides — the only guard was 4 samples, so a 4-minute ride scored 46.6%. Now requires 20 minutes of pedalling (METRICS_VERSION 14); blanks exactly one ride in the library
- [x] intervals.icu comparison pass — read-only client + offline comparison script; validated NP/EF to 0.1% and CP to 1.5% against an independent implementation. Findings: `docs/intervals-icu-comparison-findings.md`
- [x] intervals.icu as the primary activity source — cursorless windowed sync, daily reconciliation sweep with deletion mirroring, Strava-stub skipping, explicit source precedence, deletion cap with cross-sweep confirmation. Spec: `docs/design/specs/2026-08-26-intervals-icu-primary-source-design.md`
- [x] Remove Strava as an active source — its API moved to a paid tier (403 on every poll); credentials removed from the stack, segment backfill gated on Strava being configured. Historical Strava data preserved (26 rides keep their `strava_id`)
- [x] 🔄 ~~Investigate why ingestion is ~2 months behind~~ — **not a bug**: the Strava paid-API change. Closed by the intervals.icu work; the 2026-06→08 backlog was ingested on 2026-08-27

### Provider-independent metrics (METRICS_VERSION 12)
- [x] 🐛 Fix aerobic decoupling on stop-and-go rides — coasting samples excluded; mean error vs intervals.icu cut from 27.9 to 12.4, worst reading 97% → 9.6, one inverted sign corrected (findings §3)
- [x] CTL ramp rate — rolling 7-day change in CTL on Overview with the 5-7/week caution band. Pure SQL over `athlete_stats`, no schema change
- [x] Polarization index — Treff formula over the 3-zone model, validated against intervals.icu on six shared rides
- [x] Work above FTP ("matches burned") — `kj_above_ftp` column + Activity Details panel
- [x] Coasting time per ride — agrees with intervals.icu within ~2% on every shared ride

### Technical audit remediation (2026-08-24 audit)
- [x] 🔒 Dev-tooling paths reaching the public mirror — six published files, including two ingestor modules, carried doc paths naming local tooling. `docs/` is stripped at publish time, so they were both dangling references and an information leak, and they were already live on GitHub. The publish guard missed them because it matched only the dot-prefixed directory form. Directory renamed to `docs/design/`, all referrers updated, and both guards in `push-to-github.sh` widened to match the bare word — verified by confirming the old tree now fails the check
- [x] 🔴 Strava sync per-activity error isolation — one deterministically-failing activity no longer wedges all newer ingestion; RWGPS deterministic-vs-transient guard ported, dead import removed (audit #1/#11)
- [x] 🟠 Strava refresh-token file fallback — `/app/data` was root-owned and unwritable by the app user, making the fallback dead code. Dockerfile `chown`, atomic temp-file + rename write at 0600, real-file round-trip tests replacing the mocked one (audit #4/#9)
- [x] 🟠 Per-ride CP for W'bal — W'bal modelled every ride against the single latest CP, so early-season rides were scored against a CP 42-64 W too high (a 150 W effort reads as recovery against CP 192, but is above threshold against the 128 W actually current). `select_cp_for_date` picks the newest estimate at or before each ride's date; METRICS_VERSION 13 recomputed all 27 rides / 110k stream rows (audit #2)
- [x] 🟡 Sampling-cadence guard — `sampling_cadence_s` (median inter-sample delta) warns when a ride is genuinely sparse-sampled, replacing the withdrawn stream-normalization rewrite (audit #3)

### Earlier work
- [x] Imperial units support — `VELOMATE_UNITS=metric|imperial`, generated imperial dashboard variants via `scripts/gen_imperial_dashboards.py`, display-layer conversion in `velomate/units.py`, SI storage unchanged
- [x] Athlete type classification — sprinter/pursuiter/rouleur/TT/climber from W'/CP ratio
- [x] VO2max estimate — computed from CP/weight via Storer formula
- [x] Form-zone auto-annotation — TSB threshold coloring on CTL/ATL/TSB chart
- [x] Configurable backfill window via `VELOMATE_BACKFILL_MONTHS` (#89)
- [x] Auto-backfill when `VELOMATE_BACKFILL_MONTHS` is extended (#90)
- [x] Feature gap analysis covering 8 canonical competitor platforms (`docs/features-analysis-06apr26.md`)
- [x] Cluster A implementation plan (`docs/design/plans/2026-04-06-ride-analytics-depth.md`)
- [x] `velomate-features-designer` project skill for ongoing gap evaluation
- [x] Cluster A — Ride Analytics Depth (#91) — stored `aerobic_decoupling` column + trend panel + period stats, `ride_intervals` table + detection module + Activity Details interval table + monthly distribution chart
- [x] Overview polish (#92) — decoupling collision fix, Δ Avg Decoupling, loosened steady-state filter, `now-30d` default, collapsible rows for secondary sections
- [x] Overview + Training Report split design spec (`docs/design/plans/2026-04-06-overview-training-report-split.md`)
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
