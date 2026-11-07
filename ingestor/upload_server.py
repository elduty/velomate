"""Upload page: a small HTTP server that imports ride files posted to it.

``GET /`` serves the page and ``POST /upload`` takes one file as the raw
request body, named by the percent-encoded ``X-Filename`` header. Every route
is signed in with Grafana credentials over HTTP Basic auth (upload_auth).

A request is handled in this order: sign-in, then the name and length checks,
then a slot among MAX_UPLOADS_IN_FLIGHT, and only then is the body read. It is
spooled to a temporary file (in a directory of its own, emptied at startup) in CHUNK pieces, never held in memory, handed to
``file_import.import_file`` by path, and deleted whatever happens. A refusal
sent before the body was read closes the connection, so the unread body can
never be taken for a next request.

Fitness is recalculated once uploads stop changing anything: each upload that
stored or merged a ride restarts a RECALC_DEBOUNCE_SECONDS timer. The server's
debouncer is ``server.debouncer``, so whoever stops the server can flush it.

Plain HTTP, like Grafana's default; TLS is a reverse proxy's job. The
Authorization header and passwords are never logged.

See docs/design/specs/2026-11-07-file-import-design.md ("Upload page").
"""
from __future__ import annotations

import json
import os
import re
import socket
import tempfile
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Callable
from urllib.parse import unquote, urlsplit

import psycopg2

import db
import file_import
import fitness

MAX_UPLOAD_BYTES = 500 * 1024 * 1024
MAX_UPLOADS_IN_FLIGHT = 2
RECALC_DEBOUNCE_SECONDS = 20
CHUNK = 1024 * 1024
BUSY_RETRY_SECONDS = 2
# After refusing a request whose body was never read, keep reading and
# discarding for this long before closing. Closing a socket with unread data
# sends a reset, which can wipe the response from the client's buffers before
# the browser reads it, so it would see a network error instead of the 401.
LINGER_SECONDS = 2.0
# Idle limit on any one read or write, so a stalled client can't hold an
# upload slot forever.
SOCKET_TIMEOUT_SECONDS = 120
MAX_NAME_CHARS = 255
ACCEPTED_SUFFIXES = file_import.FILE_SUFFIXES + (".zip",)
REALM = 'Basic realm="VeloMate upload"'
PAGE_PATH = Path(__file__).with_name("upload_page.html")
CSP = ("default-src 'none'; style-src 'unsafe-inline'; "
       "script-src 'unsafe-inline'; connect-src 'self'; img-src 'self'; "
       "base-uri 'none'; form-action 'none'; frame-ancestors 'none'")

_CONTROL = re.compile(r"[\x00-\x1f\x7f]")


class RecalcDebouncer:
    """Run ``run`` once, ``delay`` seconds after the last ``poke``.

    ``flush`` runs pending work now, waiting for a run already in progress,
    and is what a shutdown calls. A failing run is logged, not raised: the
    ingestor's daily recalculation catches up anyway.
    """

    def __init__(self, run: Callable[[], None], delay: float):
        self._run = run
        self._delay = delay
        self._lock = threading.Lock()       # guards the fields below
        self._run_lock = threading.Lock()   # one run at a time
        self._timer: threading.Timer | None = None
        self._generation = 0
        self._pending = False

    def poke(self) -> None:
        with self._lock:
            if self._timer is not None:
                self._timer.cancel()
            self._generation += 1
            self._pending = True
            self._timer = threading.Timer(self._delay, self._fire,
                                          args=(self._generation,))
            self._timer.daemon = True
            self._timer.start()

    def _take(self, generation: int | None) -> bool:
        """Claim the pending work. ``generation`` None claims it whatever
        timer is armed; otherwise only that timer may."""
        with self._lock:
            if generation is not None and generation != self._generation:
                return False
            if self._timer is not None:
                self._timer.cancel()
                self._timer = None
            pending, self._pending = self._pending, False
            return pending

    def _fire(self, generation: int) -> None:
        with self._run_lock:
            if self._take(generation):
                self._run_safely()

    def flush(self) -> None:
        with self._run_lock:
            if self._take(None):
                self._run_safely()

    def _run_safely(self) -> None:
        try:
            self._run()
        except Exception as e:
            print(f"[upload] Fitness recalculation failed: {e}")


def _clean_name(raw: str | None) -> str | None:
    """The original file name's basename, or None when it isn't usable."""
    if raw is None:
        return None
    try:
        name = unquote(raw, errors="strict")
    except UnicodeDecodeError:
        return None
    name = name.replace("\\", "/").rsplit("/", 1)[-1].strip()
    if (not name or name in (".", "..") or len(name) > MAX_NAME_CHARS
            or _CONTROL.search(name)):
        return None
    return name


def _suffix(name: str) -> str:
    """The temp file's suffix: the name's extension, with .gz kept whole."""
    lower = name.lower()
    for suffix in sorted(ACCEPTED_SUFFIXES, key=len, reverse=True):
        if lower.endswith(suffix):
            return suffix
    return ""


def _loggable(text: str) -> str:
    return _CONTROL.sub("?", text)


def _first_line(e: Exception) -> str:
    """An exception's first line, fit for one log line: psycopg2 messages go
    on with the failing statement."""
    lines = str(e).strip().splitlines()
    return _loggable(lines[0]) if lines else type(e).__name__


class UploadHandler(BaseHTTPRequestHandler):
    server_version = "VeloMateUpload"
    sys_version = ""
    timeout = SOCKET_TIMEOUT_SECONDS
    _unread_body = False

    # ------------------------------------------------------------ plumbing

    def log_message(self, format, *args):
        # The default access log is not needed; the per-ride lines say what
        # happened. Nothing here ever sees the Authorization header.
        pass

    def _send_json(self, status: int, payload: dict, headers=()) -> None:
        data = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        for key, value in headers:
            self.send_header(key, value)
        if self._unread_body:
            self.send_header("Connection", "close")
            self.close_connection = True
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(data)

    def _error(self, status: int, message: str, headers=()) -> None:
        self._send_json(status, {"error": message}, headers)

    def _begin(self) -> None:
        self._unread_body = bool(
            self.headers.get("Transfer-Encoding")
            or self.headers.get("Content-Length", "0").strip() not in ("", "0"))

    def finish(self):
        try:
            super().finish()
        finally:
            if self._unread_body:
                self._linger()

    def _linger(self) -> None:
        """Half-close, then drain what the client is still sending (bounded),
        so the refusal reaches it instead of a connection reset. Nothing read
        here is kept."""
        sock = self.connection
        try:
            sock.shutdown(socket.SHUT_WR)
            deadline = time.monotonic() + LINGER_SECONDS
            while True:
                left = deadline - time.monotonic()
                if left <= 0:
                    return
                sock.settimeout(left)
                if not sock.recv(65536):
                    return
        except OSError:
            return

    def _authorise(self):
        """The sign-in decision, or None after answering a refusal."""
        decision = self.server.auth.check(self.headers.get("Authorization"))
        if decision.ok:
            return decision
        headers = [("WWW-Authenticate", REALM)] if decision.status == 401 else []
        self._error(decision.status, decision.reason, headers)
        return None

    def _route(self, method: str) -> str:
        return f"{method} {urlsplit(self.path).path}"

    # -------------------------------------------------------------- routes

    def do_GET(self):
        self._begin()
        if not self._authorise():
            return
        if self._route("GET") != "GET /":
            return self._error(404, "not found")
        page = self.server.page
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(page)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Security-Policy", CSP)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        if self._unread_body:
            self.send_header("Connection", "close")
            self.close_connection = True
        self.end_headers()
        self.wfile.write(page)

    def _not_found(self):
        self._begin()
        if self._authorise():
            self._error(404, "not found")

    do_HEAD = do_PUT = do_DELETE = do_PATCH = do_OPTIONS = _not_found

    def do_POST(self):
        self._begin()
        decision = self._authorise()
        if not decision:
            return
        if self._route("POST") != "POST /upload":
            return self._error(404, "not found")

        name = _clean_name(self.headers.get("X-Filename"))
        if name is None:
            return self._error(400, "missing or unusable X-Filename header")
        if not _suffix(name):
            return self._error(
                400, "only .fit, .gpx, .fit.gz, .gpx.gz and .zip files can be imported")
        if self.headers.get("Transfer-Encoding"):
            return self._error(411, "send the file with a Content-Length")
        raw_length = self.headers.get("Content-Length")
        if raw_length is None:
            return self._error(411, "Content-Length is required")
        if not re.fullmatch(r"[0-9]+", raw_length.strip()):
            return self._error(400, "bad Content-Length")
        # int() refuses more than 4300 digits; nothing near that is valid.
        if len(raw_length.strip()) > 15 or int(raw_length) > MAX_UPLOAD_BYTES:
            return self._error(
                413, "files over 500 MB go through the watched folder instead")
        length = int(raw_length)

        if not self.server.slots.acquire(blocking=False):
            return self._error(
                503, "the server is busy with other uploads, try again shortly",
                [("Retry-After", str(BUSY_RETRY_SECONDS))])
        try:
            reply = self._upload(decision.login, name, length)
            if reply is not None:
                try:
                    self._send_json(*reply)
                except (BrokenPipeError, ConnectionResetError):
                    # The browser left before the reply; the ride is stored.
                    self.close_connection = True
                    print(f"[upload] {decision.login}: {_loggable(name)}: "
                          "client disconnected before the reply")
        finally:
            self.server.slots.release()

    # -------------------------------------------------------------- upload

    def _upload(self, login: str, name: str, length: int):
        """Spool the body, import it and clean up. Returns the reply to send,
        or None when the client went away. The reply goes out only after the
        temp file is gone and any recalculation is queued."""
        try:
            tmp = tempfile.NamedTemporaryFile(
                delete=False, suffix=_suffix(name), dir=self.server.spool_dir)
        except OSError as e:
            print(f"[upload] Could not create a temporary file: {_first_line(e)}")
            return 503, {"error": "the upload could not be stored, try again shortly"}
        try:
            try:
                with tmp:
                    left = length
                    while left:
                        chunk = self.rfile.read(min(CHUNK, left))
                        if not chunk:
                            # The client went away; nobody is left to answer.
                            self.close_connection = True
                            return None
                        tmp.write(chunk)
                        left -= len(chunk)
            except OSError as e:
                # A full disk, or a client stalled past the socket timeout.
                print(f"[upload] {login}: {_loggable(name)}: receiving failed: "
                      f"{_first_line(e)}")
                return 503, {"error": "the upload could not be received, try again shortly"}
            self._unread_body = False
            return self._import(login, name, tmp.name)
        finally:
            try:
                os.unlink(tmp.name)
            except OSError:
                pass

    def _import(self, login: str, name: str, path: str):
        results = []
        conn = None
        try:
            conn = self.server.connect()
            file_import.import_file(conn, path, name, "upload",
                                    on_result=results.append)
        except (OSError, psycopg2.Error) as e:
            print(f"[upload] {login}: {_loggable(name)}: import stopped: "
                  f"{_first_line(e)}")
            reply = 503, {"error": "the file could not be imported right now, "
                                   "try again shortly"}
        except Exception:
            print(f"[upload] {login}: {_loggable(name)}: import failed:")
            traceback.print_exc()
            reply = 500, {"error": "the import failed unexpectedly"}
        else:
            reply = 200, {"results": [self._line(r) for r in results]}
        finally:
            if conn is not None:
                conn.close()
            # Rides stored before an error count too: they changed something.
            for r in results:
                note = " (already imported)" if r.from_ledger else ""
                print(f"[upload] {login}: {_loggable(r.name)}: {r.outcome}{note}")
            if any(r.outcome in file_import.CHANGED_OUTCOMES and not r.from_ledger
                   for r in results):
                self.server.debouncer.poke()
        return reply

    def _line(self, r) -> dict:
        link = None
        if r.activity_id is not None:
            link = (f"{self.server.grafana_root_url}d/velomate-activity/"
                    f"?var-activity_id={int(r.activity_id)}")
        return {"name": r.name, "outcome": r.outcome, "detail": r.detail,
                "from_ledger": r.from_ledger, "activity_id": r.activity_id,
                "link": link}


def prepare_spool_dir(path) -> str:
    """Create the upload spool directory and empty it: a file left by a killed
    process would otherwise stay for good. Only files in it are removed."""
    spool = Path(path)
    spool.mkdir(parents=True, exist_ok=True)
    for entry in spool.iterdir():
        try:
            if entry.is_file() or entry.is_symlink():
                entry.unlink()
        except OSError as e:
            print(f"[upload] Could not remove {_loggable(entry.name)} "
                  f"from the spool directory: {_first_line(e)}")
    return str(spool)


def make_server(host: str, port: int, *, auth,
                connect: Callable = db.get_connection,
                recalc: Callable = fitness.recalculate_fitness,
                grafana_root_url: str,
                debounce_seconds: float = RECALC_DEBOUNCE_SECONDS,
                spool_dir: str | None = None,
                ) -> ThreadingHTTPServer:
    """Build (not start) the upload server. ``server.debouncer`` holds the
    pending fitness recalculation; flush it when stopping the server."""

    def run_recalc():
        conn = connect()
        try:
            recalc(conn)
        finally:
            conn.close()
        print("[upload] Fitness recalculated")

    server = ThreadingHTTPServer((host, port), UploadHandler)
    server.spool_dir = prepare_spool_dir(
        spool_dir or os.path.join(tempfile.gettempdir(), "velomate-upload"))
    server.daemon_threads = True
    server.auth = auth
    server.connect = connect
    server.grafana_root_url = (grafana_root_url if grafana_root_url.endswith("/")
                               else grafana_root_url + "/")
    server.slots = threading.BoundedSemaphore(MAX_UPLOADS_IN_FLIGHT)
    server.debouncer = RecalcDebouncer(run_recalc, debounce_seconds)
    server.page = PAGE_PATH.read_bytes()
    return server
