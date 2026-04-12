"""Tests for ingestor/climbs.py — climb detection from elevation profiles."""

import sys
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


class TestClassifyClimb:
    def test_strava_categories(self):
        # score = length_m * gradient_%
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
        """2km at 5% = Cat 3 (score 10000). Should detect."""
        n = 2000
        alt = [100.0 + i * 0.05 for i in range(n)]  # 5% grade, 100m gain
        dist = [float(i) for i in range(n)]
        climbs = detect_climbs(alt, dist, min_distance_m=500)
        assert len(climbs) == 1
        assert climbs[0]["avg_grade"] == pytest.approx(5.0, abs=0.5)
        assert climbs[0]["category"] == "Cat 4"  # ~10000 score
        assert climbs[0]["score"] > 8000

    def test_small_dip_absorbed_by_dynamic_threshold(self):
        """Two uphills separated by 5m dip on a 50m climb (20% = 10m tolerance)."""
        alt = (
            [100.0 + i * 0.08 for i in range(500)]     # climb 40m at 8%
            + [140.0 - i * 0.01 for i in range(50)]     # dip 0.5m
            + [139.5 + i * 0.08 for i in range(500)]    # climb another 40m
        )
        dist = [float(i) for i in range(len(alt))]
        climbs = detect_climbs(alt, dist, min_distance_m=500)
        # Should merge into one climb of ~80m
        assert len(climbs) == 1
        assert climbs[0]["gain_m"] >= 70

    def test_large_descent_splits_via_dynamic_threshold(self):
        """Two 60m climbs separated by 20m descent (> 20% of 60m = 12m)."""
        alt = (
            [100.0 + i * 0.1 for i in range(600)]       # climb 60m
            + [160.0 - i * 0.1 for i in range(200)]      # descend 20m
            + [140.0 + i * 0.1 for i in range(600)]       # climb 60m
        )
        dist = [float(i) for i in range(len(alt))]
        climbs = detect_climbs(alt, dist, min_distance_m=500)
        assert len(climbs) == 2

    def test_gradual_incline_filtered_by_gradient(self):
        """1% gradient — below 2% minimum."""
        n = 10000
        alt = [100.0 + i * 0.01 for i in range(n)]
        dist = [float(i) for i in range(n)]
        climbs = detect_climbs(alt, dist)
        assert len(climbs) == 0

    def test_short_climb_filtered_by_distance(self):
        """Steep but only 300m long — below 500m minimum."""
        n = 300
        alt = [100.0 + i * 0.1 for i in range(n)]  # 10% for 300m
        dist = [float(i) for i in range(n)]
        climbs = detect_climbs(alt, dist, min_distance_m=500)
        assert len(climbs) == 0

    def test_empty_input(self):
        assert detect_climbs([], []) == []
        assert detect_climbs([100.0], [0.0]) == []

    def test_climb_extends_to_end(self):
        """Climb that doesn't end before the ride ends."""
        n = 2000
        alt = [100.0 + i * 0.05 for i in range(n)]  # 5% for 2km
        dist = [float(i) for i in range(n)]
        climbs = detect_climbs(alt, dist, min_distance_m=500)
        assert len(climbs) == 1
        assert climbs[0]["duration_s"] > 1500

    def test_time_offsets_used_for_duration(self):
        """When time_offsets provided, duration uses real time not indices."""
        n = 1000
        alt = [100.0 + i * 0.05 for i in range(n)]
        dist = [float(i) for i in range(n)]
        # Simulate gaps: time jumps from 500 to 1500
        offsets = list(range(500)) + list(range(1500, 2000))
        climbs = detect_climbs(alt, dist, min_distance_m=500, time_offsets=offsets)
        assert len(climbs) == 1
        # Duration should be ~1500s (real time), not 1000 (index count)
        assert climbs[0]["duration_s"] > 1400

    def test_rolling_terrain_detects_individual_hills(self):
        """Rolling terrain with 30m undulations on a rising base — should
        detect individual hills, not merge everything into one."""
        import math
        n = 4000
        alt = []
        for i in range(n):
            base = 4 + 136 * (0.5 + 0.5 * math.sin(math.pi * i / n - math.pi / 2))
            undulation = 20 * math.sin(2 * math.pi * i / 400)
            alt.append(base + undulation)
        dist = [float(i) * 5 for i in range(n)]  # 5m/s = 18km/h
        climbs = detect_climbs(alt, dist, min_distance_m=500)
        # Should find multiple hills, not 0 or 1
        assert len(climbs) >= 2

    def test_score_field_present(self):
        """Detected climbs include the Strava-style score."""
        n = 2000
        alt = [100.0 + i * 0.06 for i in range(n)]  # 6%, 120m gain
        dist = [float(i) for i in range(n)]
        climbs = detect_climbs(alt, dist, min_distance_m=500)
        assert len(climbs) == 1
        assert "score" in climbs[0]
        assert climbs[0]["score"] > 0
