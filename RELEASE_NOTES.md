# VeloMate v1.1.0 — Metric Accuracy & Single Source of Truth

## Highlights

All cycling metrics are now computed by the ingestor and stored in the database. Grafana dashboards read stored values instead of recomputing from streams. This eliminates inconsistencies between panels and ensures metrics match GoldenCheetah's Coggan standard.

## New Metrics

- **Intensity Factor (IF)**: NP / FTP, stored per ride using historical per-ride FTP
- **Variability Index (VI)**: NP / avg power, stored per ride
- **TRIMP**: Banister exponential formula computed from per-second HR data, with HRR capped at 1.0 to prevent exponential blowup when HR exceeds configured max

## New Configuration

| Variable | Default | Description |
|----------|---------|-------------|
| `VELOMATE_RESTING_HR` | 50 | Resting heart rate (bpm), used for TRIMP |
| `VELOMATE_RESET_RIDE_FTP` | 0 | Set to `1` to reset all per-ride FTP on next restart (one-shot) |

## Metric Fixes

- **NP**: Computed in Python using 30-second SMA with circular buffer (Coggan standard, matches GoldenCheetah IsoPower). Previously used a SQL window function
- **TSS**: Now uses per-ride FTP (`ride_ftp`) for historical accuracy. Previously all rides used current FTP
- **FTP backfill**: When `VELOMATE_FTP` is configured, all rides get the configured value. Auto-estimate mode (FTP=0) still uses rolling 90-day best 20-min power
- **Decoupling**: Now includes coasting samples (previously filtered `power > 0`, skewing results)
- **Power Zones**: Z7 Neuromuscular (>150% FTP) added to all panels. Previously only the Distribution panel had it

## Grafana Fixes

- All activity-detail panels (NP, EF, IF, VI, TRIMP) read stored values — no more recomputation from streams
- FTP source standardised across all panels: `configured_ftp -> estimated_ftp -> 150`
- HR fallback standardised: all panels fall back to 185 bpm when no data
- Config changes (FTP, max HR, resting HR) trigger automatic metric recalculation

## Infrastructure

- Shared CI: Gitea Actions + GitHub Actions
- Security hardening from technical audit (shell injection, postgres binding, token volume, Grafana auth)
- Push-to-GitHub script handles diverged histories, excludes dev-only files

---

## Breaking Changes

### METRICS_VERSION bump (automatic recalculation)

On first startup after upgrade, `METRICS_VERSION=7` triggers a full recalculation of all derived metrics: NP, EF, VI, IF, TSS, TRIMP, ride_ftp, and CTL/ATL/TSB. This is automatic — no manual action needed. Expect the first startup to take longer than usual depending on the number of activities.

### New database columns

Three columns are added automatically via `ALTER TABLE ... ADD COLUMN IF NOT EXISTS`:
- `activities.intensity_factor` (FLOAT)
- `activities.trimp` (FLOAT)
- `activities.variability_index` (FLOAT)

No manual migration required.

### Resting HR triggers recalculation

Changing `VELOMATE_RESTING_HR` now resets TRIMP and triggers recalculation. Previously resting HR was only used by Grafana queries and had no server-side effect.

### Configured FTP overrides per-ride estimation

When `VELOMATE_FTP` is set to a non-zero value, all rides get that FTP directly — the stream-based 90-day rolling estimation is skipped. Previously the configured FTP was only used as a fallback when the estimation returned no result.

### Grafana panels require stored metrics

The activity-detail panels for NP, EF, IF, VI, and TRIMP now read from the `activities` table. If the ingestor hasn't run yet (empty database), these panels will show "No data" instead of computing from streams.

---

## Migration Guide

### Standard upgrade (Docker Compose)

```bash
# Pull latest code
cd /path/to/velomate
git pull

# Rebuild and restart
docker compose up -d --build velomate-ingestor

# Monitor the recalculation
docker logs -f velomate-ingestor
```

Wait for the logs to show:
```
[fitness] Metrics version changed (N -> 7), recalculating everything...
[fitness] Computing NP/EF/Work...
[fitness] Computed NP/EF/Work for X activities
[fitness] Computing TRIMP...
[fitness] Computed TRIMP for X activities
[fitness] Calculated N days of fitness data
```

### Optional: Configure new env vars

Add to your `.env` file:

```bash
# Resting heart rate for TRIMP calculation (default 50)
VELOMATE_RESTING_HR=60

# If you want to set FTP explicitly (default 0 = auto-estimate)
VELOMATE_FTP=175
```

### Optional: Reset per-ride FTP

If you've changed `VELOMATE_FTP` and want all historical rides to use the new value:

```bash
# Add to .env temporarily
VELOMATE_RESET_RIDE_FTP=1

# Restart
docker compose restart velomate-ingestor

# Remove the flag after restart
# (leaving it set is harmless but re-stamps on every restart)
```

### Grafana

Dashboard JSON files are updated automatically via the volume mount. No manual Grafana action required. If dashboards don't update, restart Grafana:

```bash
docker compose restart velomate-grafana
```
