# CP / W' Foundation — Design Spec

**Date:** 2026-04-11
**Cluster:** B (Performance Modeling) — phase 1 of 3
**Status:** Designed, awaiting review

## Goal

Add Critical Power (CP) and W' (anaerobic work capacity) modeling as the new authoritative algorithmic FTP estimate, replacing the rolling 20-min × 0.95 calculation in `sync_state.estimated_ftp`. Display CP/W' progression on All Time Progression. Gracefully fall back to the old calculation when fit quality is poor, so the system never gets worse than today.

This is phase 1 of Cluster B. W'bal time series per ride and durability curves are explicitly out of scope for this spec — they will be separate spec/PR cycles after this lands and CP estimates have been validated against real data.

## Background

VeloMate currently estimates FTP from a single data point: the best 20-min mean power in the last 90 days × 0.95. This is Coggan's 1990s simplification, designed for an era when CP modeling was inaccessible. Modern platforms (intervals.icu, GoldenCheetah, WKO5) all use Critical Power models because they:

1. **Use multiple data points** — fit a curve through best efforts at several durations rather than relying on one 20-min effort
2. **Carry physiological meaning** — CP is the boundary between heavy and severe exercise, where blood lactate stabilises rather than accumulates
3. **Separate aerobic from anaerobic** — W' (the area above CP) is the anaerobic reservoir, distinct from sustainable power
4. **Are noise-resistant** — a single hard pull doesn't shift the estimate

The user has been tracking estimated_ftp via the existing rolling 20-min path. Replacing it with CP gives a more rigorous estimate without changing TSS calculations (the user has `VELOMATE_FTP` configured, so the algorithmic estimate is purely a diagnostic alongside the configured value on the Overview FTP comparison panel).

## Key Decisions

| Decision | Choice | Reason |
|---|---|---|
| **Scope** | CP/W' foundation only (no W'bal, no durability) | Smaller PR, lower risk, validates fit quality on real data before building dependent features |
| **Model** | Monod-Scherrer 2-parameter only (Morton 3-parameter deferred) | Linear fit, no scipy dependency, simpler to validate. Morton can be added later if Monod proves insufficient |
| **Integration with TSS** | None — CP replaces only the auto-estimate path, configured FTP still wins | User has `VELOMATE_FTP=175` configured, so CP becomes diagnostic only. No risk to existing TSS values |
| **Integration with `estimated_ftp`** | CP replaces rolling 20-min × 0.95 as the source of `sync_state.estimated_ftp` | One concept (Est. FTP) instead of two diagnostic values |
| **Quality gating** | R² ≥ 0.9 AND ≥ 4 distinct durations contributing → use CP. Otherwise fall back to rolling 20-min | Graceful degradation; never worse than today |
| **Fit window** | 90 days primary, 180 days fallback if 90-day fit fails quality gate | Prefer current data; widen window only when needed |
| **Update cadence** | Daily, alongside existing fitness recalc (00:05) | Cheap to recompute once per day; same trigger as existing metrics |
| **Dependencies** | numpy only (no scipy) | Monod-Scherrer is linear in 1/t — pure numpy `polyfit` is sufficient |

## Architecture

### Data Flow

```
[ingest pipeline]
  ↓ (existing) recalculate_fitness runs daily 00:05 / on startup / on METRICS_VERSION bump
  ↓ NEW: compute_cp_estimate(conn) — runs at the end of recalculate_fitness
    ↓ extract mean maximal power per ride (from activity_streams) for last 90 days
    ↓ aggregate to find period maxima at standard durations [60s, 120s, 300s, 600s, 1200s]
    ↓ fit Monod-Scherrer (linear regression on (1/t, P) points)
    ↓ assess fit quality (R² + duration count)
    ↓ if good: source="cp", value=fitted CP
    ↓ if bad: try 180-day window
    ↓ if still bad: source="20min_fallback", value=rolling 20-min × 0.95
    ↓ INSERT into cp_estimates (today's date, all values)
    ↓ UPDATE sync_state.estimated_ftp, estimated_ftp_source, estimated_cp_quality
```

### Components

**1. `ingestor/critical_power.py`** (new pure-function module)

```python
def compute_mean_maximal_power(stream_powers: list[int], duration_s: int) -> float | None:
    """Best mean power over a sliding window of duration_s seconds.
    Returns None if stream is shorter than duration_s."""

def fit_monod_scherrer(efforts: list[tuple[int, float]]) -> tuple[float, float, float]:
    """Fit P = W'/t + CP via linear regression on (1/t, P) points.
    Returns (cp_watts, w_prime_kj, r_squared).
    Requires >= 2 efforts; returns (None, None, None) otherwise."""

def assess_fit_quality(r_squared: float, duration_count: int) -> bool:
    """True iff fit is trustworthy (R² >= 0.9 AND >= 4 distinct durations)."""
```

These are pure functions — no DB, no I/O. Tested in isolation with synthetic data where the true CP/W' are known.

**2. `cp_estimates` table** (new schema)

```sql
CREATE TABLE IF NOT EXISTS cp_estimates (
    date            DATE PRIMARY KEY,
    cp_watts        FLOAT,
    w_prime_kj      FLOAT,
    r_squared       FLOAT,
    period_days     INTEGER,    -- 90 or 180
    duration_count  INTEGER,    -- distinct durations contributing
    source          TEXT,       -- 'cp' or '20min_fallback'
    fallback_ftp    FLOAT,      -- rolling 20-min × 0.95 always computed for comparison
    updated_at      TIMESTAMPTZ DEFAULT NOW()
);
```

One row per day. Upsert pattern (`ON CONFLICT (date) DO UPDATE`). Historical rows kept for the trend chart. The `fallback_ftp` column is always populated regardless of source so the trend chart can show both.

**3. `compute_cp_estimate(conn)` step in `ingestor/fitness.py`**

Runs at the end of `recalculate_fitness`, after the existing steps. Reads from `activity_streams`, fits CP, decides source, writes to `cp_estimates` and `sync_state`. It also calls (or wraps) the existing `estimate_ftp()` function to compute the fallback rolling 20-min × 0.95 value, which is always stored in `cp_estimates.fallback_ftp` regardless of which source wins. The existing `estimate_ftp()` function is preserved unchanged — it becomes the fallback path, called from inside `compute_cp_estimate`.

**4. Updated `sync_state` keys**

- `estimated_ftp` — value to display (existing key, now sourced from CP if quality good)
- `estimated_ftp_source` — `"cp"` or `"20min"` (new)
- `estimated_cp_quality` — R² as a float string (new)
- `estimated_cp_w_prime_kj` — W' in kJ as a float string (new) — for the badge

Grafana reads these directly. Existing FTP comparison panel logic doesn't need to change beyond an updated description and optional badge.

**5. Dashboard changes**

**All Time Progression — new panel pair in the Performance Progression section:**
- **CP / W' Progression** (timeseries, w=12) — `cp_watts` and `w_prime_kj` (dual axis) over time, sourced from `cp_estimates` table. Filtered to `source = 'cp'` to avoid showing fallback values. Shows quality of progression.
- **Power-Duration Curve** (timeseries or scatter, w=12) — current period's mean maximal power points + fitted Monod-Scherrer curve overlay. Visually shows the fit quality.

**Placement:** Insert as a new row immediately after the existing Aerobic Decoupling Trend / NP/kg Trend pair (currently at y=28 in the Performance Progression section), before the Weekly Power Range full-width panel. Both new panels are w=12 for consistency with the rest of the section. All Y coordinates downstream of the insertion point shift by +8 to make room.

**Overview — Est. FTP panel update:**
- Description updated to mention "Source: Critical Power model (when R² ≥ 0.9), else rolling 20-min × 0.95 fallback"
- Optionally add a small text/badge below the value showing the current source. For YAGNI: skip the badge in v1, just update the description tooltip.

**6. Module dependencies**

- numpy (already in `requirements.txt` for fitness calculations)
- No scipy (Monod-Scherrer is linear regression, numpy `polyfit` handles it)
- No new dependencies in this spec

### Quality gating logic

```
def derive_estimated_ftp(conn):
    # Try 90-day window
    cp, w_prime, r2, durations = fit_period(conn, days=90)
    if assess_fit_quality(r2, len(durations)):
        return ("cp", cp, w_prime, r2, 90)

    # Try 180-day window
    cp, w_prime, r2, durations = fit_period(conn, days=180)
    if assess_fit_quality(r2, len(durations)):
        return ("cp", cp, w_prime, r2, 180)

    # Fall back to existing rolling 20-min × 0.95
    fallback = compute_rolling_20min_ftp(conn)
    return ("20min_fallback", fallback, None, r2, 90)
```

The `fallback_ftp` (rolling 20-min) is also computed unconditionally on every run and stored in `cp_estimates.fallback_ftp` so the user can see what it would have been.

### Standard duration buckets

For fitting, extract mean maximal power at **5 standard durations**: 60s, 120s, 300s, 600s, 1200s (1, 2, 5, 10, 20 minutes).

The quality gate requires **≥ 4 distinct durations contributing** — so 4 out of 5 buckets must have at least one ride producing a max effort at that duration. This allows for one missing bucket (e.g., a rider who never holds 20-min max efforts) without rejecting the fit entirely.

Why these durations:
- All are in the "heavy/severe" exercise domain where the CP model holds
- Sub-60s efforts are dominated by neuromuscular factors and W' rather than CP — including them distorts the fit
- > 20-min efforts have insufficient density in most riders' data

The Monod-Scherrer model assumes infinite-duration sustainable power (CP). Including very short or very long efforts violates the model's assumptions. 1–20 minutes is the sweet spot used by GoldenCheetah and intervals.icu.

### When quality gate fails

The user has 12 rides with an urban-surge-dominant power profile (verified earlier). Maximal efforts at 5/10/20 min are likely scarce. Initial CP fits will probably fail the quality gate (R² < 0.9 or < 4 distinct durations).

This is the **expected** behavior:
- The system gracefully falls back to the existing rolling 20-min calculation
- The displayed `estimated_ftp` is unchanged from today's value
- The `cp_estimates` table records `source = "20min_fallback"` so the user can see CP was attempted but didn't qualify
- As more varied training accumulates, CP becomes the source automatically — no manual switch

The graceful fallback is the entire point: **CP only takes over when it's genuinely better than the alternative.**

## File Map

| File | Action | Responsibility |
|---|---|---|
| `ingestor/critical_power.py` | **Create** | Pure functions: `compute_mean_maximal_power`, `fit_monod_scherrer`, `assess_fit_quality` |
| `ingestor/db.py` | Modify | Add `CREATE TABLE IF NOT EXISTS cp_estimates ...` to schema DDL |
| `ingestor/fitness.py` | Modify | New `compute_cp_estimate(conn)` function called at end of `recalculate_fitness` |
| `grafana/dashboards/all-time-progression.json` | Modify | Add CP/W' Progression panel + Power-Duration Curve panel in Performance Progression section |
| `grafana/dashboards/overview.json` | Modify | Update `Est. FTP` panel description to mention CP source + fallback |
| `tests/test_critical_power.py` | **Create** | Pure-function tests with synthetic data (known CP/W' → recover them); edge cases (insufficient durations, perfect linear, noisy) |
| `tests/test_fitness_recalc.py` | Modify | Update mock cursor sequence for the new `compute_cp_estimate` step |
| `CLAUDE.md` | Modify | Add CP/W' to Metrics section, document the quality gate decision |
| `README.md` | Modify | Add CP/W' to the metrics list |

## Test Plan

### Unit (pure functions)

- `compute_mean_maximal_power` — synthetic flat power stream returns the flat value; ramping stream returns the highest window; stream shorter than duration returns None
- `fit_monod_scherrer` — synthetic data generated from known CP=200, W'=15kJ at 5 durations should recover (200, 15, ~1.0)
- `fit_monod_scherrer` — noisy synthetic data should still recover within tolerance with R² ~0.95
- `fit_monod_scherrer` — fewer than 2 efforts returns (None, None, None)
- `assess_fit_quality` — boundary cases (R²=0.89 fails, R²=0.9 passes; 3 durations fails, 4 passes)

### Integration

- `compute_cp_estimate(conn)` — mocked DB returns synthetic stream data → CP estimate stored, sync_state updated
- Quality gate fallback — synthetic poor-quality data → source="20min_fallback", fallback_ftp stored
- Empty database — no power streams → no CP, no fallback either, leaves estimated_ftp as-is

### Dashboard

- `tests/test_dashboards.py` — both new panels have unique IDs, valid gridPos, no overlap

### Manual verification (post-merge)

- Run on real production DB → check `cp_estimates` table has a row for today
- Verify `estimated_ftp` value in sync_state matches what's displayed on Overview
- Inspect new panels on All Time Progression — CP/W' Progression should show at least one data point, Power-Duration Curve should show the fitted line if quality is good

## Risks and Mitigations

| Risk | Mitigation |
|---|---|
| **Initial fit quality is poor on user's dataset** | Expected — graceful fallback handles it; user sees no degradation |
| **Stream extraction is slow** (computing best 60/120/300/600/1200s windows across many rides) | Cache mean maximal power per ride in a separate table or column on first compute. For initial implementation: compute on-the-fly during daily recalc, profile if slow. SQL with window functions over `activity_streams` should be fast enough for 12-month windows |
| **Linear regression on (1/t, P) is sensitive to outliers** | Mitigated by R² gating — bad fits are rejected. Could add outlier rejection (e.g., remove points with residuals > 2σ) in a future iteration |
| **`numpy.polyfit` returns nonsense for degenerate input** | Pure-function tests cover edge cases; the integration call wraps it in try/except and falls back gracefully |
| **`cp_estimates` table grows unbounded** | One row per day = 365/year. Trivial. No retention policy needed |
| **METRICS_VERSION bump needed for backfill?** | No — new column on existing table not required, only a new table. Existing rides are not affected. The CP computation runs against existing data immediately |

## Out of Scope (explicitly)

- **W'bal time series per ride** (Cluster B item 2) — separate spec/PR after this validates
- **Fresh vs fatigued power-duration curves** (Cluster B item 3) — separate spec/PR after #2
- **Athlete type classification** (sprinter/rouleur/TT from W'/CP ratio) — gap #17, low priority, deferred
- **Morton 3-parameter model** — adds scipy dependency, only useful for sub-2-minute data, defer until needed
- **Per-ride CP estimates** — too noisy to be useful; CP is inherently a multi-ride aggregate
- **Replacing `ride_ftp` with CP** — `ride_ftp` is per-ride and historical; CP is current. Different concepts. Not touching `ride_ftp`
- **Outlier rejection in the fit** — start without it; add only if real-world fits show problems

## Rollback

Revert the branch. The new table is harmless if left in place (no data dependencies elsewhere). `sync_state.estimated_ftp` reverts to whatever the existing rolling 20-min calculation produces. No data corruption risk because the new table is purely additive and the new sync_state keys are read-only by Grafana.

## Open Questions

None — all design decisions resolved during brainstorming. Ready to write implementation plan.
