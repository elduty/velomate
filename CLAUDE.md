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

## Metrics (Validated)

All cycling metrics follow industry standards:
- **TSS**: Coggan formula using NP — `(duration × NP × IF) / (FTP × 3600) × 100`
- **NP**: 30-second rolling average → 4th power → mean → 4th root. Includes zero-power (coasting). Uses ROWS windows (sample-based, not time-based)
- **FTP**: Rolling 90-day best 20-min power × 0.95. Per-ride FTP stored in `activities.ride_ftp`
- **CTL/ATL/TSB**: Exponential moving averages (42/7 day constants)
- **EF**: NP / avg_hr
- **TRIMP**: Banister exponential formula with configurable resting HR
- **Decoupling**: `first_EF / second_EF - 1` (positive = drift, per Friel/TrainingPeaks)
- **HR Zones**: Max HR percentages (60/70/80/90%)
- **Power Zones**: Coggan 7-zone including Z7 Neuromuscular (>150% FTP)

## Important Design Decisions

- **METRICS_VERSION** (currently "4"): Bumping triggers full recalculation + FTP backfill on next startup
- **estimated_ftp** persisted to sync_state — Grafana reads pre-computed FTP instead of recalculating
- **Resting HR** excluded from config change detection — only used by Grafana TRIMP queries
- **Per-ride FTP**: Historical rides preserve their TSS via `ride_ftp` column + backfill from 90-day rolling best
- **Grafana reads stored NP/EF** from activities table where possible; stream-level SQL only for historical charts (FTP Progression, Best Efforts, Power Duration Curve)

## Database

- Host: 10.7.40.15, Port: 5423, DB: velomate, User: velomate
- Config file: ~/.config/velomate/config.yaml
- Key tables: activities, activity_streams, athlete_stats, sync_state

## Memory

Memory files in `.claude/memory/` are portable project context tracked on Gitea.
On a new environment, symlink or copy them to `~/.claude/projects/<project-path>/memory/`.
