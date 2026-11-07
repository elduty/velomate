"""Upload server: sign-in, limits, spooling and results, without a database.

``import_file`` is replaced per test, and ``connect`` and ``recalc`` are
fakes, so these tests are about the HTTP side only. Every server runs on
127.0.0.1:0 in a thread, and temporary files are redirected to the test's own
directory so a test can see whether anything was spooled.
"""
import http.client
import json
import re
import socket
import sys
import tempfile
import threading
import time
from pathlib import Path
from urllib.parse import quote

import psycopg2
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "ingestor"))

import file_import  # noqa: E402
import upload_server  # noqa: E402
from file_import import Result  # noqa: E402
from upload_auth import AuthDecision  # noqa: E402

ROOT = "http://grafana.test:3021/"
BASIC = "Basic dTpw"
OK = AuthDecision(True, 200, "ok", "rider")
WWW_AUTH = 'Basic realm="VeloMate upload"'


class FakeAuth:
    def __init__(self, decision=OK):
        self.decision = decision
        self.calls = 0

    def check(self, authorization):
        self.calls += 1
        return self.decision


class FakeConn:
    def __init__(self):
        self.closed = False

    def close(self):
        self.closed = True


class Recorder:
    """Stands in for import_file: records each call and returns ``results``."""

    def __init__(self, results=None, raises=None, block=None):
        self.results = results if results is not None else [
            Result("ride.fit", "imported", None, 1, "a" * 64)]
        self.raises = raises
        self.block = block
        self.calls = []
        self.entered = threading.Semaphore(0)

    def __call__(self, conn, path, name, via, *, deadline=None, on_result=None):
        p = Path(path)
        self.calls.append({"conn": conn, "path": p, "name": name, "via": via,
                           "exists": p.exists(),
                           "body": p.read_bytes() if p.exists() else None})
        self.entered.release()
        if self.block is not None:
            assert self.block.wait(10)
        for r in self.results:
            if on_result is not None:
                on_result(r)
        if self.raises is not None:
            raise self.raises
        return list(self.results)


class RecalcCounter:
    def __init__(self):
        self.conns = []

    def __call__(self, conn):
        self.conns.append(conn)

    @property
    def count(self):
        return len(self.conns)


@pytest.fixture
def serve(monkeypatch, tmp_path):
    spool = tmp_path / "spool"
    spool.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(spool))
    baseline = set(threading.enumerate())
    started = []

    def start(*, auth=None, importer=None, recalc=None, debounce=60.0):
        conns = []

        def connect():
            c = FakeConn()
            conns.append(c)
            return c

        importer = importer or Recorder()
        monkeypatch.setattr(file_import, "import_file", importer)
        server = upload_server.make_server(
            "127.0.0.1", 0, auth=auth or FakeAuth(), connect=connect,
            recalc=recalc or RecalcCounter(), grafana_root_url=ROOT,
            debounce_seconds=debounce)
        thread = threading.Thread(target=server.serve_forever, args=(0.05,), daemon=True)
        thread.start()
        server.test_conns = conns
        server.test_spool = Path(server.spool_dir)
        server.test_importer = importer
        started.append((server, thread))
        return server

    yield start

    for server, thread in started:
        server.shutdown()
        server.server_close()
        thread.join(5)
        server.debouncer.flush()
    # Handler and timer threads must all have finished: nothing left running.
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        stray = [t for t in threading.enumerate()
                 if t not in baseline and t.is_alive()]
        if not stray:
            break
        time.sleep(0.02)
    assert not stray, stray


def _conn(server, timeout=10):
    return http.client.HTTPConnection("127.0.0.1", server.server_address[1],
                                      timeout=timeout)


def post(server, body=b"FITDATA", name="ride.fit", *, raw_name=None,
         auth=BASIC):
    c = _conn(server)
    headers = {}
    if auth is not None:
        headers["Authorization"] = auth
    if raw_name is not None:
        headers["X-Filename"] = raw_name
    elif name is not None:
        headers["X-Filename"] = quote(name)
    try:
        c.request("POST", "/upload", body=body, headers=headers)
        r = c.getresponse()
        data = r.read()
        return r.status, r, json.loads(data) if data else None
    finally:
        c.close()


def _declared_only(server, length, *, auth=BASIC, extra=b""):
    """Send headers declaring ``length`` body bytes, then only ``extra``.

    Returns everything the server sends back before it closes. The socket
    timeout is what fails the test if the server waits for the body.
    """
    s = socket.create_connection(("127.0.0.1", server.server_address[1]),
                                 timeout=5)
    try:
        head = "POST /upload HTTP/1.1\r\nHost: test\r\nX-Filename: ride.fit\r\n"
        if auth:
            head += f"Authorization: {auth}\r\n"
        if length is not None:
            head += f"Content-Length: {length}\r\n"
        s.sendall(head.encode("latin-1") + b"\r\n" + extra)
        data = b""
        while True:
            chunk = s.recv(65536)
            if not chunk:
                return data
            data += chunk
    finally:
        s.close()


def _status(raw):
    return int(raw.split(b" ", 2)[1])


def _json_body(raw):
    return json.loads(raw.split(b"\r\n\r\n", 1)[1])


# ------------------------------------------------------------------ sign-in

def test_unauthenticated_upload_is_refused_before_reading_the_body(serve):
    auth = FakeAuth(AuthDecision(False, 401, "sign in required", None))
    server = serve(auth=auth)
    started = time.monotonic()
    raw = _declared_only(server, 104857600, auth=None, extra=b"0123456789")
    elapsed = time.monotonic() - started

    assert _status(raw) == 401
    assert f"WWW-Authenticate: {WWW_AUTH}".encode() in raw
    assert _json_body(raw) == {"error": "sign in required"}
    assert elapsed < 4
    assert list(server.test_spool.iterdir()) == []
    assert server.test_importer.calls == []
    assert auth.calls == 1


def test_forbidden_is_403_and_grafana_error_is_502(serve):
    forbidden = serve(auth=FakeAuth(AuthDecision(
        False, 403, "uploading needs the Editor or Admin role", "viewer")))
    status, resp, body = post(forbidden)
    assert status == 403
    assert resp.getheader("WWW-Authenticate") is None
    assert body == {"error": "uploading needs the Editor or Admin role"}

    down = serve(auth=FakeAuth(AuthDecision(
        False, 502, "could not confirm sign-in with Grafana", None)))
    status, resp, body = post(down)
    assert status == 502
    assert resp.getheader("WWW-Authenticate") is None
    assert body == {"error": "could not confirm sign-in with Grafana"}

    assert forbidden.test_importer.calls == [] and down.test_importer.calls == []


def test_get_page_requires_auth_and_serves_html(serve):
    refused = serve(auth=FakeAuth(AuthDecision(False, 401, "sign in required", None)))
    c = _conn(refused)
    c.request("GET", "/")
    r = c.getresponse()
    body = r.read()
    c.close()
    assert r.status == 401
    assert r.getheader("WWW-Authenticate") == WWW_AUTH
    assert b"<html" not in body

    server = serve()
    c = _conn(server)
    c.request("GET", "/", headers={"Authorization": BASIC})
    r = c.getresponse()
    html = r.read().decode()
    c.close()
    assert r.status == 200
    assert r.getheader("Content-Type").startswith("text/html")
    assert "default-src 'none'" in r.getheader("Content-Security-Policy")
    assert re.search(r'<input[^>]*type="file"[^>]*multiple', html)
    assert 'accept=".fit,.gpx,.gz,.zip"' in html
    assert "500 MB" in html
    # Everything inline: nothing is fetched from anywhere else.
    assert not re.search(r'(src|href)\s*=\s*["\']?(https?:)?//', html)
    assert "<script src" not in html and "<link" not in html
    # Server-supplied strings go in as text, never as markup.
    assert "innerHTML" not in html


def test_other_paths_and_methods_are_404(serve):
    server = serve()
    for method, path in (("GET", "/nope"), ("POST", "/"), ("DELETE", "/upload"),
                         ("PUT", "/upload"), ("GET", "/upload")):
        c = _conn(server)
        c.request(method, path, headers={"Authorization": BASIC})
        r = c.getresponse()
        body = r.read()
        c.close()
        assert r.status == 404, (method, path)
        assert "error" in json.loads(body)
    assert server.test_importer.calls == []


# ------------------------------------------------------------------- limits

def test_too_large_is_413_without_reading(serve):
    server = serve()
    raw = _declared_only(server, upload_server.MAX_UPLOAD_BYTES + 1)
    assert upload_server.MAX_UPLOAD_BYTES == 500 * 1024 * 1024
    assert _status(raw) == 413
    assert "watched folder" in _json_body(raw)["error"]
    assert list(server.test_spool.iterdir()) == []
    assert server.test_importer.calls == []


def test_largest_allowed_length_is_not_refused(serve):
    """The cap is inclusive: exactly MAX_UPLOAD_BYTES gets past the check (the
    client then hangs up, so nothing is imported)."""
    server = serve()
    s = socket.create_connection(("127.0.0.1", server.server_address[1]),
                                 timeout=5)
    s.sendall(b"POST /upload HTTP/1.1\r\nHost: t\r\nX-Filename: ride.fit\r\n"
              b"Authorization: " + BASIC.encode() + b"\r\n"
              b"Content-Length: " + str(upload_server.MAX_UPLOAD_BYTES).encode()
              + b"\r\n\r\nabc")
    s.settimeout(0.5)
    with pytest.raises(socket.timeout):
        s.recv(100)  # still reading the body: no early refusal
    s.close()


def test_missing_length_is_411(serve):
    server = serve()
    raw = _declared_only(server, None)
    assert _status(raw) == 411
    assert "error" in _json_body(raw)
    assert server.test_importer.calls == []


def test_bad_length_is_400(serve):
    server = serve()
    # "²" passes str.isdigit() but not int().
    for bad in ("-1", "12x", "²"):
        raw = _declared_only(server, bad)
        assert _status(raw) == 400, bad
    assert server.test_importer.calls == []


def test_third_concurrent_upload_gets_503(serve):
    release = threading.Event()
    importer = Recorder(block=release)
    server = serve(importer=importer)
    assert upload_server.MAX_UPLOADS_IN_FLIGHT == 2

    outcomes = []
    workers = [threading.Thread(target=lambda: outcomes.append(post(server)[0]))
               for _ in range(2)]
    for w in workers:
        w.start()
    try:
        assert importer.entered.acquire(timeout=5)
        assert importer.entered.acquire(timeout=5)
        status, resp, body = post(server)
        assert status == 503
        assert resp.getheader("Retry-After") == "2"
        assert "try again" in body["error"]
    finally:
        release.set()
        for w in workers:
            w.join(10)
    assert outcomes == [200, 200]
    # The slots are free again.
    assert post(server)[0] == 200


# ------------------------------------------------------------ files, results

def test_results_carry_one_line_per_ride_with_links(serve):
    importer = Recorder(results=[
        Result("export.zip/1.fit", "imported", None, 7, "a" * 64),
        Result("export.zip/2.gpx", "failed", "no timestamps", None, "b" * 64,
               from_ledger=True),
    ])
    server = serve(importer=importer)
    status, resp, body = post(server, b"PK...", name="export.zip")
    assert status == 200
    assert resp.getheader("Content-Type").startswith("application/json")
    assert body == {"results": [
        {"name": "export.zip/1.fit", "outcome": "imported", "detail": None,
         "from_ledger": False, "activity_id": 7,
         "link": ROOT + "d/velomate-activity/?var-activity_id=7"},
        {"name": "export.zip/2.gpx", "outcome": "failed",
         "detail": "no timestamps", "from_ledger": True, "activity_id": None,
         "link": None},
    ]}
    call = importer.calls[0]
    assert call["name"] == "export.zip" and call["via"] == "upload"
    assert call["body"] == b"PK..."


def test_link_root_without_trailing_slash_gets_one():
    other = upload_server.make_server(
        "127.0.0.1", 0, auth=FakeAuth(), connect=FakeConn,
        recalc=RecalcCounter(), grafana_root_url="http://g.test")
    try:
        assert other.grafana_root_url == "http://g.test/"
    finally:
        other.server_close()


def test_filename_is_reduced_to_basename(serve):
    server = serve()
    assert post(server, raw_name="..%2F..%2Fx.fit")[0] == 200
    assert post(server, raw_name="C%3A%5Crides%5Cy.fit.gz")[0] == 200
    names = [c["name"] for c in server.test_importer.calls]
    assert names == ["x.fit", "y.fit.gz"]
    # The temp file keeps the extension, .gz included, inside the temp dir.
    paths = [c["path"] for c in server.test_importer.calls]
    assert paths[0].name.endswith(".fit") and paths[1].name.endswith(".fit.gz")
    assert all(p.parent == server.test_spool for p in paths)


def test_missing_or_bad_filename_is_400(serve):
    server = serve()
    for raw in (None, "", "..", "..%2F", "a%0Ab.fit", "%FF.fit", "notes.txt"):
        status, _, body = post(server, raw_name=raw, name=None)
        assert status == 400, raw
        assert "error" in body
    assert server.test_importer.calls == []
    assert list(server.test_spool.iterdir()) == []


def test_temp_file_is_deleted_after_success_and_after_an_error(serve):
    ok = serve()
    assert post(ok, b"ride bytes")[0] == 200
    call = ok.test_importer.calls[0]
    assert call["exists"] and call["body"] == b"ride bytes"
    assert not call["path"].exists()
    assert list(ok.test_spool.iterdir()) == []
    assert [c.closed for c in ok.test_conns] == [True]

    failing = serve(importer=Recorder(raises=OSError("unreadable")))
    status, _, body = post(failing, b"ride bytes")
    assert status == 503 and "error" in body
    assert failing.test_importer.calls[0]["exists"]
    assert list(failing.test_spool.iterdir()) == []
    assert [c.closed for c in failing.test_conns] == [True]


def test_database_error_is_503_and_server_keeps_serving(serve):
    importer = Recorder(raises=psycopg2.OperationalError("server closed the connection"))
    server = serve(importer=importer)
    status, _, body = post(server)
    assert status == 503
    assert "error" in body
    assert list(server.test_spool.iterdir()) == []

    importer.raises = None
    status, _, body = post(server)
    assert status == 200
    assert body["results"][0]["outcome"] == "imported"
    assert all(c.closed for c in server.test_conns)


# ------------------------------------------------------------ recalculation

def _wait_for(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return predicate()


def test_recalculation_runs_after_the_last_changed_upload_without_a_finish_call(serve):
    recalc = RecalcCounter()
    server = serve(recalc=recalc, debounce=0.2)
    for _ in range(3):
        assert post(server)[0] == 200
    assert recalc.count == 0  # the uploads above came in inside the debounce
    assert _wait_for(lambda: recalc.count >= 1)
    time.sleep(0.5)
    assert recalc.count == 1
    # It ran on its own connection, which was closed afterwards.
    assert isinstance(recalc.conns[0], FakeConn) and recalc.conns[0].closed


def test_ledger_hits_and_failures_do_not_trigger_recalculation(serve):
    recalc = RecalcCounter()
    importer = Recorder(results=[
        Result("a.fit", "imported", None, 1, "a" * 64, from_ledger=True),
        Result("b.fit", "merged", None, 2, "b" * 64, from_ledger=True),
        Result("c.fit", "duplicate", None, 3, "c" * 64),
        Result("d.fit", "not_cycling", "running", None, "d" * 64),
        Result("e.fit", "failed", "corrupt", None, "e" * 64),
    ])
    server = serve(recalc=recalc, importer=importer, debounce=0.05)
    assert post(server)[0] == 200
    time.sleep(0.3)
    server.debouncer.flush()
    assert recalc.count == 0


def test_rides_stored_before_a_database_error_still_trigger_recalculation(serve):
    recalc = RecalcCounter()
    importer = Recorder(
        results=[Result("x.zip/1.fit", "merged", None, 4, "a" * 64)],
        raises=psycopg2.OperationalError("connection lost"))
    server = serve(recalc=recalc, importer=importer, debounce=60)
    assert post(server, name="x.zip")[0] == 503
    server.debouncer.flush()
    assert recalc.count == 1


def test_debouncer_flush_runs_pending_work_once():
    runs = []
    d = upload_server.RecalcDebouncer(lambda: runs.append(1), 60)
    d.flush()
    assert runs == []
    d.poke()
    d.poke()
    d.flush()
    assert runs == [1]
    d.flush()
    assert runs == [1]


def test_debouncer_poke_restarts_the_timer():
    runs = []
    d = upload_server.RecalcDebouncer(lambda: runs.append(time.monotonic()), 0.3)
    first = time.monotonic()
    d.poke()
    time.sleep(0.2)
    d.poke()  # restarts: nothing may run before 0.2 + 0.3 s
    assert _wait_for(lambda: runs, 3)
    assert runs[0] - first >= 0.45
    time.sleep(0.4)
    assert len(runs) == 1


def test_debouncer_survives_a_failing_run(capsys):
    calls = []

    def run():
        calls.append(1)
        raise RuntimeError("db down")

    d = upload_server.RecalcDebouncer(run, 60)
    d.poke()
    d.flush()
    d.poke()
    d.flush()
    assert calls == [1, 1]
    assert "db down" in capsys.readouterr().out


# ------------------------------------------------------------ final fix wave

def test_page_stops_the_queue_on_401_or_403_and_offers_sign_in():
    html = upload_server.PAGE_PATH.read_text()
    m = re.search(r"response\.status === 401 \|\| response\.status === 403\)\s*\{(.*?)\n      \}", html, re.S)
    assert m, "no 401/403 branch"
    branch = m.group(1)
    assert "queue.length = 0" in branch
    assert "signInAgain(" in branch
    assert "Sign in again" in html and "location.reload()" in html


def test_spool_is_a_dedicated_dir_and_startup_empties_only_it(monkeypatch, tmp_path):
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    spool = tmp_path / "velomate-upload"
    spool.mkdir()
    (spool / "stray.fit").write_bytes(b"x" * 10)
    outside = tmp_path / "keep.txt"
    outside.write_text("keep")
    server = upload_server.make_server(
        "127.0.0.1", 0, auth=FakeAuth(), connect=FakeConn, recalc=RecalcCounter(),
        grafana_root_url=ROOT)
    try:
        assert Path(server.spool_dir) == spool
        assert list(spool.iterdir()) == []
        assert outside.read_text() == "keep"
    finally:
        server.server_close()


def test_upload_lands_under_the_spool_dir(serve):
    server = serve()
    assert post(server, b"ride")[0] == 200
    path = server.test_importer.calls[0]["path"]
    assert path.parent == server.test_spool
    assert server.test_spool.name == "velomate-upload"


def test_server_header_has_no_python_version(serve):
    server = serve()
    status, r, _ = post(server)
    assert status == 200
    assert "Python/" not in (r.getheader("Server") or "")


def test_json_responses_carry_nosniff(serve):
    server = serve()
    _, ok, _ = post(server)
    _, bad, _ = post(server, name=None)
    assert ok.getheader("X-Content-Type-Options") == "nosniff"
    assert bad.getheader("X-Content-Type-Options") == "nosniff"


def test_absurdly_long_content_length_is_413_not_a_traceback(serve, capfd):
    server = serve()
    raw = _declared_only(server, "9" * 20)
    assert _status(raw) == 413
    raw = _declared_only(server, "9" * 5000)
    assert _status(raw) == 413
    assert "Traceback" not in capfd.readouterr().err
    assert server.test_importer.calls == []


def test_client_closing_before_the_reply_is_quiet(serve, capfd):
    release = threading.Event()
    importer = Recorder(block=release)
    server = serve(importer=importer)
    s = socket.create_connection(("127.0.0.1", server.server_address[1]), timeout=5)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, b"\x01\x00\x00\x00\x00\x00\x00\x00")
    s.sendall(b"POST /upload HTTP/1.1\r\nHost: t\r\nX-Filename: ride.fit\r\n"
              b"Authorization: " + BASIC.encode() + b"\r\nContent-Length: 4\r\n\r\nabcd")
    assert importer.entered.acquire(timeout=5)
    s.close()  # reset: the reply write will fail
    time.sleep(0.2)
    release.set()
    time.sleep(0.3)
    err = capfd.readouterr().err
    assert "Traceback" not in err and "BrokenPipe" not in err
    assert post(server)[0] == 200


def test_ledger_hit_is_marked_in_the_log(serve, capsys):
    importer = Recorder(results=[
        Result("a.fit", "duplicate", None, 1, "a" * 64, from_ledger=True),
        Result("b.fit", "imported", None, 2, "b" * 64)])
    server = serve(importer=importer)
    assert post(server)[0] == 200
    out = capsys.readouterr().out
    assert "a.fit: duplicate (already imported)" in out
    assert "b.fit: imported\n" in out
