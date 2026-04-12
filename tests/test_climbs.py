"""Tests for ingestor/climbs.py — climb detection from elevation profiles."""

import sys
import math
from pathlib import Path

import pytest

_ingestor_dir = Path(__file__).resolve().parent.parent / "ingestor"
if str(_ingestor_dir) not in sys.path:
    sys.path.insert(0, str(_ingestor_dir))

from climbs import smooth_altitude, detect_climbs, classify_climb


class TestSmoothAltitude:
    def test_flat_returns_same(self):
        alt = [100.0] * 50
        result = smooth_altitude(alt, window=20)
        assert len(result) == 50
        assert all(a == pytest.approx(100.0) for a in result)

    def test_smooths_spike(self):
        alt = [100.0] * 50
        alt[25] = 200.0
        result = smooth_altitude(alt, window=20)
        assert result[25] < 115.0

    def test_empty_returns_empty(self):
        assert smooth_altitude([], 20) == []


class TestRDP:
    """Test RDP indirectly through detect_climbs behaviour."""

    def test_high_epsilon_merges_small_bumps(self):
        """With a high epsilon, small undulations are smoothed away."""
        n = 2000
        alt = [100.0 + i * 0.05 + 2 * math.sin(i * 0.1) for i in range(n)]
        dist = [float(i) for i in range(n)]
        # High epsilon should find 1 climb (bumps absorbed)
        climbs = detect_climbs(alt, dist, epsilon=20.0)
        assert len(climbs) == 1

    def test_low_epsilon_preserves_detail(self):
        """With a low epsilon, more structure is preserved."""
        n = 4000
        alt = []
        for i in range(n):
            base = 100 + 60 * (0.5 + 0.5 * math.sin(math.pi * i / n - math.pi / 2))
            alt.append(base + 15 * math.sin(2 * math.pi * i / 400))
        dist = [float(i) * 5 for i in range(n)]
        low = detect_climbs(alt, dist, epsilon=3.0)
        high = detect_climbs(alt, dist, epsilon=30.0)
        assert len(low) >= len(high)


class TestClassifyClimb:
    def test_strava_categories(self):
        assert classify_climb(1000, 8.0) == "Cat 4"    # 8000
        assert classify_climb(2000, 8.0) == "Cat 3"    # 16000
        assert classify_climb(4000, 8.0) == "Cat 2"    # 32000
        assert classify_climb(8000, 8.0) == "Cat 1"    # 64000
        assert classify_climb(10000, 8.0) == "HC"       # 80000

    def test_sub_threshold_is_climb(self):
        assert classify_climb(500, 3.0) == "Climb"      # 1500
        assert classify_climb(1000, 5.0) == "Climb"      # 5000

    def test_boundary_values(self):
        assert classify_climb(1000, 7.9) == "Climb"      # 7900 < 8000
        assert classify_climb(1000, 8.0) == "Cat 4"      # 8000 exactly


class TestDetectClimbs:
    def test_flat_road_no_climbs(self):
        alt = [100.0] * 1000
        dist = [float(i) for i in range(1000)]
        assert detect_climbs(alt, dist) == []

    def test_single_sustained_climb(self):
        """2km at 5% = 100m gain. Should detect."""
        n = 2000
        alt = [100.0 + i * 0.05 for i in range(n)]
        dist = [float(i) for i in range(n)]
        climbs = detect_climbs(alt, dist)
        assert len(climbs) == 1
        assert climbs[0]["avg_grade"] == pytest.approx(5.0, abs=0.5)
        assert climbs[0]["score"] > 8000

    def test_climb_with_small_dip_merged(self):
        """Two uphills with a small dip — RDP should merge into one climb."""
        alt = (
            [100.0 + i * 0.08 for i in range(500)]     # climb 40m
            + [140.0 - i * 0.01 for i in range(50)]     # dip 0.5m
            + [139.5 + i * 0.08 for i in range(500)]    # climb 40m more
        )
        dist = [float(i) for i in range(len(alt))]
        climbs = detect_climbs(alt, dist)
        assert len(climbs) == 1
        assert climbs[0]["gain_m"] >= 70

    def test_large_descent_splits(self):
        """Two climbs separated by a big descent — should be separate."""
        alt = (
            [100.0 + i * 0.1 for i in range(600)]       # climb 60m
            + [160.0 - i * 0.1 for i in range(400)]      # descend 40m
            + [120.0 + i * 0.1 for i in range(600)]       # climb 60m
        )
        dist = [float(i) for i in range(len(alt))]
        climbs = detect_climbs(alt, dist)
        assert len(climbs) == 2

    def test_gradual_incline_filtered(self):
        """1% gradient — below 2% minimum."""
        n = 10000
        alt = [100.0 + i * 0.01 for i in range(n)]
        dist = [float(i) for i in range(n)]
        climbs = detect_climbs(alt, dist)
        assert len(climbs) == 0

    def test_short_climb_filtered(self):
        """Steep but only 100m — below 200m minimum."""
        n = 100
        alt = [100.0 + i * 0.1 for i in range(n)]
        dist = [float(i) for i in range(n)]
        climbs = detect_climbs(alt, dist, min_distance_m=200)
        assert len(climbs) == 0

    def test_empty_input(self):
        assert detect_climbs([], []) == []
        assert detect_climbs([100.0], [0.0]) == []

    def test_climb_extends_to_end(self):
        """Climb that doesn't descend before ride ends."""
        n = 2000
        alt = [100.0 + i * 0.05 for i in range(n)]  # 5% for 2km
        dist = [float(i) for i in range(n)]
        climbs = detect_climbs(alt, dist)
        assert len(climbs) == 1
        assert climbs[0]["duration_s"] > 1500

    def test_rolling_terrain_detects_hills(self):
        """Rolling terrain with undulations on a rising base."""
        n = 4000
        alt = []
        for i in range(n):
            base = 4 + 136 * (0.5 + 0.5 * math.sin(math.pi * i / n - math.pi / 2))
            undulation = 20 * math.sin(2 * math.pi * i / 400)
            alt.append(base + undulation)
        dist = [float(i) * 5 for i in range(n)]
        climbs = detect_climbs(alt, dist)
        assert len(climbs) >= 2

    def test_time_offsets_for_duration(self):
        """Duration uses real time_offsets when provided."""
        n = 1000
        alt = [100.0 + i * 0.05 for i in range(n)]
        dist = [float(i) for i in range(n)]
        offsets = list(range(500)) + list(range(1500, 2000))
        climbs = detect_climbs(alt, dist, time_offsets=offsets)
        assert len(climbs) == 1
        assert climbs[0]["duration_s"] > 1400

    def test_strava_reference_climb(self):
        """476m at 8.5% = score 4046 = Climb. Should be detected."""
        n = 476
        alt = [50.0 + i * 0.085 for i in range(n)]  # 8.5% grade
        dist = [float(i) for i in range(n)]
        climbs = detect_climbs(alt, dist, min_distance_m=200)
        assert len(climbs) == 1
        assert climbs[0]["avg_grade"] == pytest.approx(8.5, abs=0.5)

    def test_score_field_present(self):
        n = 2000
        alt = [100.0 + i * 0.06 for i in range(n)]
        dist = [float(i) for i in range(n)]
        climbs = detect_climbs(alt, dist)
        assert len(climbs) == 1
        assert "score" in climbs[0]
        assert climbs[0]["score"] > 0
