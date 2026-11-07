"""Schema reset for the tests that run on a real PostgreSQL."""

import re


def reset_schema(connection):
    """Wipe a throwaway test database, which has to say that it is one.

    These tests run against whatever VELOMATE_TEST_DATABASE_URL names, and one
    copied from a real deployment would lose every ride with no way back. The name
    is an allow-list rather than a check for the production name, because the
    deployment's database name is configurable. "test" has to start a word, so
    velomate_latest doesn't pass as one.
    """
    with connection.cursor() as cur:
        cur.execute("SELECT current_database()")
        name = cur.fetchone()[0]
        if not re.search(r"(^|[^a-z])test", name.lower()):
            raise RuntimeError(
                f"Refusing to wipe database {name!r}: VELOMATE_TEST_DATABASE_URL must "
                "name a throwaway database with 'test' starting a word in its name, "
                "such as velomate_test")
        cur.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
