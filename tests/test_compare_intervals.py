"""Tests for scripts/compare_intervals.py — intervals.icu comparison analysis."""

import importlib.util
from datetime import date, datetime, timezone
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "compare_intervals.py"


def _load():
    spec = importlib.util.spec_from_file_location("compare_intervals", _SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


ci = _load()


def _vm(id=1, strava_id=None, start="2026-07-01T09:00:00+00:00", duration=3600,
        distance=30000.0, **metrics):
    row = {
        "id": id,
        "strava_id": strava_id,
        "intervals_icu_id": None,
        "date": datetime.fromisoformat(start),
        "duration_s": duration,
        "distance_m": distance,
    }
    row.update(metrics)
    return row


def _icu(id="i1", strava_id=None, start="2026-07-01T09:00:00Z", moving_time=3600,
         distance=30000.0, **fields):
    act = {
        "id": id,
        "strava_id": strava_id,
        "start_date": start,
        "start_date_local": start.replace("Z", ""),
        "moving_time": moving_time,
        "distance": distance,
        "type": "Ride",
    }
    act.update(fields)
    return act


class TestParseIcuDt:
    def test_parses_zulu_suffix_to_utc(self):
        assert ci.parse_icu_dt("2026-07-01T09:00:00Z") == datetime(2026, 7, 1, 9, tzinfo=timezone.utc)

    def test_assumes_utc_for_naive_timestamps(self):
        assert ci.parse_icu_dt("2026-07-01T09:00:00") == datetime(2026, 7, 1, 9, tzinfo=timezone.utc)

    def test_converts_offset_timestamps_to_utc(self):
        assert ci.parse_icu_dt("2026-07-01T10:00:00+01:00") == datetime(2026, 7, 1, 9, tzinfo=timezone.utc)

    def test_returns_none_for_junk(self):
        assert ci.parse_icu_dt("not a date") is None
        assert ci.parse_icu_dt("") is None
        assert ci.parse_icu_dt(None) is None


class TestIsFuzzyMatch:
    def test_matches_on_duration_within_fifteen_percent(self):
        assert ci.is_fuzzy_match(_icu(moving_time=3900), _vm(duration=3600)) is True

    def test_matches_on_distance_when_duration_differs_wildly(self):
        assert ci.is_fuzzy_match(_icu(moving_time=9000, distance=30500.0), _vm(duration=3600)) is True

    def test_rejects_when_start_times_are_far_apart(self):
        assert ci.is_fuzzy_match(_icu(start="2026-07-01T11:00:00Z"), _vm()) is False

    def test_short_rides_get_the_300_second_floor(self):
        # 200s vs 400s: 15% of 200 is 30s, but the floor is 300s, so this matches.
        assert ci.is_fuzzy_match(_icu(moving_time=400, distance=0), _vm(duration=200, distance=0)) is True

    def test_rejects_when_neither_duration_nor_distance_agree(self):
        assert ci.is_fuzzy_match(_icu(moving_time=9000, distance=90000.0), _vm()) is False

    def test_missing_timestamps_never_match(self):
        assert ci.is_fuzzy_match(_icu(start=""), _vm()) is False


class TestMatchActivities:
    def test_exact_strava_id_match_is_preferred(self):
        result = ci.match_activities([_icu(id="i1", strava_id="555")], [_vm(id=7, strava_id=555)])
        assert result.by_strava_id == 1
        assert result.by_fuzzy == 0
        assert result.pairs[0][0]["id"] == "i1"
        assert result.pairs[0][1]["id"] == 7

    def test_strava_id_matches_across_string_and_int_types(self):
        result = ci.match_activities([_icu(strava_id=555)], [_vm(strava_id=555)])
        assert result.by_strava_id == 1

    def test_falls_back_to_fuzzy_when_no_strava_id(self):
        result = ci.match_activities([_icu(id="i2")], [_vm(id=9)])
        assert result.by_strava_id == 0
        assert result.by_fuzzy == 1
        assert result.pairs[0][1]["id"] == 9

    def test_reports_unmatched_on_both_sides(self):
        icu = [_icu(id="i1", start="2026-07-01T09:00:00Z")]
        vm = [_vm(id=1, start="2026-07-20T09:00:00+00:00")]
        result = ci.match_activities(icu, vm)
        assert result.pairs == []
        assert [a["id"] for a in result.icu_only] == ["i1"]
        assert [r["id"] for r in result.velomate_only] == [1]

    def test_one_velomate_row_is_never_matched_twice(self):
        icu = [_icu(id="i1"), _icu(id="i2")]
        result = ci.match_activities(icu, [_vm(id=1)])
        assert len(result.pairs) == 1
        assert len(result.icu_only) == 1


class TestDetectUnitMismatch:
    def test_flags_kilo_scaling(self):
        assert "1000" in ci.detect_unit_mismatch(0.001)

    def test_flags_percent_scaling(self):
        assert "100" in ci.detect_unit_mismatch(0.01)

    def test_silent_when_units_agree(self):
        assert ci.detect_unit_mismatch(1.02) is None

    def test_silent_when_ratio_unknown(self):
        assert ci.detect_unit_mismatch(None) is None


class TestCompareMetric:
    def _metric(self):
        return ci.Metric("np", "np", "icu_weighted_avg_watts")

    def test_identical_values_report_zero_bias(self):
        pairs = [(_icu(icu_weighted_avg_watts=200), _vm(np=200.0)) for _ in range(3)]
        stats = ci.compare_metric(self._metric(), pairs)
        assert stats.n == 3
        assert stats.mean_signed_diff == pytest.approx(0.0)
        assert stats.within_tolerance_pct == pytest.approx(100.0)

    def test_systematic_bias_shows_in_signed_mean(self):
        pairs = [
            (_icu(icu_weighted_avg_watts=200), _vm(np=210.0)),
            (_icu(icu_weighted_avg_watts=100), _vm(np=110.0)),
        ]
        stats = ci.compare_metric(self._metric(), pairs)
        assert stats.mean_signed_diff == pytest.approx(10.0)

    def test_noise_cancels_in_signed_mean_but_not_in_median_pct(self):
        pairs = [
            (_icu(icu_weighted_avg_watts=200), _vm(np=210.0)),
            (_icu(icu_weighted_avg_watts=200), _vm(np=190.0)),
        ]
        stats = ci.compare_metric(self._metric(), pairs)
        assert stats.mean_signed_diff == pytest.approx(0.0)
        assert stats.median_pct_diff == pytest.approx(5.0)

    def test_skips_pairs_missing_either_side(self):
        pairs = [
            (_icu(icu_weighted_avg_watts=200), _vm(np=200.0)),
            (_icu(), _vm(np=200.0)),
            (_icu(icu_weighted_avg_watts=200), _vm()),
        ]
        assert ci.compare_metric(self._metric(), pairs).n == 1

    def test_returns_none_when_nothing_comparable(self):
        assert ci.compare_metric(self._metric(), [(_icu(), _vm())]) is None

    def test_zero_icu_value_does_not_divide_by_zero(self):
        pairs = [(_icu(icu_weighted_avg_watts=0), _vm(np=10.0))]
        stats = ci.compare_metric(self._metric(), pairs)
        assert stats.n == 1
        assert stats.median_ratio is None

    def test_kilojoule_mismatch_is_flagged(self):
        pairs = [(_icu(icu_joules=1_000_000), _vm(work_kj=1000.0)) for _ in range(3)]
        stats = ci.compare_metric(ci.Metric("work_kj", "work_kj", "icu_joules"), pairs)
        assert stats.unit_flag is not None


class TestSegmentation:
    def test_split_by_vi_uses_the_high_vi_threshold(self):
        low = (_icu(), _vm(variability_index=1.1))
        high = (_icu(), _vm(variability_index=1.45))
        normal, urban = ci.split_by_vi([low, high])
        assert normal == [low]
        assert urban == [high]

    def test_pairs_without_vi_count_as_normal(self):
        pair = (_icu(), _vm())
        normal, urban = ci.split_by_vi([pair])
        assert normal == [pair]

    def test_split_by_ftp_agreement(self):
        agree = (_icu(icu_ftp=250), _vm(ride_ftp=250.0))
        differ = (_icu(icu_ftp=250), _vm(ride_ftp=270.0))
        matched, mismatched = ci.split_by_ftp_agreement([agree, differ])
        assert matched == [agree]
        assert mismatched == [differ]

    def test_missing_ftp_counts_as_mismatched(self):
        pair = (_icu(), _vm(ride_ftp=250.0))
        matched, mismatched = ci.split_by_ftp_agreement([pair])
        assert mismatched == [pair]


class TestMetricsTable:
    def test_covers_every_spec_metric(self):
        names = {m.name for m in ci.METRICS}
        assert {"np", "tss", "intensity_factor", "variability_index", "ef",
                "trimp", "aerobic_decoupling", "work_kj", "ride_ftp"} <= names

    def test_covers_the_daily_series_metrics(self):
        icu_fields = {m.icu_field for m in ci.METRICS}
        assert {"icu_pm_cp", "icu_rolling_cp", "ss_cp", "icu_pm_w_prime",
                "icu_max_wbal_depletion", "icu_ctl", "icu_atl"} <= icu_fields

    def test_metric_names_are_unique(self):
        names = [m.name for m in ci.METRICS]
        assert len(names) == len(set(names))


class TestFieldInventory:
    def test_reports_fill_rate_for_uncompared_fields(self):
        acts = [
            _icu(polarization_index=2.1, strain_score=None),
            _icu(polarization_index=1.8, strain_score=None),
            _icu(polarization_index=None, strain_score=None),
        ]
        inv = {f.name: f for f in ci.field_inventory(acts)}
        assert inv["polarization_index"].fill_pct == pytest.approx(66.67, abs=0.01)
        assert "strain_score" not in inv  # never populated, nothing to learn

    def test_excludes_fields_already_compared(self):
        acts = [_icu(icu_weighted_avg_watts=200, polarization_index=2.0)]
        fields = {f.name for f in ci.field_inventory(acts)}
        assert "icu_weighted_avg_watts" not in fields
        assert "polarization_index" in fields

    def test_sorted_by_descending_fill_rate(self):
        acts = [_icu(rare=None, common=1), _icu(rare=5, common=1)]
        fills = [f.name for f in ci.field_inventory(acts)]
        assert fills.index("common") < fills.index("rare")

    def test_carries_a_sample_value(self):
        inv = {f.name: f for f in ci.field_inventory([_icu(polarization_index=2.1)])}
        assert inv["polarization_index"].sample == 2.1

    def test_empty_input_returns_empty_list(self):
        assert ci.field_inventory([]) == []


class TestProbeApi:
    def test_counts_distinct_activity_types(self):
        acts = [_icu(type="Ride"), _icu(type="Ride"), _icu(type="VirtualRide")]
        report = ci.probe_api(acts, {})
        assert report.activity_types == {"Ride": 2, "VirtualRide": 1}

    def test_counts_stream_type_names(self):
        streams = {"i1": [{"type": "watts", "data": [1]}, {"type": "heartrate", "data": [2]}]}
        report = ci.probe_api([_icu(id="i1")], streams)
        assert report.stream_types == {"watts": 1, "heartrate": 1}

    def test_falls_back_to_name_when_type_absent(self):
        streams = {"i1": [{"name": "cadence", "data": [1]}]}
        assert ci.probe_api([_icu(id="i1")], streams).stream_types == {"cadence": 1}

    def test_tracks_all_null_streams_separately(self):
        streams = {"i1": [{"type": "watts", "allNull": True}, {"type": "heartrate", "allNull": False}]}
        report = ci.probe_api([_icu(id="i1")], streams)
        assert report.all_null_streams == {"watts": 1}
        assert report.stream_types == {"watts": 1, "heartrate": 1}

    def test_no_streams_probed_yields_empty_stream_counts(self):
        report = ci.probe_api([_icu()], {})
        assert report.stream_types == {}


class TestEnrichVelomateRows:
    def _cp(self):
        return {date(2026, 7, 1): {"cp_watts": 250.0, "w_prime_kj": 20.0}}

    def _stats(self):
        return {date(2026, 7, 1): {"ctl": 55.0, "atl": 60.0}}

    def test_attaches_cp_and_atl_for_the_ride_date(self):
        rows = ci.enrich_velomate_rows([_vm(id=1)], self._cp(), self._stats(), {1: -1500.0})
        assert rows[0]["cp_watts"] == 250.0
        assert rows[0]["w_prime_kj"] == 20.0
        assert rows[0]["ctl"] == 55.0
        assert rows[0]["atl"] == 60.0

    def test_attaches_wbal_minimum_by_activity_id(self):
        rows = ci.enrich_velomate_rows([_vm(id=7)], {}, {}, {7: -1500.0})
        assert rows[0]["min_w_bal"] == -1500.0

    def test_missing_date_leaves_keys_none(self):
        rows = ci.enrich_velomate_rows([_vm(id=1, start="2026-09-09T09:00:00+00:00")],
                                       self._cp(), self._stats(), {})
        assert rows[0]["cp_watts"] is None
        assert rows[0]["ctl"] is None
        assert rows[0]["min_w_bal"] is None

    def test_does_not_mutate_the_input_rows(self):
        original = _vm(id=1)
        ci.enrich_velomate_rows([original], self._cp(), self._stats(), {1: -10.0})
        assert "cp_watts" not in original

    def test_rows_without_a_date_are_passed_through(self):
        row = _vm(id=1)
        row["date"] = None
        out = ci.enrich_velomate_rows([row], self._cp(), self._stats(), {})
        assert out[0]["cp_watts"] is None

    def test_empty_input_returns_empty_list(self):
        assert ci.enrich_velomate_rows([], {}, {}, {}) == []


class TestRenderReport:
    def test_includes_match_counts(self):
        result = ci.MatchResult(pairs=[], by_strava_id=3, by_fuzzy=2,
                                icu_only=[_icu()], velomate_only=[_vm()])
        out = ci.render_report(result, [], [], ci.ProbeReport())
        assert "3" in out and "2" in out
        assert "matched" in out.lower()

    def test_zero_matches_states_it_plainly(self):
        out = ci.render_report(ci.MatchResult(), [], [], ci.ProbeReport())
        assert "no matched rides" in out.lower()

    def test_renders_a_metric_row_with_bias_and_tolerance(self):
        stats = [ci.MetricStats("np", 10, 1.5, 0.8, 90.0, 1.01, None)]
        out = ci.render_report(ci.MatchResult(pairs=[1]), [("All rides", stats)], [], ci.ProbeReport())
        assert "np" in out
        assert "90.0" in out

    def test_surfaces_unit_flags(self):
        stats = [ci.MetricStats("work_kj", 5, 1.0, 2.0, 0.0, 0.001, "likely kJ vs J")]
        out = ci.render_report(ci.MatchResult(pairs=[1]), [("All rides", stats)], [], ci.ProbeReport())
        assert "kJ vs J" in out

    def test_lists_probe_results(self):
        probe = ci.ProbeReport(activity_types={"Ride": 4}, stream_types={"watts": 4})
        out = ci.render_report(ci.MatchResult(), [], [], probe)
        assert "Ride" in out and "watts" in out

    def test_lists_inventory_fields(self):
        inv = [ci.FieldFill("polarization_index", 88.0, 2.1)]
        out = ci.render_report(ci.MatchResult(), [], inv, ci.ProbeReport())
        assert "polarization_index" in out


class _ClosableConn:
    """Minimal stand-in for a psycopg2 connection: main() closes its handle."""

    def __init__(self):
        self.closed = 0

    def close(self):
        self.closed += 1


class TestMainZeroMatch:
    def test_exits_zero_and_reports_when_nothing_matches(self, monkeypatch, capsys):
        monkeypatch.setattr(ci, "_open_velomate_connection", lambda: _ClosableConn())
        monkeypatch.setattr(ci, "fetch_velomate_activities", lambda conn, oldest: [])
        monkeypatch.setattr(ci, "fetch_cp_estimates", lambda conn, oldest: {})
        monkeypatch.setattr(ci, "fetch_athlete_stats", lambda conn, oldest: {})
        monkeypatch.setattr(ci, "fetch_wbal_minima", lambda conn, oldest: {})
        monkeypatch.setattr(ci.intervals_icu, "list_activities", lambda **kw: [])
        assert ci.main(["--months", "3", "--streams", "0"]) == 0
        assert "no matched rides" in capsys.readouterr().out.lower()

    def test_db_connection_is_closed_even_when_a_read_raises(self, monkeypatch, capsys):
        """The handle must be released on the failure path too, not just at
        process exit — the integration pass reuses this pattern in a
        longer-lived context."""
        closed = {"n": 0}

        class _Conn:
            def close(self):
                closed["n"] += 1

        monkeypatch.setattr(ci, "_open_velomate_connection", lambda: _Conn())
        monkeypatch.setattr(ci, "fetch_velomate_activities",
                            lambda conn, oldest: (_ for _ in ()).throw(RuntimeError("query blew up")))
        monkeypatch.setattr(ci.intervals_icu, "list_activities", lambda **kw: [])

        with pytest.raises(RuntimeError):
            ci.main(["--months", "3", "--streams", "0"])

        assert closed["n"] == 1, "connection must be closed when a read raises"

    def test_db_connection_is_closed_on_the_success_path(self, monkeypatch, capsys):
        closed = {"n": 0}

        class _Conn:
            def close(self):
                closed["n"] += 1

        monkeypatch.setattr(ci, "_open_velomate_connection", lambda: _Conn())
        monkeypatch.setattr(ci, "fetch_velomate_activities", lambda conn, oldest: [])
        monkeypatch.setattr(ci, "fetch_cp_estimates", lambda conn, oldest: {})
        monkeypatch.setattr(ci, "fetch_athlete_stats", lambda conn, oldest: {})
        monkeypatch.setattr(ci, "fetch_wbal_minima", lambda conn, oldest: {})
        monkeypatch.setattr(ci.intervals_icu, "list_activities", lambda **kw: [])

        assert ci.main(["--months", "3", "--streams", "0"]) == 0
        assert closed["n"] == 1

    def test_exits_one_when_db_is_unreachable(self, monkeypatch, capsys):
        monkeypatch.setattr(ci, "_open_velomate_connection", lambda: None)
        assert ci.main(["--months", "3"]) == 1
        assert "could not connect" in capsys.readouterr().out.lower()
