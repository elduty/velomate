# VeloMate Features Analysis — 2026-04-06

Generated via the `velomate-features-designer` skill. Competitive context from a deep web research pass across Strava Premium, GoldenCheetah, TrainingPeaks, intervals.icu, Xert, WKO5, Garmin Connect, TrainerRoad, Wahoo SystM, and Runalyze/Sauce. Baseline from a full repo read.

## Summary

VeloMate today is a **self-hosted Strava-Premium-minus-social** with genuinely best-in-class route intelligence and a disciplined metrics pipeline. Its biggest functional gaps relative to leading platforms are **zero wellness/HRV tracking**, **single-number FTP instead of a CP/W' model**, and **no surfaced trend for cardiac drift** — all three are natural extensions that reuse existing infrastructure. The highest-leverage next step is not adding a workout builder (large, structural, low fit for a single-user tool) but surfacing the durability and drift signals already latent in the data, then adding a wellness diary to enable readiness-driven recommendations.

## Current Position

On the competitive map, VeloMate **exceeds free Strava** for analytics depth (PMC, per-ride FTP, zone splits, route planning) and **matches Strava Premium** for most core analytics while **distinctly exceeding it on route intelligence** (10 data sources vs Strava's basic route builder). Compared to **intervals.icu** — the closest functional rival and the "am I as good as the free option?" benchmark — VeloMate matches the core PMC/metrics but trails on wellness ingestion, CP/W' modeling, computed fields, and workout authoring. Versus **GoldenCheetah** it trails on analytical depth (no CP/W', W'bal, quadrant analysis, Aerolab), and versus **TrainingPeaks/WKO5** it trails on power-duration modeling (no mFTP/TTE/FRC/Stamina). Versus **Xert** it uses a fundamentally different (simpler, per-ride) model rather than a continuously adaptive signature. VeloMate is closer to "self-hosted Strava Premium" than "self-hosted intervals.icu" today; the next step should push it toward the latter while preserving its route-intelligence advantage.

## Prioritised Gap List

| # | Gap | Dimension | Leverage | Effort | Alignment | Notes |
|---|-----|-----------|----------|--------|-----------|-------|
| 1 | Cardiac drift trend over time | Ride Analytics | H | S | Natural | Decoupling already computed per ride — surface the trend |
| 2 | Wellness diary + readiness score | Recovery | H | L | Adjacent | Entire dimension at zero; biggest greenfield with high fit |
| 3 | CP/W' model (Monod-Scherrer + Morton) | Performance Modeling | H | M | Natural | Replaces single-number FTP thinking |
| 4 | W'bal time series per ride | Performance Modeling | H | M | Natural | Builds on #3; new Activity Details panel |
| 5 | Fresh vs fatigued PD curves | Performance Modeling | H | M | Natural | Uses existing data; segments by CTL |
| 6 | Auto interval detection | Ride Analytics | H | M | Natural | Transforms per-ride analysis |
| 7 | Equipment tracking with wear alerts | Equipment/Records | H | M | Adjacent | Table stakes; amateur high-value |
| 8 | Daily "ride today" recommendation | Structured Training | H | M | Natural | Extends existing `cmd_recommend` |
| 9 | Training Monotony & Strain (Foster) | Training Load | M | S | Natural | Quick overreaching warning |
| 10 | Climb categorisation (HC/Cat 1–4) | Route Intelligence | M | S | Natural | Easy route polish |
| 11 | eFTP auto-update from single efforts | Performance Modeling | M | S | Natural | Smarter FTP estimate |
| 12 | OAuth browser flow for Strava | Extensibility | M | S | Natural | Known feedback item |
| 13 | Direct FIT file import | Extensibility | M | M | Adjacent | Reduces Strava-only fragility |
| 14 | PR notifications + durability PRs | Equipment/Records | M | S | Natural | Motivating, low effort |
| 15 | VO2max from HR + power | Performance Modeling | M | M | Natural | `athlete_stats.vo2max` column already exists, unused |
| 16 | User-defined computed fields | Extensibility | M | M | Adjacent | Power-user feature |
| 17 | Athlete type classification | Performance Modeling | L | S | Natural | After #3 exists, nearly free |

## Top Gaps in Detail

### 1. Cardiac drift trend over time

**Dimension:** Ride Analytics Depth
**Leverage / Effort / Alignment:** High / Small / Natural
**Reference implementations:** intervals.icu shows Pa:Hr as a trend chart; GoldenCheetah surfaces decoupling in its season chart; TrainingPeaks has Pa:Hr as a filterable column.
**What VeloMate has today:** Aerobic decoupling is computed per ride in Grafana SQL on the Activity Details dashboard. It is not pre-stored and not surfaced as a trend on the Overview or All Time Progression dashboard.

**What to build:**
- Add `aerobic_decoupling` column to `activities` table; compute it in `ingestor/fitness.py` using the same `first_EF / second_EF − 1` formula that Grafana currently does, but on ingest.
- Bump `METRICS_VERSION` so historical rides are backfilled.
- Add a new panel on **All Time Progression** dashboard: "Aerobic Decoupling Trend — steady-state rides (VI < 1.05, duration > 60 min)" — timeseries with 10-ride rolling average and regression.
- Add a monthly summary stat on **Overview**: "Avg decoupling (steady rides)" with a delta vs previous period.
- Optionally flag rides with decoupling > 5% on the activities table as "high drift" (red pill).

**Why it matters for our user:**
Rising decoupling at the same intensity is a leading indicator of aerobic fitness degrading (or fatigue accumulating); falling decoupling means aerobic base is improving. This is one of the few metrics where the ride-to-ride signal is noisy but the monthly trend is diagnostic — and VeloMate already has all the data.

**Risks / considerations:**
- Filtering to steady-state rides only (VI < 1.05, duration > 60 min, power present) is essential; raw decoupling on crit-style rides is meaningless.
- Warn about small samples: if a month has < 3 steady-state rides the trend dot should be dimmed or omitted.

---

### 2. Wellness diary + readiness score

**Dimension:** Recovery, Wellness & Readiness
**Leverage / Effort / Alignment:** High / Large / Adjacent
**Reference implementations:** intervals.icu has a 15+ field wellness diary (HRV, sleep, soreness, stress, weight, RHR, hydration, menstrual) with auto-sync from Oura/WHOOP/Garmin/Polar/Suunto/Apple Health; Garmin Training Readiness composes HRV + sleep + load + stress; Oura has the canonical readiness score.
**What VeloMate has today:** `VELOMATE_RESTING_HR` as a static configuration value feeding TRIMP calculation. No longitudinal wellness data of any kind.

**What to build:**
- **Schema:** New `wellness_daily` table keyed by date. Columns: `rhr`, `hrv_rmssd`, `sleep_hours`, `sleep_quality` (1–10), `soreness` (1–10), `stress` (1–10), `mood` (1–10), `weight_kg`, `rpe_previous_day` (1–10), `notes`. Nullable everything.
- **Entry path:** Two input methods:
  1. **Manual CLI command** — `velomate wellness today --rhr 48 --sleep 7.5 --quality 8 --soreness 3 --notes "legs heavy"`. Fast, scriptable, works without a UI.
  2. **HRV ingestion (phase 2)**: HealthFit (Apple Health) is the broadest coverage. Add a simple CSV import path first (`velomate wellness import healthfit.csv`), then direct connectors later if demand appears.
- **Readiness score:** Daily composite with explicit components:
  - HRV z-score vs 14-day baseline (×0.4)
  - Sleep score (hours × quality, z-score vs 14-day) (×0.3)
  - Load balance (TSB normalised to [−1, +1]) (×0.2)
  - Soreness + stress (inverted, z-score) (×0.1)
  - Output: 0–100 score, plus a label (Ready / OK / Take It Easy / Rest).
- **Dashboards:** New **Wellness** dashboard or a Wellness row on Overview:
  - Readiness score today (large stat, colored)
  - HRV trend (rMSSD 7-day and 14-day baselines, with today's value)
  - RHR trend
  - Sleep hours stacked bar (quality gradient color)
  - Soreness/stress sparkline
  - Readiness calendar heatmap (past 90 days)

**Why it matters for our user:**
This transforms VeloMate from a backward-looking ride logger into a forward-looking training aid. Today the user looks at yesterday's NP; tomorrow they look at today's readiness and decide whether to ride hard. Wellness data also unlocks #8 (daily recommendation) — without wellness, "ride today" is just TSB-based, which is what `cmd_recommend` already does. With wellness, it's personalised.

**Risks / considerations:**
- **Discipline problem**: wellness diaries are only useful if populated daily. Expectation: the user will have gaps. Design the readiness score to degrade gracefully — missing HRV should not break it, just reduce confidence.
- **HRV is sensitive** — RMSSD can swing wildly day-to-day from noise. The baseline comparison (today vs 7d trailing average) is what matters, not absolute values.
- **Scope creep risk**: don't add menstrual tracking, hydration, blood pressure, glucose etc. in phase 1. Ship RHR + HRV + sleep + soreness + notes. Add fields on demand.

---

### 3. CP/W' model (Monod-Scherrer + Morton fits)

**Dimension:** Performance Modeling
**Leverage / Effort / Alignment:** High / Medium / Natural
**Reference implementations:** GoldenCheetah fits Monod-Scherrer, Morton 3-parameter, and Veloclinic; intervals.icu fits Monod and Morton and displays W', pMax, MAP; WKO5's Power-Duration Model V2 produces mFTP, TTE, FRC, Pmax, Stamina from a proprietary fit.
**What VeloMate has today:** FTP as a single number from rolling 90-day best 20-min × 0.95. Per-ride FTP backfill preserves historical TSS/IF accuracy. Power-duration curve displayed on All Time Progression and Activity Details.

**What to build:**
- **New module** `ingestor/critical_power.py` with two fit functions:
  - `fit_monod_scherrer(durations, powers)` → returns `(cp, w_prime)` from the 2-parameter hyperbolic model: `P = W'/t + CP`
  - `fit_morton(durations, powers)` → returns `(cp, w_prime, p_max)` from the 3-parameter model
- **Input:** For each fit period (30/60/90 days), extract mean maximal power at standard durations (1s, 5s, 15s, 30s, 1m, 2m, 5m, 10m, 20m, 40m, 60m, 90m) across all rides in the period.
- **Storage:** New `cp_estimates` table (date, period_days, cp_watts, w_prime_kj, p_max_watts, model_type, r_squared). Computed on the same cadence as the daily 00:05 fitness recalc.
- **Dashboard:** New section on All Time Progression:
  - "CP / W' Progression" — CP and W' trend lines over time
  - "Power-Duration Model" — scatter of mean-maximal points + fitted curve overlay + CP asymptote line
  - "Athlete Type" — stat card classifying from W'/CP ratio (sprinter > 25 J/W, rouleur 15–25, TT < 15)
- **Use it everywhere:** Optionally replace the `ride_ftp` rolling 90-day estimate with the modeled CP as the authoritative FTP. Keep the rolling 20-min calculation as a sanity check.

**Why it matters for our user:**
FTP-as-one-number conflates two physiologically distinct properties: sustainable aerobic power (CP) and anaerobic reservoir (W'). Two riders with the same FTP can have wildly different W' — the sprinter with 300W FTP and 25 kJ W' is a different athlete from the TT'er with 300W FTP and 12 kJ W'. Interval design should target CP for aerobic work and W' for anaerobic; without the split, the user is guessing. This is the single biggest upgrade to performance modeling available.

**Risks / considerations:**
- **Data quality:** CP fits need quality maximal efforts at multiple durations. If the user only ever does Zone 2, the fit will be garbage. Add an R² diagnostic and warn when < 0.9.
- **Monod vs Morton:** Monod is the classical 2-parameter model; Morton 3-parameter adds Pmax and fits short-duration data better. Offer both and let the user compare.
- **Integration with existing FTP logic:** Decide whether CP becomes the new per-ride FTP or lives alongside. Recommended: store both, default TSS to CP where available, fall back to ride_ftp otherwise.
- **Scipy dependency:** `scipy.optimize.curve_fit` is the obvious tool; adds scipy to `requirements.txt`. Acceptable.

---

### 4. W'bal time series per ride

**Dimension:** Performance Modeling
**Leverage / Effort / Alignment:** High / Medium / Natural (depends on #3)
**Reference implementations:** GoldenCheetah computes Skiba differential W'bal; intervals.icu shows it on the activity chart; Xert's MPA (Maximal Power Available) is an equivalent real-time "tank" visualisation.
**What VeloMate has today:** Nothing. Power stream exists but no W'bal computation.

**What to build:**
- **Depends on #3** (need CP and W' from the fit).
- **Compute per ride** in `ingestor/fitness.py`: Skiba differential model:
  - Above CP: `dW'bal/dt = CP − P` (depleting)
  - Below CP: refill exponentially toward W' with the Skiba tau parameter
  - Starts each ride at `W'bal = W'`
- **Storage:** New column on `activity_streams`: `w_bal_joules`. Computed on stream insert; backfill with METRICS_VERSION bump.
- **Dashboard:** Add panel on Activity Details:
  - "W'bal" — timeseries alongside power, shaded red when below 25% of W'
  - Stat card: "Lowest W'bal during ride" and "% time W'bal below 25%"

**Why it matters for our user:**
The user does a hard VO2 workout, blows up on the 5th interval, and wonders why. W'bal answers it precisely: "your anaerobic reservoir was at 2 kJ when you started the 5th interval, you needed 8 kJ to complete it at target power". It transforms the post-ride post-mortem from guessing to diagnosing. And for future rides, the user can pace intervals by W'bal instead of by feel.

**Risks / considerations:**
- Only meaningful with power data (power stream present). Rides without power should skip the calculation.
- The Skiba differential formula uses a tau parameter (W' recovery time constant); GoldenCheetah uses a specific empirical form. Use the GoldenCheetah formulation for consistency with the reference implementation.
- Large storage implication: per-second W'bal on every activity_stream row. Existing `activity_streams` schema already has per-second data, so this is a column addition not a row explosion.

---

### 5. Fresh vs fatigued power-duration curves

**Dimension:** Performance Modeling
**Leverage / Effort / Alignment:** High / Medium / Natural
**Reference implementations:** GoldenCheetah has fresh-vs-fatigued PD curves as a Season Chart overlay; intervals.icu splits the PD curve by CTL; WKO5 shows PD curves filtered by condition.
**What VeloMate has today:** A single PD curve (mean maximal power across all rides) on All Time Progression.

**What to build:**
- Dashboard query that segments best efforts by the CTL on the ride date:
  - "Fresh" = rides where CTL < (current CTL − 10)
  - "Fatigued" = rides where CTL > (current CTL − 5)
- Render two overlaid PD curves on a new panel: "Fresh vs Fatigued Power-Duration".
- Add a derived metric: **Durability Index** = fatigued_20min_power / fresh_20min_power. Stat card on All Time Progression. Values below 0.90 indicate a durability deficit; above 0.95 indicates good durability.
- Optional: do the same for other durations (5min, 60min) in a small table.

**Why it matters for our user:**
An amateur's biggest hidden weakness is usually that their peak power drops disproportionately when fatigued. They test their FTP fresh on a Saturday morning, but their real races/events happen after 1000 kJ of climbing, when their functional 20-min power is 15% lower. This panel reveals that gap and motivates training-through-fatigue work (long rides with hard efforts in the back half).

**Risks / considerations:**
- Pure Grafana SQL implementation — no ingestor changes needed, no new columns. This is low-risk but high-signal.
- Needs enough ride volume to have both fresh and fatigued buckets populated. Show a warning if either bucket has < 10 rides.
- "CTL on ride date" is already derivable from `athlete_stats` joined by date.

---

### 6. Auto interval detection

**Dimension:** Ride Analytics Depth
**Leverage / Effort / Alignment:** High / Medium / Natural
**Reference implementations:** intervals.icu does this automatically on every ride; GoldenCheetah has an interval finder; Sauce for Strava has on-demand segment detection.
**What VeloMate has today:** Zones by km (distance-bucketed) and per-km splits table. No effort-based interval detection.

**What to build:**
- **Module:** `ingestor/interval_detection.py` with a detector that walks the power stream (or HR stream fallback) and identifies contiguous regions where power > (configurable threshold × FTP) sustained for > 30 seconds, separated by ≥ 10-second recovery periods.
- **Classification:** Each detected interval gets classified by duration and intensity:
  - Sprint: < 30s at > 150% FTP
  - Anaerobic: 30s–2min at 120–150% FTP
  - VO2max: 2–5min at 105–120% FTP
  - Threshold: 5–20min at 95–105% FTP
  - Sweet spot: 5–30min at 85–95% FTP
  - Tempo: > 20min at 75–85% FTP
- **Storage:** New `ride_intervals` table keyed by `activity_id`, with start_offset, duration, avg_power, np, classification, w_prime_used.
- **Dashboards:**
  - Activity Details: new "Auto-Detected Intervals" table with per-interval stats, plus a band overlay on the power/HR timeseries highlighting each interval.
  - All Time Progression: "Interval Distribution by Type" stacked bar chart per month — shows whether the user is actually doing the polarised mix they think they are.
- **Use it to drive #9:** Training Monotony & Strain can include "interval quality" as a secondary signal — rides with detected hard intervals count differently than steady rides of the same TSS.

**Why it matters for our user:**
Today the user looks at a ride summary ("95 TSS, 220W avg, IF 0.74") and has no idea whether they did the intervals they planned. Did they actually hit four 8-min threshold intervals? Or was it a spirited group ride? Auto-detection answers precisely and builds a season-long picture of *what kind* of training was actually performed — which is often very different from what was planned.

**Risks / considerations:**
- Detection thresholds need tuning. Ship with defaults + make them configurable via `VELOMATE_INTERVAL_MIN_DURATION`, `VELOMATE_INTERVAL_MIN_INTENSITY`.
- Group-ride surges (30s at 400W) should be detected but not over-represented; tune the minimum duration carefully.
- Must work on rides without power (HR-based fallback using zones). Lower signal quality but better than nothing.

---

### 7. Equipment tracking with component mileage & wear alerts

**Dimension:** Equipment, Records & Competition
**Leverage / Effort / Alignment:** High / Medium / Adjacent
**Reference implementations:** Strava's "My Gear" with bikes + component mileage (chain/tire/cassette/BB/chainring/cable) + replacement reminders (free tier); intervals.icu has the same; Garmin Connect has it; dedicated apps like ProBikeGarage exist for this alone.
**What VeloMate has today:** Nothing. `activities.device` field exists but no bike model, no components.

**What to build:**
- **Schema:**
  - `bikes` table: id, name, description, is_active, retired_at
  - `components` table: id, bike_id, component_type (chain/tire_front/tire_rear/cassette/chainring/bb/cables/brake_pads/bar_tape), brand, model, installed_at, installed_at_mileage_km, retired_at, retired_at_mileage_km, expected_life_km, notes
  - `activities.bike_id` foreign key (nullable)
- **Attribution:** Auto-attribute rides to bikes using heuristics on `activities.device` and `activities.trainer` and `activities.sport_type`:
  - Trainer=true + device contains "KICKR/Tacx" → indoor bike
  - `sport_type` = zwift → indoor bike
  - Default outdoor → primary outdoor bike
  - Manual override via CLI (`velomate activity set-bike <id> <bike>`)
- **CLI commands:**
  - `velomate bike list`
  - `velomate bike add "Road - Canyon Aeroad"`
  - `velomate component add --bike 1 --type chain --model "Shimano CN-HG901" --expected-life 5000`
  - `velomate component replace 7 --installed-at-km $(current)` (retires old, installs new)
- **Dashboard:** New **Equipment** dashboard:
  - Bikes table with per-bike totals
  - Components table with current mileage, % of expected life, color coded
  - "Due Soon" list
- **Alerts:** Daily job checks if any component is > 90% of expected_life. Writes a warning to Grafana annotation or stdout.

**Why it matters for our user:**
Chains worn past 0.75% stretch damage cassettes, which is a 10× cost compared to replacing a chain on time. Tires at end of life are a safety issue. Tracking is the kind of thing every serious amateur means to do and nobody actually does without tooling. This is high-perceived-value per dollar of effort.

**Risks / considerations:**
- Retroactive data entry pain: initial setup requires the user to install current components with a guessed start date. Provide a CLI import or JSON seed option.
- Attribution heuristics will get things wrong. Design for manual override.
- Storage footprint is trivial; no performance concerns.

---

### 8. Daily "ride today" recommendation with form awareness

**Dimension:** Structured Training (extending existing)
**Leverage / Effort / Alignment:** High / Medium / Natural
**Reference implementations:** Xert XATA (daily workout recommended from fitness signature + availability), TrainerRoad TrainNow (three options based on freshness), Garmin Daily Suggested Workout (HR-based), intervals.icu's suggested ride based on plan.
**What VeloMate has today:** `cmd_recommend` — weekly recommendation combining fitness state + weather + past routes. This is the *existing foundation* and the closest VeloMate comes to adaptive training. No daily counterpart.

**What to build:**
- **New CLI command:** `velomate today` — single-ride recommendation for today, combining:
  1. **Current TSB state:** Fresh (> +10) → recommend hard; Neutral (−10 to +10) → recommend moderate; Fatigued (< −10) → recommend easy or rest
  2. **Wellness (if #2 is built):** Readiness score modulates the TSB recommendation — low readiness → recommend lighter than TSB would suggest
  3. **Form-zone auto-annotation:** Label today's state as "peak" / "productive" / "neutral" / "overreaching"
  4. **Weather + daylight** (reuse existing weather and sunrise/sunset intelligence)
  5. **Specific ride prescription:**
     - "Ride 2h Zone 2 (target HR 130–145 bpm, target power 140–170 W)"
     - "Do 4×8 min at threshold (210–220W), 3 min recovery, total ride 75 min"
     - "Rest day — skip today, tomorrow's TSB will be +5"
- **Output format:** Same WhatsApp-friendly markdown as existing `cmd_recommend`. Short, actionable, phone-readable.
- **Dashboard equivalent:** A "Today" row on Overview with the same recommendation as a stat card + colour.

**Why it matters for our user:**
The user's most frequent question isn't "what was my average power last month" — it's "should I ride hard today, or should I take it easy". That's the daily decision that training revolves around, and no static dashboard can answer it. A thoughtful daily recommendation driven by TSB + (eventually) readiness + weather is what turns VeloMate from a data archive into a training partner. It also extends work the user has already done — the `cmd_recommend` weekly command — without starting from scratch.

**Risks / considerations:**
- **Before #2 is built**, this is TSB-only, which is good but limited. Ship this phase 1, enhance with readiness in phase 2.
- Don't over-prescribe — the recommendation should be advisory, not dictatorial. Phrase it as "suggested" and always offer an alternative.
- The prescription needs calibrated power/HR targets — reuse existing zone calculations.

---

## Secondary Gaps

- **Training Monotony & Strain (Foster)** — daily TSS stddev + monotony + strain metric on athlete_stats; red warning on All Time Progression when monotony > 2.0. S effort, M leverage.
- **Climb categorisation (HC/Cat 1–4)** — detect climbs in GPS elevation data using the length × average grade formula; show on Activity Details route map and in a climbs table. S effort, M leverage.
- **eFTP auto-update from single maximal efforts** — detect maximal 5-min, 8-min, 20-min efforts and update FTP immediately (not just 90-day rolling). Small change to ingestor FTP logic. S effort, M leverage.
- **OAuth browser flow for Strava** (`velomate auth`) — known feedback item. Replace manual curl multi-step with a one-command browser OAuth flow. S effort, M leverage.
- **Direct FIT file import** — CLI command to ingest a .fit file directly, bypass Strava for rides done offline or on non-Strava platforms. M effort, M leverage, adjacent.
- **PR notifications + durability PRs** — daily check for new 1s/5s/1m/5m/20m/60m peaks; separately track best 5-min power after ≥ 1000 kJ (durability PR). Write to log + optional webhook. S effort, M leverage.
- **VO2max from HR + power** — Firstbeat-style estimate using max HR + 20-min power. Populates the existing unused `athlete_stats.vo2max` column. M effort, M leverage.
- **User-defined computed fields** — a YAML config where users can define `my_metric: NP / rhr` and have it automatically computed per ride. M effort, M leverage, adjacent.
- **Athlete type classification** — after CP/W' exists, classify rider as sprinter/puncheur/rouleur/TT'er from W'/CP ratio and Pmax. S effort, L leverage, nearly free.
- **Strava Webhook subscriptions** — push-based sync instead of 10-min polling. M effort, L leverage (optimization, not capability).
- **Heat-adjusted TSS** — adjust training load for temperature above 25°C. M effort, L leverage.
- **Year-over-year self-compare on route segments** — "this week vs same week last year" on favourite routes. S effort, L leverage (All Time Progression already has YoY distance).

## Non-Gaps Considered

Evaluated against leading platforms and explicitly **rejected** for VeloMate:

- **Segment leaderboards / KOMs** — social competition. VeloMate is single-user; no network effect.
- **Strava-style social feed, kudos, clubs, group rides** — all social. Out of scope by design.
- **Multi-sport unified load (running + swimming + cycling)** — cycling-only is an explicit project scope in `CLAUDE.md`.
- **Coach-athlete sharing workflows, workout comments, compliance scoring for a third party** — the user is not a coach.
- **Aerolab CdA/Crr aero testing** (GoldenCheetah) — too niche; only useful for time trialists with a dedicated aero field test.
- **Pedal analytics: left/right balance, torque effectiveness, pedal smoothness** — requires specific hardware (Garmin Vector, Favero Assioma, etc.) that most amateurs don't have; low signal-to-noise.
- **Quadrant analysis (force × cadence scatter)** — analytically deep but low decision leverage for amateurs. A coach might use it to plan cadence work, but a self-directed rider won't change behaviour from looking at it.
- **TrainingPeaks-style Annual Training Plan (ATP) with A/B/C races and periodised phases** — too coach-centric; dimensional cost is high for a self-directed amateur. Daily "ride today" (gap #8) covers the same decision space with far less ceremony.
- **4DP-style full frontal testing (Wahoo SystM)** — requires a specific protocol test; one-off friction. CP/W' fit from existing data achieves similar without a test.
- **Strava Premium Live Segments on head unit** — requires device integration VeloMate can't provide.
- **Full workout builder with FIT/ZWO/ERG export** — large structural build for unclear per-user value. Most self-directed amateurs follow YouTube or Sufferfest plans, not author their own. Revisit only if several users explicitly ask.
- **Multi-bike component tracking with full drivetrain modelling** (beyond basic equipment tracking in gap #7) — diminishing returns after the basic version.
- **Gamification / achievements / badges** — fun but not decision-changing.
- **Menstrual cycle tracking, hydration tracking, glucose integration** — extreme feature creep for wellness phase 1.

## What's Already Good

VeloMate has several strengths that should be protected — and in most cases, leveraged — when adding new features:

- **Route intelligence is class-leading.** The 10-source planner (Valhalla + OSM POIs + Strava segments + Komoot + ride history + surface + cycling infrastructure + weather + air quality + daylight) is not matched by any other self-hosted tool. This is VeloMate's distinctive moat. Preserve and extend it; don't dilute attention with parallel work that competes with it for effort.
- **Per-ride FTP backfill discipline** — historical TSS/IF accuracy is preserved via `activities.ride_ftp` + 90-day rolling rebalancing. Most tools use current FTP everywhere and produce wrong historical TSS. VeloMate gets this right.
- **METRICS_VERSION recalculation gate** — a disciplined pattern for evolving derived metrics without painful migrations. Reusable for future metric additions.
- **Stored metrics as single source of truth** — the ingestor does all heavy lifting; Grafana reads, never recomputes. Clean separation that makes it easy to add new metrics without touching dashboard queries.
- **Cross-device deduplication with richness scoring** — uncommon and genuinely useful for anyone running a Karoo + Apple Watch setup.
- **Three well-structured dashboards with 122 panels** — the presentation layer is already highly polished. New features can be slotted in without a redesign.
- **Cycling-only scope discipline** — stated explicitly in `CLAUDE.md` and enforced at ingest (Ride/VirtualRide/EBikeRide filter). Prevents feature creep into swim/run/strength territory.
- **Banister TRIMP with HRR capping** — the HRR cap at 1.0 prevents the common blowup when HR exceeds configured max. Small but correct.
- **Weekly recommendation (`cmd_recommend`)** — existing feature that's a perfect foundation for gap #8 (daily "ride today"). Don't start from scratch; extend it.
