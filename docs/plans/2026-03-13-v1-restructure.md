# VeloAI v1 — Implementation Plan

> Use `subagent-driven-development` (independent tasks) or `executing-plans` (sequential) to implement this.

**Goal:** Restructure the working ride-planner.py script into a clean modular Python package.
**Architecture:** Six focused modules (keychain, komoot, strava, weather, planner, cli) with no credential leakage.
**Tech Stack:** Python 3.9+, komPYoot, requests, macOS Keychain (security CLI)

---

## File Map

| Action | Path |
|---|---|
| Create | `veloai/__init__.py` |
| Create | `veloai/keychain.py` |
| Create | `veloai/komoot.py` |
| Create | `veloai/strava.py` |
| Create | `veloai/weather.py` |
| Create | `veloai/planner.py` |
| Create | `veloai/cli.py` |
| Create | `requirements.txt` |
| Create | `.gitignore` |
| Create | `README.md` |
| Archive | `scripts/ride-planner.py` → `scripts/ride-planner-v0.py` |

---

## Task 1: Project Scaffolding

**Files:** `veloai/__init__.py`, `requirements.txt`, `.gitignore`, `README.md`

- [ ] Create `veloai/__init__.py` with version string: `__version__ = "1.0.0"`
- [ ] Create `requirements.txt`:
  ```
  requests
  komPYoot
  ```
- [ ] Create `.gitignore`:
  ```
  __pycache__/
  *.pyc
  .env
  *.env
  venv/
  .venv/
  .DS_Store
  *.egg-info/
  dist/
  ```
- [ ] Create `README.md` — see Task 7
- [ ] Verify: `ls veloai/` shows `__init__.py`
- [ ] Commit: `git commit -m "chore: scaffold veloai package"`

---

## Task 2: keychain.py

**File:** `veloai/keychain.py`

- [ ] Extract `get_keychain()` from ride-planner.py into its own module
- [ ] Add typed return + docstring

```python
import json
import subprocess


def get(service: str) -> dict:
    """Retrieve JSON credentials from macOS Keychain."""
    raw = subprocess.check_output(
        ["security", "find-generic-password", "-a", "openclaw", "-s", service, "-w"],
        stderr=subprocess.DEVNULL,
    ).decode().strip()
    return json.loads(raw)
```

- [ ] Run: `python3 -c "from veloai.keychain import get; print(get('openclaw/strava').keys())"` — should print dict keys
- [ ] Commit: `git commit -m "feat: add keychain credential helper"`

---

## Task 3: weather.py

**File:** `veloai/weather.py`

- [ ] Extract weather fetching + `score_weather()` + WMO code map from ride-planner.py
- [ ] Public interface:

```python
def fetch_forecast(lat: float, lon: float) -> list[dict]:
    """Return list of 7 day dicts: {date, day_name, temp_max, temp_min, precip, wind, code, label, score}"""
```

- [ ] Run: `python3 -c "from veloai.weather import fetch_forecast; days = fetch_forecast(38.69, -9.32); print(days[0])"`
- [ ] Confirm output has `score` key between 0–100
- [ ] Commit: `git commit -m "feat: add weather module"`

---

## Task 4: komoot.py

**File:** `veloai/komoot.py`

- [ ] Extract Komoot auth + tour fetching + deduplication logic from ride-planner.py
- [ ] Public interface:

```python
def fetch_tours() -> list[dict]:
    """Return deduplicated list of cycling tours: {name, distance_km, elevation_m, url}"""
```

- [ ] Uses `veloai.keychain.get("openclaw/komoot")`
- [ ] Run: `python3 -c "from veloai.komoot import fetch_tours; tours = fetch_tours(); print(len(tours), 'tours')"`
- [ ] Commit: `git commit -m "feat: add komoot integration module"`

---

## Task 5: strava.py

**File:** `veloai/strava.py`

- [ ] Extract Strava OAuth refresh + activity fetch + fitness classification from ride-planner.py
- [ ] Public interface:

```python
def get_fitness_level() -> dict:
    """Return {level: str, rides_last_4w: int, avg_distance_km: float, description: str}"""
```

- [ ] Uses `veloai.keychain.get("openclaw/strava")`
- [ ] Run: `python3 -c "from veloai.strava import get_fitness_level; print(get_fitness_level())"`
- [ ] Confirm returns `level` key with a string
- [ ] Commit: `git commit -m "feat: add strava integration module"`

---

## Task 6: planner.py + cli.py

**Files:** `veloai/planner.py`, `veloai/cli.py`

- [ ] Extract recommendation logic from ride-planner.py into `planner.py`
- [ ] Public interface:

```python
def recommend(days: list, tours: list, fitness: dict) -> str:
    """Return formatted WhatsApp-friendly recommendation string."""
```

- [ ] `cli.py` — thin orchestrator:

```python
from veloai import komoot, strava, weather, planner
from veloai.config import LOCATION

def main():
    days = weather.fetch_forecast(LOCATION["lat"], LOCATION["lon"])
    tours = komoot.fetch_tours()
    fitness = strava.get_fitness_level()
    print(planner.recommend(days, tours, fitness))

if __name__ == "__main__":
    main()
```

- [ ] Run: `python3 -m veloai.cli` — full output should appear
- [ ] Compare output to old `python3 scripts/ride-planner.py` — same structure
- [ ] Commit: `git commit -m "feat: add planner and cli entry point"`

---

## Task 7: README.md

**File:** `README.md`

- [ ] Write README with:
  - What VeloAI does (1 paragraph)
  - Requirements (Python 3.9+, macOS Keychain entries)
  - Setup: `pip install -r requirements.txt`
  - Keychain setup: which keys to create and format (no values)
  - Usage: `python3 -m veloai.cli`
  - Roadmap section (link to Obsidian or keep brief)
- [ ] Commit: `git commit -m "docs: add README"`

---

## Task 8: Archive old script + final check

- [ ] Rename: `git mv scripts/ride-planner.py scripts/ride-planner-v0.py`
- [ ] Add comment at top of v0: `# ARCHIVED — superseded by the veloai/ package`
- [ ] Run full end-to-end: `python3 -m veloai.cli`
- [ ] Commit: `git commit -m "chore: archive v0 script"`

---

## After All Tasks

- Push to `gitea.mrmartian.in/MrMartian/veloai`
- Update Obsidian `4 - Projects/Active/VeloAI/Progress Log.md`
- Update `VeloAI.md` script path to `veloai/cli.py`

---

*Last updated: 2026-03-13*
