"""Tests for ingestor/critical_power.py — pure-function module."""

import sys
from pathlib import Path

import numpy as np
import pytest

_ingestor_dir = Path(__file__).resolve().parent.parent / "ingestor"
if str(_ingestor_dir) not in sys.path:
    sys.path.insert(0, str(_ingestor_dir))

from critical_power import (
    compute_mean_maximal_power,
    fit_monod_scherrer,
    assess_fit_quality,
)


class TestComputeMeanMaximalPower:
    def test_flat_stream_returns_flat_value(self):
        stream = [200.0] * 600
        assert compute_mean_maximal_power(stream, 60) == 200.0
        assert compute_mean_maximal_power(stream, 300) == 200.0
        assert compute_mean_maximal_power(stream, 600) == 200.0

    def test_ramping_stream_returns_highest_window(self):
        stream = [float(p) for p in range(100, 400)]
        result = compute_mean_maximal_power(stream, 60)
        assert result == pytest.approx(369.5, abs=0.1)

    def test_stream_shorter_than_duration_returns_none(self):
        stream = [200.0] * 30
        assert compute_mean_maximal_power(stream, 60) is None

    def test_empty_stream_returns_none(self):
        assert compute_mean_maximal_power([], 60) is None


class TestFitMonodScherrer:
    def test_recovers_known_parameters_from_clean_data(self):
        cp_true = 200.0
        w_prime_true_j = 15000.0
        durations = [60, 120, 300, 600, 1200]
        efforts = [(t, w_prime_true_j / t + cp_true) for t in durations]
        cp, w_prime_kj, r2 = fit_monod_scherrer(efforts)
        assert cp == pytest.approx(200.0, abs=0.5)
        assert w_prime_kj == pytest.approx(15.0, abs=0.1)
        assert r2 == pytest.approx(1.0, abs=0.001)

    def test_noisy_data_still_recovers_within_tolerance(self):
        rng = np.random.default_rng(42)
        cp_true = 200.0
        w_prime_true_j = 15000.0
        durations = [60, 120, 300, 600, 1200]
        efforts = [
            (t, w_prime_true_j / t + cp_true + rng.normal(0, 5))
            for t in durations
        ]
        cp, w_prime_kj, r2 = fit_monod_scherrer(efforts)
        assert cp == pytest.approx(200.0, abs=20.0)
        assert w_prime_kj == pytest.approx(15.0, abs=5.0)
        assert r2 > 0.85

    def test_fewer_than_two_efforts_returns_none(self):
        assert fit_monod_scherrer([]) == (None, None, None)
        assert fit_monod_scherrer([(60, 250.0)]) == (None, None, None)

    def test_negative_cp_rejected_as_failed(self):
        """Reverse-engineered: CP=-10W, W'=12kJ -> polyfit recovers negative intercept."""
        efforts = [(60, 190.0), (120, 90.0), (300, 30.0), (600, 10.0)]
        cp, w_prime_kj, r2 = fit_monod_scherrer(efforts)
        assert cp is None and w_prime_kj is None and r2 is None

    def test_negative_w_prime_rejected_as_failed(self):
        """P increases with longer duration -> negative slope -> W' <= 0."""
        efforts = [(60, 100.0), (120, 200.0), (300, 400.0), (600, 700.0)]
        cp, w_prime_kj, r2 = fit_monod_scherrer(efforts)
        assert cp is None and w_prime_kj is None and r2 is None


class TestAssessFitQuality:
    def test_high_r_squared_and_enough_durations_passes(self):
        assert assess_fit_quality(0.95, 5) is True
        assert assess_fit_quality(0.90, 4) is True

    def test_r_squared_below_threshold_fails(self):
        assert assess_fit_quality(0.89, 5) is False
        assert assess_fit_quality(0.5, 5) is False

    def test_too_few_durations_fails(self):
        assert assess_fit_quality(0.95, 3) is False
        assert assess_fit_quality(0.95, 0) is False

    def test_none_r_squared_returns_false(self):
        assert assess_fit_quality(None, 5) is False
        assert assess_fit_quality(None, 0) is False
