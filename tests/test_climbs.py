"""Tests for ingestor/climbs.py — climb detection from elevation profiles."""

import sys
import math
from pathlib import Path

import pytest

_ingestor_dir = Path(__file__).resolve().parent.parent / "ingestor"
if str(_ingestor_dir) not in sys.path:
    sys.path.insert(0, str(_ingestor_dir))

from climbs import smooth_altitude, detect_climbs, classify_climb, cumulative_distance_m


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


# Sampling that gives the 2195 m and 3195 m figures below exactly: 1 Hz at this
# speed, smoothed with the same 20 s window detect_climbs_for_rides uses.
RIDE_SPEED_MS = 19.9 / 3.6


def _ride(*legs):
    """A 1 Hz ride over `legs` of (length_m, grade_%), as (altitudes, distances).

    Altitudes are smoothed the way the real pipeline smooths them.
    """
    bends = [(0.0, 100.0)]
    for length, grade in legs:
        bends.append((bends[-1][0] + length, bends[-1][1] + length * grade / 100))

    altitudes, distances = [], []
    leg = 0
    for second in range(int(bends[-1][0] / RIDE_SPEED_MS) + 1):
        distance = second * RIDE_SPEED_MS
        while distance > bends[leg + 1][0]:
            leg += 1
        (x0, y0), (x1, y1) = bends[leg], bends[leg + 1]
        altitudes.append(y0 + (distance - x0) * (y1 - y0) / (x1 - x0))
        distances.append(distance)
    return smooth_altitude(altitudes, 20), distances


def _summary(climbs):
    return [(c["length_m"], c["avg_grade"]) for c in climbs]


class TestFlatApproach:
    """A true 1200 m climb at 6%, with road around it.

    By hand: 1200 m x 6% = 72 m of gain. The 20 s window is 10 s either side, or
    55 m at this speed, so smoothing can move the climb's length by at most 55 m
    and takes up to 3 m off its gain by rounding the corners. Whatever road
    surrounds it, a correct answer is 1200 +/- 55 m with 69-72 m of gain, which
    bounds the grade between 69/1255 = 5.5% and 72/1145 = 6.3%.
    """

    CLIMB = (1200, 6.0)

    def _assert_is_the_true_climb(self, climbs):
        assert len(climbs) == 1, _summary(climbs)
        assert climbs[0]["length_m"] == pytest.approx(1200, abs=55)
        assert 5.5 <= climbs[0]["avg_grade"] <= 6.3
        assert 69 <= climbs[0]["gain_m"] <= 72

    @pytest.mark.parametrize("legs, wrong", [
        ([(1000, 0.0), CLIMB], [(2195, 3.2)]),
        ([(1000, 0.0), CLIMB, (1000, 0.0)], [(3195, 2.3)]),
        ([(3000, 0.0), CLIMB], []),
    ], ids=["1 km before", "1 km both sides", "3 km before"])
    def test_these_rides_are_the_reported_failures(self, legs, wrong):
        """With no continuation grade, a segment only has to rise at all.

        Pins the fixtures to the reported figures, so the tests below cannot
        start passing because the fixture stopped provoking the defect.
        """
        altitudes, distances = _ride(*legs)
        climbs = detect_climbs(altitudes, distances, continuation_grade=0.0)
        assert _summary(climbs) == wrong

    def test_one_km_flat_before(self):
        self._assert_is_the_true_climb(detect_climbs(*_ride((1000, 0.0), self.CLIMB)))

    def test_flat_on_both_sides(self):
        self._assert_is_the_true_climb(
            detect_climbs(*_ride((1000, 0.0), self.CLIMB, (1000, 0.0))))

    def test_three_km_flat_before_is_not_lost(self):
        self._assert_is_the_true_climb(detect_climbs(*_ride((3000, 0.0), self.CLIMB)))

    def test_false_flat_below_the_continuation_grade_stays_out(self):
        self._assert_is_the_true_climb(detect_climbs(*_ride((3000, 0.5), self.CLIMB)))

    def test_false_flat_above_the_continuation_grade_stays_in(self):
        """The other side of the rule: 2 km at 1.5% then the climb is one climb.

        By hand: 30 m + 72 m = 102 m over 3200 m, 3.2%.
        """
        climbs = detect_climbs(*_ride((2000, 1.5), self.CLIMB))
        assert len(climbs) == 1, _summary(climbs)
        assert climbs[0]["length_m"] == pytest.approx(3200, abs=55)
        assert climbs[0]["avg_grade"] == pytest.approx(3.2, abs=0.2)

    def test_flat_between_two_ramps_splits_them(self):
        """Neither ramp may take the other's distance: each is 72 m over 1200 m."""
        climbs = detect_climbs(*_ride(self.CLIMB, (1000, 0.0), self.CLIMB))
        assert len(climbs) == 2, _summary(climbs)
        for climb in climbs:
            assert climb["length_m"] == pytest.approx(1200, abs=55)
            assert 5.5 <= climb["avg_grade"] <= 6.3

    def test_altitude_change_with_no_distance_does_not_raise(self):
        """A stop leaves many samples at one distance, and barometric drift can
        move the altitude while they pass, so a segment can have no length."""
        altitudes = [100.0] * 300 + [130.0] * 300
        distances = [float(i) for i in range(300)] + [299.0] * 300
        assert detect_climbs(altitudes, distances) == []


# 2 x pi x 6371000 / 360: one degree of latitude on the sphere haversine_m uses.
METRES_PER_DEGREE = 111194.93


def _north(metres, lat0=38.7, lng=-9.14):
    """The position `metres` due north of the start, so distances are known by hand."""
    return (lat0 + metres / METRES_PER_DEGREE, lng)


class TestCumulativeDistance:
    def test_measures_between_fixes(self):
        points = [_north(0), _north(100), _north(300)]
        assert cumulative_distance_m(points) == pytest.approx([0, 100, 300], abs=0.01)

    def test_a_stop_adds_nothing(self):
        points = [_north(0), _north(500), _north(500), _north(500), _north(800)]
        assert cumulative_distance_m(points) == pytest.approx([0, 500, 500, 500, 800], abs=0.01)

    def test_a_sample_with_no_fix_is_measured_across(self):
        """The next fix measures from the last one, so nothing is lost or raised.
        A half-fix counts as none: lat and lng are independently nullable."""
        points = [_north(0), (None, None), (38.7, None), (None, -9.14), _north(300)]
        assert cumulative_distance_m(points) == pytest.approx([0, 0, 0, 0, 300], abs=0.01)

    def test_samples_before_the_first_fix_are_at_zero(self):
        points = [(None, None), _north(0), _north(100)]
        assert cumulative_distance_m(points) == pytest.approx([0, 0, 100], abs=0.01)

    def test_a_ride_with_no_gps_has_no_distance(self):
        assert cumulative_distance_m([(None, None)] * 3) == [0.0, 0.0, 0.0]

    def test_empty(self):
        assert cumulative_distance_m([]) == []


class TestRecordingGaps:
    """The same true climb, 1200 m at 6%, with holes in the stream. Only samples
    the device recorded exist, so a gap is a jump in time between two samples.

    By hand: 72 m of gain over 1200 m, whatever happened during the gap.
    """

    # Gives the reported 1712 m exactly. Slow, so the 20 s window spans only 17 m
    # each way: 1200 +/- 17 m, and 70-72 m of gain, bounds the grade at 5.7-6.2%.
    STOP_SPEED_MS = 6.17 / 3.6

    def _stopped_climb(self):
        """Per second up the climb, with a 300 s stop halfway through it."""
        speed = self.STOP_SPEED_MS
        samples = int(1200 / speed)
        offsets, travelled = [], []
        offset = 0
        for i in range(samples + 1):
            if i == samples // 2 + 1:
                offset += 300
            elif i:
                offset += 1
            offsets.append(offset)
            travelled.append(i * speed)
        altitudes = smooth_altitude([100.0 + d * 0.06 for d in travelled], 20)
        return offsets, altitudes, [_north(d) for d in travelled]

    def _speed_times_elapsed(self, offsets):
        distances = [0.0]
        for previous, offset in zip(offsets, offsets[1:]):
            distances.append(distances[-1] + self.STOP_SPEED_MS * (offset - previous))
        return distances

    def test_speed_times_elapsed_time_is_the_reported_failure(self):
        """Pins the fixture: the first sample after the stop takes speed x 300 s."""
        offsets, altitudes, _ = self._stopped_climb()
        climbs = detect_climbs(altitudes, self._speed_times_elapsed(offsets),
                               time_offsets=offsets, continuation_grade=0.0)
        assert _summary(climbs) == [(1712, 4.1)]

    def test_speed_times_elapsed_time_would_split_the_climb_at_the_stop(self):
        """The distance a stop invents reads as a flat segment. Why the two fixes
        go together: with the grade rule alone, a stop cuts a climb in two."""
        offsets, altitudes, _ = self._stopped_climb()
        climbs = detect_climbs(altitudes, self._speed_times_elapsed(offsets),
                               time_offsets=offsets)
        assert len(climbs) == 2, _summary(climbs)

    def test_a_stop_mid_climb_adds_no_distance(self):
        offsets, altitudes, points = self._stopped_climb()
        climbs = detect_climbs(
            altitudes, cumulative_distance_m(points), time_offsets=offsets)
        assert len(climbs) == 1, _summary(climbs)
        assert climbs[0]["length_m"] == pytest.approx(1200, abs=17)
        assert 5.7 <= climbs[0]["avg_grade"] <= 6.2

    def _gapped_climb(self, keep):
        """A climb at 5 m/s, keeping only the seconds where keep(second) is true.

        Unsmoothed, because these two are about distance, so by hand the answer
        is exactly 1200 m at 6%.
        """
        seconds = [s for s in range(241) if keep(s)]
        altitudes = [100.0 + 5 * s * 0.06 for s in seconds]
        return detect_climbs(altitudes, cumulative_distance_m([_north(5 * s) for s in seconds]),
                             time_offsets=seconds)

    def test_a_dropout_while_riding_keeps_its_distance(self):
        """60 s missing at 5 m/s is 300 m ridden with no samples. Counting that
        as one second would report about 900 m at 8%."""
        climbs = self._gapped_climb(lambda s: not 90 < s < 150)
        assert _summary(climbs) == [(1200, 6.0)]

    def test_sparse_sampling_keeps_its_distance(self):
        """Not every device records every second. 5 s sampling covers the same
        road, where counting one second per sample would give 240 m."""
        climbs = self._gapped_climb(lambda s: s % 5 == 0)
        assert _summary(climbs) == [(1200, 6.0)]

    def test_a_ride_with_no_gps_reports_no_climb(self):
        """Altitude alone cannot give a length. Nothing is invented."""
        altitudes = [100.0 + i * 0.3 for i in range(600)]
        assert detect_climbs(altitudes, cumulative_distance_m([(None, None)] * 600)) == []
