"""Power zones must be binned at each ride's own FTP.

IF, detected intervals and kJ above FTP all use `activities.ride_ftp`, but the
zone panels binned at today's FTP. The two disagreed on the same page, and the
monthly chart re-zoned every past month whenever FTP moved.

These tests execute the panels' own SQL, read from the dashboard JSON, because
the behaviour under test is which FTP each sample is compared against.
tests/test_dashboards.py carries the static half, which runs without a database.

Run with:
    VELOMATE_TEST_DATABASE_URL=postgresql://... python3 -m pytest \
        tests/test_power_zones_sql.py -q
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

DASHBOARD_DIR = os.path.join(
    os.path.dirname(__file__), "..", "grafana", "dashboards", "metric"
)

Z1, Z2, Z3, Z4, Z5, Z6, Z7 = ZONES = (
    "Z1 Recovery", "Z2 Endurance", "Z3 Tempo", "Z4 Threshold",
    "Z5 VO2max", "Z6 Anaerobic", "Z7 Neuromuscular",
)

RIDE_PANELS = [
    ("activity.json", 32),
    ("activity.json", 41),
    ("activity.json", 403),
]
MONTHLY_PANEL = ("all-time-progression.json", 30)
ALL_PANELS = RIDE_PANELS + [MONTHLY_PANEL]

# 190 W lands in a different zone at each FTP used below, so a test can tell
# which FTP a sample was compared against.
POWER = 190
ZONE_OF_190W = {150: Z6, 200: Z4, 250: Z3, 300: Z2, 400: Z1}


def _panel_sql(filename, panel_id, activity_id):
    """The target's SQL as Grafana sends it: variables expanded."""
    with open(os.path.join(DASHBOARD_DIR, filename)) as f:
        panels = json.load(f)["panels"]
    panels += [child for p in panels for child in p.get("panels", [])]
    panel = next(p for p in panels if p["id"] == panel_id)
    sql = panel["targets"][0]["rawSql"]
    return expand(sql, {"sport_type": "all", "activity_id": activity_id})


def _seed_ride(conn, ride_ftp, powers=(POWER,) * 60, date="2026-04-26 10:00:00+00"):
    """Insert a ride with a 1 Hz power stream and return its id."""
    with conn.cursor() as cur:
        cur.execute(
            """INSERT INTO activities (name, date, distance_m, duration_s,
                                       elevation_m, sport_type, ride_ftp)
               VALUES ('Synthetic', %s, 20000.0, %s, 100.0, 'cycling_outdoor', %s)
               RETURNING id""",
            (date, len(powers), ride_ftp),
        )
        ride_id = cur.fetchone()[0]
        cur.execute(
            """INSERT INTO activity_streams (activity_id, time_offset, power, speed_kmh)
               SELECT %s, ord - 1, p, 30.0
               FROM unnest(%s::int[]) WITH ORDINALITY AS t(p, ord)""",
            (ride_id, list(powers)),
        )
    return ride_id


def _set_ftp(conn, configured=None, estimated=None):
    with conn.cursor() as cur:
        for key, value in (("configured_ftp", configured), ("estimated_ftp", estimated)):
            if value is not None:
                cur.execute(
                    "INSERT INTO sync_state (key, value) VALUES (%s, %s)",
                    (key, str(value)),
                )


def _time_in_zone(conn, filename, panel_id, activity_id=None):
    """Every zone the panel put time in, whatever its row shape: one row of
    counts, power buckets of minutes, or one row per km or month of percents."""
    with conn.cursor() as cur:
        cur.execute(_panel_sql(filename, panel_id, activity_id))
        names = [column.name for column in cur.description]
        rows = cur.fetchall()
    totals = {}
    for row in rows:
        for name, value in zip(names, row):
            if name in ZONES and value:
                totals[name] = totals.get(name, 0) + float(value)
    return totals


def _zone(conn, filename, panel_id, activity_id=None):
    """The one zone a constant-power ride landed in."""
    totals = _time_in_zone(conn, filename, panel_id, activity_id)
    assert len(totals) == 1, totals
    return next(iter(totals))


@pytest.fixture()
def conn():
    import psycopg2

    # Same leak as tests/test_route_id_stability.py: in a whole-suite run this
    # file can inherit another module's mocked psycopg2. A mock has no __file__.
    if not hasattr(psycopg2, "__file__"):
        pytest.skip(
            "psycopg2 is mocked by another module in this pytest session; run "
            "this file on its own: python -m pytest tests/test_power_zones_sql.py"
        )

    import db as velomate_db

    connection = psycopg2.connect(TEST_DB)
    connection.autocommit = True
    reset_schema(connection)
    velomate_db.create_schema(connection)
    yield connection
    connection.close()


@pytest.mark.parametrize("filename, panel_id", ALL_PANELS)
def test_stored_ftp_beats_todays(conn, filename, panel_id):
    """The bug. 190 W is Z3 at the stored 250 W, Z2 at the configured 300 W and
    Z4 at the estimated 200 W, so either wrong FTP shows up as a wrong zone."""
    _set_ftp(conn, configured=300, estimated=200)
    ride = _seed_ride(conn, ride_ftp=250)

    assert _zone(conn, filename, panel_id, ride) == ZONE_OF_190W[250]


@pytest.mark.parametrize("filename, panel_id", ALL_PANELS)
@pytest.mark.parametrize(
    "ride_ftp, configured, estimated, used_ftp",
    [
        (None, 300, 200, 300),
        (0, 300, 200, 300),
        (None, 0, 200, 200),  # the shape of a deployment with no VELOMATE_FTP
        (0, 0, 200, 200),
        (None, None, 200, 200),
        (None, None, None, 150),
        (0, None, None, 150),
    ],
)
def test_ride_with_no_stored_ftp_keeps_the_fallback_chain(
    conn, filename, panel_id, ride_ftp, configured, estimated, used_ftp
):
    """A zero FTP would put every sample in Z7, so 0 has to count as missing."""
    _set_ftp(conn, configured=configured, estimated=estimated)
    ride = _seed_ride(conn, ride_ftp=ride_ftp)

    assert _zone(conn, filename, panel_id, ride) == ZONE_OF_190W[used_ftp]


@pytest.mark.parametrize("filename, panel_id", ALL_PANELS)
@pytest.mark.parametrize(
    "power, zone",
    [
        (109, Z1), (110, Z2),
        (149, Z2), (150, Z3),
        (179, Z3), (180, Z4),
        (209, Z4), (210, Z5),
        (239, Z5), (240, Z6),
        (299, Z6), (300, Z7),
    ],
)
def test_zone_edges_stay_exact(conn, filename, panel_id, power, zone):
    """A zone owns its lower edge. ride_ftp is a FLOAT, and in float8 200 * 0.55
    is 110.00000000000001, which would drop a 110 W sample into Z1."""
    ride = _seed_ride(conn, ride_ftp=200, powers=(power,) * 60)

    assert _zone(conn, filename, panel_id, ride) == zone


@pytest.mark.parametrize("filename, panel_id", RIDE_PANELS)
def test_each_ride_uses_its_own_ftp_not_another_rides(conn, filename, panel_id):
    """The panel has to look up the selected ride, not any ride."""
    first = _seed_ride(conn, ride_ftp=200)
    second = _seed_ride(conn, ride_ftp=400)

    assert _zone(conn, filename, panel_id, first) == ZONE_OF_190W[200]
    assert _zone(conn, filename, panel_id, second) == ZONE_OF_190W[400]


def _monthly_percents(conn):
    """{month: {zone: percent}} from the monthly chart."""
    filename, panel_id = MONTHLY_PANEL
    with conn.cursor() as cur:
        cur.execute(_panel_sql(filename, panel_id, None))
        names = [column.name for column in cur.description]
        rows = cur.fetchall()
    return {
        row[0].strftime("%Y-%m"): {
            name: float(value)
            for name, value in zip(names[1:], row[1:])
            if value
        }
        for row in rows
    }


def test_monthly_history_is_not_rezoned_at_todays_ftp(conn):
    """The same power in two months, at the FTP each month actually had."""
    _set_ftp(conn, estimated=400)
    _seed_ride(conn, ride_ftp=150, date="2026-02-10 10:00:00+00")
    _seed_ride(conn, ride_ftp=250, date="2026-03-10 10:00:00+00")

    assert _monthly_percents(conn) == {
        "2026-02": {ZONE_OF_190W[150]: 100.0},
        "2026-03": {ZONE_OF_190W[250]: 100.0},
    }


def test_monthly_chart_bins_each_ride_not_each_month(conn):
    """Two rides in one month with different FTPs split that month's time."""
    _seed_ride(conn, ride_ftp=200, date="2026-03-10 10:00:00+00")
    _seed_ride(conn, ride_ftp=400, date="2026-03-20 10:00:00+00")

    assert _monthly_percents(conn) == {
        "2026-03": {ZONE_OF_190W[200]: 50.0, ZONE_OF_190W[400]: 50.0},
    }
