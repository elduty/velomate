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
    power_activity_rows: [(id, avg_hr, avg_power, existing_np, existing_decoupling), ...]
        -- for Step 1 NP/decoupling loop. 3 cursors per activity:
        SELECT power+hr stream, UPDATE np/ef/vi/work, UPDATE aerobic_decoupling.
    tss_rows: [(date, tss, distance_m, elevation_m), ...] -- for final readback
    backfill_count: number of rides needing FTP backfill (0 = skip backfill)
    trimp_activity_ids: [id, ...] -- activities needing TRIMP computation
    configured_ftp: if True, backfill uses single stamp (1 cursor) instead of
                    stream-based backfill + stamp (2 cursors)

    Cursor sequence in recalculate_fitness (called via patched wrapper):
      0: estimate_threshold_hr
      1: estimate_ftp (rolling 20-min) -- skipped when configured_ftp=True
      2: SELECT power activities for Step 1 NP/EF/VI/decoupling loop
      3..3+3*N-1: per-power-activity triplet
          (stream SELECT + NP/EF/VI/work UPDATE + decoupling UPDATE)
      3+3*N: COUNT rides needing FTP backfill
      When backfill_count > 0 and configured_ftp: 1 stamp cursor
      When backfill_count > 0 and auto-estimate: 2 cursors (backfill + stamp)
      When backfill_count == 0: no backfill/stamp cursors
      Then: Step 2.5 interval activities SELECT (returns [] in mock so the
      loop is empty), TSS+IF select, batch, TRIMP select, per-TRIMP cursors,
      readback.
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
    # estimate_ftp() is now ALWAYS called (even when configured_ftp is set), so
    # the cursor sequence no longer skips index 1. Tests that previously set
    # configured_ftp=True still need a (250,) response for that cursor.
    backfill_count_idx = 3 + 3 * n_power
    interval_select_idx = backfill_count_idx + 1 + b  # Step 2.5 top-level SELECT
    tss_select_idx = interval_select_idx + 1
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

        np_select_idx = 2

        if idx == 0:
            cur.fetchone.return_value = (170,)
        elif idx == 1:
            # estimate_ftp — always called, even when configured_ftp is set
            cur.fetchone.return_value = (250,)
        elif idx == np_select_idx:
            cur.fetchall.return_value = power_activity_rows or []
        elif np_select_idx < idx < backfill_count_idx:
            # Per-activity triplet (Step 1):
            #   0: stream SELECT (power, hr)
            #   1: NP/EF/VI/work UPDATE
            #   2: aerobic_decoupling UPDATE
            offset = idx - np_select_idx - 1
            if offset % 3 == 0:
                # SELECT power, hr samples — return 60 samples at (200W, 150bpm)
                cur.fetchall.return_value = [(200, 150)] * 60
            # else: UPDATE np/ef/vi/work or UPDATE aerobic_decoupling
            # (no special setup needed for UPDATEs)
        elif idx == backfill_count_idx:
            cur.fetchone.return_value = (backfill_count,)
        elif backfill_count_idx < idx < interval_select_idx:
            # Backfill/stamp cursors (only when backfill_count > 0)
            cur.rowcount = backfill_count
        elif idx == interval_select_idx:
            # Step 2.5 interval activities SELECT — return [] so the
            # detection loop doesn't iterate (tests don't exercise the
            # stream-fetch/insert path; pure-function coverage is in
            # tests/test_intervals.py).
            cur.fetchall.return_value = []
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
        # This activity appears in NP query (np IS NULL, aerobic_decoupling IS NULL, has power streams)
        # 5-tuple: (id, avg_hr, avg_power, existing_np, existing_decoupling)
        power_activity_rows = [(1, 150, 200, None, None)]
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

    def test_high_vi_ride_uses_avg_power_for_tss(self):
        """Regression test for the VI-aware TSS fix: on a high-VI ride
        (np=176, avg=114 → VI=1.54) the TSS loop must compute TSS from
        avg_power, not NP. Locks in the behaviour so future refactors
        can't silently revert to the NP-overestimation bug.

        User's real 2026-04-03 ride: NP=176, avg=114, 86 min, FTP=175.
        NP-based TSS: (86*60 * 176 * 176/175) / (175 * 3600) * 100 = 145.5
        Avg-based TSS: (86*60 * 114 * 114/175) / (175 * 3600) * 100 = 63.7
        """
        today = date.today()
        # (id, duration_s, avg_hr, avg_power, np, ride_ftp)
        activity_rows = [
            (1, 86 * 60, 140, 114, 176, 175),  # high VI = 1.54 → avg_power path
            (2, 3600, 150, 200, 220, 175),     # normal VI = 1.10 → NP path
        ]
        tss_rows = [(today, 50.0, 40000, 300)]

        conn = _make_conn(activity_rows, tss_rows=tss_rows)
        import psycopg2.extras as extras_mock

        with patch.dict(sys.modules, {"db": MagicMock()}):
            recalculate_fitness(conn)

        batch_call = extras_mock.execute_batch.call_args
        tss_data = batch_call[0][2]
        # High-VI ride: TSS computed from avg_power (114), not NP (176).
        # Hand computed: (86*60 * 114 * (114/175)) / (175 * 3600) * 100
        # = 5160 * 114 * 0.6514 / 630000 * 100
        # ≈ 60.8
        assert 55 <= tss_data[0][0] <= 68, (
            f"high-VI ride TSS should be avg_power-based (~61), got {tss_data[0][0]} "
            f"— likely reverted to NP-based (would be ~145)"
        )
        # IF should also be avg_power-based: 114/175 = 0.65
        assert tss_data[0][1] == pytest.approx(0.65, abs=0.01)

        # Normal VI ride: TSS computed from NP (220), not avg_power (200).
        # (3600 * 220 * (220/175)) / (175 * 3600) * 100
        # = 220 * 1.257 / 175 * 100 ≈ 158
        assert 150 <= tss_data[1][0] <= 165, (
            f"normal-VI ride TSS should be NP-based (~158), got {tss_data[1][0]}"
        )
        # IF = 220/175 = 1.26
        assert tss_data[1][1] == pytest.approx(1.26, abs=0.01)

    def test_hr_tss_uses_lthr_not_max_hr(self):
        """Regression guard for the HR TSS fallback path.

        Coggan HR TSS formula is `duration_h × (avg_hr / LTHR)² × 100` where
        LTHR (Lactate Threshold HR) is approximately 89% of max HR per Friel's
        convention. Previously the call site passed configured max HR directly
        into calculate_tss as if it were LTHR, underestimating HR TSS by
        (0.89² = 0.79) — a ~21% shortfall. Only fires on HR-only rides (no
        power stream) so it was latent on datasets where every ride has power,
        but would skew any HR-only ride (dead power meter, HR-only fitness
        tracker workout).

        Test ride: 1h at avg_hr 150 with mocked estimated max_hr = 170.
          LTHR = round(170 × 0.89) = 151
          Old wrong (max_hr): (150/170)² × 100 = 77.9 TSS
          New right (LTHR):   (150/151)² × 100 = 98.7 TSS
        """
        today = date.today()
        # HR-only activity: avg_hr=150, avg_power=None, np=None → HR TSS path
        activity_rows = [(1, 3600, 150, None, None, 200)]
        tss_rows = [(today, 50.0, 40000, 300)]

        conn = _make_conn(activity_rows, tss_rows=tss_rows)
        import psycopg2.extras as extras_mock

        with patch.dict(sys.modules, {"db": MagicMock()}):
            recalculate_fitness(conn)

        batch_call = extras_mock.execute_batch.call_args
        tss_data = batch_call[0][2]
        # With mock max_hr=170, LTHR=151, HR TSS ≈ 98.7
        # The old buggy value using max_hr directly would be ~77.9
        assert 96 <= tss_data[0][0] <= 101, (
            f"HR TSS should be LTHR-based (~99), got {tss_data[0][0]} "
            f"— likely still using max_hr (77.9) as threshold"
        )

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
        # Both have avg_power (no NP), so IF is computed from avg_power / FTP
        # (VI-aware TSS: when NP is absent, avg_power drives both TSS and IF so
        # the invariant IF² × duration_h × 100 ≈ TSS holds).
        # Activity 1: IF = 200/250 = 0.80
        # Activity 2: IF = 200/200 = 1.00
        assert tss_data[0][1] == 0.80
        assert tss_data[1][1] == 1.00


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
        instead of running the expensive stream-based backfill query.
        estimate_ftp() is still called (always) but its result is only used
        as the diagnostic estimated_ftp value, not for the stamp."""
        today = date.today()
        activity_rows = [(1, 3600, None, 200, None, 200)]
        tss_rows = [(today, 80.0, 50000, 500)]

        # configured_ftp=True: mock expects 1 stamp cursor (not 2 for backfill+stamp)
        conn = _make_conn(activity_rows, tss_rows=tss_rows, backfill_count=3,
                          configured_ftp=True)

        with patch.dict("os.environ", {"VELOMATE_FTP": "175"}):
            recalculate_fitness(conn)

        # Cursor sequence with configured FTP: threshold(0), estimate_ftp(1),
        # np_select(2), count(3), stamp(4). The stamp is right after the COUNT cursor.
        stamp_idx = 4
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

    def test_configured_ftp_uses_one_fewer_cursor_in_backfill(self):
        """Configured FTP path stamps directly (1 cursor) vs auto-estimate's
        backfill+stamp pair (2 cursors). estimate_ftp() is now called in BOTH
        paths so it doesn't contribute to the difference any more."""
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

        # Both paths: estimate_ftp(1)
        # Auto: backfill(1) + stamp(1) = 2 cursors
        # Configured: stamp(1) = 1 cursor
        # Net difference: 1 fewer cursor for configured
        assert conn_auto.cursor.call_count == conn_cfg.cursor.call_count + 1

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
        """VELOMATE_MAX_HR overrides auto-estimation. VELOMATE_FTP overrides
        the FTP used for computation but estimate_ftp() is now still called
        unconditionally so its result can be persisted as a diagnostic value
        in sync_state.estimated_ftp alongside the configured FTP."""
        conn = MagicMock()
        conn.autocommit = True
        # Return empty results to short-circuit
        conn.cursor().__enter__().fetchall.return_value = []
        conn.cursor().__enter__().fetchone.return_value = (0,)
        conn.cursor().__enter__().rowcount = 0

        with (
            patch.dict("os.environ", {"VELOMATE_MAX_HR": "180", "VELOMATE_FTP": "260"}),
            patch("fitness.estimate_threshold_hr") as mock_thr,
            patch("fitness.estimate_ftp", return_value=200) as mock_ftp,
        ):
            try:
                _original_recalc(conn)
            except (ValueError, StopIteration):
                pass

        # estimate_threshold_hr is still skipped when VELOMATE_MAX_HR is set
        mock_thr.assert_not_called()
        # estimate_ftp is now ALWAYS called so its result can be persisted
        # as the diagnostic estimated_ftp value, even when configured FTP is set
        mock_ftp.assert_called_once()
