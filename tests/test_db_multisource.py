"""Tests for multi-source (Strava + RWGPS) support in ingestor/db.py."""

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# Mock psycopg2 before importing ingestor modules (same pattern as test_strava_flow.py)
sys.modules.setdefault("psycopg2", MagicMock())
sys.modules.setdefault("psycopg2.extras", MagicMock())

_ingestor_dir = Path(__file__).resolve().parent.parent / "ingestor"
if str(_ingestor_dir) not in sys.path:
    sys.path.insert(0, str(_ingestor_dir))

import db as ingestor_db


def _make_conn(fetchone_val=None, fetchall_val=None):
    """Build a mock psycopg2 connection whose cursor records executed SQL."""
    conn = MagicMock()
    cur = MagicMock()
    conn.cursor.return_value.__enter__ = MagicMock(return_value=cur)
    conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
    cur.fetchone.return_value = fetchone_val
    cur.fetchall.return_value = fetchall_val or []
    return conn, cur


from datetime import datetime, timezone


def _activity(**overrides):
    base = {
        "name": "Ride", "date": "2026-06-01T08:00:00Z", "distance_m": 50000,
        "duration_s": 7200, "elevation_m": 500, "avg_hr": 140, "max_hr": 170,
        "avg_power": 200, "max_power": 400, "avg_cadence": 85,
        "avg_speed_kmh": 25.0, "calories": 1200, "suffer_score": None,
        "device": "karoo", "is_indoor": False, "sport_type": "cycling_outdoor",
    }
    base.update(overrides)
    return base


class TestDoInsertConflictTarget:
    def setup_method(self):
        self.now = datetime.now(timezone.utc)

    def test_strava_activity_conflicts_on_strava_id(self):
        conn, cur = _make_conn(fetchone_val=(1,))
        ingestor_db._do_insert(conn, _activity(strava_id=123), self.now)
        sql = cur.execute.call_args[0][0]
        assert "ON CONFLICT (strava_id)" in sql

    def test_strava_conflict_updates_suffer_score(self):
        conn, cur = _make_conn(fetchone_val=(1,))
        ingestor_db._do_insert(conn, _activity(strava_id=123, suffer_score=90), self.now)
        sql = cur.execute.call_args[0][0]
        assert "suffer_score = EXCLUDED.suffer_score" in sql

    def test_rwgps_activity_conflicts_on_rwgps_id(self):
        conn, cur = _make_conn(fetchone_val=(1,))
        ingestor_db._do_insert(conn, _activity(rwgps_id=987), self.now)
        sql = cur.execute.call_args[0][0]
        assert "ON CONFLICT (rwgps_id) WHERE rwgps_id IS NOT NULL" in sql

    def test_rwgps_conflict_does_not_touch_suffer_score(self):
        conn, cur = _make_conn(fetchone_val=(1,))
        ingestor_db._do_insert(conn, _activity(rwgps_id=987), self.now)
        sql = cur.execute.call_args[0][0]
        assert "suffer_score = EXCLUDED.suffer_score" not in sql

    def test_sensor_columns_null_protected_on_rwgps_conflict(self):
        """A weaker copy's 'updated' event must not NULL-clobber merged sensor data."""
        conn, cur = _make_conn(fetchone_val=(1,))
        ingestor_db._do_insert(conn, _activity(rwgps_id=987), self.now)
        sql = cur.execute.call_args[0][0]
        for col in ("avg_hr", "max_hr", "avg_power", "max_power", "avg_cadence", "calories"):
            assert f"{col} = COALESCE(EXCLUDED.{col}, activities.{col})" in sql

    def test_sensor_columns_null_protected_on_strava_conflict(self):
        conn, cur = _make_conn(fetchone_val=(1,))
        ingestor_db._do_insert(conn, _activity(strava_id=123), self.now)
        sql = cur.execute.call_args[0][0]
        for col in ("avg_hr", "max_hr", "avg_power", "max_power", "avg_cadence", "calories"):
            assert f"{col} = COALESCE(EXCLUDED.{col}, activities.{col})" in sql

    def test_missing_source_keys_default_to_none(self):
        """Strava data has no rwgps_id key and vice versa — params must not KeyError."""
        conn, cur = _make_conn(fetchone_val=(1,))
        ingestor_db._do_insert(conn, _activity(strava_id=123), self.now)
        params = cur.execute.call_args[0][1]
        assert params["rwgps_id"] is None

    def test_returns_activity_id(self):
        conn, cur = _make_conn(fetchone_val=(42,))
        assert ingestor_db._do_insert(conn, _activity(rwgps_id=987), self.now) == 42

    def test_distance_and_elevation_protected_against_zero_clobber(self):
        """_parse_trip coerces absent distance/elevation to 0, so a plain
        EXCLUDED overwrite on an 'updated' event would zero a richer source's
        value. NULLIF(.,0)+COALESCE keeps the existing value when incoming is 0."""
        for col in ("distance_m", "elevation_m"):
            conn, cur = _make_conn(fetchone_val=(1,))
            ingestor_db._do_insert(conn, _activity(rwgps_id=987), self.now)
            sql = cur.execute.call_args[0][0]
            assert f"{col} = COALESCE(NULLIF(EXCLUDED.{col}, 0), activities.{col})" in sql


class TestSchemaRwgpsColumn:
    def test_create_schema_adds_rwgps_id_column(self):
        conn, cur = _make_conn()
        ingestor_db.create_schema(conn)
        sql = cur.execute.call_args[0][0]
        assert "ADD COLUMN IF NOT EXISTS rwgps_id BIGINT" in sql

    def test_create_schema_adds_partial_unique_index(self):
        conn, cur = _make_conn()
        ingestor_db.create_schema(conn)
        sql = cur.execute.call_args[0][0]
        assert "idx_activities_rwgps_id" in sql
        assert "WHERE rwgps_id IS NOT NULL" in sql


# (id, strava_id, device, distance_m, avg_hr, avg_power, rwgps_id, suffer_score)
_STRAVA_ROW = (10, 555, "karoo", 50000, 140, 200, None, 90)
_RWGPS_ROW = (11, None, "karoo", 50000, 140, 200, 777, None)
_DUAL_ROW = (12, 555, "karoo", 50000, 140, 200, 777, 90)


class TestIsSameSourceActivity:
    def test_strava_incoming_same_id(self):
        assert ingestor_db._is_same_source_activity(_STRAVA_ROW, {"strava_id": 555}) is True

    def test_strava_incoming_different_id(self):
        assert ingestor_db._is_same_source_activity(_STRAVA_ROW, {"strava_id": 999}) is False

    def test_rwgps_incoming_same_id_bypasses_dedup(self):
        """A re-synced RWGPS trip ('updated' action) must NOT re-enter dedup."""
        assert ingestor_db._is_same_source_activity(_RWGPS_ROW, {"rwgps_id": 777}) is True

    def test_rwgps_incoming_different_id(self):
        assert ingestor_db._is_same_source_activity(_RWGPS_ROW, {"rwgps_id": 888}) is False

    def test_rwgps_incoming_vs_strava_only_row(self):
        """RWGPS copy of a Strava ride IS a cross-source duplicate -> dedup."""
        assert ingestor_db._is_same_source_activity(_STRAVA_ROW, {"rwgps_id": 777}) is False

    def test_rwgps_incoming_vs_dual_row(self):
        assert ingestor_db._is_same_source_activity(_DUAL_ROW, {"rwgps_id": 777}) is True


class TestMergeCarriesSourceIds:
    def test_merge_keeps_existing_strava_id_when_new_is_rwgps(self):
        """New RWGPS data richer than existing Strava row -> merged dict carries both IDs."""
        existing = (10, 555, "watch", 0, 140, None, None, 90)  # HR-only, no power
        new = {"rwgps_id": 777, "avg_power": 200, "avg_hr": 142, "distance_m": 50000,
               "avg_cadence": 85, "calories": 900, "elevation_m": 400}
        merged = ingestor_db.merge_activity_data(existing, new)
        assert merged.get("_skip_insert") is None
        assert merged["strava_id"] == 555
        assert merged["rwgps_id"] == 777

    def test_merge_keeps_existing_rwgps_id_when_new_is_strava(self):
        existing = (11, None, "watch", 0, 140, None, 777, None)
        new = {"strava_id": 555, "avg_power": 200, "avg_hr": 142, "distance_m": 50000,
               "avg_cadence": 85, "calories": 900, "elevation_m": 400}
        merged = ingestor_db.merge_activity_data(existing, new)
        assert merged["strava_id"] == 555
        assert merged["rwgps_id"] == 777

    def test_merge_carries_suffer_score_from_existing(self):
        """Richer RWGPS copy replaces a Strava row via delete-and-reinsert —
        the Strava-only suffer_score must survive the merge."""
        existing = (10, 555, "watch", 0, 140, None, None, 90)
        new = {"rwgps_id": 777, "avg_power": 200, "avg_hr": 142, "distance_m": 50000,
               "avg_cadence": 85, "calories": 900, "elevation_m": 400}
        merged = ingestor_db.merge_activity_data(existing, new)
        assert merged["suffer_score"] == 90


class TestUpsertActivitySkipPathStamping:
    def test_weaker_rwgps_duplicate_stamps_rwgps_id_on_existing_row(self):
        """Existing Strava row richer -> incoming RWGPS copy is skipped, but the
        UPDATE must stamp rwgps_id onto the surviving row."""
        rich_existing = (10, 555, "karoo", 50000, 140, 200, None, 90)
        weak_new = {"rwgps_id": 777, "name": "Ride", "date": "2026-06-01T08:00:00Z",
                    "duration_s": 7200, "distance_m": 50000, "avg_hr": 140,
                    "strava_type": "Ride", "trainer": False}
        conn, cur = _make_conn()
        with patch.object(ingestor_db, "find_duplicate", return_value=rich_existing):
            activity_id, streams_preserved = ingestor_db.upsert_activity(conn, weak_new)
        assert activity_id == 10
        assert streams_preserved is True
        sql = cur.execute.call_args[0][0]
        params = cur.execute.call_args[0][1]
        assert "rwgps_id" in sql and "COALESCE(rwgps_id" in sql
        assert params["rwgps_id"] == 777


class TestHandleRwgpsDeletion:
    def test_rwgps_only_row_is_deleted(self):
        conn, cur = _make_conn(fetchone_val=(11, None))  # (id, strava_id)
        assert ingestor_db.handle_rwgps_deletion(conn, 777) == "deleted"
        delete_sql = cur.execute.call_args[0][0]
        assert "DELETE FROM activities" in delete_sql

    def test_dual_source_row_is_unlinked(self):
        conn, cur = _make_conn(fetchone_val=(12, 555))
        assert ingestor_db.handle_rwgps_deletion(conn, 777) == "unlinked"
        update_sql = cur.execute.call_args[0][0]
        assert "SET rwgps_id = NULL" in update_sql

    def test_unknown_id_not_found(self):
        conn, cur = _make_conn(fetchone_val=None)
        assert ingestor_db.handle_rwgps_deletion(conn, 999) == "not_found"
        assert cur.execute.call_count == 1  # only the SELECT
