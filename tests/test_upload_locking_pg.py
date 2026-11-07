"""recalculate_fitness runs entirely under the ingest advisory lock, so the
upload service and the ingestor never recalculate while the other stores a ride.

Run with:
    VELOMATE_TEST_DATABASE_URL=postgresql://... python3 -m pytest \
        tests/test_upload_locking_pg.py -q
"""

import os
import sys
import threading
from pathlib import Path

import pytest

_tests_dir = Path(__file__).resolve().parent
if str(_tests_dir) not in sys.path:
    sys.path.insert(0, str(_tests_dir))

from pg_reset import reset_schema  # noqa: E402

TEST_DB = os.environ.get("VELOMATE_TEST_DATABASE_URL")

pytestmark = pytest.mark.skipif(
    not TEST_DB,
    reason="needs a throwaway PostgreSQL in VELOMATE_TEST_DATABASE_URL",
)


def test_recalculate_fitness_holds_the_ingest_lock():
    import psycopg2

    if not hasattr(psycopg2, "__file__"):
        pytest.skip("psycopg2 is mocked by another module in this pytest session")

    import db
    import fitness

    a = psycopg2.connect(TEST_DB)
    a.autocommit = True
    b = psycopg2.connect(TEST_DB)
    b.autocommit = True
    reset_schema(a)
    db.create_schema(a)

    def try_lock():
        with b.cursor() as cur:
            cur.execute("SELECT pg_try_advisory_lock(%s)", (db.INGEST_LOCK_KEY,))
            got = cur.fetchone()[0]
            if got:
                cur.execute("SELECT pg_advisory_unlock(%s)", (db.INGEST_LOCK_KEY,))
        return got

    reached, release = threading.Event(), threading.Event()
    real = db.get_sync_state
    first = []

    def blocking(conn, *args, **kw):
        if not first:
            first.append(1)
            reached.set()
            assert release.wait(30)
        return real(conn, *args, **kw)

    failures = []

    def run():
        try:
            fitness.recalculate_fitness(a)
        except BaseException as e:  # surfaced below
            failures.append(e)

    from unittest.mock import patch
    try:
        with patch.object(db, "get_sync_state", blocking):
            t = threading.Thread(target=run)
            t.start()
            assert reached.wait(30), "recalculate_fitness never reached a query"
            assert try_lock() is False
            release.set()
            t.join(60)
        assert not t.is_alive() and not failures, failures
        assert try_lock() is True
    finally:
        release.set()
        a.close()
        b.close()
