# VeloMate v1.2.0

Plan point-to-point routes with a new `--destination` flag. All location flags now accept both place names and coordinates.

## What's New

- **`--destination` flag** — plan routes to a named place or coordinates (`--destination Cascais` or `--destination "38.69,-9.42"`)
- **Unified location parsing** — `--start`, `--waypoints`, and `--destination` all accept place names and `lat,lng`
- **Corridor waypoints** — when destination + distance is set and the direct route is shorter than target, smart waypoints pad the distance via scenic detours
- **There-and-back routing** — `--destination Cascais --loop` routes to the destination and back home
- **Coordinate bounds validation** — rejects out-of-range lat/lng before they reach Valhalla

## Breaking Changes

- **Waypoints separator** changed from comma to semicolon: `--waypoints "Cascais;Estoril"` (commas reserved for coordinate notation)

## Other Changes

- `--duration`/`--distance` now optional when `--destination` is set
- `--loop` auto-disables with `--destination` (override with explicit `--loop`)
- Log warnings for flag clashes (baseline exceeds target, explicit waypoints skip padding)
- CI venv pip bootstrap fix for macOS runner
- 370 tests (up from 331)

## Usage

```bash
python3 -m velomate.cli plan --destination Cascais
python3 -m velomate.cli plan --destination Cascais --waypoints "Oeiras;Estoril"
python3 -m velomate.cli plan --destination Cascais --loop
python3 -m velomate.cli plan --destination Cascais --distance 50km
```

---

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
- **Configured FTP overrides estimation** — `VELOMATE_FTP` now applies to all rides directly. Previously it was only a fallback when stream-based estimation returned no result
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
