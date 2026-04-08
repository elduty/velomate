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
- **Format: `<marker> <range> — <label>`.** Optional emoji bullet, then the threshold or range expression, em-dash (`—`, U+2014), then the human-readable label.
- **Lead with context.** Put a one-line definition or formula first, then a blank line, then the range list, then any closing note. Don't lead with the ranges.
- **Use `—` (em-dash), not `-` (hyphen)**, as the separator between threshold and label. The hyphen reads as a minus sign in numeric contexts.
- **Use `–` (en-dash) or `to`** for numeric ranges inside a bucket label (e.g. `30s – 2 min`, `5-10%`). Either is fine; be consistent within a single description.

**Reference panels** (already compliant — use as templates when adding new panels):
- `overview.json` → Form (TSB) id 222, TSS id 6, Days Since Ride id 225
- `activity.json` → Intensity Factor id 35, Variability Index id 36, Power Zones id 32

**Example — good:**

```
Training Stress Balance (Form)
CTL minus ATL — are you fresh or fatigued?

🔴 < -10 — Overreached, need rest

🟠 -10 to 0 — Tired, normal training fatigue

🟢 0 to 15 — Optimal, good balance

🔵 > 15 — Fresh, race-ready (fitness may be declining)
```

**Example — bad (pre-PR #98 state):**

```
Aerobic Decoupling (Friel)
Power:HR ratio drift — first half vs second half.

Positive = cardiac drift (HR rising relative to power).
< 5% — Good aerobic fitness
5-10% — Moderate drift
> 10% — Significant drift, base fitness needs work
```

Ranges jammed onto consecutive lines with no blank separators render as one squished paragraph in the tooltip. Always insert a blank line between each range.

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
- **Grafana reads stored NP/EF/IF/VI/TRIMP** from activities table; stream-level SQL only for historical charts (FTP Progression, Best Efforts, Power Duration Curve)
- **FTP in Grafana**: All panels use standardised fallback: configured_ftp → estimated_ftp → 150

## Database

- Host: 10.7.40.15, Port: 5423, DB: velomate, User: velomate
- Config file: ~/.config/velomate/config.yaml
- Key tables: activities, activity_streams, athlete_stats, sync_state

## Memory

Memory files in `.claude/memory/` are portable project context tracked on Gitea.
On a new environment, run `link-claude-memory <repo-path>` to symlink them into Claude Code's auto-memory.
