"""Tests for the parse and unpack half of ingestor/file_import.py (no database).

Every track here is synthetic, near lat 0 / lng 0: the repo is mirrored
publicly, so no real ride data goes in a fixture.
"""

import gzip
import io
import random
import struct
import sys
import time
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import fitdecode
import pytest

_tests_dir = Path(__file__).resolve().parent
_ingestor_dir = _tests_dir.parent / "ingestor"
for _d in (_ingestor_dir, _tests_dir):
    if str(_d) not in sys.path:
        sys.path.insert(0, str(_d))

import file_import  # noqa: E402
from file_import import NotCycling, ParseError, iter_members, parse_fit, parse_gpx  # noqa: E402
from fit_builder import build_fit, fit_crc  # noqa: E402

START = datetime(2026, 5, 1, 6, 30, tzinfo=timezone.utc)
M_PER_DEG_LAT = 111195.0  # route_match's earth radius, per degree of latitude
SPEED_MS = 30 / 3.6


def _ride(n=600, stop_at=None, stop_s=0, gps=True):
    """n records at 1 Hz heading north from (0, 0) at 30 km/h.

    With ``stop_at``, the rider stands still for ``stop_s`` seconds after record
    ``stop_at``: same position, speed and power 0.
    """
    records, lat, t = [], 0.0, START
    for i in range(n):
        stopped = stop_at is not None and stop_at < i <= stop_at + stop_s
        if i and not stopped:
            lat += SPEED_MS / M_PER_DEG_LAT
        rec = {"time": t, "heart_rate": 140, "cadence": 0 if stopped else 90,
               "power": 0 if stopped else 200, "speed": 0.0 if stopped else SPEED_MS,
               "altitude": 100.0}
        if gps:
            rec["lat"], rec["lng"] = lat, 0.0
        records.append(rec)
        t += timedelta(seconds=1)
    return records


# ---------------------------------------------------------------- FIT builder

def test_fit_builder_roundtrips_through_fitdecode():
    raw = build_fit(_ride(5), total_timer_time=5, manufacturer="hammerhead",
                    local_start=datetime(2026, 5, 1, 7, 30))
    frames = {}
    with fitdecode.FitReader(io.BytesIO(raw), check_crc=fitdecode.CrcCheck.RAISE) as fr:
        for frame in fr:
            if frame.frame_type == fitdecode.FIT_FRAME_DATA:
                frames.setdefault(frame.name, []).append(frame)
    assert len(frames["record"]) == 5
    rec = frames["record"][1]
    assert rec.get_value("timestamp") == START + timedelta(seconds=1)
    assert rec.get_value("position_lat") == round(SPEED_MS / M_PER_DEG_LAT * 2 ** 31 / 180)
    assert rec.get_value("position_long") == 0
    assert rec.get_value("altitude") == pytest.approx(100.0)
    assert rec.get_value("heart_rate") == 140
    assert rec.get_value("cadence") == 90
    assert rec.get_value("speed") == pytest.approx(SPEED_MS, abs=1e-3)
    assert rec.get_value("power") == 200
    session = frames["session"][0]
    assert session.get_value("sport") == "cycling"
    assert session.get_value("sub_sport") == "road"
    assert session.get_value("total_timer_time") == pytest.approx(5)
    assert session.get_value("start_time") == START
    assert frames["file_id"][0].get_value("manufacturer") == "hammerhead"
    activity = frames["activity"][0]
    assert activity.get_value("local_timestamp").replace(tzinfo=None) == datetime(2026, 5, 1, 7, 30, 4)


# ---------------------------------------------------------------- FIT parsing

def test_fit_outdoor_ride():
    data, streams = parse_fit(build_fit(_ride(600), total_timer_time=600))
    assert data["duration_s"] == 600
    assert data["strava_type"] == "Ride"
    assert data["trainer"] is False
    assert abs(data["distance_m"] - 5000) < 100
    assert data["avg_power"] == 200
    assert data["avg_hr"] == 140
    assert data["avg_cadence"] == 90
    assert data["date"].startswith("2026-05-01T06:30:00")
    assert len(streams) == 600
    assert streams[0]["lat"] == pytest.approx(0.0, abs=1e-6)
    assert streams[0]["lng"] == pytest.approx(0.0, abs=1e-6)
    assert streams[10]["speed_kmh"] == pytest.approx(30, rel=0.02)
    assert streams[10]["altitude_m"] == pytest.approx(100.0)
    assert streams[10]["power"] == 200
    assert [s["time_offset"] for s in streams] == list(range(600))
    assert "file_sha256" not in data
    assert not {"strava_id", "rwgps_id", "intervals_icu_id"} & set(data)


def test_fit_indoor_is_trainer():
    data, streams = parse_fit(build_fit(_ride(120, gps=False), sub_sport="indoor_cycling"))
    assert data["trainer"] is True
    assert streams[0]["lat"] is None
    # No GPS: duration is elapsed time.
    assert data["duration_s"] == 119


def test_fit_virtual_is_virtualride():
    data, _ = parse_fit(build_fit(_ride(60), sub_sport="virtual_activity"))
    assert data["strava_type"] == "VirtualRide"


def test_fit_ebike_is_ebikeride():
    data, _ = parse_fit(build_fit(_ride(60), sport="e_biking", sub_sport="generic"))
    assert data["strava_type"] == "EBikeRide"


def test_fit_running_raises_not_cycling():
    with pytest.raises(NotCycling, match="running"):
        parse_fit(build_fit(_ride(60), sport="running", sub_sport="generic"))


def test_fit_corrupt_raises_parse_error():
    raw = bytearray(build_fit(_ride(60)))
    raw[200] ^= 0xFF
    with pytest.raises(ParseError):
        parse_fit(bytes(raw))


@pytest.mark.filterwarnings("ignore::UserWarning")  # fitdecode warns on each bad definition
def test_fit_garbage_with_valid_crc_raises_parse_error_or_parses():
    # A CRC proves nothing about the content: corrupt bytes with a recomputed
    # CRC once made fitdecode and the summary raise TypeError, ValueError and
    # AssertionError, which a caller would not file as a failed file.
    base = build_fit(_ride(30), local_start=datetime(2026, 5, 1, 7, 30))[:-2]
    rng = random.Random(1)
    for _ in range(400):
        raw = bytearray(base)
        for _ in range(rng.randint(1, 4)):
            raw[rng.randrange(14, len(raw))] = rng.randrange(256)
        raw += struct.pack("<H", fit_crc(bytes(raw)))
        try:
            parse_fit(bytes(raw))
        except (ParseError, NotCycling):
            pass


def test_fit_without_timer_uses_moving_time():
    # 300 records moving, 120 s stood still, 300 more moving: 720 records,
    # 719 s elapsed, 599 s moving.
    data, streams = parse_fit(build_fit(_ride(720, stop_at=299, stop_s=120)))
    assert streams[-1]["time_offset"] == 719
    assert abs(data["duration_s"] - 599) <= 2


def test_fit_name_is_ride_and_local_time():
    # Ten minutes long: the activity message is stamped at the end of the
    # ride, so reading its local_timestamp as the start would say 07:39.
    records = _ride(600)
    data, _ = parse_fit(build_fit(records, local_start=datetime(2026, 5, 1, 7, 30)))
    assert data["name"] == "Ride 2026-05-01 07:30"
    data, _ = parse_fit(build_fit(records))
    assert data["name"] == "Ride 2026-05-01 06:30"


def test_fit_device_from_manufacturer():
    assert parse_fit(build_fit(_ride(10), manufacturer="hammerhead"))[0]["device"] == "karoo"
    assert parse_fit(build_fit(_ride(10), manufacturer="zwift"))[0]["device"] == "zwift"
    assert parse_fit(build_fit(_ride(10), manufacturer="garmin"))[0]["device"] == "unknown"


# ---------------------------------------------------------------- GPX parsing

def _gpx(n=60, type_="", creator="Synthetic", timed=True, extensions=True, name="Morning Ride",
         ele=lambda i: 100.0):
    pts = []
    for i in range(n):
        t = (START + timedelta(seconds=i)).strftime("%Y-%m-%dT%H:%M:%SZ")
        ext = ("<extensions><power>200</power><gpxtpx:TrackPointExtension>"
               "<gpxtpx:hr>140</gpxtpx:hr><gpxtpx:cad>90</gpxtpx:cad>"
               "</gpxtpx:TrackPointExtension></extensions>") if extensions else ""
        pts.append(f'<trkpt lat="{i * SPEED_MS / M_PER_DEG_LAT:.7f}" lon="0.0">'
                   f'<ele>{ele(i):.1f}</ele>{f"<time>{t}</time>" if timed else ""}{ext}</trkpt>')
    return ('<?xml version="1.0" encoding="UTF-8"?>\n'
            f'<gpx version="1.1" creator="{creator}" '
            'xmlns="http://www.topografix.com/GPX/1/1" '
            'xmlns:gpxtpx="http://www.garmin.com/xmlschemas/TrackPointExtension/v1">'
            f'<trk><name>{name}</name>{type_}<trkseg>{"".join(pts)}</trkseg></trk></gpx>')


def test_gpx_with_hr_cadence_power_extensions():
    data, streams = parse_gpx(_gpx().encode())
    assert data["name"] == "Morning Ride"
    assert len(streams) == 60
    assert streams[5]["hr"] == 140
    assert streams[5]["cadence"] == 90
    assert streams[5]["power"] == 200
    assert streams[5]["altitude_m"] == pytest.approx(100.0)
    assert streams[5]["lat"] == pytest.approx(5 * SPEED_MS / M_PER_DEG_LAT, abs=1e-6)
    assert data["avg_power"] == 200
    assert data["avg_hr"] == 140
    assert abs(data["distance_m"] - 59 * SPEED_MS) < 5
    assert data["duration_s"] == 59
    assert data["date"].startswith("2026-05-01T06:30:00")


def test_gpx_ascent_ignores_noise_under_3m():
    # A steady 0.5 m/s climb with +/-1 m of noise on alternate points, from
    # 99 m to 130.5 m: 31.5 m of climbing. Summing every rise would give 75 m,
    # and restarting the count at every 1 m dip gives 52.5 m.
    data, _ = parse_gpx(_gpx(ele=lambda i: 100 + i * 0.5 + (1 if i % 2 else -1)).encode())
    assert data["elevation_m"] == pytest.approx(31.5)


def test_gpx_non_numeric_values_are_dropped():
    raw = _gpx().replace("<gpxtpx:hr>140</gpxtpx:hr>", "<gpxtpx:hr>NaN</gpxtpx:hr>", 1) \
                .replace("<power>200</power>", "<power>inf</power>", 1) \
                .replace("<ele>100.0</ele>", "<ele>high</ele>", 1)
    _, streams = parse_gpx(raw.encode())
    assert (streams[0]["hr"], streams[0]["power"], streams[0]["altitude_m"]) == (None, None, None)
    assert (streams[1]["hr"], streams[1]["power"]) == (140, 200)


def test_gpx_time_out_of_range_drops_the_point():
    bad = "<time>0001-01-01T00:00:00+14:00</time>"
    good = START.strftime("<time>%Y-%m-%dT%H:%M:%SZ</time>")
    raw = _gpx(n=3).replace(good, bad, 1)
    assert bad in raw
    data, streams = parse_gpx(raw.encode())
    assert len(streams) == 2
    assert data["date"].startswith("2026-05-01T06:30:01")
    # With only one usable time left, the file has no timestamps.
    with pytest.raises(ParseError, match="no timestamps"):
        parse_gpx(_gpx(n=2).replace(good, bad, 1).encode())


def test_gpx_out_of_range_coordinates_are_dropped():
    normal, _ = parse_gpx(_gpx().encode())
    lat10 = f"{10 * SPEED_MS / M_PER_DEG_LAT:.7f}"
    raw = _gpx().replace(f'<trkpt lat="{lat10}" lon="0.0">', '<trkpt lat="1e308" lon="0.0">', 1)
    raw = raw.replace('lon="0.0"><ele>100.0</ele><time>2026-05-01T06:30:20Z', 'lon="181"><ele>100.0</ele><time>2026-05-01T06:30:20Z', 1)
    assert 'lat="1e308"' in raw and 'lon="181"' in raw
    data, streams = parse_gpx(raw.encode())
    assert (streams[10]["lat"], streams[10]["lng"]) == (None, None)
    assert (streams[20]["lat"], streams[20]["lng"]) == (None, None)
    assert data["distance_m"] == pytest.approx(normal["distance_m"], abs=1)


def test_gpx_blank_name_falls_back_to_ride_and_time():
    data, _ = parse_gpx(_gpx(name="  ").encode())
    assert data["name"] == "Ride 2026-05-01 06:30"


def test_gpx_device_from_creator():
    assert parse_gpx(_gpx(creator="Hammerhead Karoo").encode())[0]["device"] == "karoo"
    assert parse_gpx(_gpx(creator="Apple Watch").encode())[0]["device"] == "watch"
    assert parse_gpx(_gpx(creator="Zwift").encode())[0]["device"] == "zwift"
    assert parse_gpx(_gpx().encode())[0]["device"] == "unknown"


def test_gpx_type_running_raises_not_cycling():
    with pytest.raises(NotCycling, match="running"):
        parse_gpx(_gpx(type_="<type>running</type>").encode())
    for type_ in ("<type>cycling</type>", "<type>road_biking</type>", ""):
        data, streams = parse_gpx(_gpx(type_=type_).encode())
        assert len(streams) == 60


def test_gpx_without_time_raises_parse_error():
    with pytest.raises(ParseError, match="no timestamps"):
        parse_gpx(_gpx(timed=False).encode())


_ENTITY_GPX = ('<?xml version="1.0" encoding="UTF-8"?>\n'
               '<!DOCTYPE gpx [<!ENTITY n "Entity Ride">]>\n'
               + _gpx(name="&n;").split("\n", 1)[1])

_LAUGHS_GPX = ('<?xml version="1.0"?>\n<!DOCTYPE lolz [<!ENTITY lol "lol">'
               + "".join(f'<!ENTITY lol{i} "{("&lol" + (str(i - 1) if i > 1 else "") + ";") * 10}">'
                         for i in range(1, 10))
               + ']>\n<gpx><trk><name>&lol9;</name></trk></gpx>')


def test_gpx_with_entity_declaration_fails():
    # Without the guard this file parses and the name reads "Entity Ride".
    with pytest.raises(ParseError, match="DTD not allowed"):
        parse_gpx(_ENTITY_GPX.encode())
    t0 = time.monotonic()
    with pytest.raises(ParseError, match="DTD not allowed"):
        parse_gpx(_LAUGHS_GPX.encode())
    assert time.monotonic() - t0 < 0.2


def test_gpx_not_utf8_fails():
    # The DTD guard must not be bypassable by encoding: as UTF-16 the bytes
    # never contain b"<!ENTITY", and expat would happily decode and expand it.
    entity_utf16 = _ENTITY_GPX.replace('encoding="UTF-8"', 'encoding="UTF-16"').encode("utf-16")
    assert entity_utf16.startswith((b"\xff\xfe", b"\xfe\xff"))
    with pytest.raises(ParseError, match="UTF-8"):
        parse_gpx(entity_utf16)
    plain_utf16 = _gpx().replace('encoding="UTF-8"', 'encoding="UTF-16"').encode("utf-16")
    with pytest.raises(ParseError, match="UTF-8"):
        parse_gpx(plain_utf16)


def test_gpx_parser_reads_the_decoded_text():
    # A UTF-8 file whose declaration claims Latin-1: the parser must read the
    # text the guard checked, not re-decode the raw bytes its own way.
    raw = _gpx(name="Café").replace('encoding="UTF-8"', 'encoding="ISO-8859-1"').encode("utf-8")
    data, _ = parse_gpx(raw)
    assert data["name"] == "Café"


def test_gpx_utf8_bom_is_stripped():
    data, streams = parse_gpx(b"\xef\xbb\xbf" + _gpx().encode())
    assert data["name"] == "Morning Ride"
    assert len(streams) == 60


def test_gpx_malformed_xml_raises_parse_error():
    with pytest.raises(ParseError):
        parse_gpx(b"<gpx><trk>")


# ---------------------------------------------------------------- unpacking

def _zip(path, members):
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, content in members:
            zf.writestr(name, content)
    return path


def _zip_bytes(members):
    buf = io.BytesIO()
    _zip(buf, members)
    return buf.getvalue()


def _members(path):
    return list(iter_members(str(path), path.name))


def test_iter_members_plain_gz_zip_and_nested(tmp_path):
    (tmp_path / "a.fit").write_bytes(b"fit-a")
    (tmp_path / "b.gpx.gz").write_bytes(gzip.compress(b"gpx-b"))
    inner = _zip_bytes([("z.fit.gz", gzip.compress(b"fit-z"))])
    _zip(tmp_path / "c.zip", [("x.fit", b"fit-x"), ("y.txt", b"text"), ("inner.zip", inner)])
    (tmp_path / "d.txt").write_bytes(b"ignored")

    got = []
    for name in ("a.fit", "b.gpx.gz", "c.zip", "d.txt"):
        got += _members(tmp_path / name)
    assert got == [
        ("a.fit", b"fit-a"),
        ("b.gpx.gz", b"gpx-b"),
        ("c.zip/x.fit", b"fit-x"),
        ("c.zip/inner.zip/z.fit.gz", b"fit-z"),
    ]


def test_member_over_cap_fails_and_next_member_imports(tmp_path, monkeypatch):
    monkeypatch.setattr(file_import, "MEMBER_MAX_BYTES", 1000)
    _zip(tmp_path / "c.zip", [("big.fit", b"x" * 5000), ("ok.fit", b"y" * 100)])
    got = _members(tmp_path / "c.zip")
    assert [n for n, _ in got] == ["c.zip/big.fit", "c.zip/ok.fit"]
    assert isinstance(got[0][1], ParseError)
    assert got[1][1] == b"y" * 100

    # The same cap holds for a gz on disk, plain or inside a zip.
    (tmp_path / "big.fit.gz").write_bytes(gzip.compress(b"x" * 5000))
    [(_, err)] = _members(tmp_path / "big.fit.gz")
    assert isinstance(err, ParseError)
    _zip(tmp_path / "g.zip", [("big.fit.gz", gzip.compress(b"x" * 5000))])
    [(_, err)] = _members(tmp_path / "g.zip")
    assert isinstance(err, ParseError)
    (tmp_path / "big.fit").write_bytes(b"x" * 5000)
    [(_, err)] = _members(tmp_path / "big.fit")
    assert isinstance(err, ParseError)
    # Exactly the cap still reads.
    (tmp_path / "edge.fit").write_bytes(b"x" * 1000)
    assert _members(tmp_path / "edge.fit") == [("edge.fit", b"x" * 1000)]


def _set_eocd_count(raw: bytes, count: int) -> bytes:
    """Rewrite the entry counts in a zip's end-of-central-directory record."""
    raw = bytearray(raw)
    at = raw.rfind(b"PK\x05\x06")
    struct.pack_into("<HH", raw, at + 8, count, count)
    return bytes(raw)


def _as_zip64(raw: bytes, count: int) -> bytes:
    """Give a small zip a zip64 end record claiming ``count`` entries.

    The classic record's counts become 0xFFFF, which tells a reader to follow
    the zip64 locator, as a real archive of more than 65535 entries does.
    """
    at = raw.rfind(b"PK\x05\x06")
    _, _, _, _, _, cd_size, cd_offset, _ = struct.unpack_from("<4sHHHHIIH", raw, at)
    record = struct.pack("<4sQHHIIQQQQ", b"PK\x06\x06", 44, 45, 45, 0, 0,
                         count, count, cd_size, cd_offset)
    locator = struct.pack("<4sIQI", b"PK\x06\x07", 0, at, 1)
    eocd = struct.pack("<4sHHHHIIH", b"PK\x05\x06", 0, 0, 0xFFFF, 0xFFFF,
                       cd_size, cd_offset, 0)
    return raw[:at] + record + locator + eocd


@pytest.fixture
def zipfile_opens(monkeypatch):
    """Record every zipfile.ZipFile opened for reading (the tests' own writes aren't)."""
    opened, real = [], zipfile.ZipFile

    def spy(source, *args, **kwargs):
        if (args[0] if args else kwargs.get("mode", "r")) == "r":
            opened.append(source)
        return real(source, *args, **kwargs)
    monkeypatch.setattr(file_import.zipfile, "ZipFile", spy)
    return opened


def test_zip_over_member_count_is_refused_unopened(tmp_path, monkeypatch, zipfile_opens):
    monkeypatch.setattr(file_import, "MAX_MEMBERS", 2)
    (tmp_path / "c.zip").write_bytes(_zip_bytes([("1.fit", b"1"), ("2.fit", b"2"), ("3.fit", b"3")]))
    [(name, err)] = _members(tmp_path / "c.zip")
    assert name == "c.zip"
    assert isinstance(err, ParseError)
    assert str(err) == "archive has 3 entries; limit 2"
    assert zipfile_opens == []


def test_zip_with_forged_member_count_is_refused_unopened(tmp_path, zipfile_opens):
    forged = _set_eocd_count(_zip_bytes([("1.fit", b"1")]), file_import.MAX_MEMBERS + 1)
    (tmp_path / "c.zip").write_bytes(forged)
    [(name, err)] = _members(tmp_path / "c.zip")
    assert (name, str(err)) == ("c.zip", "archive has 20001 entries; limit 20000")
    assert zipfile_opens == []


def test_zip64_member_count_is_read(tmp_path, zipfile_opens):
    small = _zip_bytes([("1.fit", b"1"), ("2.fit", b"2")])
    (tmp_path / "ok.zip").write_bytes(_as_zip64(small, 2))
    assert _members(tmp_path / "ok.zip") == [("ok.zip/1.fit", b"1"), ("ok.zip/2.fit", b"2")]
    (tmp_path / "big.zip").write_bytes(_as_zip64(small, 70000))
    [(name, err)] = _members(tmp_path / "big.zip")
    assert (name, str(err)) == ("big.zip", "archive has 70000 entries; limit 20000")
    assert len(zipfile_opens) == 1  # only ok.zip


def test_inner_zip_over_member_count_is_refused_unopened(tmp_path, monkeypatch, zipfile_opens):
    monkeypatch.setattr(file_import, "MAX_MEMBERS", 2)
    inner = _zip_bytes([("1.fit", b"1"), ("2.fit", b"2"), ("3.fit", b"3")])
    (tmp_path / "c.zip").write_bytes(_zip_bytes([("inner.zip", inner), ("ok.fit", b"ok")]))
    got = _members(tmp_path / "c.zip")
    assert [n for n, _ in got] == ["c.zip/inner.zip", "c.zip/ok.fit"]
    assert str(got[0][1]) == "archive has 3 entries; limit 2"
    assert got[1][1] == b"ok"
    assert len(zipfile_opens) == 1  # the outer zip only


def test_zip64_record_away_from_the_locator_is_refused_unopened(tmp_path, zipfile_opens):
    # zipfile reads the zip64 end record at the locator's offset first. A
    # decoy right before the locator claiming one entry must not stand in for
    # the record zipfile would actually use.
    raw = _zip_bytes([("1.fit", b"1"), ("2.fit", b"2"), ("3.fit", b"3")])
    at = raw.rfind(b"PK\x05\x06")
    _, _, _, _, _, cd_size, cd_offset, _ = struct.unpack_from("<4sHHHHIIH", raw, at)
    real = struct.pack("<4sQHHIIQQQQ", b"PK\x06\x06", 44 + 56, 45, 45, 0, 0,
                       3, 3, cd_size, cd_offset)
    decoy = struct.pack("<4sQHHIIQQQQ", b"PK\x06\x06", 44, 45, 45, 0, 0,
                        1, 1, 46, cd_offset)
    locator = struct.pack("<4sIQI", b"PK\x06\x07", 0, at, 1)
    eocd = struct.pack("<4sHHHHIIH", b"PK\x05\x06", 0, 0, 0xFFFF, 0xFFFF,
                       0xFFFFFFFF, 0xFFFFFFFF, 0)
    (tmp_path / "c.zip").write_bytes(raw[:at] + real + decoy + locator + eocd)
    assert len(zipfile.ZipFile(tmp_path / "c.zip").infolist()) == 3  # zipfile reads `real`
    zipfile_opens.clear()
    [(name, err)] = _members(tmp_path / "c.zip")
    assert name == "c.zip"
    assert isinstance(err, ParseError)
    assert "zip64" in str(err)
    assert zipfile_opens == []


def test_zip_understating_its_count_is_bounded_by_directory_size(tmp_path, monkeypatch, zipfile_opens):
    # zipfile reads the directory by its size and ignores the count, so an end
    # record claiming one entry passes the count check: the size cap holds.
    raw = _zip_bytes([(f"{i}.fit", b"") for i in range(10)])
    (tmp_path / "c.zip").write_bytes(_set_eocd_count(raw, 1))
    monkeypatch.setattr(file_import, "CENTRAL_DIR_MAX_BYTES", 200)
    [(name, err)] = _members(tmp_path / "c.zip")
    assert name == "c.zip"
    assert str(err).startswith("archive directory is ")
    assert zipfile_opens == []
    monkeypatch.setattr(file_import, "CENTRAL_DIR_MAX_BYTES", 10_000)
    assert len(_members(tmp_path / "c.zip")) == 10


def test_central_directory_cap_is_4_mib():
    assert file_import.CENTRAL_DIR_MAX_BYTES == 4 * 1024 * 1024


def test_zip_central_directory_size_is_capped(tmp_path, monkeypatch, zipfile_opens):
    monkeypatch.setattr(file_import, "CENTRAL_DIR_MAX_BYTES", 100)
    (tmp_path / "c.zip").write_bytes(_zip_bytes([("x" * 200 + ".fit", b"1")]))
    [(name, err)] = _members(tmp_path / "c.zip")
    assert name == "c.zip"
    assert isinstance(err, ParseError)
    assert zipfile_opens == []


def test_member_count_is_capped_after_opening_too(tmp_path, monkeypatch):
    # An end record that understates the count passes the first check; the
    # entry list is still cut at MAX_MEMBERS.
    monkeypatch.setattr(file_import, "MAX_MEMBERS", 2)
    raw = _zip_bytes([("1.fit", b"1"), ("2.fit", b"2"), ("3.fit", b"3")])
    (tmp_path / "c.zip").write_bytes(_set_eocd_count(raw, 2))
    assert [n for n, _ in _members(tmp_path / "c.zip")] == ["c.zip/1.fit", "c.zip/2.fit"]


def _flag_utf8_and_break_name(raw: bytes, name: bytes, local=True, central=True) -> bytes:
    """Set the UTF-8 name flag on ``name``'s headers and make its bytes invalid UTF-8."""
    raw = bytearray(raw)
    for sig, flag_at, wanted in ((b"PK\x03\x04", 6, local), (b"PK\x01\x02", 8, central)):
        if not wanted:
            continue
        at = raw.find(sig)
        while at != -1:
            name_at = at + (30 if sig == b"PK\x03\x04" else 46)
            if raw[name_at:name_at + len(name)] == name:
                flags = struct.unpack_from("<H", raw, at + flag_at)[0] | 0x800
                struct.pack_into("<H", raw, at + flag_at, flags)
                raw[name_at:name_at + 4] = b"\xff\xfe\xfd\xfc"
            at = raw.find(sig, at + 1)
    return bytes(raw)


def test_zip_member_with_bad_utf8_name_fails_and_next_member_imports(tmp_path):
    raw = _zip_bytes([("BBBB.fit", b"x"), ("ok.fit", b"ok")])
    (tmp_path / "c.zip").write_bytes(_flag_utf8_and_break_name(raw, b"BBBB", central=False))
    got = _members(tmp_path / "c.zip")
    assert [n for n, _ in got] == ["c.zip/BBBB.fit", "c.zip/ok.fit"]
    assert isinstance(got[0][1], ParseError)
    assert got[1][1] == b"ok"

    # Broken in the central directory too: the entry list can't be read, so
    # the archive fails as a whole rather than raising.
    (tmp_path / "d.zip").write_bytes(_flag_utf8_and_break_name(raw, b"BBBB"))
    [(name, err)] = _members(tmp_path / "d.zip")
    assert (name, type(err)) == ("d.zip", ParseError)

    # The same inside an inner zip.
    (tmp_path / "e.zip").write_bytes(_zip_bytes([
        ("inner.zip", _flag_utf8_and_break_name(raw, b"BBBB", central=False))]))
    got = _members(tmp_path / "e.zip")
    assert [n for n, _ in got] == ["e.zip/inner.zip/BBBB.fit", "e.zip/inner.zip/ok.fit"]
    assert isinstance(got[0][1], ParseError)
    assert got[1][1] == b"ok"

    # And on the inner zip's own entry, which fails while it is being copied.
    outer = _zip_bytes([("CCCC.zip", _zip_bytes([("z.fit", b"z")])), ("ok.fit", b"ok")])
    (tmp_path / "f.zip").write_bytes(_flag_utf8_and_break_name(outer, b"CCCC", central=False))
    got = _members(tmp_path / "f.zip")
    assert [n for n, _ in got] == ["f.zip/CCCC.zip", "f.zip/ok.fit"]
    assert isinstance(got[0][1], ParseError)
    assert got[1][1] == b"ok"


def test_inner_zip_over_cap_fails(tmp_path, monkeypatch):
    monkeypatch.setattr(file_import, "INNER_ZIP_MAX_BYTES", 100)
    inner = _zip_bytes([(f"{i}.fit", bytes(range(256)) * 4) for i in range(5)])
    assert len(inner) > 100
    _zip(tmp_path / "c.zip", [("inner.zip", inner), ("ok.fit", b"ok")])
    got = _members(tmp_path / "c.zip")
    assert [n for n, _ in got] == ["c.zip/inner.zip", "c.zip/ok.fit"]
    assert isinstance(got[0][1], ParseError)


def test_bad_archives_yield_parse_errors(tmp_path):
    (tmp_path / "bad.zip").write_bytes(b"not a zip")
    (tmp_path / "bad.fit.gz").write_bytes(b"not gzip")
    _zip(tmp_path / "c.zip", [("inner.zip", b"not a zip either"), ("ok.fit", b"ok")])
    [(n, err)] = _members(tmp_path / "bad.zip")
    assert (n, type(err)) == ("bad.zip", ParseError)
    [(n, err)] = _members(tmp_path / "bad.fit.gz")
    assert (n, type(err)) == ("bad.fit.gz", ParseError)
    got = _members(tmp_path / "c.zip")
    assert isinstance(got[0][1], ParseError)
    assert got[1] == ("c.zip/ok.fit", b"ok")


def test_zip_member_names_with_dotdot_are_harmless(tmp_path, monkeypatch):
    # Run from two levels down, so a write by name relative to the working
    # directory would land inside tmp_path, and one relative to tmp_path in
    # its parent.
    work = tmp_path / "a" / "b"
    work.mkdir(parents=True)
    monkeypatch.chdir(work)
    _zip(tmp_path / "c.zip", [("../../evil.fit", b"evil")])
    before = sorted(p.name for p in tmp_path.parent.iterdir())
    assert _members(tmp_path / "c.zip") == [("c.zip/../../evil.fit", b"evil")]
    assert sorted(p.name for p in tmp_path.parent.iterdir()) == before
    assert not list(tmp_path.rglob("evil.fit"))
    assert not list(tmp_path.parent.glob("evil.fit"))


def test_deeper_nesting_is_ignored(tmp_path):
    deep = _zip_bytes([("z.fit", b"fit-z")])
    mid = _zip_bytes([("a.fit", b"fit-a"), ("deep.zip", deep)])
    _zip(tmp_path / "outer.zip", [("mid.zip", mid)])
    assert _members(tmp_path / "outer.zip") == [("outer.zip/mid.zip/a.fit", b"fit-a")]


def test_inner_zip_leaves_no_named_temp_file(tmp_path, monkeypatch):
    # An unnamed temp file is gone the moment the process dies; a named one
    # outlives a SIGKILL or the OOM killer in /tmp.
    def named(*args, **kwargs):
        raise AssertionError("inner zip copied to a named temp file")

    monkeypatch.setattr(file_import.tempfile, "NamedTemporaryFile", named)
    inner = _zip_bytes([("z.fit", b"fit-z")])
    _zip(tmp_path / "c.zip", [("inner.zip", inner)])
    assert _members(tmp_path / "c.zip") == [("c.zip/inner.zip/z.fit", b"fit-z")]


# ---------------------------------------------------------------- plausibility

def _gpx_samples(samples, times=None):
    """A GPX with one point per ``(hr, cad, power)`` text triple, 1 s apart,
    or at the given ``times`` (aware datetimes)."""
    pts = []
    for i, (hr, cad, power) in enumerate(samples):
        t = times[i] if times else START + timedelta(seconds=i)
        pts.append(f'<trkpt lat="{i * SPEED_MS / M_PER_DEG_LAT:.7f}" lon="0.0">'
                   f'<ele>100.0</ele><time>{t:%Y-%m-%dT%H:%M:%SZ}</time>'
                   f'<extensions><power>{power}</power><gpxtpx:TrackPointExtension>'
                   f'<gpxtpx:hr>{hr}</gpxtpx:hr><gpxtpx:cad>{cad}</gpxtpx:cad>'
                   '</gpxtpx:TrackPointExtension></extensions></trkpt>')
    return ('<?xml version="1.0" encoding="UTF-8"?>\n'
            '<gpx version="1.1" creator="Synthetic" '
            'xmlns="http://www.topografix.com/GPX/1/1" '
            'xmlns:gpxtpx="http://www.garmin.com/xmlschemas/TrackPointExtension/v1">'
            f'<trk><name>Ride</name><trkseg>{"".join(pts)}</trkseg></trk></gpx>').encode()


def test_gpx_implausible_samples_become_none_and_the_ride_stays():
    data, streams = parse_gpx(_gpx_samples([
        ("99999999999", "90", "200"),   # an hr no INTEGER column holds
        ("19", "251", "3001"),          # each just outside its range
        ("261", "-1", "-1"),
        ("20", "0", "0"),               # each range's edges are kept
        ("260", "250", "3000"),
        ("140", "90", "200"),
    ]))
    assert [(s["hr"], s["cadence"], s["power"]) for s in streams] == [
        (None, 90, 200),
        (None, None, None),
        (None, None, None),
        (20, 0, 0),
        (260, 250, 3000),
        (140, 90, 200),
    ]
    assert data["max_hr"] == 260
    assert data["max_power"] == 3000


def test_fit_implausible_samples_become_none():
    records = _ride(4)
    records[0].update(heart_rate=19, cadence=251, power=3001)
    records[1].update(heart_rate=20, cadence=250, power=3000)
    _, streams = parse_fit(build_fit(records, total_timer_time=4))
    assert [(s["hr"], s["cadence"], s["power"]) for s in streams[:3]] == [
        (None, None, None), (20, 250, 3000), (140, 90, 200)]


def test_gpx_spanning_more_than_seven_days_fails():
    week = [START, START + timedelta(days=7)]
    data, streams = parse_gpx(_gpx_samples([("140", "90", "200")] * 2, times=week))
    assert len(streams) == 2
    with pytest.raises(ParseError, match="7 days"):
        parse_gpx(_gpx_samples([("140", "90", "200")] * 2,
                               times=[START, START + timedelta(days=8)]))


def test_gpx_point_dated_1900_fails_the_file():
    # 126 years of time_offset overflows the stream's INTEGER column; the
    # span rule fails the file before it gets there.
    times = [datetime(1900, 1, 1, tzinfo=timezone.utc)] + [
        START + timedelta(seconds=i) for i in range(1, 5)]
    with pytest.raises(ParseError, match="7 days"):
        parse_gpx(_gpx_samples([("140", "90", "200")] * 5, times=times))


def test_fit_spanning_more_than_seven_days_fails():
    records = _ride(10)
    records[-1]["time"] = START + timedelta(days=8)
    with pytest.raises(ParseError, match="7 days"):
        parse_fit(build_fit(records, session_start=START))


def test_fit_records_stamped_before_gps_lock_are_dropped():
    # Two records carry a clock the device hadn't set yet. 1990 itself is below
    # FIT's date floor (fitdecode leaves it a number, already dropped), so the
    # bogus clock here is the first date fitdecode does read as one.
    bogus = datetime(2000, 1, 1, tzinfo=timezone.utc)
    early = [{"time": bogus + timedelta(seconds=i), "heart_rate": 140} for i in range(2)]
    data, streams = parse_fit(build_fit(early + _ride(600), session_start=START,
                                        total_timer_time=600))
    assert data["date"].startswith("2026-05-01T06:30:00")
    assert data["name"] == "Ride 2026-05-01 06:30"
    assert len(streams) == 600
    assert [s["time_offset"] for s in streams] == list(range(600))


def test_fit_records_after_the_session_end_are_dropped():
    late = {"time": START + timedelta(seconds=599, hours=1, minutes=1), "heart_rate": 140}
    _, streams = parse_fit(build_fit(_ride(600) + [late], session_start=START,
                                     total_elapsed_time=600))
    assert len(streams) == 600
    # Without total_elapsed_time the session's end is its last record.
    _, streams = parse_fit(build_fit(_ride(600) + [late], session_start=START))
    assert len(streams) == 601


@pytest.mark.parametrize("type_", ["9", "10", " 9 ", "0"])
def test_gpx_numeric_type_other_than_1_is_not_cycling(type_):
    with pytest.raises(NotCycling):
        parse_gpx(_gpx(type_=f"<type>{type_}</type>").encode())


def test_gpx_numeric_type_1_is_cycling():
    _, streams = parse_gpx(_gpx(type_="<type>1</type>").encode())
    assert len(streams) == 60
