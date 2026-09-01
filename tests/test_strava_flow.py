"""Tests for token refresh and sync flow in ingestor/strava.py."""

import sys
import os
from datetime import datetime, timezone
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

    def test_db_persist_failure_writes_file_fallback(self, tmp_path, monkeypatch):
        """When the DB write fails, the token really lands on disk.

        Writes to a real path rather than mocking Path.write_text: the whole
        point of the fallback is that the process can actually write the file,
        and a mocked write passes even when the directory is unwritable (which
        is exactly what /app/data was — root-owned, with the app user unable to
        write it).
        """
        fallback = tmp_path / ".strava_refresh_token"
        monkeypatch.setenv("VELOMATE_TOKEN_FALLBACK", str(fallback))

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
        ):
            strava.refresh_access_token("cid", "csecret", "old_refresh")

        # In-memory token should still be updated
        assert strava._current_refresh_token == "new_rotated_token"
        # The token is really on disk, not merely attempted
        assert fallback.read_text().strip() == "new_rotated_token"

    def test_unwritable_fallback_dir_is_reported_not_swallowed(self, tmp_path, monkeypatch, capsys):
        """An unwritable fallback path must say so — the failure mode that hid
        the /app/data permission bug was a bare except that printed nothing."""
        unwritable = tmp_path / "nonexistent-dir" / ".strava_refresh_token"
        monkeypatch.setenv("VELOMATE_TOKEN_FALLBACK", str(unwritable))

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "access_token": "access_abc",
            "expires_at": 9999999999,
            "refresh_token": "new_rotated_token",
        }
        db_mock = MagicMock()
        db_mock.get_connection.return_value = MagicMock()
        db_mock.set_sync_state.side_effect = Exception("DB write failed")

        with (
            patch("strava._request_with_retry", return_value=mock_resp),
            patch.dict(sys.modules, {"db": db_mock}),
        ):
            strava.refresh_access_token("cid", "csecret", "old_refresh")

        out = capsys.readouterr().out
        assert "could not write" in out.lower()
        assert str(unwritable) in out

    def test_fallback_token_file_is_owner_readable_only(self, tmp_path, monkeypatch):
        """The refresh token is a long-lived credential — the fallback file must
        not be world-readable. Path.write_text would create it 0644 under the
        usual umask."""
        fallback = tmp_path / ".strava_refresh_token"
        # Pre-create it world-readable to prove an existing file is tightened too.
        fallback.write_text("stale")
        fallback.chmod(0o644)
        monkeypatch.setenv("VELOMATE_TOKEN_FALLBACK", str(fallback))

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "access_token": "access_abc",
            "expires_at": 9999999999,
            "refresh_token": "new_rotated_token",
        }
        db_mock = MagicMock()
        db_mock.get_connection.return_value = MagicMock()
        db_mock.set_sync_state.side_effect = Exception("DB write failed")

        with (
            patch("strava._request_with_retry", return_value=mock_resp),
            patch.dict(sys.modules, {"db": db_mock}),
        ):
            strava.refresh_access_token("cid", "csecret", "old_refresh")

        assert fallback.read_text().strip() == "new_rotated_token"
        assert oct(fallback.stat().st_mode & 0o777) == "0o600"

    def test_token_never_lands_in_the_previously_world_readable_inode(self, tmp_path, monkeypatch):
        """The secret must never occupy an inode another process could hold open.

        chmod-after-open cannot revoke read access from a descriptor someone
        already has, so a 0644 file left behind by older code can't be made
        safe in place. The write goes to a fresh temp inode and is renamed in,
        which this pins by asserting the destination inode CHANGED.
        """
        fallback = tmp_path / ".strava_refresh_token"
        fallback.write_text("stale-old-token")
        fallback.chmod(0o644)
        old_inode = fallback.stat().st_ino
        monkeypatch.setenv("VELOMATE_TOKEN_FALLBACK", str(fallback))

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "access_token": "access_abc",
            "expires_at": 9999999999,
            "refresh_token": "new_rotated_token",
        }
        db_mock = MagicMock()
        db_mock.get_connection.return_value = MagicMock()
        db_mock.set_sync_state.side_effect = Exception("DB write failed")

        with (
            patch("strava._request_with_retry", return_value=mock_resp),
            patch.dict(sys.modules, {"db": db_mock}),
        ):
            strava.refresh_access_token("cid", "csecret", "old_refresh")

        assert fallback.read_text().strip() == "new_rotated_token"
        assert oct(fallback.stat().st_mode & 0o777) == "0o600"
        assert fallback.stat().st_ino != old_inode, (
            "token was written into the pre-existing world-readable inode")

    def test_failed_write_leaves_no_temp_file_behind(self, tmp_path, monkeypatch):
        """A mid-write failure must not litter the token directory with a
        partial secret."""
        fallback = tmp_path / ".strava_refresh_token"
        monkeypatch.setenv("VELOMATE_TOKEN_FALLBACK", str(fallback))

        def boom(fd, *args, **kwargs):
            # Real os.fdopen does NOT close the fd when it fails — ownership
            # transfers only on success, which is exactly why _write_secret
            # closes it by hand in that branch. Closing here would double-close
            # and mask the injected error with EBADF.
            raise OSError("disk full")

        monkeypatch.setattr(strava.os, "fdopen", boom)

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "access_token": "access_abc",
            "expires_at": 9999999999,
            "refresh_token": "new_rotated_token",
        }
        db_mock = MagicMock()
        db_mock.get_connection.return_value = MagicMock()
        db_mock.set_sync_state.side_effect = Exception("DB write failed")

        with (
            patch("strava._request_with_retry", return_value=mock_resp),
            patch.dict(sys.modules, {"db": db_mock}),
        ):
            strava.refresh_access_token("cid", "csecret", "old_refresh")

        leftovers = list(tmp_path.iterdir())
        assert leftovers == [], f"temp files left behind: {leftovers}"

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

    def test_fallback_file_round_trips_from_write_to_read(self, tmp_path, monkeypatch):
        """A token written by the DB-failure fallback is read back on restart.

        Covers the two halves together with a real file: writing it in
        refresh_access_token and picking it up again in _get_token. Mocking
        either half hides whether the path is actually usable.
        """
        fallback = tmp_path / ".strava_refresh_token"
        monkeypatch.setenv("VELOMATE_TOKEN_FALLBACK", str(fallback))

        write_resp = MagicMock()
        write_resp.status_code = 200
        write_resp.json.return_value = {
            "access_token": "access_abc",
            "expires_at": 9999999999,
            "refresh_token": "rotated_and_persisted",
        }
        failing_db = MagicMock()
        failing_db.get_connection.return_value = MagicMock()
        failing_db.set_sync_state.side_effect = Exception("DB write failed")

        with (
            patch("strava._request_with_retry", return_value=write_resp),
            patch.dict(sys.modules, {"db": failing_db}),
        ):
            strava.refresh_access_token("cid", "csecret", "old_refresh")

        # Simulate a restart: module state cleared, DB still has nothing stored.
        strava._access_token = None
        strava._token_expires_at = 0
        strava._current_refresh_token = None

        read_resp = MagicMock()
        read_resp.status_code = 200
        read_resp.json.return_value = {
            "access_token": "token_from_restart",
            "expires_at": 9999999999,
            "refresh_token": "rotated_and_persisted",
        }
        empty_db = MagicMock()
        empty_db.get_sync_state.return_value = None

        with (
            patch.dict(os.environ, {
                "STRAVA_CLIENT_ID": "cid",
                "STRAVA_CLIENT_SECRET": "csecret",
                "STRAVA_REFRESH_TOKEN": "stale_env_token",
            }),
            patch.dict(sys.modules, {"db": empty_db}),
            patch("strava._request_with_retry", return_value=read_resp) as req,
        ):
            token = strava._get_token()

        assert token == "token_from_restart"
        # The rotated token from disk must win over the stale env var
        assert req.call_args.kwargs["data"]["refresh_token"] == "rotated_and_persisted"

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


class TestSyncActivitiesCheckpoint(_StravaTestBase):
    """Incremental cursor checkpointing (audit #4): sync must persist
    strava_last_activity_epoch after each fully-processed activity, processing
    oldest-first, so a mid-pass failure keeps progress and a large/full-history
    backfill converges instead of restarting from after_epoch every cycle."""

    def _mock_env(self):
        return patch.dict(os.environ, {
            "STRAVA_CLIENT_ID": "cid",
            "STRAVA_CLIENT_SECRET": "csecret",
            "STRAVA_REFRESH_TOKEN": "refresh_tok",
        })

    def _sync_state(self, conn, key):
        # Refresh token absent (use env, no rotation); cursor starts at 0.
        return "0" if key == "strava_last_activity_epoch" else None

    def _ride(self, id, name, start):
        return {"id": id, "type": "Ride", "name": name, "start_date": start,
                "device_name": "", "distance": 30000, "moving_time": 3600,
                "total_elevation_gain": 200, "trainer": False}

    def _token_resp(self):
        r = MagicMock(status_code=200)
        r.json.return_value = {"access_token": "tok", "expires_at": 9999999999,
                               "refresh_token": "refresh_tok"}
        return r

    def _resp(self, body):
        r = MagicMock(status_code=200)
        r.json.return_value = body
        return r

    def test_checkpoints_progress_before_a_later_failure(self):
        """Activity A (older) is fully processed and its epoch checkpointed
        BEFORE activity B (newer) is fetched. When B's detail fetch fails, A's
        cursor is already persisted, so the next run resumes after A. Also proves
        oldest-first ordering: the API returns newest-first, but A is processed
        first.

        Since audit #1 the transient failure is caught rather than propagated —
        the pass stops and returns its partial count instead of raising, so the
        poll loop logs one line instead of a traceback. The checkpoint contract
        below is unchanged and is the part that matters."""
        conn = MagicMock()
        db_mock = MagicMock()
        db_mock.get_sync_state.side_effect = self._sync_state
        db_mock.upsert_activity.return_value = (1, False)

        a_start, b_start = "2026-01-01T00:00:00Z", "2026-06-01T00:00:00Z"
        a_epoch = int(datetime(2026, 1, 1, tzinfo=timezone.utc).timestamp())
        b_epoch = int(datetime(2026, 6, 1, tzinfo=timezone.utc).timestamp())

        # API returns newest-first (B before A) to prove we sort ascending here.
        activities = self._resp([self._ride(2, "B", b_start), self._ride(1, "A", a_start)])
        boom = RuntimeError("rate limited fetching B detail")

        with self._mock_env(), \
                patch.dict(sys.modules, {"db": db_mock}), \
                patch("strava._request_with_retry", side_effect=[
                    self._token_resp(), activities,
                    self._resp({}), self._resp([]),  # A detail + A streams
                    boom,                             # B detail → fails
                ]), \
                patch("strava.time.sleep"):
            ingested = strava.sync_activities(conn, after_epoch=0)

        # A completed before B failed, so the pass reports the partial progress
        assert ingested == 1

        calls = [c.args for c in db_mock.set_sync_state.call_args_list]
        assert (conn, "strava_last_activity_epoch", str(a_epoch)) in calls, \
            f"A's epoch must be checkpointed before B fails; got {calls}"
        assert (conn, "strava_last_activity_epoch", str(b_epoch)) not in calls, \
            "B's epoch must NOT be checkpointed — B never completed"

    def test_full_run_checkpoints_newest_epoch(self):
        """A clean run of two activities ends with the cursor at the newest."""
        conn = MagicMock()
        db_mock = MagicMock()
        db_mock.get_sync_state.side_effect = self._sync_state
        db_mock.upsert_activity.return_value = (1, False)

        a_start, b_start = "2026-01-01T00:00:00Z", "2026-06-01T00:00:00Z"
        b_epoch = int(datetime(2026, 6, 1, tzinfo=timezone.utc).timestamp())
        activities = self._resp([self._ride(2, "B", b_start), self._ride(1, "A", a_start)])

        with self._mock_env(), \
                patch.dict(sys.modules, {"db": db_mock}), \
                patch("strava._request_with_retry", side_effect=[
                    self._token_resp(), activities,
                    self._resp({}), self._resp([]),  # A detail + streams
                    self._resp({}), self._resp([]),  # B detail + streams
                ]), \
                patch("strava.time.sleep"):
            strava.sync_activities(conn, after_epoch=0)

        calls = [c.args for c in db_mock.set_sync_state.call_args_list]
        assert (conn, "strava_last_activity_epoch", str(b_epoch)) in calls


class TestSyncActivitiesErrorIsolation(_StravaTestBase):
    """Per-activity error isolation (audit #1).

    Activities are processed oldest-first with the cursor checkpointed only
    after each fully succeeds. Without isolation, one activity that fails the
    same way every time aborts the pass, leaves the cursor behind it, and is
    re-hit on every poll — permanently wedging all NEWER activities. RWGPS
    already guards this (rwgps.py: deterministic KeyError/TypeError is skipped,
    anything else stops the pass); these tests hold Strava to the same contract.
    """

    def _mock_env(self):
        return patch.dict(os.environ, {
            "STRAVA_CLIENT_ID": "cid",
            "STRAVA_CLIENT_SECRET": "csecret",
            "STRAVA_REFRESH_TOKEN": "refresh_tok",
        })

    def _sync_state(self, conn, key):
        return "0" if key == "strava_last_activity_epoch" else None

    def _ride(self, id, name, start):
        return {"id": id, "type": "Ride", "name": name, "start_date": start,
                "device_name": "", "distance": 30000, "moving_time": 3600,
                "total_elevation_gain": 200, "trainer": False}

    def _token_resp(self):
        r = MagicMock(status_code=200)
        r.json.return_value = {"access_token": "tok", "expires_at": 9999999999,
                               "refresh_token": "refresh_tok"}
        return r

    def _resp(self, body):
        r = MagicMock(status_code=200)
        r.json.return_value = body
        return r

    def _epochs(self):
        return (
            int(datetime(2026, 1, 1, tzinfo=timezone.utc).timestamp()),
            int(datetime(2026, 2, 1, tzinfo=timezone.utc).timestamp()),
            int(datetime(2026, 3, 1, tzinfo=timezone.utc).timestamp()),
        )

    def test_deterministic_failure_is_skipped_so_newer_activities_still_ingest(self):
        """A ride that fails the same way every time must not block newer rides.

        This is the wedge: B raises a TypeError on every attempt. Without
        isolation the pass dies at B, the cursor stays at A, and C is never
        reached — on this poll or any future one.
        """
        conn = MagicMock()
        db_mock = MagicMock()
        db_mock.get_sync_state.side_effect = self._sync_state
        a_epoch, b_epoch, c_epoch = self._epochs()

        # B is unprocessable; A and C are fine.
        def _upsert(_conn, data):
            if data["strava_id"] == 2:
                raise TypeError("unsupported operand — malformed activity payload")
            return (data["strava_id"], False)

        db_mock.upsert_activity.side_effect = _upsert

        activities = self._resp([
            self._ride(1, "A", "2026-01-01T00:00:00Z"),
            self._ride(2, "B", "2026-02-01T00:00:00Z"),
            self._ride(3, "C", "2026-03-01T00:00:00Z"),
        ])

        with self._mock_env(), \
                patch.dict(sys.modules, {"db": db_mock}), \
                patch("strava._request_with_retry", side_effect=[
                    self._token_resp(), activities,
                    self._resp({}), self._resp([]),   # A detail + streams
                    self._resp({}),                    # B detail (upsert then raises)
                    self._resp({}), self._resp([]),   # C detail + streams
                ]), \
                patch("strava.time.sleep"):
            ingested = strava.sync_activities(conn, after_epoch=0)

        # C must have been reached and stored despite B being broken
        stored = [c.args[1]["strava_id"] for c in db_mock.upsert_activity.call_args_list]
        assert 3 in stored, f"C must still ingest after B fails; upserts were {stored}"
        assert ingested == 2, "A and C ingested, B skipped"

        # The cursor must advance PAST B, otherwise B is re-hit forever
        calls = [c.args for c in db_mock.set_sync_state.call_args_list]
        assert (conn, "strava_last_activity_epoch", str(c_epoch)) in calls, \
            f"cursor must reach C's epoch; got {calls}"

    def test_transient_failure_stops_the_pass_without_advancing_past_it(self):
        """A network/5xx failure must NOT be skipped — the ride is real and
        retryable, so the pass stops and the cursor stays put for a retry."""
        conn = MagicMock()
        db_mock = MagicMock()
        db_mock.get_sync_state.side_effect = self._sync_state
        db_mock.upsert_activity.return_value = (1, False)
        a_epoch, b_epoch, c_epoch = self._epochs()

        activities = self._resp([
            self._ride(1, "A", "2026-01-01T00:00:00Z"),
            self._ride(2, "B", "2026-02-01T00:00:00Z"),
            self._ride(3, "C", "2026-03-01T00:00:00Z"),
        ])

        with self._mock_env(), \
                patch.dict(sys.modules, {"db": db_mock}), \
                patch("strava._request_with_retry", side_effect=[
                    self._token_resp(), activities,
                    self._resp({}), self._resp([]),          # A detail + streams
                    ConnectionError("connection reset by peer"),  # B detail → transient
                ]), \
                patch("strava.time.sleep"):
            strava.sync_activities(conn, after_epoch=0)

        calls = [c.args for c in db_mock.set_sync_state.call_args_list]
        assert (conn, "strava_last_activity_epoch", str(a_epoch)) in calls, \
            "A completed, so its epoch is checkpointed"
        assert (conn, "strava_last_activity_epoch", str(b_epoch)) not in calls, \
            "B must NOT be checkpointed — a transient failure has to be retried"
        assert (conn, "strava_last_activity_epoch", str(c_epoch)) not in calls, \
            "C must not be processed after the pass stops"
