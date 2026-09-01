"""Generate grafana/dashboards/imperial/ from grafana/dashboards/metric/.

Conversion strategy — three complementary passes applied to every panel:

1. **Unit-id pass** (fieldConfig.defaults.unit / byName override unit):
   For columns declared with a convertible Grafana unit id (lengthkm/lengthm/
   velocitykmh), swap the unit id to its imperial equivalent and multiply the
   matching SELECT output column by the conversion factor.

2. **Alias-token pass** (NEW):
   Output columns whose alias CONTAINS a metric unit token — (km), (m), (km/h)
   — but whose fieldConfig carries no unit declaration.  Examples: "Distance (km)"
   used purely as an x-axis label, "Elev Gain (m)" in a table with no unit set,
   "Speed (km/h)" in a panel whose byName override only sets color/placement.
   For each such column:
     - multiply its value by the appropriate factor (inside ROUND if present)
     - rename the alias token: (km)→(mi), (m)→(ft), (km/h)→(mph)
     - update every byName override matcher that references the old alias
     - update options.xField if it references the old alias

3. **String-concat pass** (NEW):
   Handles `<expr> || ' km'` / `|| ' m'` / `|| ' km/h'` display-string columns
   (e.g. "Longest Ride" value in All-Time Records).  Multiplies the numeric
   expression before the concat and replaces the unit literal.

Double-conversion guard: a column that is already handled by the unit-id pass is
NOT re-processed by the alias-token pass, so no alias is converted twice even when
both a fieldConfig unit and a metric token appear in the same column.

Description rewrite: metric unit tokens in a converted panel's `description` field
are also updated — only the token substrings, nothing else.

Aborts if a convertible unit's output column can't be located in the SQL, so
unconvertible panels surface instead of silently shipping wrong numbers.

Per-kilometre-bucketed panels (ids 400, 403) stay km-based: their distance x-axis
is 1-km-wide bucket labels by construction (CTE generate_series steps stay metric).
Full per-mile re-bucketing is future work.  See docs/design/specs/
2026-06-16-imperial-units-design.md "Out of scope".
"""
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from velomate.units import GRAFANA_UNIT_MAP

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
METRIC_DIR = os.path.join(ROOT, "grafana", "dashboards", "metric")
IMPERIAL_DIR = os.path.join(ROOT, "grafana", "dashboards", "imperial")

# Ordered list of (metric_token, imperial_token, unit_id, factor).
# Order matters: check (km/h) before (m) to avoid partial matches.
ALIAS_TOKENS = [
    ("km/h", "mph",  "velocitykmh", 0.621371),
    ("km",   "mi",   "lengthkm",    0.621371),
    ("m",    "ft",   "lengthm",     3.28084),
]

# Text tokens shown in aliases/titles per metric Grafana unit id.
# (metric_token, imperial_token) — matched inside parentheses.
UNIT_TOKENS = {
    "lengthkm":    ("km",   "mi"),
    "lengthm":     ("m",    "ft"),
    "velocitykmh": ("km/h", "mph"),
}

# Ordered SQL concat literals to replace: (' km', ' mi'), (' m', ' ft'), (' km/h', ' mph')
CONCAT_REPLACEMENTS = [
    (" km/h", " mph", "velocitykmh", 0.621371),
    (" km",   " mi",  "lengthkm",    0.621371),
    (" m",    " ft",  "lengthm",     3.28084),
]

# Panel ids whose distance bucket label column (a km-integer concat) must NOT
# be converted.  These panels use 1-km-wide buckets by construction; converting
# the label to fractional miles is semantically wrong.  Full per-mile
# re-bucketing is future work.
CONCAT_EXEMPT_PANEL_IDS: frozenset[int] = frozenset({400, 403})

# Aliases in specific panels that are per-km bucket identifiers and must not be
# converted by the alias-token pass.  Key: panel id, value: set of alias names.
ALIAS_TOKEN_EXEMPT: dict[int, set[str]] = {
    33: {"KM"},   # panel 33 "KM" column is the 1-km bucket number; speed/elevation DO convert
}

# Explicit SQL substitutions for columns whose display value is embedded inside
# a CASE...END || expr string-concat and cannot be handled by the generic passes.
# Key: (panel_id, alias).  Value: (old_sql_fragment, new_sql_fragment).
# The old fragment must match exactly once in the rawSql; the new fragment
# replaces it.  Existing MIN/MAX OVER() comparisons (order-preserving on the
# metric value) are intentionally left untouched so sort order is correct.
EXPLICIT_SQL_SUBSTITUTIONS: dict[tuple[int, str], tuple[str, str]] = {
    # Panel 33 "Avg Speed": the displayed value is `avg_speed::text` (km/h).
    # Convert only the displayed number; leave the OVER() comparators metric.
    (33, "Avg Speed"): (
        "avg_speed::text AS \"Avg Speed\"",
        "ROUND((avg_speed * 0.621371)::numeric, 1)::text AS \"Avg Speed\"",
    ),
}

# Properties within an override that may contain the alias as a string value
# and must be renamed when the alias is renamed.
ALIAS_REFERENCING_PROPS = {
    "custom.axisLabel",
    "custom.fillBelowTo",
    "displayName",
}

ROUND_PAT = re.compile(
    r"^ROUND\(\s*(?P<inner>.+?)\s*(?P<cast>::numeric)?\s*,\s*(?P<dec>\d+)\s*\)$",
    re.DOTALL,
)

# Decimal places for ROUND() when wrapping a bare conversion expression.
# Keyed on the factor value.
_FACTOR_ROUND_DECIMALS: dict[float, int] = {
    3.28084:  0,   # metres → feet: no decimals
    0.621371: 1,   # km→mi and km/h→mph: one decimal
}


def _find_column_expr(sql: str, alias: str):
    """Locate the expression for output column ``alias`` in a SELECT statement.

    Handles subqueries, CTEs, and OVER() clauses by tracking parenthesis depth
    when walking backwards from the AS keyword.  Returns (expr_start, as_start,
    expr_text) or None if the alias is not present.
    """
    as_pat = re.compile(r"\s+AS\s+\"" + re.escape(alias) + r"\"")
    as_m = as_pat.search(sql)
    if not as_m:
        return None

    as_start = as_m.start()

    # Forward scan from start of SQL, tracking parenthesis depth.  The output
    # column lives in the final top-level SELECT list (the SELECT after any CTE
    # definitions).  We must distinguish two kinds of top-level (depth-0) comma:
    #   - a SELECT-list separator (between output columns) — a valid boundary
    #   - a CTE separator (between `curr AS (...)` and `prev AS (...)`) — NOT a
    #     boundary; it sits *before* the final SELECT in a WITH ... query.
    # So we record the position just after the last top-level SELECT keyword and
    # only treat a top-level comma as a column boundary when it occurs *after*
    # that SELECT.
    depth = 0
    last_top_comma = None
    last_top_select_end = None

    i = 0
    while i < as_start:
        ch = sql[i]
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        elif depth == 0:
            if ch == ",":
                last_top_comma = i
            else:
                # Check for a SELECT keyword starting at this position
                # (case-insensitive, on a word boundary).
                tail = sql[i : i + 6].upper()
                if tail == "SELECT" and (
                    i == 0 or not (sql[i - 1].isalnum() or sql[i - 1] == "_")
                ) and (
                    as_start - i == 6
                    or not (sql[i + 6].isalnum() or sql[i + 6] == "_")
                ):
                    last_top_select_end = i + len("SELECT")
        i += 1

    # The SELECT-list region begins right after the final top-level SELECT.
    # A top-level comma only delimits a column when it falls inside that region.
    if (
        last_top_comma is not None
        and (last_top_select_end is None or last_top_comma > last_top_select_end)
    ):
        expr_start = last_top_comma + 1
    elif last_top_select_end is not None:
        expr_start = last_top_select_end
    else:
        expr_start = 0

    expr = sql[expr_start:as_start].strip()
    return expr_start, as_start, expr


def _wrap_with_factor(expr: str, factor: float) -> str:
    """Wrap ``expr`` to multiply its value by ``factor``.

    If the expression is ``ROUND(<inner>::numeric, N)`` the factor is inserted
    inside the ROUND so rounding precision is applied to the converted value.
    Otherwise the expression is wrapped as ``ROUND(((<expr>) * <factor>), N)``
    where N is determined by the factor (0 for ft, 1 for mi/mph), ensuring
    converted values never emit long decimal strings.
    """
    m = ROUND_PAT.match(expr)
    if m:
        inner = m.group("inner")
        cast = m.group("cast") or ""
        dec = m.group("dec")
        # Keep the ::numeric cast on the inner expression so ROUND() receives
        # a numeric operand (required by PostgreSQL's ROUND signature).
        return f"ROUND(({inner}{cast}) * {factor}, {dec})"
    # Bare expression: wrap in ROUND with appropriate decimal places.
    decimals = _FACTOR_ROUND_DECIMALS.get(factor, 2)
    return f"ROUND((({expr}) * {factor})::numeric, {decimals})"


def _rewrite_unit_token(text: str, metric_tok: str, imp_tok: str) -> str:
    """Replace ``(metric_tok)`` with ``(imp_tok)`` in *text*."""
    return re.sub(
        r"\(" + re.escape(metric_tok) + r"\)", f"({imp_tok})", text
    )


def _convert_sql_output_column(sql: str, alias: str, factor: float) -> str:
    """Multiply the output column aliased *alias* by *factor* in *sql*.

    Raises ``ValueError`` if the alias is not found in the SQL.  Only the
    output expression is modified; WHERE / HAVING / $__timeFilter clauses are
    untouched (they appear after FROM, never before AS "<alias>").
    """
    result = _find_column_expr(sql, alias)
    if result is None:
        raise ValueError(
            f"alias {alias!r} not found in SQL — cannot apply conversion factor.\n"
            f"SQL excerpt: {sql[:300]!r}"
        )
    expr_start, as_start, expr = result
    new_expr = _wrap_with_factor(expr, factor)
    # Preserve leading whitespace/newline from the original slice
    leading = sql[expr_start : expr_start + len(sql[expr_start:as_start]) - len(sql[expr_start:as_start].lstrip())]
    return sql[:expr_start] + leading + new_expr + sql[as_start:]


def _alias_metric_token(alias: str):
    """If the alias contains a metric unit token in parentheses, return
    (metric_tok, imp_tok, unit_id, factor), else return None.

    Check km/h before km to avoid partial matches.
    """
    for metric_tok, imp_tok, unit_id, factor in ALIAS_TOKENS:
        if f"({metric_tok})" in alias:
            return metric_tok, imp_tok, unit_id, factor
    return None


def _override_unit(override: dict) -> str | None:
    """Return the 'unit' property value from an override, or None."""
    for p in override.get("properties", []):
        if p.get("id") == "unit":
            return p.get("value")
    return None


def _convertible_fields(panel: dict):
    """Yield (kind, alias_or_None, unit_id, holder) for each convertible field.

    - kind == "override": a column named by a byName override whose unit is in
      GRAFANA_UNIT_MAP. ``alias_or_None`` is the column name; ``holder`` is the
      override dict.
    - kind == "default": the panel-level default unit is convertible.
      ``alias_or_None`` is None; ``holder`` is the fieldConfig.defaults dict.
      Yielded at most once (the defaults dict itself).

    This generator does NOT decide which columns the default unit applies to —
    it always yields the default entry when the default unit is convertible,
    even if some columns carry their own overrides. The override-wins-over-default
    enforcement (skipping default conversion for any alias that has its own unit
    override) lives in ``convert_panel`` via ``aliases_with_override_unit``.
    """
    fc = panel.get("fieldConfig") or {}
    defaults = fc.get("defaults") or {}
    overrides = fc.get("overrides") or []

    for ov in overrides:
        if ov.get("matcher", {}).get("id") != "byName":
            continue
        alias = ov["matcher"].get("options", "")
        unit = _override_unit(ov)
        if unit in GRAFANA_UNIT_MAP:
            yield ("override", alias, unit, ov)

    default_unit = defaults.get("unit")
    if default_unit in GRAFANA_UNIT_MAP:
        yield ("default", None, default_unit, defaults)


def _apply_concat_conversions(sql: str) -> str:
    """Convert `<expr> || ' km'` / `|| ' m'` / `|| ' km/h'` string-concat
    columns to imperial.  The numeric expression before `||` is multiplied by
    the appropriate factor inside any ROUND(); the literal is renamed.

    Handled expression forms (matching right-to-left from the `||`):
      ROUND(<inner>, N) || ' km'
      <identifier>::<type> || ' km'
      <identifier> || ' km'

    Order: km/h first, then km, then m — avoids partial matches.
    Only literal unit strings are touched; WHERE / HAVING clauses never
    contain `|| ' km'` so they are safe.

    Raises ValueError if any metric concat literal remains after substitution
    (i.e. an unconverted form was present that the regex did not handle).
    """
    for metric_unit, imp_unit, _uid, factor in CONCAT_REPLACEMENTS:
        # Match: ROUND(...) || ' unit'  OR  <ident>[::cast] || ' unit'
        # We build a pattern that captures the numeric expression before ||.
        #
        # Case 1: ROUND(...) — balanced parens captured as a whole
        # Case 2: identifier with optional ::cast
        # The lookahead ensures the unit is exactly this token (e.g. ' km' not ' km/h').
        escaped = re.escape(metric_unit)
        pat = re.compile(
            r"(ROUND\([^)]*(?:\([^)]*\)[^)]*)*\)"   # ROUND(...) possibly nested
            r"|[A-Za-z_][A-Za-z0-9_.]*(?:::[A-Za-z]+)?)"  # ident or ident::cast
            r"(\s*\|\|\s*'"
            + escaped
            + r"'(?!\w))"  # || ' unit' not followed by word char (so ' km' != ' km/h')
        )
        def _replacer(m, factor=factor, imp_unit=imp_unit):
            expr = m.group(1)
            new_expr = _wrap_with_factor(expr, factor)
            return new_expr + f" || '{imp_unit}'"

        sql = pat.sub(_replacer, sql)

        # Hardening: if the metric literal still appears, the regex missed a form.
        if f"|| '{metric_unit}'" in sql:
            raise ValueError(
                f"concat literal {metric_unit!r} still present after substitution — "
                f"unhandled SQL form; update _apply_concat_conversions.\n"
                f"SQL excerpt: {sql[:400]!r}"
            )

    return sql


def convert_panel(panel: dict) -> None:
    """Convert all convertible fields in *panel* in-place."""
    panel_id = panel.get("id")
    fc = panel.get("fieldConfig") or {}
    overrides = fc.get("overrides") or []
    targets = panel.get("targets") or []

    # Build set of aliases that have their own override unit (any unit, not just convertible).
    aliases_with_override_unit: set[str] = set()
    for ov in overrides:
        if ov.get("matcher", {}).get("id") == "byName":
            if _override_unit(ov) is not None:
                aliases_with_override_unit.add(ov["matcher"].get("options", ""))

    # Track which aliases have already been converted (by unit-id pass or alias-token pass)
    # to prevent double-conversion.
    already_converted_aliases: set[str] = set()

    alias_renames: dict[str, str] = {}  # old_alias -> new_alias (for xField / option rewriting)

    # ── Pass 0: explicit SQL substitutions ─────────────────────────────────────
    # Handles columns where the display value is embedded inside a CASE...END
    # string-concat and the generic passes cannot safely isolate it.
    for t in targets:
        raw = t.get("rawSql", "")
        for (pid, alias), (old_frag, new_frag) in EXPLICIT_SQL_SUBSTITUTIONS.items():
            if pid != panel_id:
                continue
            if old_frag not in raw:
                continue
            raw = raw.replace(old_frag, new_frag, 1)
            already_converted_aliases.add(alias)
        t["rawSql"] = raw

    # ── Pass 1: unit-id based conversion (fieldConfig units) ────────────────
    for kind, alias, unit, holder in _convertible_fields(panel):
        imp_unit, factor = GRAFANA_UNIT_MAP[unit]
        metric_tok, imp_tok = UNIT_TOKENS[unit]

        if kind == "override":
            # 1. Swap the unit in the override properties
            for p in holder.get("properties", []):
                if p.get("id") == "unit":
                    p["value"] = imp_unit
                # Rename string properties that reference the alias name
                if p.get("id") in ALIAS_REFERENCING_PROPS and isinstance(p.get("value"), str):
                    p["value"] = _rewrite_unit_token(p["value"], metric_tok, imp_tok)

            # 2. Rename the matcher alias
            new_alias = _rewrite_unit_token(alias, metric_tok, imp_tok)
            if new_alias != alias:
                holder["matcher"]["options"] = new_alias
                alias_renames[alias] = new_alias

            # 3. Multiply the output column in every target that contains this alias
            for t in targets:
                raw = t.get("rawSql", "")
                if f'AS "{alias}"' not in raw:
                    continue
                raw = _convert_sql_output_column(raw, alias, factor)
                if new_alias != alias:
                    raw = raw.replace(f'AS "{alias}"', f'AS "{new_alias}"', 1)
                t["rawSql"] = raw

            # Mark as converted (use new alias to catch lookups post-rename)
            already_converted_aliases.add(alias)
            if new_alias != alias:
                already_converted_aliases.add(new_alias)

        else:  # kind == "default"
            # Swap the default unit
            holder["unit"] = imp_unit

            # Convert every output alias that is NOT covered by an override unit
            for t in targets:
                raw = t.get("rawSql", "")
                for col_alias in re.findall(r'AS\s+"([^"]+)"', raw):
                    if col_alias.lower() == "time":
                        continue
                    if col_alias in aliases_with_override_unit:
                        continue  # handled by its own override
                    # Convert the value
                    raw = _convert_sql_output_column(raw, col_alias, factor)
                    # Rename alias token if it contains the metric token
                    new_col_alias = _rewrite_unit_token(col_alias, metric_tok, imp_tok)
                    if new_col_alias != col_alias:
                        raw = raw.replace(f'AS "{col_alias}"', f'AS "{new_col_alias}"', 1)
                        alias_renames[col_alias] = new_col_alias
                    already_converted_aliases.add(col_alias)
                    already_converted_aliases.add(new_col_alias)
                t["rawSql"] = raw

    # ── Pass 2: alias-token conversion (columns with metric unit in their name) ──
    # For columns that carry a metric token in their alias (e.g. "Distance (km)",
    # "Elev Gain (m)") but have no fieldConfig unit declaration, we:
    #   a) multiply the column value by the appropriate factor
    #   b) rename the alias token
    #   c) update any byName override matchers pointing to the old alias
    #   d) update options.xField if it references the old alias
    #
    # The unit-id pass already handled any alias that had a fieldConfig unit, so
    # we skip those (already_converted_aliases guard prevents double-conversion).
    #
    # Exemption: aliases listed in ALIAS_TOKEN_EXEMPT[panel_id] are per-km bucket
    # identifiers and must not be converted.
    exempt_aliases: set[str] = ALIAS_TOKEN_EXEMPT.get(panel_id, set())
    for t in targets:
        raw = t.get("rawSql", "")
        for col_alias in re.findall(r'AS\s+"([^"]+)"', raw):
            if col_alias.lower() == "time":
                continue
            if col_alias in already_converted_aliases:
                continue  # already handled by unit-id pass or explicit pass
            if col_alias in exempt_aliases:
                continue  # per-km bucket label — keep km
            tok_info = _alias_metric_token(col_alias)
            if tok_info is None:
                continue
            metric_tok, imp_tok, _unit_id, factor = tok_info
            # Multiply the column value
            raw = _convert_sql_output_column(raw, col_alias, factor)
            new_col_alias = _rewrite_unit_token(col_alias, metric_tok, imp_tok)
            if new_col_alias != col_alias:
                raw = raw.replace(f'AS "{col_alias}"', f'AS "{new_col_alias}"', 1)
                alias_renames[col_alias] = new_col_alias
            already_converted_aliases.add(col_alias)
            already_converted_aliases.add(new_col_alias)
        t["rawSql"] = raw

    # After alias-token pass: update any byName override matchers that still
    # reference an old alias (no unit override, just color/placement matchers).
    for ov in overrides:
        if ov.get("matcher", {}).get("id") != "byName":
            continue
        old_alias = ov["matcher"].get("options", "")
        if old_alias in alias_renames:
            new_alias = alias_renames[old_alias]
            ov["matcher"]["options"] = new_alias
            # Also rename any alias-referencing string properties in this override
            for p in ov.get("properties", []):
                if p.get("id") in ALIAS_REFERENCING_PROPS and isinstance(p.get("value"), str):
                    for m_tok, i_tok, _uid, _f in ALIAS_TOKENS:
                        p["value"] = _rewrite_unit_token(p["value"], m_tok, i_tok)

    # ── Pass 3: string-concat literal conversion ────────────────────────────
    # Handles `<expr> || ' km'` / `|| ' m'` / `|| ' km/h'` in table panels where
    # the display value is a formatted string (e.g. "12.3 km").
    # Panels in CONCAT_EXEMPT_PANEL_IDS use 1-km-wide buckets by construction;
    # their `km || ' km'` bucket label must NOT be converted.
    if panel_id not in CONCAT_EXEMPT_PANEL_IDS:
        for t in targets:
            raw = t.get("rawSql", "")
            needs_conversion = any(
                f"|| '{_m}'" in raw
                for _m, _i, _uid, _f in CONCAT_REPLACEMENTS
            )
            if needs_conversion:
                raw = _apply_concat_conversions(raw)
            t["rawSql"] = raw

    # ── Update panel title if it contains a unit token ───────────────────────
    title = panel.get("title", "")
    new_title = title
    for unit in list(GRAFANA_UNIT_MAP):
        metric_tok, imp_tok = UNIT_TOKENS[unit]
        new_title = _rewrite_unit_token(new_title, metric_tok, imp_tok)
    if new_title != title:
        panel["title"] = new_title

    # ── Update panel description if it contains metric unit tokens ────────────
    # Only rewrite the unit tokens in a description, not other text.
    desc = panel.get("description", "")
    if desc:
        new_desc = desc
        # Apply in order: km/h before km to avoid partial matches; m last.
        for metric_tok, imp_tok, _uid, _f in ALIAS_TOKENS:
            new_desc = _rewrite_unit_token(new_desc, metric_tok, imp_tok)
        if new_desc != desc:
            panel["description"] = new_desc

    # ── Update options.xField and any other options referencing renamed aliases ──
    if alias_renames:
        opts = panel.get("options") or {}
        if "xField" in opts and opts["xField"] in alias_renames:
            opts["xField"] = alias_renames[opts["xField"]]


def convert_dashboard(d: dict) -> dict:
    """Convert all panels in dashboard *d* in-place and return it."""

    def walk(panels):
        for p in panels:
            if p.get("type") != "row":
                convert_panel(p)
            if "panels" in p:
                walk(p["panels"])

    walk(d.get("panels", []))
    return d


def main():
    os.makedirs(IMPERIAL_DIR, exist_ok=True)
    for fn in sorted(os.listdir(METRIC_DIR)):
        if not fn.endswith(".json"):
            continue
        with open(os.path.join(METRIC_DIR, fn)) as f:
            d = json.load(f)
        convert_dashboard(d)
        with open(os.path.join(IMPERIAL_DIR, fn), "w") as f:
            json.dump(d, f, indent=2)
            f.write("\n")
        print(f"  wrote {fn}")
    print(f"Generated imperial dashboards in {IMPERIAL_DIR}")


if __name__ == "__main__":
    main()
