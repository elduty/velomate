# VeloAI Tech Audit — 2026-03-16

**Auditor:** Nora  
**Repo:** `~/Developer/veloai`  
**Current commit:** `e8202f6` (pulled 2026-03-16 ~21:45)  
**Prior audit:** 2026-03-15 internal audit (71 tests, long findings list)  
**Scope:** Deep project audit — architecture, dashboards, code quality, open findings. All-time-progression board excluded (rework pending).

---

## 1. What Changed Since Last Audit

### Pull summary (`afcff76..e8202f6`) — 9 files, ~3600 insertions, ~3500 deletions

| Category | Changes |
|----------|---------|
| Grafana dashboards | Massive (see §6) |
| `ingestor/main.py` | FTP/HR env vars now persisted to `sync_state` on startup |
| `pyproject.toml` | **New** — project now has proper Python packaging |
| `requirements.txt` | Deps updated |
| `CLAUDE.md` / `README.md` | Docs updated for dashboard redesign |

### Notable changes in detail

**`ingestor/main.py` — FTP/HR persistence (new, 15 lines):**
```python
env_ftp = os.environ.get("VELOAI_FTP", "")
if env_ftp and int(env_ftp) > 0:
    set_sync_state(conn, "configured_ftp", env_ftp)
```
Configured FTP and Max HR from `.env` are now written to `sync_state` on every startup. This allows Grafana dashboard queries to read them without requiring hardcoded values. Clean design — dashboards query `sync_state` table rather than injecting env vars into SQL.

**`pyproject.toml` added:** O16 from prior audit ("CLI can't be pip-installed") is **resolved**. CLI is now pip-installable.

**Dashboard consolidation:**  
- Deleted: `training-log.json`, `weekly-report.json`, `year-in-review.json`  
- Added: `all-time-progression.json`  
- Now 3 dashboards: Overview, Activity Details, All Time Progression

---

## 2. Architecture

```
Any device (Karoo ✅ arrived today) → Strava
                                          ↓
                              veloai-ingestor (Python 3.11)
                              └─ polls every 10 min
                              └─ backfills 12 months on first run
                              └─ dedup (time-window + distance-match)
                              └─ fitness calc (CTL/ATL/TSB EMA)
                                          ↓
                              veloai-postgres (PostgreSQL 15, port 5423)
                              ├─ activities         (ride summaries)
                              ├─ activity_streams   (per-second telemetry)
                              ├─ athlete_stats      (daily CTL/ATL/TSB)
                              ├─ sync_state         (bookmarks + configured FTP/HR)
                              └─ routes             (unused — legacy)
                                          ↓
                              veloai-grafana (Grafana 12.4, port 3021)
                              ├─ Overview dashboard
                              ├─ Activity Details dashboard
                              └─ All Time Progression dashboard (excluded from this audit)

Local machine (Mac mini):
  VeloAI CLI ── DB (read-only) ── route_intelligence.py ── Overpass/Strava/Komoot/Open-Meteo
             └─ route_generator.py ── Valhalla (public instance)
             └─ map_preview.py ── GPX → HTML preview
```

**No changes to architecture since last audit.** Karoo 3 arrived today — first real rides with HR/cadence/power streams incoming. This is the key unlock for Activity Details dashboard.

---

## 3. Open Findings Status (from 2026-03-15 internal audit)

### Previously Open P1 Findings

| ID | Status | Notes |
|----|--------|-------|
| O1 | ⚠️ **Still open** | Stream restoration wiped by `upsert_streams`. Requires API contract change. |
| O2 | ⚠️ **Still open** | `find_duplicate_by_distance` defined but never called. |
| O3 | ⚠️ **Still open** | Stale refresh token if DB write fails. Rare edge case. |
| O4 | ⚠️ **Still open** | Boolean env var can't be set to False (`bool` subclass of `int`). No current boolean envvars affected. |
| O5 | ⚠️ **Still open** | Sunrise/sunset comparison uses local ride_time vs UTC. Timezone offset bug. |
| O6 | ⚠️ **Still open** | Wind bearing uses `atan2(dlng, dlat)` without cos(lat) correction. ~15° error acceptable (45° buckets). |
| O7 | ⚠️ **Still open** | FTP estimation window assumes gapless stream data. Acceptable within partition. |

### Previously Open P2 Findings

| ID | Status | Notes |
|----|--------|-------|
| O8 | ⚠️ Open | No DB startup retry. Docker health checks mitigate. |
| O9 | ⚠️ Open | `upsert_streams` not atomic (autocommit). Crash between DELETE/INSERT loses streams. |
| O10–O11 | ⚠️ Open | O(n²) weekly totals / N+1 TSS updates. Acceptable at current scale. |
| O12 | ⚠️ Open | Hardcoded cos(lat)=0.75 for Strava segments bounding box. Minor. |
| O13–O15 | ⚠️ Open | Long URL elevation API, fixed downsampling, config caching. Low impact. |
| O16 | ✅ **FIXED** | `pyproject.toml` added — CLI is now pip-installable. |
| O17–O21 | ⚠️ Open | Deps floor pins, Dockerfile runs as root, global warning suppression, dedup rounding, atan2 comment. |

---

## 4. New Findings This Audit

### P1 — Significant Bugs

| ID | File | Issue |
|----|------|-------|
| N1 | `ingestor/main.py:87-101` | FTP/HR persistence block opens a **new connection** after `finally: conn.close()`. If DB is unreachable at this point (unlikely but possible), the second `get_connection()` will raise and crash the startup flow. The block has its own `try/finally` but no exception handling — a crash here aborts `run()` before backfill check. |
| N2 | `ingestor/main.py:94-100` | `int(env_ftp)` will raise `ValueError` if `.env` contains a non-numeric value (e.g., `"220w"` or a quoted string). No validation before cast. Same for `env_hr`. |

### P2 — Minor Issues

| ID | File | Issue |
|----|------|-------|
| N3 | `pyproject.toml` | `[project.scripts]` entry point not present. `pyproject.toml` adds packaging metadata but doesn't wire up `veloai` as an installed command yet. |
| N4 | `grafana/dashboards/` | `all-time-progression.json` is 732 lines — included in repo and provisioned, but Marcin noted it will be reworked. No functional issue; just noting it's live in Grafana now. |
| N5 | `requirements.txt` (root) | Root `requirements.txt` and `ingestor/requirements.txt` are separate. Running `pip install -r requirements.txt` at root installs CLI deps only. Ingestor deps are installed in Docker. This is correct but undocumented — a new contributor could miss it. |

---

## 5. Tests

```
73 passed in 0.79s
```

**+2 vs prior audit** (was 71). All passing. The two new tests presumably cover the FTP/HR persistence or fitness changes — source not identified in this pass.

### Still-untested high-value pure functions (from prior audit — no change)

- `route_intelligence.py` — `_haversine_km`, `_density_at`
- `route_generator.py` — `_decode_polyline6`, `_loop_waypoints`, `_build_gpx`
- `strava.py` — `_detect_device`, `_parse_activity`, `_parse_streams`
- `weather.py` — `best_ride_hours`
- `route_planner.py` — `parse_distance`, `_analyze_wind`

---

## 6. Grafana Dashboards

### Dashboard inventory

| Dashboard | File | Status |
|-----------|------|--------|
| Overview | `overview.json` | ✅ Heavily reworked — see below |
| Activity Details | `activity.json` | ✅ Major rework — ready for Karoo data |
| All Time Progression | `all-time-progression.json` | ⚠️ Live but excluded (rework pending) |

### Deleted dashboards (good hygiene)
- `training-log.json` — removed
- `weekly-report.json` — removed  
- `year-in-review.json` — removed (replaced by All Time Progression)

### Overview dashboard changes (since afcff76)
- Sections restructured, panels reorganized
- VS Previous Period comparison is a dedicated expanded section
- Fitness section: added Weekly Streak + Days Since Ride stats
- Charts split by activity type (Outdoor/Zwift/E-Bike/Indoor) using color palette
- Configured FTP/Max HR now read from `sync_state` via dashboard variables

### Activity Details changes (since afcff76)
- **Power Metrics section added:** NP, IF, VI, EF, Work, TRIMP
- **Power Duration Curve:** Both this ride and all-time best, as trend panel
- **HR/Power/Cadence/Grade:** Zone charts as stacked horizontal bars; per-km splits with gradient backgrounds; cadence+grade combined panel
- **Tooltips:** Human-readable tooltips on all panels (Power Metrics, Fitness, Zones, Splits, Deltas)
- **TRIMP:** Replaced Suffer Score — computed locally from HR stream, color-coded by zone
- **Max HR delta:** Inverted (previously wrong direction)
- **Prev/Next navigation:** Added then reverted — looked bad
- **Power curve iterations:** Went through barchart → smooth line → barchart → trend panel. Final: trend panel with distance-based x-axis. Multiple reverts visible in git history (normal design exploration)

### Dashboard quality assessment
The activity dashboard has gone through heavy iteration. The commit history shows systematic exploration (try, revert, refine) which is good. The final state is well-structured. Key readiness question: **all power/HR panels will be empty until Karoo syncs a ride** — this is expected and correct, not a bug.

---

## 7. Deployment Status

| Component | Status |
|-----------|--------|
| Docker Compose (homelab 10.7.40.15) | ✅ Deployed (Portainer, HTTPS clone — resolved today) |
| Grafana port | ✅ Running on 3021 (port 3000 conflict resolved) |
| Strava token | ✅ Full scope (`activity:read_all`) re-authorized |
| Karoo 3 | ✅ Arrived today — first ride will trigger auto-sync |
| CLI (local Mac mini) | ✅ Working — route planning confirmed today |

---

## 8. Recommendations (Prioritised)

| Priority | ID | Action |
|----------|----|--------|
| 🔴 P1 | N1 | Wrap second `get_connection()` in `main.py` in try/except — log warning on failure, don't crash startup |
| 🔴 P1 | N2 | Add `int(env_ftp)` validation in `main.py` — catch ValueError, log and skip if non-numeric |
| 🔴 P1 | O1 | Stream restoration bug: `upsert_streams` wipes restored streams during dedup-merge. Needs API contract design discussion. |
| 🟡 P2 | N3 | Add `[project.scripts]` entry point to `pyproject.toml` so `veloai` works as installed CLI command |
| 🟡 P2 | O5 | Fix sunrise/sunset timezone comparison in `route_planner.py` |
| 🟡 P2 | O9 | Make `upsert_streams` atomic (wrap DELETE+INSERT in a transaction) |
| 🟡 P2 | O3 | Persist refreshed Strava token to a fallback on DB failure |
| 🟢 P3 | O17 | Add upper-bound pins to `requirements.txt` (e.g., `requests>=2.31,<3`) |
| 🟢 P3 | O18 | Add `USER veloai` to Dockerfile — avoid running ingestor as root |
| 🟢 P3 | O19 | Replace global `warnings.filterwarnings("ignore")` in `cli.py` with targeted suppression |
| 🔵 Test | — | Add tests for `_haversine_km`, `_decode_polyline6`, `_parse_activity`, `best_ride_hours` — highest value, pure functions |

---

## 9. Summary

**Overall state: solid and improving.** The prior audit cleared all P0 crashes and most P1 bugs. This release focused on dashboard polish and infrastructure (FTP/HR persistence, pyproject.toml). The Karoo arriving today is the real inflection point — the activity dashboard was built for power/HR stream data and will light up properly now.

**Two new P1s** (N1, N2) are minor — both in the new FTP/HR persistence code in `main.py`, easy to fix. No regressions from prior audit.

**Key watch items:**
- First Karoo ride → verify streams ingest correctly with HR + power
- O1 (stream wipe during dedup) becomes more relevant once real stream data flows
- All Time Progression dashboard is live but acknowledged as pending rework

---

*Prior audit: [docs/audit-2026-03-15-internal.md](../../../Developer/veloai/docs/audit-2026-03-15-internal.md)*  
*Tests: 73/73 passing*
