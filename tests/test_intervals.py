"""Tests for auto interval detection in ingestor/intervals.py."""

import pytest

from intervals import detect_intervals, classify_interval


# --- classify_interval ---

class TestClassifyInterval:
    """Coggan-style classification from duration + avg power relative to FTP."""

    def test_sprint(self):
        # 20s at 200% FTP
        assert classify_interval(20, 400, ftp=200) == "sprint"

    def test_anaerobic(self):
        # 60s at 140% FTP
        assert classify_interval(60, 280, ftp=200) == "anaerobic"

    def test_vo2(self):
        # 4 min at 115% FTP
        assert classify_interval(240, 230, ftp=200) == "vo2"

    def test_threshold(self):
        # 10 min at 100% FTP
        assert classify_interval(600, 200, ftp=200) == "threshold"

    def test_sweetspot(self):
        # 20 min at 90% FTP
        assert classify_interval(1200, 180, ftp=200) == "sweetspot"

    def test_tempo(self):
        # 30 min at 80% FTP
        assert classify_interval(1800, 160, ftp=200) == "tempo"

    def test_unclassified_short_easy(self):
        """Short easy effort below tempo threshold -> None (not an interval)."""
        assert classify_interval(30, 100, ftp=200) is None

    def test_unclassified_long_easy(self):
        """Long effort below tempo threshold -> None."""
        assert classify_interval(2400, 120, ftp=200) is None

    def test_dead_zone_120s_at_150pct(self):
        """Boundary dead zone: 120s at exactly 150% FTP is above anaerobic's
        pct range (pct <= 1.50 is OK but duration >= 120 excludes it) and
        above vo2's pct ceiling (pct > 1.20). Locks in None so future bounds
        changes can't silently shift this edge case."""
        assert classify_interval(120, 300, ftp=200) is None

    def test_dead_zone_30s_at_151pct(self):
        """Boundary dead zone: 30s at 151% FTP is above sprint's duration
        ceiling (duration >= 30) and above anaerobic's pct ceiling
        (pct > 1.50)."""
        assert classify_interval(30, 302, ftp=200) is None


# --- detect_intervals ---

class TestDetectIntervals:
    """Detection: find contiguous sustained efforts ≥ 30s above threshold_pct × FTP."""

    def test_empty(self):
        assert detect_intervals([], ftp=200) == []

    def test_no_effort(self):
        """All zone 2 — no intervals detected."""
        samples = [120] * 600  # 10 min at 60% FTP
        assert detect_intervals(samples, ftp=200) == []

    def test_single_threshold_interval(self):
        """10 min warmup, 8 min threshold, 10 min cooldown."""
        samples = [120] * 600 + [210] * 480 + [120] * 600
        intervals = detect_intervals(samples, ftp=200, threshold_pct=0.85)
        assert len(intervals) == 1
        iv = intervals[0]
        assert iv["start_offset_s"] == 600
        assert iv["duration_s"] == 480
        assert 200 <= iv["avg_power"] <= 220
        assert iv["classification"] == "threshold"

    def test_multiple_intervals(self):
        """Four 2-min VO2 reps with 1-min recovery."""
        samples = [100] * 300  # 5 min warmup
        for _ in range(4):
            samples += [240] * 120  # 2 min at 120% FTP
            samples += [100] * 60   # 1 min recovery
        samples += [100] * 300  # 5 min cooldown
        intervals = detect_intervals(samples, ftp=200, threshold_pct=0.85)
        assert len(intervals) == 4
        assert all(iv["classification"] == "vo2" for iv in intervals)
        assert all(115 <= iv["duration_s"] <= 125 for iv in intervals)

    def test_minimum_duration_filter(self):
        """A 20-second surge (< 30s) should not be detected as an interval."""
        samples = [100] * 300 + [250] * 20 + [100] * 300
        intervals = detect_intervals(samples, ftp=200, min_duration_s=30)
        assert intervals == []

    def test_spike_bridges_gap(self):
        """A 5-second dip in the middle of a 5-min threshold effort should not split it."""
        samples = [100] * 300 + [210] * 150 + [100] * 5 + [210] * 150 + [100] * 300
        intervals = detect_intervals(samples, ftp=200, threshold_pct=0.85, gap_tolerance_s=10)
        assert len(intervals) == 1
        assert intervals[0]["duration_s"] >= 300  # bridged

    def test_filters_none_samples(self):
        """None in the stream should be treated as zero, not cause TypeError."""
        samples = [100] * 300 + [None] * 60 + [210] * 300 + [100] * 300
        # Should still find the 5-min threshold effort
        intervals = detect_intervals(samples, ftp=200, threshold_pct=0.85)
        assert len(intervals) >= 1
