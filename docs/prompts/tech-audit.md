# Technical Audit Prompt

Use this prompt to audit the codebase for bugs, security issues, performance problems, and code quality.

---

You are performing a technical audit of VeloAI, a self-hosted cycling data platform. Your goal is to find bugs, security issues, performance problems, and code quality concerns — then provide concrete fixes.

Read these files:

**Ingestor (Docker service):**
- `ingestor/main.py` — scheduler, entry point
- `ingestor/strava.py` — Strava API client, OAuth token refresh
- `ingestor/db.py` — schema DDL, upserts, deduplication
- `ingestor/fitness.py` — CTL/ATL/TSB calculation, NP/EF/Work

**CLI (local):**
- `veloai/cli.py` — entry point
- `veloai/planner.py` — ride recommendation formatting
- `veloai/weather.py` — Open-Meteo API client
- `veloai/db.py` — read-only DB client
- `veloai/config.py` — YAML config + env vars
- `veloai/route_planner.py` — route planning logic
- `veloai/route_generator.py` — Valhalla GPX generation

**Configuration:**
- `docker-compose.yml`
- `Dockerfile`
- `.env.example`
- `config.example.yaml`

**Tests:**
- `tests/` — all test files

Also read `CLAUDE.md` for architecture context and `ingestor/db.py:create_schema()` for the full database schema.

## What to Check

**1. Bugs**
- Unhandled exceptions that would crash the service
- Race conditions (concurrent Strava syncs, fitness recalculation)
- Data corruption risks (partial writes, missing transactions)
- Off-by-one errors in time windows, date calculations
- NULL handling — missing COALESCE, division by zero, None propagation
- Edge cases: first run (empty DB), single activity, no power data, no HR data

**2. Security**
- Credential exposure (hardcoded secrets, logged tokens, error messages leaking creds)
- SQL injection (even though this is a personal project)
- API token handling (refresh flow, token storage, expiry)
- Docker security (running as root, exposed ports, volume permissions)
- Config file permissions (password_cmd, credential chain)

**3. Performance**
- N+1 query patterns
- Missing database indexes for common query patterns
- Unbounded queries (no LIMIT, full table scans)
- Memory issues (loading all stream data into memory)
- Connection leaks (cursors/connections not closed)
- Inefficient loops that could be single SQL queries

**4. Code Quality**
- Dead code (unused functions, unreachable branches)
- Inconsistent error handling patterns
- Functions doing too many things
- Missing type hints on public interfaces
- Test coverage gaps (untested code paths, mocked-but-never-real tests)

**5. Operational**
- Logging gaps (silent failures, missing context in log messages)
- Graceful shutdown handling
- Retry logic for external APIs (Strava, Open-Meteo, Valhalla)
- What happens when PostgreSQL is down?
- What happens when Strava API returns 500?
- What happens when the ingestor crashes mid-sync?

## Output Format

For each finding, provide:
1. **Severity**: P0 (data loss/security), P1 (service crash), P2 (incorrect results), P3 (code quality)
2. **File:line** — exact location
3. **What's wrong** — specific, reproducible
4. **Fix** — concrete code change or approach

Group by severity. For P0/P1, include the exact fix code. For P2/P3, a description is sufficient.

At the end, provide a summary:
- Total findings by severity
- Top 3 most impactful fixes
- Any architectural concerns that aren't bugs but limit future development
