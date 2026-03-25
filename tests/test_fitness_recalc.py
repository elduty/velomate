"""Tests for recalculate_fitness flow in ingestor/fitness.py."""

import sys
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch, call, PropertyMock

import pytest

# Mock psycopg2 before importing ingestor modules
sys.modules.setdefault("psycopg2", MagicMock())
sys.modules.setdefault("psycopg2.extras", MagicMock())

_ingestor_dir = Path(__file__).resolve().parent.parent / "ingestor"
if str(_ingestor_dir) not in sys.path:
    sys.path.insert(0, str(_ingestor_dir))

# Ensure a mock 'db' module is available for the local import inside recalculate_fitness
_db_mock = sys.modules.get("db") or MagicMock()
sys.modules["db"] = _db_mock

from fitness import recalculate_fitness, compute_ef, METRICS_VERSION

# Wrap recalculate_fitness to patch get_sync_state (skip NP/EF reset)
_original_recalc = recalculate_fitness

def recalculate_fitness_patched(conn):
    with patch("db.get_sync_state", return_value=METRICS_VERSION), \
         patch("db.set_sync_state"):
        return _original_recalc(conn)

# Override for all tests in this file
recalculate_fitness = recalculate_fitness_patched


def _make_conn(activity_rows, power_activity_rows=None, tss_rows=None,
               backfill_count=0, trimp_activity_ids=None, configured_ftp=False):
    """Build a mock connection that returns prescribed rows for each query.

    activity_rows: [(id, duration_s, avg_hr, avg_power, np, ride_ftp), ...]
    power_activity_rows: [(id, avg_hr, avg_power), ...] -- for NP query
    tss_rows: [(date, tss, distance_m, elevation_m), ...] -- for final readback
    backfill_count: number of rides needing FTP backfill (0 = skip backfill)
    trimp_activity_ids: [id, ...] -- activities needing TRIMP computation
    configured_ftp: if True, backfill uses single stamp (1 cursor) instead of
                    stream-based backfill + stamp (2 cursors)

    Cursor sequence in recalculate_fitness (called via patched wrapper):
      0: estimate_threshold_hr
      1: estimate_ftp (rolling 20-min) -- skipped when configured_ftp=True
      2: SELECT power activities for NP/EF/VI
      3..3+2*N-1: per-power-activity (NP rolling query + update with VI)
      3+2*N: COUNT rides needing FTP backfill
      When backfill_count > 0 and configured_ftp: 1 stamp cursor
      When backfill_count > 0 and auto-estimate: 2 cursors (backfill + stamp)
      When backfill_count == 0: no backfill/stamp cursors
      Then: TSS+IF select, batch, TRIMP select, per-TRIMP cursors, readback
    """
    conn = MagicMock()
    conn.autocommit = True

    trimp_ids = trimp_activity_ids or []
    n_power = len(power_activity_rows) if power_activity_rows else 0
    n_trimp = len(trimp_ids)
    if backfill_count > 0:
        b = 1 if configured_ftp else 2  # stamp-only vs backfill+stamp
    else:
        b = 0
    ftp_cursor_offset = 1 if configured_ftp else 0  # estimate_ftp skipped when configured
    backfill_count_idx = 3 + 2 * n_power - ftp_cursor_offset
    tss_select_idx = backfill_count_idx + 1 + b
    tss_batch_idx = tss_select_idx + 1
    trimp_select_idx = tss_batch_idx + 1
    trimp_start_idx = trimp_select_idx + 1
    readback_idx = trimp_start_idx + 2 * n_trimp

    cursor_call_count = [0]
    captured_cursors = []  # stores (idx, cur) for post-hoc inspection

    def make_cursor():
        ctx = MagicMock()
        cur = MagicMock()
        ctx.__enter__ = MagicMock(return_value=cur)
        ctx.__exit__ = MagicMock(return_value=False)

        idx = cursor_call_count[0]
        cursor_call_count[0] += 1
        captured_cursors.append((idx, cur))

        np_select_idx = 2 - ftp_cursor_offset

        if idx == 0:
            cur.fetchone.return_value = (170,)
        elif idx == 1 and not configured_ftp:
            cur.fetchone.return_value = (250,)
        elif idx == np_select_idx:
            cur.fetchall.return_value = power_activity_rows or []
        elif np_select_idx < idx < backfill_count_idx:
            # NP: per-activity power stream fetch (fetchall) + update (alternate)
            offset = idx - np_select_idx - 1
            if offset % 2 == 0:
                # SELECT power samples — return 60 samples at 200W
                cur.fetchall.return_value = [(200, 200)] * 60
            # else: UPDATE np/ef/vi (no special setup)
        elif idx == backfill_count_idx:
            cur.fetchone.return_value = (backfill_count,)
        elif backfill_count_idx < idx < tss_select_idx:
            # Backfill/stamp cursors (only when backfill_count > 0)
            cur.rowcount = backfill_count
        elif idx == tss_select_idx:
            cur.fetchall.return_value = activity_rows
        elif idx == tss_batch_idx:
            pass  # execute_batch
        elif idx == trimp_select_idx:
            cur.fetchall.return_value = [(aid,) for aid in trimp_ids]
        elif trimp_start_idx <= idx < readback_idx:
            # TRIMP cursors: alternating SELECT hr / UPDATE trimp
            offset = idx - trimp_start_idx
            if offset % 2 == 0:
                cur.fetchall.return_value = [(140,)] * 120  # mock HR samples
            # else: UPDATE trimp (no special setup needed)
        elif idx == readback_idx:
            cur.fetchall.return_value = tss_rows or []
        # else: upsert_athlete_stats calls

        return ctx

    conn.cursor.side_effect = make_cursor
    conn._cursors = captured_cursors  # expose for test assertions
    return conn


# ---------------------------------------------------------------------------
# CTL / ATL / TSB calculation
# ---------------------------------------------------------------------------

class TestCTLATLCalculation:
    """Verify EMA-based CTL/ATL/TSB with known TSS values."""

    def test_single_activity_day(self):
        """One activity on day 1 should produce non-zero CTL/ATL."""
        today = date.today()
        activity_rows = [(1, 3600, None, 200, None, 200)]
        tss_rows = [(today, 80.0, 50000, 500)]

        conn = _make_conn(activity_rows, tss_rows=tss_rows)
        upsert_mock = MagicMock()

        with patch.dict(sys.modules, {"db": MagicMock(upsert_athlete_stats=upsert_mock)}):
            recalculate_fitness(conn)

        # At least one call to upsert_athlete_stats
        assert upsert_mock.call_count >= 1
        # First call should have non-zero CTL and ATL
        stats = upsert_mock.call_args_list[0][0][2]
        assert stats["ctl"] > 0
        assert stats["atl"] > 0

    def test_three_activities_over_seven_days(self):
        """Three activities over 7 days: verify CTL < ATL (short ramp-up)."""
        base = date.today() - timedelta(days=6)
        activity_rows = [
            (1, 3600, None, 200, None, 200),
            (2, 5400, None, 180, None, 200),
            (3, 3600, None, 220, None, 200),
        ]
        # Day 0: TSS=80, Day 3: TSS=70, Day 6: TSS=90
        tss_rows = [
            (base, 80.0, 40000, 300),
            (base + timedelta(days=3), 70.0, 35000, 250),
            (base + timedelta(days=6), 90.0, 50000, 500),
        ]

        conn = _make_conn(activity_rows, tss_rows=tss_rows)

        upsert_calls = []
        upsert_mock = MagicMock(side_effect=lambda c, d, s: upsert_calls.append((d, s)))

        with patch.dict(sys.modules, {"db": MagicMock(upsert_athlete_stats=upsert_mock)}):
            recalculate_fitness(conn)

        # Should have 7 days of stats
        assert len(upsert_calls) >= 7
        # CTL uses 42-day window, ATL uses 7-day window
        # After 7 days with activities, ATL should be larger than CTL (shorter window responds faster)
        final_stats = upsert_calls[-1][1]
        assert final_stats["atl"] > final_stats["ctl"]

    def test_tsb_equals_ctl_minus_atl(self):
        """TSB should always equal CTL - ATL."""
        today = date.today()
        activity_rows = [(1, 3600, None, 200, None, 200)]
        tss_rows = [(today, 100.0, 50000, 500)]

        conn = _make_conn(activity_rows, tss_rows=tss_rows)

        upsert_calls = []
        upsert_mock = MagicMock(side_effect=lambda c, d, s: upsert_calls.append((d, s)))

        with patch.dict(sys.modules, {"db": MagicMock(upsert_athlete_stats=upsert_mock)}):
            recalculate_fitness(conn)

        for day_date, stats in upsert_calls:
            assert stats["tsb"] == pytest.approx(stats["ctl"] - stats["atl"], abs=0.01)


# ---------------------------------------------------------------------------
# Rest day decay
# ---------------------------------------------------------------------------

class TestRestDayDecay:
    """Days with no activity should still decay CTL/ATL."""

    def test_ctl_atl_decay_on_rest_day(self):
        """After an activity, a rest day should show lower ATL."""
        base = date.today() - timedelta(days=2)
        activity_rows = [(1, 3600, None, 200, None, 200)]
        # Activity on day 0 only, days 1-2 are rest
        tss_rows = [(base, 100.0, 50000, 500)]

        conn = _make_conn(activity_rows, tss_rows=tss_rows)

        upsert_calls = []
        upsert_mock = MagicMock(side_effect=lambda c, d, s: upsert_calls.append((d, s)))

        with patch.dict(sys.modules, {"db": MagicMock(upsert_athlete_stats=upsert_mock)}):
            recalculate_fitness(conn)

        # Day 0 has activity, day 1+ are rest
        day0_stats = upsert_calls[0][1]
        day1_stats = upsert_calls[1][1]
        day2_stats = upsert_calls[2][1]

        # ATL should decay each rest day
        assert day1_stats["atl"] < day0_stats["atl"]
        assert day2_stats["atl"] < day1_stats["atl"]

        # CTL should also decay (slower)
        assert day1_stats["ctl"] < day0_stats["ctl"]
        assert day2_stats["ctl"] < day1_stats["ctl"]

    def test_rest_day_tsb_rises(self):
        """TSB should rise on rest days (ATL drops faster than CTL)."""
        base = date.today() - timedelta(days=3)
        activity_rows = [(1, 3600, None, 200, None, 200)]
        tss_rows = [(base, 100.0, 50000, 500)]

        conn = _make_conn(activity_rows, tss_rows=tss_rows)

        upsert_calls = []
        upsert_mock = MagicMock(side_effect=lambda c, d, s: upsert_calls.append((d, s)))

        with patch.dict(sys.modules, {"db": MagicMock(upsert_athlete_stats=upsert_mock)}):
            recalculate_fitness(conn)

        # TSB should increase on rest days because ATL (7-day) decays faster than CTL (42-day)
        day1_tsb = upsert_calls[1][1]["tsb"]
        day2_tsb = upsert_calls[2][1]["tsb"]
        assert day2_tsb > day1_tsb


# ---------------------------------------------------------------------------
# NP skip guard
# ---------------------------------------------------------------------------

class TestNPSkipGuard:
    """Activities with NP already computed (np IS NOT NULL) should be skipped."""

    def test_no_power_activities_skips_np_computation(self):
        """When NP query returns empty list, no NP updates are issued."""
        today = date.today()
        activity_rows = [(1, 3600, 150, None, None, 200)]
        tss_rows = [(today, 50.0, 40000, 300)]

        conn = _make_conn(activity_rows, power_activity_rows=[], tss_rows=tss_rows)

        with patch.dict(sys.modules, {"db": MagicMock()}):
            recalculate_fitness(conn)

        # The NP query returns empty, so no individual NP update cursors opened
        # Verify conn.commit was called (final commit)
        conn.commit.assert_called()

    def test_power_activity_triggers_np_calculation(self):
        """Activities with power streams and np IS NULL should get NP computed."""
        today = date.today()
        activity_rows = [(1, 3600, 150, 200, None, 200)]
        # This activity appears in NP query (np IS NULL, has power streams)
        power_activity_rows = [(1, 150, 200)]
        tss_rows = [(today, 80.0, 50000, 500)]

        conn = _make_conn(activity_rows, power_activity_rows=power_activity_rows, tss_rows=tss_rows)

        with patch.dict(sys.modules, {"db": MagicMock()}):
            recalculate_fitness(conn)

        conn.commit.assert_called()


# ---------------------------------------------------------------------------
# compute_ef integration
# ---------------------------------------------------------------------------

class TestComputeEFInFlow:
    """Verify compute_ef is called correctly during NP computation."""

    def test_compute_ef_called_with_np_and_hr(self):
        """When NP is computed, EF = NP / avg_hr."""
        result = compute_ef(220.0, 150)
        assert result == pytest.approx(1.47, abs=0.01)

    def test_compute_ef_none_when_no_hr(self):
        """No avg_hr -> EF is None."""
        result = compute_ef(220.0, None)
        assert result is None

    def test_compute_ef_none_when_zero_hr(self):
        result = compute_ef(220.0, 0)
        assert result is None


# ---------------------------------------------------------------------------
# Batch TSS update uses execute_batch
# ---------------------------------------------------------------------------

class TestBatchTSSUpdate:
    """Verify TSS updates use psycopg2.extras.execute_batch."""

    def test_execute_batch_called_with_tss_updates(self):
        """execute_batch should be called for TSS updates."""
        today = date.today()
        activity_rows = [(1, 3600, 150, None, None, 200), (2, 5400, 160, None, None, 200)]
        tss_rows = [(today, 50.0, 40000, 300)]

        conn = _make_conn(activity_rows, tss_rows=tss_rows)

        import psycopg2.extras as extras_mock

        with patch.dict(sys.modules, {"db": MagicMock()}):
            recalculate_fitness(conn)

        # execute_batch should have been called at least once
        assert extras_mock.execute_batch.called

    def test_tss_update_contains_all_activities(self):
        """The batch update should include TSS for each activity."""
        today = date.today()
        activity_rows = [
            (1, 3600, 150, None, None, 200),  # HR-based TSS
            (2, 3600, None, 200, None, 200),  # Power-based TSS
            (3, 3600, None, None, None, 200),  # No HR or power -> TSS=0
        ]
        tss_rows = [(today, 50.0, 40000, 300)]

        conn = _make_conn(activity_rows, tss_rows=tss_rows)

        import psycopg2.extras as extras_mock

        with patch.dict(sys.modules, {"db": MagicMock()}):
            recalculate_fitness(conn)

        # Check execute_batch was called with 3 updates (tss, if_val, id)
        batch_call = extras_mock.execute_batch.call_args
        tss_data = batch_call[0][2]  # third positional arg is the data list
        assert len(tss_data) == 3
        # Third activity (no HR/power) should have TSS=0, IF=None
        assert tss_data[2][0] == 0
        assert tss_data[2][1] is None

    def test_ride_ftp_none_falls_back_to_global_ftp(self):
        """Activity with ride_ftp=None should use global FTP (250) for TSS.
        Defensive path: in production, backfill+stamp would set ride_ftp before
        TSS calculation, but this guards against gaps or future code changes."""
        today = date.today()
        # ride_ftp=None triggers fallback; ride_ftp=200 uses per-ride value
        activity_rows = [
            (1, 3600, None, 200, None, None),   # ride_ftp=None -> global FTP=250
            (2, 3600, None, 200, None, 200),     # ride_ftp=200
        ]
        tss_rows = [(today, 50.0, 40000, 300)]

        # backfill_count=0: in production, backfill+stamp would set ride_ftp
        # before TSS. This intentionally skips backfill to test the defensive fallback.
        conn = _make_conn(activity_rows, tss_rows=tss_rows)

        import psycopg2.extras as extras_mock

        with patch.dict(sys.modules, {"db": MagicMock()}):
            recalculate_fitness(conn)

        batch_call = extras_mock.execute_batch.call_args
        tss_data = batch_call[0][2]  # (tss, if_val, id) tuples
        # Activity 1 (global FTP=250): TSS = (3600 * 200 * 0.8) / (250 * 3600) * 100 = 64.0
        # Activity 2 (ride FTP=200):   TSS = (3600 * 200 * 1.0) / (200 * 3600) * 100 = 100.0
        assert tss_data[0][0] == 64.0
        assert tss_data[1][0] == 100.0
        # Both have avg_power (not NP), so IF is None (IF only set when NP available)
        assert tss_data[0][1] is None
        assert tss_data[1][1] is None


# ---------------------------------------------------------------------------
# TRIMP computation path
# ---------------------------------------------------------------------------

class TestTRIMPComputation:
    """Verify TRIMP wiring: SELECT ids → fetch HR → compute → UPDATE."""

    def test_trimp_computed_for_listed_activities(self):
        """Activities in trimp_activity_ids get TRIMP computed and stored."""
        today = date.today()
        activity_rows = [(1, 3600, 150, 200, None, 200)]
        tss_rows = [(today, 80.0, 50000, 500)]

        conn = _make_conn(activity_rows, tss_rows=tss_rows, trimp_activity_ids=[1])

        with patch.dict(sys.modules, {"db": MagicMock()}):
            recalculate_fitness(conn)

        # TRIMP SELECT is idx 5 (thr=0, ftp=1, np_sel=2, bf_count=3, stamp=4, tss_sel=5, tss_batch=6, trimp_sel=7)
        # With 0 power activities and 0 backfill: trimp_select_idx = 3+0+1+1+1 = 6
        # trimp_start_idx = 7, HR fetch = idx 7, UPDATE = idx 8
        trimp_update_idx = None
        for idx, cur in conn._cursors:
            # Find the UPDATE that sets trimp (last UPDATE before readback)
            calls = cur.execute.call_args_list
            for c in calls:
                sql = c[0][0] if c[0] else ""
                if "UPDATE activities SET trimp" in sql:
                    trimp_update_idx = idx
                    params = c[0][1]
        assert trimp_update_idx is not None, "TRIMP UPDATE was never called"
        # Verify TRIMP value was computed (mock returns [140]*120 HR samples)
        assert params[0] > 0  # trimp_val
        assert params[1] == 1  # activity id

    def test_no_trimp_activities_skips_computation(self):
        """When no activities need TRIMP, no HR fetch or UPDATE cursors open."""
        today = date.today()
        activity_rows = [(1, 3600, 150, 200, None, 200)]
        tss_rows = [(today, 80.0, 50000, 500)]

        conn = _make_conn(activity_rows, tss_rows=tss_rows, trimp_activity_ids=[])

        with patch.dict(sys.modules, {"db": MagicMock()}):
            recalculate_fitness(conn)

        # No TRIMP UPDATE should appear
        for idx, cur in conn._cursors:
            for c in cur.execute.call_args_list:
                sql = c[0][0] if c[0] else ""
                assert "UPDATE activities SET trimp" not in sql


# ---------------------------------------------------------------------------
# FTP backfill path
# ---------------------------------------------------------------------------

class TestFTPBackfill:
    """Verify the backfill code path is exercised when rides need ride_ftp."""

    def test_backfill_adds_extra_cursors(self):
        """Backfill path opens extra cursors (backfill + stamp) vs no-backfill."""
        today = date.today()
        activity_rows = [(1, 3600, None, 200, None, 200)]
        tss_rows = [(today, 80.0, 50000, 500)]

        conn_no_backfill = _make_conn(activity_rows, tss_rows=tss_rows, backfill_count=0)
        conn_with_backfill = _make_conn(activity_rows, tss_rows=tss_rows, backfill_count=3)

        with patch.dict(sys.modules, {"db": MagicMock()}):
            recalculate_fitness(conn_no_backfill)
        with patch.dict(sys.modules, {"db": MagicMock()}):
            recalculate_fitness(conn_with_backfill)

        # Backfill path adds 2 cursors (backfill UPDATE + stamp remaining)
        assert conn_with_backfill.cursor.call_count == conn_no_backfill.cursor.call_count + 2
        conn_with_backfill.commit.assert_called()

    def test_configured_ftp_stamps_directly_no_backfill(self):
        """When VELOMATE_FTP is set, rides get stamped with configured FTP
        instead of running the expensive stream-based backfill query."""
        today = date.today()
        activity_rows = [(1, 3600, None, 200, None, 200)]
        tss_rows = [(today, 80.0, 50000, 500)]

        # configured_ftp=True: mock expects 1 stamp cursor (not 2 for backfill+stamp)
        # and no FTP estimate cursor (skipped when env var is set)
        conn = _make_conn(activity_rows, tss_rows=tss_rows, backfill_count=3,
                          configured_ftp=True)

        with patch.dict("os.environ", {"VELOMATE_FTP": "175"}):
            recalculate_fitness(conn)

        # The stamp cursor is right after the COUNT cursor
        # With configured_ftp: backfill_count_idx = 2, stamp = idx 3
        stamp_idx = 3
        _, stamp_cur = conn._cursors[stamp_idx]
        sql = stamp_cur.execute.call_args[0][0]
        params = stamp_cur.execute.call_args[0][1]
        assert "UPDATE activities SET ride_ftp" in sql
        assert "COALESCE" not in sql, "Should stamp directly, not use stream backfill"
        assert params == (175,)

        # Verify no COALESCE backfill query was executed anywhere
        for idx, cur in conn._cursors:
            for c in cur.execute.call_args_list:
                sql_str = c[0][0] if c[0] else ""
                assert "rolling_avg" not in sql_str, \
                    "Stream-based backfill should not run when FTP is configured"

    def test_configured_ftp_adds_one_cursor_vs_auto(self):
        """Configured FTP uses 1 cursor (stamp) vs auto-estimate's 2 (backfill+stamp)."""
        today = date.today()
        activity_rows = [(1, 3600, None, 200, None, 200)]
        tss_rows = [(today, 80.0, 50000, 500)]

        conn_auto = _make_conn(activity_rows, tss_rows=tss_rows, backfill_count=3,
                               configured_ftp=False)
        conn_cfg = _make_conn(activity_rows, tss_rows=tss_rows, backfill_count=3,
                              configured_ftp=True)

        recalculate_fitness(conn_auto)
        with patch.dict("os.environ", {"VELOMATE_FTP": "175"}):
            recalculate_fitness(conn_cfg)

        # Auto-estimate: estimate_ftp(1) + backfill(1) + stamp(1) = 3 extra
        # Configured: no estimate_ftp + stamp(1) = 1 extra
        # Net difference: 2 fewer cursors
        assert conn_auto.cursor.call_count == conn_cfg.cursor.call_count + 2

    def test_backfill_update_receives_ftp_fallback(self):
        """Backfill UPDATE should be called with global FTP (250) as COALESCE fallback."""
        today = date.today()
        activity_rows = [(1, 3600, None, 200, None, 200)]
        tss_rows = [(today, 80.0, 50000, 500)]

        conn = _make_conn(activity_rows, tss_rows=tss_rows, backfill_count=3)

        with patch.dict(sys.modules, {"db": MagicMock()}):
            recalculate_fitness(conn)

        # Backfill UPDATE cursor is idx 4 (threshold=0, ftp=1, np_select=2, count=3, backfill=4)
        backfill_idx, backfill_cur = conn._cursors[4]
        assert backfill_idx == 4
        sql = backfill_cur.execute.call_args[0][0]
        params = backfill_cur.execute.call_args[0][1]
        assert "UPDATE activities" in sql
        assert "COALESCE" in sql  # discriminates backfill from stamp UPDATE
        # FTP fallback param should be the auto-estimated FTP (250 from mock)
        assert params == (250,)


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------

class TestRecalcEdgeCases:
    def test_no_activities_returns_early(self):
        """When there are no activities, function should return without error."""
        conn = _make_conn(activity_rows=[], tss_rows=[])
        upsert_mock = MagicMock()

        with patch.dict(sys.modules, {"db": MagicMock(upsert_athlete_stats=upsert_mock)}):
            recalculate_fitness(conn)

        # No stats should be upserted
        upsert_mock.assert_not_called()

    def test_env_var_overrides_auto_estimation(self):
        """VELOMATE_MAX_HR and VELOMATE_FTP env vars skip auto-estimation."""
        conn = MagicMock()
        conn.autocommit = True
        # Return empty results to short-circuit
        conn.cursor().__enter__().fetchall.return_value = []
        conn.cursor().__enter__().fetchone.return_value = (0,)
        conn.cursor().__enter__().rowcount = 0

        with (
            patch.dict("os.environ", {"VELOMATE_MAX_HR": "180", "VELOMATE_FTP": "260"}),
            patch("fitness.estimate_threshold_hr") as mock_thr,
            patch("fitness.estimate_ftp") as mock_ftp,
        ):
            try:
                _original_recalc(conn)
            except (ValueError, StopIteration):
                pass

        # Auto-estimation functions should NOT be called when env vars are set
        mock_thr.assert_not_called()
        mock_ftp.assert_not_called()
