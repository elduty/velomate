"""The schema reset must refuse any database that doesn't say it is a test one."""
from unittest.mock import MagicMock

import pytest

from pg_reset import reset_schema


def _connection(name):
    connection = MagicMock()
    cursor = connection.cursor.return_value.__enter__.return_value
    cursor.fetchone.return_value = (name,)
    return connection, cursor


def _dropped(cursor):
    return any("DROP SCHEMA" in call.args[0] for call in cursor.execute.call_args_list)


@pytest.mark.parametrize("name", ["velomate", "postgres", "rides",
                                  "velomate_latest", "contest", "attestation"])
def test_refuses_a_database_not_named_as_a_test_one(name):
    connection, cursor = _connection(name)

    with pytest.raises(RuntimeError, match=name):
        reset_schema(connection)

    assert not _dropped(cursor)


@pytest.mark.parametrize("name", ["velomate_test", "test", "CI_TEST_db"])
def test_wipes_a_test_database(name):
    connection, cursor = _connection(name)

    reset_schema(connection)

    assert _dropped(cursor)
