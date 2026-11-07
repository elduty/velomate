"""FIT and GPX file import: unpacking archives and parsing rides.

``iter_members`` finds every .fit/.gpx (plain or .gz) reachable from one file on
disk, through one zip and one inner zip level, under per-member size caps.
``parse_fit`` and ``parse_gpx`` turn one decompressed file into the
``(data, streams)`` shapes ``rwgps._parse_trip`` and
``rwgps._parse_track_points`` produce, so the rest of ingestion is shared.
``import_file`` stores what one file holds, and ``scan_folder`` runs it over
the read-only watched folder on every poll.

See docs/design/specs/2026-11-07-file-import-design.md.
"""

from __future__ import annotations

import gzip
import hashlib
import io
import math
import os
import stat
import struct
import tempfile
import time
import xml.etree.ElementTree as ET
import zipfile
import zlib
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable, Iterator

import fitdecode
import psycopg2

import db
from route_match import haversine_m
from rwgps import _parse_track_points
from segments import MOVING_SPEED_KMH

MEMBER_MAX_BYTES = 64 * 1024 * 1024    # one decompressed .fit/.gpx
INNER_ZIP_MAX_BYTES = 2 * 1024 ** 3    # one inner zip, copied to a temp file
MAX_MEMBERS = 20000                    # entries read from one zip
# A zip's central directory is loaded whole when it opens. This cap is what
# bounds that memory: zipfile reads the directory by its size and ignores the
# end record's count. 20000 Garmin-style entries take about 2.9 MB.
CENTRAL_DIR_MAX_BYTES = 4 * 1024 * 1024
CHUNK = 1024 * 1024

EOCD, EOCD_SIZE = b"PK\x05\x06", 22
ZIP64_LOCATOR, ZIP64_LOCATOR_SIZE = b"PK\x06\x07", 20
ZIP64_EOCD, ZIP64_EOCD_SIZE = b"PK\x06\x06", 56

FILE_SUFFIXES = (".fit", ".gpx", ".fit.gz", ".gpx.gz")
ASCENT_HYSTERESIS_M = 3.0
SEMICIRCLE_DEG = 180 / 2 ** 31

CYCLING_SPORTS = ("cycling", "e_biking")
TRAINER_SUB_SPORTS = ("indoor_cycling", "spin")
NON_CYCLING_GPX_TYPES = ("run", "walk", "hik", "swim", "ski", "row", "paddl",
                         "kayak", "yoga", "strength", "elliptical")
# A GPX <type> that is a number is an activity code (Strava writes them). Only
# 1 is taken as a ride: a run imported as a ride would corrupt fitness, and a
# ride skipped this way can still be imported from its FIT file.
CYCLING_GPX_TYPE_CODE = "1"

# Plausible ranges for one sample, inclusive. A value outside one is a glitch
# or a corrupt file and is dropped from that sample; the ride is kept.
HR_RANGE = (20, 260)
CADENCE_RANGE = (0, 250)
POWER_RANGE = (0, 3000)
# A file whose timed points span longer than this is not one ride. It also
# keeps every time_offset inside the stream table's INTEGER column.
MAX_SPAN_S = 7 * 24 * 3600
# A FIT record this far outside its session carries a clock the device had not
# set yet (before GPS lock), not a time in the ride.
SESSION_SLACK = timedelta(hours=1)


class ParseError(Exception):
    """The file can't be read as a ride; the message is the ledger detail."""


class NotCycling(Exception):
    """The file is a ride of another sport; the message names it."""


# ---------------------------------------------------------------- unpacking

def _read_capped(stream, cap: int, what: str) -> bytes:
    """Read ``stream`` to the end, refusing it once it passes ``cap`` bytes."""
    buf = bytearray()
    while True:
        chunk = stream.read(min(CHUNK, cap + 1 - len(buf)))
        if not chunk:
            return bytes(buf)
        buf += chunk
        if len(buf) > cap:
            raise ParseError(f"{what} is larger than {cap} bytes")


def _wanted(name: str) -> bool:
    return name.lower().endswith(FILE_SUFFIXES)


def _read_member(stream, name: str) -> bytes:
    """Read one .fit/.gpx, decompressing a .gz, under MEMBER_MAX_BYTES."""
    if not name.lower().endswith(".gz"):
        return _read_capped(stream, MEMBER_MAX_BYTES, "file")
    try:
        with gzip.GzipFile(fileobj=stream, mode="rb") as gz:
            return _read_capped(gz, MEMBER_MAX_BYTES, "decompressed file")
    except (gzip.BadGzipFile, EOFError, zlib.error) as e:
        raise ParseError(f"can't decompress: {e}") from e


def _check_zip_directory(f) -> None:
    """Refuse a zip whose end record claims too many entries or too big a directory.

    zipfile loads the whole central directory when it opens an archive, before
    any slice of ``infolist()`` can apply, so the limits are checked here
    first, from the same end record zipfile will use: the classic one at the
    end of the file (found the way zipfile finds it), and the zip64 one when a
    zip64 locator sits right before it. The directory size is the real bound;
    the count only refuses an honest archive early.
    """
    f.seek(0, io.SEEK_END)
    size = f.tell()
    tail_start = max(0, size - EOCD_SIZE - 0xFFFF)
    f.seek(tail_start)
    tail = f.read()
    at = len(tail) - EOCD_SIZE
    if at < 0 or tail[at:at + 4] != EOCD or tail[-2:] != b"\x00\x00":
        at = tail.rfind(EOCD)
    if at < 0 or at + EOCD_SIZE > len(tail):
        raise ParseError("not a readable zip: no end of central directory")
    count, cd_size = struct.unpack_from("<HI", tail, at + 10)
    eocd_pos = tail_start + at
    if eocd_pos >= ZIP64_LOCATOR_SIZE + ZIP64_EOCD_SIZE:
        locator_pos = eocd_pos - ZIP64_LOCATOR_SIZE
        f.seek(locator_pos)
        locator = f.read(ZIP64_LOCATOR_SIZE)
        if locator[:4] == ZIP64_LOCATOR:
            # zipfile reads the record at the locator's offset first and only
            # then right before the locator; real exports put it in both
            # places at once (no zip64 extensible data), so anything else is
            # refused rather than guessing which one zipfile will use.
            record_pos = locator_pos - ZIP64_EOCD_SIZE
            if struct.unpack_from("<Q", locator, 8)[0] != record_pos:
                raise ParseError("not a readable zip: zip64 end record isn't where its locator points")
            f.seek(record_pos)
            record = f.read(ZIP64_EOCD_SIZE)
            if record[:4] != ZIP64_EOCD:
                raise ParseError("not a readable zip: bad zip64 end record")
            count, cd_size = struct.unpack_from("<QQ", record, 32)
    if count > MAX_MEMBERS:
        raise ParseError(f"archive has {count} entries; limit {MAX_MEMBERS}")
    if cd_size > CENTRAL_DIR_MAX_BYTES:
        raise ParseError(f"archive directory is {cd_size} bytes; limit {CENTRAL_DIR_MAX_BYTES}")
    f.seek(0)


def _iter_zip(f, display: str, nested: bool) -> Iterator[tuple[str, bytes | Exception]]:
    """Members of one zip, open as the binary file ``f``.

    Members are read by entry, in memory, and never written anywhere by name.
    """
    try:
        _check_zip_directory(f)
        zf = zipfile.ZipFile(f)
    except ParseError as e:
        yield display, e
        return
    except Exception as e:  # zipfile raises many kinds on a crafted directory
        yield display, ParseError(f"not a readable zip: {type(e).__name__}: {e}")
        return
    with zf:
        for info in zf.infolist()[:MAX_MEMBERS]:
            if info.is_dir():
                continue
            member = f"{display}/{info.filename}"
            lower = info.filename.lower()
            if lower.endswith(".zip"):
                if nested:
                    yield from _iter_inner_zip(zf, info, member)
                continue
            if _wanted(lower):
                yield member, _read_zip_member(zf, info, lower)


def _read_zip_member(zf, info, lower: str) -> bytes | ParseError:
    try:
        with zf.open(info) as stream:
            return _read_member(stream, lower)
    except ParseError as e:
        return e
    except Exception as e:
        # A bad CRC, an unsupported method, an encrypted member, a header
        # name that isn't the UTF-8 its flag claims: zipfile raises a different
        # kind for each, and none of them may stop the next member.
        return ParseError(f"can't read zip member: {type(e).__name__}: {e}")


def _copy_inner_zip(zf, info, tmp) -> ParseError | None:
    try:
        with zf.open(info) as stream:
            copied = 0
            while chunk := stream.read(CHUNK):
                copied += len(chunk)
                if copied > INNER_ZIP_MAX_BYTES:
                    return ParseError(f"inner zip is larger than {INNER_ZIP_MAX_BYTES} bytes")
                tmp.write(chunk)
    except Exception as e:
        return ParseError(f"can't read zip member: {type(e).__name__}: {e}")
    tmp.flush()
    return None


def _iter_inner_zip(zf, info, display: str) -> Iterator[tuple[str, bytes | Exception]]:
    """An inner zip, copied to a temp file under its cap, then read unnested."""
    # Unnamed, so a process killed mid-copy leaves nothing behind in /tmp.
    tmp = tempfile.TemporaryFile()
    try:
        error = _copy_inner_zip(zf, info, tmp)
        if error is not None:
            yield display, error
            return
        yield from _iter_zip(tmp, display, nested=False)
    finally:
        tmp.close()


def iter_members(path: str, name: str) -> Iterator[tuple[str, bytes | Exception]]:
    """Yield ``(display_name, bytes)`` for each ride file reachable from ``path``.

    ``name`` is the display name of the file itself; members join it with
    ``/`` (``export.zip/inner.zip/123.fit``). A member refused by a cap, or one
    that can't be read, yields a ``ParseError`` in place of its bytes, and the
    next member is still read. An archive refused as a whole yields one
    ``ParseError`` under its own name. Files of any other kind yield nothing.
    A path that can't be opened raises ``OSError``, whatever its type.
    """
    lower = name.lower()
    if lower.endswith(".zip"):
        with open(path, "rb") as f:
            yield from _iter_zip(f, name, nested=True)
    elif _wanted(lower):
        with open(path, "rb") as f:
            try:
                data = _read_member(f, lower)
            except ParseError as e:
                data = e
        yield name, data


# ---------------------------------------------------------------- summaries

def _moving_time(streams: list) -> int:
    """Seconds spent moving at MOVING_SPEED_KMH or more, over GPS steps."""
    moving = 0
    prev = None
    for pt in streams:
        if pt["lat"] is None or pt["lng"] is None:
            continue
        if prev is not None:
            dt = pt["time_offset"] - prev["time_offset"]
            d = haversine_m(prev["lat"], prev["lng"], pt["lat"], pt["lng"])
            if dt > 0 and d / dt * 3.6 >= MOVING_SPEED_KMH:
                moving += dt
        prev = pt
    return moving


def _gps_distance(streams: list) -> float:
    fixes = [(p["lat"], p["lng"]) for p in streams
             if p["lat"] is not None and p["lng"] is not None]
    return sum(haversine_m(*a, *b) for a, b in zip(fixes, fixes[1:]))


def _ascent(streams: list) -> float:
    """Sum of rises after a 3 m hysteresis.

    A climb starts once the altitude clears the lowest point since the last
    climb by 3 m, and ends once it drops 3 m below the highest point of the
    climb, so wobbles smaller than 3 m add nothing either way.
    """
    gain, ref, climbing = 0.0, None, False
    for p in streams:
        alt = p["altitude_m"]
        if alt is None:
            continue
        if ref is None:
            ref = alt
        elif climbing:
            if alt > ref:
                gain += alt - ref
                ref = alt
            elif ref - alt >= ASCENT_HYSTERESIS_M:
                climbing, ref = False, alt
        elif alt < ref:
            ref = alt
        elif alt - ref >= ASCENT_HYSTERESIS_M:
            climbing = True
            gain += alt - ref
            ref = alt
    return gain


def _mean(streams: list, key: str):
    vals = [p[key] for p in streams if p[key] is not None]
    return sum(vals) / len(vals) if vals else None


def _max(streams: list, key: str):
    vals = [p[key] for p in streams if p[key] is not None]
    return max(vals) if vals else None


def _device(text: str) -> str:
    text = (text or "").lower()
    if "hammerhead" in text or "karoo" in text:
        return "karoo"
    if "apple" in text or "watch" in text:
        return "watch"
    if "zwift" in text:
        return "zwift"
    return "unknown"


def _summary(streams: list, start: datetime, local_start: datetime, *,
             name=None, session=None, device="unknown",
             strava_type="Ride", trainer=False) -> dict:
    """The rwgps._parse_trip-shaped dict: session values, else the stream's."""
    s = session or {}

    def pick(key, fallback):
        value = s.get(key)
        return fallback() if value is None else value

    has_gps = any(p["lat"] is not None and p["lng"] is not None for p in streams)
    distance_m = pick("total_distance", lambda: _gps_distance(streams))
    duration_s = round(pick("total_timer_time", lambda: (
        _moving_time(streams) if has_gps else streams[-1]["time_offset"])))
    blank = not (name or "").strip()
    return {
        "name": f"Ride {local_start:%Y-%m-%d %H:%M}" if blank else name.strip(),
        "date": start.astimezone(timezone.utc).isoformat(),
        "distance_m": distance_m,
        "duration_s": duration_s,
        "elevation_m": pick("total_ascent", lambda: _ascent(streams)),
        "avg_hr": _int_or_none(pick("avg_heart_rate", lambda: _mean(streams, "hr"))),
        "max_hr": _int_or_none(pick("max_heart_rate", lambda: _max(streams, "hr"))),
        "avg_power": _int_or_none(pick("avg_power", lambda: _mean(streams, "power"))),
        "max_power": _int_or_none(pick("max_power", lambda: _max(streams, "power"))),
        "avg_cadence": _int_or_none(pick("avg_cadence", lambda: _mean(streams, "cadence"))),
        "avg_speed_kmh": round(distance_m / duration_s * 3.6, 2) if duration_s else 0.0,
        "calories": _int_or_none(s.get("total_calories")),
        "suffer_score": None,
        "device": device,
        "strava_type": strava_type,
        "trainer": trainer,
    }


def _int_or_none(value):
    return None if value is None else int(round(value))


def _in_range(value, bounds):
    lo, hi = bounds
    return value if value is not None and lo <= value <= hi else None


def _track(points: list) -> list:
    """Stream points from parsed ``t``/``h``/``c``/``p`` points, ``t`` in Unix seconds.

    Raises ``ParseError`` when the points span more than MAX_SPAN_S. A heart
    rate, cadence or power outside its plausible range becomes None.
    """
    times = [p["t"] for p in points]
    span = max(times) - min(times)
    if span > MAX_SPAN_S:
        raise ParseError(f"points span {span / 86400:.1f} days; the limit is 7 days")
    for p in points:
        p["h"] = _in_range(p["h"], HR_RANGE)
        p["c"] = _in_range(p["c"], CADENCE_RANGE)
        p["p"] = _in_range(p["p"], POWER_RANGE)
    return _parse_track_points(points)


# ---------------------------------------------------------------- FIT

def _unexpected_is_parse_error(kind, parse):
    """Run ``parse``; any error other than ParseError/NotCycling becomes a ParseError.

    The input is an untrusted file, and a valid checksum or well-formed XML
    still lets it hold values no rule here expects. Whatever they raise means
    the file can't be read, and the caller files it as failed.
    """
    try:
        return parse()
    except (ParseError, NotCycling):
        raise
    except Exception as e:
        raise ParseError(f"unreadable {kind} file: {type(e).__name__}: {e}") from e


def parse_fit(raw: bytes) -> tuple[dict, list]:
    """Parse one FIT file into ``(data, streams)``.

    Raises ``ParseError`` for an unreadable file (bad CRC, truncated, no
    session, fewer than two timed records, records spanning more than 7 days)
    and ``NotCycling`` when the first session's sport isn't cycling or
    e-biking. Records more than an hour outside the session are dropped.
    """
    return _unexpected_is_parse_error("FIT", lambda: _parse_fit(raw))


def _parse_fit(raw: bytes) -> tuple[dict, list]:
    points, session, file_id, activity = [], None, {}, {}
    try:
        with fitdecode.FitReader(io.BytesIO(raw), check_crc=fitdecode.CrcCheck.RAISE) as fr:
            for frame in fr:
                if frame.frame_type != fitdecode.FIT_FRAME_DATA:
                    continue
                if frame.name == "record":
                    points.append(_fit_point(frame))
                elif frame.name == "session" and session is None:
                    session = {**_numbers(frame, (
                        "total_timer_time", "total_elapsed_time", "total_distance",
                        "total_ascent", "total_calories", "avg_heart_rate",
                        "max_heart_rate", "avg_power", "max_power", "avg_cadence")),
                        **_fields(frame, ("sport", "sub_sport", "start_time"))}
                elif frame.name == "file_id" and not file_id:
                    file_id = _fields(frame, ("manufacturer", "product", "product_name"))
                elif frame.name == "activity" and not activity:
                    activity = _fields(frame, ("timestamp", "local_timestamp"))
    except fitdecode.FitError as e:
        raise ParseError(f"unreadable FIT file: {e}") from e

    if session is None:
        raise ParseError("no session in FIT file")
    sport = str(session["sport"])
    sub_sport = str(session["sub_sport"] or "")
    if sport not in CYCLING_SPORTS:
        raise NotCycling(sport)

    timed = [p for p in points if isinstance(p["t"], datetime)]
    # A device can stamp its first records before GPS lock sets its clock,
    # years off. The session's own start (and end, when it says how long it
    # ran) bounds the ride, so those records neither date it nor stretch it.
    session_start = session["start_time"]
    if isinstance(session_start, datetime):
        lo = session_start - SESSION_SLACK
        elapsed = session["total_elapsed_time"]
        hi = None if elapsed is None else \
            session_start + timedelta(seconds=elapsed) + SESSION_SLACK
        timed = [p for p in timed if p["t"] >= lo and (hi is None or p["t"] <= hi)]
    if len(timed) < 2:
        raise ParseError("no timestamps")
    start = min(p["t"] for p in timed)
    for p in timed:
        p["t"] = p["t"].timestamp()
    streams = _track(timed)

    # activity.local_timestamp is the device's wall clock at activity.timestamp
    # (written as if UTC); the gap between the two is the local offset.
    local_start = start
    if isinstance(activity.get("timestamp"), datetime) and \
            isinstance(activity.get("local_timestamp"), datetime):
        local_start = start + (activity["local_timestamp"] - activity["timestamp"])

    if sub_sport == "virtual_activity":
        strava_type = "VirtualRide"
    elif sport == "e_biking" or sub_sport.startswith("e_bike"):
        strava_type = "EBikeRide"
    else:
        strava_type = "Ride"

    device_text = " ".join(str(file_id.get(k) or "") for k in
                           ("manufacturer", "product", "product_name"))
    data = _summary(streams, start, local_start, session=session,
                    device=_device(device_text), strava_type=strava_type,
                    trainer=sub_sport in TRAINER_SUB_SPORTS)
    return data, streams


def _fields(frame, names) -> dict:
    return {n: frame.get_value(n, fallback=None) for n in names}


def _numbers(frame, names) -> dict:
    """Numeric fields; anything else (an array from a bad definition) is None."""
    return {n: v if isinstance(v, (int, float)) and not isinstance(v, bool) else None
            for n, v in _fields(frame, names).items()}


def _fit_point(frame) -> dict:
    v = _numbers(frame, ("position_lat", "position_long", "altitude",
                         "enhanced_altitude", "speed", "enhanced_speed",
                         "heart_rate", "cadence", "power"))
    v["timestamp"] = frame.get_value("timestamp", fallback=None)
    speed = v["enhanced_speed"] if v["enhanced_speed"] is not None else v["speed"]
    alt = v["enhanced_altitude"] if v["enhanced_altitude"] is not None else v["altitude"]
    lat, lng = v["position_lat"], v["position_long"]
    has_fix = isinstance(lat, int) and isinstance(lng, int)
    lat, lng = _coords(lat * SEMICIRCLE_DEG, lng * SEMICIRCLE_DEG) if has_fix else (None, None)
    return {
        "t": v["timestamp"],
        "y": lat,
        "x": lng,
        "e": alt,
        "s": None if speed is None else speed * 3.6,
        "h": _int_or_none(v["heart_rate"]),
        "c": _int_or_none(v["cadence"]),
        "p": _int_or_none(v["power"]),
    }


# ---------------------------------------------------------------- GPX

def _local(tag) -> str:
    return tag.rsplit("}", 1)[-1] if isinstance(tag, str) else ""


def _child(elem, name):
    return next((c for c in elem if _local(c.tag) == name), None)


def _number(text):
    try:
        value = float(text)
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) else None


def _gpx_time(text):
    """Unix seconds, or None for a time that isn't one (or is out of range)."""
    try:
        t = datetime.fromisoformat((text or "").strip().replace("Z", "+00:00"))
        if t.tzinfo is None:
            t = t.replace(tzinfo=timezone.utc)
        # Converting to UTC can leave datetime's range: year 1 at +14:00 is
        # year 0 in UTC. The round trip proves the instant is representable.
        return datetime.fromtimestamp(t.timestamp(), timezone.utc).timestamp()
    except (ValueError, OverflowError, OSError):
        return None


def _coords(lat, lng):
    """A fix inside the globe, else no fix at all."""
    if lat is None or lng is None or not (-90 <= lat <= 90 and -180 <= lng <= 180):
        return None, None
    return lat, lng


def parse_gpx(raw: bytes) -> tuple[dict, list]:
    """Parse one GPX file into ``(data, streams)``.

    The bytes must be UTF-8 (a UTF-8 BOM is allowed). The DTD guard and the
    parser both read the same decoded text, so another encoding can't carry an
    entity declaration past the guard.
    """
    return _unexpected_is_parse_error("GPX", lambda: _parse_gpx(raw))


def _parse_gpx(raw: bytes) -> tuple[dict, list]:
    if raw.startswith(b"\xef\xbb\xbf"):
        raw = raw[3:]
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as e:
        raise ParseError("GPX must be UTF-8") from e
    if "<!ENTITY" in text or "<!DOCTYPE" in text:
        raise ParseError("DTD not allowed")
    try:
        root = ET.fromstring(text)
    except ET.ParseError as e:
        raise ParseError(f"unreadable GPX file: {e}") from e

    trk = next((e for e in root.iter() if _local(e.tag) == "trk"), None)
    name = type_ = None
    if trk is not None:
        name_el, type_el = _child(trk, "name"), _child(trk, "type")
        name = name_el.text if name_el is not None else None
        type_ = (type_el.text or "").strip().lower() if type_el is not None else ""
        if type_.isascii() and type_.isdigit():
            if type_ != CYCLING_GPX_TYPE_CODE:
                raise NotCycling(f"type {type_}")
        elif any(word in type_ for word in NON_CYCLING_GPX_TYPES):
            raise NotCycling(type_)

    points = []
    for pt in root.iter():
        if _local(pt.tag) != "trkpt":
            continue
        time_el, ele_el = _child(pt, "time"), _child(pt, "ele")
        t = _gpx_time(time_el.text) if time_el is not None else None
        if t is None:
            continue
        lat, lng = _coords(_number(pt.get("lat")), _number(pt.get("lon")))
        ext = {}
        for e in pt.iter():
            key = _local(e.tag)
            if key in ("hr", "cad", "power", "PowerInWatts") and key not in ext:
                ext[key] = _number(e.text)
        power = ext.get("power")
        if power is None:
            power = ext.get("PowerInWatts")
        points.append({
            "t": t,
            "y": lat,
            "x": lng,
            "e": _number(ele_el.text) if ele_el is not None else None,
            "s": None,
            "h": _int_or_none(ext.get("hr")),
            "c": _int_or_none(ext.get("cad")),
            "p": _int_or_none(power),
        })
    if len(points) < 2:
        raise ParseError("no timestamps")
    start = datetime.fromtimestamp(min(p["t"] for p in points), timezone.utc)
    streams = _track(points)
    data = _summary(streams, start, start, name=name, device=_device(root.get("creator")))
    return data, streams


# ---------------------------------------------------------------- import

# The outcomes that changed a stored ride, so fitness needs recalculating.
CHANGED_OUTCOMES = {"imported", "merged"}

# Columns the skip path's UPDATE can fill on the ride a file matched. A change
# in any of them means the file added something.
_FILLABLE = ("avg_hr", "max_hr", "avg_power", "max_power", "avg_cadence",
             "calories", "elevation_m", "distance_m")


@dataclass
class Result:
    name: str
    outcome: str
    detail: str | None
    activity_id: int | None
    sha256: str | None
    # True when the imported_files ledger answered for these bytes: the outcome
    # is from an earlier import, and nothing was stored this time.
    from_ledger: bool = False


def _message(e: Exception) -> str:
    """An exception's first line: psycopg2 errors end in a newline and can go
    on with the failing statement, which a log line or ledger detail has no
    use for."""
    lines = str(e).strip().splitlines()
    return lines[0] if lines else type(e).__name__


class PassBudgetExceeded(Exception):
    """A folder pass ran out of time partway through one file.

    ``results`` holds the members handled before it did. The rest are left for
    the next pass, where the ledger answers for these without parsing them.
    """

    def __init__(self, results: list[Result]):
        super().__init__(f"time budget spent after {len(results)} rides")
        self.results = results


def import_file(conn, path: str, name: str, via: str, *,
                deadline: float | None = None,
                on_result: Callable[[Result], None] | None = None) -> list[Result]:
    """Import every ride file reachable from ``path``; one Result per member.

    A member already in the imported_files ledger returns its stored result
    without being parsed, a failed one included, since the same bytes fail the
    same way. A bad file never raises. A database error does, and so does a
    ``path`` that can't be opened (``OSError``, from ``iter_members``).

    ``deadline`` is a ``time.monotonic()`` value. Once it has passed, the next
    member raises ``PassBudgetExceeded`` instead of being imported. The first
    member always is, so every call gets somewhere.

    ``on_result`` is called with each Result as soon as it exists, so a caller
    still hears about the rides stored before an exception.
    """
    results = []
    for member, payload in iter_members(path, name):
        if results and deadline is not None and time.monotonic() > deadline:
            raise PassBudgetExceeded(results)
        result = _import_member(conn, member, payload, via)
        results.append(result)
        if on_result is not None:
            on_result(result)
    return results


def _import_member(conn, member: str, payload, via: str) -> Result:
    if isinstance(payload, Exception):
        # Refused before it was read (over a cap, an unreadable archive), so
        # there is no hash to ledger it under.
        return Result(member, "failed", str(payload), None, None)

    sha256 = hashlib.sha256(payload).hexdigest()
    with conn.cursor() as cur:
        cur.execute("""
            UPDATE imported_files SET last_seen = NOW() WHERE sha256 = %s
            RETURNING outcome, detail, activity_id
        """, (sha256,))
        seen = cur.fetchone()
    if seen:
        return Result(member, *seen, sha256, from_ledger=True)

    parse = parse_fit if member.lower().endswith((".fit", ".fit.gz")) else parse_gpx
    try:
        data, streams = parse(payload)
    except NotCycling as e:
        result = Result(member, "not_cycling", str(e), None, sha256)
    except ParseError as e:
        result = Result(member, "failed", str(e), None, sha256)
    else:
        data["file_sha256"] = sha256
        try:
            with db.ingest_lock(conn):
                outcome, activity_id = _store(conn, data, streams)
                result = Result(member, outcome, None, activity_id, sha256)
                # The ledger row goes in under the same lock as the store: a
                # concurrent import of the same bytes waits here, then finds the
                # ride held and cannot ledger "duplicate" ahead of the storing
                # outcome.
                _ledger(conn, result, member, payload, via)
        except psycopg2.DataError as e:
            # A value the parser let through and a column can't hold. The same
            # bytes fail the same way, so it is ledgered like a parse failure.
            result = Result(member, "failed", _message(e), None, sha256)
            _ledger(conn, result, member, payload, via)
        return result

    _ledger(conn, result, member, payload, via)
    return result


def _ledger(conn, result: Result, member: str, payload, via: str) -> None:
    """Record the outcome for these bytes. file_sha256's unique index already
    makes a repeat of the store idempotent; a repeat here keeps the first
    outcome and only moves last_seen."""
    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO imported_files
                (sha256, name, size_bytes, via, outcome, detail, activity_id)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (sha256) DO UPDATE SET last_seen = NOW()
        """, (result.sha256, member, len(payload), via, result.outcome,
              result.detail, result.activity_id))


def _store(conn, data: dict, streams: list) -> tuple[str, int]:
    """Store one parsed ride under the ingest lock. Returns (outcome, id)."""
    # These bytes are already on a ride: stored before and the ledger row lost,
    # merged onto a cloud ride, or an earlier import committed the row and then
    # failed writing streams. The row is never rewritten: that would rewrite the
    # streams of a file-only ride, clearing its W' and segment matches for
    # nothing. Only what a failed stream write left out is finished: streams on
    # a ride that has none, or the channel a failed swap didn't bring. Equal
    # channels make the swap a no-op, so a repeat adds nothing.
    with conn.cursor() as cur:
        cur.execute("""
            SELECT a.id, EXISTS (SELECT 1 FROM activity_streams s WHERE s.activity_id = a.id)
            FROM activities a WHERE a.file_sha256 = %s
        """, (data["file_sha256"],))
        held = cur.fetchone()
    if held:
        held_id, has_streams = held
        if not has_streams:
            db.upsert_streams(conn, held_id, streams)
            return "merged", held_id
        swapped = db.replace_streams_if_richer(conn, held_id, streams)
        return ("merged" if swapped else "duplicate"), held_id

    # upsert_activity looks for a duplicate under the same condition and the
    # same lock, so it finds the same row.
    dup = None
    if data.get("date") and data.get("duration_s"):
        dup = db.find_duplicate(conn, data["date"], data["duration_s"],
                                data.get("distance_m", 0))

    if dup is None:
        activity_id, preserved = db.upsert_activity(conn, data, streams=streams)
        if not preserved:
            db.upsert_streams(conn, activity_id, streams)
        return "imported", activity_id

    if db.merge_activity_data(dup, db.classify_activity(data)).get("_skip_insert"):
        # The stored ride stays the base: the file can only fill its empty
        # summary columns, and its streams only when they add a channel.
        before = _fillable(conn, dup[0])
        activity_id, _ = db.upsert_activity(conn, data, streams=streams)
        swapped = db.replace_streams_if_richer(conn, activity_id, streams)
        changed = swapped or _fillable(conn, activity_id) != before
        return ("merged" if changed else "duplicate"), activity_id

    activity_id, _ = db.upsert_activity(conn, data, streams=streams)
    return "merged", activity_id


def _fillable(conn, activity_id: int) -> tuple:
    with conn.cursor() as cur:
        cur.execute(f"SELECT {', '.join(_FILLABLE)} FROM activities WHERE id = %s",
                    (activity_id,))
        return cur.fetchone()


# ---------------------------------------------------------------- watched folder

# Where compose mounts the watched folder, read-only. Unconfigured, it is an
# empty named volume, so the scan finds nothing.
IMPORT_DIR = "/app/import"

# How long one folder pass may run. A first load of thousands of files spreads
# over several polls, and the ledger makes resuming free.
FOLDER_PASS_SECONDS = 240

# The errors that mean the database is unreachable, so every later file would
# fail the same way and the pass ends. Any other error belongs to one file.
_CONNECTION_ERRORS = (psycopg2.OperationalError, psycopg2.InterfaceError)


def folder_configured() -> bool:
    """Whether the operator set VELOMATE_IMPORT_DIR. Compose passes it through
    empty when unset, and "0" reads as unset too, the way the other variables do."""
    return os.environ.get("VELOMATE_IMPORT_DIR", "") not in ("", "0")


def _walk(root: str, problems: list[str]) -> Iterator[tuple[str, str, os.stat_result]]:
    """Yield ``(path, relative name, stat)`` for each regular file, in a stable order.

    Symlinks are not followed, files or directories: a host-absolute one would
    not resolve inside the container anyway, and a loop would never end. A
    missing folder, or one that vanished mid-walk, is nothing to report.
    """
    def report(e: OSError):
        if not isinstance(e, FileNotFoundError):
            problems.append(f"{os.path.relpath(e.filename, root) if e.filename else root}: {e}")

    for dirpath, dirnames, filenames in os.walk(root, onerror=report):
        dirnames.sort()
        for filename in sorted(filenames):
            path = os.path.join(dirpath, filename)
            try:
                st = os.lstat(path)
            except FileNotFoundError:
                continue
            except OSError as e:
                report(e)
                continue
            if stat.S_ISREG(st.st_mode):
                yield path, os.path.relpath(path, root).replace(os.sep, "/"), st


def scan_folder(conn, root: str = IMPORT_DIR,
                budget_s: float = FOLDER_PASS_SECONDS) -> tuple[int, list[str]]:
    """Import what is new or changed under ``root``. Returns (changed rides, problems).

    A file whose size and mtime match its imported_paths row is skipped without
    being read. Anything else goes through ``import_file``, and its path is
    recorded only once every ride in it was handled, so a pass that runs out
    of time inside an archive picks it up again next time. Files are never
    moved or deleted: the folder is mounted read-only.

    A file that fails to parse is a ledgered outcome, not a problem. Any other
    error from one file (it can't be read, or a bug) is a problem for this
    pass: its path isn't recorded, and the pass goes on. Losing the database
    connection is a problem that ends the pass.

    Every result stored this pass is printed as ``[file] <name>: <outcome>``,
    with its detail in brackets. A ledger hit prints nothing and doesn't count
    as changed: the pass that stored it already did both. A failure with no
    hash (an archive refused whole, a member over a cap) has no ledger row, so
    it prints every time its file is read.
    """
    deadline = time.monotonic() + budget_s
    changed, problems = 0, []

    def report(result: Result):
        nonlocal changed
        if result.from_ledger:
            return
        detail = f" ({result.detail})" if result.detail else ""
        print(f"[file] {result.name}: {result.outcome}{detail}")
        if result.outcome in CHANGED_OUTCOMES:
            changed += 1

    try:
        with conn.cursor() as cur:
            cur.execute("SELECT path, size_bytes, mtime_ns FROM imported_paths")
            known = {path: (size, mtime) for path, size, mtime in cur.fetchall()}
    except Exception as e:
        return 0, [f"reading imported_paths: {e}"]

    for path, name, st in _walk(root, problems):
        if time.monotonic() > deadline:
            break
        if known.get(name) == (st.st_size, st.st_mtime_ns):
            continue
        try:
            import_file(conn, path, name, "folder", deadline=deadline, on_result=report)
        except PassBudgetExceeded:
            break
        except _CONNECTION_ERRORS as e:
            problems.append(f"{name}: {_message(e)}")
            break
        except Exception as e:
            problems.append(f"{name}: {_message(e)}")
            continue
        try:
            with conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO imported_paths (path, size_bytes, mtime_ns)
                    VALUES (%s, %s, %s)
                    ON CONFLICT (path) DO UPDATE SET size_bytes = EXCLUDED.size_bytes,
                        mtime_ns = EXCLUDED.mtime_ns, done_at = NOW()
                """, (name, st.st_size, st.st_mtime_ns))
        except _CONNECTION_ERRORS as e:
            problems.append(f"{name}: {_message(e)}")
            break
        except Exception as e:
            problems.append(f"{name}: {_message(e)}")
    return changed, problems
