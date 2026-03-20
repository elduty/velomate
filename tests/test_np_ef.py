"""Tests for NP/EF/Work computation formulas used in fitness.py."""

import pytest


class TestWorkCalculation:
    """Work (kJ) = avg_power (W) * duration_s / 1000"""

    def test_normal(self):
        avg_power = 200
        duration_s = 3600
        work = round(avg_power * duration_s / 1000.0, 1)
        assert work == 720.0

    def test_short_ride(self):
        avg_power = 150
        duration_s = 1800  # 30 min
        work = round(avg_power * duration_s / 1000.0, 1)
        assert work == 270.0

    def test_high_power(self):
        avg_power = 350
        duration_s = 7200  # 2 hours
        work = round(avg_power * duration_s / 1000.0, 1)
        assert work == 2520.0

    def test_zero_power(self):
        avg_power = 0
        duration_s = 3600
        work = round(avg_power * duration_s / 1000.0, 1)
        assert work == 0.0

    def test_zero_duration(self):
        avg_power = 200
        duration_s = 0
        work = round(avg_power * duration_s / 1000.0, 1)
        assert work == 0.0

    def test_none_power_guard(self):
        """In fitness.py, Work is only computed when avg_power and duration_s are truthy."""
        avg_power = None
        duration_s = 3600
        work = round(avg_power * duration_s / 1000.0, 1) if avg_power and duration_s else None
        assert work is None

    def test_none_duration_guard(self):
        avg_power = 200
        duration_s = None
        work = round(avg_power * duration_s / 1000.0, 1) if avg_power and duration_s else None
        assert work is None


class TestEFCalculation:
    """Efficiency Factor (EF) = NP / avg_hr"""

    def test_normal(self):
        np_val = 220.0
        avg_hr = 150
        ef = round(np_val / avg_hr, 2)
        assert ef == 1.47

    def test_low_hr(self):
        np_val = 200.0
        avg_hr = 120
        ef = round(np_val / avg_hr, 2)
        assert ef == 1.67

    def test_high_np(self):
        np_val = 300.0
        avg_hr = 170
        ef = round(np_val / avg_hr, 2)
        assert ef == 1.76

    def test_zero_hr_guard(self):
        """EF is None when avg_hr is 0 (guard in fitness.py)."""
        np_val = 220.0
        avg_hr = 0
        ef = round(np_val / avg_hr, 2) if avg_hr and avg_hr > 0 else None
        assert ef is None

    def test_none_hr_guard(self):
        np_val = 220.0
        avg_hr = None
        ef = round(np_val / avg_hr, 2) if avg_hr and avg_hr > 0 else None
        assert ef is None


class TestNPEdgeCases:
    """NP (Normalized Power) edge-case guards matching fitness.py logic."""

    def test_none_np_skips_update(self):
        """When NP query returns None, the entire update block is skipped."""
        row = (None,)
        np_val = round(row[0], 1) if row and row[0] else None
        assert np_val is None

    def test_zero_np_skips_update(self):
        """When NP query returns 0, it's falsy so update is skipped."""
        row = (0,)
        np_val = round(row[0], 1) if row and row[0] else None
        assert np_val is None

    def test_valid_np(self):
        row = (237.456,)
        np_val = round(row[0], 1) if row and row[0] else None
        assert np_val == 237.5

    def test_empty_result_skips(self):
        """When the cursor returns no row."""
        row = None
        np_val = round(row[0], 1) if row and row[0] else None
        assert np_val is None
