"""The real startup path on a real PostgreSQL, deciding whether to reset rides.

A threshold that changes makes startup wipe derived metrics on every stored ride,
and this runs unattended on each deploy. So: an unconfigured threshold must leave
everything alone, and a value that cannot be read must not count as a change.

    VELOMATE_TEST_DATABASE_URL=postgresql://... python3 -m pytest \
        tests/test_thresholds_sql.py
"""
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from pg_reset import reset_schema

TEST_DB = os.environ.get("VELOMATE_TEST_DATABASE_URL")

pytestmark = pytest.mark.skipif(
    not TEST_DB,
    reason="needs a throwaway PostgreSQL in VELOMATE_TEST_DATABASE_URL",
)

_INGESTOR = str(Path(__file__).resolve().parent.parent / "ingestor")

SETTLED = {"configured_ftp": "0", "configured_max_hr": "185",
           "configured_resting_hr": "50", "configured_weight": "0",
           "configured_backfill_months": "12"}
SETTLED_ENV = {"VELOMATE_MAX_HR": "185", "VELOMATE_RESTING_HR": "50"}


@pytest.fixture(autouse=True)
def _no_ambient_config(monkeypatch):
    for name in ("VELOMATE_FTP", "VELOMATE_WEIGHT", "VELOMATE_MAX_HR",
                 "VELOMATE_RESTING_HR", "VELOMATE_BACKFILL_MONTHS",
                 "VELOMATE_RESET_RIDE_FTP", "STRAVA_CLIENT_ID", "RWGPS_API_KEY"):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture()
def conn():
    import psycopg2

    if not hasattr(psycopg2, "__file__"):
        pytest.skip("needs the real psycopg2, which is not installed here")

    if _INGESTOR not in sys.path:
        sys.path.insert(0, _INGESTOR)
    import db as velomate_db

    connection = psycopg2.connect(TEST_DB)
    connection.autocommit = True
    reset_schema(connection)
    velomate_db.create_schema(connection)
    yield connection
    connection.close()


@pytest.fixture()
def settled(conn):
    """A deployment that has started before: a computed ride, and stored thresholds."""
    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO activities (name, date, tss, intensity_factor, trimp, ride_ftp)
            VALUES ('Synthetic', '2026-10-01', 80, 0.9, 100, 250)
        """)
        cur.execute("INSERT INTO athlete_stats (date, ctl) VALUES ('2026-10-01', 40)")
    _store(conn, SETTLED)
    return conn


def _store(conn, values):
    with conn.cursor() as cur:
        for key, value in values.items():
            cur.execute("""
                INSERT INTO sync_state (key, value) VALUES (%s, %s)
                ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value
            """, (key, value))


def _rides(conn):
    with conn.cursor() as cur:
        cur.execute("""
            SELECT tss, intensity_factor, trimp, ride_ftp FROM activities ORDER BY id
        """)
        rides = cur.fetchall()
        cur.execute("SELECT count(*) FROM athlete_stats")
        return rides, cur.fetchone()[0]


def _stored(conn, key):
    with conn.cursor() as cur:
        cur.execute("SELECT value FROM sync_state WHERE key = %s", (key,))
        return cur.fetchone()[0]


UNTOUCHED = ([(80, 0.9, 100, 250)], 1)


@pytest.fixture()
def start(monkeypatch):
    """The real run() up to its polling loop, so no ride is fetched or recomputed."""
    sys.modules.setdefault("schedule", MagicMock())
    import main as ingestor_main

    monkeypatch.setenv("DATABASE_URL", TEST_DB)
    monkeypatch.setenv("INTERVALS_ICU_ATHLETE_ID", "i1")
    monkeypatch.setenv("INTERVALS_ICU_API_KEY", "k")

    def run():
        with patch.object(ingestor_main, "poll_intervals_icu"), \
                patch.object(ingestor_main, "recalculate_fitness"), \
                patch.object(ingestor_main.schedule, "every"), \
                patch.object(ingestor_main.schedule, "run_pending",
                             side_effect=KeyboardInterrupt):
            with pytest.raises(KeyboardInterrupt):
                ingestor_main.run()

    return run


class TestUnconfiguredFtp:
    """Must behave the same before and after the parser, on a deployment that has
    no FTP set, as it arrives from the environment in each form."""

    @pytest.mark.parametrize("raw", [None, "", "0"],
                             ids=["absent", "empty", "compose default"])
    def test_a_restart_changes_nothing(self, settled, start, monkeypatch, raw):
        for name, value in SETTLED_ENV.items():
            monkeypatch.setenv(name, value)
        if raw is not None:
            monkeypatch.setenv("VELOMATE_FTP", raw)

        start()

        assert _rides(settled) == UNTOUCHED
        assert _stored(settled, "configured_ftp") == "0"
        assert _stored(settled, "configured_max_hr") == "185"
        assert _stored(settled, "configured_resting_hr") == "50"


class TestADecimalFtp:
    def test_takes_effect_rounded(self, settled, start, monkeypatch):
        for name, value in SETTLED_ENV.items():
            monkeypatch.setenv(name, value)
        monkeypatch.setenv("VELOMATE_FTP", "262.5")

        start()

        assert _stored(settled, "configured_ftp") == "263"
        assert _rides(settled) == ([(None, None, None, None)], 0)

    def test_reads_as_unchanged_on_the_next_restart(self, settled, start, monkeypatch):
        """Compared as written, 262.5 against 263 would reset rides on every start."""
        for name, value in SETTLED_ENV.items():
            monkeypatch.setenv(name, value)
        _store(settled, {"configured_ftp": "263"})
        monkeypatch.setenv("VELOMATE_FTP", "262.5")

        start()

        assert _rides(settled) == UNTOUCHED


class TestEachThresholdHasItsOwnStoredValue:
    """Distinct values, so reading the wrong key cannot go unnoticed."""

    STORED = {"configured_ftp": "251", "configured_max_hr": "186",
              "configured_resting_hr": "47"}
    GOOD_ENV = {"VELOMATE_FTP": "251", "VELOMATE_MAX_HR": "186",
                "VELOMATE_RESTING_HR": "47"}

    @pytest.mark.parametrize("name", sorted(GOOD_ENV))
    def test_startup_keeps_it_when_the_value_is_bad(self, settled, start, monkeypatch, name):
        _store(settled, self.STORED)
        for variable, value in self.GOOD_ENV.items():
            monkeypatch.setenv(variable, value)
        monkeypatch.setenv(name, "not a number")

        start()

        assert _rides(settled) == UNTOUCHED
        for key, value in self.STORED.items():
            assert _stored(settled, key) == value

    def test_recalculation_reads_each_from_its_own_variable(
            self, settled, monkeypatch, capsys):
        import fitness

        for variable, value in self.GOOD_ENV.items():
            monkeypatch.setenv(variable, value)

        fitness.recalculate_fitness(settled)

        out = capsys.readouterr().out
        assert "Using configured FTP: 251W" in out
        assert "Using configured max HR: 186" in out
        assert "Resting HR: 47 (configured)" in out

    def test_recalculation_keeps_each_when_every_value_is_bad(
            self, settled, monkeypatch, capsys):
        import fitness

        _store(settled, self.STORED)
        for variable in self.GOOD_ENV:
            monkeypatch.setenv(variable, "not a number")

        fitness.recalculate_fitness(settled)

        out = capsys.readouterr().out
        assert "Using configured FTP: 251W" in out
        assert "Using configured max HR: 186" in out
        assert "Resting HR: 47 (configured)" in out


class TestABadFtp:
    @pytest.fixture()
    def configured(self, settled, monkeypatch):
        for name, value in SETTLED_ENV.items():
            monkeypatch.setenv(name, value)
        _store(settled, {"configured_ftp": "250"})
        monkeypatch.setenv("VELOMATE_FTP", "262W")
        return settled

    def test_resets_nothing_and_keeps_the_last_good_value(self, configured, start):
        start()

        assert _rides(configured) == UNTOUCHED
        assert _stored(configured, "configured_ftp") == "250"

    def test_does_not_lose_a_good_value_beside_it(self, configured, start, monkeypatch):
        """Resting HR changes TRIMP, so that reset is correct. ride_ftp belongs to
        the FTP that did not change, so it survives."""
        monkeypatch.setenv("VELOMATE_RESTING_HR", "48")

        start()

        assert _stored(configured, "configured_resting_hr") == "48"
        assert _stored(configured, "configured_ftp") == "250"
        assert _rides(configured) == ([(None, None, None, 250)], 0)

    def test_recalculation_uses_the_value_startup_kept(self, configured, start):
        """The split: startup kept one FTP while recalculation used another."""
        import fitness

        with configured.cursor() as cur:
            cur.execute("""
                INSERT INTO activities (name, date, duration_s, avg_power, np)
                VALUES ('New', '2026-10-02', 3600, 200, 200) RETURNING id
            """)
            new_ride = cur.fetchone()[0]

        start()
        fitness.recalculate_fitness(configured)

        with configured.cursor() as cur:
            cur.execute("SELECT ride_ftp FROM activities WHERE id = %s", (new_ride,))
            assert cur.fetchone()[0] == 250
