"""Grafana sign-in check for the upload page, against a fake Grafana."""
import base64
import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "ingestor"))

from upload_auth import GrafanaAuth, AuthDecision  # noqa: E402


def basic(user="u", pw="p"):
    return "Basic " + base64.b64encode(f"{user}:{pw}".encode()).decode()


class FakeGrafana:
    def __init__(self):
        self.requests = []
        self.orgs = [{"orgId": 1, "name": "Main", "role": "Editor"}]
        self.is_admin = False
        self.user_status = 200
        self.orgs_status = 200
        self.valid = None  # accepted Authorization header, None = any
        self.login = "rider"
        self.user_raw = None  # raw body for /api/user, overrides the JSON


@pytest.fixture
def grafana():
    fake = FakeGrafana()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            fake.requests.append((self.path, self.headers.get("Authorization")))
            if fake.valid and self.headers.get("Authorization") != fake.valid:
                return self._send(401, {"message": "Invalid username or password"})
            if self.path == "/api/user":
                if fake.user_raw is not None:
                    return self._send_raw(200, fake.user_raw)
                body = {"login": fake.login, "isGrafanaAdmin": fake.is_admin}
                return self._send(fake.user_status, body)
            if self.path == "/api/user/orgs":
                return self._send(fake.orgs_status, fake.orgs)
            self._send(404, {})

        def _send(self, status, body):
            self._send_raw(status, json.dumps(body).encode())

        def _send_raw(self, status, data):
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    fake.url = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        yield fake
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def test_editor_in_org_1_is_allowed(grafana):
    d = GrafanaAuth(grafana.url).check(basic())
    assert d == AuthDecision(True, 200, "ok", "rider")


def test_admin_in_org_1_is_allowed(grafana):
    grafana.orgs = [{"orgId": 1, "name": "Main", "role": "Admin"}]
    d = GrafanaAuth(grafana.url).check(basic())
    assert d.ok and d.status == 200 and d.login == "rider"


def test_viewer_is_refused_403(grafana):
    grafana.orgs = [{"orgId": 1, "name": "Main", "role": "Viewer"}]
    d = GrafanaAuth(grafana.url).check(basic())
    assert not d.ok and d.status == 403


def test_server_admin_who_is_viewer_in_org_1_is_refused(grafana):
    grafana.is_admin = True
    grafana.orgs = [{"orgId": 1, "name": "Main", "role": "Viewer"}]
    d = GrafanaAuth(grafana.url).check(basic())
    assert not d.ok and d.status == 403


def test_role_only_in_another_org_is_refused(grafana):
    grafana.orgs = [{"orgId": 2, "name": "Other", "role": "Admin"}]
    d = GrafanaAuth(grafana.url).check(basic())
    assert not d.ok and d.status == 403


@pytest.mark.parametrize("header", [None, "", "Bearer abc", "Basic", "Basic !!!"])
def test_missing_or_bearer_header_is_401_without_calling_grafana(grafana, header):
    d = GrafanaAuth(grafana.url).check(header)
    assert not d.ok and d.status == 401
    assert grafana.requests == []


def test_bad_password_is_401(grafana):
    grafana.valid = basic("u", "right")
    d = GrafanaAuth(grafana.url).check(basic("u", "wrong"))
    assert not d.ok and d.status == 401


def test_grafana_down_is_502():
    import socket
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    d = GrafanaAuth(f"http://127.0.0.1:{port}", timeout=1).check(basic())
    assert not d.ok and d.status == 502


def test_grafana_500_is_502(grafana):
    grafana.user_status = 500
    d = GrafanaAuth(grafana.url).check(basic())
    assert not d.ok and d.status == 502


def test_orgs_500_is_502(grafana):
    grafana.orgs_status = 500
    d = GrafanaAuth(grafana.url).check(basic())
    assert not d.ok and d.status == 502


def test_success_is_cached_for_300_seconds(grafana):
    clock = Clock()
    auth = GrafanaAuth(grafana.url, clock=clock)
    assert auth.check(basic()).ok
    assert len(grafana.requests) == 2
    clock.t += 299
    assert auth.check(basic()).ok
    assert len(grafana.requests) == 2
    clock.t += 2  # 301 s after the first call
    assert auth.check(basic()).ok
    assert len(grafana.requests) == 4


def test_cache_is_per_credential(grafana):
    auth = GrafanaAuth(grafana.url, clock=Clock())
    auth.check(basic("a", "1"))
    auth.check(basic("b", "2"))
    assert len(grafana.requests) == 4


def test_failure_is_never_cached(grafana):
    grafana.orgs = [{"orgId": 1, "name": "Main", "role": "Viewer"}]
    auth = GrafanaAuth(grafana.url, clock=Clock())
    assert auth.check(basic()).status == 403
    n = len(grafana.requests)
    grafana.orgs = [{"orgId": 1, "name": "Main", "role": "Editor"}]
    assert auth.check(basic()).ok
    assert len(grafana.requests) > n


def test_forwards_the_header_unchanged(grafana):
    h = basic("rider", "pässword:with:colons")
    GrafanaAuth(grafana.url).check(h)
    assert [a for _, a in grafana.requests] == [h, h]
    assert [p for p, _ in grafana.requests] == ["/api/user", "/api/user/orgs"]


def _raw_server(handler_cls):
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler_cls)
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    return server, t


def _stop(server, t):
    server.shutdown()
    server.server_close()
    t.join()


def test_redirect_is_502_and_target_gets_nothing():
    seen = []

    class Target(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            seen.append(self.headers.get("Authorization"))
            data = b'{"login": "evil"}'
            self.send_response(200)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    target, tt = _raw_server(Target)
    loc = f"http://127.0.0.1:{target.server_address[1]}/api/user"

    class Redirector(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            self.send_response(302)
            self.send_header("Location", loc)
            self.send_header("Content-Length", "0")
            self.end_headers()

    red, rt = _raw_server(Redirector)
    try:
        d = GrafanaAuth(f"http://127.0.0.1:{red.server_address[1]}").check(basic())
    finally:
        _stop(red, rt)
        _stop(target, tt)
    assert not d.ok and d.status == 502
    assert seen == []


def test_garbage_status_line_is_502():
    class Bad(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            self.wfile.write(b"garbage\r\n\r\n")
            self.close_connection = True

    s, t = _raw_server(Bad)
    try:
        d = GrafanaAuth(f"http://127.0.0.1:{s.server_address[1]}").check(basic())
    finally:
        _stop(s, t)
    assert not d.ok and d.status == 502


def test_null_login_is_refused(grafana):
    grafana.login = None
    d = GrafanaAuth(grafana.url).check(basic())
    assert not d.ok and d.status == 502


def test_html_200_from_api_user_is_502(grafana):
    grafana.user_raw = b"<html>login</html>"
    d = GrafanaAuth(grafana.url).check(basic())
    assert not d.ok and d.status == 502


@pytest.mark.parametrize("orgs", [{"orgId": 1, "role": "Admin"}, "Admin"])
def test_wrong_shaped_orgs_is_502(grafana, orgs):
    grafana.orgs = orgs
    d = GrafanaAuth(grafana.url).check(basic())
    assert not d.ok and d.status == 502


def test_401_and_502_are_never_cached(grafana):
    auth = GrafanaAuth(grafana.url, clock=Clock())
    grafana.valid = basic("u", "right")
    assert auth.check(basic("u", "wrong")).status == 401
    n = len(grafana.requests)
    assert auth.check(basic("u", "wrong")).status == 401
    assert len(grafana.requests) > n
    grafana.valid = None
    grafana.user_status = 500
    assert auth.check(basic()).status == 502
    n = len(grafana.requests)
    assert auth.check(basic()).status == 502
    assert len(grafana.requests) > n
