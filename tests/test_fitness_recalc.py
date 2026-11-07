"""Source-order and helper checks for recalculate_fitness.

What it stores is tested on a real PostgreSQL in
tests/test_recalculation_stored_values.py.
"""

import inspect
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# Mock psycopg2 before importing ingestor modules
sys.modules.setdefault("psycopg2", MagicMock())
sys.modules.setdefault("psycopg2.extras", MagicMock())

_ingestor_dir = Path(__file__).resolve().parent.parent / "ingestor"
if str(_ingestor_dir) not in sys.path:
    sys.path.insert(0, str(_ingestor_dir))

import fitness as ingestor_fitness
from fitness import compute_ef

# ---------------------------------------------------------------------------
# compute_ef integration
# ---------------------------------------------------------------------------

class TestComputeEFInFlow:
    """Verify compute_ef is called correctly during NP computation."""

    def test_compute_ef_called_with_np_and_hr(self):
        """When NP is computed, EF = NP / avg_hr."""
        result = compute_ef(220.0, 150)
        assert result == pytest.approx(1.47, abs=0.01)

    def test_compute_ef_none_when_no_hr(self):
        """No avg_hr -> EF is None."""
        result = compute_ef(220.0, None)
        assert result is None

    def test_compute_ef_none_when_zero_hr(self):
        result = compute_ef(220.0, 0)
        assert result is None


class TestRideMetricsComputedAfterFtpBackfill:
    """Audit of PR #174: a METRICS_VERSION reset NULLs ride_ftp, and Step 2 is
    what backfills it. Computing the ftp-dependent ride metrics before that
    would hand them a NULL threshold, store NULL for every ride, and never
    recompute — the selector gates on coasting_time_s, which would already be
    set. This pins the ordering in the source itself."""

    def test_ride_metrics_step_runs_after_the_ride_ftp_backfill(self):
        src = (Path(__file__).resolve().parent.parent / "ingestor" / "fitness.py").read_text()
        reset_at = src.index("ride_ftp = NULL")
        backfill_at = src.index("# Step 2: Backfill ride_ftp")
        metrics_at = src.index("# Step 2.6: Provider-independent ride metrics")
        assert reset_at < backfill_at < metrics_at, (
            "the ride-metrics step must come after the ride_ftp backfill, "
            "otherwise a METRICS_VERSION recalc stores NULL for every "
            "ftp-dependent metric and never retries"
        )

    def test_ride_metrics_selector_reads_ride_ftp(self):
        src = (Path(__file__).resolve().parent.parent / "ingestor" / "fitness.py").read_text()
        step = src[src.index("# Step 2.6: Provider-independent ride metrics"):]
        step = step[:step.index("# Step 3:")]
        assert "a.ride_ftp" in step, "the step must read the backfilled ride_ftp"


class TestStravaSegmentBackfillGating:
    """With Strava removed as a source, the segment backfill ran on every
    recalculation and hit the OAuth endpoint purely to fail — pointless traffic
    and a recurring error line that trains the reader to ignore the log."""

    def test_skipped_when_strava_is_not_configured(self):
        with patch.dict(os.environ, {}, clear=True):
            assert ingestor_fitness._strava_configured() is False

    def test_configured_only_when_all_three_are_present(self):
        with patch.dict(os.environ, {"STRAVA_CLIENT_ID": "a",
                                     "STRAVA_CLIENT_SECRET": "b"}, clear=True):
            assert ingestor_fitness._strava_configured() is False
        with patch.dict(os.environ, {"STRAVA_CLIENT_ID": "a", "STRAVA_CLIENT_SECRET": "b",
                                     "STRAVA_REFRESH_TOKEN": "c"}, clear=True):
            assert ingestor_fitness._strava_configured() is True

    def test_climb_detection_still_runs_when_strava_is_skipped(self):
        """The gate must not be an early return — Step 8b follows it."""
        src = (Path(__file__).resolve().parent.parent / "ingestor" / "fitness.py").read_text()
        gate = src.index("Strava not configured — skipping segment backfill")
        climbs = src.index("# Step 8b: Detect climbs")
        assert gate < climbs
        between = src[gate:climbs]
        assert "return" not in between, "an early return here would skip climb detection"

    def test_the_backfill_call_is_actually_guarded(self):
        """The helper being correct is not enough — the call site must sit
        behind the gate. Without this, replacing the condition with `if True`
        passes every other test in this class."""
        src = (Path(__file__).resolve().parent.parent / "ingestor" / "fitness.py").read_text()
        i = src.index("if _strava_configured():")          # raises if the gate is gone
        guarded = src[i:src.index("else:", i)]
        assert "_backfill_strava_segments_safely(conn)" in guarded

    def test_backfill_helper_swallows_failures(self):
        conn = MagicMock()
        with patch.dict(sys.modules, {"strava": MagicMock()}) as mods:
            mods["strava"].backfill_strava_segments.side_effect = RuntimeError("token dead")
            ingestor_fitness._backfill_strava_segments_safely(conn)   # must not raise


class TestRouteAssignment:
    """Step 2.7 groups rides onto routes. It MUST run after Step 2.

    A METRICS_VERSION reset NULLs derived columns, and Step 2 is what backfills
    ride_ftp. A step placed before it writes NULLs that are never recomputed —
    the trap that left two metrics permanently NULL earlier in this project.
    """

    def test_route_step_runs_after_ftp_backfill(self):
        """Assert on ORDER, not merely that both steps exist."""
        src = inspect.getsource(ingestor_fitness.recalculate_fitness)
        assert "Step 2.7" in src, "route assignment step is missing"
        assert src.index("Step 2:") < src.index("Step 2.7"), (
            "Step 2.7 must come after the ride_ftp backfill in Step 2"
        )

    def test_route_step_runs_before_tss(self):
        """And before Step 3, so the pass keeps its documented order."""
        src = inspect.getsource(ingestor_fitness.recalculate_fitness)
        assert src.index("Step 2.7") < src.index("Step 3:")

    def test_metrics_version_bumped_for_route_backfill(self):
        """Without a bump, existing installs never build signatures."""
        assert int(ingestor_fitness.METRICS_VERSION) >= 15

    def test_reset_clears_route_signature(self):
        """Signatures are reused when present, so a version reset must clear
        them — otherwise a CELL_DEGREES change would silently have no effect."""
        src = inspect.getsource(ingestor_fitness.recalculate_fitness)
        reset = [l for l in src.splitlines() if "UPDATE activities SET tss = NULL" in l]
        assert reset, "metrics reset statement not found"
        assert "route_signature = NULL" in reset[0], (
            "the METRICS_VERSION reset must clear route_signature"
        )

    def test_endpoint_query_guards_both_coordinates(self):
        """lat and lng are independently nullable; a lat-without-lng row would
        reach haversine_m as None and abort assignment for the whole library."""
        src = inspect.getsource(ingestor_fitness.recalculate_fitness)
        step = src[src.index("Step 2.7"):src.index("Step 3:")]
        endpoints = step[step.index("ORDER BY time_offset LIMIT 1") - 400:]
        assert step.count("lat IS NOT NULL AND lng IS NOT NULL") >= 3, (
            "every activity_streams read in Step 2.7 must guard lat AND lng"
        )


def test_metrics_version_bumped_for_interval_classes():
    """Without a bump, stored rides keep their old class names: the detection
    step skips any ride that already has rows."""
    assert int(ingestor_fitness.METRICS_VERSION) >= 16
