# Overview + Training Report Dashboard Split

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or superpowers:executing-plans to implement this plan task-by-task when ready. This document is a design spec + high-level task list; the detailed TDD tasks should be expanded at execution time.

**Goal:** Split the Overview dashboard into two surfaces — a lean daily-glance Overview and a new Training Report for weekly/monthly review — to resolve the "too dense" and "sparse third row" issues on the current Overview.

**Status:** Designed. Approved for execution. Deferred to a later session.

**Architecture:** Two Grafana dashboards, each answering a single coherent question. Overview = "am I OK today? what did I ride recently?" Training Report = "how did this period go vs last? what are my trends?" The existing All Time Progression and Activity Details dashboards are untouched.

**Tech Stack:** Grafana 12.4 dashboard JSON, Postgres-backed panels (no ingestor or schema changes).

---

## Background

This plan is the follow-up to PR #92 (Overview polish). After PR #92 landed, feedback surfaced two problems that the collapsible-rows approach didn't fully solve:

1. **Sparse third rows.** Period Summary and vs Previous Period each gained a third row for Avg Decoupling / Δ Avg Decoupling, but those rows have only 1 of 4 cells filled. Visually awkward, and resizing everything to fit would churn too many existing panels.
2. **Overview is doing too many jobs.** It tries to be both the daily training-state view AND the weekly/monthly review. Density keeps growing as new metrics get added (wellness readiness score, monotony/strain, etc. are all on the backlog).

The split is the convergent answer used by every leading platform: Strava (Dashboard vs Training Log), intervals.icu (Home vs Training), TrainingPeaks (Dashboard vs Calendar).

## Information Architecture — Before / After

### Before (current state, post PR #92)

One Overview dashboard with 8 row sections, 44 panels:
- Period Summary (9 stats including Avg Decoupling, sparse 3rd row)
- vs Previous Period (9 deltas including Δ Avg Decoupling, sparse 3rd row)
- Fitness (CTL/ATL/TSB + form + FTP + streak + gauge + pie + fitness Δ)
- Ride Patterns (1 barchart, collapsed)
- Trends (6 timeseries, collapsed)
- Outdoor Records (1 table, collapsed)
- Activities (1 table)
- Ride Map (1 geomap, collapsed)

### After

**Overview** (lean, ~15 panels, single question: "am I OK right now?"):
- Fitness row (unchanged): CTL/ATL/TSB chart + form gauge + FTP + Weekly Streak + Days Since Ride + 6w Fitness Δ + Ride Types pie
- This Period row (trimmed to 4 core stats): Rides / Distance / Hours / TSS at w=6 each (fills 24 columns cleanly, no sparse row)
- Activities table (unchanged)
- Default time range: `now-30d` (unchanged from PR #92)

**Training Report** (new dashboard, ~30 panels, single question: "how did this period go vs last?"):
- Period Summary — all 9 stats in a **2×5 layout** at w=4 (20/24 width, small right gutter). Stats: Rides, Distance, Elevation, Duration, TSS, Avg Power, Avg HR, Avg Speed, Avg Decoupling.
- vs Previous Period — all 9 deltas in the same 2×5 layout, mirroring Period Summary above.
- Trends row (expanded by default) — the 6 timeseries: Distance & Elevation, Duration & TSS, Avg Power & Avg HR, Avg Speed & Avg Cadence, Calories & Rides, Rolling Weekly Volume.
- Ride Patterns (expanded) — When You Ride barchart.
- Outdoor Records (expanded) — the full records table.
- Ride Map (expanded) — lifetime heatmap.
- Default time range: `now-30d` (matches Overview).

**All Time Progression** (unchanged) — the long-term multi-year view.

**Activity Details** (unchanged) — click-through from any activity list.

## Why this architecture

1. **Fixes the sparse row** — the 2×5 layout at w=4 has no empty cells in the last row (9 stats fit into 10 slots, leaving 1 gutter cell that's aesthetically fine).
2. **Fixes "too dense"** — Overview drops from 44 panels to ~15. Training Report holds the rest.
3. **Matches industry convention** — every leading platform uses a two-surface daily/review split.
4. **No panels deleted** — everything moves somewhere. Rollback is "revert the branch".
5. **Scales for future metrics** — wellness readiness (gap #2), monotony/strain (gap #9), CP/W' (gap #3), W'bal (gap #4), and other planned features have a clear home (Training Report usually; Overview only if genuinely daily-relevant).
6. **Clean navigation** — 4 dashboards, 4 questions: Overview (today), Training Report (this period), All Time Progression (multi-year), Activity Details (one ride).

## Decisions (confirmed with user)

- **Name**: "Training Report" (alternatives considered: Weekly Review, Training Log, Period Report, Training Analysis).
- **Overview "This Period" stats**: exactly 4 — Rides, Distance, Hours, TSS. One row, 4×w=6.

## File Map

| File | Action | Responsibility |
|---|---|---|
| `grafana/dashboards/training-report.json` | **Create** | New dashboard with Period Summary (2×5), vs Previous Period (2×5), Trends (6 expanded), Ride Patterns, Outdoor Records, Ride Map |
| `grafana/dashboards/overview.json` | Modify | Trim to Fitness + This Period (4 stats) + Activities. Remove everything else. Update nav links. |
| `grafana/provisioning/dashboards/dashboards.yml` | Modify (if needed) | Confirm `training-report.json` is picked up by the provisioning path (likely auto — the existing config usually globs `*.json`). |
| `tests/test_dashboards.py` | No change | `iter_all_panels()` already added in PR #92 recurses into nested row panels. The parametrized tests will cover the new dashboard automatically. |

## High-level Task List (to be expanded at execution time)

1. **Create branch** `feat/dashboards-split-training-report`
2. **Verify provisioning behaviour** — read `grafana/provisioning/dashboards/*.yml` to confirm `training-report.json` will auto-load without a provisioning edit. If not, add the necessary entry.
3. **Author `training-report.json`** — copy Overview as a starting template, update title/uid/time, populate panels per the Training Report section above. Reorganize Period Summary and vs Previous Period into 2×5 layouts at w=4.
4. **Add cross-dashboard nav links** to `training-report.json` — links to Overview, All Time Progression, Activity Details (pattern matches the existing links in other dashboards).
5. **Trim `overview.json`**:
   - Delete panels now on Training Report: full vs Previous Period section, Trends row + children, Ride Patterns row + children, Outdoor Records row + children, Ride Map row + children. Also delete Avg Decoupling (id 1011) and Δ Avg Decoupling (id 1012) since they live on Training Report now.
   - Reshape Period Summary to a single 4-stat row (Rides/Distance/Hours/TSS). Delete TSS/Avg Power/Avg HR/Avg Speed/Avg Decoupling that used to be the second row.
   - Wait — we're keeping TSS in the 4 core. So delete Avg Power/Avg HR/Avg Speed/Avg Decoupling but keep Rides/Distance/Duration/TSS. Then rename "Duration" to "Hours" if the existing card doesn't already show hours.
   - Actually re-check: user said 4 core stats = Rides, Distance, Hours, TSS. The existing "Duration" panel shows total duration. If it's in a human-readable format (HH:MM:SS or "18h 30m") that's fine — call it "Duration" in the UI but conceptually it's the hours stat. No rename needed.
   - Shift remaining panels (Fitness row and Activities) up to close the gap.
   - Update nav links to include Training Report.
6. **Run dashboard tests** — `python3 -m pytest tests/test_dashboards.py -q` should report all passing including the new Training Report file.
7. **Local sanity check** — load both dashboards in a local Grafana (or visually confirm JSON is well-structured via Python spot checks).
8. **Commit 1**: `feat(dashboards): add Training Report dashboard for period comparison + trends`
9. **Commit 2**: `refactor(dashboards): trim Overview to daily-glance view`
10. **Push branch, open PR via tea** with test plan covering both dashboards.
11. **Server-side verification post-merge** — open both dashboards in Grafana, confirm navigation works, confirm all panels render, confirm no broken queries.
12. **Merge via Raven + cleanup**.

## Test Plan

Local (pre-PR):
- [ ] `python3 -m pytest tests/test_dashboards.py -q` — all pass (should include the new file in parametrized runs)
- [ ] JSON validity and panel ID uniqueness for both `overview.json` and `training-report.json` via Python spot checks
- [ ] Panel count reduction on Overview verified (from 44 to ~15)
- [ ] Panel count on Training Report reasonable (~25-30)

Server-side (post-merge):
- [ ] Open Overview — verify only 15-ish panels, no vs Previous Period section, 4-stat Period Summary row, Fitness + Activities still present
- [ ] Open Training Report via nav link — verify Period Summary (2×5), vs Previous Period (2×5), Trends expanded with all 6 charts, Ride Patterns/Outdoor Records/Ride Map all visible
- [ ] Cross-dashboard nav links work in both directions
- [ ] Time ranges respected on both dashboards
- [ ] Sport type template variable works on both dashboards

## Follow-ups / Out of Scope for this Plan

Things explicitly NOT in scope for the split. Track separately if needed:

- **"Sample too small" warning on delta cards.** When either period has fewer than ~3 qualifying rides, the delta is noisy. Could show a warning icon or suppress the card. Minor UX polish — do only if it continues to confuse after the split lands.
- **Consolidating period + delta into a single table panel** (the Option 2 from our earlier discussion). Still an interesting alternative; revisit only if the 2×5 grid layout on Training Report turns out to be too wide or too noisy in practice.
- **Activity Details dashboard changes.** Out of scope — it's already single-purpose.
- **All Time Progression dashboard changes.** Out of scope — it's already the multi-year view.
- **Mobile responsiveness.** Grafana's grid is what it is. Don't try to optimize for mobile in this plan.

## Rollback

Revert the branch. All changes are dashboard JSON + provisioning config. No schema, no ingestor code, no production data touched.
