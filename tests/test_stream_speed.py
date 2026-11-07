"""Speed derived from GPS for streams that have none.

Ride with GPS phone recordings carry position but no speed, so Max Speed, the speed
chart and the by-kilometre zones stayed empty. Recorded speed always wins.
"""

import math
import sys
from pathlib import Path

import pytest

_ingestor_dir = Path(__file__).resolve().parent.parent / "ingestor"
if str(_ingestor_dir) not in sys.path:
    sys.path.insert(0, str(_ingestor_dir))

import db
from route_match import haversine_m

LAT0 = 46.0
LNG0 = 10.0
M_PER_DEG_LAT = haversine_m(LAT0, LNG0, LAT0 + 1, LNG0)
M_PER_DEG_LNG = haversine_m(LAT0, LNG0, LAT0, LNG0 + 1)


def _track(seconds, kmh=25.0, dt=1):
    """Due north at a steady speed, with position and no speed."""
    step_deg = kmh / 3.6 * dt / M_PER_DEG_LAT
    return [{"time_offset": i * dt, "lat": LAT0 + i * step_deg, "lng": LNG0, "speed_kmh": None}
            for i in range(seconds)]


def test_a_steady_ride_reads_its_real_speed():
    speeds = [p["speed_kmh"] for p in db.fill_missing_speed(_track(30, kmh=40.0))[5:]]
    assert speeds == pytest.approx([40.0] * 25, abs=0.05)


def test_gps_noise_does_not_become_a_max_speed():
    # 3 m either side of the road at 1 Hz. One step at a time reads about 33 km/h.
    track = _track(120, kmh=25.0)
    for i, point in enumerate(track):
        point["lng"] += (3 if i % 2 else -3) / M_PER_DEG_LNG
    speeds = [p["speed_kmh"] for p in db.fill_missing_speed(track) if p["speed_kmh"] is not None]
    assert max(speeds) < 27


def test_distance_from_speed_matches_the_track():
    # The by-kilometre panels integrate speed over time
    # The opening seconds have no full window yet, which is nothing over a whole ride
    track = db.fill_missing_speed(_track(1800, kmh=30.0))
    metres = sum((p["speed_kmh"] or 0) / 3.6 for p in track)
    assert metres == pytest.approx(1799 * 30 / 3.6, rel=0.005)


def test_recorded_speed_is_kept():
    track = _track(10)
    track[5]["speed_kmh"] = 33.3
    db.fill_missing_speed(track)
    assert track[5]["speed_kmh"] == 33.3
    assert track[6]["speed_kmh"] == pytest.approx(25.0, abs=0.05)


def test_a_zero_from_the_device_is_kept():
    track = _track(10)
    track[5]["speed_kmh"] = 0
    assert db.fill_missing_speed(track)[5]["speed_kmh"] == 0


def test_the_opening_seconds_wait_for_a_full_window():
    # A shorter measure is the one-step noise the window exists to keep out, and nothing
    # marks a stored speed as derived, so it could never be corrected later
    speeds = [p["speed_kmh"] for p in db.fill_missing_speed(_track(8))]
    assert speeds[:5] == [None] * 5
    assert speeds[5:] == pytest.approx([25.0] * 3, abs=0.05)


def test_sparse_fixes_wait_for_a_full_window_too():
    speeds = [p["speed_kmh"] for p in db.fill_missing_speed(_track(5, dt=2))]
    assert speeds[:3] == [None] * 3
    assert speeds[3:] == pytest.approx([25.0] * 2, abs=0.05)


def test_a_gps_gap_measures_from_the_last_fix():
    track = _track(12, kmh=25.0)
    for point in track[5:9]:
        point["lat"] = point["lng"] = None
    db.fill_missing_speed(track)
    assert [p["speed_kmh"] for p in track[5:9]] == [None] * 4
    assert track[9]["speed_kmh"] == pytest.approx(25.0, abs=0.05)


def test_an_indoor_stream_stays_empty():
    track = [{"time_offset": i, "lat": None, "lng": None, "speed_kmh": None} for i in range(10)]
    assert all(p["speed_kmh"] is None for p in db.fill_missing_speed(track))


def test_a_jump_in_position_stays_empty_then_recovers():
    track = _track(30, kmh=25.0)
    for point in track[15:]:
        point["lat"] += 1000 / M_PER_DEG_LAT
    db.fill_missing_speed(track)
    assert track[15]["speed_kmh"] is None
    assert track[21]["speed_kmh"] == pytest.approx(25.0, abs=0.05)


def test_a_repeated_second_is_skipped():
    track = _track(20)
    track[10]["time_offset"] = track[9]["time_offset"]
    db.fill_missing_speed(track)
    assert track[10]["speed_kmh"] is None
    assert track[11]["speed_kmh"] is not None
    assert not any(isinstance(p["speed_kmh"], float) and math.isnan(p["speed_kmh"]) for p in track)


@pytest.mark.parametrize("field", ["time_offset", "lat", "lng"])
def test_a_point_it_cannot_read_is_skipped(field):
    # This runs inside the stream write, so raising here would fail a ride over a
    # stat. The database still gets the point, and decides for itself.
    track = _track(10)
    track[5][field] = "not a number"
    db.fill_missing_speed(track)
    assert track[5]["speed_kmh"] is None
    assert track[6]["speed_kmh"] == pytest.approx(25.0, abs=0.05)
