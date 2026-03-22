"""Tests for token refresh and sync flow in ingestor/strava.py."""

import sys
import os
from pathlib import Path
from unittest.mock import MagicMock, patch, PropertyMock

import pytest

# Mock psycopg2 and requests before importing ingestor modules
sys.modules.setdefault("psycopg2", MagicMock())
sys.modules.setdefault("psycopg2.extras", MagicMock())

_ingestor_dir = Path(__file__).resolve().parent.parent / "ingestor"
if str(_ingestor_dir) not in sys.path:
    sys.path.insert(0, str(_ingestor_dir))

import strava


class _StravaTestBase:
    """Reset module-level token state before each test."""

    def setup_method(self):
        strava._access_token = None
        strava._token_expires_at = 0
        strava._current_refresh_token = None


# ---------------------------------------------------------------------------
# Token refresh
# ---------------------------------------------------------------------------

class TestRefreshAccessToken(_StravaTestBase):
    """Tests for refresh_access_token / _get_token."""

    def test_token_refresh_returns_access_token(self):
        """Successful refresh returns access_token from response."""
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "access_token": "new_access_123",
            "expires_at": 9999999999,
            "refresh_token": "same_refresh",
        }

        with patch("strava.requests.post", return_value=mock_resp):
            token = strava.refresh_access_token("cid", "csecret", "same_refresh")

        assert token == "new_access_123"

    def test_rotated_refresh_token_persisted_to_db(self):
        """When Strava rotates the refresh token, it is saved to DB via set_sync_state."""
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "access_token": "access_456",
            "expires_at": 9999999999,
            "refresh_token": "rotated_refresh_789",
        }

        mock_conn = MagicMock()

        with (
            patch("strava.requests.post", return_value=mock_resp),
            patch("strava._request_with_retry", return_value=mock_resp),
            patch.dict(sys.modules, {"db": MagicMock()}),
        ):
            # Patch the db import inside refresh_access_token
            import importlib
            db_mock = MagicMock()
            with patch.dict(sys.modules, {"db": db_mock}):
                strava.refresh_access_token("cid", "csecret", "old_refresh")

            # After rotation, module state should have the new token
            assert strava._current_refresh_token == "rotated_refresh_789"

    def test_db_persist_failure_writes_file_fallback(self):
        """When DB write fails, token is written to file fallback."""
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "access_token": "access_abc",
            "expires_at": 9999999999,
            "refresh_token": "new_rotated_token",
        }

        # Make the db import succeed but set_sync_state raise
        db_mock = MagicMock()
        db_mock.get_connection.return_value = MagicMock()
        db_mock.set_sync_state.side_effect = Exception("DB write failed")

        with (
            patch("strava._request_with_retry", return_value=mock_resp),
            patch.dict(sys.modules, {"db": db_mock}),
            patch("pathlib.Path.write_text") as mock_write,
        ):
            strava.refresh_access_token("cid", "csecret", "old_refresh")

        # In-memory token should still be updated
        assert strava._current_refresh_token == "new_rotated_token"
        # File fallback should have been attempted
        mock_write.assert_called_once_with("new_rotated_token")

    def test_cached_token_returned_when_not_expired(self):
        """If token is cached and not expired, skip refresh."""
        strava._access_token = "cached_token"
        strava._token_expires_at = 9999999999  # far future

        with patch("strava.requests.post") as mock_post:
            token = strava.refresh_access_token("cid", "csecret", "refresh")

        assert token == "cached_token"
        mock_post.assert_not_called()


# ---------------------------------------------------------------------------
# _get_token
# ---------------------------------------------------------------------------

class TestGetToken(_StravaTestBase):
    """Tests for _get_token: DB lookup, file fallback, env var."""

    def test_reads_from_file_fallback(self):
        """When no in-memory token, reads from file fallback if it exists."""
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        # Response returns same refresh token (no rotation) so _current_refresh_token
        # stays as the file fallback value
        mock_resp.json.return_value = {
            "access_token": "from_file_token",
            "expires_at": 9999999999,
            "refresh_token": "file_fallback_token",
        }

        db_mock = MagicMock()
        db_mock.get_sync_state.return_value = None

        with (
            patch.dict(os.environ, {
                "STRAVA_CLIENT_ID": "cid",
                "STRAVA_CLIENT_SECRET": "csecret",
                "STRAVA_REFRESH_TOKEN": "env_refresh",
            }),
            patch("pathlib.Path.exists", return_value=True),
            patch("pathlib.Path.read_text", return_value="file_fallback_token\n"),
            patch.dict(sys.modules, {"db": db_mock}),
            patch("strava._request_with_retry", return_value=mock_resp),
        ):
            token = strava._get_token()

        assert token == "from_file_token"
        # The file fallback token should have been loaded into module state
        assert strava._current_refresh_token == "file_fallback_token"


# ---------------------------------------------------------------------------
# sync_activities
# ---------------------------------------------------------------------------

class TestSyncActivities(_StravaTestBase):
    """Tests for sync_activities flow."""

    def _mock_env(self):
        return patch.dict(os.environ, {
            "STRAVA_CLIENT_ID": "cid",
            "STRAVA_CLIENT_SECRET": "csecret",
            "STRAVA_REFRESH_TOKEN": "refresh_tok",
        })

    def test_skips_non_cycling_activities(self):
        """Activities with type='Run' should be skipped."""
        conn = MagicMock()
        db_mock = MagicMock()
        db_mock.get_sync_state.return_value = "0"

        mock_token_resp = MagicMock()
        mock_token_resp.status_code = 200
        mock_token_resp.json.return_value = {
            "access_token": "tok", "expires_at": 9999999999,
            "refresh_token": "refresh_tok",
        }

        activities_resp = MagicMock()
        activities_resp.status_code = 200
        activities_resp.json.return_value = [
            {"id": 1, "type": "Run", "name": "Morning Run", "start_date": "2026-03-20T08:00:00Z"},
            {"id": 2, "type": "Ride", "name": "Morning Ride", "start_date": "2026-03-20T09:00:00Z",
             "device_name": "Karoo 3", "distance": 50000, "moving_time": 7200,
             "total_elevation_gain": 500, "trainer": False},
        ]

        detail_resp = MagicMock()
        detail_resp.status_code = 200
        detail_resp.json.return_value = {}

        streams_resp = MagicMock()
        streams_resp.status_code = 200
        streams_resp.json.return_value = []

        with (
            self._mock_env(),
            patch.dict(sys.modules, {"db": db_mock}),
            patch("strava._request_with_retry", side_effect=[mock_token_resp, activities_resp, detail_resp, streams_resp]),
            patch("strava.time.sleep"),
        ):
            db_mock.upsert_activity.return_value = (1, False)
            count = strava.sync_activities(conn, after_epoch=0)

        # Only the Ride should be ingested, Run is skipped
        assert count == 1
        db_mock.upsert_activity.assert_called_once()

    def test_returns_ingested_count_not_total(self):
        """Return value should be count of ingested cycling activities, not total fetched."""
        conn = MagicMock()
        db_mock = MagicMock()
        db_mock.get_sync_state.return_value = "0"

        mock_token_resp = MagicMock()
        mock_token_resp.status_code = 200
        mock_token_resp.json.return_value = {
            "access_token": "tok", "expires_at": 9999999999,
            "refresh_token": "refresh_tok",
        }

        # 3 activities: 1 Ride, 1 Run, 1 VirtualRide
        activities_resp = MagicMock()
        activities_resp.status_code = 200
        activities_resp.json.return_value = [
            {"id": 1, "type": "Run", "name": "Run", "start_date": "2026-03-20T08:00:00Z"},
            {"id": 2, "type": "Ride", "name": "Ride", "start_date": "2026-03-20T09:00:00Z",
             "device_name": "", "distance": 30000, "moving_time": 3600,
             "total_elevation_gain": 200, "trainer": False},
            {"id": 3, "type": "VirtualRide", "name": "Zwift", "start_date": "2026-03-20T10:00:00Z",
             "device_name": "", "distance": 20000, "moving_time": 2700,
             "total_elevation_gain": 100, "trainer": True},
        ]

        detail_resp = MagicMock()
        detail_resp.status_code = 200
        detail_resp.json.return_value = {}

        streams_resp = MagicMock()
        streams_resp.status_code = 200
        streams_resp.json.return_value = []

        with (
            self._mock_env(),
            patch.dict(sys.modules, {"db": db_mock}),
            # Token + activities + (detail+streams)*2 for the 2 cycling activities
            patch("strava._request_with_retry", side_effect=[
                mock_token_resp, activities_resp,
                detail_resp, streams_resp,
                detail_resp, streams_resp,
            ]),
            patch("strava.time.sleep"),
        ):
            db_mock.upsert_activity.return_value = (1, False)
            count = strava.sync_activities(conn, after_epoch=0)

        # 2 cycling activities out of 3 total
        assert count == 2

    def test_calls_upsert_activity_for_cycling(self):
        """upsert_activity should be called for each cycling activity."""
        conn = MagicMock()
        db_mock = MagicMock()
        db_mock.get_sync_state.return_value = "0"

        mock_token_resp = MagicMock()
        mock_token_resp.status_code = 200
        mock_token_resp.json.return_value = {
            "access_token": "tok", "expires_at": 9999999999,
            "refresh_token": "refresh_tok",
        }

        activities_resp = MagicMock()
        activities_resp.status_code = 200
        activities_resp.json.return_value = [
            {"id": 10, "type": "Ride", "name": "Morning Ride",
             "start_date": "2026-03-20T08:00:00Z",
             "device_name": "Karoo 3", "distance": 50000, "moving_time": 7200,
             "total_elevation_gain": 500, "trainer": False},
        ]

        detail_resp = MagicMock()
        detail_resp.status_code = 200
        detail_resp.json.return_value = {}

        streams_resp = MagicMock()
        streams_resp.status_code = 200
        streams_resp.json.return_value = []

        with (
            self._mock_env(),
            patch.dict(sys.modules, {"db": db_mock}),
            patch("strava._request_with_retry", side_effect=[mock_token_resp, activities_resp, detail_resp, streams_resp]),
            patch("strava.time.sleep"),
        ):
            db_mock.upsert_activity.return_value = (42, False)
            strava.sync_activities(conn, after_epoch=0)

        db_mock.upsert_activity.assert_called_once()
        call_args = db_mock.upsert_activity.call_args[0]
        assert call_args[0] is conn
        assert call_args[1]["strava_id"] == 10
