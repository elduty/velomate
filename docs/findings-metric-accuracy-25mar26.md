# Metric Accuracy Findings — 25 Mar 2026

Root-cause analysis of TRIMP, TSS, and IF discrepancies compared to
GoldenCheetah (22 Mar 2026 ride). All issues resolved in PRs #80-86.

## Golden Record Reference (GoldenCheetah, FTP=245)

| Metric            | Value   | Notes                          |
|-------------------|---------|--------------------------------|
| Duration          | 1:56:25 |                                |
| Avg Power         | 109 W   |                                |
| Avg HR            | 144 bpm |                                |
| Max HR            | 175 bpm |                                |
| IsoPower (NP)     | 164 W   | Originally misread as 118 (CP) |
| CP Estimate       | 118 W   | Not NP — different metric      |
| BikeIntensity (IF)| 0.458   | Uses xPower/CP, not NP/FTP     |
| VI                | 1.549   | Uses xPower, not NP            |
| EF                | 1.140   | Uses xPower, not NP            |
| TSS               | 87      | Coggan TSS with FTP=245        |
| Decoupling        | 8.3 %   |                                |

## Resolved: No single source of truth (PR #80)

Grafana panels recomputed NP, EF, VI, IF, TRIMP from streams instead
of reading stored values. Fixed: ingestor computes everything, Grafana
reads from activities table.

## Resolved: IF used global FTP, not per-ride FTP (PR #80)

TSS used `ride_ftp` but IF used current `estimated_ftp`. Fixed: both
use `ride_ftp`.

## Resolved: TRIMP exponential blowup (PR #80)

HRR not capped at 1.0 — HR above max_hr caused exponential explosion.
Fixed: HRR capped in `compute_trimp()`.

## Resolved: Inconsistent FTP fallback chains (PR #80)

Different panels used different FTP resolution. Fixed: all panels use
`configured_ftp → estimated_ftp → 150`.

## Resolved: Configured FTP ignored by backfill (PR #82)

Setting `VELOMATE_FTP` didn't affect historical rides because the
stream-based backfill overrode it. Fixed: when FTP is configured,
stamp all rides directly.

## Resolved: NP algorithm (PR #85 → reverted in PR #86)

NP was temporarily changed to EWMA based on a misread golden record
(CP=118 was confused with NP=164). The original 30-second SMA was
correct all along — matches GoldenCheetah's Coggan.cpp implementation.

## Key Lesson: GoldenCheetah metric naming

GC uses different power models for different metrics:
- **IsoPower (NP)**: 30-second SMA, used for Coggan TSS
- **xPower**: 25-second EWMA, used for VI, EF, BikeIntensity, BikeScore
- **CP Estimate**: Critical Power model, independent of NP

Direct comparison requires knowing which metric GC uses for each value.
