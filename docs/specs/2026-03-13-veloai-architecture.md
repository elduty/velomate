# VeloAI — Architecture Design Spec

**Date:** 2026-03-13
**Status:** Approved
**Author:** Nora (AI assistant)

---

## Problem

Marcin rides outdoors but has no easy way to answer: *"Should I ride this week, and which route?"*  
Relevant data (weather, fitness, saved routes) lives in three separate services with no unified view.

---

## Goal

A local Python tool that combines Komoot routes, Strava fitness data, and Open-Meteo weather to produce a weekly ride recommendation — delivered as WhatsApp-friendly text.

---

## Scope (v1)

**In scope:**
- Fetch cycling tours/routes from Komoot (via komPYoot + macOS Keychain)
- Fetch recent activity from Strava (OAuth refresh flow + Keychain)
- Fetch 7-day weather forecast for São Domingos de Rana (Open-Meteo)
- Score each day and recommend top day(s) + top 3 routes matching current fitness
- CLI: `python -m veloai` or `veloai` (if installed)

**Out of scope (v1):**
- Web UI or API server
- Push notifications / scheduling (handled externally by OpenClaw cron)
- New route discovery
- Multi-location support

---

## Architecture

### Module Breakdown

| Module | Responsibility |
|---|---|
| `veloai/keychain.py` | Retrieve JSON credentials from macOS Keychain |
| `veloai/komoot.py` | Authenticate + fetch cycling tours from Komoot |
| `veloai/strava.py` | Refresh OAuth token + fetch recent activities |
| `veloai/weather.py` | Fetch + parse 7-day forecast from Open-Meteo |
| `veloai/planner.py` | Score days, match routes to fitness, build recommendation text |
| `veloai/cli.py` | Entry point — orchestrates modules, prints output |

### Data Flow

```
Keychain → komoot.py → List[Tour]  ──┐
Keychain → strava.py → FitnessLevel──┼──► planner.py → Recommendation text → stdout
           weather.py → List[Day]  ──┘
```

### Credential Security

- Zero credentials in source code or config files
- All secrets via macOS Keychain (`openclaw/komoot`, `openclaw/strava`)
- Keychain keys documented in README — values never in repo

---

## Configuration

Hardcoded constants (non-secret, safe to commit):
- `LOCATION = {"lat": 38.69, "lon": -9.32, "name": "São Domingos de Rana"}`
- `STRAVA_ATHLETE_ID = 204728438`

---

## Output Format

WhatsApp-friendly: bold + bullets, no markdown tables or headers, emojis.

```
🚴 *VeloAI Ride Recommendation*

*Best day this week: Saturday*
⭐⭐⭐⭐⭐ Clear sky · 22°C · Wind 12km/h

*Top routes for your fitness level (getting back into it):*
• Cascais Loop — 42km, 380m elevation
• SDR–Sintra — 28km, 520m elevation
• Coastal Easy — 18km, 120m elevation
```

---

## Key Risks

- 🟡 **komPYoot** is unofficial — may break on Komoot API changes. Monitor.
- 🟡 **Strava rate limits** — 100 req/15min, 1000/day. Not a concern at current usage.
- 🟢 **Open-Meteo** — free, no auth, no rate limit concern.

---

*Last updated: 2026-03-13*
