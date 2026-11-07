import socket
import sys
import webbrowser
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "ingestor"))

# Import the real `requests` and `psycopg2` first, when they are installed.
#
# Several test modules stand in a MagicMock for them so the suite can run without
# them. pytest imports every test module into one process, so whichever module is
# imported first wins for the whole session — and since nothing has imported them
# at that point, even `setdefault` installs the mock. The mock then leaks into
# unrelated modules, where `except requests.RequestException` is no longer a real
# exception class and comparisons against MagicMock raise TypeError, and a test
# passes or fails by which module happened to come first. Loading the genuine
# modules here makes every later `setdefault` a no-op, so there is one state in
# every order, while an environment genuinely missing them still falls back to
# the mock.
#
# schedule is deliberately left mocked, because tests assert against the mock's
# call records rather than any real behaviour.
try:
    import requests  # noqa: F401
except ImportError:
    pass

try:
    import psycopg2.extras  # noqa: F401
except ImportError:
    pass

LOOPBACK = {"127.0.0.1", "::1", "localhost", None}


@pytest.fixture(autouse=True)
def _offline(monkeypatch):
    """Keep every test off the real network and out of the browser.

    Refused the way an unreachable host refuses, so the code under test takes its
    real offline path, and a test needing a live service fails here instead of
    passing either way. Python only: a subprocess, or libpq, is not covered.
    """
    real_connect = socket.socket.connect
    real_getaddrinfo = socket.getaddrinfo

    def connect(self, address):
        if self.family == socket.AF_UNIX or address[0] in LOOPBACK:
            return real_connect(self, address)
        raise ConnectionRefusedError(f"blocked in tests: {address}")

    def getaddrinfo(host, *args, **kwargs):
        if host in LOOPBACK:
            return real_getaddrinfo(host, *args, **kwargs)
        raise socket.gaierror(socket.EAI_NONAME, f"blocked in tests: {host}")

    monkeypatch.setattr(socket.socket, "connect", connect)
    monkeypatch.setattr(socket, "getaddrinfo", getaddrinfo)
    monkeypatch.setattr(webbrowser, "open", lambda *args, **kwargs: False)
