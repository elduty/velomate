"""Tests for pure calculation functions in ingestor/fitness.py."""

import math

import pytest
from fitness import (
    calculate_tss, calculate_tss_power,
    compute_np, compute_trimp, compute_if, compute_vi,
    compute_decoupling,
    select_power_for_tss, HIGH_VI_THRESHOLD,
)


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
        power = [200] * 200
        hr = [150] * 200
        assert compute_decoupling(power, hr) == 0.0

    def test_cardiac_drift(self):
        """HR rising in second half while power constant -> positive decoupling."""
        power = [200] * 200
        hr = [140] * 100 + [160] * 100
        # first_ef = 200/140 = 1.4286; second_ef = 200/160 = 1.25
        # decoupling = (1.4286/1.25 - 1) * 100 = 14.29
        result = compute_decoupling(power, hr)
        assert result == pytest.approx(14.29, abs=0.1)

    def test_negative_drift(self):
        """HR falling in second half -> negative decoupling (rare but valid)."""
        power = [200] * 200
        hr = [160] * 100 + [140] * 100
        # first_ef = 1.25; second_ef = 1.4286; decoupling = (1.25/1.4286 - 1) * 100 = -12.5
        result = compute_decoupling(power, hr)
        assert result == pytest.approx(-12.5, abs=0.1)

    def test_empty(self):
        assert compute_decoupling([], []) is None

    def test_mismatched_lengths(self):
        assert compute_decoupling([200, 200], [150]) is None

    def test_too_few_samples(self):
        """Fewer than 2 samples per half -> None."""
        assert compute_decoupling([200, 200], [150, 150]) is None

    def test_zero_hr_in_first_half(self):
        """Zero HR samples in first half should not produce infinity."""
        power = [200] * 200
        hr = [0] * 100 + [150] * 100
        # first half has no valid HR -> cannot compute first_ef -> None
        assert compute_decoupling(power, hr) is None

    def test_none_samples_filtered(self):
        """None values in the stream should be filtered, not cause TypeError."""
        power = [200, None, 200] * 100
        hr = [140, None, 140] * 100
        result = compute_decoupling(power, hr)
        assert result is not None


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
