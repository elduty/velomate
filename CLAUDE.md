# VeloMate — Project Instructions

## Philosophy

Do not over-engineer. This project values a good balance between iteration speed and functionality. Ship working code, fix real bugs, skip marginal improvements.

## PR Reviews

When analysing Raven review findings, apply judgement:
- Fix findings that catch real bugs or security issues (e.g., files leaking to GitHub, wrong calculations)
- Skip findings that are theoretical, premature optimisations, or diminishing-return polish
- Carried/repeated findings that have already been assessed don't need re-evaluation every cycle
- If a review is stabilised (same carried findings, no new real issues), recommend merging — don't chase zero findings

## Workflow

- All changes go through PRs on Gitea, reviewed by Raven bot — never push directly to main unless explicitly asked
- Always squash merge PRs for clean history
- Always delete remote+local feature branch after merging
- After creating a PR, follow up on Raven review, address findings, report outcome, clean up
- Never add Co-Authored-By Claude or AI mentions in commits
- Run independent tasks in parallel (agents/worktrees) when possible

## Architecture

- **Stack**: Docker Compose — PostgreSQL 15, Python ingestor, Grafana 12.4
- **Server**: 10.7.40.15 (PostgreSQL on port 5423, Grafana on 3021)
- **Gitea**: gitea.mrmartian.in (primary repo, has Raven review bot)
- **GitHub**: github.com/elduty/velomate (public mirror, no AI evidence)
- **Push to GitHub**: `scripts/push-to-github.sh` handles diverged histories, strips dev-only files

## Key Files

- `ingestor/fitness.py` — Core fitness engine: TSS, NP, EF, CTL/ATL/TSB, FTP estimation, per-ride FTP backfill
- `ingestor/main.py` — Startup, polling, config persistence (FTP/HR/Resting HR)
- `ingestor/db.py` — Schema DDL, upserts, dedup, sync_state
- `ingestor/strava.py` — Strava API client, token management
- `grafana/dashboards/*.json` — Three dashboards: activity, overview, all-time-progression
  - Activity Details has both Zone charts (5-6 standard buckets) and Distribution histograms (full granular shape) — these are not redundant, keep both

## Dashboard Conventions

### Panel tooltip formatting

Panel `description` fields are the hover tooltips users see in Grafana. Follow this format whenever a description contains numeric ranges or thresholds:

- **One range per line.** Never jam multiple ranges onto one line with commas or a compressed sentence. Each threshold / bucket gets its own line.
- **Blank line separator between ranges.** The colored-bullet panels (Form, TSS, Intensity Factor, Power Zones, etc.) use double-newlines between entries because Grafana's Markdown renderer collapses single newlines — blank lines force line breaks.
- **Format: `<emoji> <range> — <label>`.** Emoji bullet (see palette below), then the threshold or range expression, em-dash (`—`, U+2014), then the human-readable label.
- **Lead with context.** Put a one-line definition or formula first, then a blank line, then the range list, then any closing note. Don't lead with the ranges.
- **Use `—` (em-dash), not `-` (hyphen)**, as the separator between threshold and label. The hyphen reads as a minus sign in numeric contexts.
- **Use `–` (en-dash) or `to`** for numeric ranges inside a bucket label (e.g. `30s – 2 min`, `5-10%`). Either is fine; be consistent within a single description.

### Tooltip color icons MUST match the panel's actual chart colors

**Rule:** whenever a panel is visually color-coded (threshold steps, zone color overrides, cell color mappings), the tooltip description MUST include an emoji bullet on each range that matches the actual color shown on the chart. Users should be able to hover a red bar on a chart and find a matching 🔴 emoji in the tooltip explaining what red means.

This applies to:
- Stat panels with `thresholds` color steps (Form TSB, Days Since Ride, TRIMP, Aerobic Decoupling, IF, VI, etc.)
- Bar/stacked charts with per-series `color` overrides (HR Zones, Power Zones, Monthly Zone Distribution, etc.)
- Table panels with `color-background` cell `mappings` (Detected Intervals)

**Unified emoji palette** — matches the actual chart hex colors. Use these when adding new color-coded panels or editing existing ones:

| Emoji | Chart color | Generic meaning | Used for |
|---|---|---|---|
| 🔘 | grey `#808080` | Baseline / recovery / easy floor | Power Z1, HR Z1, TRIMP < 50 |
| 🔵 | blue `#3498db` | Easy endurance / fresh | Power Z2, HR Z2, TRIMP 50-75, Detected Intervals tempo, Form (TSB) "fresh" |
| 🟢 | green `#2ecc71` | Good / optimal / moderate | Power Z3, HR Z3, TRIMP 75-100, Detected Intervals sweetspot, Aerobic Decoupling < 5%, VI normal, Form (TSB) optimal, Days Since Ride active |
| 🟡 | yellow `#f1c40f` | Moderate warning / threshold | Power Z4, HR Z4, TRIMP 100-125, Detected Intervals threshold, Aerobic Decoupling 5-10% |
| 🟠 | orange `#e67e22` | Warning / hard | **Power Z5 (7-zone)**, TRIMP 125-150, Detected Intervals vo2, Days Since Ride extended |
| 🔴 | red `#e74c3c` | Very hard / danger / top of compressed palettes | **Power Z6 (7-zone), HR Z5 (5-zone max)**, TRIMP > 150, Detected Intervals anaerobic, Aerobic Decoupling > 10%, Form (TSB) overreached |
| 🟣 | purple `#9333ea` | Maximum / sprint / supramaximal | **Power Z7 (7-zone)**, Detected Intervals sprint |

**Compressed palettes**: Power Zones is a **7-zone** system that uses the full palette. HR Zones is a **5-zone** system that compresses to 5 colours, truncating at red — so HR Z5 (the top zone, VO2max) uses 🔴 red, **not** 🟠 orange, even though both are called "VO2max". The chart defines the color for each panel; the "Generic meaning" column above is descriptive of where each emoji gets used across the codebase, not a prescriptive Z-number mapping.

TRIMP, Form (TSB), Days Since Ride, Aerobic Decoupling, VI, and other non-zone metrics have their own threshold-based palettes (2-6 colors) that select a subset of this unified palette based on the panel's own threshold count. Always defer to the panel's chart colors as the source of truth.

Single-direction deltas (e.g. `Δ Avg Decoupling`, `6w Fitness Δ`) are fine without emojis — they have a threshold but not a "range" per se. The rule applies to panels with ≥2 named range buckets.

**Reference panels** — use as templates when adding new panels:
- `overview.json` → Form (TSB) id 222, Days Since Ride id 225
- `activity.json` → Intensity Factor id 35, Variability Index id 36, Power Zones id 32, TRIMP id 43, Aerobic Decoupling id 411, HR Zones id 31, Detected Intervals id 1100

**Example — good:**

```
Time in each power zone.

Zones based on FTP, estimated from best 20-min power × 0.95 or configured via VELOMATE_FTP.

🔘 Z1 < 55% — Recovery

🔵 Z2 55-75% — Endurance

🟢 Z3 75-90% — Tempo

🟡 Z4 90-105% — Threshold

🟠 Z5 105-120% — VO2max

🔴 Z6 120-150% — Anaerobic

🟣 Z7 > 150% — Neuromuscular
```

**Example — bad:**

```
Time in each power zone.

Z1 < 55% — Recovery
Z2 55-75% — Endurance
...
Z7 > 150% — Neuromuscular
```

Two problems: ranges jammed with no blank-line separators (renders as one paragraph) AND no color icons (user can't connect chart colors to description).

**When the panel's palette is wrong:** if you find a panel whose color icons in the tooltip don't match the chart colors (e.g. tooltip uses ⚡ but chart uses purple), fix the tooltip to match the chart, not the other way around. The chart colors are the source of truth because users see them continuously; the tooltip is a hover aid.

This rule applies whenever you add, edit, or migrate panel descriptions. Run `python3 -m pytest tests/test_dashboards.py -q` after any dashboard JSON change.

## Metrics (Validated)

All cycling metrics follow industry standards. The ingestor is the single source of truth — Grafana reads stored values from the activities table.
- **TSS**: Coggan formula — `(duration × P × IF) / (FTP × 3600) × 100` where P is **VI-aware**: NP when VI ≤ 1.30 (standard Coggan), avg_power when VI > 1.30 (urban stop-and-go rides where NP's 4th-power weighting overestimates sustained load). Threshold constant `HIGH_VI_THRESHOLD` in `fitness.py`
- **NP**: 30-second SMA (circular buffer) → 4th power → mean → 4th root (Coggan standard, matches GoldenCheetah). Computed in Python. Includes zero-power (coasting). Always stored; whether it drives TSS depends on VI
- **FTP**: Rolling 90-day best 20-min power × 0.95. Per-ride FTP stored in `activities.ride_ftp`. Algorithmic estimate always stored in `sync_state.estimated_ftp` for diagnostic display even when `VELOMATE_FTP` is configured
- **IF**: Computed from the SAME power used for TSS (NP or avg_power depending on VI) divided by ride_ftp, so `TSS ≈ duration_h × IF² × 100` holds. Stored in `activities.intensity_factor`
- **VI**: NP / avg_power. Stored in `activities.variability_index`
- **TRIMP**: Banister exponential formula (male: k=0.64, c=1.92), HRR capped at 1.0. Stored in `activities.trimp`
- **CTL/ATL/TSB**: Exponential moving averages (42/7 day constants)
- **EF**: NP / avg_hr
- **Decoupling**: `first_EF / second_EF - 1` (positive = drift, per Friel/TrainingPeaks). Includes coasting samples.
- **W/kg**: NP / ride_weight. Uses NP (not avg_power) for physiological accuracy. Per-ride `ride_weight` from `VELOMATE_WEIGHT`, preserved on weight change
- **CP / W'**: Critical Power and W' (anaerobic work capacity) modeled via Monod-Scherrer 2-parameter fit (`P = W'/t + CP`) on mean maximal power at 5 standard durations (60s/120s/300s/600s/1200s). Stored daily in `cp_estimates`. Quality gate: R² >= 0.9 AND >= 4 of 5 durations contributing. Graceful fallback to rolling 20-min x 0.95 when the gate fails. Replaces the rolling 20-min calculation as the source of `sync_state.estimated_ftp`
- **HR Zones**: Max HR percentages (60/70/80/90%), default fallback 185 bpm
- **Power Zones**: Coggan 7-zone including Z7 Neuromuscular (>150% FTP)

### GoldenCheetah comparison

When validating VeloMate metrics against GoldenCheetah, know which model GC uses for each value — they're not interchangeable:
- **IsoPower** — 30-second SMA, used for Coggan TSS. This is what VeloMate stores as `np`.
- **xPower** — 25-second EWMA, used for GC's VI, EF, BikeIntensity, BikeScore. VeloMate does *not* compute xPower — our VI/EF/IF all use NP (IsoPower).
- **CP Estimate** — Critical Power model, independent of NP. Don't confuse it with NP when reading a GC ride report.

Direct one-to-one comparison of VI, EF, and IF between VeloMate and GoldenCheetah will therefore differ slightly — VeloMate's values are NP-derived, GC's are xPower-derived. TSS and NP values should match.

## Important Design Decisions

- **METRICS_VERSION** (currently "10"): Bumping triggers full recalculation + FTP backfill on next startup
- **estimated_ftp** persisted to sync_state — Grafana reads pre-computed FTP instead of recalculating
- **Resting HR** included in config change detection — changing it triggers TRIMP recalculation
- **Per-ride FTP**: Historical rides preserve their TSS and IF via `ride_ftp` column + backfill from 90-day rolling best
- **Per-ride weight**: `ride_weight` column stores configured weight at time of processing. Unlike `ride_ftp`, weight changes do NOT reset historical rides — old rides keep their stamped weight, only new rides get the new value. Weight is intentionally excluded from the METRICS_VERSION reset because it's user-configured, not derived
- **CP/W' modeling**: Monod-Scherrer linear fit via `numpy.polyfit` (no scipy dependency). Quality gate (R² >= 0.9 AND >= 4/5 durations) with 90d -> 180d -> existing `estimate_ftp()` fallback. CP replaces only the auto-estimate path — `VELOMATE_FTP` (when configured) still wins for TSS calculation. Pure functions in `ingestor/critical_power.py`, DB-touching helpers in `ingestor/fitness.py`. Physiological sanity check rejects fits with CP <= 0 or W' <= 0
- **Grafana reads stored NP/EF/IF/VI/TRIMP** from activities table; stream-level SQL only for historical charts (FTP Progression, Best Efforts, Power Duration Curve)
- **FTP in Grafana**: All panels use standardised fallback: configured_ftp → estimated_ftp → 150

## Database

- Host: 10.7.40.15, Port: 5423, DB: velomate, User: velomate
- Config file: ~/.config/velomate/config.yaml
- Key tables: activities, activity_streams, athlete_stats, sync_state

## Memory

Memory files in `.claude/memory/` are portable project context tracked on Gitea.
On a new environment, run `link-claude-memory <repo-path>` to symlink them into Claude Code's auto-memory.
