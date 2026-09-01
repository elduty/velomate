"""Smoke tests for ingestor/main.py — import coverage + key function guards."""

import os
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
            count = ingestor_main.run_backfill(sources=["strava"])
        assert count == 5
        mock_conn.close.assert_called_once()

    def test_closes_conn_on_exception(self):
        mock_conn = MagicMock()
        with (
            patch("main.get_connection", return_value=mock_conn),
            patch("main.create_schema", side_effect=Exception("schema error")),
        ):
            with pytest.raises(Exception, match="schema error"):
                ingestor_main.run_backfill(sources=["strava"])
        mock_conn.close.assert_called_once()

    def test_propagates_backfill_exception(self):
        mock_conn = MagicMock()
        with (
            patch("main.get_connection", return_value=mock_conn),
            patch("main.create_schema"),
            patch("main.backfill", side_effect=RuntimeError("backfill failed")),
        ):
            with pytest.raises(RuntimeError, match="backfill failed"):
                ingestor_main.run_backfill(sources=["strava"])

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
            ingestor_main.run_backfill(sources=["strava"])
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
            ingestor_main.run_backfill(sources=["strava"])
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
            ingestor_main.run_backfill(sources=["strava"])
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
            ingestor_main.run_backfill(sources=["strava"])
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
            ingestor_main.run_backfill(sources=["strava"])
        mock_backfill.assert_called_once_with(mock_conn, months=12)


class TestPollRwgps:
    def test_incremental_sync_when_cursor_present(self):
        """With a persisted cursor, poll does an incremental sync_activities."""
        conn = MagicMock()
        mock_rwgps = MagicMock()
        mock_rwgps.sync_activities.return_value = (3, 0)
        with (
            patch("main._get_healthy_conn", return_value=conn),
            patch("main.get_sync_state", return_value="2026-06-01T00:00:00Z"),
            patch("main.rwgps", mock_rwgps),
            patch("main.recalculate_fitness"),
        ):
            ingestor_main.poll_rwgps()
        mock_rwgps.sync_activities.assert_called_once_with(conn)
        mock_rwgps.backfill.assert_not_called()

    def test_bounded_backfill_when_cursor_absent(self):
        """No cursor means the initial backfill never completed cleanly (e.g. a
        transient failure withheld it). The poll must re-run the BOUNDED backfill,
        not fall through to sync_activities() which would default to since=1970
        with no window and ingest the entire history, ignoring the configured
        VELOMATE_BACKFILL_MONTHS."""
        conn = MagicMock()
        mock_rwgps = MagicMock()
        mock_rwgps.backfill.return_value = 7
        with (
            patch("main._get_healthy_conn", return_value=conn),
            patch("main.get_sync_state", return_value=None),
            patch("main.rwgps", mock_rwgps),
            patch("main._backfill_months", return_value=12),
            patch("main.recalculate_fitness"),
        ):
            ingestor_main.poll_rwgps()
        mock_rwgps.backfill.assert_called_once_with(conn, months=12)
        mock_rwgps.sync_activities.assert_not_called()


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
# _poll_interval_minutes — typo-tolerant like _backfill_months
# ---------------------------------------------------------------------------

class TestPollIntervalMinutes:
    """POLL_INTERVAL_MINUTES must never crash-loop the container on a bad value.
    A non-numeric or non-positive value falls back to the default of 10."""

    def test_default_when_unset(self, monkeypatch):
        monkeypatch.delenv("POLL_INTERVAL_MINUTES", raising=False)
        assert ingestor_main._poll_interval_minutes() == 10

    def test_valid_value(self, monkeypatch):
        monkeypatch.setenv("POLL_INTERVAL_MINUTES", "5")
        assert ingestor_main._poll_interval_minutes() == 5

    def test_invalid_string_falls_back(self, monkeypatch):
        monkeypatch.setenv("POLL_INTERVAL_MINUTES", "ten")
        assert ingestor_main._poll_interval_minutes() == 10

    def test_zero_falls_back(self, monkeypatch):
        """An interval of 0 is nonsensical for schedule.every(N).minutes."""
        monkeypatch.setenv("POLL_INTERVAL_MINUTES", "0")
        assert ingestor_main._poll_interval_minutes() == 10

    def test_negative_falls_back(self, monkeypatch):
        monkeypatch.setenv("POLL_INTERVAL_MINUTES", "-3")
        assert ingestor_main._poll_interval_minutes() == 10

    def test_empty_falls_back(self, monkeypatch):
        monkeypatch.setenv("POLL_INTERVAL_MINUTES", "")
        assert ingestor_main._poll_interval_minutes() == 10


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


# ---------------------------------------------------------------------------
# _enabled_sources — detect configured activity sources
# ---------------------------------------------------------------------------

class TestEnabledSources:
    _STRAVA_ENV = {"STRAVA_CLIENT_ID": "1", "STRAVA_CLIENT_SECRET": "2",
                   "STRAVA_REFRESH_TOKEN": "3"}
    _RWGPS_ENV = {"RWGPS_API_KEY": "k", "RWGPS_AUTH_TOKEN": "t"}

    def test_both_sources(self):
        with patch.dict(os.environ, {**self._STRAVA_ENV, **self._RWGPS_ENV}, clear=True):
            assert ingestor_main._enabled_sources() == ["strava", "rwgps"]

    def test_strava_only(self):
        with patch.dict(os.environ, self._STRAVA_ENV, clear=True):
            assert ingestor_main._enabled_sources() == ["strava"]

    def test_rwgps_only(self):
        with patch.dict(os.environ, self._RWGPS_ENV, clear=True):
            assert ingestor_main._enabled_sources() == ["rwgps"]

    def test_none(self):
        with patch.dict(os.environ, {}, clear=True):
            assert ingestor_main._enabled_sources() == []

    def test_partial_strava_creds_not_enabled(self):
        with patch.dict(os.environ, {"STRAVA_CLIENT_ID": "1"}, clear=True):
            assert ingestor_main._enabled_sources() == []

    def test_partial_rwgps_creds_not_enabled(self):
        with patch.dict(os.environ, {"RWGPS_API_KEY": "k"}, clear=True):
            assert ingestor_main._enabled_sources() == []


class TestIntervalsIcuSource:
    def test_enabled_when_both_credentials_present(self):
        with patch.dict(os.environ, {"INTERVALS_ICU_ATHLETE_ID": "i1",
                                     "INTERVALS_ICU_API_KEY": "k"}, clear=True):
            assert "intervals_icu" in ingestor_main._enabled_sources()

    def test_not_enabled_with_only_the_athlete_id(self):
        with patch.dict(os.environ, {"INTERVALS_ICU_ATHLETE_ID": "i1"}, clear=True):
            assert "intervals_icu" not in ingestor_main._enabled_sources()

    def test_not_enabled_with_only_the_api_key(self):
        with patch.dict(os.environ, {"INTERVALS_ICU_API_KEY": "k"}, clear=True):
            assert "intervals_icu" not in ingestor_main._enabled_sources()

    def test_poll_closes_the_connection_on_success(self):
        with patch.object(ingestor_main, "_get_healthy_conn") as gc, \
             patch.object(ingestor_main.intervals_icu, "sync_activities", return_value=(2, 1)), \
             patch.object(ingestor_main, "recalculate_fitness"):
            conn = gc.return_value
            ingestor_main.poll_intervals_icu()
        conn.close.assert_called_once()

    def test_poll_closes_the_connection_on_error(self):
        with patch.object(ingestor_main, "_get_healthy_conn") as gc, \
             patch.object(ingestor_main.intervals_icu, "sync_activities",
                          side_effect=RuntimeError("boom")):
            conn = gc.return_value
            ingestor_main.poll_intervals_icu()   # must not raise
        conn.close.assert_called_once()

    def test_poll_recalculates_only_when_something_was_ingested(self):
        with patch.object(ingestor_main, "_get_healthy_conn"), \
             patch.object(ingestor_main.intervals_icu, "sync_activities", return_value=(0, 3)), \
             patch.object(ingestor_main, "recalculate_fitness") as recalc:
            ingestor_main.poll_intervals_icu()
        assert not recalc.called, "a window with no new or re-analysed rides must not recalc"

    def test_poll_recalculates_when_rides_were_ingested(self):
        with patch.object(ingestor_main, "_get_healthy_conn"), \
             patch.object(ingestor_main.intervals_icu, "sync_activities", return_value=(2, 0)), \
             patch.object(ingestor_main, "recalculate_fitness") as recalc:
            ingestor_main.poll_intervals_icu()
        assert recalc.called

    def test_poll_skips_cleanly_without_a_db_connection(self):
        with patch.object(ingestor_main, "_get_healthy_conn", return_value=None), \
             patch.object(ingestor_main.intervals_icu, "sync_activities") as sync:
            ingestor_main.poll_intervals_icu()
        assert not sync.called

    def test_reconcile_closes_the_connection(self):
        with patch.object(ingestor_main, "_get_healthy_conn") as gc, \
             patch.object(ingestor_main.intervals_icu, "reconcile", return_value=(1, 0)):
            conn = gc.return_value
            ingestor_main._reconcile_intervals_icu()
        conn.close.assert_called_once()

    def test_reconcile_survives_an_error(self):
        with patch.object(ingestor_main, "_get_healthy_conn") as gc, \
             patch.object(ingestor_main.intervals_icu, "reconcile",
                          side_effect=RuntimeError("boom")):
            conn = gc.return_value
            ingestor_main._reconcile_intervals_icu()   # must not raise
        conn.close.assert_called_once()


class TestPositiveIntEnv:
    def test_default_when_unset(self):
        with patch.dict(os.environ, {}, clear=True):
            assert ingestor_main._positive_int_env("NOPE", 14) == 14

    def test_reads_a_valid_value(self):
        with patch.dict(os.environ, {"X": "30"}, clear=True):
            assert ingestor_main._positive_int_env("X", 14) == 30

    def test_non_numeric_falls_back(self):
        with patch.dict(os.environ, {"X": "abc"}, clear=True):
            assert ingestor_main._positive_int_env("X", 14) == 14

    def test_non_positive_falls_back(self):
        """A zero or negative window would make the sweep delete the library."""
        with patch.dict(os.environ, {"X": "0"}, clear=True):
            assert ingestor_main._positive_int_env("X", 90) == 90
        with patch.dict(os.environ, {"X": "-5"}, clear=True):
            assert ingestor_main._positive_int_env("X", 90) == 90
