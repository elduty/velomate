"""Structural and drift tests for the generated imperial dashboard set."""
import glob
import importlib.util
import json
import os
import re

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
METRIC = os.path.join(ROOT, "grafana", "dashboards", "metric")
IMPERIAL = os.path.join(ROOT, "grafana", "dashboards", "imperial")

_METRIC_UNIT_IDS = ("lengthkm", "lengthm", "velocitykmh")
_IMPERIAL_UNIT_IDS = ("lengthmi", "lengthft", "velocitymph")


def _load_generator():
    """Load scripts/gen_imperial_dashboards.py as a module."""
    spec = importlib.util.spec_from_file_location(
        "gen", os.path.join(ROOT, "scripts", "gen_imperial_dashboards.py")
    )
    gen = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(gen)
    return gen


def _read_all(directory: str) -> str:
    return "".join(
        open(f).read() for f in sorted(glob.glob(os.path.join(directory, "*.json")))
    )


def test_no_metric_length_speed_units_remain_in_imperial():
    blob = _read_all(IMPERIAL)
    # Use word-boundary matching so "lengthm" is not falsely found inside "lengthmi".
    still_present = [
        u for u in _METRIC_UNIT_IDS
        if re.search(r"\b" + re.escape(u) + r"\b", blob)
    ]
    assert still_present == [], (
        f"imperial set still contains metric unit ids: {still_present}"
    )


def test_imperial_has_imperial_units():
    blob = _read_all(IMPERIAL)
    missing = [u for u in _IMPERIAL_UNIT_IDS if u not in blob]
    assert missing == [], (
        f"imperial set missing expected imperial unit ids: {missing}"
    )


def test_filters_unchanged_vs_metric():
    """WHERE / HAVING / $__timeFilter clauses must be byte-identical between sets."""
    for mf in sorted(glob.glob(os.path.join(METRIC, "*.json"))):
        imp_path = os.path.join(IMPERIAL, os.path.basename(mf))
        met = json.load(open(mf))
        imp = json.load(open(imp_path))

        def extract_filters(d: dict) -> list[str]:
            blob = json.dumps(d)
            # Stop WHERE/HAVING captures at ) to avoid capturing context from
            # surrounding expressions when WHERE appears inside a subquery.
            return sorted(
                re.findall(
                    r"(WHERE[^)\"]*|HAVING[^)\"]*|\$__timeFilter\([^)]*\))", blob
                )
            )

        met_filters = extract_filters(met)
        imp_filters = extract_filters(imp)
        assert met_filters == imp_filters, (
            f"filters differ in {os.path.basename(mf)}:\n"
            f"  metric-only: {set(met_filters) - set(imp_filters)}\n"
            f"  imperial-only: {set(imp_filters) - set(met_filters)}"
        )


def _iter_panels(dashboard):
    """Yield every panel, including those nested in collapsed rows."""
    for p in dashboard.get("panels", []):
        yield p
        if p.get("type") == "row":
            for child in p.get("panels", []) or []:
                yield child


def _final_top_level_select_pos(sql: str) -> int:
    """Return the index of the final top-level (paren-depth-0) SELECT keyword.

    For a `WITH curr AS (...), prev AS (...) SELECT ... ` query this is the
    SELECT that follows the CTE definitions — i.e. the boundary between the CTE
    region and the output column list.  Returns 0 if no top-level SELECT exists.
    """
    depth = 0
    last = 0
    i = 0
    n = len(sql)
    while i < n:
        ch = sql[i]
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        elif depth == 0 and sql[i : i + 6].upper() == "SELECT":
            before_ok = i == 0 or not (sql[i - 1].isalnum() or sql[i - 1] == "_")
            after_ok = i + 6 >= n or not (sql[i + 6].isalnum() or sql[i + 6] == "_")
            if before_ok and after_ok:
                last = i
        i += 1
    return last


def test_cte_region_unchanged_vs_metric():
    """The CTE-definition region (everything before the final top-level SELECT)
    of every panel's SQL must be byte-identical between the metric and imperial
    sets.  This catches the multi-CTE bug where the conversion factor was wrapped
    around a CTE body instead of the SELECT-list output expression — producing
    structurally invalid SQL that still happened to balance parentheses."""
    for mf in sorted(glob.glob(os.path.join(METRIC, "*.json"))):
        name = os.path.basename(mf)
        met = json.load(open(mf))
        imp = json.load(open(os.path.join(IMPERIAL, name)))

        met_panels = {p["id"]: p for p in _iter_panels(met)}
        imp_panels = {p["id"]: p for p in _iter_panels(imp)}

        for pid, mpanel in met_panels.items():
            ipanel = imp_panels[pid]
            m_targets = mpanel.get("targets") or []
            i_targets = ipanel.get("targets") or []
            for mt, it in zip(m_targets, i_targets):
                msql = mt.get("rawSql", "")
                isql = it.get("rawSql", "")
                # Only check CTE region for queries that actually have CTEs.
                # UNION ALL queries (like All-Time Records) have no CTE body to
                # protect — the "CTE region" heuristic does not apply to them.
                if not msql.strip().upper().startswith("WITH"):
                    continue
                m_region = msql[: _final_top_level_select_pos(msql)]
                i_region = isql[: _final_top_level_select_pos(isql)]
                assert m_region == i_region, (
                    f"CTE region changed in {name} panel {pid} "
                    f"'{mpanel.get('title')}':\n"
                    f"  metric:   {m_region!r}\n"
                    f"  imperial: {i_region!r}"
                )


def test_convert_sql_output_column_raises_when_alias_missing():
    """The generator must fail loudly (raise ValueError) when a convertible field's
    output column cannot be located in the SQL — surfacing the problem rather than
    silently shipping unconverted numbers."""
    gen = _load_generator()

    # A convertible field declared (lengthkm, factor 0.621371) but the SQL has no
    # output column with the matching alias — the loud-failure path.
    sql = "SELECT SUM(distance_m) / 1000.0 AS \"SomethingElse\" FROM activities WHERE id = 1;"
    with pytest.raises(ValueError):
        gen._convert_sql_output_column(sql, "Distance (km)", 0.621371)


def test_convert_panel_raises_on_override_with_missing_column():
    """End-to-end: a panel whose byName override targets a convertible-unit column
    that is present as text in the SQL but whose SELECT expression cannot be located
    must raise rather than silently emit wrong numbers."""
    gen = _load_generator()

    panel = {
        "type": "timeseries",
        "title": "Synthetic",
        "fieldConfig": {
            "defaults": {},
            "overrides": [
                {
                    "matcher": {"id": "byName", "options": "Distance (km)"},
                    "properties": [{"id": "unit", "value": "lengthkm"}],
                }
            ],
        },
        # rawSql references the alias (so the override branch attempts conversion)
        # but provides no resolvable SELECT-list expression for it.
        "targets": [{"rawSql": 'AS "Distance (km)" FROM activities;'}],
        "options": {},
    }

    with pytest.raises(ValueError):
        gen.convert_panel(panel)


def test_no_metric_display_tokens_remain():
    """Every imperial/*.json must be free of metric display tokens.

    Checks:
    - Column aliases / byName matchers: ` (km)`, ` (m)`, ` (km/h)` (space-prefixed
      so SQL function calls like MAX(km), FLOOR(km) are not false-matched — those
      always follow an alphabetic identifier, not a space).
    - String-concat literals: `|| ' km'`, `|| ' m'`, `|| ' km/h'` (the raw concat
      form that appears in SQL output columns).
    - Grafana unit ids: `lengthkm`, `lengthm`, `velocitykmh` (word-boundary matched
      so `lengthm` is not found inside `lengthmi` / `lengthft`).

    Display labels always have a space before the opening paren (e.g. `Speed (km/h)`,
    `Dist (km)`, `Elev (m)`) whereas SQL function calls do not (`MAX(km)`, `FLOOR(km)`).
    Concat literals `|| ' km'` in the raw SQL output are the only form that does NOT
    have a space-paren; they are checked separately.

    Exemption — per-kilometre-bucketed panels (ids 400 & 403, "HR/Power Zones by
    Kilometer"): these panels use 1-km-wide distance buckets by construction (the
    CTE accumulates speed_kmh / 3600 in km).  Converting the bucket label to
    fractional miles would be semantically wrong.  Full per-mile re-bucketing is
    future work.  The concat literal `km || ' km' AS "Kilometer"` is therefore
    intentionally kept in the imperial output for exactly these two panels.
    The allowlist below is tight: it lists the exact (panel_id, alias) pairs whose
    concat literal is permitted; any new panel that leaves a metric literal must
    update this list explicitly.
    """
    # Tight allowlist: (panel_id, output_alias) pairs where a metric concat literal
    # is intentionally preserved.  Any new exempt panel must be listed here.
    _CONCAT_EXEMPT: dict[str, set[int]] = {
        # activity.json panels 400 and 403: 1-km bucket labels.
        "activity.json": {400, 403},
    }

    for filepath in sorted(glob.glob(os.path.join(IMPERIAL, "*.json"))):
        name = os.path.basename(filepath)
        with open(filepath) as f:
            blob = f.read()
        dashboard = json.loads(blob)
        exempt_panel_ids: set[int] = _CONCAT_EXEMPT.get(name, set())

        failures = []

        # Display-label tokens: always " (km)", " (m)", " (km/h)" with a space before (.
        # This excludes SQL function calls like MAX(km) or FLOOR(km).
        for metric_tok in ("km/h", "km", "m"):
            pattern = r" \(" + re.escape(metric_tok) + r"\)"
            if re.search(pattern, blob):
                failures.append(f"display label ' ({metric_tok})' still present")

        # String-concat unit literals: check per-panel, skipping exempt panels.
        if exempt_panel_ids:
            # Build a set of non-exempt SQL blobs to check.
            non_exempt_sql_parts: list[str] = []
            for panel in _iter_panels(dashboard):
                pid = panel.get("id")
                if pid in exempt_panel_ids:
                    continue
                for t in panel.get("targets") or []:
                    non_exempt_sql_parts.append(t.get("rawSql", ""))
            non_exempt_blob = "\n".join(non_exempt_sql_parts)
            for literal in (" km'", " m'", " km/h'"):
                if literal in non_exempt_blob:
                    failures.append(f"concat literal {literal!r} still present (non-exempt panels)")
        else:
            for literal in (" km'", " m'", " km/h'"):
                if literal in blob:
                    failures.append(f"concat literal {literal!r} still present")

        # Grafana unit ids (word-boundary to avoid lengthm matching inside lengthmi).
        for uid in ("lengthkm", "lengthm", "velocitykmh"):
            if re.search(r"\b" + re.escape(uid) + r"\b", blob):
                failures.append(f"metric unit id {uid!r} still present")

        assert not failures, (
            f"{name}: metric display tokens remain:\n"
            + "\n".join(f"  - {f}" for f in failures)
        )


def test_drift_regenerate_matches_committed():
    """Regenerate imperial set in-memory and assert equality with committed files."""
    gen = _load_generator()

    for mf in sorted(glob.glob(os.path.join(METRIC, "*.json"))):
        d = json.load(open(mf))
        gen.convert_dashboard(d)
        committed_path = os.path.join(IMPERIAL, os.path.basename(mf))
        committed = json.load(open(committed_path))
        assert d == committed, (
            f"{os.path.basename(mf)} drifted from committed imperial/ — "
            f"re-run: python3 scripts/gen_imperial_dashboards.py"
        )
