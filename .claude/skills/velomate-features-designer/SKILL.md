---
name: velomate-features-designer
description: Use when evaluating VeloMate's cycling analytics feature gaps, deciding what to build next, or comparing the platform against leading systems (Strava Premium, GoldenCheetah, intervals.icu, TrainingPeaks, Xert, WKO5, Garmin, TrainerRoad) for an amateur cyclist focused on progress, fitness, and performance
---

# VeloMate Features Designer

Project-specific feature-gap evaluator for VeloMate. Produces a prioritised report of missing cycling analytics capabilities, grounded in what leading platforms offer and in what VeloMate already has. The output answers one question: **"If I want to move the needle on progress, fitness, and performance as an amateur cyclist, what's the next most valuable thing to build?"**

This skill is:
- **NOT** a general architecture review → use `arch-features-designer`
- **NOT** a bug/security audit → use `technical-audit`
- **NOT** a code reviewer → it identifies *capabilities* that don't exist yet
- **NOT** a feature wishlist — every gap must be justified against the target user's goals

## Target User Profile

A serious amateur cyclist enthusiast. They ride 5–15 hours per week, care about data, and want to use VeloMate to:

- **Track rides** to understand training patterns and volume
- **Monitor fitness, form, and fatigue** — know when fresh vs overreaching
- **Optimise power, endurance, and durability** — raise the ceiling systematically
- **Plan rides intelligently** — routes, timing, weather, terrain
- **Replace paid tools** (Strava Premium, TrainingPeaks, Xert) with self-hosted equivalents

They are **not** a coach managing athletes, **not** a beginner who needs hand-holding, and **not** a pro with a support staff. They are data-curious, tinker-willing, and motivated by measurable improvement. They already have a power meter and HR strap.

**Design implication:** features should reward engagement without requiring it. A cyclist who logs RPE daily should get more value — but a cyclist who just syncs Strava shouldn't see broken dashboards.

## When to Use

Trigger on any of these:

- "What should we build next in VeloMate?"
- "What's missing compared to [Strava / intervals.icu / TrainingPeaks / Xert / GoldenCheetah]?"
- "Is our fitness / power / training load model good enough?"
- "Are we competitive with free alternatives like intervals.icu?"
- "What would make me switch from [paid tool] to VeloMate?"
- "Review the dashboard — what's missing?"
- Any request to brainstorm, prioritise, or road-map cycling analytics features for this project

## Process

1. **Snapshot current state.** Read `README.md`, `CLAUDE.md`, `CHANGELOG.md`, `RELEASE_NOTES.md`, and the project structure. Cross-reference with the "What VeloMate Already Has" section below — but verify it's still accurate, the project moves fast. Don't propose features that already exist.
2. **Walk each dimension.** For each of the 8 evaluation dimensions below, check: does VeloMate cover this? At what depth? Compared to baseline expectations (Coggan PMC, power-duration curves, wellness ingestion)?
3. **Identify high-leverage gaps.** A gap qualifies only if: (a) multiple leading platforms offer it, (b) VeloMate lacks it or has only a thin version, (c) it matches the target user's goals. Dimension-by-dimension, not random wishlist.
4. **Prioritise** using the rubric. Rank each gap by leverage × effort × current-state-alignment.
5. **Produce the report** in the format below.

**Key principle:** A gap is only a gap if it would change how the user trains or improves. "Nice to have" is not enough. Ask: "Would this feature produce a measurable change in the rider's decisions or performance over 3 months?"

## What VeloMate Already Has (Baseline)

Use this as the "don't propose these" list. Verify against the current repo before relying on any item — it can drift.

**Data pipeline**
- Strava polling every 10 min (Ride, VirtualRide, EBikeRide, Handcycle, Velomobile; filters out runs/swims/strength)
- Per-second telemetry: HR, power, cadence, speed, altitude, lat/lng
- Cross-device deduplication with data-richness scoring
- 12-month backfill on first run

**Computed per-ride metrics** (stored in `activities` table; ingestor is single source of truth)
- NP (Coggan 30-sec SMA → 4th power)
- TSS (power-based with HR fallback)
- IF (NP / per-ride FTP)
- VI (NP / avg power)
- EF (NP / avg HR)
- Work (kJ from power stream)
- TRIMP (Banister exponential, male coefficients, HRR capped)
- Aerobic decoupling (computed in Grafana SQL, not pre-stored)
- Per-ride FTP (rolling 90-day best 20-min × 0.95)

**Daily fitness metrics** (stored in `athlete_stats`)
- CTL (42-day EMA of TSS)
- ATL (7-day EMA of TSS)
- TSB (CTL − ATL)
- Recalculated daily at 00:05 UTC + on new activities + on config changes + on METRICS_VERSION bumps

**FTP estimation**
- Rolling 90-day best 20-min × 0.95
- Configurable override (`VELOMATE_FTP`)
- Per-ride FTP backfill preserves historical TSS accuracy

**Zones**
- HR zones: 60/70/80/90% of threshold HR (5 zones)
- Power zones: Coggan 7-zone (Z1–Z7, Z7 = >150% FTP)

**Dashboards** (3 Grafana dashboards, ~122 panels)
1. **Overview** — fitness cards, period stats with deltas, trend charts, ride patterns, outdoor records, activities table, ride heatmap
2. **Activity Details** — summary + power quality cards, GPS map, HR/power zones by km, distributions, power-duration curve, telemetry, per-km splits, power-vs-HR scatter
3. **All Time Progression** — totals, performance progression with rolling averages, FTP progression, best efforts (1/5/20 min), zone polarisation monthly, fitness history, cumulative totals, YoY compare, annual totals, personal records, all-time ride map

**CLI** (`python3 -m velomate.cli`)
- Default: weekly ride recommendation (fitness + weather + past routes)
- `plan`: intelligent route generator with 13 flags (duration/distance/destination/surface/safety/preference/waypoints/date/time/start/loop/output)

**Route intelligence — VeloMate's distinctive strength** (10 data sources)
1. OSM POIs (Overpass)
2. Strava segments
3. Komoot highlights
4. Ride history GPS density (variety/comfort mode)
5. OSM surface tags
6. OSM cycling infrastructure → safety score
7. Open-Meteo weather + hourly forecast
8. Open-Meteo air quality
9. Open Topo Data elevation
10. Sunrise/sunset daylight awareness

Valhalla routing engine (bicycle/mountain_bike profiles). GPX output + interactive HTML preview.

**Automation**
- Daily fitness recalc 00:05
- METRICS_VERSION-gated full recalculation
- Config-change detection → selective recalculation
- Strava token rotation with DB + file fallback

**Known explicit non-goals or gaps noted in the repo**
- `routes` table is legacy
- `athlete_stats.vo2max` column exists but is never populated
- `komoot_tour_id` column exists but never written
- Manual Strava OAuth (no `velomate auth` command yet — known feedback item)

## Evaluation Dimensions

For each dimension: what the user gets, the baseline industry expectation, upgrade signals with named reference implementations, and what VeloMate currently has.

### 1. Training Load & Fitness Modeling

**Purpose:** Help the user understand current fitness, fatigue, and form over time so they can decide when to push and when to recover.

**Baseline:** Coggan Performance Management Chart (PMC) with CTL/ATL/TSB on 42/7 day EMA. Visible trend.

**Upgrade signals**
- **Training Monotony & Training Strain** (Foster): Monotony = weekly mean TSS ÷ stddev; Strain = weekly TSS × monotony. High values correlate with illness/overtraining. Runalyze and GoldenCheetah surface this.
- **Multi-model load comparison**: Show TSS (power) vs hrTSS vs TRIMP vs session RPE side by side for rides where multiple are computable. Reveals measurement quality.
- **Load forecasting**: Project CTL/ATL N days ahead if planned rides are completed vs skipped. intervals.icu does this on the calendar.
- **Configurable EMA constants**: Some athletes prefer 35/7 or 50/10 instead of 42/7. WKO5 and intervals.icu allow this.
- **Form-zone auto-annotation**: Auto-label timeline regions as "peak" (TSB > +10), "fresh" (0 to +10), "neutral" (-10 to 0), "productive" (-30 to -10), "overreaching" (< -30). Garmin's "Training Status" is the most famous example.
- **Weekly distribution across intensity zones** (polarised vs pyramidal vs threshold): percentage of time in Z1/Z2 vs Z3 vs Z4+. Several platforms offer this.

**VeloMate has:** Coggan PMC (CTL/ATL/TSB 42/7), TRIMP, TSS, monthly zone polarisation on All-Time Progression.
**Common gaps:** Monotony/Strain, load forecast, configurable EMAs, form-zone auto-annotation.

### 2. Performance Modeling

**Purpose:** Help the user understand their current capability ceiling and which energy systems need work.

**Baseline:** FTP (one number) + best efforts by duration (1s, 5s, 1m, 5m, 20m, 60m). A single FTP number is the amateur norm but is the thinnest serious model.

**Upgrade signals**
- **Power-Duration curve** with mean-maximal power across all durations. ✓ VeloMate already has this on All-Time Progression and Activity Details.
- **Critical Power / W' model**: Fit a Monod-Scherrer 2-parameter or Morton 3-parameter CP model to the PD curve. Outputs: CP (sustainable power, similar to FTP but model-derived) and W' (anaerobic work capacity in kJ). More diagnostic than a single FTP. Present in GoldenCheetah, intervals.icu, WKO5.
- **W'bal time series** per ride (Skiba differential model): second-by-second depletion/refill of W'. Shows exactly when the tank ran dry during hard efforts. Invaluable for crit and interval pacing. Present in GoldenCheetah, intervals.icu, Xert (as MPA — Maximal Power Available).
- **Fresh vs Fatigued PD curves**: Compare best efforts achieved when CTL was low (fresh) vs high (fatigued/loaded). Reveals durability deficits — amateurs often have a fresh 5-min power that collapses when tired. GoldenCheetah, intervals.icu, WKO5.
- **Athlete type classification**: Sprinter / puncheur / time-trialist / rouleur / climber from PD curve shape. Xert and WKO5 do this; it's motivating and useful for training design.
- **eFTP auto-update from single efforts**: Don't wait for a 20-min test; if someone does a hard 8-min effort, immediately update FTP estimate. intervals.icu does this.
- **Fatigue resistance** (durability): Drop-off in peak 5-min power after ≥1000 kJ of work. A TrainingPeaks/WKO metric.
- **VO2max estimate from HR + power**: Garmin produces one; Runalyze too. VeloMate has the schema column but never populates it.
- **Advanced power-duration outputs (WKO5-style)**: mFTP, TTE (time-to-exhaustion at FTP), FRC (functional reserve capacity), Pmax, Stamina. These come from fitting the PD model.

**VeloMate has:** FTP (rolling 90-day × 0.95), NP, IF, VI, EF, PD curve, best efforts by duration, monthly FTP progression.
**Common gaps:** CP/W' model, W'bal time series, fresh-vs-fatigued PD, athlete type, eFTP auto-update, fatigue resistance, VO2max, WKO5-style metrics.

### 3. Ride Analytics Depth

**Purpose:** Per-ride insights that help the user understand *how* a ride went, not just that it happened.

**Baseline:** HR/power/cadence/speed charts, zone distributions, basic NP/IF/TSS.

**Upgrade signals**
- **Automatic interval detection** with spike correction: Detect hard efforts without manual laps. Classify as steady vs VO2 vs sprint. intervals.icu does this automatically; GoldenCheetah has an interval finder.
- **Quadrant analysis** (force × cadence scatter): Plot every pedal stroke by pedal force vs cadence; four quadrants show muscular vs cardiovascular emphasis. Crucial for characterising whether training is neuromuscular, muscular endurance, or tempo. GoldenCheetah, TrainingPeaks, WKO5.
- **Pedal analytics** from FIT fields: left/right balance, torque effectiveness, pedal smoothness (when recorded). GoldenCheetah, TrainingPeaks, WKO5, Garmin.
- **Cardiac drift trend over time**: VeloMate computes decoupling per ride but shows it only on Activity Details. Surface it as a *trend* across the season — rising decoupling at steady efforts = aerobic fitness degrading.
- **Pa:Hr ratio** (power-to-HR drift) as an aerobic fitness indicator: specifically flag steady-state rides (VI < 1.05, duration > 60 min) and chart drift monthly.
- **Matches and efforts**: How many "matches" (short hard efforts above FTP) were burned? Where? GoldenCheetah shows this.
- **Isopower and xPower alternatives** to NP (for comparison).
- **Power-vs-HR scatter with regression drift**: VeloMate has scatter on Activity Details; consider trending regression slope changes over time.
- **Heat stress / thermal load estimate**: At high temps, effective intensity is higher — some platforms adjust TSS for temperature.

**VeloMate has:** Per-ride NP/IF/VI/EF, HR/power zones by km, distributions, PD curve, splits table, decoupling (per ride only), power-vs-HR scatter.
**Common gaps:** Auto interval detection, quadrant analysis, pedal analytics, decoupling trend, matches/efforts, heat adjustment.

### 4. Structured Training & Periodisation

**Purpose:** Help the user plan and execute workouts with purpose, not just ride randomly.

**Baseline:** None — VeloMate has nothing in this dimension today. This is a complete greenfield.

**Upgrade signals** (each of these is a significant feature on its own)
- **Workout builder** with target power/HR/cadence per step. Structured blocks: warm-up, intervals × N, cool-down. TrainingPeaks, intervals.icu, Xert, TrainerRoad, Wahoo SystM, Strava Premium.
- **Workout export to head unit**: FIT format (Garmin), ZWO (Zwift), ERG/MRC (smart trainers). Every major platform does this.
- **Workout library**: Pre-built workouts targeting specific energy systems (VO2 max 30/30, sweet spot 3×20, threshold 4×8, anaerobic capacity 8×40s). Joe Friel's library is canonical.
- **Training plan templates**: Multi-week blocks (base → build → peak → taper). Base templates for race distance (century, gran fondo, crit, time trial).
- **Calendar view** with planned vs completed, compliance scoring.
- **Annual Training Plan (ATP)** TrainingPeaks-style: target A/B/C races, periodised phases, weekly TSS targets.
- **Adaptive plans**: Adjust future workouts based on compliance, TSB, progression-level surveys. TrainerRoad's Adaptive Training and Xert's XATA are the references.
- **"What to ride today" recommendation**: Given current TSB, wellness, weather, and plan phase, suggest today's workout. Xert XATA, TrainerRoad TrainNow, Garmin Daily Suggested Workout. **VeloMate already has a weekly version of this in `cmd_recommend`** — this is the closest VeloMate comes to structured training.
- **Race-day taper modelling**: Project the TSB trajectory needed for race day; compare to current.

**VeloMate has:** `cmd_recommend` (weekly ride suggestion from fitness + weather + past routes) — a thin but genuine version of adaptive recommendations.
**Common gaps:** Everything else. This is the biggest functional gap relative to leading platforms.

### 5. Recovery, Wellness & Readiness

**Purpose:** Help the user understand whether their body is ready to train hard today, and whether they're trending toward overreaching.

**Baseline:** None — VeloMate has configured resting HR as a one-time value, but no longitudinal wellness tracking.

**Upgrade signals**
- **Wellness diary**: Daily log of RHR, sleep hours, sleep quality, HRV (rMSSD or SDNN), soreness, mood, stress, motivation, weight, hydration, RPE. intervals.icu and TrainingPeaks both have extensive wellness diaries with custom fields.
- **HRV ingestion from wearables**: Auto-sync from Oura, WHOOP, Garmin (HRV Status), Polar, Apple Health (via HealthFit). intervals.icu supports all of these.
- **Readiness score**: Composite (HRV + sleep + load balance + stress) → today's training capacity. Garmin Training Readiness, Wahoo Training Capacity Score, Oura readiness.
- **Wellness-adjusted training recommendations**: If HRV is depressed and sleep is poor, recommend recovery even if TSB would suggest otherwise. This is where wellness pays off.
- **Trend warnings**: "HRV trending down for 7 days" or "RHR elevated 5% above baseline" → soft warning.
- **Weight trend tracking** with W/kg power adjustment: if weight changes, recompute power-to-weight across history.
- **Menstrual cycle tracking** (for athletes who want it): training recommendations aware of cycle phase.

**VeloMate has:** Resting HR as a static config value used in TRIMP calculation. Nothing longitudinal.
**Common gaps:** Everything. Starting from a clean slate, the biggest open question is data ingestion (manual form vs wearable sync).

### 6. Route & Environment Intelligence

**Purpose:** Help the user plan rides that match their goal, fitness, and external conditions.

**Baseline in the industry:** Strava Route Builder (surface + popularity), Garmin Course Builder (round-trip routing), Komoot/RWGPS full planners.

**VeloMate's position: distinctive strength.** The 10-source route intelligence is the most novel thing the platform has. Outside of the paid Strava/Komoot/RWGPS axis, no self-hosted tool comes close. Evaluate this dimension for *polish and depth*, not gaps — but these still qualify:

**Upgrade signals**
- **Route repeat analysis**: "You've ridden this road N times, fastest on date X." Sauce for Strava has similar.
- **Climb categorisation**: Auto-categorise climbs (HC, Cat 1–4) by length × grade. Strava does this; VeloMate has elevation data but no climb detection that I can see.
- **Segment-style PRs on user-chosen routes**: Track times on frequently-ridden routes as informal segments.
- **Weather-adaptive route suggestion**: "Wind is 30 km/h NW today — suggest an out-and-back into the wind first." Some route planners do this.
- **Route library with metadata**: Save favourite routes, add tags (recovery, sweet-spot, hills, coffee stop), surface notes.
- **CdA / Crr aero estimation** (Aerolab-style): From power, speed, elevation, and wind data, estimate aerodynamic drag coefficient and rolling resistance. GoldenCheetah has this. Useful for time trialists.
- **Headwind-adjusted pacing guidance**: Given planned route + wind forecast, estimate effective power required per segment.
- **Ride report with road quality feedback loop**: User rates the surface/safety post-ride; updates confidence on OSM tags for future plans.

**VeloMate has:** Valhalla routing, 10-source intelligence, safety scoring from OSM cycling infra, weather-aware timing, air quality, daylight awareness, scenic scoring, surface verification, GPX output, interactive preview, waymarked cycling trail detection, corridor padding for destination rides.
**Common gaps:** Climb categorisation, route library, repeat tracking, Aerolab-style aero, headwind pacing.

### 7. Equipment, Records & Competition

**Purpose:** Track the bike, track the bests, give the user things to chase.

**Baseline:** Component mileage tracking is table-stakes (Strava, Garmin, intervals.icu, TrainingPeaks all have it). Records are expected. Segments are Strava-specific but PR tracking on user routes is not.

**Upgrade signals — Equipment**
- **Multi-bike support** with per-bike component trees: chain, tires, cassette, chainrings, bottom bracket, cables, brake pads, bar tape.
- **Wear tracking and reminders**: Per-component mileage with replacement thresholds. Alert when due.
- **Maintenance log**: Service history, notes, cost tracking.
- **Auto-attribution**: Infer which bike was ridden from device name / power meter ID / trainer flag.

**Upgrade signals — Records & Competition**
- **Power PRs by duration** with notifications: "You just set a new 5-min PR." VeloMate has the table but probably not the alerting.
- **Durability PRs**: Best power after 1000/2000/3000 kJ.
- **Streak tracking beyond "days since ride"**: Ride-a-day streaks, weekly consistency streaks.
- **Virtual segments**: User-defined segments on favourite roads, tracked across rides.
- **Leaderboard mode**: Compare self vs past self (year-over-year on the same route/segment/week).
- **Achievements / badges**: Gamification — not critical but motivating for amateurs.

**VeloMate has:** Outdoor records table, all-time records table, best efforts by duration, personal records table, weekly streak, days since ride.
**Common gaps:** Equipment tracking entirely, PR notifications, durability PRs, virtual segments, year-over-year self-compare.

### 8. Extensibility, Integration & Data Sources

**Purpose:** Let power users shape VeloMate to their needs and get data in from more than just Strava.

**Baseline:** Strava-only ingestion. SQL access to the DB. Grafana dashboards are customisable via JSON.

**Upgrade signals — Extensibility**
- **User-defined computed fields at activity level**: Let the user write `(NP / resting_hr) × duration_h` and have it available on every activity and chart. intervals.icu computed fields, GoldenCheetah Python/R, WKO5 expressions are the references.
- **Custom metric dashboards** (beyond Grafana): User-built trend charts with user-defined Y-axis formulas. intervals.icu's Plotly-based custom charts are the archetype.
- **Event hooks**: Trigger a shell command or webhook on new activity / PR / TSB threshold crossing.
- **Metric plugins**: Drop-in Python module that adds a new per-ride metric without forking the ingestor.

**Upgrade signals — Integration**
- **Direct FIT file import**: Import from Garmin/Wahoo head units without going through Strava. This protects the user from Strava API changes and lets them backfill non-Strava rides. intervals.icu does this.
- **Other data sources**: Polar Flow, Suunto, Coros, Garmin Connect direct, Wahoo Cloud, Komoot, RWGPS import. These are easy wins once FIT import exists.
- **HRV / wellness wearables**: Oura, WHOOP, Garmin HRV, Polar H10 (Elite HRV), HealthFit (Apple Health). Each would need a connector.
- **OAuth browser flow for Strava**: Known feedback item — replace the manual curl token exchange with a `velomate auth` command that opens a browser.
- **Webhook-driven sync** (Strava subscriptions): Replace 10-min polling with push notifications from Strava Webhooks API. Lower latency, fewer API calls.

**VeloMate has:** Strava polling (Ride/VirtualRide/EBikeRide), config.yaml, METRICS_VERSION-gated recalc, environment variables, Grafana JSON dashboards, password_cmd secret manager support.
**Common gaps:** Direct FIT import, non-Strava sources, HRV wearables, OAuth flow, user-defined computed fields, event hooks.

## Priority Rubric

Rate each gap on three axes:

**Leverage** — how much does this change the user's training decisions or outcomes?
- **High:** Enables a whole new class of decisions (wellness-driven load adjustment, CP/W' model changes how intervals are designed).
- **Medium:** Improves existing decisions with better data (monotony warning, climb categorisation).
- **Low:** Nice polish (better chart styling, additional zones).

**Effort** — rough build cost.
- **Small:** < 1 day (add a metric calculation, new SQL query, new dashboard panel).
- **Medium:** 1–5 days (new ingestor logic, new CLI command, schema migration with backfill).
- **Large:** > 5 days (new subsystem: workout builder, wellness ingestion, FIT import, CP modeling with fits).

**Alignment** — fits VeloMate's current shape?
- **Natural:** Extends existing patterns (new per-ride metric, new dashboard panel, new computed column).
- **Adjacent:** Adds a new capability but uses existing infrastructure (schema additions, new ingestor routine).
- **Structural:** Requires a new subsystem that doesn't exist yet (workout authoring, HRV sync, adaptive recommendation engine).

Score = Leverage × (1 / Effort). **Top priority = high leverage, small/medium effort, natural/adjacent alignment.** Structural work can be high priority if leverage is transformative — but flag it clearly so the user can decide whether to commit.

## Output Format

```markdown
# VeloMate Features Analysis — {date}

## Summary

{2–3 sentences: overall cycling-analytics maturity, the most significant gaps, whether VeloMate is closer to "self-hosted Strava" or "self-hosted intervals.icu" today, and where the next step should go.}

## Current Position

{1 paragraph: VeloMate's competitive position relative to the 5 canonical points on the map — free Strava, Strava Premium, intervals.icu, TrainingPeaks/WKO5, Xert. What it already beats, what it matches, what it lags on.}

## Prioritised Gap List

| # | Gap | Dimension | Leverage | Effort | Alignment | Score |
|---|-----|-----------|----------|--------|-----------|-------|
| 1 | ... | ... | H/M/L | S/M/L | Nat/Adj/Struct | ... |

## Top Gaps in Detail

For each top-priority gap (target 5–8):

### 1. {Gap title}

**Dimension:** {which of the 8}
**Leverage / Effort / Alignment:** H/S/Natural
**Reference implementations:** {platforms that have it, with one-line description}
**What VeloMate has today:** {current state, if any}

**What to build:**
{Concrete description of the feature: what data, what computation, where it lives in the stack, what panel/command/dashboard exposes it. Be specific about schema changes, new modules, new dependencies.}

**Why it matters for our user:**
{1–2 sentences tying it to the target-user goals (progress, fitness, performance).}

**Risks / considerations:**
{Anything non-obvious — data quality, Strava API limits, stream availability, etc.}

---

(repeat for each top gap)

## Secondary Gaps

{Bullet list of medium-priority gaps with 1-line description each. These are on the roadmap but not next.}

## Non-Gaps Considered

{List features from leading platforms that were evaluated and *rejected* for VeloMate, with a one-line reason. E.g. "Segment leaderboards (Strava social feature — doesn't fit self-hosted single-user model)", "Multi-sport unified load (VeloMate is cycling-only by design)". This demonstrates judgment.}

## What's Already Good

{2–3 sentences: what VeloMate already does better than most alternatives. Route intelligence, per-ride FTP backfill, METRICS_VERSION recalculation discipline, stored-metrics-as-single-source-of-truth, etc. Calibrates the gap list against existing strength.}
```

## What to Skip

Do **not** propose any of these — they either don't fit the target user or aren't what this skill is for:

- **Social / community features** (Strava-style feed, kudos, followers, group rides, clubs). The user is self-hosting; single-user is the design.
- **Multi-sport beyond cycling** (running, swimming). VeloMate is cycling-only by explicit scope in `CLAUDE.md`.
- **Coaching/athlete-sharing workflows** (TrainingPeaks coach seats, workout comments, completed-vs-planned compliance scoring for a third-party). The user is not a coach.
- **Generic architecture/infrastructure advice** (Kubernetes, microservices, plugin systems, message queues). That's `arch-features-designer` territory. This skill is domain-specific.
- **Bug fixes, refactoring, polish** (renaming columns, adding type hints, improving error messages). Not a code review.
- **Things the project explicitly marks as non-goals** (the `routes` table rebuild, generic weight-management features).
- **Vague improvements** ("better documentation", "improved UX"). Only concrete, buildable capabilities.
- **Paid-API dependencies** without a free fallback (no "integrate with [paid service]" unless there's an offline path too — VeloMate's pattern is free/self-hosted).

## Reference: Competitor Landscape Cheat Sheet

**Strava Premium** — Fitness & Freshness (their PMC), Power Curve, Power Analysis, Route Builder, Live Segments, segment leaderboards, structured workout builder, training plans. Free Strava is essentially social + logging.

**GoldenCheetah** — Open source desktop reference. Quadrant analysis, pedal analytics, W'bal (Skiba), multiple CP models (Monod, Morton, Veloclinic), Aerolab aero testing, Python/R custom metrics, deep wellness diary. No cloud, no route building.

**TrainingPeaks** (Premium = $20/mo) — Canonical Coggan PMC. Annual Training Plan (ATP) with A/B/C races. Structured workout builder + workout library (Joe Friel). Coach-athlete sharing. WKO5 is the desktop companion for power-duration modeling: mFTP, TTE, FRC, Pmax, Stamina.

**intervals.icu** — Free Strava companion. Closest functional equivalent to VeloMate's ambition. PMC, CP/W' with Monod + Morton fits, eFTP auto-update, wellness ingestion from every major wearable (Oura/WHOOP/Garmin/Polar/Suunto/Coros/Amazfit/HealthFit), plan calendar, computed fields with formulas, Plotly custom charts, workout builder with FIT/ZWO export. **The benchmark for "am I as good as the free option?"**

**Xert** (freemium → ~$10/mo) — Adaptive Fitness Signature (Threshold Power, HIE = W', Peak Power) auto-updated from rides. MPA (Maximal Power Available) real-time during rides. XSS load split into Low/High/Peak (aerobic/threshold/anaerobic). Fitness Breakthrough detection. Forecast AI for outcome-driven plans. XATA daily recommendation. Athlete Type classification.

**Garmin Connect** — Device ecosystem default. Acute Load, Training Status (Productive/Peaking/Overreaching/etc.), Training Load Focus (low/high aerobic + anaerobic bins), HRV Status (overnight baseline), Body Battery, Training Readiness (composite). Daily Suggested Workout. VO2max estimate from HR+power. Course builder with Trendline routing (Strava heatmap based).

**TrainerRoad** (~$20/mo) — AI FTP Detection. Adaptive Training with per-zone Progression Levels that respond to workout compliance surveys. Plan Builder from goals + event date. 3000+ workout library. Indoor-focused.

**Wahoo SystM** (~$15/mo) — 4DP profile (Neuromuscular / Anaerobic Capacity / MAP / FTP) from Full Frontal test. Workouts personalise per system. Video-led structured training (Sufferfest heritage).

**Runalyze** — Self-hostable. VO2max per activity, TRIMP-based monotony/strain warnings, automatic climb categorisation, custom zone metrics. A reference for self-hosted feature density.

**Sauce for Strava** (browser extension) — CTL/ATL/TSB on Strava pages, NP/xPower/IF, on-demand segment generation, FIT/TCX/GPX export, performance predictor. Shows what "stretch" features on top of Strava look like.

Use these as the "how does intervals.icu handle this?" or "what would Xert do?" mental check when evaluating a gap.

## Common Mistakes

- **Proposing features that already exist.** Always cross-check "What VeloMate Already Has" section AND the current repo state.
- **Recommending social features.** Single-user self-hosted. No.
- **Generic "add X integration" without a path.** Specify the data, the API, the cost, the config.
- **Forgetting the target user is an amateur.** Don't propose Aerolab-style aero testing as high priority unless the user is a time trialist (and even then, it's specialty). But don't be condescending either — this user is serious.
- **Not tying the gap to an outcome.** Every gap must answer "what decision or behaviour does this change for the user?"
- **Treating "completeness vs TrainingPeaks" as the goal.** VeloMate doesn't need to be a full replacement for every paid tool. It needs to be *useful enough* that the user prefers it for their workflow.
- **Skipping the priority rubric.** Leverage × (1/Effort) is the whole point — don't produce a flat feature list.
- **Ignoring VeloMate's strengths.** Route intelligence is genuinely distinctive. Build on it rather than dilute attention with unrelated work.

## Key Principle

**A gap is only a gap if it would measurably change how the target user trains or improves over 3 months.** If you can't answer "what decision does this feature change?", it's not a gap — it's a wish. The goal is to find the smallest set of additions that most raise the ceiling on the user's training quality, not to clone every feature of every leading platform.
