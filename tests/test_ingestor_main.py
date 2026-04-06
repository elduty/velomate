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

    def test_uses_default_12_months(self, monkeypatch):
        """No VELOMATE_BACKFILL_MONTHS env var -> backfill called with months=12."""
        monkeypatch.delenv("VELOMATE_BACKFILL_MONTHS", raising=False)
        mock_conn = MagicMock()
        mock_backfill = MagicMock(return_value=5)
        with (
            patch("main.get_connection", return_value=mock_conn),
            patch("main.create_schema"),
            patch("main.backfill", mock_backfill),
            patch("main.recalculate_fitness"),
        ):
            ingestor_main.run_backfill()
        mock_backfill.assert_called_once_with(mock_conn, months=12)

    def test_reads_months_from_env(self, monkeypatch):
        """VELOMATE_BACKFILL_MONTHS=24 -> backfill called with months=24."""
        monkeypatch.setenv("VELOMATE_BACKFILL_MONTHS", "24")
        mock_conn = MagicMock()
        mock_backfill = MagicMock(return_value=5)
        with (
            patch("main.get_connection", return_value=mock_conn),
            patch("main.create_schema"),
            patch("main.backfill", mock_backfill),
            patch("main.recalculate_fitness"),
        ):
            ingestor_main.run_backfill()
        mock_backfill.assert_called_once_with(mock_conn, months=24)

    def test_zero_means_full_history(self, monkeypatch):
        """VELOMATE_BACKFILL_MONTHS=0 -> backfill called with months=0 (full history)."""
        monkeypatch.setenv("VELOMATE_BACKFILL_MONTHS", "0")
        mock_conn = MagicMock()
        mock_backfill = MagicMock(return_value=5)
        with (
            patch("main.get_connection", return_value=mock_conn),
            patch("main.create_schema"),
            patch("main.backfill", mock_backfill),
            patch("main.recalculate_fitness"),
        ):
            ingestor_main.run_backfill()
        mock_backfill.assert_called_once_with(mock_conn, months=0)

    def test_invalid_env_falls_back_to_default(self, monkeypatch):
        """A typo in the env var should not block ingestion — default to 12."""
        monkeypatch.setenv("VELOMATE_BACKFILL_MONTHS", "twelve")
        mock_conn = MagicMock()
        mock_backfill = MagicMock(return_value=5)
        with (
            patch("main.get_connection", return_value=mock_conn),
            patch("main.create_schema"),
            patch("main.backfill", mock_backfill),
            patch("main.recalculate_fitness"),
        ):
            ingestor_main.run_backfill()
        mock_backfill.assert_called_once_with(mock_conn, months=12)

    def test_negative_env_falls_back_to_default(self, monkeypatch):
        """Negative values are nonsensical — default to 12."""
        monkeypatch.setenv("VELOMATE_BACKFILL_MONTHS", "-3")
        mock_conn = MagicMock()
        mock_backfill = MagicMock(return_value=5)
        with (
            patch("main.get_connection", return_value=mock_conn),
            patch("main.create_schema"),
            patch("main.backfill", mock_backfill),
            patch("main.recalculate_fitness"),
        ):
            ingestor_main.run_backfill()
        mock_backfill.assert_called_once_with(mock_conn, months=12)


# ---------------------------------------------------------------------------
# _parse_backfill_months
# ---------------------------------------------------------------------------

class TestParseBackfillMonths:
    def test_none(self):
        assert ingestor_main._parse_backfill_months(None) is None

    def test_integer_string(self):
        assert ingestor_main._parse_backfill_months("12") == 12

    def test_zero(self):
        assert ingestor_main._parse_backfill_months("0") == 0

    def test_large_value(self):
        assert ingestor_main._parse_backfill_months("240") == 240

    def test_invalid_string(self):
        assert ingestor_main._parse_backfill_months("twelve") is None

    def test_empty_string(self):
        assert ingestor_main._parse_backfill_months("") is None


# ---------------------------------------------------------------------------
# _describe_backfill_months
# ---------------------------------------------------------------------------

class TestDescribeBackfillMonths:
    def test_zero_is_full_history(self):
        assert ingestor_main._describe_backfill_months(0) == "FULL history"

    def test_positive(self):
        assert ingestor_main._describe_backfill_months(12) == "12 months"
        assert ingestor_main._describe_backfill_months(24) == "24 months"


# ---------------------------------------------------------------------------
# _backfill_window_extended
# ---------------------------------------------------------------------------

class TestBackfillWindowExtended:
    """True when the configured window grew and a re-backfill should be forced."""

    def test_fresh_install_is_never_extended(self):
        """has_data=False → False regardless of values (first-run path handles it)."""
        assert ingestor_main._backfill_window_extended(12, None, has_data=False) is False
        assert ingestor_main._backfill_window_extended(24, None, has_data=False) is False
        assert ingestor_main._backfill_window_extended(0, None, has_data=False) is False
        assert ingestor_main._backfill_window_extended(0, "12", has_data=False) is False

    def test_existing_deployment_no_persisted_value_same_as_historical(self):
        """old=None on existing deployment → assume historical default 12. new=12 is same."""
        assert ingestor_main._backfill_window_extended(12, None, has_data=True) is False

    def test_existing_deployment_no_persisted_value_extending(self):
        """old=None on existing deployment → assume 12. new=24 extends."""
        assert ingestor_main._backfill_window_extended(24, None, has_data=True) is True

    def test_existing_deployment_no_persisted_value_full_history(self):
        """old=None on existing deployment → assume 12. new=0 (full) extends."""
        assert ingestor_main._backfill_window_extended(0, None, has_data=True) is True

    def test_existing_deployment_no_persisted_value_shrinking(self):
        """old=None on existing deployment → assume 12. new=6 is shrinking, not extending."""
        assert ingestor_main._backfill_window_extended(6, None, has_data=True) is False

    def test_same_value(self):
        assert ingestor_main._backfill_window_extended(12, "12", has_data=True) is False
        assert ingestor_main._backfill_window_extended(0, "0", has_data=True) is False

    def test_extending(self):
        assert ingestor_main._backfill_window_extended(24, "12", has_data=True) is True

    def test_shrinking(self):
        assert ingestor_main._backfill_window_extended(12, "24", has_data=True) is False

    def test_bounded_to_full_history(self):
        """Any bounded → 0 (infinite) is an extension."""
        assert ingestor_main._backfill_window_extended(0, "12", has_data=True) is True
        assert ingestor_main._backfill_window_extended(0, "24", has_data=True) is True

    def test_full_history_to_bounded(self):
        """0 (infinite) → any bounded is a shrink, not an extension."""
        assert ingestor_main._backfill_window_extended(12, "0", has_data=True) is False
        assert ingestor_main._backfill_window_extended(24, "0", has_data=True) is False

    def test_corrupted_old_value_forces_refresh(self):
        """Garbage in sync_state → safer to refresh than silently ignore."""
        assert ingestor_main._backfill_window_extended(12, "foo", has_data=True) is True
        assert ingestor_main._backfill_window_extended(0, "xyz", has_data=True) is True


# ---------------------------------------------------------------------------
# _backfill_window_shrunk (logging only, never triggers action)
# ---------------------------------------------------------------------------

class TestBackfillWindowShrunk:
    def test_fresh_install(self):
        assert ingestor_main._backfill_window_shrunk(12, None, has_data=False) is False

    def test_no_persisted_value(self):
        """old=None → False (no baseline to compare against for shrink detection)."""
        assert ingestor_main._backfill_window_shrunk(6, None, has_data=True) is False

    def test_same_value(self):
        assert ingestor_main._backfill_window_shrunk(12, "12", has_data=True) is False

    def test_shrinking_bounded(self):
        assert ingestor_main._backfill_window_shrunk(12, "24", has_data=True) is True

    def test_extending_bounded_not_shrunk(self):
        assert ingestor_main._backfill_window_shrunk(24, "12", has_data=True) is False

    def test_bounded_to_full_not_shrunk(self):
        """Going to full history is extending, not shrinking."""
        assert ingestor_main._backfill_window_shrunk(0, "12", has_data=True) is False

    def test_full_to_bounded_is_shrunk(self):
        """Going from full to bounded is a shrink."""
        assert ingestor_main._backfill_window_shrunk(12, "0", has_data=True) is True
        assert ingestor_main._backfill_window_shrunk(24, "0", has_data=True) is True

    def test_corrupted_old_value(self):
        """Corrupted values are handled by _backfill_window_extended — shrunk returns False."""
        assert ingestor_main._backfill_window_shrunk(12, "foo", has_data=True) is False


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
