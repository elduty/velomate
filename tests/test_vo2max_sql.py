"""Est. VO2max comes from the best 5-minute power and that ride's weight.

Sitko et al. (2022), Int J Sports Physiol Perform 17(1):9-15:
    VO2max = 16.6 + 8.87 x 5-min relative power (W/kg)

These tests execute the panels' own SQL, read from the dashboard JSON, because the
behaviour under test *is* the SQL: which ride wins, whose weight divides it, and
that a period with nothing to report emits no row rather than a NULL one.
tests/test_dashboards.py carries the static half, which runs without a database.

Run with:
    VELOMATE_TEST_DATABASE_URL=postgresql://... python3 -m pytest \
        tests/test_vo2max_sql.py -q
"""

import json
import os
from datetime import date, timedelta
from decimal import Decimal

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
ALL_TIME = "2000-01-01T00:00:00Z"


def vo2(watts, weight=81.0):
    """The equation by hand, rounded the way the panels round it."""
    return Decimal(str(16.6 + 8.87 * watts / weight)).quantize(Decimal("0.1"))


def _panel_sql(filename, panel_id, time_from=ALL_TIME):
    """The panel's SQL as Grafana sends it, with its macro expanded."""
    with open(os.path.join(DASHBOARD_DIR, filename)) as f:
        panel = next(p for p in json.load(f)["panels"] if p["id"] == panel_id)
    return expand(
        panel["targets"][0]["rawSql"],
        time_from=time_from,
        time_to="2100-01-01T00:00:00Z",
    )


def _seed_ride(conn, powers, days_ago=1, weight=81.0):
    """Insert a ride with a 1 Hz stream, days_ago before the database's today."""
    with conn.cursor() as cur:
        cur.execute(
            """INSERT INTO activities (name, date, distance_m, duration_s,
                                       elevation_m, sport_type, ride_weight)
               VALUES ('Synthetic', CURRENT_DATE - %s::int + TIME '12:00', 20000.0,
                       %s, 100.0, 'cycling_outdoor', %s)
               RETURNING id""",
            (days_ago, len(powers), weight),
        )
        ride_id = cur.fetchone()[0]
        cur.execute(
            """INSERT INTO activity_streams (activity_id, time_offset, power)
               SELECT %s, ord - 1, p
               FROM unnest(%s::int[]) WITH ORDINALITY AS t(p, ord)""",
            (ride_id, powers),
        )
    return ride_id


def _card(conn):
    """The Est. VO2max stat's raw rows. [] is the panel's noValue; [(None,)] would
    be a NULL value row, which is what must never happen."""
    with conn.cursor() as cur:
        cur.execute(_panel_sql("overview.json", 1701))
        return cur.fetchall()


def _series(conn, time_from=ALL_TIME):
    """VO2max Progression as {days before today: value}."""
    with conn.cursor() as cur:
        cur.execute(_panel_sql("all-time-progression.json", 1700, time_from))
        rows = cur.fetchall()
        cur.execute("SELECT CURRENT_DATE")
        today = cur.fetchone()[0]
    days = [(today - when.date()).days for when, _ in rows]
    assert len(set(days)) == len(days), "a day appears twice on the X axis"
    assert days == sorted(days, reverse=True), "X axis is not ascending"
    return dict(zip(days, (value for _, value in rows)))


@pytest.fixture()
def conn():
    import psycopg2

    # Same leak as tests/test_power_best_sql.py: in a whole-suite run this file can
    # inherit another module's mocked psycopg2. A mock has no __file__.
    if not hasattr(psycopg2, "__file__"):
        pytest.skip(
            "psycopg2 is mocked by another module in this pytest session; run "
            "this file on its own: python -m pytest tests/test_vo2max_sql.py"
        )

    import db as velomate_db

    connection = psycopg2.connect(TEST_DB)
    connection.autocommit = True
    reset_schema(connection)
    velomate_db.create_schema(connection)
    yield connection
    connection.close()


def test_equation_is_sitko_on_watts_per_kilo(conn):
    """3.0 W/kg is 43.2 by Sitko. The ACSM equation it replaces gives 39.4."""
    _seed_ride(conn, [243] * 300, weight=81.0)

    assert _card(conn) == [(Decimal("43.2"),)]


def test_critical_power_is_not_an_input(conn):
    """A strong CP fit and a weight on a ride with no stream: nothing to report."""
    _seed_ride(conn, [], weight=81.0)
    with conn.cursor() as cur:
        cur.execute(
            """INSERT INTO cp_estimates (date, cp_watts, w_prime_kj, r_squared, source)
               VALUES (CURRENT_DATE, 300, 20, 0.99, 'cp')"""
        )

    assert _card(conn) == []
    assert _series(conn) == {}


def test_short_ride_emits_no_row(conn):
    """299 samples hold no full window. Its opening partial windows must not
    count, and the query must end with no row at all, not a NULL one."""
    _seed_ride(conn, [500] * 299)

    assert _card(conn) == []
    assert _series(conn) == {}


@pytest.mark.parametrize("samples", [300, 600])
def test_full_window_beats_a_hard_opening(conn, samples):
    """Ten hard seconds then steady: the ride's first partial windows average
    500 W, the best full window averages 210 W. 300 is the guard's boundary."""
    _seed_ride(conn, ([500] * 10 + [200] * samples)[:samples])

    assert _card(conn) == [(vo2(210),)]


def test_a_rides_score_is_its_best_window_not_its_average(conn):
    """Exactly 300 samples at 300 W inside an easy ride: a ride's mean or a 301-row
    window would both read lower, and on both panels."""
    _seed_ride(conn, [100] * 300 + [300] * 300 + [100] * 300, days_ago=0)

    assert _card(conn) == [(vo2(300),)]
    assert _series(conn) == {0: vo2(300)}


def test_ride_without_power_emits_no_row(conn):
    ride = _seed_ride(conn, [])
    with conn.cursor() as cur:
        cur.execute(
            """INSERT INTO activity_streams (activity_id, time_offset, hr)
               SELECT %s, n, 140 FROM generate_series(0, 599) AS n""",
            (ride,),
        )

    assert _card(conn) == []
    assert _series(conn) == {}


@pytest.mark.parametrize("weight", [None, 0])
def test_ride_without_weight_emits_no_row(conn, weight):
    """Unweighed, the only ride cannot be divided: no row, not a NULL, not a crash."""
    _seed_ride(conn, [250] * 300, weight=weight)

    assert _card(conn) == []
    assert _series(conn) == {}


def test_unweighed_best_gives_way_to_the_next_ride(conn):
    """The best ride has no weight. It must drop out of the ranking, rather than
    win it and take the panel down to nothing."""
    _seed_ride(conn, [400] * 300, days_ago=2, weight=None)
    _seed_ride(conn, [200] * 300, days_ago=1, weight=81.0)

    assert _card(conn) == [(vo2(200),)]


def test_card_divides_by_the_best_rides_own_weight(conn):
    """The ride's weight, not today's: 75 kg gives 52.1, the latest 60 kg gives 61.0."""
    _seed_ride(conn, [300] * 300, days_ago=20, weight=75.0)
    _seed_ride(conn, [200] * 300, days_ago=1, weight=60.0)

    assert _card(conn) == [(vo2(300, 75.0),)]


def test_card_ranks_by_power_not_by_watts_per_kilo(conn):
    """The best 5-minute power wins. Here that is the heavier, lower W/kg ride."""
    _seed_ride(conn, [300] * 300, days_ago=2, weight=100.0)
    _seed_ride(conn, [280] * 300, days_ago=1, weight=70.0)

    assert _card(conn) == [(vo2(300, 100.0),)]


@pytest.mark.parametrize("days_ago, counted", [(90, True), (91, False)])
def test_card_looks_back_ninety_days(conn, days_ago, counted):
    _seed_ride(conn, [300] * 300, days_ago=days_ago)
    _seed_ride(conn, [200] * 300, days_ago=1)

    assert _card(conn) == [(vo2(300 if counted else 200),)]


def test_series_has_one_point_a_day_through_today(conn):
    _seed_ride(conn, [243] * 300, days_ago=3)

    assert _series(conn) == {3: vo2(243), 2: vo2(243), 1: vo2(243), 0: vo2(243)}


def test_series_rolls_each_best_off_after_ninety_days(conn):
    """300 W counts for 90 days after it, then the 200 W ride does, then there is
    nothing in the window and the line stops rather than emitting a NULL."""
    _seed_ride(conn, [300] * 300, days_ago=120)
    _seed_ride(conn, [200] * 300, days_ago=100)

    expected = {days: vo2(300) for days in range(120, 29, -1)}
    expected.update({days: vo2(200) for days in range(29, 9, -1)})
    assert _series(conn) == expected


def test_series_divides_by_each_best_rides_own_weight(conn):
    """A later, heavier ride takes over from its own date. It does not rewrite the
    days before it, which is what dividing history by today's weight did."""
    _seed_ride(conn, [300] * 300, days_ago=20, weight=75.0)
    _seed_ride(conn, [320] * 300, days_ago=10, weight=100.0)

    series = _series(conn)

    assert series[11] == vo2(300, 75.0)
    assert series[10] == vo2(320, 100.0)


def test_series_keeps_a_best_from_before_the_time_range(conn):
    """Zooming the dashboard must not change a day's value: the time filter narrows
    what is shown, not the 90 days each point looks back over."""
    _seed_ride(conn, [300] * 300, days_ago=40)
    _seed_ride(conn, [200] * 300, days_ago=5)
    zoomed = (date.today() - timedelta(days=10)).isoformat() + "T00:00:00Z"

    assert set(_series(conn, zoomed).values()) == {vo2(300)}


def test_series_gives_two_rides_on_one_day_one_point(conn):
    _seed_ride(conn, [300] * 300, days_ago=1)
    _seed_ride(conn, [200] * 300, days_ago=1)

    assert _series(conn) == {1: vo2(300), 0: vo2(300)}


def test_trend_ends_on_the_stat_cards_value(conn):
    """Both look back from today, so a reader never sees the two disagree, even
    when today's window has lost the best ride the line had yesterday."""
    _seed_ride(conn, [300] * 300, days_ago=91)
    _seed_ride(conn, [200] * 300, days_ago=30)

    assert _series(conn)[0] == _card(conn)[0][0] == vo2(200)
