# W'bal Time Series Per Ride — Design Spec

**Date:** 2026-04-12
**Cluster:** B (Performance Modeling) — phase 2 of 3
**Status:** Designed, awaiting review
**Depends on:** CP/W' foundation (PR #108, merged)

## Goal

Compute per-second W'bal (remaining anaerobic work capacity) for every ride with power data. Display it as a time series on Activity Details alongside the power trace, so the rider can see exactly when they drained their battery, how much they had left, and where it refilled.

## Background

W'bal is the real-time "fuel gauge" for efforts above Critical Power. It starts full at the beginning of a ride (equal to W') and drains whenever power exceeds CP. Below CP, it refills exponentially toward W'. The rider who blows up on the 5th interval can see exactly why: "W'bal was at 2 kJ when interval 5 started, but the interval needed 8 kJ."

VeloMate now has CP and W' from the Monod-Scherrer fit (PR #108). This spec adds the per-ride computation and visualization.

## Key Decisions

| Decision | Choice | Reason |
|---|---|---|
| **Model** | Skiba differential with GoldenCheetah tau | Industry standard, matches reference implementation |
| **CP/W' source** | Use the **latest** `cp_estimates` row (not per-ride-date). When `w_prime_kj` is NULL (fallback rows), default W' to 20 kJ | Latest is simpler and avoids "no nearby CP estimate" edge cases for early rides. CP changes slowly for amateurs (weeks/months). W'bal's value is in the shape (when you drain/refill), not the absolute number — approximate CP still gives the right insight. 20 kJ is the Skiba default and within the normal amateur range (15-25 kJ) |
| **CP for W'bal** | When `source='cp'`: use `cp_watts`. When `source='20min_fallback'`: use `fallback_ftp` | The fallback FTP is the best available aerobic ceiling estimate when CP fit fails |
| **Storage** | New column `w_bal` (FLOAT) on `activity_streams` | Per-second value, same pattern as existing stream columns (hr, power, cadence, etc.) |
| **Computation trigger** | During `recalculate_fitness`, after CP estimate. Backfill rides where `w_bal IS NULL` and power stream exists | Same pattern as NP/EF computation in Step 1 |
| **METRICS_VERSION** | No bump needed — `w_bal IS NULL` filter handles backfill naturally | Same reasoning as CP: new column, not changed calculation on existing columns |
| **Dashboard** | New timeseries panel on Activity Details + two stat cards (min W'bal, time below 25%) | Matches GoldenCheetah/intervals.icu layout |

## Architecture

### Skiba Differential Model

```
For each second t in the ride:
    if P(t) > CP:
        W'bal(t) = W'bal(t-1) - (P(t) - CP)       # draining
    else:
        tau = 546 * exp(-0.01 * (CP - P(t))) + 316  # GoldenCheetah tau
        W'bal(t) = W' - (W' - W'bal(t-1)) * exp(-1/tau)  # refilling exponentially

    W'bal(0) = W'   (start full)
    W'bal(t) = max(W'bal(t), 0)       (can't go negative)
    W'bal(t) = min(W'bal(t), W')      (can't exceed W' on recovery)
```

**Tau formula:** `546 * e^(-0.01 * (CP - P)) + 316` is the empirical recovery time constant from Skiba et al., as implemented in GoldenCheetah. Higher power below CP = faster recovery (shorter tau). The constants are fixed — not user-configurable.

### Data Flow

```
recalculate_fitness runs (daily / startup)
  ↓ existing: compute CP estimate → cp_estimates row for today
  ↓ NEW: compute_wbal_for_rides(conn)
    ↓ get latest cp_estimates row (cp_watts or fallback_ftp, w_prime_kj or 20.0)
    ↓ find rides with power streams where w_bal IS NULL
    ↓ for each ride:
      ↓ read power stream from activity_streams
      ↓ compute W'bal per second via Skiba differential
      ↓ batch UPDATE activity_streams SET w_bal = ... for each row
```

### Components

**1. `ingestor/critical_power.py`** (modify — add pure function)

```python
def compute_wbal(
    powers: list[float], cp: float, w_prime_j: float
) -> list[float]:
    """Compute per-second W'bal using Skiba differential model.

    Args:
        powers: per-second power values (watts)
        cp: Critical Power (watts)
        w_prime_j: W' in joules (NOT kJ)

    Returns:
        list of W'bal values (joules), same length as powers.
        W'bal starts at w_prime_j and is clamped to [0, w_prime_j].
    """
```

This is a pure function — no DB, no I/O. Input is a power stream + CP + W', output is a W'bal stream. Tested with synthetic data.

**2. `ingestor/fitness.py`** (modify — add orchestrator)

```python
def compute_wbal_for_rides(conn) -> int:
    """Compute W'bal for rides that don't have it yet.

    Reads CP/W' from the latest cp_estimates row. For rides with power
    streams where w_bal IS NULL, computes per-second W'bal via Skiba
    differential and writes it back to activity_streams.

    Returns the number of rides processed.
    """
```

Gets CP/W' from `cp_estimates` (latest row). When `w_prime_kj IS NULL` (fallback rows), defaults to 20.0 kJ. When `cp_watts IS NULL`, uses `fallback_ftp`. Processes rides in chronological order.

**3. `ingestor/db.py`** (modify — schema)

```sql
ALTER TABLE activity_streams ADD COLUMN IF NOT EXISTS w_bal FLOAT;
```

**4. Activity Details dashboard panels**

**W'bal timeseries** — full-width panel below the existing power/HR traces. Shows W'bal in kJ on the Y-axis, time/distance on the X-axis (matching existing telemetry panels). Shaded red below 25% of W'. Power trace overlaid as a secondary series for context.

Query:
```sql
SELECT s.time_offset AS time,
  ROUND((s.w_bal / 1000.0)::numeric, 1) AS "W'bal (kJ)",
  s.power AS "Power (W)"
FROM activity_streams s
WHERE s.activity_id = ${activity_id}
  AND s.w_bal IS NOT NULL
ORDER BY s.time_offset;
```

**Min W'bal stat card** — smallest W'bal value during the ride. Shows how close to empty the rider got.

```sql
SELECT ROUND((MIN(s.w_bal) / 1000.0)::numeric, 1) AS "Min W'bal (kJ)"
FROM activity_streams s
WHERE s.activity_id = ${activity_id}
  AND s.w_bal IS NOT NULL;
```

**Time below 25% stat card** — percentage of ride time where W'bal was below 25% of W'. Shows how much of the ride was "in the red."

```sql
SELECT ROUND(
  (COUNT(*) FILTER (WHERE s.w_bal < (
    SELECT COALESCE(w_prime_kj, 20.0) * 1000.0 * 0.25
    FROM cp_estimates ORDER BY date DESC LIMIT 1
  )) * 100.0 / NULLIF(COUNT(*), 0))::numeric, 1
) AS "Time below 25% (%)"
FROM activity_streams s
WHERE s.activity_id = ${activity_id}
  AND s.w_bal IS NOT NULL;
```

**Note:** The time-below-25% SQL uses the latest `cp_estimates` W' for the 25% threshold, but the stored `w_bal` values were computed with whatever CP/W' was current at computation time. If W' changes between computation and query, the threshold won't exactly match the W' used to generate the stored values. This is a known approximation acceptable for v1 — the discrepancy is small for slowly-changing CP and the stat is directional, not precise.

## File Map

| File | Action | Responsibility |
|---|---|---|
| `ingestor/critical_power.py` | Modify | Add `compute_wbal` pure function |
| `ingestor/fitness.py` | Modify | Add `compute_wbal_for_rides` orchestrator, wire into `recalculate_fitness` after CP estimate |
| `ingestor/db.py` | Modify | Add `w_bal FLOAT` column to `activity_streams` |
| `tests/test_critical_power.py` | Modify | Add tests for `compute_wbal` |
| `tests/test_fitness_recalc.py` | Modify | Update mock cursor sequence for new W'bal step |
| `grafana/dashboards/activity.json` | Modify | Add W'bal timeseries panel + Min W'bal stat + Time below 25% stat |
| `CLAUDE.md` | Modify | Add W'bal to Metrics section |
| `README.md` | Modify | Add W'bal to metrics list |

## Test Plan

### Unit (pure functions)

- `compute_wbal` — constant power below CP returns W' unchanged (no drain)
- `compute_wbal` — constant power above CP drains linearly at `(P - CP)` joules per second
- `compute_wbal` — power above CP then below CP shows drain then exponential recovery
- `compute_wbal` — W'bal never goes below 0 (clamped)
- `compute_wbal` — W'bal never exceeds W' (clamped on recovery)
- `compute_wbal` — empty stream returns empty list
- `compute_wbal` — known synthetic example matches hand-computed values

### Integration

- `compute_wbal_for_rides` — mocked DB with synthetic stream → w_bal values written
- No CP estimates in DB → function skips gracefully
- Ride without power stream → skipped

### Dashboard

- `tests/test_dashboards.py` — new panels have unique IDs, valid gridPos

### Manual verification (post-merge)

- Open an Activity Details ride with power data → W'bal chart visible
- Check min W'bal and time-below-25% stat cards show values
- Compare W'bal drain/refill pattern against the power trace visually

## Risks and Mitigations

| Risk | Mitigation |
|---|---|
| **Large batch update** (per-second writes for all historical rides) | Process one ride at a time, commit per ride. Same pattern as NP computation in Step 1 |
| **Default W'=20kJ is inaccurate for some riders** | Shape is correct even with wrong magnitude. As CP fit improves, W'bal accuracy improves automatically. User can manually check `cp_estimates.w_prime_kj` to see if CP fit is active |
| **No CP estimate at all** (empty `cp_estimates` table) | `compute_wbal_for_rides` short-circuits if no CP row exists. Rides get W'bal once the first CP estimate is computed |
| **Performance on large streams** | `compute_wbal` is a simple O(n) loop over the power stream. For a 4-hour ride (14400 samples), this is trivially fast |

## Out of Scope

- **W'bal-based pacing recommendations** — future feature, not in this spec
- **Real-time W'bal during live rides** — VeloMate is post-ride analytics only
- **Custom tau parameters** — use GoldenCheetah's empirical constants for now
- **W'bal on the Overview dashboard** — per-ride metric, belongs on Activity Details
- **Recalculating W'bal when CP changes** — would require METRICS_VERSION bump to reset all w_bal. Deferred — the initial CP is good enough for the "battery shape" insight, and W'bal for already-computed rides can be manually reset if needed

## Rollback

Revert the branch. The `w_bal` column on `activity_streams` is harmless if left populated (no other code reads it). Dashboard panels disappear with the JSON revert.
