"""Tests for auto interval detection in ingestor/intervals.py."""

import random

import pytest

from intervals import INTERVAL_CLASSES, classify_interval, detect_intervals

FTP = 200

# The class table, restated here on purpose: an oracle built from the module's
# own constants could not notice a wrong band edge.
SURGE_CEILING_S = 60
BANDS = (
    (1.20, "anaerobic"),
    (1.05, "vo2"),
    (0.95, "threshold"),
    (0.88, "sweetspot"),
    (0.75, "tempo"),
)

DETECTOR_MIN_S = 30
DURATIONS = (30, 31, 45, 59, 60, 61, 75, 90, 119, 120, 121, 299, 300, 301,
             899, 900, 1199, 1200, 1201, 1215, 3600, 3601)
INTENSITIES = (0.75, 0.76, 0.80, 0.83, 0.84, 0.85, 0.86, 0.87, 0.88, 0.90, 0.92,
               0.94, 0.95, 0.96, 1.00, 1.04, 1.05, 1.06, 1.10, 1.19, 1.20, 1.21,
               1.50, 1.51, 2.00, 3.00)


def expected_class(duration_s, pct):
    if duration_s < SURGE_CEILING_S:
        return "surge"
    for lower_bound, name in BANDS:
        if pct >= lower_bound:
            return name
    raise AssertionError(f"{pct} is below the detection floor")


# --- classify_interval ---

class TestClassifyInterval:
    """Contiguous %FTP bands, with a surge class under the duration ceiling."""

    @pytest.mark.parametrize("pct,expected", [
        (0.75, "tempo"), (0.8799, "tempo"),
        (0.88, "sweetspot"), (0.9499, "sweetspot"),
        (0.95, "threshold"), (1.0499, "threshold"),
        (1.05, "vo2"), (1.1999, "vo2"),
        (1.20, "anaerobic"), (1.50, "anaerobic"), (3.00, "anaerobic"),
    ])
    def test_band_edges(self, pct, expected):
        """Each edge from both sides: 0.0001 either way changes the class."""
        assert classify_interval(600, pct * FTP, ftp=FTP) == expected

    @pytest.mark.parametrize("duration,pct,expected", [
        (480, 1.06, "vo2"),         # 8 min at 106%
        (1215, 1.04, "threshold"),  # a 20-min FTP test as the detector sees it
        (600, 0.90, "sweetspot"),   # 10 min at 90%
        (600, 0.92, "sweetspot"),   # 10 min at 92%
        (90, 1.10, "vo2"),          # 90 s at 110%
        (1210, 0.95, "threshold"),  # one rep of 2x20 at 95%
    ])
    def test_efforts_the_old_buckets_discarded(self, duration, pct, expected):
        """Every one of these returned None under duration-and-intensity buckets."""
        assert classify_interval(duration, pct * FTP, ftp=FTP) == expected

    @pytest.mark.parametrize("duration,pct,expected", [
        (30, 1.40, "surge"),
        (59, 1.40, "surge"),
        (59, 2.50, "surge"),
        (45, 0.80, "surge"),
        (60, 1.40, "anaerobic"),
        (75, 1.30, "anaerobic"),
        (90, 1.25, "anaerobic"),
        (90, 2.00, "anaerobic"),
    ])
    def test_surge_is_exactly_the_sub_minute_efforts(self, duration, pct, expected):
        """A deliberate 60-90 s effort stays anaerobic; one second less is a surge."""
        assert classify_interval(duration, pct * FTP, ftp=FTP) == expected

    def test_sprint_is_gone(self):
        """No effort the detector can report is called a sprint any more."""
        assert "sprint" not in INTERVAL_CLASSES
        assert classify_interval(1, 3.0 * FTP, ftp=FTP) == "surge"
        assert classify_interval(29, 3.0 * FTP, ftp=FTP) == "surge"
        assert classify_interval(30, 3.0 * FTP, ftp=FTP) == "surge"
        assert classify_interval(120, 3.0 * FTP, ftp=FTP) == "anaerobic"

    @pytest.mark.parametrize("duration,watts", [(30, 100), (2400, 120), (600, 149.9)])
    def test_below_the_detection_floor_is_not_an_effort(self, duration, watts):
        assert classify_interval(duration, watts, ftp=FTP) is None

    @pytest.mark.parametrize("duration,watts,ftp", [
        (0, 300, 200), (-1, 300, 200), (60, 0, 200), (60, 300, 0), (60, 300, -5),
    ])
    def test_invalid_input(self, duration, watts, ftp):
        assert classify_interval(duration, watts, ftp=ftp) is None

    @pytest.mark.parametrize("ftp", [128, 150, 173, 187.5, 200, 333.3])
    def test_no_holes(self, ftp):
        """Every duration from the detector's minimum up, at every intensity from
        the detection floor up, gets exactly the class the table gives it."""
        missing = []
        wrong = []
        for duration in list(range(DETECTOR_MIN_S, 1300)) + [1800, 3600, 7200, 18000]:
            for hundredth in range(75, 301):
                pct = hundredth / 100
                watts = ftp * pct
                got = classify_interval(duration, watts, ftp=ftp)
                if got is None:
                    missing.append((duration, pct))
                elif got != expected_class(duration, pct):
                    wrong.append((duration, pct, got))
        assert missing == []
        assert wrong == []

    @pytest.mark.parametrize("edge,ftp,expected", [
        (0.75, 170.7, "tempo"),
        (0.75, 171.2, "tempo"),
        (0.88, 151.6, "sweetspot"),
        (0.95, 150.2, "threshold"),
        (1.20, 214.1, "anaerobic"),
    ])
    def test_a_mean_exactly_on_an_edge_stays_on_it(self, edge, ftp, expected):
        """The detector compares watts against ftp * edge, so the classifier has
        to as well. Per-ride FTP is a float, and for these values dividing instead
        rounds the ratio to just under the edge: at 0.75 that would drop the effort."""
        assert (ftp * edge) / ftp < edge, "fixture is wrong: this FTP does not round under"
        assert classify_interval(600, ftp * edge, ftp=ftp) == expected


# --- detect_intervals ---

def ride_with(effort_watts, duration_s, easy_watts=100, easy_s=300):
    return [easy_watts] * easy_s + [effort_watts] * duration_s + [easy_watts] * easy_s


def shape(intervals):
    return [(iv["start_offset_s"], iv["duration_s"], iv["classification"]) for iv in intervals]


class TestDetectIntervals:
    """Efforts at or above 75% FTP; anything harder than tempo stays separate."""

    def test_empty(self):
        assert detect_intervals([], ftp=FTP) == []

    def test_no_effort(self):
        """All zone 2 — no intervals detected."""
        assert detect_intervals([120] * 600, ftp=FTP) == []

    def test_every_sustained_effort_is_detected_and_classified(self):
        """The no-holes property through the detector, not just the classifier:
        one block of each duration and intensity comes back as exactly that block."""
        failures = []
        for duration in DURATIONS:
            for pct in INTENSITIES:
                got = shape(detect_intervals(ride_with(FTP * pct, duration), ftp=FTP))
                want = [(300, duration, expected_class(duration, pct))]
                if got != want:
                    failures.append((duration, pct, got))
        assert failures == []

    def test_detection_starts_at_75_percent(self):
        """The old 85% default found nothing here."""
        assert shape(detect_intervals(ride_with(160, 1800), ftp=FTP)) == [(300, 1800, "tempo")]

    def test_just_below_the_floor_is_not_detected(self):
        assert detect_intervals(ride_with(149, 1800), ftp=FTP) == []

    def test_minimum_duration(self):
        """29 s is under the detector's minimum; 30 s is not."""
        assert detect_intervals(ride_with(250, 29), ftp=FTP) == []
        assert shape(detect_intervals(ride_with(250, 30), ftp=FTP)) == [(300, 30, "surge")]

    def test_single_threshold_interval(self):
        """10 min warmup, 8 min threshold, 10 min cooldown."""
        samples = [120] * 600 + [200] * 480 + [120] * 600
        intervals = detect_intervals(samples, ftp=FTP)
        assert shape(intervals) == [(600, 480, "threshold")]
        assert intervals[0]["avg_power"] == 200

    def test_interval_fields(self):
        iv = detect_intervals(ride_with(210, 120), ftp=FTP)[0]
        assert iv["start_offset_s"] == 300
        assert iv["duration_s"] == 120
        assert iv["avg_power"] == 210.0
        assert iv["max_power"] == 210.0
        assert iv["classification"] == "vo2"
        assert 0 < iv["np"] <= 210

    def test_repeated_vo2_reps(self):
        """Four 2-min reps at 115% with 1-min easy recovery."""
        samples = [100] * 300
        for _ in range(4):
            samples += [230] * 120 + [100] * 60
        samples += [100] * 300
        assert shape(detect_intervals(samples, ftp=FTP)) == [
            (300, 120, "vo2"), (480, 120, "vo2"), (660, 120, "vo2"), (840, 120, "vo2"),
        ]

    def test_gap_is_bridged(self):
        """A 5-second dip in a threshold effort does not split it."""
        samples = [100] * 300 + [200] * 150 + [100] * 5 + [200] * 150 + [100] * 300
        assert shape(detect_intervals(samples, ftp=FTP)) == [(300, 305, "threshold")]

    def test_gap_longer_than_the_tolerance_splits(self):
        samples = [100] * 300 + [200] * 150 + [100] * 11 + [200] * 150 + [100] * 300
        assert shape(detect_intervals(samples, ftp=FTP)) == [
            (300, 150, "threshold"), (461, 150, "threshold"),
        ]

    def test_filters_none_samples(self):
        """None counts as zero rather than raising."""
        samples = [100] * 300 + [None] * 60 + [200] * 300 + [100] * 300
        assert shape(detect_intervals(samples, ftp=FTP)) == [(360, 300, "threshold")]

    def test_stretch_averaging_under_the_floor_is_dropped(self):
        """Bursts bridged across coasts can clear the sample threshold while the
        mean does not: that is no sustained effort at any level."""
        burst = [160] * 11 + [0] * 10
        assert detect_intervals([0] * 100 + burst * 4 + [0] * 100, ftp=FTP) == []


class TestStructureInsideLongRides:
    """Dropping the floor to 75% must not merge a ride into one tempo interval."""

    def test_hard_reps_inside_tempo_stay_separate(self):
        """3x5 min at 110% with the recoveries still at tempo. A single pass at
        75% reports all of it as one 43-minute effort."""
        rep, tempo = [220] * 300, [156] * 420
        samples = [100] * 300 + tempo + rep + tempo + rep + tempo + rep + tempo + [100] * 300
        assert shape(detect_intervals(samples, ftp=FTP)) == [
            (300, 420, "tempo"), (720, 300, "vo2"),
            (1020, 420, "tempo"), (1440, 300, "vo2"),
            (1740, 420, "tempo"), (2160, 300, "vo2"),
            (2460, 420, "tempo"),
        ]

    def test_sweetspot_blocks_inside_tempo_stay_separate(self):
        block, tempo = [182] * 1200, [156] * 600
        samples = [100] * 300 + block + tempo + block + [100] * 300
        assert shape(detect_intervals(samples, ftp=FTP)) == [
            (300, 1200, "sweetspot"), (1500, 600, "tempo"), (2100, 1200, "sweetspot"),
        ]

    def test_surge_inside_tempo_stays_separate(self):
        samples = [156] * 900 + [280] * 45 + [156] * 900
        assert shape(detect_intervals(samples, ftp=FTP)) == [
            (0, 900, "tempo"), (900, 45, "surge"), (945, 900, "tempo"),
        ]

    def test_steady_tempo_ride_is_one_interval(self):
        """Wandering across 85% without ever being harder than tempo is one effort:
        the second pass must not cut it into a mosaic."""
        samples = ([156] * 240 + [172] * 60) * 12
        assert shape(detect_intervals(samples, ftp=FTP)) == [(0, 3600, "tempo")]

    def test_short_spikes_do_not_split_tempo(self):
        """Spikes under the detector's minimum are not efforts of their own."""
        samples = ([156] * 280 + [300] * 20) * 12
        assert shape(detect_intervals(samples, ftp=FTP)) == [(0, 3600, "tempo")]

    def test_endurance_ride_below_the_floor_stays_empty(self):
        """Two hours at 70% is not a two-hour tempo interval."""
        assert detect_intervals([140] * 7200, ftp=FTP) == []

    def test_riding_on_the_floor_does_not_fuse(self):
        """Dips under 75% longer than the tolerance end the effort."""
        samples = ([156] * 300 + [140] * 60) * 6
        got = detect_intervals(samples, ftp=FTP)
        assert [iv["duration_s"] for iv in got] == [300] * 6
        assert {iv["classification"] for iv in got} == {"tempo"}


class TestStopAndGo:
    """Urban riding: the shape that used to read as 'anaerobic'."""

    def test_traffic_accelerations_are_surges(self):
        samples = ([0] * 30 + [260] * 40) * 10
        got = detect_intervals(samples, ftp=FTP)
        assert len(got) == 10
        assert {iv["classification"] for iv in got} == {"surge"}

    def test_deliberate_effort_from_a_standstill_is_not_a_surge(self):
        """The rule is duration, so where the effort starts cannot downgrade it."""
        samples = [0] * 60 + [260] * 75 + [0] * 60
        assert shape(detect_intervals(samples, ftp=FTP)) == [(60, 75, "anaerobic")]

    def test_deliberate_effort_in_a_steady_ride_is_not_a_surge(self):
        samples = [130] * 600 + [280] * 90 + [130] * 600
        assert shape(detect_intervals(samples, ftp=FTP)) == [(600, 90, "anaerobic")]


class TestGeneratedRides:
    @pytest.mark.parametrize("seed", range(40))
    def test_output_is_well_formed(self, seed):
        """Whatever the ride, intervals are ordered, disjoint, in range and named."""
        rng = random.Random(seed)
        samples = []
        while len(samples) < 7200:
            watts = rng.choice([0, 60, 120, 145, 150, 156, 170, 176, 190, 210, 260, 340, None])
            samples += [watts] * rng.randint(1, 400)

        got = detect_intervals(samples, ftp=FTP)

        assert got, "fixture is wrong: a ride this varied must contain an effort"
        end = 0
        for iv in got:
            assert iv["start_offset_s"] >= end
            end = iv["start_offset_s"] + iv["duration_s"]
            assert iv["duration_s"] >= DETECTOR_MIN_S
            assert iv["classification"] in INTERVAL_CLASSES
        assert end <= len(samples)
