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
_STRAVA_ROW = (10, 555, "karoo", 50000, 140, 200, None, 90, None, None)
_RWGPS_ROW = (11, None, "karoo", 50000, 140, 200, 777, None, None, None)
_DUAL_ROW = (12, 555, "karoo", 50000, 140, 200, 777, 90, None, None)


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
        existing = (10, 555, "watch", 0, 140, None, None, 90, None, None)  # HR-only, no power
        new = {"rwgps_id": 777, "avg_power": 200, "avg_hr": 142, "distance_m": 50000,
               "avg_cadence": 85, "calories": 900, "elevation_m": 400}
        merged = ingestor_db.merge_activity_data(existing, new)
        assert merged.get("_skip_insert") is None
        assert merged["strava_id"] == 555
        assert merged["rwgps_id"] == 777

    def test_merge_keeps_existing_rwgps_id_when_new_is_strava(self):
        existing = (11, None, "watch", 0, 140, None, 777, None, None, None)
        new = {"strava_id": 555, "avg_power": 200, "avg_hr": 142, "distance_m": 50000,
               "avg_cadence": 85, "calories": 900, "elevation_m": 400}
        merged = ingestor_db.merge_activity_data(existing, new)
        assert merged["strava_id"] == 555
        assert merged["rwgps_id"] == 777

    def test_merge_carries_suffer_score_from_existing(self):
        """Richer RWGPS copy replaces a Strava row via delete-and-reinsert —
        the Strava-only suffer_score must survive the merge."""
        existing = (10, 555, "watch", 0, 140, None, None, 90, None, None)
        new = {"rwgps_id": 777, "avg_power": 200, "avg_hr": 142, "distance_m": 50000,
               "avg_cadence": 85, "calories": 900, "elevation_m": 400}
        merged = ingestor_db.merge_activity_data(existing, new)
        assert merged["suffer_score"] == 90


class TestUpsertActivitySkipPathStamping:
    def test_weaker_rwgps_duplicate_stamps_rwgps_id_on_existing_row(self):
        """Existing Strava row richer -> incoming RWGPS copy is skipped, but the
        UPDATE must stamp rwgps_id onto the surviving row."""
        rich_existing = (10, 555, "karoo", 50000, 140, 200, None, 90, None, None)
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


class TestMergePathStreamHandling:
    """When the incoming copy is RICHER than the existing duplicate, the merge
    keeps the incoming record and deletes the old one — so the caller's freshly
    fetched (richer) streams must win. upsert_activity must return
    streams_preserved=False so the caller writes them, instead of silently
    keeping the deleted duplicate's (weaker/absent) streams."""

    def test_new_richer_merge_does_not_preserve_streams(self):
        """New copy richer -> streams_preserved=False so the caller writes its streams."""
        weak_existing = (10, 555, "watch", 0, 140, None, None, 90, None, None)  # HR-only, no power
        rich_new = {
            "rwgps_id": 777, "name": "Ride", "date": "2026-06-01T08:00:00Z",
            "duration_s": 7200, "distance_m": 50000, "avg_hr": 142, "avg_power": 210,
            "max_hr": 175, "max_power": 400, "avg_cadence": 85, "calories": 900,
            "elevation_m": 400, "suffer_score": None,
            "strava_type": "Ride", "trainer": False,
        }
        conn, cur = _make_conn()
        cur.fetchall.return_value = []  # deleted duplicate had no streams to restore
        with patch.object(ingestor_db, "find_duplicate", return_value=weak_existing), \
             patch.object(ingestor_db, "_do_insert", return_value=99), \
             patch.object(ingestor_db, "classify_activity", side_effect=lambda d: d):
            activity_id, streams_preserved = ingestor_db.upsert_activity(conn, rich_new)
        assert activity_id == 99
        assert streams_preserved is False

    def test_merge_restores_old_streams_as_fallback(self):
        """If the incoming richer copy brings no streams of its own, the old
        duplicate's streams are restored so the merged ride is not left empty.
        The restore is the fallback; streams_preserved stays False so a caller
        that DID fetch streams still overwrites them."""
        weak_existing = (10, 555, "watch", 0, 140, None, None, 90, None, None)
        rich_new = {
            "rwgps_id": 777, "name": "Ride", "date": "2026-06-01T08:00:00Z",
            "duration_s": 7200, "distance_m": 50000, "avg_hr": 142, "avg_power": 210,
            "max_hr": 175, "max_power": 400, "avg_cadence": 85, "calories": 900,
            "elevation_m": 400, "suffer_score": None,
            "strava_type": "Ride", "trainer": False,
        }
        conn, cur = _make_conn()
        # Old duplicate has one saved stream row; new activity has none (COUNT=0).
        cur.fetchall.return_value = [(0, 140, 200, 85, 25.0, 100.0, 38.7, -9.1)]
        cur.fetchone.return_value = (0,)
        with patch.object(ingestor_db, "find_duplicate", return_value=weak_existing), \
             patch.object(ingestor_db, "_do_insert", return_value=99), \
             patch.object(ingestor_db, "classify_activity", side_effect=lambda d: d), \
             patch.object(ingestor_db.psycopg2.extras, "execute_values") as mock_restore:
            activity_id, streams_preserved = ingestor_db.upsert_activity(conn, rich_new)
        assert activity_id == 99
        assert streams_preserved is False
        mock_restore.assert_called_once()  # old streams restored as fallback


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


class TestIntervalsIcuSchema:
    def test_create_schema_adds_intervals_icu_columns(self):
        conn = MagicMock()
        cur = conn.cursor.return_value.__enter__.return_value
        ingestor_db.create_schema(conn)
        sql = " ".join(str(c.args[0]) for c in cur.execute.call_args_list)
        assert "intervals_icu_id TEXT" in sql
        assert "intervals_icu_analyzed TIMESTAMPTZ" in sql

    def test_create_schema_adds_intervals_icu_partial_unique_index(self):
        conn = MagicMock()
        cur = conn.cursor.return_value.__enter__.return_value
        ingestor_db.create_schema(conn)
        sql = " ".join(str(c.args[0]) for c in cur.execute.call_args_list)
        assert "idx_activities_intervals_icu_id" in sql
        assert "WHERE intervals_icu_id IS NOT NULL" in sql


class TestGetIntervalsIcuAnalyzed:
    def test_returns_stored_timestamp_as_iso_string(self):
        conn = MagicMock()
        conn.cursor.return_value.__enter__.return_value.fetchone.return_value = (
            datetime(2026, 8, 24, 14, 26, 27, tzinfo=timezone.utc),)
        assert ingestor_db.get_intervals_icu_analyzed(conn, "i1").startswith("2026-08-24T14:26:27")

    def test_absent_activity_returns_the_sentinel_not_none(self):
        """Must be distinguishable from a stored NULL, or an unanalysed ride
        re-ingests on every poll."""
        conn = MagicMock()
        conn.cursor.return_value.__enter__.return_value.fetchone.return_value = None
        assert ingestor_db.get_intervals_icu_analyzed(conn, "i-new") is ingestor_db.ACTIVITY_ABSENT

    def test_returns_none_when_never_analyzed(self):
        conn = MagicMock()
        conn.cursor.return_value.__enter__.return_value.fetchone.return_value = (None,)
        assert ingestor_db.get_intervals_icu_analyzed(conn, "i1") is None


class TestIntervalsIcuDeletion:
    def test_single_source_row_is_deleted(self):
        conn = MagicMock()
        cur = conn.cursor.return_value.__enter__.return_value
        cur.fetchone.return_value = (7, None, None)
        assert ingestor_db.handle_intervals_icu_deletion(conn, "i1") == "deleted"

    def test_dual_source_row_is_unlinked_not_deleted(self):
        conn = MagicMock()
        cur = conn.cursor.return_value.__enter__.return_value
        cur.fetchone.return_value = (7, 555, None)
        assert ingestor_db.handle_intervals_icu_deletion(conn, "i1") == "unlinked"
        assert any("SET intervals_icu_id = NULL" in str(c.args[0])
                   for c in cur.execute.call_args_list)

    def test_unknown_id_reports_not_found(self):
        conn = MagicMock()
        conn.cursor.return_value.__enter__.return_value.fetchone.return_value = None
        assert ingestor_db.handle_intervals_icu_deletion(conn, "i-gone") == "not_found"


class TestIntervalsIcuPersistence:
    """Drives the REAL insert path rather than mocking upsert_activity.

    Every sync test mocks upsert_activity, so nothing there would notice if
    _do_insert failed to enumerate the new columns — intervals_icu_id would
    store NULL, get_intervals_icu_analyzed would never match (re-downloading
    every stream every poll), and reconcile's WHERE intervals_icu_id IS NOT
    NULL would silently return nothing. All with tests green.
    """

    def _icu_data(self):
        return {
            "intervals_icu_id": "i179264457",
            "intervals_icu_analyzed": "2026-08-24T14:26:27.824+00:00",
            "name": "Afternoon Ride", "date": "2026-08-24T13:09:25Z",
            "distance_m": 20611.3, "duration_s": 4223, "elevation_m": 371.0,
            "avg_hr": 146, "max_hr": 175, "avg_power": 120, "max_power": None,
            "avg_cadence": 60.7, "avg_speed_kmh": 17.57, "calories": 561,
            "suffer_score": None, "device": "karoo", "strava_type": "Ride",
            "trainer": False,
        }

    def _run_insert(self, data):
        conn = MagicMock()
        cur = conn.cursor.return_value.__enter__.return_value
        cur.fetchone.return_value = (42,)
        activity_id = ingestor_db._do_insert(conn, ingestor_db.classify_activity(data),
                                             datetime(2026, 8, 26, tzinfo=timezone.utc))
        sql, params = cur.execute.call_args[0]
        return activity_id, sql, params

    def test_insert_enumerates_the_intervals_icu_columns(self):
        _, sql, params = self._run_insert(self._icu_data())
        assert "intervals_icu_id" in sql
        assert "intervals_icu_analyzed" in sql
        assert params["intervals_icu_id"] == "i179264457"
        assert params["intervals_icu_analyzed"] == "2026-08-24T14:26:27.824+00:00"

    def test_icu_only_row_uses_the_intervals_icu_conflict_target(self):
        """With no strava_id or rwgps_id it must NOT fall through to the
        rwgps_id conflict target, which would be NULL."""
        _, sql, _ = self._run_insert(self._icu_data())
        assert "ON CONFLICT (intervals_icu_id) WHERE intervals_icu_id IS NOT NULL" in sql

    def test_strava_row_still_uses_the_strava_conflict_target(self):
        data = self._icu_data()
        data.pop("intervals_icu_id")
        data["strava_id"] = 555
        _, sql, _ = self._run_insert(data)
        assert "ON CONFLICT (strava_id)" in sql

    def test_rwgps_row_still_uses_the_rwgps_conflict_target(self):
        data = self._icu_data()
        data.pop("intervals_icu_id")
        data["rwgps_id"] = 999
        _, sql, _ = self._run_insert(data)
        assert "ON CONFLICT (rwgps_id) WHERE rwgps_id IS NOT NULL" in sql

    def test_icu_columns_are_coalesced_so_a_resync_cannot_null_them(self):
        _, sql, _ = self._run_insert(self._icu_data())
        assert "intervals_icu_id = COALESCE(EXCLUDED.intervals_icu_id" in sql
        assert "intervals_icu_analyzed = COALESCE(EXCLUDED.intervals_icu_analyzed" in sql

    def test_activity_without_any_source_id_does_not_crash(self):
        data = self._icu_data()
        data.pop("intervals_icu_id")
        _, sql, params = self._run_insert(data)
        assert params["intervals_icu_id"] is None


class TestDedupSkipPathStampsIcuColumns:
    """A weaker intervals.icu copy deduped onto a richer Strava/RWGPS row must
    stamp BOTH ids. Stamping intervals_icu_id without intervals_icu_analyzed
    leaves analyzed NULL forever, so get_intervals_icu_analyzed keeps returning
    None, the analyzed gate fires on every poll, streams are re-fetched and
    discarded, and the ride is counted as ingested every cycle — forcing a full
    fitness recalculation each poll."""

    def _skip_path_update(self):
        conn = MagicMock()
        cur = conn.cursor.return_value.__enter__.return_value
        # a richer existing row -> merge_activity_data returns _skip_insert
        cur.fetchone.return_value = (7, 555, "karoo", 30000.0, 150, 200, None, 80, None, None)
        data = {"intervals_icu_id": "i1",
                "intervals_icu_analyzed": "2026-08-24T14:26:27.824+00:00",
                "name": "Ride", "date": "2026-08-24T13:09:25Z",
                "duration_s": 3600, "distance_m": 30000.0}
        with patch.object(ingestor_db, "merge_activity_data",
                          return_value={"_skip_insert": True}):
            ingestor_db.upsert_activity(conn, data)
        for c in cur.execute.call_args_list:
            if "UPDATE activities SET" in str(c.args[0]) and "synced_at" in str(c.args[0]):
                return str(c.args[0]), c.args[1]
        raise AssertionError("skip-path UPDATE not found")

    def test_stamps_intervals_icu_id(self):
        sql, params = self._skip_path_update()
        assert "intervals_icu_id = COALESCE(intervals_icu_id" in sql
        assert params["intervals_icu_id"] == "i1"

    def test_stamps_intervals_icu_analyzed_too(self):
        sql, params = self._skip_path_update()
        assert "intervals_icu_analyzed = COALESCE(intervals_icu_analyzed" in sql
        assert params["intervals_icu_analyzed"] == "2026-08-24T14:26:27.824+00:00"


class TestTenColumnTuple:
    """find_duplicate's tuple widens 8 -> 10, carrying intervals_icu_id and
    intervals_icu_analyzed. It is unpacked positionally in three places, all in
    db.py, so every site changes together."""

    def test_find_duplicate_selects_both_intervals_icu_columns(self):
        conn = MagicMock()
        cur = conn.cursor.return_value.__enter__.return_value
        cur.fetchone.return_value = None
        ingestor_db.find_duplicate(conn, "2026-08-24T13:00:00Z", 3600, 30000)
        sql = str(cur.execute.call_args[0][0])
        assert "intervals_icu_id" in sql
        assert "intervals_icu_analyzed" in sql

    def test_same_source_guard_recognises_intervals_icu(self):
        dup = (7, None, "karoo", 30000.0, 140, 120, None, None, "i179264457", None)
        assert ingestor_db._is_same_source_activity(dup, {"intervals_icu_id": "i179264457"}) is True
        assert ingestor_db._is_same_source_activity(dup, {"intervals_icu_id": "iOTHER"}) is False

    def test_existing_intervals_icu_row_is_never_demoted_by_a_lower_source(self):
        """Precedence, not carry-forward, is what protects an intervals.icu row
        from a Strava copy: the row is not replaced at all, so it keeps both of
        its columns. The skip-path COALESCE UPDATE then stamps the Strava id."""
        ts = "2026-08-24T14:26:27.824000+00:00"
        existing = (7, None, "karoo", 30000.0, 140, 120, None, None, "i179264457", ts)
        merged = ingestor_db.merge_activity_data(existing, {
            "strava_id": 555, "avg_power": 200, "avg_hr": 150, "distance_m": 30000.0})
        assert merged.get("_skip_insert") is True

    def test_merge_carries_analyzed_forward_within_the_tier(self):
        """Where a merge CAN replace an intervals.icu row — a different
        intervals.icu activity at the same tier winning on richness — the
        delete-and-reinsert must not lose analyzed, or the sync gate fires on
        every poll for that ride forever."""
        ts = "2026-08-24T14:26:27.824000+00:00"
        existing = (7, None, "karoo", 30000.0, 140, None, None, None, "i1", ts)
        richer_icu = {"intervals_icu_id": "i2", "avg_power": 200, "avg_hr": 150,
                      "distance_m": 30000.0, "avg_cadence": 85, "calories": 900}
        merged = ingestor_db.merge_activity_data(existing, richer_icu)
        assert merged.get("_skip_insert") is None
        assert merged["intervals_icu_analyzed"] == ts


class TestSourcePrecedence:
    def test_intervals_icu_wins_over_a_richer_strava_row(self):
        """Primacy is explicit, not emergent: the Strava row scores higher on
        richness and must still lose the merge base."""
        rich_strava = (7, 555, "karoo", 30000.0, 150, 200, None, 80, None, None)
        icu_incoming = {"intervals_icu_id": "i1", "avg_power": None,
                        "avg_hr": None, "distance_m": 30000.0}
        merged = ingestor_db.merge_activity_data(rich_strava, icu_incoming)
        assert not merged.get("_skip_insert"), "intervals.icu must win the base"
        assert merged["intervals_icu_id"] == "i1"
        assert merged["avg_power"] == 200
        assert merged["avg_hr"] == 150

    def test_merged_row_keeps_its_tier_against_a_lower_source(self):
        """A row carrying intervals_icu_id + strava_id must outrank an incoming
        rwgps row — the max-priority rule, not bottom-tier."""
        merged_row = (7, 555, "karoo", 30000.0, 150, 200, None, 80, "i1", None)
        rwgps_incoming = {"rwgps_id": 999, "avg_power": 210, "avg_hr": 151,
                          "distance_m": 30000.0}
        merged = ingestor_db.merge_activity_data(merged_row, rwgps_incoming)
        assert merged.get("_skip_insert") is True, "lower-priority source must not win"

    def test_same_tier_falls_back_to_richness(self):
        existing = (7, None, "karoo", 30000.0, 150, 200, None, None, None, None)
        poor = {"distance_m": 30000.0}
        assert ingestor_db.merge_activity_data(existing, poor).get("_skip_insert") is True

    def test_source_tier_uses_max_not_first_match(self):
        assert ingestor_db._source_tier({"rwgps_id": 1, "intervals_icu_id": "i1"}) == 2
        assert ingestor_db._source_tier({"strava_id": 1}) == 1
        assert ingestor_db._source_tier({}) == 0

    def test_strava_and_rwgps_share_a_tier(self):
        """Ranking them against each other was never the requirement and loses
        elevation_m / distance_m, which the skip-path UPDATE does not carry."""
        assert (ingestor_db._source_tier({"strava_id": 1})
                == ingestor_db._source_tier({"rwgps_id": 1}))

    def test_same_tier_still_decided_by_richness(self):
        """Strava vs RWGPS behaviour is unchanged by the precedence work."""
        thin_strava = (10, 555, "watch", 0, 140, None, None, 90, None, None)
        rich_rwgps = {"rwgps_id": 777, "avg_power": 200, "avg_hr": 142,
                      "distance_m": 50000, "avg_cadence": 85, "calories": 900}
        merged = ingestor_db.merge_activity_data(thin_strava, rich_rwgps)
        assert merged.get("_skip_insert") is None, "richer copy still wins between equals"


class TestSkipPathCarriesElevationAndDistance:
    """The skip path used to carry sensor columns but not elevation_m or
    distance_m. That was the justification for same-tiering Strava and RWGPS —
    but it still bit whenever a richer lower-tier copy lost the base to an
    existing intervals.icu row. Carrying them closes it for every source."""

    def _skip_update(self, data):
        conn = MagicMock()
        cur = conn.cursor.return_value.__enter__.return_value
        cur.fetchone.return_value = (7, 555, "karoo", 30000.0, 150, 200, None, 80, None, None)
        with patch.object(ingestor_db, "merge_activity_data",
                          return_value={"_skip_insert": True}):
            ingestor_db.upsert_activity(conn, data)
        for c in cur.execute.call_args_list:
            if "UPDATE activities SET" in str(c.args[0]) and "synced_at" in str(c.args[0]):
                return str(c.args[0]), c.args[1]
        raise AssertionError("skip-path UPDATE not found")

    def _data(self, **over):
        d = {"intervals_icu_id": "i1", "name": "Ride", "date": "2026-08-24T13:09:25Z",
             "duration_s": 3600, "distance_m": 50000.0, "elevation_m": 400.0}
        d.update(over)
        return d

    def test_elevation_and_distance_are_carried(self):
        sql, params = self._skip_update(self._data())
        assert "elevation_m" in sql and "distance_m" in sql
        assert params["elevation_m"] == 400.0
        assert params["distance_m"] == 50000.0

    def test_zero_is_treated_as_absent_not_as_a_measurement(self):
        """RWGPS coerces an absent distance/elevation to 0, so a plain COALESCE
        would write 0 over a real stored value."""
        sql, _ = self._skip_update(self._data(distance_m=0, elevation_m=0))
        assert "NULLIF(%(elevation_m)s, 0)" in sql
        assert "NULLIF(%(distance_m)s, 0)" in sql
