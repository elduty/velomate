"""What recalculate_fitness leaves in the tables, on rides simple enough to check.

The tests before these fed recalculation a mock cursor and read back the calls it
made, so a metric could change, or take the wrong FTP, with the suite green. Here
a ride goes in through upsert_activity and a stored row comes out. Every expected
value is worked out in the test, from the numbers in its stream and the metric's
formula, never by calling the code that stored it.

Most rides here sit between two others, so that a stored FTP of 190 W and a global
estimate of 285 W are both in play and a metric reading the wrong one shows.

Run with:
    VELOMATE_TEST_DATABASE_URL=postgresql://... python3 -m pytest \
        tests/test_recalculation_stored_values.py -q
"""

import math
import os
from datetime import date, timedelta

import pytest

from pg_reset import reset_schema

TEST_DB = os.environ.get("VELOMATE_TEST_DATABASE_URL")

pytestmark = pytest.mark.skipif(
    not TEST_DB,
    reason="needs a throwaway PostgreSQL in VELOMATE_TEST_DATABASE_URL",
)

RIDE_FTP = 190     # 200 W for 20 minutes a month before the ride, x 0.95
GLOBAL_FTP = 285   # 300 W for 20 minutes after it, x 0.95

# P = W'/t + CP at the five durations the fit uses, with CP 200 W and W' 12 kJ.
CP_WATTS = 200.0
W_PRIME_KJ = 12.0
CURVE = {60: 400, 120: 300, 300: 240, 600: 220, 1200: 210}


def _day(days_ago):
    return date.today() - timedelta(days=days_ago)


def _ride(conn, days_ago, watts=(), hr=None, offsets=None, **columns):
    """One ride through the intervals.icu write path, at noon, `days_ago` days back.

    A None in `watts` is a second with no power reading. `offsets` replaces the
    default of one row per second, for a stream with whole seconds missing.
    """
    import db as velomate_db

    readings = [p for p in watts if p is not None]
    streams = [{"time_offset": offsets[i] if offsets else i, "power": p,
                "hr": hr[i] if hr else None} for i, p in enumerate(watts)]
    data = {
        "name": f"Ride {days_ago}", "date": f"{_day(days_ago)}T12:00:00+00:00",
        "duration_s": len(watts) or 3600, "distance_m": 20000.0, "elevation_m": 100.0,
        "avg_hr": None, "max_hr": None,
        "avg_power": round(sum(readings) / len(readings)) if readings else None,
        "max_power": max(readings) if readings else None, "avg_cadence": None,
        "avg_speed_kmh": 25.0, "calories": None, "suffer_score": None, "device": "karoo",
        "strava_type": "Ride", "trainer": False, "intervals_icu_id": f"t{days_ago}",
        **columns,
    }
    return velomate_db.upsert_activity(conn, data, streams=streams)[0]


def _ride_between_two_ftps(conn, watts, **kwargs):
    """A ride a month ago whose FTP is 190 W, with today's estimate at 285 W."""
    _ride(conn, 60, [200] * 1300)
    ride = _ride(conn, 30, watts, **kwargs)
    _ride(conn, 10, [300] * 1300)
    return ride


def _recalculate(conn):
    """Recalculate as a running deployment does. See tests/test_merge_in_place.py."""
    import db as velomate_db
    import fitness

    velomate_db.set_sync_state(conn, "metrics_version", fitness.METRICS_VERSION)
    fitness.recalculate_fitness(conn)


def _row(conn, ride, *columns) -> dict:
    with conn.cursor() as cur:
        cur.execute(f"SELECT {', '.join(columns)} FROM activities WHERE id = %s", (ride,))
        return dict(zip(columns, cur.fetchone()))


def _execute(conn, sql, *params):
    with conn.cursor() as cur:
        cur.execute(sql, params)


def _np_by_hand(watts):
    """30 s SMA, always over 30, then 4th power, mean, 4th root."""
    smoothed = [sum(watts[max(0, i - 29):i + 1]) / 30 for i in range(len(watts))]
    return round((sum(s ** 4 for s in smoothed) / len(smoothed)) ** 0.25, 1)


def _stats(conn, day):
    with conn.cursor() as cur:
        cur.execute("SELECT ctl, atl, tsb FROM athlete_stats WHERE date = %s", (day,))
        return cur.fetchone()


def _estimate_row(conn, day):
    with conn.cursor() as cur:
        cur.execute("""SELECT cp_watts, w_prime_kj, r_squared, period_days, duration_count,
                              source, fallback_ftp
                       FROM cp_estimates WHERE date = %s""", (day,))
        return cur.fetchone()


def _curve_rides(conn, oldest_days_ago):
    """Five rides on consecutive days, one per point on the power-duration curve."""
    for days, (seconds, watts) in enumerate(CURVE.items()):
        _ride(conn, oldest_days_ago - days, [watts] * seconds)


@pytest.fixture(autouse=True)
def _no_ambient_config(monkeypatch):
    """Recalculation reads these, and a developer's shell may set them."""
    for name in ("VELOMATE_FTP", "VELOMATE_WEIGHT", "VELOMATE_MAX_HR",
                 "VELOMATE_RESTING_HR", "STRAVA_CLIENT_ID",
                 "STRAVA_CLIENT_SECRET", "STRAVA_REFRESH_TOKEN"):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture()
def conn():
    import psycopg2

    # A mocked psycopg2 has no __file__. See tests/test_vo2max_sql.py.
    if not hasattr(psycopg2, "__file__"):
        pytest.skip(
            "psycopg2 is mocked by another module in this pytest session; run this "
            "file on its own: python -m pytest tests/test_recalculation_stored_values.py"
        )

    import db as velomate_db

    connection = psycopg2.connect(TEST_DB)
    connection.autocommit = True  # what db.get_connection hands production
    reset_schema(connection)
    velomate_db.create_schema(connection)
    yield connection
    connection.close()


# ---------------------------------------------------------------------------
# NP, TSS, IF, VI, EF, work
# ---------------------------------------------------------------------------

def test_a_normal_ride_takes_tss_and_if_from_np(conn):
    """An hour of 250 W and 150 W in 5-minute blocks. Average 200 W, VI about 1.08."""
    watts = ([250] * 300 + [150] * 300) * 6
    ride = _ride_between_two_ftps(conn, watts, avg_hr=140)
    np = _np_by_hand(watts)
    assert 1.0 < np / 200 <= 1.30

    _recalculate(conn)

    stored = _row(conn, ride, "ride_ftp", "np", "variability_index", "ef", "work_kj",
                  "intensity_factor", "tss")
    assert stored["ride_ftp"] == RIDE_FTP
    assert stored["np"] == pytest.approx(np)
    assert stored["variability_index"] == pytest.approx(round(np / 200, 2))
    assert stored["ef"] == pytest.approx(round(np / 140, 2))
    assert stored["work_kj"] == pytest.approx(3600 * 200 / 1000)
    # One hour, so TSS = IF^2 x 100.
    assert stored["intensity_factor"] == pytest.approx(round(np / RIDE_FTP, 2))
    assert stored["tss"] == pytest.approx(round((np / RIDE_FTP) ** 2 * 100, 1))


def test_a_stop_and_go_ride_takes_tss_and_if_from_average_power(conn):
    """5 minutes at 400 W and 15 stopped, three times: average 100 W, VI about 2.8."""
    watts = ([400] * 300 + [0] * 900) * 3
    ride = _ride_between_two_ftps(conn, watts, avg_hr=140)
    np = _np_by_hand(watts)
    assert np / 100 > 1.30

    _recalculate(conn)

    stored = _row(conn, ride, "np", "variability_index", "ef", "intensity_factor", "tss")
    assert stored["np"] == pytest.approx(np)
    assert stored["variability_index"] == pytest.approx(round(np / 100, 2))
    assert stored["ef"] == pytest.approx(round(np / 140, 2))
    assert stored["intensity_factor"] == pytest.approx(0.53)   # 100 / 190
    assert stored["tss"] == pytest.approx(27.7)                # (100 / 190)^2 x 100


def test_a_ride_with_heart_rate_only_uses_lthr(conn):
    """1 h at 150 bpm, max 170, so LTHR is 151. Max HR itself would give 77.9."""
    ride = _ride(conn, 5, avg_hr=150, max_hr=170)

    _recalculate(conn)

    stored = _row(conn, ride, "tss", "intensity_factor")
    assert stored["tss"] == pytest.approx(98.7)                # (150 / 151)^2 x 100
    assert stored["intensity_factor"] is None


def test_a_ride_with_neither_power_nor_heart_rate_scores_zero(conn):
    ride = _ride(conn, 5, [200] * 60)
    _execute(conn, "UPDATE activity_streams SET power = NULL WHERE activity_id = %s", ride)
    _execute(conn, "UPDATE activities SET avg_power = NULL WHERE id = %s", ride)

    _recalculate(conn)

    assert _row(conn, ride, "tss", "intensity_factor", "trimp") == {
        "tss": 0.0, "intensity_factor": None, "trimp": None}


def test_a_ride_with_no_usable_stored_ftp_falls_back_to_the_current_one(conn, monkeypatch):
    """1 h at 200 W: 64 TSS at the current 250 W, 100 at a stored 200 W.

    A NULL is always stamped before this step, so a trigger keeps one in place.
    """
    monkeypatch.setenv("VELOMATE_FTP", "250")
    zero = _ride(conn, 5, avg_power=200)
    null = _ride(conn, 4, avg_power=200)
    usable = _ride(conn, 3, avg_power=200)
    _execute(conn, "UPDATE activities SET ride_ftp = 0 WHERE id = %s", zero)
    _execute(conn, "UPDATE activities SET ride_ftp = 200 WHERE id = %s", usable)
    _execute(conn, """
        CREATE FUNCTION keep_ride_ftp_null() RETURNS trigger AS $$
        BEGIN NEW.ride_ftp := NULL; RETURN NEW; END $$ LANGUAGE plpgsql""")
    _execute(conn, f"""
        CREATE TRIGGER keep_null BEFORE UPDATE ON activities FOR EACH ROW
        WHEN (OLD.id = {null}) EXECUTE FUNCTION keep_ride_ftp_null()""")

    _recalculate(conn)

    for ride in (zero, null):
        assert _row(conn, ride, "tss", "intensity_factor") == {
            "tss": 64.0, "intensity_factor": 0.80}
    assert _row(conn, usable, "tss", "intensity_factor") == {
        "tss": 100.0, "intensity_factor": 1.00}


def test_what_a_ride_already_has_is_not_computed_again(conn):
    """Both stored values are ones the stream could not produce."""
    ride = _ride(conn, 5, [200] * 600, hr=[150] * 600)
    _recalculate(conn)
    assert _row(conn, ride, "np", "trimp")["np"] != 123.0
    _execute(conn, "UPDATE activities SET np = 123, trimp = 7 WHERE id = %s", ride)

    _recalculate(conn)

    assert _row(conn, ride, "np", "trimp") == {"np": 123.0, "trimp": 7.0}


# ---------------------------------------------------------------------------
# Coasting, work above FTP, polarization, decoupling
# ---------------------------------------------------------------------------

def test_coasting_and_the_ftp_metrics_read_the_rides_own_ftp(conn):
    """Of 2400 s pedalling, half is Z1 and a quarter each Z2 and Z3, at FTP 190 W.

    At the global 285 W nothing reaches Z3: no index, and no work above FTP.
    """
    watts = [100] * 1200 + [170] * 600 + [250] * 600 + [0] * 600
    ride = _ride_between_two_ftps(conn, watts)

    _recalculate(conn)

    stored = _row(conn, ride, "coasting_time_s", "kj_above_ftp", "polarization_index",
                  "work_kj")
    assert stored["coasting_time_s"] == 600
    assert stored["kj_above_ftp"] == pytest.approx((250 - RIDE_FTP) * 600 / 1000)
    # log10((0.5 / 0.25) x 0.25 x 100). Counting coasting as Z1 would give 1.78.
    assert stored["polarization_index"] == pytest.approx(round(math.log10(50), 2))
    assert stored["work_kj"] == pytest.approx((1200 * 100 + 600 * 170 + 600 * 250) / 1000)


def test_decoupling_leaves_out_coasting(conn):
    """Second half: same power when pedalling, 10% more heart rate, half of it coasting.

    Pedalling EF falls by exactly the heart rate, so 10%. Averaging the coasting
    in would halve the second half's power and read 120%.
    """
    watts = [200] * 1800 + [0] * 600
    ride = _ride(conn, 5, watts, hr=[150] * 1200 + [165] * 1200)

    _recalculate(conn)

    assert _row(conn, ride, "aerobic_decoupling")["aerobic_decoupling"] == pytest.approx(10.0)


# ---------------------------------------------------------------------------
# TRIMP
# ---------------------------------------------------------------------------

def test_trimp_uses_the_configured_heart_rates_and_caps_hrr(conn, monkeypatch):
    """20 min at HRR 0.75 and 10 min above max, which counts as HRR 1.0.

    The ride's own max_hr of 170 would be the auto-estimate. It must lose to 180.
    """
    monkeypatch.setenv("VELOMATE_MAX_HR", "180")
    monkeypatch.setenv("VELOMATE_RESTING_HR", "60")
    ride = _ride(conn, 5, [200] * 1800, hr=[150] * 1200 + [200] * 600, max_hr=170)

    _recalculate(conn)

    per_minute = lambda hrr: hrr * 0.64 * math.exp(1.92 * hrr)
    expected = 20 * per_minute(0.75) + 10 * per_minute(1.0)
    assert _row(conn, ride, "trimp")["trimp"] == pytest.approx(round(expected, 1))


def test_trimp_defaults_to_a_resting_heart_rate_of_50(conn):
    """HRR (140 - 50) / (170 - 50) = 0.75, for 30 min."""
    ride = _ride(conn, 5, [200] * 1800, hr=[140] * 1800, max_hr=170)

    _recalculate(conn)

    expected = 30 * 0.75 * 0.64 * math.exp(1.92 * 0.75)
    assert _row(conn, ride, "trimp")["trimp"] == pytest.approx(round(expected, 1))


# ---------------------------------------------------------------------------
# CTL, ATL, TSB
# ---------------------------------------------------------------------------

def test_ctl_and_atl_are_42_and_7_day_averages_that_decay_on_rest_days(conn, monkeypatch):
    """Two 100-TSS rides 11 days apart, each an hour at FTP, summed in closed form."""
    monkeypatch.setenv("VELOMATE_FTP", "200")
    first, second = 14, 3
    _ride(conn, first, avg_power=200)
    _ride(conn, second, avg_power=200)

    _recalculate(conn)

    def ema(constant, days_ago):
        return sum(100 / constant * (1 - 1 / constant) ** (ride - days_ago)
                   for ride in (first, second) if ride >= days_ago)

    for days_ago in (first, first - 1, first - 5, second, second - 1, 0):
        ctl, atl, tsb = _stats(conn, _day(days_ago))
        assert ctl == pytest.approx(ema(42, days_ago), abs=0.01)
        assert atl == pytest.approx(ema(7, days_ago), abs=0.01)
        assert tsb == pytest.approx(ema(42, days_ago) - ema(7, days_ago), abs=0.01)


def test_nothing_is_written_with_no_rides(conn):
    _recalculate(conn)

    with conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM athlete_stats")
        assert cur.fetchone()[0] == 0


# ---------------------------------------------------------------------------
# Per-ride FTP
# ---------------------------------------------------------------------------

def test_each_ride_gets_the_best_full_20_minutes_of_the_90_days_before_it(conn):
    """`subject` has one complete 20 minutes in range, the 200 W ride: 190 W.

    Older, partial and later efforts would each give it something else: 285, 475
    and 380 W. The oldest ride has nothing before it and takes today's estimate.
    """
    oldest = _ride(conn, 125, [300] * 1300)
    base = _ride(conn, 60, [200] * 1300)
    short = _ride(conn, 30, [500] * 600)
    subject = _ride(conn, 20, [100] * 300)
    latest = _ride(conn, 5, [400] * 1300)

    _recalculate(conn)

    stored = {name: _row(conn, ride, "ride_ftp")["ride_ftp"] for name, ride in (
        ("oldest", oldest), ("base", base), ("short", short), ("subject", subject),
        ("latest", latest))}
    assert stored == {"oldest": 380, "base": 285, "short": 190, "subject": 190,
                      "latest": 190}


def test_a_configured_ftp_goes_on_every_ride_in_place_of_the_history(conn, monkeypatch):
    monkeypatch.setenv("VELOMATE_FTP", "175")
    first = _ride(conn, 60, [200] * 1300)
    second = _ride(conn, 30, [200] * 1300)

    _recalculate(conn)

    assert _row(conn, first, "ride_ftp")["ride_ftp"] == 175
    assert _row(conn, second, "ride_ftp")["ride_ftp"] == 175


def test_the_estimate_is_stored_beside_a_configured_ftp(conn, monkeypatch):
    """No stream, so the CP step leaves sync_state alone and 200 W is the estimate."""
    import db as velomate_db

    monkeypatch.setenv("VELOMATE_FTP", "175")
    ride = _ride(conn, 5, avg_power=200)

    _recalculate(conn)

    assert velomate_db.get_sync_state(conn, "estimated_ftp") == "200"
    assert _row(conn, ride, "tss")["tss"] == pytest.approx(130.6)    # (200 / 175)^2 x 100


# ---------------------------------------------------------------------------
# Intervals
# ---------------------------------------------------------------------------

def test_an_interval_after_a_power_gap_keeps_its_real_start_and_the_rides_ftp(conn):
    """300 s at 250 W after a 100 s dropout: sample 200, but second 300 of the ride.

    Anaerobic at FTP 190 W, and only sweetspot at the global 285 W.
    """
    watts = [100] * 200 + [None] * 100 + [250] * 300 + [100] * 200
    ride = _ride_between_two_ftps(conn, watts, hr=[120] * 300 + [170] * 300 + [120] * 200)

    _recalculate(conn)

    with conn.cursor() as cur:
        cur.execute("""SELECT start_offset_s, duration_s, avg_power, np, avg_hr, classification
                       FROM ride_intervals WHERE activity_id = %s""", (ride,))
        rows = cur.fetchall()
    assert rows == [(300, 300, 250, _np_by_hand([250] * 300), 170, "anaerobic")]


# ---------------------------------------------------------------------------
# CP, W', W'bal
# ---------------------------------------------------------------------------

def test_fit_period_finds_the_curve_it_is_given(conn):
    import fitness

    _curve_rides(conn, 20)

    cp, w_prime_kj, r_squared, durations = fitness.fit_period(conn, days=90)

    assert cp == pytest.approx(CP_WATTS)
    assert w_prime_kj == pytest.approx(W_PRIME_KJ)
    assert r_squared == pytest.approx(1.0)
    assert durations == sorted(CURVE)


def test_fit_period_counts_its_window_back_from_today(conn):
    """A 60 s best of 1000 W would pull the fit off the curve if it were read."""
    import fitness

    _ride(conn, 100, [1000] * 60)
    _curve_rides(conn, 20)

    assert fitness.fit_period(conn, days=90)[0] == pytest.approx(CP_WATTS)


def test_fit_period_on_a_past_date_reads_only_the_window_before_it(conn):
    import fitness

    _ride(conn, 120, [1000] * 60)
    _curve_rides(conn, 30)
    _ride(conn, 5, [1000] * 60)

    fit = fitness.fit_period(conn, days=90, as_of_date=str(_day(25)))

    assert fit[0] == pytest.approx(CP_WATTS)
    assert fit[1] == pytest.approx(W_PRIME_KJ)


def test_the_estimate_counts_a_ride_from_today(conn):
    """A ride is timestamped, so comparing it with today's date compares it with
    midnight, and a ride this morning would wait for tomorrow to count."""
    import fitness

    _ride(conn, 0, [240] * 1200)

    assert fitness.estimate_ftp(conn) == 228  # 240 W x 0.95


def test_the_estimate_on_a_past_date_counts_that_day_and_stops_there(conn):
    import fitness

    _ride(conn, 10, [200] * 1200)
    _ride(conn, 5, [300] * 1200)

    assert fitness.estimate_ftp(conn, as_of_date=str(_day(5))) == 285  # 300 W x 0.95
    assert fitness.estimate_ftp(conn, as_of_date=str(_day(6))) == 190  # 200 W x 0.95


def test_the_estimate_with_no_streams_counts_that_day_too(conn):
    """The fallback for rides with no 20 minutes of power to read."""
    import fitness

    _ride(conn, 5, avg_power=190)

    assert fitness.estimate_ftp(conn, as_of_date=str(_day(5))) == 190
    assert fitness.estimate_ftp(conn, as_of_date=str(_day(6))) == fitness.DEFAULT_FTP


def test_fit_period_on_a_past_date_counts_that_day_and_stops_there(conn):
    """The ride on the date itself is the one off the curve, so the fit shows
    whether it was read. Today's fit already reads today's rides."""
    import fitness

    _curve_rides(conn, 30)
    _ride(conn, 5, [1000] * 60)

    assert fitness.fit_period(conn, days=90, as_of_date=str(_day(6)))[0] == pytest.approx(CP_WATTS)
    assert fitness.fit_period(conn, days=90, as_of_date=str(_day(5)))[0] != pytest.approx(CP_WATTS)
    assert fitness.fit_period(conn, days=90)[0] != pytest.approx(CP_WATTS)


def test_a_rides_own_ftp_still_comes_from_the_rides_before_it(conn):
    """A bigger effort on the same day can't be what the ride is scored against."""
    _ride(conn, 10, [200] * 1200)
    ride = _ride(conn, 5, [300] * 1200)
    _ride(conn, 5, [320] * 1200)

    _recalculate(conn)

    assert _row(conn, ride, "ride_ftp") == {"ride_ftp": 190}  # 200 W x 0.95


def test_a_good_fit_is_stored_with_what_it_rests_on(conn):
    """The 20-minute fallback is 210 W x 0.95, kept beside the fit to compare."""
    import db as velomate_db
    import fitness

    _curve_rides(conn, 20)

    fitness.compute_cp_estimate(conn)

    cp, w_prime_kj, r_squared, days, durations, source, fallback = _estimate_row(
        conn, date.today())
    assert (days, durations, source, fallback) == (90, 5, "cp", 200)
    assert cp == pytest.approx(CP_WATTS)
    assert w_prime_kj == pytest.approx(W_PRIME_KJ)
    assert r_squared == pytest.approx(1.0)
    assert velomate_db.get_sync_state(conn, "estimated_ftp") == "200"
    assert velomate_db.get_sync_state(conn, "estimated_ftp_source") == "cp"


def test_a_ride_too_short_to_fit_falls_back_and_stores_no_cp(conn):
    """Two durations can't pass the gate. 300 W is the only avg_power there is."""
    import db as velomate_db
    import fitness

    _ride(conn, 5, [300] * 120)

    fitness.compute_cp_estimate(conn)

    assert _estimate_row(conn, date.today()) == (
        None, None, None, None, None, "20min_fallback", 300)
    assert velomate_db.get_sync_state(conn, "estimated_ftp") == "300"


def test_a_thin_90_days_widens_to_180(conn):
    """One duration in the last 90 days, and the whole curve in the 90 before."""
    import fitness

    _curve_rides(conn, 150)
    _ride(conn, 5, [100] * 60)

    fitness.compute_cp_estimate(conn)

    row = _estimate_row(conn, date.today())
    assert (row[3], row[5]) == (180, "cp")
    assert row[0] == pytest.approx(CP_WATTS)


def test_backfill_fits_each_past_date_on_what_had_been_ridden_by_then(conn):
    """Two rides before the 15-day mark, so too few durations there. All five by 10."""
    import fitness

    for days_ago, (seconds, watts) in zip((30, 25, 12, 11, 10), CURVE.items()):
        _ride(conn, days_ago, [watts] * seconds)
    _ride(conn, 15, [100] * 60)
    _ride(conn, 5, [100] * 60)

    assert fitness.backfill_cp_estimates(conn) == 7

    early = _estimate_row(conn, _day(15))
    assert (early[0], early[5]) == (None, "20min_fallback")
    late = _estimate_row(conn, _day(5))
    assert (late[3], late[4], late[5]) == (90, 5, "cp")
    assert late[0] == pytest.approx(CP_WATTS)
    assert late[1] == pytest.approx(W_PRIME_KJ)


def test_recalculation_models_a_ride_on_the_estimate_fitted_by_its_own_date(conn):
    """A ride before the curve has no fit, so the 20 kJ default. One after it has the
    fit's 12 kJ, and 60 s at CP + 100 W takes 6 kJ off that."""
    early = _ride(conn, 30, [300] * 60)
    _curve_rides(conn, 20)
    late = _ride(conn, 10, [300] * 60)

    _recalculate(conn)

    assert _row(conn, early, "wbal_w_prime_j")["wbal_w_prime_j"] == 20000
    assert _row(conn, late, "wbal_cp", "wbal_w_prime_j") == pytest.approx(
        {"wbal_cp": CP_WATTS, "wbal_w_prime_j": W_PRIME_KJ * 1000})
    with conn.cursor() as cur:
        cur.execute("""SELECT w_bal FROM activity_streams
                       WHERE activity_id = %s AND time_offset = 59""", (late,))
        assert cur.fetchone()[0] == pytest.approx(W_PRIME_KJ * 1000 - 60 * 100)


def test_w_bal_lands_on_real_time_offsets_across_missing_seconds(conn):
    """60 s at CP + 100 W leaves 6 kJ of 12, then a 40 s hole, then coasting.

    The stream has no rows for the hole, so the first coasting sample is the 61st
    row and second 100. Recovery there follows tau at P = 0 W.
    """
    import fitness

    ride = _ride(conn, 5, [300] * 60 + [0] * 30,
                 offsets=list(range(60)) + list(range(100, 130)))
    _execute(conn, """INSERT INTO cp_estimates (date, cp_watts, w_prime_kj, source)
                      VALUES (%s, %s, %s, 'cp')""", _day(5), CP_WATTS, W_PRIME_KJ)

    assert fitness.compute_wbal_for_rides(conn) == 1

    tau = 546 * math.exp(-0.01 * CP_WATTS) + 316
    with conn.cursor() as cur:
        cur.execute("""SELECT time_offset, w_bal FROM activity_streams
                       WHERE activity_id = %s AND time_offset IN (59, 100)
                       ORDER BY time_offset""", (ride,))
        (_, drained), (_, recovering) = cur.fetchall()
    assert drained == pytest.approx(6000.0)
    assert recovering == pytest.approx(12000 - 6000 * math.exp(-1 / tau))
