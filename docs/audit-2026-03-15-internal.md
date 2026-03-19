# VeloAI Internal Audit — 2026-03-15

Comprehensive production-readiness audit. All findings validated against source code.

## Findings Fixed in This Pass

### P0 — Crashes / Data Corruption

| ID | File:Line | Issue | Fix |
|----|-----------|-------|-----|
| A1 | `strava.py:301` | `data['date'][:10]` crashes with `TypeError` if Strava returns activity without `start_date` (None). Also `data['distance_m']/1000` crashes if distance is None. Aborts entire sync loop. | Used `(data.get('date') or '')[:10]` and `(data.get('distance_m') or 0)` |
| A2 | `planner.py:66-69` | `fitness['atl']` and `fitness['tsb']` accessed without `.get()` after checking only `ctl`. Crashes with `KeyError` on partial fitness dict. | Added `.get()` with guards |
| A3 | `planner.py:111` | Komoot URLs use `activities.id` (internal DB serial PK, e.g., 1, 2, 3), not Komoot tour IDs. Every link 404s. Also `"touringbicycle"` replacement is dead code. | Removed broken URLs and dead code |
| A4 | `cli.py:59-60` | `--start` arg parsing crashes with `IndexError`/`ValueError` on malformed input like `"38.7"` or `"abc,def"`. | Added validation with error messages |
| A5 | `route_intelligence.py:374` | Elevation profile: null elevations from API are skipped but `samples` array is not filtered to match. `elevations[i]` no longer corresponds to `samples[i]`, producing wrong gradient calculations. | Paired elevations with sample points, skip nulls together |

### P1 — Significant Bugs

| ID | File:Line | Issue | Fix |
|----|-----------|-------|-----|
| A6 | `db.py:174-178` | Asymmetric data-richness scoring: existing record scored on 3 fields (max 6), new record scored by `_data_richness` on 6 fields (max 9). Biases merge toward new records even when existing has richer data. | Both now use `_data_richness()` |
| A7 | `strava.py:192` | `raw.get("average_speed", 0)` — if Strava API returns `average_speed: null`, `.get()` returns `None` (key present, value null), and `None * 3.6` raises `TypeError`. | Changed to `(raw.get("average_speed") or 0)` |
| A8 | `map_preview.py:48-53,198` | Waypoint names and reasons from OSM/Strava not HTML-escaped. XSS via `bindPopup()` and waypoint list HTML. | Added `html.escape()` |
| A9 | `route_planner.py:79` | `parse_time("13pm")` returns `"25:00"` — invalid time. `hour=13` + `pm` → `hour+=12` = 25. | Added `if hour > 23: return None` |
| A10 | `route_generator.py:134-137` | Safety param silently ignored for MTB. `costing_options` stays `{}` (falsy), so `if costing_options:` skips the `use_roads` assignment. | Changed guard to `if costing_options or costing == "mountain_bike":` |
| A11 | `route_generator.py:134` | Comment says "0.0 = max safety" but CLI/README say "0.0 = fastest". Code is correct, comment inverted. | Fixed comment |
| A12 | `route_intelligence.py:379` | Hardcoded `85000` m/deg for longitude in `get_elevation_profile`. Only correct at ~40°N. 30% error at equator/60°N. | Now uses `cos(lat)` correction, consistent with `_haversine_km` |
| A13 | `route_intelligence.py:737` | Angular separation check doesn't handle wrapping at ±π. Two waypoints 20° apart near due West (±170°) would pass as "far apart" (diff=340°). | Fixed with proper modular angle difference |
| A14 | `weather.py:223` | Docstring says return dict has `golden_hour_end` but actual key is `civil_twilight_end`. | Fixed docstring |
| A15 | `config.example.yaml:14` | Default port `5432` but Docker maps `5423:5432`. CLI users copying example will fail to connect. | Changed to `5423` |
| A16 | `route_intelligence.py` | All `print()` calls go to stdout, mixing diagnostic output with data. | Changed all to `file=sys.stderr` |
| A17 | `geocode.py:37` | Error message to stdout instead of stderr. | Fixed |
| A18 | `CLAUDE.md:23,58` | Says "Grafana 12.0" (actual: 12.4), "Three dashboard JSON files" (actual: 5). | Fixed |

### Dead Code Removed

| Item | Location |
|------|----------|
| `_headers()` | `strava.py:98-99` — never called, all fetch functions take explicit `access_token` |
| Komoot URLs | `planner.py:111` — links to `komoot.com/tour/{db_id}` with wrong IDs |
| `"touringbicycle"` replace | `planner.py:109` — sport values never contain this string |

---

## Validated Open Findings (Not Fixed — Require Design Decisions)

### P1

| ID | File | Issue | Why Not Fixed |
|----|------|-------|---------------|
| O1 | `db.py:255-285` + `strava.py:288` | Stream restoration in dedup merge is immediately wiped by `upsert_streams` call in `sync_activities`. The carefully saved+restored streams get DELETE'd and replaced. | Fix requires changing the dedup-merge API contract. `upsert_activity` should signal to caller not to overwrite streams. Needs design discussion. |
| O2 | `db.py:113` | `find_duplicate_by_distance` is defined but never called. Was for Strava-Komoot cross-platform matching. | ✅ Fixed — removed dead code; added explanatory comment to `find_duplicate` |
| O3 | `strava.py:56-64` | If DB write of rotated refresh token fails, `_current_refresh_token` is not updated. Next auth attempt uses old (now-invalid) token → auth failure. | ✅ Fixed — `_current_refresh_token` updated in memory even on DB write failure |
| O4 | `config.py:75-77` | `bool` is subclass of `int`, so `type(True)("false")` returns `True`. Can't set boolean config to False via env vars. | No boolean configs currently use env vars (`loop` has no ENV_MAP entry). Latent bug. |
| O5 | `route_planner.py:504` | String comparison of local ride_time vs UTC sunrise/sunset times. Off by timezone offset. | ✅ Fixed — `fetch_sunrise_sunset` now parses ISO8601 and returns local times + tz label |
| O6 | `route_planner.py:136` | `_analyze_wind` bearing uses `atan2(dlng, dlat)` without cos(lat) correction on dlng. ~15° error at Lisbon. | Same issue as old RI3; acceptable for wind direction classification (45° buckets). |
| O7 | `fitness.py:57-62` | FTP estimation `RANGE BETWEEN 1199 PRECEDING` is correct for gapless data but overestimates if multiple activities' streams intermingle in the window partition. | Partition is `BY activity_id`, so this only affects within-activity gaps. Acceptable. |

### P2

| ID | Issue |
|----|-------|
| O8 | `main.py:53` — No startup retry if DB not ready. First `get_connection()` in `run_backfill`/`run` has no try/except. Docker Compose health checks mitigate this. | ✅ Fixed — 10-attempt retry loop with 5s backoff |
| O9 | `db.py` (ingestor) — `autocommit=True` means `upsert_streams` DELETE+INSERT are not atomic. Crash between them loses streams. | ✅ Fixed — wrapped in transaction with autocommit=False |
| O10 | `fitness.py:183` — Weekly totals use O(n²) iteration. Acceptable for ~365 days. |
| O11 | `fitness.py:141` — N+1 TSS UPDATE per activity. ~365 round-trips for a year. |
| O12 | `route_intelligence.py:98` — Strava segments bounding box uses hardcoded `cos(lat)=0.75`. Only correct at ~41°N. | ✅ Fixed — replaced with `math.cos(math.radians(lat))` |
| O13 | `route_intelligence.py:352` — Elevation API uses GET with coordinates in URL. Long URLs may be rejected by proxies. |
| O14 | `map_preview.py:42` — Fixed `coords[::5]` downsampling regardless of route length. |
| O15 | `config.py:54-55` — Config caching ignores `config_path` argument after first load. | ✅ Fixed — cache keyed by path (`_config_path_used` tracks last path) |
| O16 | No `pyproject.toml` or `setup.py` — CLI can't be pip-installed. | ✅ Fixed — `pyproject.toml` already present |
| O17 | Dependencies use floor pins only (`>=`), no upper bounds. | ✅ Fixed — `requirements.txt` already uses `~=` compatible release pins |
| O18 | Dockerfile runs as root (no USER directive). | ✅ Fixed — `ingestor/Dockerfile` already uses `USER app` |
| O19 | `cli.py:8` — `warnings.filterwarnings("ignore")` suppresses all warnings globally. | ✅ Fixed — scoped to `mapbox_vector_tile` and `google.protobuf` modules |
| O20 | `planner.py:23` — Distance dedup rounding boundary creates arbitrary split at 500m intervals. |
| O21 | `route_intelligence.py:652` — Comment says "clockwise" but atan2(dlat, dlng) produces East-origin counterclockwise ordering. | ✅ Fixed — comment corrected to "counterclockwise" |

---

## Test Coverage

71 tests pass. All assertions are correct.

### Untested Pure Functions (highest value targets)

| Module | Function | Lines | Why It Matters |
|--------|----------|-------|----------------|
| `route_intelligence.py` | `_haversine_km` | 9-14 | Critical math used in all scoring |
| `route_intelligence.py` | `_density_at` | 574-579 | Grid lookup with normalization |
| `route_generator.py` | `_decode_polyline6` | 54-78 | Algorithmic, bug-prone |
| `route_generator.py` | `_loop_waypoints` | 34-51 | Geometry |
| `route_generator.py` | `_build_gpx` | 81-101 | XML generation |
| `strava.py` | `_detect_device` | 165-177 | Device classification |
| `strava.py` | `_parse_activity` | 177-199 | Speed conversion (`*3.6`) |
| `strava.py` | `_parse_streams` | 229-260 | Data transformation |
| `weather.py` | `best_ride_hours` | 87-131 | Pure scoring, no HTTP |
| `route_planner.py` | `parse_distance` | 207-215 | Input parsing |
| `route_planner.py` | `_analyze_wind` | 113-167 | Geometry + wind classification |

### Test Quality Notes

- `test_classify_and_weather.py` mocks `psycopg2`/`requests` at import time via `sys.modules` — persists for entire pytest session
- `_score_weather` UV penalty path never tested (all calls use `uv_max=0`)
- `parse_time` edge cases (`"13pm"`, `"25:00"`) not tested

---

*Generated 2026-03-15 by internal audit*
