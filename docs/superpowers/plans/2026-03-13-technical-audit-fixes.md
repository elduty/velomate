# Technical Audit Fixes — Implementation Plan

> **For agentic workers:** REQUIRED: Use superpowers:subagent-driven-development (if subagents available) or superpowers:executing-plans to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix bugs, close resource leaks, add missing error handling, and update stale documentation found in the technical audit.

**Architecture:** Targeted fixes across ingestor, CLI, Grafana provisioning, and docs. No new features — only correctness, safety, and documentation accuracy.

**Tech Stack:** Python 3.11, PostgreSQL 15, Grafana, Docker Compose

---

## Audit Summary

Full codebase audit found 9 real issues worth fixing and 6 documentation inconsistencies. Several reported "bugs" were verified as false positives:

- **NOT a bug:** `strava.py:21` global variable ordering — Python `global` is a scope declaration, not a reference. `_current_refresh_token` is defined at module level (line 53) before any function is called.
- **NOT a bug:** Grafana distance calculation "1000x error" — `speed_kmh / 3600.0 * seconds` correctly yields km (km/h to km/s conversion).
- **NOT a bug:** `komoot.py:36` division by zero — the `if distance_m and duration_s:` guard ensures both are truthy before dividing.
- **NOT a bug:** `planner.py:66-69` KeyError — all three keys (ctl/atl/tsb) always come together from `get_latest_fitness()`.

---

## File Map

| File | Action | Responsibility |
|------|--------|----------------|
| `ingestor/main.py` | Modify | Close connection in `run_backfill()` |
| `ingestor/db.py` | Modify | Add division-by-zero guard, atomic merge |
| `ingestor/strava.py` | Modify | Add 429 retry, log suppressed exceptions |
| `ingestor/requirements.txt` | Modify | Pin dependency versions |
| `veloai/keychain.py` | Modify | Add error handling |
| `veloai/cli.py` | Modify | Fix connection leak on exception |
| `veloai/weather.py` | Modify | Handle API failure gracefully |
| `grafana/provisioning/datasources/postgres.yml` | Modify | Document SSL decision |
| `README.md` | Modify | Fix stale content |
| `CLAUDE.md` | Modify | Add keychain entry, fix Grafana version |
| `docker-compose.yml` | Modify | Pin Grafana version |

---

## Chunk 1: Bug Fixes

### Task 1: Fix connection leak in `run_backfill()`

`ingestor/main.py:64-72` creates a DB connection but never closes it. The `run()` function also creates a connection at line 77 that stays open for the process lifetime (acceptable for a long-running poller), but `run_backfill()` creates a second one that leaks.

**Files:**
- Modify: `ingestor/main.py:64-72`

- [ ] **Step 1: Fix `run_backfill()` to close its connection**

```python
def run_backfill():
    """One-time backfill — call manually or on first run."""
    conn = get_connection()
    try:
        create_schema(conn)
        count = backfill(conn, months=12)
        recalculate_fitness(conn)
        sync_komoot(conn)
        print(f"[backfill] Complete — {count} Strava + Komoot activities ingested")
        return count
    finally:
        conn.close()
```

- [ ] **Step 2: Verify ingestor still starts cleanly**

Run: `cd /Users/marcin/Git/veloai && docker compose build ingestor && docker compose up -d ingestor && sleep 5 && docker compose logs --tail=20 ingestor`
Expected: No crashes, "[main] Schema ready" in output

- [ ] **Step 3: Commit**

```bash
git add ingestor/main.py
git commit -m "fix: close DB connection in run_backfill() to prevent leak"
```

---

### Task 2: Fix connection leak in CLI

`veloai/cli.py:19-28` only closes the connection in the success path. If an exception occurs between `get_connection()` and `conn.close()`, the connection leaks.

**Files:**
- Modify: `veloai/cli.py:11-33`

- [ ] **Step 1: Wrap DB usage in try/finally**

```python
def main():
    fitness = {}
    tours = None

    # Try DB first
    try:
        from veloai.db import get_connection, get_latest_fitness, get_routes
        conn = get_connection()
        if conn:
            try:
                print("Connected to VeloAI DB", file=sys.stderr)
                fitness = get_latest_fitness(conn)
                db_routes = get_routes(conn)
                if db_routes:
                    tours = db_routes
                    print(f"  → {len(tours)} routes from DB", file=sys.stderr)
                if fitness:
                    print(f"  → Fitness: CTL={fitness.get('ctl', '?')}, ATL={fitness.get('atl', '?')}, TSB={fitness.get('tsb', '?')}", file=sys.stderr)
            finally:
                conn.close()
        else:
            print("DB unavailable, falling back to Komoot API", file=sys.stderr)
    except Exception as e:
        print(f"DB error ({e}), falling back to Komoot API", file=sys.stderr)
```

- [ ] **Step 2: Test CLI still runs**

Run: `cd /Users/marcin/Git/veloai && python3 -m veloai.cli 2>&1 | head -5`
Expected: Output with no tracebacks (may fail on DB connection if not on homelab — that's fine, should show fallback message)

- [ ] **Step 3: Commit**

```bash
git add veloai/cli.py
git commit -m "fix: close DB connection in CLI error path"
```

---

### Task 3: Add error handling to `keychain.py`

`veloai/keychain.py` has zero error handling. `subprocess.check_output()` raises `CalledProcessError` if the keychain entry doesn't exist, and `json.loads()` crashes on non-JSON output.

**Files:**
- Modify: `veloai/keychain.py`

- [ ] **Step 1: Add try/except with clear error message**

```python
import json
import subprocess


def get(service: str) -> dict:
    """Retrieve JSON credentials from macOS Keychain.
    Raises RuntimeError if entry not found or not valid JSON.
    """
    try:
        raw = subprocess.check_output(
            ["security", "find-generic-password", "-a", "openclaw", "-s", service, "-w"],
            stderr=subprocess.DEVNULL,
        ).decode().strip()
    except subprocess.CalledProcessError:
        raise RuntimeError(f"Keychain entry not found: openclaw/{service}")
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        raise RuntimeError(f"Keychain entry openclaw/{service} is not valid JSON")
```

- [ ] **Step 2: Commit**

```bash
git add veloai/keychain.py
git commit -m "fix: add error handling for missing/invalid keychain entries"
```

---

### Task 4: Guard SQL division by zero in `find_duplicate_by_distance()`

`ingestor/db.py:124` divides by the `distance_m` parameter. While callers currently guard against zero, the SQL itself should be safe regardless.

**Files:**
- Modify: `ingestor/db.py:119-126`

- [ ] **Step 1: Add distance > 0 guard to SQL**

```python
def find_duplicate_by_distance(conn, date_str: str, distance_m: float, tolerance_pct: float = 0.10):
    """Find an existing activity on the same calendar day with similar distance (±10%)."""
    if not distance_m or distance_m <= 0:
        return None
    with conn.cursor() as cur:
        cur.execute("""
            SELECT id, strava_id, device, distance_m, avg_hr, avg_power
            FROM activities
            WHERE date::date = %s::date
              AND distance_m > 0
              AND ABS(distance_m - %s) / %s < %s
        """, (date_str, distance_m, distance_m, tolerance_pct))
        return cur.fetchone()
```

- [ ] **Step 2: Commit**

```bash
git add ingestor/db.py
git commit -m "fix: guard against division by zero in find_duplicate_by_distance()"
```

---

### Task 5: Make activity merge atomic in `upsert_activity()`

`ingestor/db.py:188-190` deletes an existing activity then inserts the merged one as two separate auto-committed statements. If the process crashes between delete and insert, data is lost.

**Files:**
- Modify: `ingestor/db.py:169-224`

- [ ] **Step 1: Wrap delete+insert in explicit transaction**

The connection uses `autocommit=True`, so SAVEPOINTs won't work (PostgreSQL requires an active transaction block). Instead, temporarily disable autocommit for the merge path:

```python
def upsert_activity(conn, data: dict) -> int:
    """Insert or update an activity. Returns the activity id."""
    now = datetime.now(timezone.utc)
    data = classify_activity(data)

    # Duplicate detection
    if data.get("date") and data.get("duration_s"):
        duplicate = find_duplicate(conn, data["date"], data["duration_s"])
        if duplicate and duplicate[1] != data.get("strava_id"):
            ex_id = duplicate[0]
            merged = merge_activity_data(duplicate, data)
            if merged.get("_skip_insert"):
                print(f"  [dedup] Skipping {data['name']} — weaker duplicate of existing activity {ex_id}")
                return ex_id
            else:
                # Atomic merge: disable autocommit so DELETE + INSERT are one transaction
                print(f"  [dedup] Merging {data['name']} with existing activity {ex_id} (device priority)")
                conn.autocommit = False
                try:
                    with conn.cursor() as cur:
                        cur.execute("DELETE FROM activities WHERE id = %s", (ex_id,))
                    data = merged
                    # INSERT happens below, still inside this transaction
                    activity_id = _do_insert(conn, data, now)
                    conn.commit()
                    return activity_id
                except Exception:
                    conn.rollback()
                    raise
                finally:
                    conn.autocommit = True

    return _do_insert(conn, data, now)


def _do_insert(conn, data: dict, now) -> int:
    """Execute the INSERT ... ON CONFLICT for an activity. Returns activity id."""
    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO activities (
                strava_id, name, date, distance_m, duration_s, elevation_m,
                avg_hr, max_hr, avg_power, max_power, avg_cadence,
                avg_speed_kmh, calories, suffer_score, device,
                is_indoor, sport_type, synced_at
            ) VALUES (
                %(strava_id)s, %(name)s, %(date)s, %(distance_m)s, %(duration_s)s, %(elevation_m)s,
                %(avg_hr)s, %(max_hr)s, %(avg_power)s, %(max_power)s, %(avg_cadence)s,
                %(avg_speed_kmh)s, %(calories)s, %(suffer_score)s, %(device)s,
                %(is_indoor)s, %(sport_type)s, %(synced_at)s
            )
            ON CONFLICT (strava_id) DO UPDATE SET
                name = EXCLUDED.name,
                distance_m = EXCLUDED.distance_m,
                duration_s = EXCLUDED.duration_s,
                elevation_m = EXCLUDED.elevation_m,
                avg_hr = EXCLUDED.avg_hr,
                max_hr = EXCLUDED.max_hr,
                avg_power = EXCLUDED.avg_power,
                max_power = EXCLUDED.max_power,
                avg_cadence = EXCLUDED.avg_cadence,
                avg_speed_kmh = EXCLUDED.avg_speed_kmh,
                calories = EXCLUDED.calories,
                suffer_score = EXCLUDED.suffer_score,
                device = EXCLUDED.device,
                is_indoor = EXCLUDED.is_indoor,
                sport_type = EXCLUDED.sport_type,
                synced_at = EXCLUDED.synced_at
            RETURNING id
        """, {**data, "synced_at": now})
        return cur.fetchone()[0]
```

- [ ] **Step 2: Verify ingestor builds and starts**

Run: `cd /Users/marcin/Git/veloai && docker compose build ingestor`
Expected: Build succeeds

- [ ] **Step 3: Commit**

```bash
git add ingestor/db.py
git commit -m "fix: make activity merge atomic using savepoints"
```

---

## Chunk 2: Resilience Improvements

### Task 6: Add Strava 429 rate limit retry

`ingestor/strava.py` has no retry logic for HTTP 429 responses. Strava enforces 100 requests per 15 minutes for short-term and 1000 per day.

**Files:**
- Modify: `ingestor/strava.py`

- [ ] **Step 1: Add retry helper at top of file**

After the imports (line 7), add:

```python
def _request_with_retry(method, url, max_retries=3, **kwargs):
    """Make an HTTP request with exponential backoff on 429."""
    kwargs.setdefault("timeout", 15)
    for attempt in range(max_retries + 1):
        resp = method(url, **kwargs)
        if resp.status_code == 429:
            wait = min(60 * (2 ** attempt), 900)  # max 15 min
            print(f"[strava] Rate limited (429), waiting {wait}s (attempt {attempt + 1})")
            time.sleep(wait)
            continue
        return resp
    return resp  # return last response even if still 429
```

- [ ] **Step 2: Replace `requests.get/post` calls with `_request_with_retry`**

In `fetch_recent_activities`, `fetch_activity_detail`, `fetch_activity_streams`, and `refresh_access_token`, replace direct `requests.get(...)` / `requests.post(...)` with `_request_with_retry(requests.get, ...)` / `_request_with_retry(requests.post, ...)`.

For example in `fetch_recent_activities` (line 91-96):
```python
        resp = _request_with_retry(
            requests.get,
            f"{API_BASE}/athlete/activities",
            headers=headers,
            params={"after": after_epoch, "per_page": 50, "page": page},
        )
```

Apply the same pattern to `fetch_activity_detail` (line 113-117), `fetch_activity_streams` (line 129-134), and `refresh_access_token` (line 26-31).

- [ ] **Step 3: Log suppressed exception in `_get_token()`**

Change line 70-71 from bare `except Exception: pass` to:

```python
        except Exception as e:
            print(f"[strava] Could not load stored refresh token: {e}")
```

- [ ] **Step 4: Verify ingestor builds**

Run: `cd /Users/marcin/Git/veloai && docker compose build ingestor`
Expected: Build succeeds

- [ ] **Step 5: Commit**

```bash
git add ingestor/strava.py
git commit -m "fix: add 429 rate limit retry with exponential backoff"
```

---

### Task 7: Handle weather API failure gracefully in CLI

`veloai/weather.py:71-72` crashes the entire CLI if Open-Meteo is down.

**Files:**
- Modify: `veloai/weather.py:68-94`

- [ ] **Step 1: Add `import sys` to weather.py**

Add `import sys` after the existing `from datetime import datetime` line at the top of the file.

- [ ] **Step 2: Add fallback for API failure**

```python
def fetch_forecast(lat: float, lon: float) -> List[Dict]:
    """Return list of 7 day dicts. Returns empty list if API unavailable."""
    url = WEATHER_URL.format(lat=lat, lon=lon)
    try:
        r = requests.get(url, timeout=10)
        r.raise_for_status()
    except requests.RequestException as e:
        print(f"[weather] Open-Meteo API error: {e}", file=sys.stderr)
        return []
    data = r.json()["daily"]

    forecast = []
    for i, date_str in enumerate(data["time"]):
        dt = datetime.strptime(date_str, "%Y-%m-%d")
        code = data["weathercode"][i]
        precip = data["precipitation_sum"][i]
        wind = data["windspeed_10m_max"][i]
        temp_max = data["temperature_2m_max"][i]
        temp_min = data["temperature_2m_min"][i]
        forecast.append({
            "date": date_str,
            "day_name": DAY_NAMES[dt.weekday()],
            "temp_max": temp_max,
            "temp_min": temp_min,
            "precip": precip,
            "wind": wind,
            "code": code,
            "weather": WMO_CODES.get(code, "Unknown"),
            "score": _score_weather(precip, wind, temp_max, code),
        })
    return forecast
```

- [ ] **Step 2: Handle empty forecast in `cli.py`**

In `veloai/cli.py`, after `days = weather.fetch_forecast(...)`, add:

```python
    if not days:
        print("Weather unavailable — skipping recommendation", file=sys.stderr)
        return
```

- [ ] **Step 3: Commit**

```bash
git add veloai/weather.py veloai/cli.py
git commit -m "fix: handle weather API failure gracefully in CLI"
```

---

### Task 8: Pin dependency versions

Both `requirements.txt` files have unpinned dependencies. The ingestor `Dockerfile` also uses `grafana/grafana:latest` which is unpredictable.

**Files:**
- Modify: `ingestor/requirements.txt`
- Modify: `requirements.txt`
- Modify: `docker-compose.yml`

- [ ] **Step 1: Check currently installed versions in the ingestor container**

Run: `cd /Users/marcin/Git/veloai && docker compose exec veloai-ingestor pip freeze 2>/dev/null || echo "Container not running — pin manually"`

- [ ] **Step 2: Pin ingestor dependencies**

Update `ingestor/requirements.txt` with `>=` minimum versions (don't over-constrain):

```
psycopg2-binary>=2.9
requests>=2.28
schedule>=1.2
komPYoot>=0.1
```

- [ ] **Step 3: Pin CLI dependencies**

Update `requirements.txt`:

```
psycopg2-binary>=2.9
requests>=2.28
komPYoot>=0.1
```

- [ ] **Step 4: Pin Grafana image version**

In `docker-compose.yml`, change line 42 from `grafana/grafana:latest` to a specific version:

```yaml
    image: grafana/grafana:12.0.0
```

(Check the actual version running: `docker compose exec veloai-grafana grafana-server -v 2>/dev/null` and use that.)

- [ ] **Step 5: Commit**

```bash
git add ingestor/requirements.txt requirements.txt docker-compose.yml
git commit -m "chore: pin dependency versions for reproducible builds"
```

---

## Chunk 3: Documentation Fixes

### Task 9: Fix documentation inconsistencies

Multiple docs reference outdated names, versions, and missing features.

**Files:**
- Modify: `CLAUDE.md`
- Modify: `README.md`

- [ ] **Step 1: Fix CLAUDE.md**

1. Change "Grafana 12" to match the pinned version from Task 8
2. Add `openclaw/veloai-db` to the Keychain entries in Environment section:

```
Credentials in macOS Keychain: `openclaw/strava` (Strava OAuth), `openclaw/komoot` (Komoot login), `openclaw/veloai-db` (CLI DB password).
```

- [ ] **Step 2: Fix README.md stale content**

1. Add `is_indoor` and `sport_type` columns to the schema section
2. Add power-based TSS to the fitness section (note that power is preferred over HR)
3. Add cross-device deduplication to the data pipeline section
4. Fix the DB_PORT comment (CLI default is 5423, not 5432)
5. Add `VELOAI_DB_*` variables to the environment section if not already there

- [ ] **Step 3: Commit**

```bash
git add CLAUDE.md README.md
git commit -m "docs: fix stale schema, version, and keychain references"
```

---

## Issues NOT Fixed (Deliberate Decisions)

These were flagged in the audit but are intentional or not worth the complexity:

| Issue | Reason to skip |
|-------|---------------|
| Grafana SSL disabled | Internal homelab network, no internet exposure. Add a YAML comment noting this. |
| No unique constraint on `(activity_id, time_offset)` | Streams are always deleted+reinserted atomically per activity. Unique constraint would add overhead with no benefit. |
| `activities.date` allows NULL | Some activities may legitimately lack a date during partial sync. Fitness calc already filters `WHERE date IS NOT NULL`. |
| Thread safety on token refresh | Ingestor is single-threaded (`schedule` library). No concurrency risk. |
| `planner.py` accessing `fitness['atl']` without `.get()` | Always co-present with `ctl` from same DB row. Defensive `.get()` would hide real bugs. |
