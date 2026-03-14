# Route Planner (v1) — Design Spec

**Goal:** Add a `plan` subcommand to the VeloAI CLI that generates cycling routes based on natural language or flags, enriched with fitness and weather data, then opens the route on Komoot for saving to Karoo.

**Approach:** Parse user input (LLM or flags) → enrich with DB/weather data → build Komoot planner URL with waypoints → open in browser → print summary.

---

## Design Decisions

1. **Komoot is the routing engine** — VeloAI doesn't do pathfinding. It translates user intent into Komoot route parameters and opens the Komoot web planner with pre-filled waypoints. komPYoot has no confirmed tour creation API, so v1 uses URL-based handoff.
2. **Claude API for natural language parsing** — extracts structured parameters (duration, surface, loop, waypoints) from free-text input via `requests` (direct API call, no `anthropic` SDK). Flags bypass the LLM entirely.
3. **Fitness-aware distance** — target distance derived from duration × avg speed (from ride history), adjusted by TSB.
4. **Weather-aware output** — fetches forecast for planned ride date, warns about wind/rain/heat.
5. **Graceful degradation** — if Claude API fails, fall back to requiring flags. If weather fails, skip weather. If DB is unavailable, use default speed estimates.
6. **CLI refactoring** — `cli.py` is refactored from bare `main()` to argparse subcommands. Running without arguments (`python3 -m veloai.cli`) preserves existing behavior (weekly recommendation).

---

## User Interface

### Natural language (default)
```bash
python3 -m veloai.cli plan "2h gravel loop through coastal viewpoints"
python3 -m veloai.cli plan "easy recovery ride, 1 hour, avoid hills"
python3 -m veloai.cli plan "long road ride to Sintra and back"
```

### Structured flags
```bash
python3 -m veloai.cli plan --duration 2h --surface gravel --loop
python3 -m veloai.cli plan --duration 1h --surface road --loop --preference comfort
python3 -m veloai.cli plan --duration 3h --surface road --waypoints "Sintra,Cascais"
```

### Parameters

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| duration | string | required | Target ride time (e.g., "2h", "1h30m", "90min") |
| surface | enum | `gravel` | `road`, `gravel`, `mtb` — maps to Komoot sport type |
| loop | boolean | `true` | Round-trip back to start |
| waypoints | list | empty | Place names to route through |
| date | string | `tomorrow` | When to ride — used for weather lookup. Relative dates ("tomorrow", "saturday") resolved to next occurrence within 7-day forecast window |

---

## Processing Pipeline

### Step 1: Parse input

**Natural language path:** Send user input to Claude API with a system prompt:

```
Extract cycling route parameters from this request. Return JSON:
{
  "duration_minutes": <int>,
  "surface": "road" | "gravel" | "mtb",
  "loop": <bool>,
  "waypoints": [<string>],
  "date": "YYYY-MM-DD",
  "notes": "<any extra context>"
}
```

**Flags path:** argparse directly, no LLM call.

### Step 2: Estimate distance

Query ride history for avg speed by surface type:

```sql
SELECT ROUND(AVG(avg_speed_kmh)::numeric, 1) AS avg_speed
FROM activities
WHERE sport_type = 'cycling_outdoor'
  AND avg_speed_kmh > 0
  AND distance_m > 5000
```

The DB does not distinguish surface type per activity (only indoor/outdoor), so surface-specific speeds use fixed multipliers against overall outdoor average: road ≈ avg × 1.1, gravel ≈ avg × 0.85, mtb ≈ avg × 0.7. If no ride history exists, use defaults: road=27, gravel=22, mtb=17 km/h.

Target distance = duration_hours × estimated_speed.

### Step 3: Fitness adjustment

Read latest TSB from `athlete_stats`:
- TSB > 10 (fresh): no adjustment, note "good day to push"
- TSB -10 to 10 (neutral): no adjustment
- TSB < -10 (fatigued): reduce distance by 20%, note "adjusted for fatigue"

### Step 4: Weather check

Fetch Open-Meteo forecast for the target date (reuse existing `weather.py`):
- Wind > 30 km/h: warn in output
- Rain > 5mm: warn in output
- Temp > 35°C: warn about heat
- Include conditions in output summary

### Step 5: Build Komoot planner URL

Map parameters to Komoot URL:

| VeloAI param | Komoot equivalent |
|-------------|-------------------|
| surface=road | sport=`racebike` |
| surface=gravel | sport=`touringbicycle` |
| surface=mtb | sport=`mtb` |

Komoot planner URL flow:
1. Geocode waypoints (place names → lat/lng) using Nominatim (OpenStreetMap geocoder, free, no API key)
2. Build Komoot planner URL with start point (home: reuse `LOCATION` from `cli.py`), waypoints, and sport type
3. Open URL in browser via `webbrowser.open()`
4. User saves the route in Komoot → auto-syncs to Karoo

Komoot planner URL format: `https://www.komoot.com/plan/@{lat},{lng},{zoom}/{sport}?wp={lat1},{lng1}&wp={lat2},{lng2}`

### Step 6: Output

```
🗺 Route Plan: 2h Gravel Loop
  📏 ~44 km (estimated from your avg 22 km/h)
  🌤 Clear, 22°C, wind 15 km/h NW
  💪 TSB +3 (neutral) — normal distance
  🔗 Opening Komoot planner...

  Save the route in Komoot → syncs to Karoo automatically
```

---

## File Structure

| File | Responsibility |
|------|----------------|
| `veloai/route_planner.py` | Core pipeline: parse → enrich → build URL → format output |
| `veloai/llm.py` | Claude API wrapper via `requests` (direct Messages API call, no SDK) |
| `veloai/cli.py` | Refactor to argparse subcommands: default=`recommend`, new=`plan` |
| `veloai/geocode.py` | Nominatim geocoder (place name → lat/lng) |
| `veloai/weather.py` | Already exists — reused for forecast |
| `veloai/db.py` | Already exists — add `get_avg_speed()` query |
| `veloai/keychain.py` | Already exists — add `get_string()` for non-JSON values |

### New dependency

- No new pip dependencies — uses `requests` (already installed) for Claude API and Nominatim
- Anthropic API key stored in macOS Keychain: `openclaw/anthropic` (plain string, retrieved via new `keychain.get_string()`)

### CLI refactoring

`cli.py` must be refactored from bare `main()` to argparse with subcommands:
- `python3 -m veloai.cli` (no args) → runs existing `recommend` flow (backward compatible)
- `python3 -m veloai.cli plan "..."` → runs new route planner
- `python3 -m veloai.cli plan --duration 2h --surface gravel --loop`

---

## Komoot Integration — URL-Based (v1)

komPYoot has no confirmed tour creation method and Komoot has no official public API. For v1, the CLI opens the Komoot web planner with pre-filled parameters (sport type, start coordinates, waypoints). The user saves the route in their browser, and it auto-syncs to Karoo.

Future: if Komoot's internal API for tour creation is reverse-engineered, a v2 can automate the save step.

---

## Surface Type Mapping

| User input | Komoot sport | Typical avg speed (estimated) |
|-----------|-------------|-------------------------------|
| road | `racebike` | 25-30 km/h |
| gravel | `touringbicycle` | 20-25 km/h |
| mtb | `mtb` | 15-20 km/h |

Speed estimates are overridden by actual ride history averages when available.

---

## Configuration

| Key | Source | Value |
|-----|--------|-------|
| Home coordinates | `LOCATION` in `cli.py` | 38.69, -9.32 (São Domingos de Rana) — reuse existing constant |
| Anthropic API key | Keychain | `openclaw/anthropic` (plain string via `keychain.get_string()`) |
| Default surface | Config/flag | `gravel` |

---

## Error Handling

| Failure | Behavior |
|---------|----------|
| Claude API unreachable/invalid key | Print error, fall back to requiring flags |
| LLM returns malformed JSON | Retry once, then fall back to flags |
| LLM returns invalid values (duration=0) | Apply defaults, warn user |
| Nominatim geocoding fails | Skip waypoints, plan from home only |
| Weather API fails | Skip weather section in output |
| DB unavailable | Use default speed estimates (road=27, gravel=22, mtb=17 km/h) |
| Date outside 7-day forecast | Show weather as "unavailable" |

## Out of Scope (v2 — Route Intelligence Layer)

- OpenStreetMap surface tag queries (Overpass API)
- Strava heatmap/segment popularity data
- Ride history GPS clustering for variety/comfort preference
- Detailed surface breakdown percentages
- "Avoid this road" / "prefer this road" learning
- Route history (tracking which planned routes were actually ridden)
- Direct Komoot tour creation via API (if API becomes available)
