"""Structural validation of Grafana dashboard JSON files."""

import glob
import json
import os
import re

import pytest
import yaml

DASHBOARD_DIR = os.path.join(os.path.dirname(__file__), "..", "grafana", "dashboards")
ALERTS_FILE = os.path.join(
    os.path.dirname(__file__), "..", "grafana", "provisioning", "alerting", "alerts.yaml"
)

# The one Form (TSB) band set, on Friel's scale. The stat, the chart, the
# overtraining alert and the planner are all checked against this list, so a
# band can't move in one of them alone. Each entry is
# (lower bound, tooltip range, label, palette hex). A band owns its lower bound
# because Grafana activates a threshold step at value >= step.
FORM_BANDS = [
    (None, "< -30", "High risk", "#e74c3c"),
    (-30, "-30 to -10", "Productive", "#2ecc71"),
    (-10, "-10 to +5", "Transitional", "#808080"),
    (5, "+5 to +25", "Fresh", "#3498db"),
    (25, "> +25", "Detraining", "#f1c40f"),
]
HIGH_RISK_BOUNDARY = FORM_BANDS[1][0]

# Tooltip emoji for each chart colour in the dashboard palette.
PALETTE_EMOJI = {
    "#808080": "🔘",
    "#3498db": "🔵",
    "#2ecc71": "🟢",
    "#f1c40f": "🟡",
    "#e67e22": "🟠",
    "#e74c3c": "🔴",
    "#9333ea": "🟣",
}

OVERVIEW_FILES = ["metric/overview.json", "imperial/overview.json"]
FORM_STAT_ID = 222
FORM_CHART_ID = 201
AVERAGE_DELTA_CARD_IDS = [307, 308, 309, 1012]
POWER_ZONE_PANELS = [
    ("activity.json", 32),
    ("activity.json", 41),
    ("activity.json", 403),
    ("all-time-progression.json", 30),
]


def get_dashboard_files():
    # All dashboards under grafana/dashboards/<set>/*.json (metric, imperial)
    pattern = os.path.join(DASHBOARD_DIR, "*", "*.json")
    return sorted(os.path.relpath(p, DASHBOARD_DIR) for p in glob.glob(pattern))


def iter_all_panels(dashboard):
    """Yield every panel in the dashboard, including panels nested inside
    collapsed row panels. A collapsed row stores its children in its own
    `panels` array instead of the top-level one."""
    for p in dashboard.get("panels", []):
        yield p
        if p.get("type") == "row":
            for child in p.get("panels", []) or []:
                yield child


@pytest.mark.parametrize("filename", get_dashboard_files())
class TestDashboardStructure:
    def load(self, filename):
        with open(os.path.join(DASHBOARD_DIR, filename)) as f:
            return json.load(f)

    def test_valid_json(self, filename):
        """Dashboard file is valid JSON."""
        self.load(filename)

    def test_has_uid(self, filename):
        d = self.load(filename)
        assert "uid" in d and d["uid"], f"{filename} missing uid"

    def test_has_panels(self, filename):
        d = self.load(filename)
        assert "panels" in d and len(d["panels"]) > 0

    def test_all_panels_have_id(self, filename):
        d = self.load(filename)
        for p in iter_all_panels(d):
            assert "id" in p, f"Panel '{p.get('title', '?')}' missing id"

    def test_no_duplicate_panel_ids(self, filename):
        d = self.load(filename)
        ids = [p["id"] for p in iter_all_panels(d)]
        dupes = [i for i in ids if ids.count(i) > 1]
        assert not dupes, f"Duplicate panel ids: {set(dupes)}"

    def test_all_panels_have_gridpos(self, filename):
        d = self.load(filename)
        for p in iter_all_panels(d):
            assert "gridPos" in p, f"Panel {p['id']} missing gridPos"
            gp = p["gridPos"]
            for key in ["h", "w", "x", "y"]:
                assert key in gp, f"Panel {p['id']} gridPos missing '{key}'"

    def test_has_graph_tooltip(self, filename):
        d = self.load(filename)
        assert d.get("graphTooltip") == 2, f"{filename} should have shared crosshair (graphTooltip: 2)"

    def test_has_nav_links(self, filename):
        d = self.load(filename)
        links = d.get("links", [])
        assert len(links) >= 1, f"{filename} should link to at least one other dashboard"

    def test_value_stat_panels_guard_null(self, filename):
        """A colorMode:value stat panel reading a single activity's column must
        filter NULLs (IS NOT NULL) so it returns *no rows* — not a row with a
        NULL value — when the metric is absent (e.g. a GPS-only ride with no
        HR/power sensors). A NULL value-row in a colorMode:value stat panel
        crashes the Grafana 12.4 scene renderer, which blanks every sibling
        panel on the dashboard (the no-sensor "all N/A" bug)."""
        d = self.load(filename)
        for p in iter_all_panels(d):
            if p.get("type") != "stat":
                continue
            if (p.get("options") or {}).get("colorMode") != "value":
                continue
            sql = " ".join(t.get("rawSql", "") for t in (p.get("targets") or []))
            if "${activity_id}" not in sql and "${route}" not in sql:
                continue  # panels scoped to a single activity or route
            assert "IS NOT NULL" in sql.upper(), (
                f"{filename} panel {p['id']} '{p.get('title')}': colorMode:value "
                f"per-ride stat with no NULL guard — a no-sensor ride emits a NULL "
                f"value-row and crashes the dashboard scene. Add an IS NOT NULL "
                f"filter so it returns no rows (graceful noValue) instead."
            )


# Each interval class, the colour its chart paints it, and the emoji the tooltip
# must show for that colour: a tooltip's icons have to match the chart.
INTERVAL_CLASS_COLOURS = {
    "surge": ("#808080", "\U0001F518"),
    "tempo": ("blue", "\U0001F535"),
    "sweetspot": ("green", "\U0001F7E2"),
    "threshold": ("yellow", "\U0001F7E1"),
    "vo2": ("orange", "\U0001F7E0"),
    "anaerobic": ("red", "\U0001F534"),
}
INTERVAL_TOOLTIP_LABELS = {
    "surge": "Surge", "tempo": "Tempo", "sweetspot": "Sweet spot",
    "threshold": "Threshold", "vo2": "VO2", "anaerobic": "Anaerobic",
}


def load_unit_panel(dashboard_set, filename, panel_id):
    path = os.path.join(DASHBOARD_DIR, dashboard_set, filename)
    with open(path) as f:
        dashboard = json.load(f)
    return next(p for p in iter_all_panels(dashboard) if p.get("id") == panel_id)


def tooltip_emoji_by_label(description):
    """`<emoji> <range> — <label>` lines, as {label: emoji}."""
    found = {}
    for line in description.split("\n"):
        if " — " in line:
            head, label = line.rsplit(" — ", 1)
            found[label.strip()] = head.split(" ", 1)[0]
    return found


@pytest.mark.parametrize("dashboard_set", ["metric", "imperial"])
class TestIntervalClassConsumers:
    """The classes the ingestor stores are the classes the dashboards name."""

    def test_the_colour_table_covers_every_stored_class(self, dashboard_set):
        from intervals import INTERVAL_CLASSES
        assert set(INTERVAL_CLASS_COLOURS) == set(INTERVAL_CLASSES)

    def test_detected_intervals_colours_every_class(self, dashboard_set):
        panel = load_unit_panel(dashboard_set, "activity.json", 1100)
        override = next(o for o in panel["fieldConfig"]["overrides"]
                        if o["matcher"]["options"] == "Type")
        mappings = next(p["value"] for p in override["properties"] if p["id"] == "mappings")
        colours = {name: entry["color"] for name, entry in mappings[0]["options"].items()}
        assert colours == {name: c for name, (c, _) in INTERVAL_CLASS_COLOURS.items()}

    def test_monthly_distribution_counts_and_colours_every_class(self, dashboard_set):
        panel = load_unit_panel(dashboard_set, "all-time-progression.json", 1110)
        sql = panel["targets"][0]["rawSql"]
        counted = {name for name in INTERVAL_CLASS_COLOURS
                   if f"ri.classification = '{name}'" in sql and f'AS "{name}"' in sql}
        assert counted == set(INTERVAL_CLASS_COLOURS)
        assert "sprint" not in sql
        colours = {}
        for override in panel["fieldConfig"]["overrides"]:
            prop = next(p for p in override["properties"] if p["id"] == "color")
            colours[override["matcher"]["options"]] = prop["value"]["fixedColor"]
        assert colours == {name: c for name, (c, _) in INTERVAL_CLASS_COLOURS.items()}

    @pytest.mark.parametrize("filename,panel_id", [
        ("activity.json", 1100), ("all-time-progression.json", 1110),
    ])
    def test_tooltip_icons_match_the_chart(self, dashboard_set, filename, panel_id):
        description = load_unit_panel(dashboard_set, filename, panel_id)["description"]
        expected = {INTERVAL_TOOLTIP_LABELS[name]: emoji
                    for name, (_, emoji) in INTERVAL_CLASS_COLOURS.items()}
        assert tooltip_emoji_by_label(description) == expected
        assert "Sprint" not in description

    @pytest.mark.parametrize("filename,panel_id", [
        ("activity.json", 1100), ("all-time-progression.json", 1110),
    ])
    def test_tooltip_states_the_real_band_edges(self, dashboard_set, filename, panel_id):
        """The two tooltips disagreed with each other and with the code."""
        description = load_unit_panel(dashboard_set, filename, panel_id)["description"]
        for fragment in ("Under 60 s", "75–88%", "88–95%", "95–105%",
                         "105–120%", "≥ 120%"):
            assert fragment in description
def form_probes():
    """Every band boundary and a value either side, with the band each is in."""
    yield -40, FORM_BANDS[0][2]
    for below, band in zip(FORM_BANDS, FORM_BANDS[1:]):
        boundary, label = band[0], band[2]
        yield boundary - 0.1, below[2]
        yield boundary, label
        yield boundary + 0.1, label
    yield 40, FORM_BANDS[-1][2]


def form_threshold_steps():
    return [{"color": color, "value": lower} for lower, _, _, color in FORM_BANDS]


def form_tooltip_lines():
    return [f"{PALETTE_EMOJI[color]} {rng} — {label}" for _, rng, label, color in FORM_BANDS]


def load_panel(filename, panel_id):
    with open(os.path.join(DASHBOARD_DIR, filename)) as f:
        dashboard = json.load(f)
    return next(p for p in iter_all_panels(dashboard) if p["id"] == panel_id)


def tooltip_range_lines(description):
    """The tooltip paragraphs that state a numeric range."""
    return [para for para in description.split("\n\n") if re.match(r"\S+ [<>+\-\d]", para)]


@pytest.mark.parametrize("filename", OVERVIEW_FILES)
class TestFormBands:
    def test_stat_thresholds(self, filename):
        thresholds = load_panel(filename, FORM_STAT_ID)["fieldConfig"]["defaults"]["thresholds"]
        assert thresholds["mode"] == "absolute"
        assert thresholds["steps"] == form_threshold_steps()

    def test_chart_thresholds(self, filename):
        panel = load_panel(filename, FORM_CHART_ID)
        override = next(
            o for o in panel["fieldConfig"]["overrides"] if o["matcher"]["options"] == "Form (TSB)"
        )
        thresholds = next(p["value"] for p in override["properties"] if p["id"] == "thresholds")
        assert thresholds["mode"] == "absolute"
        assert thresholds["steps"] == form_threshold_steps()

    def test_stat_word_agrees_with_stat_colour_at_every_boundary(self, filename):
        """The stat displays the mapped word, coloured by the threshold step.
        Range mappings are inclusive at both ends and Grafana takes the first
        match, so at a boundary the upper band's mapping has to come first or
        the word names a different band from the colour."""
        mappings = load_panel(filename, FORM_STAT_ID)["fieldConfig"]["defaults"]["mappings"]
        for value, label in form_probes():
            word = next(
                (m["options"]["result"]["text"] for m in mappings
                 if m["options"]["from"] <= value <= m["options"]["to"]),
                None,
            )
            assert word == label, f"TSB {value} shows {word!r}, its colour says {label!r}"

    @pytest.mark.parametrize("panel_id", [FORM_STAT_ID, FORM_CHART_ID])
    def test_tooltip_lists_exactly_these_bands(self, filename, panel_id):
        """One `<emoji> <range> — <label>` paragraph per band, emoji taken from
        the band's chart colour. A leftover range from an old band set fails too."""
        description = load_panel(filename, panel_id)["description"]
        assert tooltip_range_lines(description) == form_tooltip_lines()

    def test_stat_query_cannot_emit_a_null_row(self, filename):
        """tsb is a nullable column and a colorMode:value stat that renders a
        NULL row blanks the whole dashboard, so the query has to return no rows
        instead."""
        panel = load_panel(filename, FORM_STAT_ID)
        assert panel["options"]["colorMode"] == "value"
        assert "TSB IS NOT NULL" in panel["targets"][0]["rawSql"].upper()


class TestFormAlert:
    def rule(self):
        with open(ALERTS_FILE, encoding="utf-8") as f:
            config = yaml.safe_load(f)
        rules = [r for group in config["groups"] for r in group["rules"]]
        return next(r for r in rules if r["uid"] == "velomate-overtraining")

    def test_fires_below_the_high_risk_boundary(self):
        data = {d["refId"]: d for d in self.rule()["data"]}
        evaluator = data["C"]["model"]["conditions"][0]["evaluator"]
        assert evaluator == {"type": "lt", "params": [HIGH_RISK_BOUNDARY]}

    def test_summary_states_the_boundary_and_two_days(self):
        summary = self.rule()["annotations"]["summary"]
        assert f"below {HIGH_RISK_BOUNDARY} for 2+ days" in summary
        assert FORM_BANDS[0][2].lower() in summary.lower()

    def test_two_days_means_both_days(self):
        """The larger of the last two days must be below the threshold, which
        needs both of them below it. With min, one bad day would fire it."""
        data = {d["refId"]: d for d in self.rule()["data"]}
        assert "LIMIT 2" in data["A"]["model"]["rawSql"]
        assert data["B"]["model"]["reducer"] == "max"

    def test_comments_name_the_same_number(self):
        """yaml.safe_load drops comments, so read the rule's raw text."""
        with open(ALERTS_FILE, encoding="utf-8") as f:
            raw = f.read()
        block = raw[raw.index("uid: velomate-overtraining"):raw.index("uid: velomate-detraining")]
        assert set(re.findall(r"(?<![\w.])-\d+", block)) == {str(HIGH_RISK_BOUNDARY)}


@pytest.mark.parametrize("tsb, label", list(form_probes()))
def test_planner_uses_the_same_bands(tsb, label):
    from velomate.planner import form_band

    assert form_band(tsb) == label.lower()
@pytest.mark.parametrize("units", ["metric", "imperial"])
@pytest.mark.parametrize("filename, panel_id, durations", [
    ("activity.json", 40, [5, 10, 30, 60, 120, 300, 600, 1200, 3600]),
    ("all-time-progression.json", 21, [60, 300, 1200]),
    ("overview.json", 1701, [300]),
    ("all-time-progression.json", 1700, [300]),
])
def test_power_bests_only_count_full_windows(units, filename, panel_id, durations):
    """`ROWS BETWEEN N-1 PRECEDING` also yields a value for a ride's first N-1
    rows, so without a row count on the same window a 290 s ride reports a
    60-minute best. The ingestor applies this guard to the same window
    (estimate_ftp, compute_mean_maximal_power).

    tests/test_power_best_sql.py executes this SQL, but it needs a database.
    This is the half CI always runs."""
    with open(os.path.join(DASHBOARD_DIR, units, filename)) as f:
        panel = next(p for p in iter_all_panels(json.load(f)) if p["id"] == panel_id)
    arms = [arm for t in panel["targets"] for arm in t["rawSql"].split("UNION ALL")]

    assert len(arms) == len(durations)
    for arm, duration in zip(arms, durations):
        assert f"ROWS BETWEEN {duration - 1} PRECEDING" in arm
        assert re.search(r"AVG\(\S+\) OVER w\b", arm), arm
        assert "COUNT(*) OVER w AS window_size" in arm, arm
        assert re.search(rf"window_size >= {duration}\b", arm), arm


@pytest.mark.parametrize("filename", OVERVIEW_FILES)
@pytest.mark.parametrize("panel_id", AVERAGE_DELTA_CARD_IDS)
def test_average_delta_card_emits_no_row_for_an_empty_period(filename, panel_id):
    """An average over no rides is unknown. COALESCE-ing it to 0 shows the other
    period's whole average as the change, and returning NULL instead blanks the
    whole dashboard, because this is a colorMode:value stat. So the query must
    drop the row, which renders the panel's noValue.

    tests/test_delta_cards_sql.py executes this SQL, but it needs a database.
    This is the half CI always runs."""
    panel = load_panel(filename, panel_id)
    sql = panel["targets"][0]["rawSql"]

    assert panel["options"]["colorMode"] == "value"
    assert panel["fieldConfig"]["defaults"]["noValue"]
    assert "COALESCE" not in sql.upper()
    assert "FROM curr, prev\nWHERE curr.avg IS NOT NULL AND prev.avg IS NOT NULL;" in sql


@pytest.mark.parametrize("units", ["metric", "imperial"])
@pytest.mark.parametrize("filename, panel_id", [
    ("overview.json", 1701),
    ("all-time-progression.json", 1700),
])
def test_vo2max_is_sitko_on_best_five_minute_power(units, filename, panel_id):
    """CP is a sustainable intensity and the ACSM equation is sub-maximal, so the
    pair read about 10 ml/kg/min low. Sitko's equation is fitted on 5-minute power,
    and it needs the weight of the ride that set that power.

    tests/test_vo2max_sql.py executes this SQL, but it needs a database. This is
    the half CI always runs."""
    panel = load_unit_panel(units, filename, panel_id)
    sql = panel["targets"][0]["rawSql"]

    assert re.search(r"16\.6 \+ 8\.87 \* \w+\.watts / \w+\.ride_weight", sql), sql
    assert "a.ride_weight > 0" in sql
    assert "cp_estimates" not in sql
    assert "Sitko" in panel["description"]
    assert "Storer" not in panel["description"]


@pytest.mark.parametrize("filename", OVERVIEW_FILES)
def test_vo2max_card_tooltip_icons_match_the_chart(filename):
    panel = load_panel(filename, 1701)
    steps = panel["fieldConfig"]["defaults"]["thresholds"]["steps"]
    icons = list(tooltip_emoji_by_label(panel["description"]).values())

    assert icons == [PALETTE_EMOJI[step["color"]] for step in steps]


@pytest.mark.parametrize("filename", OVERVIEW_FILES)
def test_overview_second_stat_strip_fills_the_width_without_overlap(filename):
    with open(os.path.join(DASHBOARD_DIR, filename)) as f:
        panels = json.load(f)["panels"]
    strip = sorted(
        (p["gridPos"]["x"], p["gridPos"]["x"] + p["gridPos"]["w"])
        for p in panels if p["gridPos"]["y"] == 33
    )

    assert strip[0][0] == 0 and strip[-1][1] == 24
    assert all(left[1] == right[0] for left, right in zip(strip, strip[1:])), strip


@pytest.mark.parametrize("filename", OVERVIEW_FILES)
def test_ride_types_is_tall_enough_to_draw_its_donut(filename):
    """At 3 rows, what the legend leaves of the panel is a dot."""
    assert load_panel(filename, 402)["gridPos"]["h"] >= 6


@pytest.mark.parametrize("units", ["metric", "imperial"])
@pytest.mark.parametrize("filename, panel_id", POWER_ZONE_PANELS)
def test_power_zones_bin_at_the_rides_stored_ftp(units, filename, panel_id):
    """IF, intervals and kJ above FTP use ride_ftp, so zones must too, or the
    same page disagrees with itself and the monthly chart re-zones history. The
    stored FTP goes first, and the old chain stays behind it for a ride that has
    none. The cast keeps the arithmetic numeric, because ride_ftp is a FLOAT and
    in float8 a zone edge lands just above its exact value.

    tests/test_power_zones_sql.py executes this SQL, but it needs a database.
    This is the half CI always runs."""
    sql = load_panel(f"{units}/{filename}", panel_id)["targets"][0]["rawSql"]

    chain = ["NULLIF(a.ride_ftp, 0)::numeric", "'configured_ftp'", "'estimated_ftp'", "150\n"]
    positions = [sql.index(term) for term in chain]
    assert positions == sorted(positions)


def test_every_panel_binning_power_at_ftp_is_on_the_stored_ftp():
    """A new zone panel can't quietly go back to today's FTP."""
    binning = set()
    for filename in get_dashboard_files():
        with open(os.path.join(DASHBOARD_DIR, filename)) as f:
            dashboard = json.load(f)
        for panel in iter_all_panels(dashboard):
            sql = " ".join(t.get("rawSql", "") for t in panel.get("targets") or [])
            if "configured_ftp" in sql and "0.55" in sql:
                binning.add((os.path.basename(filename), panel["id"]))

    assert binning == set(POWER_ZONE_PANELS)


@pytest.mark.parametrize("units", ["metric", "imperial"])
@pytest.mark.parametrize("filename, panel_id", [
    ("activity.json", 32),
    ("activity.json", 403),
    ("all-time-progression.json", 30),
])
def test_zone_tooltip_says_which_ftp_the_panel_bins_at(units, filename, panel_id):
    description = load_panel(f"{units}/{filename}", panel_id)["description"]

    assert "FTP stored with" in description
    assert "VELOMATE_FTP" not in description
    assert "Based on configured FTP" not in description


LOAD_RATIO_ID = 1410
WORK_SPLIT_ID = 620
# Garmin's table, read on the one-decimal value the card shows. Each entry is
# (lower bound, tooltip range, label, palette hex).
LOAD_RATIO_BANDS = [
    (None, "< 0.8", "Low", "#3498db"),
    (0.8, "0.8 – 1.4", "Optimal", "#2ecc71"),
    (1.5, "1.5 – 1.9", "High", "#f1c40f"),
    (2.0, "≥ 2.0", "Very high", "#e74c3c"),
]
WORK_SPLIT_SERIES = {"Below FTP": "#3498db", "Above FTP": "#e67e22"}


@pytest.mark.parametrize("filename", OVERVIEW_FILES)
def test_load_ratio_thresholds_are_the_bands(filename):
    steps = load_panel(filename, LOAD_RATIO_ID)["fieldConfig"]["defaults"]["thresholds"]["steps"]

    assert [(s["value"], s["color"]) for s in steps] == [
        (bound, colour) for bound, _, _, colour in LOAD_RATIO_BANDS
    ]


@pytest.mark.parametrize("filename", OVERVIEW_FILES)
def test_load_ratio_tooltip_lists_exactly_these_bands(filename):
    description = load_panel(filename, LOAD_RATIO_ID)["description"]
    lines = [p for p in description.split("\n\n") if " — " in p]

    assert lines == [
        f"{PALETTE_EMOJI[colour]} {band} — {label}"
        for _, band, label, colour in LOAD_RATIO_BANDS
    ]


@pytest.mark.parametrize("filename", OVERVIEW_FILES)
def test_load_ratio_tooltip_says_whose_bands_and_warns_about_low_ctl(filename):
    """The bands come from a different load model, and on a small CTL one ride
    reaches the top of them. A rider reading a red card needs to know both."""
    description = load_panel(filename, LOAD_RATIO_ID)["description"]

    assert "Garmin" in description and "EPOC" in description
    assert "Noisy at low CTL" in description


@pytest.mark.parametrize("filename", OVERVIEW_FILES)
def test_load_ratio_picks_the_latest_day_before_guarding_it(filename):
    """A guard outside the LIMIT reports an older day's ratio as today's.

    tests/test_load_panels_sql.py executes this SQL, but it needs a database. This
    is the half CI always runs."""
    panel = load_panel(filename, LOAD_RATIO_ID)
    sql = panel["targets"][0]["rawSql"]

    assert panel["fieldConfig"]["defaults"]["noValue"]
    assert "ORDER BY date DESC LIMIT 1) latest" in sql
    assert sql.rstrip(";\n").endswith("WHERE ctl > 0 AND atl IS NOT NULL")
    assert "COALESCE" not in sql.upper()


@pytest.mark.parametrize("filename", OVERVIEW_FILES)
def test_work_split_reads_stored_values_only(filename):
    """Below + above has to equal stored work, so a ride missing either value is
    left out of both series rather than counted in one.

    tests/test_load_panels_sql.py executes this SQL, but it needs a database. This
    is the half CI always runs."""
    sql = load_panel(filename, WORK_SPLIT_ID)["targets"][0]["rawSql"]

    assert "SUM(a.work_kj - a.kj_above_ftp)" in sql
    assert "SUM(a.kj_above_ftp)" in sql
    assert "a.work_kj IS NOT NULL AND a.kj_above_ftp IS NOT NULL" in sql
    assert "activity_streams" not in sql
    assert "cp_estimates" not in sql


@pytest.mark.parametrize("filename", OVERVIEW_FILES)
def test_work_split_is_a_stacked_bar_of_whole_weeks(filename):
    panel = load_panel(filename, WORK_SPLIT_ID)
    custom = panel["fieldConfig"]["defaults"]["custom"]
    sql = panel["targets"][0]["rawSql"]

    assert custom["drawStyle"] == "bars"
    assert custom["stacking"]["mode"] == "normal"
    assert "generate_series(date_trunc('week', $__timeFrom()::timestamptz)" in sql
    assert "ORDER BY gs" in sql
    assert "$__timeFilter" not in sql, "a filter on the ride would cut the first week"


@pytest.mark.parametrize("filename", OVERVIEW_FILES)
def test_work_split_keeps_its_own_12_week_range(filename):
    """Overview opens on 7 days, where a weekly chart has two bars at most."""
    panel = load_panel(filename, WORK_SPLIT_ID)

    assert panel["timeFrom"] == "12w"
    assert "last 12 weeks" in panel["description"]


@pytest.mark.parametrize("filename", OVERVIEW_FILES)
def test_work_split_tooltip_icons_match_the_chart(filename):
    panel = load_panel(filename, WORK_SPLIT_ID)
    chart = {
        override["matcher"]["options"]: prop["value"]["fixedColor"]
        for override in panel["fieldConfig"]["overrides"]
        for prop in override["properties"] if prop["id"] == "color"
    }

    assert chart == WORK_SPLIT_SERIES
    assert tooltip_emoji_by_label(panel["description"]) == {
        series: PALETTE_EMOJI[colour] for series, colour in chart.items()
    }


@pytest.mark.parametrize("filename", OVERVIEW_FILES)
def test_work_split_tooltip_says_what_load_is_and_where_the_line_is(filename):
    description = load_panel(filename, WORK_SPLIT_ID)["description"]

    assert "kJ" in description
    assert "FTP stored with" in description
    assert "not at CP" in description


@pytest.mark.parametrize("filename", OVERVIEW_FILES)
def test_load_panels_overlap_nothing(filename):
    def cells(panel):
        grid = panel["gridPos"]
        return {
            (x, y)
            for x in range(grid["x"], grid["x"] + grid["w"])
            for y in range(grid["y"], grid["y"] + grid["h"])
        }

    with open(os.path.join(DASHBOARD_DIR, filename)) as f:
        panels = list(iter_all_panels(json.load(f)))

    for panel_id in (LOAD_RATIO_ID, WORK_SPLIT_ID):
        own = next(p for p in panels if p["id"] == panel_id)
        for other in panels:
            if other["id"] != panel_id:
                assert not cells(own) & cells(other), (panel_id, other["id"])


@pytest.mark.parametrize("panel_id", [LOAD_RATIO_ID, WORK_SPLIT_ID])
def test_imperial_leaves_the_load_panels_alone(panel_id):
    """Neither panel has a distance or speed in it, so the generator has nothing
    to convert."""
    assert load_panel("imperial/overview.json", panel_id) == load_panel(
        "metric/overview.json", panel_id
    )


WBAL_PANELS = {1200: "Min W'bal", 1201: "Time W'bal < 25%", 1202: "W'bal (Anaerobic Battery)"}


@pytest.mark.parametrize("units", ["metric", "imperial"])
@pytest.mark.parametrize("panel_id", WBAL_PANELS)
def test_wbal_tooltip_names_the_estimate_the_ride_was_modelled_with(units, panel_id):
    panel = load_panel(f"{units}/activity.json", panel_id)

    assert panel["title"] == WBAL_PANELS[panel_id]
    assert "ride's date" in panel["description"]
    assert "latest" not in panel["description"].lower()


@pytest.mark.parametrize("units", ["metric", "imperial"])
def test_wbal_threshold_reads_the_w_prime_stored_on_the_ride(units):
    """w_bal is in joules and so is wbal_w_prime_j: a 1000 here would put the line
    three orders of magnitude out and read as a plausible 0%."""
    sql = load_panel(f"{units}/activity.json", 1201)["targets"][0]["rawSql"]

    assert "wbal_w_prime_j * 0.25" in sql
    assert "cp_estimates" not in sql and "w_prime_kj" not in sql
    assert "1000" not in sql


@pytest.mark.parametrize("units", ["metric", "imperial"])
@pytest.mark.parametrize("panel_id", [1200, 1201])
def test_wbal_aggregates_group_so_a_ride_without_wbal_gives_no_row(units, panel_id):
    """An aggregate with no GROUP BY returns one row over no input, and here that
    row is NULL. A power-less ride has to reach the panel's noValue instead."""
    sql = load_panel(f"{units}/activity.json", panel_id)["targets"][0]["rawSql"]

    assert "GROUP BY" in sql.upper()


def test_no_activity_panel_interprets_a_ride_against_the_estimates_table():
    """cp_estimates describes today's fitness or a past day's. Nothing on a page
    about one ride has a reason to ask it, because the ride stores what it used."""
    with open(os.path.join(DASHBOARD_DIR, "metric", "activity.json")) as f:
        dashboard = json.load(f)

    readers = [p["id"] for p in iter_all_panels(dashboard)
               for t in p.get("targets") or [] if "cp_estimates" in t.get("rawSql", "")]

    assert readers == []


def _ingestor_source(filename):
    path = os.path.join(os.path.dirname(__file__), "..", "ingestor", filename)
    with open(path) as f:
        return f.read()


def test_every_reset_of_derived_columns_clears_the_stored_w_prime():
    """The real-database half of this is tests/test_wbal_per_ride.py, which CI
    skips. A column missing from any of these is the stale-W' bug."""
    version_reset = next(line for line in _ingestor_source("fitness.py").splitlines()
                         if "UPDATE activities SET tss = NULL" in line)
    db_source = _ingestor_source("db.py")
    stream_reset = db_source[db_source.index("def _clear_stream_derived"):db_source.index("def _merge_in_place")]
    replace = db_source[db_source.index("def _replace_streams"):db_source.index("def upsert_streams")]

    for source in (version_reset, stream_reset, replace):
        assert "wbal_cp = NULL" in source
        assert "wbal_w_prime_j = NULL" in source


def test_a_missing_stored_w_prime_selects_a_ride_for_the_wbal_step():
    """That NULL is how a stream modelled before the columns existed gets one, and
    how a merge that kept its streams gets its W' back."""
    source = _ingestor_source("fitness.py")
    step = source[source.index("def compute_wbal_for_rides"):source.index("def detect_climbs_for_rides")]

    assert "a.wbal_w_prime_j IS NULL" in step
