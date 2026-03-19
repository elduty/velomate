"""Tests for audit fix items O3, O5, O8, O9, O15."""

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch, call
from datetime import datetime, timezone, timedelta

import pytest

# Mock psycopg2 and requests before any ingestor imports
sys.modules.setdefault("psycopg2", MagicMock())
sys.modules.setdefault("psycopg2.extras", MagicMock())
sys.modules.setdefault("requests", MagicMock())
sys.modules.setdefault("schedule", MagicMock())

_ingestor_dir = Path(__file__).resolve().parent.parent / "ingestor"
if str(_ingestor_dir) not in sys.path:
    sys.path.insert(0, str(_ingestor_dir))


# ---------------------------------------------------------------------------
# O3 — refresh_access_token updates _current_refresh_token in memory
#        even when DB write fails.
# ---------------------------------------------------------------------------

class TestO3RefreshTokenMemoryUpdate:
    """Audit item O3: if DB write of rotated refresh token fails, the in-memory
    _current_refresh_token must still be updated so the current process doesn't
    reuse the old (now-invalid) token."""

    def _make_mock_response(self, new_refresh="new_token_xyz"):
        resp = MagicMock()
        resp.json.return_value = {
            "access_token": "fresh_access",
            "expires_at": 9999999999,
            "refresh_token": new_refresh,
        }
        resp.raise_for_status = MagicMock()
        return resp

    def test_token_updated_in_memory_when_db_write_fails(self):
        import strava
        # Reset module state
        strava._access_token = None
        strava._token_expires_at = 0
        strava._current_refresh_token = None

        mock_resp = self._make_mock_response("rotated_token")

        # strava.py uses inline `from db import get_connection, set_sync_state`
        # so we patch via sys.modules['db']
        import sys
        mock_db = MagicMock()
        mock_db.get_connection.side_effect = Exception("DB down")
        sys.modules["db"] = mock_db

        try:
            with patch.object(strava, "_request_with_retry", return_value=mock_resp):
                result = strava.refresh_access_token("id", "secret", "old_token")
        finally:
            # restore original db module
            del sys.modules["db"]

        assert result == "fresh_access"
        # Even though DB write failed, in-memory token must be updated
        assert strava._current_refresh_token == "rotated_token"

    def test_token_updated_in_memory_when_db_set_sync_state_fails(self):
        import strava
        strava._access_token = None
        strava._token_expires_at = 0
        strava._current_refresh_token = None

        mock_resp = self._make_mock_response("rotated_v2")
        mock_conn = MagicMock()

        import sys
        mock_db = MagicMock()
        mock_db.get_connection.return_value = mock_conn
        mock_db.set_sync_state.side_effect = Exception("write failed")
        sys.modules["db"] = mock_db

        try:
            with patch.object(strava, "_request_with_retry", return_value=mock_resp):
                strava.refresh_access_token("id", "secret", "old_token")
        finally:
            del sys.modules["db"]

        assert strava._current_refresh_token == "rotated_v2"

    def test_no_update_when_refresh_token_unchanged(self):
        """If Strava returns the same token, don't touch _current_refresh_token."""
        import strava
        strava._access_token = None
        strava._token_expires_at = 0
        strava._current_refresh_token = "existing_token"

        mock_resp = self._make_mock_response("existing_token")  # same as old

        with patch.object(strava, "_request_with_retry", return_value=mock_resp):
            strava.refresh_access_token("id", "secret", "existing_token")

        # Token wasn't rotated, so _current_refresh_token stays as-is
        assert strava._current_refresh_token == "existing_token"


# ---------------------------------------------------------------------------
# O5 — _to_local() correctly converts UTC ISO timestamps to local HH:MM
#       using timezone offset, including fractional offsets (India, Nepal).
# ---------------------------------------------------------------------------

class TestO5ToLocal:
    """Audit item O5: _to_local() must handle fractional UTC offsets correctly."""

    def _call_fetch_sunrise(self, iso_str: str):
        """Use the actual _to_local logic by calling fetch_sunrise_sunset with mocked data."""
        from unittest.mock import patch as _patch
        import requests as req_mod
        # We test _to_local indirectly via fetch_sunrise_sunset
        from veloai.weather import fetch_sunrise_sunset

        mock_resp = MagicMock()
        mock_resp.json.return_value = {
            "status": "OK",
            "results": {
                "sunrise": iso_str,
                "sunset": iso_str,
                "civil_twilight_end": iso_str,
            }
        }
        mock_resp.raise_for_status = MagicMock()

        with _patch("veloai.weather.requests.get", return_value=mock_resp):
            result = fetch_sunrise_sunset(0.0, 0.0, "2026-03-19")
        return result

    def test_utc_timestamp(self):
        """UTC+0: 2026-03-19T06:00:00+00:00 → 06:00, offset 0."""
        result = self._call_fetch_sunrise("2026-03-19T06:00:00+00:00")
        assert result["sunrise"] == "06:00"
        assert result["utc_offset_h"] == 0.0
        assert result["tz_label"] == "UTC"

    def test_positive_offset(self):
        """UTC+2: 2026-03-19T08:00:00+02:00 → 08:00, offset 2."""
        result = self._call_fetch_sunrise("2026-03-19T08:00:00+02:00")
        assert result["sunrise"] == "08:00"
        assert result["utc_offset_h"] == 2.0
        assert result["tz_label"] == "UTC+2"

    def test_negative_offset(self):
        """UTC-5: 2026-03-19T01:00:00-05:00 → 01:00, offset -5."""
        result = self._call_fetch_sunrise("2026-03-19T01:00:00-05:00")
        assert result["sunrise"] == "01:00"
        assert result["utc_offset_h"] == -5.0
        assert result["tz_label"] == "UTC-5"

    def test_india_fractional_offset(self):
        """India UTC+5:30 — must NOT be truncated to 5."""
        # 2026-03-19T11:30:00+05:30 = local 11:30
        result = self._call_fetch_sunrise("2026-03-19T11:30:00+05:30")
        assert result["sunrise"] == "11:30"
        assert result["utc_offset_h"] == pytest.approx(5.5)
        assert result["tz_label"] == "UTC+5:30"

    def test_nepal_fractional_offset(self):
        """Nepal UTC+5:45 — must NOT be truncated to 5."""
        result = self._call_fetch_sunrise("2026-03-19T11:45:00+05:45")
        assert result["sunrise"] == "11:45"
        assert result["utc_offset_h"] == pytest.approx(5.75)
        assert result["tz_label"] == "UTC+5:45"

    def test_empty_string_returns_empty(self):
        result = self._call_fetch_sunrise("")
        assert result["sunrise"] == ""
        assert result["utc_offset_h"] == 0


# ---------------------------------------------------------------------------
# O8 — startup retry logic: exits after max attempts, succeeds on retry.
# ---------------------------------------------------------------------------

class TestO8StartupRetry:
    """Audit item O8: run() retries DB connection up to max_attempts, then exits."""

    def test_exits_after_max_attempts(self):
        import main as ingestor_main

        with (
            patch("main.get_connection", side_effect=Exception("DB down")),
            patch("main.time.sleep"),
            patch("main.sys.exit") as mock_exit,
        ):
            # Prevent infinite loop — sys.exit is mocked, so raise to break out
            mock_exit.side_effect = SystemExit(1)
            with pytest.raises(SystemExit):
                ingestor_main.run()

        mock_exit.assert_called_once_with(1)

    def test_retry_count_before_exit(self):
        """Verifies exactly max_attempts (10) connection attempts before exit."""
        import main as ingestor_main

        attempt_count = []

        def failing_conn():
            attempt_count.append(1)
            raise Exception("DB down")

        with (
            patch("main.get_connection", side_effect=failing_conn),
            patch("main.time.sleep"),
            patch("main.sys.exit", side_effect=SystemExit(1)),
        ):
            with pytest.raises(SystemExit):
                ingestor_main.run()

        assert len(attempt_count) == 10  # max_attempts = 10

    def test_succeeds_on_retry(self):
        """If DB is available on attempt N < max_attempts, run continues."""
        import main as ingestor_main

        # Fail twice during the retry loop, then succeed.
        # Note: run() also calls get_connection() once more after the retry loop
        # for fitness recalculation when has_data is truthy — hence call_count can
        # exceed the retry attempt count. We verify DB-down attempts are < max_attempts.
        retry_failures = [0]
        succeeded = [False]
        mock_conn = MagicMock()

        def flaky_conn():
            if not succeeded[0]:
                retry_failures[0] += 1
                if retry_failures[0] < 3:
                    raise Exception("not ready yet")
                succeeded[0] = True
            return mock_conn

        with (
            patch("main.get_connection", side_effect=flaky_conn),
            patch("main.create_schema"),
            patch("main.get_sync_state", return_value="some_value"),
            patch("main.time.sleep"),
            patch("main.recalculate_fitness"),
            patch("main.poll_strava"),
            patch("main.schedule.every", return_value=MagicMock()),
            # Break the infinite while loop
            patch("main.schedule.run_pending", side_effect=KeyboardInterrupt),
        ):
            with pytest.raises(KeyboardInterrupt):
                ingestor_main.run()

        # Should have retried exactly 3 times before succeeding (2 fails + 1 success)
        assert retry_failures[0] == 3
        # Did NOT exhaust max_attempts
        assert retry_failures[0] < 10


# ---------------------------------------------------------------------------
# O9 — upsert_streams atomicity: uses a transaction (autocommit=False).
# ---------------------------------------------------------------------------

class TestO9UpsertStreamsAtomicity:
    """Audit item O9: upsert_streams must use a transaction."""

    def test_sets_autocommit_false_before_operations(self):
        from db import upsert_streams

        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_conn.cursor.return_value.__enter__ = MagicMock(return_value=mock_cursor)
        mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)

        autocommit_sequence = []

        def track_autocommit(val):
            autocommit_sequence.append(val)

        type(mock_conn).autocommit = property(
            fget=lambda self: autocommit_sequence[-1] if autocommit_sequence else True,
            fset=lambda self, val: track_autocommit(val),
        )

        upsert_streams(mock_conn, activity_id=42, streams=[])

        # autocommit should have been set to False then back to True
        assert False in autocommit_sequence
        assert autocommit_sequence[-1] is True

    def test_commits_on_success(self):
        from db import upsert_streams

        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_conn.cursor.return_value.__enter__ = MagicMock(return_value=mock_cursor)
        mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)

        upsert_streams(mock_conn, activity_id=1, streams=[])

        mock_conn.commit.assert_called_once()
        mock_conn.rollback.assert_not_called()

    def test_rollbacks_on_exception(self):
        from db import upsert_streams

        mock_conn = MagicMock()
        mock_conn.cursor.return_value.__enter__ = MagicMock(
            side_effect=Exception("cursor error")
        )
        mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)

        with pytest.raises(Exception, match="cursor error"):
            upsert_streams(mock_conn, activity_id=1, streams=[])

        mock_conn.rollback.assert_called_once()
        # autocommit restored to True even after exception
        assert mock_conn.autocommit is True

    def test_restores_autocommit_on_exception(self):
        """autocommit must be restored to True even if an exception occurs."""
        from db import upsert_streams

        mock_conn = MagicMock()
        autocommit_values = []

        def set_ac(val):
            autocommit_values.append(val)

        type(mock_conn).autocommit = property(
            fget=lambda self: autocommit_values[-1] if autocommit_values else True,
            fset=lambda self, val: set_ac(val),
        )
        mock_conn.cursor.return_value.__enter__ = MagicMock(
            side_effect=Exception("DB error")
        )
        mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)

        with pytest.raises(Exception):
            upsert_streams(mock_conn, 1, [])

        assert autocommit_values[-1] is True


# ---------------------------------------------------------------------------
# O15 — config cache: different config_path triggers reload.
# ---------------------------------------------------------------------------

class TestO15ConfigCache:
    """Audit item O15: config.load() must reload when config_path changes."""

    def _reset_config(self):
        """Reset module-level cache between tests."""
        import veloai.config as cfg
        cfg._config = None
        cfg._config_path_used = None

    def test_same_path_returns_cached(self, tmp_path):
        import veloai.config as cfg
        self._reset_config()

        config_file = tmp_path / "config.yaml"
        config_file.write_text("home:\n  lat: 1.0\n  lng: 2.0\n")

        result1 = cfg.load(str(config_file))
        result2 = cfg.load(str(config_file))

        assert result1 is result2  # same object — from cache

    def test_different_path_triggers_reload(self, tmp_path):
        import veloai.config as cfg
        self._reset_config()

        config_a = tmp_path / "config_a.yaml"
        config_b = tmp_path / "config_b.yaml"
        config_a.write_text("home:\n  lat: 10.0\n  lng: 20.0\n")
        config_b.write_text("home:\n  lat: 50.0\n  lng: 60.0\n")

        result_a = cfg.load(str(config_a))
        result_b = cfg.load(str(config_b))

        assert result_a["home"]["lat"] == pytest.approx(10.0)
        assert result_b["home"]["lat"] == pytest.approx(50.0)
        # Configs from different paths must differ
        assert result_a is not result_b

    def test_cache_returns_correct_path(self, tmp_path):
        import veloai.config as cfg
        self._reset_config()

        config_file = tmp_path / "config.yaml"
        config_file.write_text("home:\n  lat: 5.0\n  lng: 6.0\n")

        cfg.load(str(config_file))
        assert cfg._config_path_used == str(config_file)

    def test_reload_after_path_change_updates_cache_key(self, tmp_path):
        import veloai.config as cfg
        self._reset_config()

        config_a = tmp_path / "a.yaml"
        config_b = tmp_path / "b.yaml"
        config_a.write_text("home:\n  lat: 1.0\n  lng: 1.0\n")
        config_b.write_text("home:\n  lat: 2.0\n  lng: 2.0\n")

        cfg.load(str(config_a))
        assert cfg._config_path_used == str(config_a)

        cfg.load(str(config_b))
        assert cfg._config_path_used == str(config_b)
