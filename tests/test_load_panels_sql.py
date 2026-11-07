"""Load Ratio and Weekly Work Below / Above FTP on Overview.

These tests execute the panels' own SQL, read from the dashboard JSON, because the
behaviour under test *is* the SQL: which athlete_stats row the ratio reads, that a
missing base emits no row rather than a NULL one or a division by zero, and how
stored work lands in weekly buckets. tests/test_dashboards.py carries the static
half, which runs without a database.

Run with:
    VELOMATE_TEST_DATABASE_URL=postgresql://... python3 -m pytest \
        tests/test_load_panels_sql.py -q
"""

import json
import os
from decimal import Decimal

import pytest

from grafana_sql import expand
from pg_reset import reset_schema

TEST_DB = os.environ.get("VELOMATE_TEST_DATABASE_URL")

pytestmark = pytest.mark.skipif(
    not TEST_DB,
    reason="needs a throwaway PostgreSQL in VELOMATE_TEST_DATABASE_URL",
)

OVERVIEW = os.path.join(
    os.path.dirname(__file__), "..", "grafana", "dashboards", "metric", "overview.json"
)
LOAD_RATIO_ID = 1410
WORK_SPLIT_ID = 620

# Mondays, so a window written with them starts on a week boundary.
WEEK_1 = "2026-03-02"
WEEK_2 = "2026-03-09"
WEEK_3 = "2026-03-16"


def _panel(panel_id):
    with open(OVERVIEW) as f:
        return next(p for p in json.load(f)["panels"] if p["id"] == panel_id)


def _ratio_rows(conn):
    """The stat's raw rows. [] is the panel's noValue; [(None,)] would be a NULL
    value row, which is what must never happen."""
    with conn.cursor() as cur:
        cur.execute(_panel(LOAD_RATIO_ID)["targets"][0]["rawSql"])
        return cur.fetchall()


def _seed_stats(conn, *days):
    """athlete_stats rows as (days before today, ctl, atl)."""
    with conn.cursor() as cur:
        for days_ago, ctl, atl in days:
            cur.execute(
                "INSERT INTO athlete_stats (date, ctl, atl) "
                "VALUES (CURRENT_DATE - %s::int, %s, %s)",
                (days_ago, ctl, atl),
            )


def _band(value):
    """The colour Grafana paints a value: the last step at or below it."""
    steps = _panel(LOAD_RATIO_ID)["fieldConfig"]["defaults"]["thresholds"]["steps"]
    colour = steps[0]["color"]
    for step in steps[1:]:
        if value >= Decimal(str(step["value"])):
            colour = step["color"]
    return colour


def _seed_ride(conn, when, work_kj, kj_above_ftp, sport="cycling_outdoor"):
    with conn.cursor() as cur:
        cur.execute(
            """INSERT INTO activities (name, date, distance_m, duration_s, elevation_m,
                                       sport_type, work_kj, kj_above_ftp)
               VALUES ('Synthetic', %s, 20000.0, 3600, 100.0, %s, %s, %s)""",
            (when, sport, work_kj, kj_above_ftp),
        )


def _weeks(conn, time_from, time_to, sport="all"):
    """The chart's rows as Grafana sends them, as (Monday, below, above)."""
    sql = expand(
        _panel(WORK_SPLIT_ID)["targets"][0]["rawSql"],
        {"sport_type": sport},
        f"{time_from}T00:00:00Z",
        f"{time_to}T23:59:59Z",
    )
    with conn.cursor() as cur:
        cur.execute(sql)
        rows = cur.fetchall()
    stamps = [when for when, _, _ in rows]
    assert stamps == sorted(set(stamps)), "X axis is not strictly ascending"
    return [(when.date().isoformat(), below, above) for when, below, above in rows]


@pytest.fixture()
def conn():
    import psycopg2

    # Same leak as tests/test_power_best_sql.py: in a whole-suite run this file can
    # inherit another module's mocked psycopg2. A mock has no __file__.
    if not hasattr(psycopg2, "__file__"):
        pytest.skip(
            "psycopg2 is mocked by another module in this pytest session; run "
            "this file on its own: python -m pytest tests/test_load_panels_sql.py"
        )

    import db as velomate_db

    connection = psycopg2.connect(TEST_DB)
    connection.autocommit = True
    reset_schema(connection)
    velomate_db.create_schema(connection)
    with connection.cursor() as cur:
        cur.execute("SET TIME ZONE 'UTC'")
    yield connection
    connection.close()


# --- Load Ratio ---

def test_ratio_is_atl_over_ctl_on_the_latest_day(conn):
    _seed_stats(conn, (1, 10.0, 30.0), (0, 10.0, 12.0))

    assert _ratio_rows(conn) == [(Decimal("1.2"),)]


@pytest.mark.parametrize("ctl, atl", [
    pytest.param(0.0, 3.0, id="ctl-zero"),
    pytest.param(None, 3.0, id="ctl-null"),
    pytest.param(5.0, None, id="atl-null"),
])
def test_unusable_latest_day_emits_no_row(conn, ctl, atl):
    """Yesterday has a good ratio, and it must not stand in for today's. A filter
    outside the LIMIT would quietly report it as the current value."""
    _seed_stats(conn, (1, 10.0, 12.0), (0, ctl, atl))

    assert _ratio_rows(conn) == []


def test_no_stats_at_all_emits_no_row(conn):
    assert _ratio_rows(conn) == []


def test_zero_atl_is_a_real_ratio(conn):
    """A long break takes ATL to 0 while CTL lingers. That is a very low ratio,
    not missing data."""
    _seed_stats(conn, (0, 5.0, 0.0))

    assert _ratio_rows(conn) == [(Decimal("0.0"),)]


@pytest.mark.parametrize("atl, shown, colour", [
    pytest.param(7.9, "0.8", "#2ecc71", id="0.79-rounds-into-optimal"),
    pytest.param(7.4, "0.7", "#3498db", id="0.74-stays-low"),
    pytest.param(14.4, "1.4", "#2ecc71", id="1.44-stays-optimal"),
    pytest.param(14.5, "1.5", "#f1c40f", id="1.45-rounds-into-high"),
    pytest.param(19.4, "1.9", "#f1c40f", id="1.94-stays-high"),
    pytest.param(19.5, "2.0", "#e74c3c", id="1.95-rounds-into-very-high"),
])
def test_bands_apply_to_the_rounded_value(conn, atl, shown, colour):
    """Garmin's table jumps from 1.4 to 1.5. Rounding to one decimal closes that
    gap, so the number on the card and its colour can't disagree with the tooltip."""
    _seed_stats(conn, (0, 10.0, atl))

    assert _ratio_rows(conn) == [(Decimal(shown),)]
    assert _band(Decimal(shown)) == colour


# --- Weekly Work Below / Above FTP ---

def test_below_is_total_work_minus_work_above_ftp(conn):
    _seed_ride(conn, "2026-03-04 10:00+00", 1000.0, 120.0)

    assert _weeks(conn, WEEK_1, "2026-03-08") == [
        (WEEK_1, Decimal("880.0"), Decimal("120.0")),
    ]


def test_rides_in_a_week_add_up_to_total_work(conn):
    _seed_ride(conn, "2026-03-03 10:00+00", 1000.0, 120.0)
    _seed_ride(conn, "2026-03-05 10:00+00", 500.5, 30.2)

    [(_, below, above)] = _weeks(conn, WEEK_1, "2026-03-08")

    assert (below, above) == (Decimal("1350.3"), Decimal("150.2"))
    assert below + above == Decimal("1500.5")


def test_sunday_night_and_monday_morning_are_different_weeks(conn):
    _seed_ride(conn, "2026-03-08 23:30+00", 100.0, 10.0)
    _seed_ride(conn, "2026-03-09 00:30+00", 200.0, 20.0)

    assert _weeks(conn, WEEK_1, "2026-03-15") == [
        (WEEK_1, Decimal("90.0"), Decimal("10.0")),
        (WEEK_2, Decimal("180.0"), Decimal("20.0")),
    ]


def test_week_with_no_rides_is_zero_not_null(conn):
    _seed_ride(conn, "2026-03-03 10:00+00", 100.0, 10.0)
    _seed_ride(conn, "2026-03-17 10:00+00", 200.0, 20.0)

    assert _weeks(conn, WEEK_1, "2026-03-22") == [
        (WEEK_1, Decimal("90.0"), Decimal("10.0")),
        (WEEK_2, Decimal("0"), Decimal("0")),
        (WEEK_3, Decimal("180.0"), Decimal("20.0")),
    ]


def test_window_with_no_rides_is_all_zeros(conn):
    _seed_ride(conn, "2026-01-07 10:00+00", 100.0, 10.0)

    assert _weeks(conn, WEEK_1, "2026-03-15") == [
        (WEEK_1, Decimal("0"), Decimal("0")),
        (WEEK_2, Decimal("0"), Decimal("0")),
    ]


def test_ride_without_power_counts_in_neither_series(conn):
    _seed_ride(conn, "2026-03-03 10:00+00", 100.0, 10.0)
    _seed_ride(conn, "2026-03-04 10:00+00", None, None)

    assert _weeks(conn, WEEK_1, "2026-03-08") == [
        (WEEK_1, Decimal("90.0"), Decimal("10.0")),
    ]


@pytest.mark.parametrize("work_kj, kj_above_ftp", [
    pytest.param(500.0, None, id="work-but-no-threshold"),
    pytest.param(None, 50.0, id="above-but-no-work"),
])
def test_half_stored_ride_counts_in_neither_series(conn, work_kj, kj_above_ftp):
    """Counting half a ride makes the stack stop adding up to total work. Work with
    no threshold is not all below it: nobody knows."""
    _seed_ride(conn, "2026-03-03 10:00+00", 100.0, 10.0)
    _seed_ride(conn, "2026-03-04 10:00+00", work_kj, kj_above_ftp)

    assert _weeks(conn, WEEK_1, "2026-03-08") == [
        (WEEK_1, Decimal("90.0"), Decimal("10.0")),
    ]


def test_first_week_is_whole_when_the_window_starts_midweek(conn):
    """A window opening on Thursday must not show that week's Monday ride missing:
    the bar would look like a light week."""
    _seed_ride(conn, "2026-03-02 10:00+00", 100.0, 10.0)
    _seed_ride(conn, "2026-03-06 10:00+00", 200.0, 20.0)

    assert _weeks(conn, "2026-03-05", "2026-03-08") == [
        (WEEK_1, Decimal("270.0"), Decimal("30.0")),
    ]


def test_ride_after_the_last_week_is_not_counted(conn):
    _seed_ride(conn, "2026-03-03 10:00+00", 100.0, 10.0)
    _seed_ride(conn, "2026-03-10 10:00+00", 200.0, 20.0)

    assert _weeks(conn, WEEK_1, "2026-03-08") == [
        (WEEK_1, Decimal("90.0"), Decimal("10.0")),
    ]


@pytest.mark.parametrize("sport, below", [
    pytest.param("all", "270.0", id="all"),
    pytest.param("zwift", "180.0", id="one-sport"),
])
def test_sport_filter(conn, sport, below):
    _seed_ride(conn, "2026-03-03 10:00+00", 100.0, 10.0, sport="cycling_outdoor")
    _seed_ride(conn, "2026-03-04 10:00+00", 200.0, 20.0, sport="zwift")

    assert _weeks(conn, WEEK_1, "2026-03-08", sport)[0][1] == Decimal(below)
