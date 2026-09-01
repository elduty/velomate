"""Tests for pure calculation functions in ingestor/fitness.py."""

import math
import re
from unittest.mock import MagicMock

import pytest
from datetime import date
from fitness import (
    calculate_tss, calculate_tss_power,
    compute_np, compute_trimp, compute_if, compute_vi,
    compute_decoupling,
    MIN_DECOUPLING_SAMPLES,
    select_cp_for_date,
    sampling_cadence_s,
    compute_coasting_time,
    compute_kj_above_ftp,
    compute_polarization_index,
    select_power_for_tss, HIGH_VI_THRESHOLD,
    estimate_ftp,
)


# --- estimate_ftp (rolling 20-min best power) ---

class TestEstimateFtpWindowGuard:
    """estimate_ftp's rolling 20-min max must only consider FULL 1200-sample
    windows. Without the guard, a hard effort in a ride's first 20 minutes is
    averaged over a partial (<1200-sample) window and mis-scored as 20-min
    power, inflating the auto-estimated FTP. The ride_ftp backfill query already
    guards this way (COUNT(*) OVER w >= 1200); estimate_ftp must match.

    psycopg2 is mocked here so the query is not executed — this asserts the
    guard is emitted. Behavioural verification runs against a real PostgreSQL.
    """

    def _mock_conn(self, rolling_result):
        conn = MagicMock()
        cur = MagicMock()
        conn.cursor.return_value.__enter__ = MagicMock(return_value=cur)
        conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
        cur.fetchone.return_value = rolling_result
        return conn, cur

    def test_rolling_query_counts_window_samples(self):
        """The rolling CTE must COUNT samples per window to detect partial ones."""
        conn, cur = self._mock_conn((300,))  # value returned → early return after 1st query
        estimate_ftp(conn)
        rolling_sql = cur.execute.call_args_list[0][0][0]
        assert re.search(r"count\(\*\)\s+over", rolling_sql, re.IGNORECASE), \
            "estimate_ftp must COUNT window samples to detect partial windows"

    def test_rolling_query_rejects_partial_windows(self):
        """Only windows with the full 1200 samples may contribute to MAX."""
        conn, cur = self._mock_conn((300,))
        estimate_ftp(conn)
        rolling_sql = cur.execute.call_args_list[0][0][0]
        assert re.search(r"window_size\s*>=\s*1200", rolling_sql, re.IGNORECASE), \
            "estimate_ftp must reject windows shorter than 1200 samples"


# --- calculate_tss (HR-based) ---

class TestCalculateTss:
    def test_normal(self):
        """3600s, 150bpm, 170bpm threshold -> (1h) * (150/170)^2 * 100"""
        result = calculate_tss(3600, 150, 170)
        expected = 1.0 * (150 / 170) ** 2 * 100
        assert result == pytest.approx(expected, abs=0.01)

    def test_zero_duration(self):
        assert calculate_tss(0, 150, 170) == 0.0

    def test_zero_hr(self):
        assert calculate_tss(3600, 0, 170) == 0.0

    def test_none_hr(self):
        assert calculate_tss(3600, None, 170) == 0.0

    def test_none_duration(self):
        assert calculate_tss(None, 150, 170) == 0.0


# --- calculate_tss_power (power-based) ---

class TestCalculateTssPower:
    def test_normal(self):
        """3600s, 200W, 250W FTP -> (3600 * 200 * 0.8) / (250 * 3600) * 100 = 64.0"""
        result = calculate_tss_power(3600, 200, 250)
        intensity = 200 / 250  # 0.8
        expected = (3600 * 200 * intensity) / (250 * 3600) * 100  # 64.0
        assert result == pytest.approx(expected, abs=0.01)

    def test_zero_power(self):
        assert calculate_tss_power(3600, 0, 250) == 0.0

    def test_zero_ftp(self):
        assert calculate_tss_power(3600, 200, 0) == 0.0

    def test_none_power(self):
        assert calculate_tss_power(3600, None, 250) == 0.0

    def test_high_intensity(self):
        """300W at 250W FTP -> above threshold, TSS > 100."""
        result = calculate_tss_power(3600, 300, 250)
        assert result > 100


# --- compute_np (Normalized Power, 30s SMA) ---

class TestComputeNP:
    """NP uses 30-second SMA with circular buffer (Coggan/GoldenCheetah standard)."""

    def test_constant_power(self):
        """Constant 200W for 600s → NP should equal avg power.
        Needs enough samples for the zero-initialized buffer warmup to be negligible."""
        result = compute_np([200] * 3600)
        assert result == pytest.approx(200, abs=0.5)

    def test_too_few_samples(self):
        """Less than 30 samples → None."""
        assert compute_np([200] * 29) is None

    def test_empty(self):
        assert compute_np([]) is None

    def test_variable_power_higher_than_avg(self):
        """Alternating 0/300W → NP should be well above half the peak."""
        samples = [0, 300] * 600  # 1200 samples
        result = compute_np(samples)
        assert result > 140

    def test_sma_circular_buffer(self):
        """SMA with circular buffer: after 30 samples, old values are replaced."""
        # 30 samples at 200W, then 30 at 0W → rolling avg drops to 0
        samples = [200] * 60 + [0] * 60
        result = compute_np(samples)
        # NP should be above 0 (first 60s contribute) but below 200
        assert 50 < result < 200

    def test_all_zeros(self):
        """All zero power → NP should be 0/None."""
        result = compute_np([0] * 120)
        assert result is None or result == 0.0


# --- compute_trimp (Banister) ---

class TestComputeTrimp:
    """Banister TRIMP with HRR capped at 1.0."""

    def test_normal(self):
        """60 samples at 144bpm, max=175, rest=50."""
        hrr = (144 - 50) / (175 - 50)  # 0.752
        expected_per_sample = (1 / 60) * hrr * 0.64 * math.exp(1.92 * hrr)
        expected = round(expected_per_sample * 60, 1)
        result = compute_trimp([144] * 60, max_hr=175, resting_hr=50)
        assert result == expected

    def test_hrr_capped_at_one(self):
        """HR above max_hr should be capped at HRR=1.0."""
        capped = (1 / 60) * 1.0 * 0.64 * math.exp(1.92 * 1.0)
        expected = round(capped * 60, 1)
        result = compute_trimp([200] * 60, max_hr=175, resting_hr=50)
        assert result == expected

    def test_hr_below_resting_excluded(self):
        """Samples at or below resting HR contribute 0."""
        result = compute_trimp([40, 45, 50] * 20, max_hr=175, resting_hr=50)
        assert result == 0.0

    def test_empty(self):
        assert compute_trimp([], max_hr=175, resting_hr=50) == 0.0

    def test_zero_max_hr(self):
        assert compute_trimp([144] * 60, max_hr=0, resting_hr=50) == 0.0

    def test_max_equals_resting(self):
        assert compute_trimp([144] * 60, max_hr=50, resting_hr=50) == 0.0


# --- compute_if (Intensity Factor) ---

class TestComputeIF:
    """IF = NP / FTP."""

    def test_normal(self):
        assert compute_if(118, 250) == 0.47

    def test_high_intensity(self):
        assert compute_if(300, 250) == 1.2

    def test_zero_ftp(self):
        assert compute_if(200, 0) is None

    def test_none_np(self):
        assert compute_if(None, 250) is None

    def test_none_ftp(self):
        assert compute_if(200, None) is None


# --- compute_vi (Variability Index) ---

class TestComputeVI:
    """VI = NP / avg_power."""

    def test_normal(self):
        assert compute_vi(118, 109) == 1.08

    def test_steady_ride(self):
        assert compute_vi(200, 200) == 1.0

    def test_zero_avg(self):
        assert compute_vi(200, 0) is None

    def test_none_np(self):
        assert compute_vi(None, 150) is None


# --- compute_decoupling (Friel) ---

class TestComputeDecoupling:
    """Aerobic decoupling: (first_half_EF / second_half_EF - 1) * 100.
    EF = avg_power / avg_hr. Positive = cardiac drift (HR rising relative to power).
    """

    def test_no_drift(self):
        """Constant power and HR across both halves -> 0% decoupling."""
        power = [200] * 1200
        hr = [150] * 1200
        assert compute_decoupling(power, hr) == 0.0

    def test_cardiac_drift(self):
        """HR rising in second half while power constant -> positive decoupling."""
        power = [200] * 1200
        hr = [140] * 600 + [160] * 600
        # first_ef = 200/140 = 1.4286; second_ef = 200/160 = 1.25
        # decoupling = (1.4286/1.25 - 1) * 100 = 14.29
        result = compute_decoupling(power, hr)
        assert result == pytest.approx(14.29, abs=0.1)

    def test_negative_drift(self):
        """HR falling in second half -> negative decoupling (rare but valid)."""
        power = [200] * 1200
        hr = [160] * 600 + [140] * 600
        # first_ef = 1.25; second_ef = 1.4286; decoupling = (1.25/1.4286 - 1) * 100 = -12.5
        result = compute_decoupling(power, hr)
        assert result == pytest.approx(-12.5, abs=0.1)

    def test_empty(self):
        assert compute_decoupling([], []) is None

    def test_mismatched_lengths(self):
        assert compute_decoupling([200, 200], [150]) is None

    def test_far_below_the_minimum(self):
        """A two-sample stream is nowhere near the floor -> None.

        (There is no separate two-per-half guard any more; this returns None
        because 2 < MIN_DECOUPLING_SAMPLES. Kept as a degenerate-input check.)
        """
        assert compute_decoupling([200, 200], [150, 150]) is None

    def test_ride_shorter_than_minimum_returns_none(self):
        """A ride too short for the halves to mean anything gets no value.

        Heart rate lags effort by minutes, so on a short ride the first half is
        mostly HR still climbing — which reads as huge drift that never
        happened. A real 4-minute Zwift effort scored 46.6%, where >10% is
        already a strong signal on an endurance ride.
        """
        n = MIN_DECOUPLING_SAMPLES - 1
        power = [200] * n
        hr = [140] * (n // 2) + [160] * (n - n // 2)   # would read as big drift
        assert compute_decoupling(power, hr) is None

    def test_minimum_is_twenty_minutes(self):
        """Pin the value, not just the behaviour.

        Tests that size their data from the constant pass for ANY value of it,
        so they cannot catch the floor being set wrong. At 1 Hz this is 20
        minutes of pedalling, leaving each half a 10-minute average.
        """
        assert MIN_DECOUPLING_SAMPLES == 1200

    def test_ride_at_the_minimum_is_computed(self):
        """The floor must not blank rides that are long enough to trust.

        Literal 1200, deliberately: raising the constant must fail here rather
        than silently scaling the fixture along with it.
        """
        power = [200] * 1200
        hr = [140] * 600 + [160] * 600
        assert compute_decoupling(power, hr) == pytest.approx(14.29, abs=0.1)

    def test_zero_hr_in_first_half(self):
        """Zero HR samples in first half should not produce infinity."""
        power = [200] * 1200
        hr = [0] * 600 + [150] * 600
        # first half has no valid HR -> cannot compute first_ef -> None
        assert compute_decoupling(power, hr) is None

    def test_none_samples_filtered(self):
        """None values in the stream should be filtered, not cause TypeError."""
        power = [200, None, 200] * 600
        hr = [140, None, 140] * 600
        result = compute_decoupling(power, hr)
        assert result is not None

    def test_coasting_excluded_so_uneven_coasting_does_not_fake_drift(self):
        """Coasting must not register as cardiac drift.

        The rider holds exactly 200W whenever pedalling and HR is flat at 150,
        so true decoupling is 0%. The second half simply contains more coasting
        (traffic, descents). Averaging those zeros into each half's power drags
        the second half's EF down and manufactures large positive drift that the
        rider never experienced — the bug that made real urban rides read up to
        97% against intervals.icu's 38%.
        """
        # first half: 10% coasting; second half: 60% coasting
        first_power = [0] * 60 + [200] * 540
        second_power = [0] * 360 + [200] * 240
        power = first_power + second_power
        hr = [150] * 1200
        assert compute_decoupling(power, hr) == pytest.approx(0.0, abs=0.01)

    def test_coasting_only_half_returns_none(self):
        """A half with no pedalling at all has no meaningful EF."""
        power = [200] * 600 + [0] * 600
        hr = [150] * 1200
        assert compute_decoupling(power, hr) is None

    def test_real_drift_still_detected_with_coasting_present(self):
        """Excluding coasting must not mask genuine drift."""
        # Same pedalling power, HR climbs 140 -> 160, coasting present in both halves
        power = ([0] * 120 + [200] * 480) * 2      # 1200 samples: 600 per half
        hr = [140] * 600 + [160] * 600
        # first_ef = 200/140, second_ef = 200/160 -> 14.29%
        assert compute_decoupling(power, hr) == pytest.approx(14.29, abs=0.1)


# --- select_power_for_tss (VI-aware TSS input selection) ---

class TestSelectPowerForTss:
    """Picks which power value to feed into Coggan TSS.

    Standard rides (VI <= 1.30) use NP because it correctly models steady
    physiological load. High-VI rides (urban stop-and-go, VI > 1.30) use
    avg_power because NP's 4th-power weighting overestimates load on rides
    dominated by coasting + surges. The boundary matches the published
    Coggan-model validity range.
    """

    def test_threshold_constant_is_1_30(self):
        """Sanity-check the documented threshold value. Matches the
        Coggan-model validity range reported in cycling-physiology
        literature for steady-state assumptions."""
        assert HIGH_VI_THRESHOLD == 1.30

    def test_standard_ride_uses_np(self):
        """Steady ride with VI 1.10 → use NP for TSS."""
        # np=220, avg=200 → vi=1.10
        assert select_power_for_tss(np=220, avg_power=200) == 220

    def test_high_vi_ride_uses_avg_power(self):
        """Urban ride with VI 1.54 (the user's real case) → use avg_power."""
        # np=176, avg=114 → vi=1.54
        assert select_power_for_tss(np=176, avg_power=114) == 114

    def test_right_at_threshold_uses_np(self):
        """VI exactly at 1.30 is the boundary — still use NP.
        (Strict > comparison so the threshold itself is "standard".)"""
        # np=130, avg=100 → vi=1.30 exactly
        assert select_power_for_tss(np=130, avg_power=100) == 130

    def test_just_above_threshold_uses_avg_power(self):
        """VI just above the threshold triggers the fallback."""
        # np=131, avg=100 → vi=1.31
        assert select_power_for_tss(np=131, avg_power=100) == 100

    def test_none_np_falls_back_to_avg_power(self):
        """When NP is missing entirely (too few samples), use avg_power."""
        assert select_power_for_tss(np=None, avg_power=180) == 180

    def test_none_avg_power_with_np_uses_np(self):
        """When avg_power is missing but NP is present, use NP (best we have)."""
        assert select_power_for_tss(np=220, avg_power=None) == 220

    def test_both_none_returns_none(self):
        """When neither is available, nothing to return."""
        assert select_power_for_tss(np=None, avg_power=None) is None

    def test_zero_avg_power_falls_back_to_np(self):
        """avg_power=0 should not be used (divide-by-zero on VI and
        meaningless TSS input). Fall back to NP if present."""
        assert select_power_for_tss(np=220, avg_power=0) == 220

    def test_zero_np_falls_back_to_avg_power(self):
        """np=0 (no valid power samples) should not be used. Fall back
        to avg_power if present and non-zero."""
        assert select_power_for_tss(np=0, avg_power=180) == 180

    def test_both_zero_returns_none(self):
        """Both zero is effectively 'no power data'."""
        assert select_power_for_tss(np=0, avg_power=0) is None


# --- provider-independent ride metrics ---

class TestComputeCoastingTime:
    """Seconds spent freewheeling. Computed from our own power stream so it is
    identical whichever provider delivered the ride."""

    def test_counts_zero_power_samples(self):
        assert compute_coasting_time([0] * 30 + [200] * 70) == 30

    def test_no_coasting_returns_zero(self):
        assert compute_coasting_time([200] * 50) == 0

    def test_none_samples_count_as_coasting(self):
        # A dropped sample is not pedalling; treat it the same as zero.
        assert compute_coasting_time([None, None, 200, 200]) == 2

    def test_empty_stream_returns_zero(self):
        assert compute_coasting_time([]) == 0


class TestComputeKjAboveFtp:
    """Anaerobic work spent above threshold — the 'matches burned' metric."""

    def test_sums_only_the_excess_over_ftp(self):
        # 100 samples at 300W with FTP 200 -> 100 * 100J = 10 kJ
        assert compute_kj_above_ftp([300] * 100, 200) == pytest.approx(10.0)

    def test_power_below_ftp_contributes_nothing(self):
        assert compute_kj_above_ftp([150] * 100, 200) == 0.0

    def test_mixed_stream_counts_only_the_above_portion(self):
        assert compute_kj_above_ftp([300] * 100 + [100] * 100, 200) == pytest.approx(10.0)

    def test_none_samples_are_skipped(self):
        assert compute_kj_above_ftp([None, 300, None], 200) == pytest.approx(0.1)

    def test_missing_ftp_returns_none(self):
        assert compute_kj_above_ftp([300] * 10, 0) is None
        assert compute_kj_above_ftp([300] * 10, None) is None


class TestComputePolarizationIndex:
    """Treff polarization index over the 3-zone model. PI >= 2.0 = polarised."""

    def test_polarised_distribution_scores_at_or_above_two(self):
        # Lots of easy, little middle, some hard — the polarised shape
        power = [100] * 800 + [160] * 50 + [250] * 150   # FTP 200
        pi = compute_polarization_index(power, 200)
        assert pi is not None and pi >= 2.0

    def test_threshold_heavy_distribution_scores_low(self):
        # Most time in the middle zone — the opposite of polarised
        power = [100] * 100 + [180] * 800 + [250] * 100
        pi = compute_polarization_index(power, 200)
        assert pi is not None and pi < 2.0

    def test_no_hard_time_returns_none(self):
        # Z3 empty -> log10 of zero is undefined; no meaningful index
        assert compute_polarization_index([100] * 500 + [180] * 500, 200) is None

    def test_no_middle_time_returns_none(self):
        # Z2 empty -> division by zero
        assert compute_polarization_index([100] * 500 + [250] * 500, 200) is None

    def test_missing_ftp_returns_none(self):
        assert compute_polarization_index([100, 200, 300], 0) is None

    def test_empty_stream_returns_none(self):
        assert compute_polarization_index([], 200) is None


class TestPolarizationIndexDomainGuard:
    """log10(0) raises ValueError, which would abort the whole recalculation
    run rather than skipping one ride."""

    def test_empty_z1_with_populated_z2_and_z3_returns_none(self):
        """Isolates the z1 guard specifically.

        FTP 200 -> z1 < 150, z2 150-210, z3 >= 210. This stream has no easy
        time but real z2 and z3, so the z2/z3 guards do NOT fire and the log
        argument is (0/z2)*z3 == 0. Without the z1 guard this raises
        ValueError: math domain error and aborts the whole recalculation run.
        """
        stream = [180] * 100 + [260] * 100
        assert compute_polarization_index(stream, 200) is None

    def test_a_sustained_climb_does_not_raise(self):
        """A real shape that produces it: a climb where every pedalling sample
        is tempo-or-harder once coasting is excluded."""
        climb = [0] * 20 + [190] * 300 + [230] * 200
        assert compute_polarization_index(climb, 200) is None

    def test_all_hard_samples_returns_none(self):
        assert compute_polarization_index([260] * 200, 200) is None


class TestCpAsOfDate:
    """W'bal must model each ride against the fitness the rider actually had
    that day, not today's. Using the single latest CP applies current fitness
    to a ride from months ago, and because the selector gates on w_bal IS NULL
    the value is then frozen at whatever CP happened to be latest when it was
    first computed."""

    def test_picks_the_most_recent_estimate_at_or_before_the_ride(self):
        rows = [(date(2026, 1, 1), 180.0, 18.0, "cp", 170.0),
                (date(2026, 5, 1), 195.0, 20.0, "cp", 190.0),
                (date(2026, 8, 1), 210.0, 22.0, "cp", 205.0)]
        cp, wj = select_cp_for_date(rows, date(2026, 6, 15))
        assert cp == 195.0 and wj == 20000.0

    def test_a_ride_before_any_estimate_uses_the_earliest(self):
        """Better an early-season CP than none — the alternative is no W'bal
        at all for the oldest rides."""
        rows = [(date(2026, 5, 1), 195.0, 20.0, "cp", 190.0)]
        cp, wj = select_cp_for_date(rows, date(2026, 1, 1))
        assert cp == 195.0

    def test_falls_back_to_the_20min_estimate_when_the_fit_failed(self):
        rows = [(date(2026, 5, 1), None, None, "20min_fallback", 188.0)]
        cp, wj = select_cp_for_date(rows, date(2026, 6, 1))
        assert cp == 188.0
        assert wj == 20000.0, "Skiba default W' when no fit is available"

    def test_exact_date_match_uses_that_day(self):
        rows = [(date(2026, 5, 1), 195.0, 20.0, "cp", 190.0),
                (date(2026, 6, 1), 200.0, 21.0, "cp", 195.0)]
        cp, _ = select_cp_for_date(rows, date(2026, 6, 1))
        assert cp == 200.0

    def test_no_usable_estimate_returns_none(self):
        assert select_cp_for_date([], date(2026, 6, 1)) == (None, None)
        assert select_cp_for_date(
            [(date(2026, 5, 1), None, None, "no_fit", None)], date(2026, 6, 1)) == (None, None)

    def test_a_ride_with_no_date_returns_none(self):
        rows = [(date(2026, 5, 1), 195.0, 20.0, "cp", 190.0)]
        assert select_cp_for_date(rows, None) == (None, None)


class TestSamplingCadence:
    """NP, MMP/CP and W'bal all treat one sample as one second. That holds for
    a 1 Hz moving-time stream with auto-pause gaps — measured at 99.5-99.8% of
    steps on real rides — but breaks under smart recording, where samples are
    genuinely sparse *while moving*. This turns the silent assumption into a
    checked one."""

    def test_contiguous_stream_is_one_hz(self):
        assert sampling_cadence_s(list(range(600))) == 1.0

    def test_pauses_do_not_look_like_sparse_sampling(self):
        """A few long stops must not be mistaken for smart recording — the
        median step is still 1 s."""
        offs = list(range(0, 300)) + list(range(900, 1200))   # one 600 s pause
        assert sampling_cadence_s(offs) == 1.0

    def test_smart_recording_is_detected(self):
        """Samples every 4 s while moving — the case that would make a 30-sample
        NP window span 120 s."""
        assert sampling_cadence_s(list(range(0, 1200, 4))) == 4.0

    def test_too_few_samples_returns_none(self):
        assert sampling_cadence_s([]) is None
        assert sampling_cadence_s([0]) is None

    def test_irregular_stream_reports_the_median_not_the_mean(self):
        # one huge gap must not drag the estimate up
        assert sampling_cadence_s([0, 1, 2, 3, 4, 1000]) == 1.0
