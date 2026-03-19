"""Smoke tests for ingestor/main.py — import coverage + key function guards."""

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch, call

import pytest

# Mock DB and external deps before importing ingestor modules
sys.modules.setdefault("psycopg2", MagicMock())
sys.modules.setdefault("psycopg2.extras", MagicMock())
sys.modules.setdefault("requests", MagicMock())
sys.modules.setdefault("schedule", MagicMock())

# Add ingestor/ to path (no __init__.py)
_ingestor_dir = Path(__file__).resolve().parent.parent / "ingestor"
if str(_ingestor_dir) not in sys.path:
    sys.path.insert(0, str(_ingestor_dir))

# ---------------------------------------------------------------------------
# Import smoke test — catches syntax errors and import failures
# ---------------------------------------------------------------------------

import main as ingestor_main  # noqa: E402  (must come after sys.path setup)


class TestImportSmoke:
    """Verify ingestor/main.py can be imported and key names exist."""

    def test_module_imports(self):
        assert ingestor_main is not None

    def test_get_healthy_conn_exists(self):
        assert callable(ingestor_main._get_healthy_conn)

    def test_poll_strava_exists(self):
        assert callable(ingestor_main.poll_strava)

    def test_run_backfill_exists(self):
        assert callable(ingestor_main.run_backfill)

    def test_run_reclassify_exists(self):
        assert callable(ingestor_main.run_reclassify)

    def test_run_exists(self):
        assert callable(ingestor_main.run)


# ---------------------------------------------------------------------------
# _get_healthy_conn
# ---------------------------------------------------------------------------

class TestGetHealthyConn:
    def test_returns_conn_on_success(self):
        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_conn.cursor.return_value.__enter__ = MagicMock(return_value=mock_cursor)
        mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)

        with patch("main.get_connection", return_value=mock_conn):
            result = ingestor_main._get_healthy_conn()
        assert result is mock_conn

    def test_returns_none_when_first_conn_raises_and_reconnect_fails(self):
        with patch("main.get_connection", side_effect=Exception("DB down")):
            result = ingestor_main._get_healthy_conn()
        assert result is None

    def test_reconnects_when_cursor_fails(self):
        """If SELECT 1 fails, tries get_connection() again."""
        bad_conn = MagicMock()
        bad_conn.cursor.side_effect = Exception("connection lost")
        good_conn = MagicMock()
        good_cursor = MagicMock()
        good_conn.cursor.return_value.__enter__ = MagicMock(return_value=good_cursor)
        good_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)

        with patch("main.get_connection", side_effect=[bad_conn, good_conn]):
            result = ingestor_main._get_healthy_conn()
        assert result is good_conn

    def test_returns_none_when_reconnect_also_fails(self):
        bad_conn = MagicMock()
        bad_conn.cursor.side_effect = Exception("connection lost")

        with patch("main.get_connection", side_effect=[bad_conn, Exception("still down")]):
            result = ingestor_main._get_healthy_conn()
        assert result is None


# ---------------------------------------------------------------------------
# run_backfill — guards against missing DB
# ---------------------------------------------------------------------------

class TestRunBackfill:
    def test_closes_conn_on_success(self):
        mock_conn = MagicMock()
        with (
            patch("main.get_connection", return_value=mock_conn),
            patch("main.create_schema"),
            patch("main.backfill", return_value=5),
            patch("main.recalculate_fitness"),
        ):
            count = ingestor_main.run_backfill()
        assert count == 5
        mock_conn.close.assert_called_once()

    def test_closes_conn_on_exception(self):
        mock_conn = MagicMock()
        with (
            patch("main.get_connection", return_value=mock_conn),
            patch("main.create_schema", side_effect=Exception("schema error")),
        ):
            with pytest.raises(Exception, match="schema error"):
                ingestor_main.run_backfill()
        mock_conn.close.assert_called_once()

    def test_propagates_backfill_exception(self):
        mock_conn = MagicMock()
        with (
            patch("main.get_connection", return_value=mock_conn),
            patch("main.create_schema"),
            patch("main.backfill", side_effect=RuntimeError("backfill failed")),
        ):
            with pytest.raises(RuntimeError, match="backfill failed"):
                ingestor_main.run_backfill()


# ---------------------------------------------------------------------------
# run_reclassify — guards against missing DB
# ---------------------------------------------------------------------------

class TestRunReclassify:
    def test_closes_conn_on_success(self):
        mock_conn = MagicMock()
        with (
            patch("main.get_connection", return_value=mock_conn),
            patch("main.reclassify_activities"),
            patch("main.recalculate_fitness"),
        ):
            ingestor_main.run_reclassify()
        mock_conn.close.assert_called_once()

    def test_closes_conn_on_exception(self):
        mock_conn = MagicMock()
        with (
            patch("main.get_connection", return_value=mock_conn),
            patch("main.reclassify_activities", side_effect=Exception("reclassify failed")),
        ):
            with pytest.raises(Exception, match="reclassify failed"):
                ingestor_main.run_reclassify()
        mock_conn.close.assert_called_once()


# ---------------------------------------------------------------------------
# N1 — connection leak fix: failed conn closed before reconnect
# ---------------------------------------------------------------------------

class TestGetHealthyConnN1:
    def test_closes_failed_conn_before_reconnect(self):
        """N1: first connection that fails SELECT 1 must be closed before reconnect."""
        bad_conn = MagicMock()
        bad_conn.cursor.return_value.__enter__ = MagicMock(side_effect=Exception("conn dead"))
        bad_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)

        good_conn = MagicMock()

        with patch("main.get_connection", side_effect=[bad_conn, good_conn]):
            result = ingestor_main._get_healthy_conn()

        bad_conn.close.assert_called_once()
        assert result is good_conn

    def test_does_not_leak_when_reconnect_also_fails(self):
        """N1: failed conn is still closed even when reconnect raises."""
        bad_conn = MagicMock()
        bad_conn.cursor.return_value.__enter__ = MagicMock(side_effect=Exception("conn dead"))
        bad_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)

        with patch("main.get_connection", side_effect=[bad_conn, Exception("reconnect failed")]):
            result = ingestor_main._get_healthy_conn()

        bad_conn.close.assert_called_once()
        assert result is None
