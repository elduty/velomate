# VeloMate v1.1.0

All metrics now computed by the ingestor and stored in the database. Grafana reads stored values — no more inline recomputation.

## What's New

- **IF, VI, TRIMP** stored per ride (previously only existed as Grafana SQL)
- **NP** computed in Python using Coggan 30s SMA (matches GoldenCheetah)
- **Per-ride FTP** — historical rides preserve their TSS/IF accuracy
- **Z7 Neuromuscular** (>150% FTP) added to all power zone panels
- **VELOMATE_RESTING_HR** — configure resting HR for TRIMP
- **VELOMATE_RESET_RIDE_FTP=1** — one-shot flag to reset all per-ride FTP values

## Fixes

- TRIMP: HRR capped at 1.0 (no more exponential blowup)
- TSS: uses per-ride FTP, not current global FTP
- Configured FTP stamps all rides directly (no more stream re-estimation)
- Decoupling includes coasting samples
- FTP/HR fallbacks standardised across all Grafana panels
- Config changes trigger automatic recalculation

## Breaking Changes

- **METRICS_VERSION=7** — first startup recalculates all metrics (automatic, may take a minute)
- **Configured FTP overrides estimation** — setting `VELOMATE_FTP` now stamps all rides with that value
- **Resting HR changes reset TRIMP** — previously had no server-side effect

## Upgrade

```bash
git pull
docker compose up -d --build velomate-ingestor
docker logs -f velomate-ingestor  # wait for "Calculated N days of fitness data"
```

Optional `.env` additions:
```bash
VELOMATE_RESTING_HR=60
VELOMATE_FTP=175
```
