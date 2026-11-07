"""Write minimal valid FIT activity files for tests.

Every track the tests build is synthetic, near lat 0 / lng 0, so no real ride
data enters the repo (it is mirrored publicly).

The file holds a 14-byte header, one definition and its data messages for
file_id (global 0), record (20) and session (18), plus activity (34) when
``local_start`` is given, then the file CRC. Field numbers and scales come from
the FIT profile. A value left out of a record is written as the base type's
invalid value, which a FIT reader reports as missing.
"""

import struct
from datetime import datetime, timezone

from fitdecode.profile import FIELD_TYPES

FIT_EPOCH = datetime(1989, 12, 31, tzinfo=timezone.utc)

# Base types: (code, struct format, invalid value)
ENUM = (0x00, "B", 0xFF)
UINT8 = (0x02, "B", 0xFF)
UINT16 = (0x84, "H", 0xFFFF)
SINT32 = (0x85, "i", 0x7FFFFFFF)
UINT32 = (0x86, "I", 0xFFFFFFFF)

_CRC_TABLE = (
    0x0000, 0xCC01, 0xD801, 0x1400, 0xF001, 0x3C00, 0x2800, 0xE401,
    0xA001, 0x6C00, 0x7800, 0xB401, 0x5000, 0x9C01, 0x8801, 0x4400,
)


def fit_crc(data: bytes) -> int:
    crc = 0
    for byte in data:
        tmp = _CRC_TABLE[crc & 0xF]
        crc = (crc >> 4) & 0x0FFF
        crc = crc ^ tmp ^ _CRC_TABLE[byte & 0xF]
        tmp = _CRC_TABLE[crc & 0xF]
        crc = (crc >> 4) & 0x0FFF
        crc = crc ^ tmp ^ _CRC_TABLE[(byte >> 4) & 0xF]
    return crc


def _enum(type_name: str, value):
    """A profile enum name ('cycling', 'hammerhead') to its number."""
    if value is None or isinstance(value, int):
        return value
    for number, name in FIELD_TYPES[type_name].enum.items():
        if name == value:
            return number
    raise ValueError(f"{value!r} is not a FIT {type_name}")


def _fit_time(dt: datetime) -> int:
    if dt.tzinfo is None:  # a local wall-clock time, written as if it were UTC
        dt = dt.replace(tzinfo=timezone.utc)
    return int((dt - FIT_EPOCH).total_seconds())


def _semicircles(degrees):
    return None if degrees is None else round(degrees * (2 ** 31) / 180)


class _Writer:
    def __init__(self):
        self.body = bytearray()

    def define(self, local: int, global_num: int, fields: list):
        self.body += struct.pack("<BBBHB", 0x40 | local, 0, 0, global_num, len(fields))
        for number, (code, fmt, _invalid) in fields:
            self.body += struct.pack("<BBB", number, struct.calcsize(fmt), code)

    def data(self, local: int, fields: list, values: list):
        self.body.append(local)
        for (_number, (_code, fmt, invalid)), value in zip(fields, values):
            self.body += struct.pack("<" + fmt, invalid if value is None else value)


FILE_ID = [(0, ENUM), (1, UINT16), (4, UINT32)]
RECORD = [(253, UINT32), (0, SINT32), (1, SINT32), (2, UINT16), (3, UINT8),
          (4, UINT8), (6, UINT16), (7, UINT16)]
SESSION = [(253, UINT32), (2, UINT32), (5, ENUM), (6, ENUM), (8, UINT32)]
ACTIVITY = [(253, UINT32), (5, UINT32)]


def build_fit(records: list, sport="cycling", sub_sport="road",
              total_timer_time=None, manufacturer=None, local_start=None,
              total_calories=None, session_start=None,
              total_elapsed_time=None) -> bytes:
    """Return the bytes of a FIT activity file.

    Each record is a dict with ``time`` (an aware UTC datetime) and any of
    ``lat``/``lng`` (degrees), ``altitude`` (m), ``heart_rate``, ``cadence``,
    ``speed`` (m/s) and ``power``. ``local_start`` is the naive local
    wall-clock time of the first record; it adds an activity message whose
    timestamp is the last record's and whose local_timestamp is the matching
    local time, the way a device writes it at the end of a ride.
    ``total_calories`` (kcal) adds that field to the session; without it the
    bytes are what they were before the option existed.
    ``session_start`` overrides the session's start_time (the first record's
    time by default), and ``total_elapsed_time`` (s) adds that session field;
    left out, neither changes the bytes.
    """
    first, last = records[0]["time"], records[-1]["time"]
    w = _Writer()

    w.define(0, 0, FILE_ID)
    w.data(0, FILE_ID, [4, _enum("manufacturer", manufacturer), _fit_time(first)])

    w.define(1, 20, RECORD)
    for r in records:
        alt = r.get("altitude")
        speed = r.get("speed")
        w.data(1, RECORD, [
            _fit_time(r["time"]),
            _semicircles(r.get("lat")),
            _semicircles(r.get("lng")),
            None if alt is None else round((alt + 500) * 5),
            r.get("heart_rate"),
            r.get("cadence"),
            None if speed is None else round(speed * 1000),
            r.get("power"),
        ])

    calories = [] if total_calories is None else [(11, UINT16)]
    elapsed = [] if total_elapsed_time is None else [(7, UINT32)]
    w.define(2, 18, SESSION + calories + elapsed)
    w.data(2, SESSION + calories + elapsed, [
        _fit_time(last),
        _fit_time(first if session_start is None else session_start),
        _enum("sport", sport),
        _enum("sub_sport", sub_sport),
        None if total_timer_time is None else round(total_timer_time * 1000),
    ] + ([] if total_calories is None else [total_calories])
      + ([] if total_elapsed_time is None else [round(total_elapsed_time * 1000)]))

    if local_start is not None:
        w.define(3, 34, ACTIVITY)
        w.data(3, ACTIVITY, [_fit_time(last), _fit_time(local_start + (last - first))])

    body = bytes(w.body)
    header = struct.pack("<BBHI4s", 14, 0x20, 2132, len(body), b".FIT")
    header += struct.pack("<H", fit_crc(header))
    out = header + body
    return out + struct.pack("<H", fit_crc(out))
