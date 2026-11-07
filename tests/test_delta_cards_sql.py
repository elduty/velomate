"""The Overview Δ Avg cards must not invent a change from an empty period.

An average over no rides is unknown, not 0. Treating it as 0 made the whole
average of the other period read as the change: "+210 W" against an empty prior
period, and a large drop in HR for an empty current week.

The card can't return a NULL instead. A colorMode:value stat with a NULL value
row blanks the whole dashboard in Grafana 12.4, which is why the zero was added.
So an empty period has to produce no row at all, which renders the panel's
noValue text.

These tests execute the panels' own SQL, read from the dashboard JSON, because
the difference between a NULL row and no row only exists once the query runs.
tests/test_dashboards.py carries the static half, which runs without a database.

Run with:
    VELOMATE_TEST_DATABASE_URL=postgresql://... python3 -m pytest \
        tests/test_delta_cards_sql.py -q
"""

import json
import os

import pytest

from grafana_sql import expand
from pg_reset import reset_schema

TEST_DB = os.environ.get("VELOMATE_TEST_DATABASE_URL")

pytestmark = pytest.mark.skipif(
    not TEST_DB,
    reason="needs a throwaway PostgreSQL in VELOMATE_TEST_DATABASE_URL",
)

DASHBOARD_DIR = os.path.join(os.path.dirname(__file__), "..", "grafana", "dashboards")

# The current period, and the previous one the cards derive from its length.
PERIOD_START = "2026-03-08T00:00:00Z"
PERIOD_END = "2026-03-15T00:00:00Z"
IN_CURRENT = "2026-03-10 10:00:00+00"
IN_PREVIOUS = "2026-03-03 10:00:00+00"

# panel id, column, current value, previous value, expected change
CARDS = [
    (307, "avg_power", 250, 200, 50),
    (308, "avg_speed_kmh", 30.0, 25.0, 5.0),
    (309, "avg_hr", 150, 140, 10),
    (1012, "aerobic_decoupling", 6.5, 4.0, 2.5),
]
CARD_IDS = [card[0] for card in CARDS]
CARD_COLUMNS = {card[0]: card[1] for card in CARDS}
SPEED_CARD = 308


def _panel_sql(panel_id, sport_type="all", units="metric"):
    """The target's SQL as Grafana sends it: variables and macros expanded."""
    with open(os.path.join(DASHBOARD_DIR, units, "overview.json")) as f:
        panels = json.load(f)["panels"]
    panels += [child for p in panels for child in p.get("panels", [])]
    panel = next(p for p in panels if p["id"] == panel_id)
    return expand(
        panel["targets"][0]["rawSql"],
        {"sport_type": sport_type},
        PERIOD_START,
        PERIOD_END,
    )


def _rows(conn, panel_id, **kwargs):
    with conn.cursor() as cur:
        cur.execute(_panel_sql(panel_id, **kwargs))
        return cur.fetchall()


def _seed_ride(conn, date, column, value, sport_type="cycling_outdoor"):
    """A ride long enough for every card's filter, with one metric set."""
    with conn.cursor() as cur:
        cur.execute(
            f"""INSERT INTO activities (name, date, distance_m, duration_s,
                                        elevation_m, sport_type, {column})
                VALUES ('Synthetic', %s, 20000.0, 3600, 100.0, %s, %s)""",
            (date, sport_type, value),
        )


@pytest.fixture()
def conn():
    import psycopg2

    # Same leak as tests/test_route_id_stability.py: in a whole-suite run this
    # file can inherit another module's mocked psycopg2. A mock has no __file__.
    if not hasattr(psycopg2, "__file__"):
        pytest.skip(
            "psycopg2 is mocked by another module in this pytest session; run "
            "this file on its own: python -m pytest tests/test_delta_cards_sql.py"
        )

    import db as velomate_db

    connection = psycopg2.connect(TEST_DB)
    connection.autocommit = True
    reset_schema(connection)
    velomate_db.create_schema(connection)
    yield connection
    connection.close()


@pytest.mark.parametrize("panel_id, column, current, previous, change", CARDS)
def test_two_populated_periods_show_the_change(
    conn, panel_id, column, current, previous, change
):
    """The working case has to survive: a real number, in one row."""
    _seed_ride(conn, IN_CURRENT, column, current)
    _seed_ride(conn, IN_PREVIOUS, column, previous)

    assert [float(value) for value, in _rows(conn, panel_id)] == [change]


@pytest.mark.parametrize("panel_id, column, current, previous, change", CARDS)
def test_empty_previous_period_emits_no_row(
    conn, panel_id, column, current, previous, change
):
    """The '+210 W' in green: no row, not the whole current average."""
    _seed_ride(conn, IN_CURRENT, column, current)

    assert _rows(conn, panel_id) == []


@pytest.mark.parametrize("panel_id, column, current, previous, change", CARDS)
def test_empty_current_period_emits_no_row(
    conn, panel_id, column, current, previous, change
):
    """The quiet week: no row, not minus the whole previous average."""
    _seed_ride(conn, IN_PREVIOUS, column, previous)

    assert _rows(conn, panel_id) == []


@pytest.mark.parametrize("panel_id", CARD_IDS)
def test_two_empty_periods_emit_no_row(conn, panel_id):
    """Not 0, which reads as "no change", and not a NULL row."""
    assert _rows(conn, panel_id) == []


@pytest.mark.parametrize("panel_id, column, current, previous, change", CARDS)
def test_rides_that_lack_the_metric_leave_their_period_empty(
    conn, panel_id, column, current, previous, change
):
    """A period can hold rides and still have no average: a GPS-only ride has
    no power or HR. It must behave as empty, not as a row of NULL."""
    _seed_ride(conn, IN_CURRENT, column, current)
    _seed_ride(conn, IN_PREVIOUS, column, None)

    assert _rows(conn, panel_id) == []


@pytest.mark.parametrize("panel_id, column, current, previous, change", CARDS)
def test_a_period_filled_only_by_another_sport_is_empty(
    conn, panel_id, column, current, previous, change
):
    """The empty period is the one the dashboard's filter leaves, not the
    table's: both periods hold rides, only one holds a Zwift ride."""
    _seed_ride(conn, IN_CURRENT, column, current, sport_type="zwift")
    _seed_ride(conn, IN_PREVIOUS, column, previous, sport_type="cycling_outdoor")

    assert _rows(conn, panel_id, sport_type="zwift") == []


def test_imperial_speed_card_converts_the_change(conn):
    """The generator rewrites this card's output column, so its copy of the SQL
    is not the one the metric tests run."""
    _seed_ride(conn, IN_CURRENT, CARD_COLUMNS[SPEED_CARD], 30.0)
    _seed_ride(conn, IN_PREVIOUS, CARD_COLUMNS[SPEED_CARD], 25.0)

    assert [float(v) for v, in _rows(conn, SPEED_CARD, units="imperial")] == [3.1]


def test_imperial_speed_card_emits_no_row_for_an_empty_period(conn):
    _seed_ride(conn, IN_CURRENT, CARD_COLUMNS[SPEED_CARD], 30.0)

    assert _rows(conn, SPEED_CARD, units="imperial") == []
