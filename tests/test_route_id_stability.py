"""Route ids must survive a recalculation.

Step 2.7 rebuilds the route table from scratch on every recalculation. When the
rebuild also re-allocated ids from the serial, every route was renumbered on
every new ride — which silently broke any saved `var-route=<id>` URL on the
Route Comparison dashboard: the panels filter `WHERE route_id = <id>`, so a
stale id matched nothing and all five panels rendered empty.

These tests need a real PostgreSQL because the behaviour under test *is* the
SQL: the snapshot has to be taken before the DELETE, and the INSERT has to
carry the preserved id. A mocked cursor would only re-assert the code I wrote.

Run with:
    VELOMATE_TEST_DATABASE_URL=postgresql://... python3 -m pytest \
        tests/test_route_id_stability.py -q
"""

import os

import pytest

TEST_DB = os.environ.get("VELOMATE_TEST_DATABASE_URL")

pytestmark = pytest.mark.skipif(
    not TEST_DB,
    reason="needs a throwaway PostgreSQL in VELOMATE_TEST_DATABASE_URL",
)

# 0.001 degrees is exactly CELL_DEGREES, so consecutive points land in
# consecutive cells and 100 points yield ~100 distinct ones. MIN_GPS_POINTS is
# 60, so this clears the sparsity gate with room to spare.
_POINTS = 100


def _track(lat0, lng0):
    """A straight synthetic track of _POINTS fixes."""
    return [(lat0 + i * 0.001, lng0) for i in range(_POINTS)]


def _seed(conn, rides):
    """Insert rides as (name, date, track) and their GPS streams."""
    with conn.cursor() as cur:
        for name, date, track in rides:
            cur.execute(
                """INSERT INTO activities (name, date, distance_m, duration_s,
                                           elevation_m, sport_type)
                   VALUES (%s, %s, %s, %s, %s, 'cycling_outdoor')
                   RETURNING id""",
                (name, date, 20000.0, 3600, 100.0),
            )
            act_id = cur.fetchone()[0]
            for offset, (lat, lng) in enumerate(track):
                cur.execute(
                    """INSERT INTO activity_streams
                           (activity_id, time_offset, lat, lng, altitude_m)
                       VALUES (%s, %s, %s, %s, %s)""",
                    (act_id, offset, lat, lng, 50.0),
                )


def _route_map(conn):
    """activity name -> route id, plus the full route id set."""
    with conn.cursor() as cur:
        cur.execute("""SELECT a.name, a.route_id FROM activities a
                       ORDER BY a.date""")
        by_name = {name: route_id for name, route_id in cur.fetchall()}
        cur.execute("SELECT id, label, ride_count FROM ride_routes ORDER BY id")
        routes = cur.fetchall()
    return by_name, routes


@pytest.fixture()
def conn():
    import psycopg2

    # Several test modules install a MagicMock for psycopg2 via
    # sys.modules.setdefault, and pytest imports every module into one process,
    # so in a whole-suite run this file can inherit the mock — the same leak
    # conftest.py guards against for `requests`. Those modules assert against
    # the mock, so it cannot simply be pre-empted here; a real connection needs
    # its own session instead. A mocked module has no __file__.
    if not hasattr(psycopg2, "__file__"):
        pytest.skip(
            "psycopg2 is mocked by another module in this pytest session; run "
            "this file on its own: python -m pytest tests/test_route_id_stability.py"
        )

    import db as velomate_db

    connection = psycopg2.connect(TEST_DB)
    # Production hands recalculate_fitness an autocommit connection
    # (db.get_connection, db.py:14) and the function toggles autocommit itself
    # partway through, which psycopg2 refuses inside an open transaction. A
    # fixture that left one open would fail on plumbing rather than on routes.
    connection.autocommit = True
    with connection.cursor() as cur:
        # A clean slate per test: the whole point is comparing ids across runs,
        # so leftovers from a previous test would poison the comparison.
        cur.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
    velomate_db.create_schema(connection)
    yield connection
    connection.close()


def test_route_ids_survive_a_recalculation(conn):
    """The regression: a second recalculation must not renumber routes.

    Two rides share a track (one repeated route) and a third is elsewhere, so
    the library has exactly the shape the dashboard's `ride_count > 1` variable
    query selects on.
    """
    import fitness

    _seed(conn, [
        ("Loop first",  "2026-04-26 10:00:00+00", _track(38.700, -9.140)),
        ("Loop second", "2026-05-03 10:00:00+00", _track(38.700, -9.140)),
        ("Elsewhere",   "2026-05-10 10:00:00+00", _track(39.500, -8.000)),
    ])

    fitness.recalculate_fitness(conn)
    before_by_name, before_routes = _route_map(conn)

    assert any(r[2] > 1 for r in before_routes), (
        "fixture is wrong: no repeated route, so the dashboard variable would "
        "have nothing to select"
    )

    # A new ride is what triggers a recalculation in production, so recalculate
    # again exactly the way ingesting one does.
    fitness.recalculate_fitness(conn)
    after_by_name, after_routes = _route_map(conn)

    assert after_by_name == before_by_name, (
        "route ids were renumbered by a recalculation — a saved "
        "var-route=<id> URL on Route Comparison now matches no rides"
    )
    assert after_routes == before_routes


def test_a_new_ride_does_not_renumber_existing_routes(conn):
    """The production sequence: ingest a ride, recalculate, ids hold."""
    import fitness

    _seed(conn, [
        ("Loop first",  "2026-04-26 10:00:00+00", _track(38.700, -9.140)),
        ("Loop second", "2026-05-03 10:00:00+00", _track(38.700, -9.140)),
    ])
    fitness.recalculate_fitness(conn)
    before_by_name, _ = _route_map(conn)

    _seed(conn, [
        ("Friday ride", "2026-09-04 10:00:00+00", _track(38.700, -9.140)),
    ])
    fitness.recalculate_fitness(conn)
    after_by_name, _ = _route_map(conn)

    for name, route_id in before_by_name.items():
        assert after_by_name[name] == route_id, (
            f"{name} moved from route {route_id} to {after_by_name[name]} "
            f"because a later ride was added"
        )
    # And the new ride joined that same route rather than founding its own.
    assert after_by_name["Friday ride"] == before_by_name["Loop first"]


def test_upgrade_preserves_ids_from_the_existing_assignment(conn):
    """The first run after this fix ships must not renumber either.

    Every existing install has routes but no recorded founder, so the rebuild
    falls back to deriving one from the current assignment. Without that
    fallback the upgrade itself would renumber once and break exactly the saved
    URLs it exists to protect.
    """
    import fitness

    _seed(conn, [
        ("Loop first",  "2026-04-26 10:00:00+00", _track(38.700, -9.140)),
        ("Loop second", "2026-05-03 10:00:00+00", _track(38.700, -9.140)),
        ("Elsewhere",   "2026-05-10 10:00:00+00", _track(39.500, -8.000)),
    ])
    fitness.recalculate_fitness(conn)
    before_by_name, before_routes = _route_map(conn)

    # A pre-upgrade database exactly: routes assigned, founder unrecorded.
    with conn.cursor() as cur:
        cur.execute("UPDATE ride_routes SET founding_activity_id = NULL")

    fitness.recalculate_fitness(conn)
    after_by_name, after_routes = _route_map(conn)

    assert after_by_name == before_by_name, (
        "the upgrade run renumbered routes derived from an existing assignment"
    )
    assert after_routes == before_routes


def test_a_lagging_sequence_does_not_collide_with_a_preserved_id(conn):
    """A restored database can leave ride_routes_id_seq behind the live ids.

    The rebuild reclaims preserved ids with explicit-id INSERTs, so a route
    founded during the same loop draws a DEFAULT id from a sequence that may
    still be pointing below them — landing on an id another founder is about to
    claim. The primary-key collision aborts the whole recalculation, and keeps
    aborting on every run until someone fixes the sequence by hand.
    """
    import fitness

    _seed(conn, [
        ("Loop first",  "2026-04-26 10:00:00+00", _track(38.700, -9.140)),
        ("Loop second", "2026-05-03 10:00:00+00", _track(38.700, -9.140)),
        ("Elsewhere",   "2026-05-10 10:00:00+00", _track(39.500, -8.000)),
    ])
    fitness.recalculate_fitness(conn)
    before_by_name, _ = _route_map(conn)

    # A restore that reloaded the rows but not the sequence.
    with conn.cursor() as cur:
        cur.execute("SELECT setval('ride_routes_id_seq', 1, false)")

    # A ride founding a genuinely new route is what needs a fresh id, and so
    # what collides.
    _seed(conn, [
        ("New place", "2026-09-05 10:00:00+00", _track(41.000, -7.000)),
    ])

    fitness.recalculate_fitness(conn)   # must not raise
    after_by_name, _ = _route_map(conn)

    for name, route_id in before_by_name.items():
        assert after_by_name[name] == route_id, (
            f"{name} moved from route {route_id} to {after_by_name[name]}"
        )
    assert after_by_name["New place"] not in before_by_name.values(), (
        "the new route reused a live id instead of taking a fresh one"
    )


def test_founding_activity_id_is_recorded(conn):
    """The preserved id is keyed on the founding ride, so it must be stored."""
    import fitness

    _seed(conn, [
        ("Loop first",  "2026-04-26 10:00:00+00", _track(38.700, -9.140)),
        ("Loop second", "2026-05-03 10:00:00+00", _track(38.700, -9.140)),
    ])
    fitness.recalculate_fitness(conn)

    with conn.cursor() as cur:
        cur.execute("""SELECT r.founding_activity_id, a.name
                       FROM ride_routes r
                       JOIN activities a ON a.id = r.founding_activity_id""")
        founders = dict(cur.fetchall())

    assert founders, "no route recorded a founding activity"
    # Oldest ride first, so the earlier of the two founds the shared route.
    assert "Loop first" in founders.values()
    assert "Loop second" not in founders.values()
