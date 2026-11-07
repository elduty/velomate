"""Every dashboard query, run on the data states that have broken them before.

A stat panel whose query emits a NULL value row blanks the whole dashboard in
Grafana 12.4, not just that panel. Guards for it were added one panel at a time,
for the cases someone thought of. This module reads the queries from the dashboard
JSON instead, so a new panel is covered without being listed anywhere. The imperial
set runs too: scripts/gen_imperial_dashboards.py rewrites its SQL, so the metric
query passing says nothing about the one Grafana sends for imperial.

Each query runs on three states, built through the real ingest path and a real
recalculation rather than inserted by hand, so a derived column holds what the
ingestor would leave in it:

- an empty database, where a query variable has no options and goes in empty
- one GPS-only ride: no power, HR, cadence or weight
- one normal ride, the control: a query that returns the same thing there as on
  the empty database never saw the ride, and its passes elsewhere prove nothing

A stat panel is held to "no rows or a number" whatever its colorMode. The crash
has only been documented on colorMode value, but nothing shows background is safe.

An expected failure is a named entry below with its reason, and is checked in both
directions, so one that stops failing has to be removed. pytest's xfail can't be
used: scripts/run_pg_tests.py fails a run on any skip, and xfail reports as one.

Run with:
    VELOMATE_TEST_DATABASE_URL=postgresql://... python3 -m pytest \
        tests/test_every_dashboard_query.py -q
"""

import collections
import datetime
import decimal
import glob
import json
import math
import os

import pytest

from grafana_sql import expand
from pg_reset import reset_schema

TEST_DB = os.environ.get("VELOMATE_TEST_DATABASE_URL")

DASHBOARDS_DIR = os.path.join(os.path.dirname(__file__), "..", "grafana", "dashboards")
UNIT_SETS = ("metric", "imperial")

EMPTY, GPS_ONLY, NORMAL_RIDE = "empty", "gps_only", "normal_ride"
STATES = (EMPTY, GPS_ONLY, NORMAL_RIDE)

NOW = datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0)
# The start of today, so the ride is in the current day, week, month and year
# whenever this runs, and a query's result doesn't depend on the weekday.
RIDE_DATE = NOW.replace(hour=0, minute=0, second=0)
TIME_FROM = (NOW - datetime.timedelta(days=730)).strftime("%Y-%m-%dT%H:%M:%SZ")
TIME_TO = NOW.strftime("%Y-%m-%dT%H:%M:%SZ")

# What ${activity_id}, ${route} and ${segment} hold on an install with nothing to pick: a query
# variable with no options and no "All" is an empty string, and it goes in as bare
# text. A made-up id here would be valid SQL, and hide every query that can't take it.
NO_OPTIONS = ""

RIDE_SECONDS = 3600
FTP_WATTS = "200"
WEIGHT_KG = "75"
SETTINGS = ("VELOMATE_FTP", "VELOMATE_MAX_HR", "VELOMATE_RESTING_HR",
            "VELOMATE_WEIGHT", "STRAVA_REFRESH_TOKEN")

# A dashboard file is named with its unit set, "imperial/overview.json", so an entry
# holds for one set and is checked against that set alone. The generated query can
# fail where the metric one doesn't.

# (dashboard file, panel id, refId, state): why it fails. For a query the small
# guard can't fix, or whose behaviour is arguably right. Empty is the goal.
KNOWN_SQL_ERRORS = {}

# The same, for a stat panel emitting a row that isn't all numbers.
KNOWN_NULL_ROWS = {}


def _in_each_unit_set(entries):
    """An entry for both sets. This rests on the generator keeping panel ids and
    refIds, which test_unit_sets_hold_the_same_panels checks. It stays one entry per
    set, so a query that only one set's rewrite changes still fails on that set."""
    return {(f"{units}/{file}", *rest): why
            for units in UNIT_SETS
            for (file, *rest), why in entries.items()}


# (dashboard file, panel id, refId): why one ride can't change what it returns.
_ALL_TIME = "all-time-progression.json"
_OVERVIEW = "overview.json"
_ANOTHER_SPORT = "a series for another sport, and the ride is outdoor"
_NEEDS_PREVIOUS_PERIOD = "compares with the previous period, which has no ride"
NOT_REACHED_BY_ONE_RIDE = _in_each_unit_set({
    **{(_ALL_TIME, panel, ref): _ANOTHER_SPORT
       for panel in (60, 61, 62, 63) for ref in "BCD"},
    (_ALL_TIME, 70, "B"): "last year's series, and the ride is this year",
    (_OVERVIEW, 307, "A"): _NEEDS_PREVIOUS_PERIOD,
    (_OVERVIEW, 308, "A"): _NEEDS_PREVIOUS_PERIOD,
    (_OVERVIEW, 309, "A"): _NEEDS_PREVIOUS_PERIOD,
    (_OVERVIEW, 1012, "A"): _NEEDS_PREVIOUS_PERIOD,
    (_OVERVIEW, 403, "A"): "needs fitness from six weeks ago",
    (_OVERVIEW, 1402, "A"): "needs fitness from a week ago",
})

Query = collections.namedtuple(
    "Query", "file panel_id ref_id title panel_type color_mode sql variables"
)
Outcome = collections.namedtuple("Outcome", "rows error")
State = collections.namedtuple("State", "outcomes rides streams")


def _panels(container):
    """Every panel, at any depth: a collapsed row holds its children itself."""
    for panel in container.get("panels", []):
        yield panel
        yield from _panels(panel)


def _custom_variables(dashboard):
    """Custom variables at the value the dashboard opens with, as (name, value)."""
    return tuple(
        (variable["name"], variable["current"]["value"])
        for variable in dashboard.get("templating", {}).get("list", [])
        if variable["type"] == "custom"
    )


def _dashboard_files(root=DASHBOARDS_DIR):
    return sorted(glob.glob(os.path.join(root, "*", "*.json")))


def _load(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def discover(root=DASHBOARDS_DIR):
    """Every target of every panel of every dashboard of every unit set under root."""
    queries = []
    for path in _dashboard_files(root):
        dashboard = _load(path)
        for panel in _panels(dashboard):
            for target in panel.get("targets", []):
                queries.append(Query(
                    file=os.path.relpath(path, root),
                    panel_id=panel["id"],
                    ref_id=target["refId"],
                    title=panel.get("title"),
                    panel_type=panel["type"],
                    color_mode=(panel.get("options") or {}).get("colorMode"),
                    sql=target["rawSql"],
                    variables=_custom_variables(dashboard),
                ))
    return queries


QUERIES = discover()
STAT_QUERIES = [query for query in QUERIES if query.panel_type == "stat"]


def _query_id(query):
    return f"{query.file}-panel{query.panel_id}-{query.ref_id}"


def _key(query):
    return query.file, query.panel_id, query.ref_id


def _label(query):
    return f"{_query_id(query)} {query.title!r}"


def _hill(second):
    """A 200 m climb between the 10th and 25th minute, then back down."""
    if second < 600:
        return 60.0
    if second < 1500:
        return 60.0 + (second - 600) * 200 / 900
    if second < 2400:
        return 260.0 - (second - 1500) * 200 / 900
    return 60.0


def _position(second):
    """One GPS sample of a ride heading north at about 24 km/h."""
    return {
        "time_offset": second,
        "lat": 38.7 + second * 0.00006,
        "lng": -9.14,
        "speed_kmh": 24.0,
        "altitude_m": _hill(second),
    }


def _power(second):
    """A one-minute burst then 20 minutes at 250 W, which fits a Critical Power of
    250 W. Then easy riding in the two lower polarization zones, so with the burst
    all three hold time, and a coast in each block."""
    if second < 60:
        return 550
    if second < 1260:
        return 250
    in_block = second % 300
    if in_block < 150:
        return 170
    return 0 if in_block >= 270 else 100


def _with_sensors(sample):
    power = _power(sample["time_offset"])
    return {**sample, "power": power, "hr": 120 + power // 6,
            "cadence": 85 if power else 0}


def _ride(**sensors):
    """An activity as an intervals.icu sync hands it to the database."""
    return {
        "intervals_icu_id": "i1",
        "name": "Synthetic",
        "date": RIDE_DATE.isoformat(),
        "distance_m": 24000.0,
        "duration_s": RIDE_SECONDS,
        "elevation_m": 200.0,
        "avg_speed_kmh": 24.0,
        "avg_hr": None,
        "max_hr": None,
        "avg_power": None,
        "max_power": None,
        "avg_cadence": None,
        "calories": None,
        "suffer_score": None,
        "device": "unknown",
        **sensors,
    }


def _ingest(conn, ride, streams):
    """A poll that delivered one ride. Returns its query variables."""
    import db as velomate_db
    import fitness

    activity_id, _ = velomate_db.upsert_activity(conn, ride, streams)
    velomate_db.set_sync_state(conn, "last_successful_poll", "ok")
    velomate_db.set_sync_state(conn, "intervals_icu_skipped_stubs", "0")
    fitness.recalculate_fitness(conn)
    with conn.cursor() as cur:
        cur.execute("SELECT route_id FROM activities WHERE id = %s", (activity_id,))
        route = cur.fetchone()[0]
    return {"activity_id": activity_id, "route": route,
            "segment": _timed_segment(conn, activity_id)}


def _timed_segment(conn, activity_id):
    """A segment with this ride's effort on it, which is the climb in _hill.

    Inserted by hand because it takes two rides to make a segment, and a ride that
    never reached the Climbs panels would leave their passes meaning nothing. Its
    speed and heart rate are the ride's, so a GPS-only ride has neither sensor column.
    """
    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO climb_segments (auto_name, start_lat, start_lng, end_lat, end_lng,
                                        start_alt, end_alt, length_m, gain_m, avg_grade,
                                        category)
            VALUES ('60→260 m · 6.0 km · 3.3%%', 38.736, -9.14, 38.79, -9.14, 60, 260,
                    6000, 200, 3.3, 'Cat 3')
            RETURNING id""")
        segment_id = cur.fetchone()[0]
        cur.execute("""
            INSERT INTO climb_efforts (segment_id, activity_id, start_offset, end_offset,
                                       elapsed_s, moving_s, avg_speed_kmh, vam_mh, avg_hr,
                                       avg_power)
            SELECT %s, %s, 600, 1500, 900, 900, 24.0, 800,
                   ROUND(AVG(hr)), ROUND(AVG(power))
            FROM activity_streams
            WHERE activity_id = %s AND time_offset BETWEEN 600 AND 1500
            """, (segment_id, activity_id, activity_id))
    return segment_id


def _seed(conn, state, env):
    """Fill the database for a state. Returns its query variables."""
    import db as velomate_db

    positions = [_position(second) for second in range(RIDE_SECONDS)]
    if state == EMPTY:
        return {"activity_id": NO_OPTIONS, "route": NO_OPTIONS, "segment": NO_OPTIONS}
    if state == GPS_ONLY:
        return _ingest(conn, _ride(), positions)

    env.setenv("VELOMATE_FTP", FTP_WATTS)
    env.setenv("VELOMATE_WEIGHT", WEIGHT_KG)
    velomate_db.set_sync_state(conn, "configured_ftp", FTP_WATTS)
    return _ingest(
        conn,
        _ride(avg_hr=150, max_hr=211, avg_power=210, max_power=550, avg_cadence=80,
              calories=740),
        [_with_sensors(sample) for sample in positions],
    )


def _run(conn, query, variables):
    """A query's result. An error is an outcome, so it fails that panel alone."""
    import psycopg2

    try:
        sql = expand(query.sql, {**dict(query.variables), **variables},
                     TIME_FROM, TIME_TO)
        with conn.cursor() as cur:
            cur.execute(sql)
            return Outcome(cur.fetchall(), None)
    except (ValueError, psycopg2.Error) as error:
        return Outcome([], str(error).strip())


def _build_state(conn, state):
    import db as velomate_db

    with pytest.MonkeyPatch.context() as env:
        for name in SETTINGS:
            env.delenv(name, raising=False)
        reset_schema(conn)
        velomate_db.create_schema(conn)
        variables = _seed(conn, state, env)
    with conn.cursor() as cur:
        cur.execute("SELECT avg_power, avg_hr, avg_cadence, ride_weight FROM activities")
        rides = cur.fetchall()
        cur.execute("""SELECT COUNT(lat), COUNT(lng), COUNT(speed_kmh),
                              COUNT(altitude_m), COUNT(power), COUNT(hr),
                              COUNT(cadence)
                       FROM activity_streams""")
        streams = cur.fetchone()
    return State({query: _run(conn, query, variables) for query in QUERIES},
                 rides, streams)


@pytest.fixture(scope="module")
def states():
    """{state: State}, built once and kept in memory: the queries only read."""
    if not TEST_DB:
        pytest.skip("needs a throwaway PostgreSQL in VELOMATE_TEST_DATABASE_URL")

    import psycopg2

    # Same leak as tests/test_route_id_stability.py: in a whole-suite run this
    # file can inherit another module's mocked psycopg2. A mock has no __file__.
    if not hasattr(psycopg2, "__file__"):
        pytest.skip(
            "psycopg2 is mocked by another module in this pytest session; run "
            "this file on its own: python -m pytest "
            "tests/test_every_dashboard_query.py"
        )

    # UTC, so RIDE_DATE is today for a query's ::date cast on any server.
    connection = psycopg2.connect(TEST_DB, options="-c timezone=UTC")
    connection.autocommit = True
    try:
        return {state: _build_state(connection, state) for state in STATES}
    finally:
        connection.close()


def _is_value(cell):
    """A number, or a date, which Grafana reads as one. Not NULL, NaN or text."""
    if isinstance(cell, datetime.date):
        return True
    return isinstance(cell, (int, float, decimal.Decimal)) and math.isfinite(cell)


def _bad_rows(rows):
    return [row for row in rows if not all(_is_value(cell) for cell in row)]


def _without_dates(rows):
    """What a ride can change. Time columns move with the clock, not the data."""
    return [[cell for cell in row if not isinstance(cell, datetime.date)]
            for row in rows]


def _raw_sql_count(node):
    """rawSql keys anywhere in the JSON, by a walk that knows nothing of panels."""
    if isinstance(node, dict):
        return ("rawSql" in node) + sum(_raw_sql_count(v) for v in node.values())
    if isinstance(node, list):
        return sum(_raw_sql_count(item) for item in node)
    return 0


def _by_panel(units):
    """{(dashboard, panel id, refId): query} for one unit set."""
    return {(query.file.removeprefix(f"{units}/"), query.panel_id, query.ref_id): query
            for query in QUERIES if query.file.startswith(f"{units}/")}


def test_every_dashboard_has_queries():
    files = {os.path.relpath(path, DASHBOARDS_DIR) for path in _dashboard_files()}

    for units in UNIT_SETS:
        assert len([file for file in files if file.startswith(f"{units}/")]) >= 4
    assert {query.file for query in QUERIES} == files
    assert STAT_QUERIES


def test_unit_sets_hold_the_same_panels():
    """What lets one entry in the lists above stand for both sets. The drift test in
    test_imperial_dashboards.py keeps the generated files current; this is about
    what the generator is allowed to change."""
    assert _by_panel("imperial").keys() == _by_panel("metric").keys()


def test_imperial_runs_its_own_sql():
    """Discovery reading the metric files twice would pass everything else here."""
    metric, imperial = ({query.sql for query in _by_panel(units).values()}
                        for units in UNIT_SETS)

    assert metric != imperial


def test_discovery_finds_every_query_in_the_json():
    """A panel moved somewhere the walk doesn't look would otherwise just vanish
    from the run, and everything left would still pass."""
    assert len(QUERIES) == sum(_raw_sql_count(_load(path))
                               for path in _dashboard_files())


def test_every_stat_panel_is_held_to_the_rule():
    """Whatever its colorMode. Narrowing the rule would pass while the dashboards
    are clean, and is exactly how a background panel would stop being checked."""
    panels = [panel for path in _dashboard_files()
              for panel in _panels(_load(path)) if panel["type"] == "stat"]

    assert len(STAT_QUERIES) == sum(len(panel["targets"]) for panel in panels)


def test_no_query_variable_changes_what_an_empty_install_sends():
    """NO_OPTIONS holds for a plain single-value variable. "All", an allValue or
    multi-select would send something else, and the empty state would then be
    testing a value Grafana never sends."""
    variables = [variable for path in _dashboard_files()
                 for variable in _load(path)["templating"]["list"]
                 if variable["type"] == "query"]

    assert {variable["name"] for variable in variables} == {"activity_id", "route", "segment"}
    for variable in variables:
        assert not {"includeAll", "allValue", "multi"} & {
            key for key, value in variable.items() if value
        }, variable["name"]


@pytest.mark.parametrize("units", UNIT_SETS)
def test_discovery_finds_a_panel_nothing_lists(tmp_path, units):
    """A new panel is covered by being in the JSON, including inside a row."""
    (tmp_path / units).mkdir()
    (tmp_path / units / "new.json").write_text(json.dumps({"panels": [{
        "id": 1, "type": "row", "panels": [{
            "id": 2, "type": "stat", "title": "New",
            "targets": [{"refId": "A", "rawSql": "SELECT NULL"}],
        }],
    }]}))

    assert [(q.file, q.panel_id, q.ref_id) for q in discover(tmp_path)] == [
        (f"{units}/new.json", 2, "A")
    ]


def test_expected_failures_name_real_queries():
    keys = {_key(query) for query in QUERIES}

    assert not {key for key in NOT_REACHED_BY_ONE_RIDE if key not in keys}
    for known in (KNOWN_SQL_ERRORS, KNOWN_NULL_ROWS):
        assert not {key for key in known if key[:3] not in keys or key[3] not in STATES}


def test_the_states_hold_what_they_claim(states):
    samples = RIDE_SECONDS

    assert states[EMPTY].rides == []
    assert states[GPS_ONLY].rides == [(None, None, None, None)]
    assert states[GPS_ONLY].streams == (samples, samples, samples, samples, 0, 0, 0)

    (power, hr, cadence, weight), = states[NORMAL_RIDE].rides
    assert None not in (power, hr, cadence, weight)
    assert states[NORMAL_RIDE].streams == (samples,) * 7


@pytest.mark.parametrize("state", STATES)
@pytest.mark.parametrize("query", QUERIES, ids=_query_id)
def test_query_executes(states, query, state):
    error = states[state].outcomes[query].error
    if (*_key(query), state) in KNOWN_SQL_ERRORS:
        assert error, f"{_label(query)} runs now: remove it from KNOWN_SQL_ERRORS"
    else:
        assert error is None, f"{_label(query)} failed on {state}: {error}"


@pytest.mark.parametrize("state", STATES)
@pytest.mark.parametrize("query", STAT_QUERIES, ids=_query_id)
def test_stat_panel_emits_no_rows_or_a_number(states, query, state):
    bad = _bad_rows(states[state].outcomes[query].rows)
    if (*_key(query), state) in KNOWN_NULL_ROWS:
        assert bad, f"{_label(query)} is clean now: remove it from KNOWN_NULL_ROWS"
    else:
        assert not bad, (
            f"{_label(query)} (colorMode {query.color_mode}) on {state} emitted {bad}. "
            "A query with nothing to show must return no row: filter on the value."
        )


@pytest.mark.parametrize("query", QUERIES, ids=_query_id)
def test_a_normal_ride_reaches_the_query(states, query):
    """Without this, a query running against nothing passes everything above: no
    rows and no NULL. Rows alone don't show it, because a chart built on
    generate_series returns the same empty days whatever the database holds."""
    ride = _without_dates(states[NORMAL_RIDE].outcomes[query].rows)
    empty = _without_dates(states[EMPTY].outcomes[query].rows)
    if _key(query) in NOT_REACHED_BY_ONE_RIDE:
        assert ride == empty, (
            f"{_label(query)} sees the ride now: "
            "remove it from NOT_REACHED_BY_ONE_RIDE"
        )
    else:
        assert ride != empty, f"{_label(query)} returned the same with a ride as without"


@pytest.mark.parametrize("cell, expected", [
    (0, True),
    (1.5, True),
    (decimal.Decimal("-15.2"), True),
    (datetime.date(2026, 3, 1), True),
    (None, False),
    (float("nan"), False),
    (decimal.Decimal("NaN"), False),
    (float("inf"), False),
    ("12", False),
])
def test_is_value(cell, expected):
    assert _is_value(cell) is expected


@pytest.mark.parametrize("rows, bad", [
    ([], []),
    ([(1,)], []),
    ([(1, datetime.date(2026, 3, 1))], []),
    ([(None,)], [(None,)]),
    ([(1,), (None,)], [(None,)]),
    ([(1, None)], [(1, None)]),
])
def test_bad_rows(rows, bad):
    """Any row and any cell: the first row of a query can be the good one."""
    assert _bad_rows(rows) == bad


def test_expand_fills_variables_and_macros():
    sql = expand(
        "SELECT ${col} FROM t WHERE $__timeFilter(a.date) "
        "AND b > $__timeFrom() AND b < $__timeTo()",
        {"col": "speed"}, "2026-01-01T00:00:00Z", "2026-02-01T00:00:00Z",
    )

    assert sql == (
        "SELECT speed FROM t WHERE a.date BETWEEN '2026-01-01T00:00:00Z' AND "
        "'2026-02-01T00:00:00Z' AND b > '2026-01-01T00:00:00Z' "
        "AND b < '2026-02-01T00:00:00Z'"
    )


def test_expand_leaves_a_regular_expression_alone():
    """Overview's Skipped Rides panel, where a $ is not a Grafana token."""
    sql = "SELECT 1 WHERE value ~ '^[0-9]+$'"

    assert expand(sql) == sql


@pytest.mark.parametrize("sql", [
    "SELECT ${route}",
    "SELECT $route",
    "SELECT [[route]]",
    "SELECT $__timeGroup(date, '1d')",
    "SELECT $__timeFrom()",
])
def test_expand_refuses_what_it_did_not_fill_in(sql):
    with pytest.raises(ValueError, match="unexpanded Grafana token"):
        expand(sql)
