"""Tests for EF computation function in fitness.py."""

import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "ingestor"))

from fitness import compute_ef


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
