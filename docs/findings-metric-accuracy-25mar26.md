# Metric Accuracy Findings — 25 Mar 2026

Root-cause analysis of TRIMP, TSS, and IF discrepancies compared to
GoldenCheetah golden record (22 Mar 2026 ride).

## Golden Record Reference (GoldenCheetah)

| Metric            | Value   |
|-------------------|---------|
| Duration          | 1:56:25 |
| Avg Power         | 109 W   |
| Avg HR            | 144 bpm |
| Max HR            | 175 bpm |
| IsoPower (NP)     | 118 W   |
| BikeIntensity (IF)| 0.458   |
| VI                | 1.549   |
| EF                | 1.140   |
| TSS               | 87      |
| Decoupling        | 8.3 %   |

## Problem 1 — No single source of truth

The ingestor computes and stores NP, EF, TSS, and work_kj. But Grafana
activity-detail panels **recompute NP, EF, VI from raw streams** on every
page load instead of reading stored values. TRIMP and IF are **not stored
at all** — they only exist as inline Grafana SQL.

| Metric | Stored by ingestor? | Grafana reads stored? | Issue                        |
|--------|---------------------|-----------------------|------------------------------|
| NP     | activities.np       | No — recomputes       | Redundant, potential drift   |
| EF     | activities.ef       | No — recomputes       | Redundant                    |
| IF     | **Not stored**      | Computes with global FTP | Wrong FTP for historical rides |
| TRIMP  | **Not stored**      | Computes from streams | No HRR cap — exponential blowup |
| VI     | Not stored          | Recomputes NP inline  | Mixes recomputed + stored    |
| TSS    | activities.tss      | Yes                   | OK                           |

## Problem 2 — IF uses global FTP, TSS uses per-ride FTP

TSS is computed with `ride_ftp` (historical per-ride FTP from 90-day
rolling best at the time of the ride). The Grafana IF panel uses a
fallback chain that ends at current `estimated_ftp`.

For a ride from 3 months ago when FTP was lower, IF is computed with
today's higher FTP (lower IF) while TSS was computed with the correct
historical FTP. **IF and TSS are mathematically inconsistent.**

Example with golden record ride (NP = 118 W):
- If current FTP = 250 W → IF = 0.47, but TSS was computed with ride_ftp = 175 W
- If ride_ftp = 175 W → IF = 0.67 and TSS = 88 (matches GC's 87)

## Problem 3 — TRIMP exponential blowup (no HRR ceiling)

The Banister TRIMP formula uses `exp(1.92 × HRR)` where
HRR = (HR − resting) / (max − resting). When any HR sample exceeds the
auto-estimated max_hr (95th percentile of ride max HRs), HRR > 1.0 and
the exponential explodes.

With max_hr auto-estimated at 172 (95th percentile) and a sample at
180 bpm: HRR = 1.066 → exp(2.05) = 7.77 instead of the correct ceiling
exp(1.92) = 6.82 — **14% inflation per sample**. Many such samples
compound across the ride.

The query also does not filter `s.hr <= max_hr`, so every spike above
the estimated max contributes inflated TRIMP.

## Problem 4 — Inconsistent FTP fallback chains

Different Grafana panels resolve FTP differently:

| Panel                      | Fallback chain                                              |
|----------------------------|-------------------------------------------------------------|
| IF (activity)              | configured_ftp → estimated_ftp → inline 90-day recalc → percentile |
| Power Zones (activity)     | configured_ftp → estimated_ftp → inline 90-day recalc → percentile → 150 |
| Power Zones (monthly)      | estimated_ftp → 150                                         |
| Power Zones by km          | configured_ftp → estimated_ftp → inline recalc (no date filter!) → percentile → 150 |

Panels on the same page can disagree on FTP when some fallbacks hit and
others don't.

## Problem 5 — GoldenCheetah comparison caveat

GoldenCheetah uses **xPower** (Skiba's exponentially-weighted moving
average) internally for VI and EF, not Coggan's NP. That explains:

- GC VI = 1.549 vs Coggan VI = NP / avg_power = 118 / 109 = 1.08
- GC EF = 1.140 vs Coggan EF = NP / avg_hr = 118 / 144 = 0.82

These are different power models. VeloMate follows Coggan/TrainingPeaks
consistently, which is correct — but **direct comparison of VI and EF
against GC won't match**. TSS, IF, and TRIMP *should* match when using
the same FTP and HR parameters.

## Fix Plan

**Principle: ingestor computes everything, Grafana only reads stored values.**

1. Add `intensity_factor` and `trimp` columns to activities table
2. Compute IF = NP / ride_ftp in ingestor (per-ride FTP, consistent with TSS)
3. Compute TRIMP in ingestor (Banister formula, HRR capped at 1.0)
4. Grafana activity panels read stored NP, EF, IF, TRIMP, VI (= stored NP / avg_power)
5. Standardise all remaining FTP references to `estimated_ftp` from sync_state
6. Bump METRICS_VERSION to "5" to trigger full recalculation
7. Pass VELOMATE_RESTING_HR through docker-compose for TRIMP computation
