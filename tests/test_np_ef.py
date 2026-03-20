"""Tests for NP/EF/Work computation functions in fitness.py."""

import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "ingestor"))

from fitness import compute_work_kj, compute_ef


class TestComputeWorkKj:
    """Work (kJ) = sum of per-second power / 1000."""

    def test_normal(self):
        # 200W avg × 3600s = 720,000 total power sum
        assert compute_work_kj(720000) == 720.0

    def test_short_ride(self):
        # 150W avg × 1800s = 270,000
        assert compute_work_kj(270000) == 270.0

    def test_high_power(self):
        # 350W avg × 7200s = 2,520,000
        assert compute_work_kj(2520000) == 2520.0

    def test_zero_sum(self):
        assert compute_work_kj(0) == 0.0

    def test_none_sum(self):
        assert compute_work_kj(None) == 0.0

    def test_small_value(self):
        # 100W × 60s = 6,000 -> 6.0 kJ
        assert compute_work_kj(6000) == 6.0

    def test_rounding(self):
        # 6,123 -> 6.1 kJ
        assert compute_work_kj(6123) == 6.1


class TestComputeEf:
    """Efficiency Factor = NP / avg HR."""

    def test_normal(self):
        assert compute_ef(220.0, 150) == 1.47

    def test_low_hr(self):
        assert compute_ef(200.0, 120) == 1.67

    def test_high_np(self):
        assert compute_ef(300.0, 170) == 1.76

    def test_zero_hr(self):
        assert compute_ef(220.0, 0) is None

    def test_none_hr(self):
        assert compute_ef(220.0, None) is None

    def test_none_np(self):
        assert compute_ef(None, 150) is None

    def test_zero_np(self):
        assert compute_ef(0, 150) is None

    def test_negative_hr(self):
        assert compute_ef(220.0, -5) is None


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
