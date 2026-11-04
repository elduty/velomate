"""Tests for ingestor/route_match.py — pure route identity functions."""

import pytest

from route_match import (
    CELL_DEGREES, MATCH_THRESHOLD, MIN_GPS_POINTS, LOOP_CLOSURE_M,
    build_signature, encode_signature, decode_signature, jaccard,
    best_route_match, is_loop, derive_label, haversine_m,
)

LOOP = [(38.70 + i * 0.001, -9.40 + i * 0.001) for i in range(80)]
ELSEWHERE = [(50.0 + i * 0.001, 10.0 + i * 0.001) for i in range(80)]


class TestBuildSignature:
    def test_same_path_same_cells(self):
        assert build_signature(LOOP) == build_signature(LOOP)

    def test_direction_agnostic(self):
        """Riding the loop backwards is the same road, so the same cells."""
        assert build_signature(LOOP) == build_signature(list(reversed(LOOP)))

    def test_pause_agnostic(self):
        """Standing still repeats a coordinate; a set absorbs the duplicates."""
        paused = LOOP[:40] + [LOOP[40]] * 300 + LOOP[40:]
        assert build_signature(paused) == build_signature(LOOP)

    def test_scaling_uses_each_points_own_latitude(self):
        """The regression that scaling by the ride's FIRST latitude reintroduces.

        cos(lat0) multiplies absolute longitude (~-9.4 here), so deriving it from
        the ride's start shifts every cell when two rides begin at different
        points on the same road — 0.05 degrees of start latitude is 5.1 cells,
        570 m of pure offset. Using each point's own latitude makes a cell a
        function of the coordinate alone.
        """
        second_half_first = LOOP[40:] + LOOP[:40]      # same road, different start
        assert build_signature(second_half_first) == build_signature(LOOP)

    def test_too_few_points_is_empty(self):
        assert build_signature(LOOP[:MIN_GPS_POINTS - 1]) == set()

    def test_no_points_is_empty(self):
        assert build_signature([]) == set()

    def test_sparsity_gate_counts_valid_points_only(self):
        """The gate must apply AFTER nulls are dropped.

        Counting raw rows lets a track with 60 rows but 55 null coordinates
        through, and it then builds a signature from 5 points — exactly the
        under-sampled case MIN_GPS_POINTS exists to reject.
        """
        mostly_null = LOOP[:5] + [(None, None)] * (MIN_GPS_POINTS + 10)
        assert len(mostly_null) > MIN_GPS_POINTS      # passes a naive length check
        assert build_signature(mostly_null) == set()  # but is still too sparse

    def test_null_coordinates_are_skipped(self):
        """lat and lng are independently nullable in activity_streams."""
        dirty = LOOP + [(None, -9.4), (38.7, None), (None, None)]
        assert build_signature(dirty) == build_signature(LOOP)


class TestJaccard:
    def test_identical(self):
        assert jaccard({(1, 1)}, {(1, 1)}) == 1.0

    def test_disjoint(self):
        assert jaccard({(1, 1)}, {(2, 2)}) == 0.0

    def test_partial(self):
        assert jaccard({(1, 1), (2, 2)}, {(2, 2), (3, 3)}) == pytest.approx(1 / 3)

    def test_both_empty_does_not_divide_by_zero(self):
        assert jaccard(set(), set()) == 0.0


class TestEncoding:
    def test_round_trip(self):
        cells = build_signature(LOOP)
        assert decode_signature(encode_signature(cells)) == cells

    def test_encoding_is_stable(self):
        """Sorted output, so the same path always encodes to the same string."""
        cells = build_signature(LOOP)
        assert encode_signature(cells) == encode_signature(set(cells))

    def test_decode_empty(self):
        assert decode_signature("") == set()

    def test_negative_indices_round_trip(self):
        """Longitude west of Greenwich yields negative cell indices."""
        cells = {(-38700, -7334), (12, -1)}
        assert decode_signature(encode_signature(cells)) == cells


class TestBestRouteMatch:
    def test_no_routes_returns_none(self):
        assert best_route_match(build_signature(LOOP), {}) == (None, 0.0)

    def test_matches_above_threshold(self):
        cells = build_signature(LOOP)
        route, score = best_route_match(cells, {7: [cells]})
        assert route == 7
        assert score == 1.0

    def test_rejects_below_threshold(self):
        route, score = best_route_match(build_signature(LOOP),
                                        {7: [build_signature(ELSEWHERE)]})
        assert route is None
        assert score < MATCH_THRESHOLD

    def test_picks_the_better_of_two(self):
        cells = build_signature(LOOP)
        near = build_signature(LOOP[:70])
        far = build_signature(ELSEWHERE)
        route, _ = best_route_match(cells, {1: [far], 2: [near]})
        assert route == 2

    def test_averages_across_members(self):
        """Average linkage: one identical member does not carry an unrelated one."""
        cells = build_signature(LOOP)
        far = build_signature(ELSEWHERE)
        route, score = best_route_match(cells, {1: [cells, far]})
        assert route is None
        assert score == pytest.approx(0.5, abs=0.01)

    def test_empty_member_list_is_skipped(self):
        cells = build_signature(LOOP)
        assert best_route_match(cells, {1: []}) == (None, 0.0)


class TestLoopAndLabel:
    def test_loop_detected(self):
        there_and_back = LOOP + list(reversed(LOOP))
        assert is_loop(there_and_back) is True

    def test_point_to_point_detected(self):
        assert is_loop(LOOP) is False

    def test_two_endpoints_are_enough(self):
        """Step 2.7 passes only the track's first and last point, because the
        signature-reuse path never loads the full track."""
        assert is_loop([(38.70, -9.40), (38.70, -9.40)]) is True
        assert is_loop([(38.70, -9.40), (38.90, -9.40)]) is False

    def test_loop_closure_boundary(self):
        """LOOP_CLOSURE_M is the cutoff: just inside is a loop, just outside is not."""
        deg = 1.0 / 111_000.0
        assert is_loop([(38.70, -9.40),
                        (38.70 + (LOOP_CLOSURE_M - 50) * deg, -9.40)]) is True
        assert is_loop([(38.70, -9.40),
                        (38.70 + (LOOP_CLOSURE_M + 50) * deg, -9.40)]) is False

    def test_too_few_points_is_not_a_loop(self):
        assert is_loop([]) is False
        assert is_loop([(38.7, -9.4)]) is False

    def test_label_loop(self):
        assert derive_label(21000.0, True) == "21.0 km loop"

    def test_label_point_to_point(self):
        assert derive_label(12800.0, False) == "12.8 km point-to-point"

    def test_label_without_distance(self):
        assert derive_label(None, True) == "unknown distance loop"
        assert derive_label(0, True) == "unknown distance loop"


def test_haversine_known_distance():
    """One degree of latitude is ~111 km."""
    assert haversine_m(38.0, -9.0, 39.0, -9.0) == pytest.approx(111_000, rel=0.01)


def test_cell_degrees_is_about_111_metres():
    """Pins the constant: the grid is ~111 m, which the threshold assumes."""
    assert haversine_m(38.70, -9.40, 38.70 + CELL_DEGREES, -9.40) == pytest.approx(111, rel=0.02)
