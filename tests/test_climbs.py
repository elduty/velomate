"""Tests for ingestor/climbs.py — climb detection from elevation profiles."""

import sys
from pathlib import Path

import pytest

_ingestor_dir = Path(__file__).resolve().parent.parent / "ingestor"
if str(_ingestor_dir) not in sys.path:
    sys.path.insert(0, str(_ingestor_dir))

from climbs import smooth_altitude, detect_climbs


class TestSmoothAltitude:
    def test_flat_returns_same(self):
        alt = [100.0] * 50
        result = smooth_altitude(alt, window=20)
        assert len(result) == 50
        assert all(a == pytest.approx(100.0) for a in result)

    def test_smooths_spike(self):
        alt = [100.0] * 50
        alt[25] = 200.0  # spike
        result = smooth_altitude(alt, window=20)
        # Spike should be significantly reduced
        assert result[25] < 115.0

    def test_empty_returns_empty(self):
        assert smooth_altitude([], 20) == []


class TestDetectClimbs:
    def test_flat_road_no_climbs(self):
        alt = [100.0] * 1000
        dist = [float(i) for i in range(1000)]  # 1m per second
        assert detect_climbs(alt, dist) == []

    def test_single_sustained_climb(self):
        """300m at 6% = 5000m long. Should detect as Cat 3."""
        n = 5000
        alt = [100.0 + (i * 0.06) for i in range(n)]  # 6% grade
        dist = [float(i) for i in range(n)]
        climbs = detect_climbs(alt, dist)
        assert len(climbs) == 1
        assert climbs[0]["gain_m"] >= 250
        assert climbs[0]["category"] == "Cat 3"
        assert climbs[0]["avg_grade"] == pytest.approx(6.0, abs=0.5)

    def test_small_dip_merged(self):
        """Two uphills separated by 5m dip should merge (< 10m tolerance)."""
        alt = (
            [100.0 + i * 0.08 for i in range(500)]     # climb 40m at 8%
            + [140.0 - i * 0.01 for i in range(50)]     # dip 0.5m
            + [139.5 + i * 0.08 for i in range(500)]    # climb another 40m
        )
        dist = [float(i) for i in range(len(alt))]
        climbs = detect_climbs(alt, dist)
        # Should merge into one climb of ~80m
        assert len(climbs) == 1
        assert climbs[0]["gain_m"] >= 70

    def test_large_descent_splits(self):
        """Two uphills separated by 40m descent should be two separate climbs."""
        alt = (
            [100.0 + i * 0.1 for i in range(600)]       # climb 60m at 10%
            + [160.0 - i * 0.1 for i in range(400)]      # descend 40m (> 30m tolerance)
            + [120.0 + i * 0.1 for i in range(600)]       # climb 60m at 10%
        )
        dist = [float(i) for i in range(len(alt))]
        climbs = detect_climbs(alt, dist)
        assert len(climbs) == 2

    def test_gradual_incline_filtered_by_gradient(self):
        """1% gradient over a long distance — not a climb."""
        n = 10000
        alt = [100.0 + i * 0.01 for i in range(n)]  # 1% grade
        dist = [float(i) for i in range(n)]
        climbs = detect_climbs(alt, dist, min_gradient=3.0)
        assert len(climbs) == 0

    def test_category_classification(self):
        """Verify category thresholds."""
        def make_climb(gain, gradient=5.0):
            length = int(gain / (gradient / 100))
            alt = [100.0 + i * (gradient / 100) for i in range(length)]
            dist = [float(i) for i in range(length)]
            return detect_climbs(alt, dist)

        assert make_climb(50)[0]["category"] == "Climb"
        assert make_climb(120)[0]["category"] == "Cat 4"
        assert make_climb(300)[0]["category"] == "Cat 3"
        assert make_climb(600)[0]["category"] == "Cat 2"
        assert make_climb(1100)[0]["category"] == "Cat 1"
        assert make_climb(1600)[0]["category"] == "HC"

    def test_empty_input(self):
        assert detect_climbs([], []) == []
        assert detect_climbs([100.0], [0.0]) == []

    def test_climb_extends_to_end(self):
        """Climb that doesn't end before the ride ends."""
        alt = [100.0 + i * 0.05 for i in range(1000)]  # 5% for 1000s = 50m
        dist = [float(i) for i in range(1000)]
        climbs = detect_climbs(alt, dist)
        assert len(climbs) == 1
        assert climbs[0]["duration_s"] > 900
