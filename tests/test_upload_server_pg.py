"""Upload server end to end: a FIT file posted over HTTP becomes a stored ride.

The real import_file and a real PostgreSQL; only sign-in and recalculation are
fakes. The track is synthetic, near lat 0 / lng 0: the repo is mirrored
publicly.

Run with:
    VELOMATE_TEST_DATABASE_URL=postgresql://... python3 -m pytest \
        tests/test_upload_server_pg.py -q
"""
import http.client
import json
import os
import sys
import tempfile
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

_tests_dir = Path(__file__).resolve().parent
sys.path.insert(0, str(_tests_dir))
sys.path.insert(0, str(_tests_dir.parent / "ingestor"))

from fit_builder import build_fit  # noqa: E402
from pg_reset import reset_schema  # noqa: E402

TEST_DB = os.environ.get("VELOMATE_TEST_DATABASE_URL")

pytestmark = pytest.mark.skipif(
    not TEST_DB,
    reason="needs a throwaway PostgreSQL in VELOMATE_TEST_DATABASE_URL",
)

START = datetime(2026, 6, 1, 8, 0, tzinfo=timezone.utc)
ROOT = "http://grafana.test:3021/"


class AllowAll:
    def check(self, authorization):
        from upload_auth import AuthDecision
        return AuthDecision(True, 200, "ok", "rider")


def _connect():
    import psycopg2
    conn = psycopg2.connect(TEST_DB)
    conn.autocommit = True
    return conn


@pytest.fixture()
def conn():
    import psycopg2
    if not hasattr(psycopg2, "__file__"):
        pytest.skip("psycopg2 is mocked by another module in this pytest session")
    import db as velomate_db
    connection = _connect()
    reset_schema(connection)
    velomate_db.create_schema(connection)
    yield connection
    connection.close()


def _fit() -> bytes:
    records, lat = [], 0.0
    for i in range(600):
        if i:
            lat += (30 / 3.6) / 111195.0
        records.append({"time": START + timedelta(seconds=i), "lat": lat,
                        "lng": 0.0, "heart_rate": 140, "cadence": 90,
                        "speed": 30 / 3.6, "altitude": 100.0, "power": 200})
    return build_fit(records, total_timer_time=600)


def test_end_to_end_fit_upload_imports_a_ride(conn, monkeypatch, tmp_path):
    import upload_server

    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    recalcs = []
    server = upload_server.make_server(
        "127.0.0.1", 0, auth=AllowAll(), connect=_connect,
        recalc=lambda c: recalcs.append(c), grafana_root_url=ROOT,
        debounce_seconds=60)
    thread = threading.Thread(target=server.serve_forever, args=(0.05,), daemon=True)
    thread.start()
    try:
        c = http.client.HTTPConnection("127.0.0.1", server.server_address[1],
                                       timeout=30)
        c.request("POST", "/upload", body=_fit(),
                  headers={"Authorization": "Basic dTpw",
                           "X-Filename": "morning%20ride.fit"})
        r = c.getresponse()
        body = json.loads(r.read())
        c.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(5)

    assert r.status == 200, body
    [result] = body["results"]
    assert result["name"] == "morning ride.fit"
    assert result["outcome"] == "imported"
    assert result["from_ledger"] is False
    activity_id = result["activity_id"]
    assert isinstance(activity_id, int)
    assert result["link"] == f"{ROOT}d/velomate-activity/?var-activity_id={activity_id}"

    with conn.cursor() as cur:
        cur.execute("SELECT via, outcome, activity_id, name FROM imported_files")
        assert cur.fetchall() == [("upload", "imported", activity_id,
                                   "morning ride.fit")]
        cur.execute("SELECT count(*) FROM activities WHERE id = %s", (activity_id,))
        assert cur.fetchone() == (1,)
    assert list((tmp_path / "velomate-upload").iterdir()) == []

    # The changed upload left a recalculation pending; shutdown runs it.
    server.debouncer.flush()
    assert len(recalcs) == 1
    recalcs[0].close()
