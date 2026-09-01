#!/usr/bin/env python3
"""Compare VeloMate's computed metrics against intervals.icu's for the same rides.

Read-only reconnaissance for the intervals.icu integration. Reads VeloMate
through the CLI's own config path (~/.config/velomate/config.yaml) and
intervals.icu through ingestor/intervals_icu.py. Writes nothing to either.

See docs/design/specs/2026-08-05-intervals-icu-source-design.md.
"""

import argparse
import csv
import os
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from statistics import median

_INGESTOR = Path(__file__).resolve().parent.parent / "ingestor"
if str(_INGESTOR) not in sys.path:
    sys.path.insert(0, str(_INGESTOR))

import intervals_icu

# Mirrors db.find_duplicate()'s cross-source dedup rule so the comparison
# exercises the same matching logic the integration pass will rely on:
# start within 300s AND (duration within max(300s, 15%) OR distance within 10%).
FUZZY_WINDOW_S = 300
DURATION_TOLERANCE = 0.15
DISTANCE_TOLERANCE = 0.10


@dataclass
class MatchResult:
    pairs: list = field(default_factory=list)      # [(icu_activity, velomate_row)]
    by_strava_id: int = 0
    by_fuzzy: int = 0
    icu_only: list = field(default_factory=list)
    velomate_only: list = field(default_factory=list)


def parse_icu_dt(value):
    """Parse an intervals.icu ISO-8601 timestamp into an aware UTC datetime."""
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _vm_dt(row):
    dt = row.get("date")
    if not isinstance(dt, datetime):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def is_fuzzy_match(icu: dict, vm: dict) -> bool:
    """True when the two records look like the same ride."""
    icu_start = parse_icu_dt(icu.get("start_date") or icu.get("start_date_local"))
    vm_start = _vm_dt(vm)
    if icu_start is None or vm_start is None:
        return False
    if abs((icu_start - vm_start).total_seconds()) >= FUZZY_WINDOW_S:
        return False

    icu_dur = icu.get("moving_time") or 0
    vm_dur = vm.get("duration_s") or 0
    if abs(icu_dur - vm_dur) < max(FUZZY_WINDOW_S, vm_dur * DURATION_TOLERANCE):
        return True

    icu_dist = icu.get("distance") or 0
    vm_dist = vm.get("distance_m") or 0
    if icu_dist > 0 and vm_dist > 0 and abs(icu_dist - vm_dist) < vm_dist * DISTANCE_TOLERANCE:
        return True
    return False


def _as_strava_key(value):
    """Normalise a Strava ID to a string key. intervals.icu returns strings,
    VeloMate stores BIGINT, so they must be compared in one type."""
    if value in (None, ""):
        return None
    return str(value)


def match_activities(icu_activities: list, vm_rows: list) -> MatchResult:
    """Pair intervals.icu activities with VeloMate rows.

    Exact Strava-ID matching wins where intervals.icu received the ride via
    Strava; the fuzzy rule is the fallback for Garmin/Wahoo/Zwift rides that
    never touched Strava. A VeloMate row is never matched twice.
    """
    result = MatchResult()
    by_strava = {}
    for row in vm_rows:
        key = _as_strava_key(row.get("strava_id"))
        if key:
            by_strava[key] = row

    used_ids = set()
    for icu in icu_activities:
        key = _as_strava_key(icu.get("strava_id"))
        row = by_strava.get(key) if key else None
        if row is not None and row["id"] not in used_ids:
            result.pairs.append((icu, row))
            result.by_strava_id += 1
            used_ids.add(row["id"])
            continue

        match = next(
            (r for r in vm_rows if r["id"] not in used_ids and is_fuzzy_match(icu, r)),
            None,
        )
        if match is not None:
            result.pairs.append((icu, match))
            result.by_fuzzy += 1
            used_ids.add(match["id"])
        else:
            result.icu_only.append(icu)

    result.velomate_only = [r for r in vm_rows if r["id"] not in used_ids]
    return result


# Matches fitness.HIGH_VI_THRESHOLD — VeloMate deliberately uses avg_power
# instead of NP above this VI, so urban rides are expected to read lower
# than intervals.icu's straight Coggan TSS. Segmenting keeps that expected
# divergence from contaminating the aggregate.
HIGH_VI_THRESHOLD = 1.30


@dataclass(frozen=True)
class Metric:
    name: str
    vm_field: str
    icu_field: str


METRICS = [
    Metric("np", "np", "icu_weighted_avg_watts"),
    Metric("tss", "tss", "icu_training_load"),
    Metric("intensity_factor", "intensity_factor", "icu_intensity"),
    Metric("variability_index", "variability_index", "icu_variability_index"),
    Metric("ef", "ef", "icu_efficiency_factor"),
    Metric("trimp", "trimp", "trimp"),
    Metric("aerobic_decoupling", "aerobic_decoupling", "decoupling"),
    Metric("work_kj", "work_kj", "icu_joules"),
    Metric("ride_ftp", "ride_ftp", "icu_ftp"),
    Metric("ride_weight", "ride_weight", "icu_weight"),
    Metric("avg_power", "avg_power", "icu_average_watts"),
    Metric("avg_hr", "avg_hr", "average_heartrate"),
    Metric("distance_m", "distance_m", "distance"),
    Metric("duration_s", "duration_s", "moving_time"),
    Metric("elevation_m", "elevation_m", "total_elevation_gain"),

    # Daily-series and stream-derived values. These VeloMate keys are attached
    # to the row by enrich_velomate_rows() in Task 6; compare_metric() returns
    # None for them until then, so this list stays valid either way.
    #
    # intervals.icu carries several CP/W' estimates at once, so ours is compared
    # against each to identify the real analogue rather than assuming one.
    Metric("cp_vs_pm", "cp_watts", "icu_pm_cp"),
    Metric("cp_vs_rolling", "cp_watts", "icu_rolling_cp"),
    Metric("cp_vs_ss", "cp_watts", "ss_cp"),
    Metric("w_prime_vs_pm", "w_prime_kj", "icu_pm_w_prime"),
    Metric("w_prime_vs_ss", "w_prime_kj", "ss_w_prime"),
    # Semantically inverse: ours is the minimum W'bal *remaining*, theirs is the
    # maximum *depletion*. Diagnostic, not an equality check — read the signed mean.
    Metric("min_w_bal", "min_w_bal", "icu_max_wbal_depletion"),
    Metric("ctl", "ctl", "icu_ctl"),
    Metric("atl", "atl", "icu_atl"),
]


@dataclass
class MetricStats:
    name: str
    n: int
    mean_signed_diff: float
    median_pct_diff: float
    within_tolerance_pct: float
    median_ratio: float = None
    unit_flag: str = None


_UNIT_HINTS = (
    (0.001, "VeloMate is ~1000x smaller — likely kJ vs J"),
    (1000.0, "VeloMate is ~1000x larger — likely J vs kJ"),
    (0.01, "VeloMate is ~100x smaller — likely ratio vs percent"),
    (100.0, "VeloMate is ~100x larger — likely percent vs ratio"),
)


def detect_unit_mismatch(median_ratio):
    """Name the scaling factor when two fields disagree by a round multiple."""
    if median_ratio in (None, 0):
        return None
    for factor, label in _UNIT_HINTS:
        if abs(median_ratio / factor - 1) < 0.05:
            return label
    return None


def _number(value):
    """Coerce to float, or None if the value is absent or not numeric."""
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def compare_metric(metric: Metric, pairs: list, tolerance: float = 0.02):
    """Per-metric agreement stats. None when no pair has both values."""
    diffs, pct_diffs, ratios = [], [], []
    for icu, vm in pairs:
        icu_val = _number(icu.get(metric.icu_field))
        vm_val = _number(vm.get(metric.vm_field))
        if icu_val is None or vm_val is None:
            continue
        diffs.append(vm_val - icu_val)
        if icu_val != 0:
            pct_diffs.append(abs(vm_val - icu_val) / abs(icu_val) * 100.0)
            ratios.append(vm_val / icu_val)

    if not diffs:
        return None

    median_ratio = median(ratios) if ratios else None
    within = [p for p in pct_diffs if p <= tolerance * 100.0]
    return MetricStats(
        name=metric.name,
        n=len(diffs),
        mean_signed_diff=sum(diffs) / len(diffs),
        median_pct_diff=median(pct_diffs) if pct_diffs else 0.0,
        within_tolerance_pct=(len(within) / len(pct_diffs) * 100.0) if pct_diffs else 0.0,
        median_ratio=median_ratio,
        unit_flag=detect_unit_mismatch(median_ratio),
    )


def split_by_vi(pairs: list, threshold: float = HIGH_VI_THRESHOLD):
    """Split pairs into (normal VI, high VI). Missing VI counts as normal."""
    normal, urban = [], []
    for pair in pairs:
        vi = _number(pair[1].get("variability_index"))
        (urban if vi is not None and vi > threshold else normal).append(pair)
    return normal, urban


def split_by_ftp_agreement(pairs: list, tolerance_w: float = 1.0):
    """Split pairs into (same FTP, different FTP).

    TSS is FTP-relative, so a TSS difference on a pair with differing FTP is an
    input difference, not a formula difference. A pair missing either FTP counts
    as mismatched because agreement cannot be established.
    """
    matched, mismatched = [], []
    for icu, vm in pairs:
        icu_ftp = _number(icu.get("icu_ftp"))
        vm_ftp = _number(vm.get("ride_ftp"))
        if icu_ftp is None or vm_ftp is None or abs(icu_ftp - vm_ftp) > tolerance_w:
            mismatched.append((icu, vm))
        else:
            matched.append((icu, vm))
    return matched, mismatched


COMPARED_ICU_FIELDS = {m.icu_field for m in METRICS}

# Identity and bookkeeping fields — present on every activity, nothing to learn.
_UNINTERESTING_ICU_FIELDS = {
    "id", "icu_athlete_id", "start_date", "start_date_local", "name",
    "strava_id", "external_id", "created", "type", "source",
}


@dataclass
class FieldFill:
    name: str
    fill_pct: float
    sample: object = None


@dataclass
class ProbeReport:
    activity_types: dict = field(default_factory=dict)
    stream_types: dict = field(default_factory=dict)
    all_null_streams: dict = field(default_factory=dict)


def field_inventory(icu_activities: list, compared: set = None) -> list:
    """Every populated intervals.icu field VeloMate has no equivalent for.

    This is the enrichment shortlist: what they compute that we don't, ranked
    by how often it is actually populated in real data.
    """
    if not icu_activities:
        return []
    skip = (compared if compared is not None else COMPARED_ICU_FIELDS) | _UNINTERESTING_ICU_FIELDS

    counts, samples = {}, {}
    for act in icu_activities:
        for key, value in act.items():
            if key in skip:
                continue
            if value is None or value == [] or value == {}:
                continue
            counts[key] = counts.get(key, 0) + 1
            samples.setdefault(key, value)

    total = len(icu_activities)
    fills = [
        FieldFill(name=key, fill_pct=count / total * 100.0, sample=samples[key])
        for key, count in counts.items()
    ]
    return sorted(fills, key=lambda f: (-f.fill_pct, f.name))


def probe_api(icu_activities: list, streams_by_id: dict) -> ProbeReport:
    """Resolve the two facts the OpenAPI spec leaves untyped.

    Activity.type and ActivityStream.type are both bare strings with no enum,
    so the real values are discovered from live data here and the integration
    pass is written against the answer instead of a guess.
    """
    report = ProbeReport()
    for act in icu_activities:
        kind = act.get("type")
        if kind:
            report.activity_types[kind] = report.activity_types.get(kind, 0) + 1

    for streams in streams_by_id.values():
        for stream in streams or []:
            name = stream.get("type") or stream.get("name")
            if not name:
                continue
            report.stream_types[name] = report.stream_types.get(name, 0) + 1
            if stream.get("allNull"):
                report.all_null_streams[name] = report.all_null_streams.get(name, 0) + 1
    return report


def fetch_cp_estimates(conn, oldest: str) -> dict:
    """CP and W' keyed by date. VeloMate stores one row per day."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT date, cp_watts, w_prime_kj FROM cp_estimates WHERE date >= %s",
            (oldest,),
        )
        return {r[0]: {"cp_watts": r[1], "w_prime_kj": r[2]} for r in cur.fetchall()}


def fetch_athlete_stats(conn, oldest: str) -> dict:
    """CTL and ATL keyed by date."""
    with conn.cursor() as cur:
        cur.execute("SELECT date, ctl, atl FROM athlete_stats WHERE date >= %s", (oldest,))
        return {r[0]: {"ctl": r[1], "atl": r[2]} for r in cur.fetchall()}


def fetch_wbal_minima(conn, oldest: str) -> dict:
    """Lowest W'bal per activity — the ride-level analogue of intervals.icu's
    icu_max_wbal_depletion, which they report as a depletion rather than a
    remaining value."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT s.activity_id, MIN(s.w_bal) FROM activity_streams s "
            "JOIN activities a ON a.id = s.activity_id "
            "WHERE a.date >= %s AND s.w_bal IS NOT NULL GROUP BY s.activity_id",
            (oldest,),
        )
        return {r[0]: r[1] for r in cur.fetchall()}


def enrich_velomate_rows(rows: list, cp_by_date: dict, stats_by_date: dict,
                         wbal_by_activity: dict) -> list:
    """Attach daily-series and stream-derived values to each activity row.

    Returns new dicts; the input rows are left untouched.
    """
    enriched = []
    for row in rows:
        out = dict(row)
        day = row["date"].date() if isinstance(row.get("date"), datetime) else None
        cp = cp_by_date.get(day) or {}
        stats = stats_by_date.get(day) or {}
        out["cp_watts"] = cp.get("cp_watts")
        out["w_prime_kj"] = cp.get("w_prime_kj")
        out["ctl"] = stats.get("ctl")
        out["atl"] = stats.get("atl")
        out["min_w_bal"] = wbal_by_activity.get(row.get("id"))
        enriched.append(out)
    return enriched


VELOMATE_COLUMNS = [
    "id", "strava_id", "date", "duration_s", "distance_m", "elevation_m",
    "avg_hr", "avg_power", "np", "tss", "intensity_factor", "variability_index",
    "ef", "trimp", "aerobic_decoupling", "work_kj", "ride_ftp", "ride_weight",
]


def _open_velomate_connection():
    """Connect via the CLI's config path. Returns None when unavailable."""
    repo_root = Path(__file__).resolve().parent.parent
    if str(repo_root) not in sys.path:
        sys.path.insert(0, str(repo_root))
    from velomate.db import get_connection
    return get_connection()


def fetch_velomate_activities(conn, oldest: str) -> list:
    """Read activities on or after `oldest` as dicts keyed by column name."""
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT {', '.join(VELOMATE_COLUMNS)} FROM activities "
            "WHERE date >= %s ORDER BY date DESC",
            (oldest,),
        )
        return [dict(zip(VELOMATE_COLUMNS, row)) for row in cur.fetchall()]


def _fmt(value, places=2):
    return "—" if value is None else f"{value:.{places}f}"


def render_report(result, stats_sections, inventory, probe) -> str:
    """Render the whole comparison as a Markdown document."""
    lines = ["# intervals.icu vs VeloMate — comparison report", ""]

    matched = result.by_strava_id + result.by_fuzzy
    lines += [
        "## Ride matching",
        "",
        f"- matched by Strava ID: **{result.by_strava_id}**",
        f"- matched by fuzzy rule: **{result.by_fuzzy}**",
        f"- total matched: **{matched}**",
        f"- intervals.icu only: {len(result.icu_only)}",
        f"- VeloMate only: {len(result.velomate_only)}",
        "",
    ]

    if not result.pairs:
        lines += [
            "**No matched rides — nothing to compare.** Widen `--months`, or check that "
            "intervals.icu has backfilled the period VeloMate covers.",
            "",
        ]

    for title, stats in stats_sections:
        if not stats:
            continue
        lines += [
            f"## {title}",
            "",
            "| metric | n | mean signed Δ | median % Δ | within tol | median ratio | flag |",
            "|---|---:|---:|---:|---:|---:|---|",
        ]
        for s in stats:
            lines.append(
                f"| {s.name} | {s.n} | {_fmt(s.mean_signed_diff)} | {_fmt(s.median_pct_diff)} | "
                f"{_fmt(s.within_tolerance_pct, 1)}% | {_fmt(s.median_ratio, 4)} | {s.unit_flag or ''} |"
            )
        lines.append("")

    lines += ["## API probe", ""]
    lines.append(f"- activity types: {probe.activity_types or '(none seen)'}")
    lines.append(f"- stream types: {probe.stream_types or '(none probed)'}")
    lines.append(f"- all-null streams: {probe.all_null_streams or '(none)'}")
    lines.append("")

    if inventory:
        lines += [
            "## Enrichment candidates — populated intervals.icu fields with no VeloMate equivalent",
            "",
            "| field | fill % | sample |",
            "|---|---:|---|",
        ]
        for f in inventory:
            lines.append(f"| {f.name} | {f.fill_pct:.1f} | {str(f.sample)[:60]} |")
        lines.append("")

    return "\n".join(lines)


def write_csv(path: str, pairs: list):
    """Per-ride values for both systems, for manual inspection."""
    header = ["icu_id", "velomate_id", "date"]
    for m in METRICS:
        header += [f"vm_{m.name}", f"icu_{m.name}"]
    with open(path, "w", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(header)
        for icu, vm in pairs:
            row = [icu.get("id"), vm.get("id"), icu.get("start_date_local")]
            for m in METRICS:
                row += [vm.get(m.vm_field), icu.get(m.icu_field)]
            writer.writerow(row)


def main(argv: list = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--months", type=int,
                        default=int(os.environ.get("VELOMATE_BACKFILL_MONTHS", "12")),
                        help="how far back to compare (default: VELOMATE_BACKFILL_MONTHS or 12)")
    parser.add_argument("--tolerance", type=float, default=0.02,
                        help="agreement tolerance as a fraction (default 0.02 = 2%%)")
    parser.add_argument("--streams", type=int, default=5,
                        help="how many rides to probe streams on (0 to skip)")
    parser.add_argument("--csv", help="also write per-ride values to this CSV path")
    args = parser.parse_args(argv)

    conn = _open_velomate_connection()
    if conn is None:
        print("Could not connect to the VeloMate database — check ~/.config/velomate/config.yaml")
        return 1

    oldest = (datetime.now(timezone.utc) - timedelta(days=args.months * 30)).strftime("%Y-%m-%dT00:00:00")
    # Everything the DB is needed for happens here, so the handle is released
    # before the (slow, network-bound) intervals.icu calls rather than being
    # held open for the whole run — and it closes even if a read raises.
    try:
        vm_rows = enrich_velomate_rows(
            fetch_velomate_activities(conn, oldest),
            fetch_cp_estimates(conn, oldest),
            fetch_athlete_stats(conn, oldest),
            fetch_wbal_minima(conn, oldest),
        )
    finally:
        conn.close()
    icu_activities = intervals_icu.list_activities(oldest=oldest)
    print(f"VeloMate rides: {len(vm_rows)}   intervals.icu activities: {len(icu_activities)}")

    result = match_activities(icu_activities, vm_rows)

    sections = []
    if result.pairs:
        ftp_matched, ftp_mismatched = split_by_ftp_agreement(result.pairs)
        normal_vi, high_vi = split_by_vi(result.pairs)
        for title, pairs in (
            ("All matched rides", result.pairs),
            (f"FTP-aligned rides only ({len(ftp_mismatched)} excluded for differing FTP)", ftp_matched),
            (f"Normal VI (≤ {HIGH_VI_THRESHOLD})", normal_vi),
            (f"High VI (> {HIGH_VI_THRESHOLD}) — expected to diverge on TSS", high_vi),
        ):
            stats = [s for s in (compare_metric(m, pairs, args.tolerance) for m in METRICS) if s]
            sections.append((title, stats))

    streams_by_id = {}
    for icu, _ in result.pairs[: max(0, args.streams)]:
        streams_by_id[icu["id"]] = intervals_icu.get_streams(icu["id"])

    report = render_report(
        result, sections, field_inventory(icu_activities), probe_api(icu_activities, streams_by_id)
    )
    print(report)

    if args.csv and result.pairs:
        write_csv(args.csv, result.pairs)
        print(f"\nPer-ride CSV written to {args.csv}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
