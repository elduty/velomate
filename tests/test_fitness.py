"""Tests for pure calculation functions in ingestor/fitness.py."""

import math

import pytest
from fitness import (
    calculate_tss, calculate_tss_power,
    compute_np, compute_trimp, compute_if, compute_vi,
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
