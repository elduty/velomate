"""Tests for ingestor/segments.py — climb segments matched across rides."""

from unittest.mock import MagicMock

import pytest

import segments
from segments import (build_segment, cluster_climbs, find_efforts, haversine_m,
                      is_covered)

LAT0, LNG = 46.0, 10.0
STEP = 0.0001                                   # ≈ 11.1 m of latitude
STEP_M = haversine_m(LAT0, LNG, LAT0 + STEP, LNG)


def ride(path, dt=2, grade=0.08, hr=None):
    """Track along a north-south road. `path` is a list of road positions
    (index along the road, 0 = valley); altitude rises with position."""
    return [{"time_offset": i * dt, "lat": LAT0 + k * STEP, "lng": LNG,
             "altitude_m": 1000 + k * STEP_M * grade, "hr": hr, "power": None}
            for i, k in enumerate(path)]


START, END = (LAT0 + 10 * STEP, LNG), (LAT0 + 110 * STEP, LNG)   # ~1.1 km climb


class TestFindEfforts:
    def test_a_detour_between_the_two_points_is_not_an_effort(self):
        """Up past halfway, back down a way, then on to the top: about 180 steps over
        a 100-step climb, so it's the same two points by another road."""
        track = ride([*range(0, 60), *range(60, 30, -1), *range(30, 130)])

        assert len(find_efforts(track, START, END)) == 1
        assert find_efforts(track, START, END, max_path_m=130 * STEP_M) == []

    def test_single_climb_timed_start_to_end(self):
        efforts = find_efforts(ride(range(0, 130)), START, END)
        assert len(efforts) == 1
        e = efforts[0]
        assert e["elapsed_s"] == 100 * 2
        assert e["path_m"] == pytest.approx(100 * STEP_M, rel=1e-3)
        assert e["gain_m"] == pytest.approx(100 * STEP_M * 0.08, rel=1e-3)
        assert e["vam_mh"] == round(e["gain_m"] / 200 * 3600)

    def test_descent_is_not_an_effort(self):
        assert find_efforts(ride(range(130, -1, -1)), START, END) == []

    def test_stop_counts_in_elapsed_not_moving(self):
        path = list(range(0, 61)) + [60] * 30 + list(range(61, 130))  # 60 s stop
        e = find_efforts(ride(path), START, END)[0]
        assert e["elapsed_s"] == e["moving_s"] + 30 * 2

    def test_repeats_give_one_effort_each(self):
        lap = list(range(0, 130)) + list(range(129, -1, -1))
        assert len(find_efforts(ride(lap * 2), START, END)) == 2

    def test_circling_at_the_start_times_from_last_pass(self):
        path = list(range(0, 20)) + list(range(19, -1, -1)) + list(range(0, 130))
        e = find_efforts(ride(path), START, END)[0]
        assert e["elapsed_s"] == 100 * 2

    def test_detour_longer_than_max_path_rejected(self):
        # Goes past the start, back down, then up to the end: path ≈ 3 × segment
        path = list(range(0, 40)) + list(range(39, 0, -1)) + list(range(0, 130))
        found = find_efforts(ride(path), (LAT0 + 30 * STEP, LNG), END,
                             max_path_m=1.3 * 80 * STEP_M)
        assert len(found) == 1  # only the clean last traversal survives
        assert found[0]["path_m"] == pytest.approx(80 * STEP_M, rel=1e-3)

    def test_ride_elsewhere_has_no_effort(self):
        far = [dict(p, lng=LNG + 0.05) for p in ride(range(0, 130))]
        assert find_efforts(far, START, END) == []

    def test_hr_time_weighted(self):
        e = find_efforts(ride(range(0, 130), hr=150), START, END)[0]
        assert e["avg_hr"] == 150
        assert e["avg_power"] is None


def climb(aid, start_k, end_k, gain=100):
    return {"activity_id": aid, "start": (LAT0 + start_k * STEP, LNG),
            "end": (LAT0 + end_k * STEP, LNG), "start_alt": 1000 + start_k,
            "peak_alt": 1000 + end_k, "gain_m": gain}


class TestClusterClimbs:
    def test_same_climb_with_drifting_start_clusters(self):
        clusters = cluster_climbs([climb(1, 10, 110), climb(2, 30, 111), climb(3, 0, 109)])
        assert len(clusters) == 1 and len(clusters[0]) == 3

    def test_different_summits_split(self):
        clusters = cluster_climbs([climb(1, 10, 110), climb(2, 10, 110),
                                   climb(3, 10, 300), climb(4, 10, 300)])
        assert len(clusters) == 2

    def test_single_ride_is_not_a_segment(self):
        assert cluster_climbs([climb(1, 10, 110), climb(1, 12, 110)]) == []


class TestBuildSegment:
    def test_consensus_start_is_the_one_every_ride_passes(self):
        # Ride 2 starts on the road at position 30, so a start at 10 only
        # covers ride 1 — the segment must start where both rides were.
        tracks = {1: ride(range(0, 130)), 2: ride(range(30, 130))}
        seg = build_segment([climb(1, 10, 110), climb(2, 30, 110)], tracks)
        assert seg["start_lat"] == pytest.approx(LAT0 + 30 * STEP)
        assert seg["length_m"] == pytest.approx(80 * STEP_M, abs=1)
        assert seg["avg_grade"] == pytest.approx(8.0, abs=0.1)

    def test_none_when_only_one_ride_rides_it(self):
        tracks = {1: ride(range(0, 130)),
                  2: [dict(p, lng=LNG + 0.05) for p in ride(range(0, 130))]}
        assert build_segment([climb(1, 10, 110), climb(2, 10, 110)], tracks) is None

    def test_too_small_climb_rejected(self):
        tracks = {1: ride(range(0, 130), grade=0.01), 2: ride(range(0, 130), grade=0.01)}
        assert build_segment([climb(1, 10, 110), climb(2, 10, 110)], tracks) is None


class TestIsCovered:
    def test_existing_segment_covers_nearby_cluster(self):
        seg = {"start_lat": LAT0 + 10 * STEP, "start_lng": LNG,
               "end_lat": LAT0 + 110 * STEP, "end_lng": LNG}
        assert is_covered(climb(1, 20, 111), [seg])
        assert not is_covered(climb(1, 20, 300), [seg])


class TestStreamReplacementResetsEfforts:
    def test_upsert_streams_clears_efforts_and_scans(self):
        import db
        conn = MagicMock()
        cur = MagicMock()
        conn.cursor.return_value.__enter__ = MagicMock(return_value=cur)
        conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
        db.upsert_streams(conn, 5, [])
        sql = " ".join(c.args[0] for c in cur.execute.call_args_list)
        assert "DELETE FROM climb_efforts" in sql
        assert "DELETE FROM climb_segment_scans" in sql


class TestRenameSegment:
    def _conn(self, rowcount=1):
        conn = MagicMock()
        cur = MagicMock()
        cur.rowcount = rowcount
        conn.cursor.return_value.__enter__ = MagicMock(return_value=cur)
        conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
        return conn, cur

    def test_sets_trimmed_name(self):
        conn, cur = self._conn()
        assert segments.rename_segment(conn, 4, "  Donasè ")
        assert cur.execute.call_args.args[1] == ("Donasè", 4)

    def test_empty_name_restores_auto_name(self):
        conn, cur = self._conn()
        segments.rename_segment(conn, 4, "")
        assert cur.execute.call_args.args[1] == (None, 4)

    def test_unknown_id(self):
        conn, _ = self._conn(rowcount=0)
        assert not segments.rename_segment(conn, 99, "x")
