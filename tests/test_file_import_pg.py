"""import_file: one file on disk in, one Result per ride file it holds out.

Each member is hashed, checked against the imported_files ledger, parsed only
when the ledger hasn't seen it, and stored through the dedup path. The outcome
says what that did: `imported` (a new ride), `merged` (it filled something on a
ride already stored), `duplicate` (it added nothing), `not_cycling`, `failed`.
These tests need a real PostgreSQL because the outcome is read from what the
SQL actually changed.

Every track is synthetic, near lat 0 / lng 0: the repo is mirrored publicly.

Run with:
    VELOMATE_TEST_DATABASE_URL=postgresql://... python3 -m pytest \
        tests/test_file_import_pg.py -q
"""

import hashlib
import os
import sys
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from pg_reset import reset_schema

_tests_dir = Path(__file__).resolve().parent
if str(_tests_dir) not in sys.path:
    sys.path.insert(0, str(_tests_dir))

from fit_builder import build_fit  # noqa: E402

TEST_DB = os.environ.get("VELOMATE_TEST_DATABASE_URL")

pytestmark = pytest.mark.skipif(
    not TEST_DB,
    reason="needs a throwaway PostgreSQL in VELOMATE_TEST_DATABASE_URL",
)

START = datetime(2026, 6, 1, 8, 0, tzinfo=timezone.utc)
OTHER_START = datetime(2026, 6, 3, 8, 0, tzinfo=timezone.utc)
M_PER_DEG_LAT = 111195.0
SPEED_MS = 30 / 3.6
N = 600
STRAVA_ID = 987654321


@pytest.fixture()
def conn():
    import psycopg2

    if not hasattr(psycopg2, "__file__"):
        pytest.skip("psycopg2 is mocked by another module in this pytest session")

    import db as velomate_db

    connection = psycopg2.connect(TEST_DB)
    connection.autocommit = True
    reset_schema(connection)
    velomate_db.create_schema(connection)
    yield connection
    connection.close()


def _records(start=START, n=N, power=200, hr=140, odd_hr_at=None):
    """n records at 1 Hz heading north from (0, 0) at 30 km/h.

    ``power=None`` leaves the power channel out entirely. ``odd_hr_at`` changes
    one heart-rate sample, which is enough to change the file's bytes.
    """
    records, lat = [], 0.0
    for i in range(n):
        if i:
            lat += SPEED_MS / M_PER_DEG_LAT
        rec = {"time": start + timedelta(seconds=i), "lat": lat, "lng": 0.0,
               "heart_rate": hr + 1 if i == odd_hr_at else hr, "cadence": 90,
               "speed": SPEED_MS, "altitude": 100.0}
        if power is not None:
            rec["power"] = power
        records.append(rec)
    return records


def _fit(start=START, sport="cycling", calories=None, **record_kw) -> bytes:
    return build_fit(_records(start, **record_kw), sport=sport,
                     sub_sport="road" if sport == "cycling" else "generic",
                     total_timer_time=N, total_calories=calories)


def _write(tmp_path, name, raw) -> str:
    path = tmp_path / name
    path.write_bytes(raw)
    return str(path)


def _sha(raw) -> str:
    return hashlib.sha256(raw).hexdigest()


def _seed_strava(conn, *, stream_fit: bytes, **overrides) -> int:
    """A Strava ride of the same ride, inserted directly, with streams parsed
    from ``stream_fit``. Every column the skip path can fill is already set,
    so only the column a test clears can make the file fill anything."""
    import db
    from file_import import parse_fit

    row = {
        "strava_id": STRAVA_ID, "name": "Strava ride", "date": START.isoformat(),
        "distance_m": 5000.0, "duration_s": N, "elevation_m": 50.0,
        "avg_hr": 141, "max_hr": 170, "avg_power": 190, "max_power": 400,
        "avg_cadence": 88, "avg_speed_kmh": 30.0, "calories": 500,
        "suffer_score": 12, "device": "unknown", "is_indoor": False,
        "sport_type": "cycling_outdoor", "tss": 42.0,
    }
    row.update(overrides)
    columns = ", ".join(row)
    placeholders = ", ".join(f"%({c})s" for c in row)
    with conn.cursor() as cur:
        cur.execute(
            f"INSERT INTO activities ({columns}) VALUES ({placeholders}) RETURNING id",
            row)
        activity_id = cur.fetchone()[0]
    db.upsert_streams(conn, activity_id, parse_fit(stream_fit)[1])
    return activity_id


def _one(conn, sql, params=()):
    with conn.cursor() as cur:
        cur.execute(sql, params)
        return cur.fetchone()


def _all(conn, sql, params=()):
    with conn.cursor() as cur:
        cur.execute(sql, params)
        return cur.fetchall()


def _ledger(conn):
    return _all(conn, "SELECT sha256, name, size_bytes, via, outcome, detail, activity_id "
                      "FROM imported_files ORDER BY first_seen, sha256")


def _streams(conn, activity_id):
    """(points, points with power, max power) for one ride."""
    return _one(conn, "SELECT COUNT(*), COUNT(power), MAX(power) FROM activity_streams "
                      "WHERE activity_id = %s", (activity_id,))


@pytest.fixture()
def no_parse(monkeypatch):
    """Make any parse fail loudly. Not a ParseError, which import_file would
    file as `failed`: this has to escape so a test sees the parse happen."""
    import file_import

    def boom(raw):
        raise AssertionError("parsed a file the ledger already holds")

    monkeypatch.setattr(file_import, "parse_fit", boom)
    monkeypatch.setattr(file_import, "parse_gpx", boom)


# ---------------------------------------------------------------- outcomes

def test_new_file_is_imported(conn, tmp_path):
    from file_import import import_file

    raw = _fit()
    results = import_file(conn, _write(tmp_path, "ride.fit", raw), "ride.fit", "folder")

    assert len(results) == 1
    r = results[0]
    rows = _all(conn, "SELECT id, file_sha256, strava_id FROM activities")
    assert rows == [(r.activity_id, _sha(raw), None)]
    assert (r.name, r.outcome, r.detail, r.sha256) == ("ride.fit", "imported", None, _sha(raw))
    assert _streams(conn, r.activity_id) == (N, N, 200)
    assert _ledger(conn) == [
        (_sha(raw), "ride.fit", len(raw), "folder", "imported", None, r.activity_id)]


def test_same_file_twice_returns_ledger_result(conn, tmp_path, monkeypatch):
    import file_import

    raw = _fit()
    path = _write(tmp_path, "ride.fit", raw)
    first = file_import.import_file(conn, path, "ride.fit", "folder")[0]
    with conn.cursor() as cur:
        cur.execute("UPDATE imported_files SET last_seen = '2000-01-01T00:00:00Z'")

    monkeypatch.setattr(file_import, "parse_fit",
                        lambda raw: pytest.fail("parsed a file the ledger holds"))
    second = file_import.import_file(conn, path, "again.fit", "upload")[0]

    assert (second.name, second.outcome, second.activity_id, second.sha256) == (
        "again.fit", "imported", first.activity_id, _sha(raw))
    assert (first.from_ledger, second.from_ledger) == (False, True)
    assert _one(conn, "SELECT COUNT(*) FROM activities") == (1,)
    seen = _one(conn, "SELECT first_seen, last_seen, via FROM imported_files")
    assert seen[1] > datetime(2001, 1, 1, tzinfo=timezone.utc)
    assert seen[1] >= seen[0]
    assert seen[2] == "folder"  # the ledger keeps how it was first seen


def test_cloud_ride_then_same_ride_file_is_duplicate(conn, tmp_path):
    from file_import import import_file

    # Same channels as the file, but other values, so a swap would show.
    strava = _seed_strava(conn, stream_fit=_fit(power=180))
    raw = _fit(power=200)

    r = import_file(conn, _write(tmp_path, "ride.fit", raw), "ride.fit", "folder")[0]

    assert (r.outcome, r.activity_id) == ("duplicate", strava)
    assert _all(conn, "SELECT id, name, tss, calories, avg_power, file_sha256 "
                      "FROM activities") == [
        (strava, "Strava ride", 42.0, 500, 190, _sha(raw))]
    assert _streams(conn, strava) == (N, N, 180)
    assert _ledger(conn)[0][4:] == ("duplicate", None, strava)


def test_powerless_cloud_ride_gets_power_from_file(conn, tmp_path):
    from file_import import import_file

    strava = _seed_strava(conn, stream_fit=_fit(power=None))
    assert _streams(conn, strava)[1] == 0

    r = import_file(conn, _write(tmp_path, "ride.fit", _fit()), "ride.fit", "folder")[0]

    # Every summary column was already set, so only the stream swap can have
    # made this a merge.
    assert (r.outcome, r.activity_id) == ("merged", strava)
    assert _streams(conn, strava) == (N, N, 200)
    assert _all(conn, "SELECT id, name, tss, strava_id FROM activities") == [
        (strava, "Strava ride", None, STRAVA_ID)]


def test_file_fills_empty_summary_column_is_merged(conn, tmp_path):
    from file_import import import_file

    strava = _seed_strava(conn, stream_fit=_fit(power=180), calories=None)
    raw = _fit(power=200, calories=777)

    r = import_file(conn, _write(tmp_path, "ride.fit", raw), "ride.fit", "folder")[0]

    assert (r.outcome, r.activity_id) == ("merged", strava)
    assert _all(conn, "SELECT id, name, calories, tss FROM activities") == [
        (strava, "Strava ride", 777, 42.0)]
    assert _streams(conn, strava) == (N, N, 180)  # no swap: the calories did it


def test_same_ride_different_bytes_is_one_ride(conn, tmp_path):
    from file_import import import_file

    raw_a = _fit()
    raw_b = _fit(odd_hr_at=300)
    assert raw_a != raw_b

    a = import_file(conn, _write(tmp_path, "a.fit", raw_a), "a.fit", "folder")[0]
    b = import_file(conn, _write(tmp_path, "b.fit", raw_b), "b.fit", "folder")[0]

    assert a.outcome == "imported"
    # Both are tier 0 with equal richness, so the later file wins the base and
    # the merge path rewrites the ride: always `merged`, never `duplicate`.
    assert b.outcome == "merged"
    assert _one(conn, "SELECT COUNT(*) FROM activities") == (1,)
    assert {(sha, outcome, activity_id) for sha, _n, _s, _v, outcome, _d, activity_id
            in _ledger(conn)} == {(_sha(raw_a), "imported", a.activity_id),
                                  (_sha(raw_b), b.outcome, a.activity_id)}

    # The first file's hash may have left the row in a file-vs-file merge;
    # its ledger row still answers for it.
    again = import_file(conn, _write(tmp_path, "a2.fit", raw_a), "a2.fit", "folder")[0]
    assert (again.outcome, again.activity_id) == ("imported", a.activity_id)
    assert _one(conn, "SELECT COUNT(*) FROM activities") == (1,)


def test_running_fit_is_not_cycling_and_ledgered(conn, tmp_path, request):
    from file_import import import_file

    raw = _fit(sport="running")
    path = _write(tmp_path, "run.fit", raw)
    r = import_file(conn, path, "run.fit", "folder")[0]

    assert (r.outcome, r.detail, r.activity_id, r.sha256) == (
        "not_cycling", "running", None, _sha(raw))
    assert _one(conn, "SELECT COUNT(*) FROM activities") == (0,)
    assert _ledger(conn) == [(_sha(raw), "run.fit", len(raw), "folder",
                              "not_cycling", "running", None)]

    request.getfixturevalue("no_parse")
    again = import_file(conn, path, "run.fit", "folder")[0]
    assert (again.outcome, again.detail) == ("not_cycling", "running")

    # Control: without the ledger row the same call does parse, so the
    # assertion above is what kept it from parsing.
    with conn.cursor() as cur:
        cur.execute("DELETE FROM imported_files")
    with pytest.raises(AssertionError, match="ledger already holds"):
        import_file(conn, path, "run.fit", "folder")


def test_corrupt_file_failed_and_not_retried(conn, tmp_path, request):
    from file_import import ParseError, import_file, parse_fit

    raw = b"this is not a FIT file at all"
    with pytest.raises(ParseError) as err:
        parse_fit(raw)
    path = _write(tmp_path, "bad.fit", raw)

    r = import_file(conn, path, "bad.fit", "upload")[0]

    assert (r.outcome, r.detail, r.activity_id) == ("failed", str(err.value), None)
    assert _ledger(conn) == [(_sha(raw), "bad.fit", len(raw), "upload",
                              "failed", str(err.value), None)]

    request.getfixturevalue("no_parse")
    again = import_file(conn, path, "bad.fit", "upload")[0]
    assert (again.outcome, again.detail) == ("failed", str(err.value))

    with conn.cursor() as cur:
        cur.execute("DELETE FROM imported_files")
    with pytest.raises(AssertionError, match="ledger already holds"):
        import_file(conn, path, "bad.fit", "upload")


def test_reimport_without_ledger_row_is_idempotent(conn, tmp_path):
    from file_import import import_file

    raw = _fit()
    path = _write(tmp_path, "ride.fit", raw)
    first = import_file(conn, path, "ride.fit", "folder")[0]
    # What recalculation would have written. Rewriting the streams of the same
    # bytes would clear the W' and the segment matches for nothing.
    with conn.cursor() as cur:
        cur.execute("UPDATE activities SET tss = 30, wbal_cp = 210, wbal_w_prime_j = 20000")
        cur.execute("SELECT id FROM activity_streams ORDER BY id LIMIT 1")
        first_point = cur.fetchone()
        cur.execute("DELETE FROM imported_files")

    again = import_file(conn, path, "ride.fit", "folder")[0]

    assert (again.outcome, again.activity_id) == ("duplicate", first.activity_id)
    assert _all(conn, "SELECT id, file_sha256, tss, wbal_cp, wbal_w_prime_j "
                      "FROM activities") == [
        (first.activity_id, _sha(raw), 30.0, 210.0, 20000.0)]
    assert _one(conn, "SELECT id FROM activity_streams ORDER BY id LIMIT 1") == first_point
    assert _ledger(conn)[0][4:] == ("duplicate", None, first.activity_id)


def test_file_already_merged_onto_cloud_ride_without_ledger_row(conn, tmp_path):
    from file_import import import_file

    strava = _seed_strava(conn, stream_fit=_fit(power=None))
    path = _write(tmp_path, "ride.fit", _fit())
    assert import_file(conn, path, "ride.fit", "folder")[0].outcome == "merged"
    with conn.cursor() as cur:
        cur.execute("UPDATE activities SET tss = 30")
        cur.execute("DELETE FROM imported_files")

    again = import_file(conn, path, "ride.fit", "folder")[0]

    assert (again.outcome, again.activity_id) == ("duplicate", strava)
    assert _all(conn, "SELECT name, tss FROM activities") == [("Strava ride", 30.0)]


def _fail_once(monkeypatch, module, attr):
    """Make module.attr raise on its first call only, as a dropped connection would."""
    real = getattr(module, attr)
    calls = []

    def flaky(*args, **kwargs):
        calls.append(args)
        if len(calls) == 1:
            raise RuntimeError("connection dropped")
        return real(*args, **kwargs)

    monkeypatch.setattr(module, attr, flaky)


def test_failed_stream_write_is_recovered_on_retry(conn, tmp_path, monkeypatch):
    import db
    from file_import import import_file

    raw = _fit()
    path = _write(tmp_path, "ride.fit", raw)
    _fail_once(monkeypatch, db, "upsert_streams")

    with pytest.raises(RuntimeError, match="connection dropped"):
        import_file(conn, path, "ride.fit", "folder")
    # The row and its hash committed before the stream write failed.
    activity_id, = _one(conn, "SELECT id FROM activities WHERE file_sha256 = %s", (_sha(raw),))
    assert _streams(conn, activity_id) == (0, 0, None)
    assert _ledger(conn) == []

    again = import_file(conn, path, "ride.fit", "folder")[0]

    assert (again.outcome, again.activity_id) == ("merged", activity_id)
    assert _streams(conn, activity_id) == (N, N, 200)
    assert _ledger(conn) == [(_sha(raw), "ride.fit", len(raw), "folder",
                              "merged", None, activity_id)]


def test_failed_stream_swap_is_recovered_on_retry(conn, tmp_path, monkeypatch):
    import db
    from file_import import import_file

    strava = _seed_strava(conn, stream_fit=_fit(power=None))
    raw = _fit()
    path = _write(tmp_path, "ride.fit", raw)
    _fail_once(monkeypatch, db, "replace_streams_if_richer")

    with pytest.raises(RuntimeError, match="connection dropped"):
        import_file(conn, path, "ride.fit", "folder")
    # The skip path's UPDATE stamped the hash before the swap failed.
    assert _one(conn, "SELECT file_sha256 FROM activities WHERE id = %s", (strava,)) == (
        _sha(raw),)
    assert _streams(conn, strava)[1] == 0

    again = import_file(conn, path, "ride.fit", "folder")[0]

    assert (again.outcome, again.activity_id) == ("merged", strava)
    assert _streams(conn, strava) == (N, N, 200)
    assert _all(conn, "SELECT name, tss FROM activities") == [("Strava ride", None)]
    assert _ledger(conn)[0][4:] == ("merged", None, strava)


def test_zip_yields_one_result_per_ride(conn, tmp_path):
    from file_import import import_file

    ride_1, run, ride_2 = _fit(), _fit(sport="running"), _fit(start=OTHER_START)
    archive = tmp_path / "export.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("activities/1.fit", ride_1)
        zf.writestr("activities/2.fit", run)
        zf.writestr("activities/3.fit", ride_2)

    results = import_file(conn, str(archive), "export.zip", "upload")

    assert [(r.name, r.outcome, r.sha256) for r in results] == [
        ("export.zip/activities/1.fit", "imported", _sha(ride_1)),
        ("export.zip/activities/2.fit", "not_cycling", _sha(run)),
        ("export.zip/activities/3.fit", "imported", _sha(ride_2)),
    ]
    assert results[0].activity_id != results[2].activity_id
    assert _one(conn, "SELECT COUNT(*) FROM activities") == (2,)
    assert len(_ledger(conn)) == 3


def test_unreadable_member_is_failed_without_a_ledger_row(conn, tmp_path, monkeypatch):
    import file_import

    monkeypatch.setattr(file_import, "iter_members", lambda path, name: iter(
        [("big.zip/huge.fit", file_import.ParseError("huge.fit is too large"))]))

    results = file_import.import_file(conn, str(tmp_path / "big.zip"), "big.zip", "folder")

    assert [(r.name, r.outcome, r.detail, r.activity_id, r.sha256) for r in results] == [
        ("big.zip/huge.fit", "failed", "huge.fit is too large", None, None)]
    assert _ledger(conn) == []


def test_missing_path_raises(conn, tmp_path):
    from file_import import import_file

    with pytest.raises(OSError):
        import_file(conn, str(tmp_path / "gone.fit"), "gone.fit", "folder")


def test_changed_outcomes_are_the_ones_needing_recalculation():
    from file_import import CHANGED_OUTCOMES

    assert CHANGED_OUTCOMES == {"imported", "merged"}


# ---------------------------------------------------------------- folder scan

def _paths(conn):
    return _all(conn, "SELECT path, size_bytes, mtime_ns FROM imported_paths ORDER BY path")


def _stat_key(path):
    st = os.stat(path)
    return st.st_size, st.st_mtime_ns


def test_scan_imports_new_files_and_skips_unchanged(conn, tmp_path, monkeypatch):
    import file_import

    sub = tmp_path / "2026"
    sub.mkdir()
    first = _write(tmp_path, "a.fit", _fit())
    second = _write(sub, "b.fit", _fit(start=OTHER_START))

    assert file_import.scan_folder(conn, str(tmp_path)) == (2, [])
    assert _one(conn, "SELECT COUNT(*) FROM activities") == (2,)
    assert _paths(conn) == [("2026/b.fit", *_stat_key(second)), ("a.fit", *_stat_key(first))]
    assert sorted(r[1] for r in _ledger(conn)) == ["2026/b.fit", "a.fit"]

    monkeypatch.setattr(file_import, "import_file",
                        lambda *a, **k: pytest.fail("read a file whose path is unchanged"))
    assert file_import.scan_folder(conn, str(tmp_path)) == (0, [])


def test_changed_file_is_read_again(conn, tmp_path, monkeypatch):
    import file_import

    path = _write(tmp_path, "a.fit", _fit())
    assert file_import.scan_folder(conn, str(tmp_path)) == (1, [])

    raw = _fit(start=OTHER_START)
    Path(path).write_bytes(raw)
    os.utime(path, ns=(2_000_000_000_000_000_000, 2_000_000_000_000_000_000))
    seen = []
    real = file_import.import_file
    monkeypatch.setattr(file_import, "import_file",
                        lambda *a, **k: seen.append(a[2]) or real(*a, **k))

    assert file_import.scan_folder(conn, str(tmp_path)) == (1, [])
    assert seen == ["a.fit"]
    assert _paths(conn) == [("a.fit", len(raw), 2_000_000_000_000_000_000)]
    assert _one(conn, "SELECT COUNT(*) FROM activities") == (2,)


def test_time_bound_mid_zip_resumes_next_pass(conn, tmp_path, monkeypatch):
    import file_import

    rides = [_fit(start=START + timedelta(days=d)) for d in range(3)]
    archive = tmp_path / "export.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        for i, raw in enumerate(rides):
            zf.writestr(f"{i}.fit", raw)

    # The clock jumps past the budget as soon as the first ride is stored.
    clock = [0.0]
    real_member = file_import._import_member

    def slow_member(*args, **kwargs):
        result = real_member(*args, **kwargs)
        clock[0] += 1000
        return result

    with monkeypatch.context() as m:
        m.setattr(file_import.time, "monotonic", lambda: clock[0])
        m.setattr(file_import, "_import_member", slow_member)
        assert file_import.scan_folder(conn, str(tmp_path), budget_s=10) == (1, [])
    assert _one(conn, "SELECT COUNT(*) FROM activities") == (1,)
    assert _paths(conn) == []

    # The first ride comes back from the ledger and was counted by the pass
    # that stored it, so only the two new ones count.
    assert file_import.scan_folder(conn, str(tmp_path)) == (2, [])
    assert _one(conn, "SELECT COUNT(*), COUNT(DISTINCT file_sha256) FROM activities") == (3, 3)
    assert sorted(r[0] for r in _ledger(conn)) == sorted(_sha(r) for r in rides)
    assert _paths(conn) == [("export.zip", *_stat_key(archive))]


def test_a_spent_deadline_still_imports_the_first_member(conn, tmp_path):
    import time

    import file_import

    archive = tmp_path / "export.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        for d in range(3):
            zf.writestr(f"{d}.fit", _fit(start=START + timedelta(days=d)))

    with pytest.raises(file_import.PassBudgetExceeded) as raised:
        file_import.import_file(conn, str(archive), "export.zip", "folder",
                                deadline=time.monotonic() - 1)

    assert [(r.name, r.outcome) for r in raised.value.results] == [
        ("export.zip/0.fit", "imported")]
    assert _one(conn, "SELECT COUNT(*) FROM activities") == (1,)


def test_budget_spent_before_a_file_leaves_it_for_the_next_pass(conn, tmp_path, monkeypatch):
    import file_import

    _write(tmp_path, "a.fit", _fit())
    _write(tmp_path, "b.fit", _fit(start=OTHER_START))
    clock = [0.0]
    real_member = file_import._import_member

    def slow_member(*args, **kwargs):
        result = real_member(*args, **kwargs)
        clock[0] += 1000
        return result

    with monkeypatch.context() as m:
        m.setattr(file_import.time, "monotonic", lambda: clock[0])
        m.setattr(file_import, "_import_member", slow_member)
        assert file_import.scan_folder(conn, str(tmp_path), budget_s=10) == (1, [])
    assert [p[0] for p in _paths(conn)] == ["a.fit"]

    assert file_import.scan_folder(conn, str(tmp_path)) == (1, [])
    assert [p[0] for p in _paths(conn)] == ["a.fit", "b.fit"]


@pytest.mark.parametrize("make", ["missing", "empty"])
def test_scan_of_missing_or_empty_dir_is_a_quiet_success(conn, tmp_path, make):
    import file_import

    root = tmp_path / "import"
    if make == "empty":
        root.mkdir()
    assert file_import.scan_folder(conn, str(root)) == (0, [])
    assert _paths(conn) == []


def test_unreadable_file_is_a_problem_and_the_pass_goes_on(conn, tmp_path, monkeypatch):
    import file_import

    _write(tmp_path, "a.fit", _fit())
    _write(tmp_path, "b.fit", _fit(start=OTHER_START))
    real = file_import.import_file

    def flaky(conn_, path, name, via, **kw):
        if name == "a.fit":
            raise PermissionError(13, "Permission denied", path)
        return real(conn_, path, name, via, **kw)

    monkeypatch.setattr(file_import, "import_file", flaky)
    changed, problems = file_import.scan_folder(conn, str(tmp_path))

    assert changed == 1
    assert len(problems) == 1 and problems[0].startswith("a.fit: ")
    assert "Permission denied" in problems[0]
    assert [p[0] for p in _paths(conn)] == ["b.fit"]


@pytest.mark.parametrize("error", ["OperationalError", "InterfaceError"])
def test_database_error_ends_the_pass(conn, tmp_path, monkeypatch, error):
    import psycopg2

    import file_import

    _write(tmp_path, "a.fit", _fit())
    _write(tmp_path, "b.fit", _fit(start=OTHER_START))
    calls = []

    def broken(conn_, path, name, via, **kw):
        calls.append(name)
        raise getattr(psycopg2, error)("server closed the connection")

    monkeypatch.setattr(file_import, "import_file", broken)
    changed, problems = file_import.scan_folder(conn, str(tmp_path))

    assert calls == ["a.fit"]
    assert changed == 0
    assert problems == ["a.fit: server closed the connection"]
    assert _paths(conn) == []


def test_symlink_is_not_followed(conn, tmp_path):
    import file_import

    outside = tmp_path / "outside"
    outside.mkdir()
    target = _write(outside, "ride.fit", _fit())
    root = tmp_path / "import"
    root.mkdir()
    (root / "link.fit").symlink_to(target)
    (root / "dir").symlink_to(outside, target_is_directory=True)

    assert file_import.scan_folder(conn, str(root)) == (0, [])
    assert _one(conn, "SELECT COUNT(*) FROM activities") == (0,)


# ---------------------------------------------------------------- one bad file

def _gpx(start=START, hr="140", n=5, first_time=None):
    """A short synthetic GPX heading north from (0, 0), 1 s per point."""
    pts = []
    for i in range(n):
        t = start + timedelta(seconds=i)
        stamp = first_time if (i == 0 and first_time) else f"{t:%Y-%m-%dT%H:%M:%SZ}"
        pts.append(f'<trkpt lat="{i * 0.0001:.4f}" lon="0"><ele>100</ele><time>{stamp}</time>'
                   f'<extensions><gpxtpx:TrackPointExtension><gpxtpx:hr>{hr}</gpxtpx:hr>'
                   '</gpxtpx:TrackPointExtension></extensions></trkpt>')
    return ('<?xml version="1.0" encoding="UTF-8"?><gpx version="1.1" creator="x" '
            'xmlns="http://www.topografix.com/GPX/1/1" '
            'xmlns:gpxtpx="http://www.garmin.com/xmlschemas/TrackPointExtension/v1">'
            f'<trk><name>r</name><trkseg>{"".join(pts)}</trkseg></trk></gpx>').encode()


def _no_reads(monkeypatch):
    import file_import

    monkeypatch.setattr(file_import, "import_file",
                        lambda *a, **k: pytest.fail(f"read {a[2]} again"))


def test_hr_no_column_holds_keeps_the_ride_and_the_scan_going(conn, tmp_path, monkeypatch):
    import file_import

    _write(tmp_path, "a_bad.gpx", _gpx(hr="99999999999"))
    _write(tmp_path, "b_good.gpx", _gpx(start=OTHER_START))

    assert file_import.scan_folder(conn, str(tmp_path)) == (2, [])
    assert _all(conn, "SELECT name, outcome FROM imported_files ORDER BY name") == [
        ("a_bad.gpx", "imported"), ("b_good.gpx", "imported")]
    assert _all(conn, "SELECT avg_hr, max_hr FROM activities ORDER BY date") == [
        (None, None), (140, 140)]
    assert [p[0] for p in _paths(conn)] == ["a_bad.gpx", "b_good.gpx"]

    _no_reads(monkeypatch)
    assert file_import.scan_folder(conn, str(tmp_path)) == (0, [])


def test_point_dated_1900_fails_the_file_and_the_scan_goes_on(conn, tmp_path, monkeypatch):
    import file_import

    _write(tmp_path, "a_bad.gpx", _gpx(first_time="1900-01-01T00:00:00Z"))
    _write(tmp_path, "b_good.gpx", _gpx(start=OTHER_START))

    assert file_import.scan_folder(conn, str(tmp_path)) == (1, [])
    ledger = dict((name, (outcome, detail)) for name, outcome, detail in _all(
        conn, "SELECT name, outcome, detail FROM imported_files"))
    assert ledger["a_bad.gpx"][0] == "failed" and "7 days" in ledger["a_bad.gpx"][1]
    assert ledger["b_good.gpx"] == ("imported", None)
    assert _one(conn, "SELECT COUNT(*) FROM activities") == (1,)
    assert [p[0] for p in _paths(conn)] == ["a_bad.gpx", "b_good.gpx"]

    _no_reads(monkeypatch)
    assert file_import.scan_folder(conn, str(tmp_path)) == (0, [])


def test_database_rejecting_a_ride_fails_the_file_and_the_scan_goes_on(conn, tmp_path,
                                                                       monkeypatch):
    import psycopg2

    import file_import

    bad = _fit()
    _write(tmp_path, "a_bad.fit", bad)
    _write(tmp_path, "b_good.fit", _fit(start=OTHER_START))
    real_store = file_import._store

    def store(conn_, data, streams):
        if data["file_sha256"] == _sha(bad):
            raise psycopg2.DataError("integer out of range\n")
        return real_store(conn_, data, streams)

    monkeypatch.setattr(file_import, "_store", store)
    assert file_import.scan_folder(conn, str(tmp_path)) == (1, [])
    assert _all(conn, "SELECT name, outcome, detail, activity_id FROM imported_files "
                      "ORDER BY name")[0] == ("a_bad.fit", "failed", "integer out of range", None)
    assert _one(conn, "SELECT COUNT(*) FROM activities") == (1,)
    assert [p[0] for p in _paths(conn)] == ["a_bad.fit", "b_good.fit"]

    _no_reads(monkeypatch)
    assert file_import.scan_folder(conn, str(tmp_path)) == (0, [])


def test_an_unexpected_error_from_one_file_does_not_stop_the_scan(conn, tmp_path, monkeypatch):
    import file_import

    _write(tmp_path, "a.fit", _fit())
    _write(tmp_path, "b.fit", _fit(start=OTHER_START))
    real = file_import.import_file

    def buggy(conn_, path, name, via, **kw):
        if name == "a.fit":
            raise ValueError("a bug reading a.fit")
        return real(conn_, path, name, via, **kw)

    monkeypatch.setattr(file_import, "import_file", buggy)
    assert file_import.scan_folder(conn, str(tmp_path)) == (1, ["a.fit: a bug reading a.fit"])
    assert [p[0] for p in _paths(conn)] == ["b.fit"]


def test_rides_stored_before_a_database_error_still_count(conn, tmp_path, monkeypatch):
    # The ledger answers for them next pass, and a ledger hit isn't counted, so
    # this pass is the only one that can ask for the recalculation.
    import psycopg2

    import file_import

    archive = tmp_path / "export.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("0.fit", _fit())
        zf.writestr("1.fit", _fit(start=OTHER_START))
    real_member = file_import._import_member

    def member(conn_, name, payload, via):
        if name.endswith("1.fit"):
            raise psycopg2.OperationalError("server closed the connection")
        return real_member(conn_, name, payload, via)

    monkeypatch.setattr(file_import, "_import_member", member)
    changed, problems = file_import.scan_folder(conn, str(tmp_path))
    assert (changed, problems) == (1, ["export.zip: server closed the connection"])
    assert _paths(conn) == []


# ---------------------------------------------------------------- outcome log

def test_each_fresh_outcome_is_logged_and_ledger_hits_are_quiet(conn, tmp_path, capsys):
    import file_import

    (tmp_path / "bad.zip").write_bytes(b"not a zip")
    _write(tmp_path, "good.fit", _fit())
    _write(tmp_path, "run.fit", _fit(sport="running"))

    assert file_import.scan_folder(conn, str(tmp_path)) == (1, [])
    bad_zip = "[file] bad.zip: failed (not a readable zip: no end of central directory)"
    assert capsys.readouterr().out.splitlines() == [
        bad_zip,
        "[file] good.fit: imported",
        "[file] run.fit: not_cycling (running)",
    ]

    # Forget the paths, so every file is read again: the two ledgered ones
    # come back from the ledger, say nothing and change nothing. The archive
    # has no hash to ledger it under, so it is reported again.
    with conn.cursor() as cur:
        cur.execute("DELETE FROM imported_paths")
    assert file_import.scan_folder(conn, str(tmp_path)) == (0, [])
    assert capsys.readouterr().out.splitlines() == [bad_zip]


def test_concurrent_imports_of_same_bytes_ledger_the_storing_outcome(tmp_path, monkeypatch):
    """Two processes importing the same bytes at once: the ledger must record
    the outcome of the call that stored the ride (`imported`), never the
    `duplicate` of the one that lost the race."""
    import threading

    import psycopg2

    import db as velomate_db
    import file_import

    path = _write(tmp_path, "ride.fit", _fit())

    # Widen the window the fix closes: pause just after the lock is released.
    # With the ledger write outside the lock, the loser stores, finds the ride
    # held and ledgers "duplicate" during this pause, ahead of the winner.
    import contextlib
    import time
    real_lock = velomate_db.ingest_lock
    releases = []
    depth = threading.local()

    @contextlib.contextmanager
    def slow_release(conn):
        # upsert_activity takes the lock again inside the import's own: only the
        # outermost release is the real one.
        depth.n = getattr(depth, "n", 0) + 1
        try:
            with real_lock(conn):
                yield
        finally:
            depth.n -= 1
        # Only the first to really release (the one that stored the ride) pauses.
        if depth.n == 0 and not releases:
            releases.append(1)
            time.sleep(0.3)

    monkeypatch.setattr(velomate_db, "ingest_lock", slow_release)

    for attempt in range(20):
        setup = psycopg2.connect(TEST_DB)
        setup.autocommit = True
        reset_schema(setup)
        velomate_db.create_schema(setup)
        conns = [psycopg2.connect(TEST_DB) for _ in range(2)]
        for c in conns:
            c.autocommit = True
        releases.clear()
        barrier = threading.Barrier(2)
        results = [None, None]

        def run(i):
            barrier.wait()
            results[i] = file_import.import_file(conns[i], path, "ride.fit", "upload")[0]

        threads = [threading.Thread(target=run, args=(i,)) for i in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        try:
            with setup.cursor() as cur:
                cur.execute("SELECT COUNT(*) FROM activities")
                assert cur.fetchone()[0] == 1, attempt
                cur.execute("SELECT outcome, activity_id FROM imported_files")
                ledger = cur.fetchall()
            assert len(ledger) == 1, attempt
            storing = [r for r in results if r.outcome == "imported"]
            assert len(storing) == 1, (attempt, results)
            assert ledger[0] == ("imported", storing[0].activity_id), (attempt, ledger)
        finally:
            for c in conns + [setup]:
                c.close()
