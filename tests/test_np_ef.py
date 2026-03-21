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

    def test_negative_np(self):
        """Negative NP is physically impossible — currently not guarded,
        returns a negative value. P3: consider adding np <= 0 guard."""
        assert compute_ef(-100.0, 150) == -0.67

    def test_very_small_np(self):
        """Very small but positive NP should return a value, not be treated as zero."""
        result = compute_ef(0.001, 150)
        assert result is not None
        assert result == 0.0  # 0.001/150 rounds to 0.00

    def test_very_high_hr(self):
        """High HR (220) is valid — should return a value."""
        result = compute_ef(200.0, 220)
        assert result is not None
        assert result == 0.91  # 200/220 = 0.909...

    def test_np_zero_float(self):
        """NP=0.0 is falsy — should return None."""
        assert compute_ef(0.0, 150) is None
