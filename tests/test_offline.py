"""The fixture in conftest that keeps every test off the real network."""
import socket
import webbrowser

import pytest


def test_a_connection_to_another_host_is_refused():
    with socket.socket() as connection:
        connection.settimeout(0.2)
        with pytest.raises(ConnectionRefusedError, match="blocked"):
            connection.connect(("192.0.2.1", 80))  # reserved, never routed


def test_a_name_lookup_is_refused():
    with pytest.raises(socket.gaierror, match="blocked"):
        socket.getaddrinfo("example.invalid", 80)


def test_a_connection_by_name_is_refused():
    """The path every HTTP client takes."""
    with pytest.raises(OSError, match="blocked"):
        socket.create_connection(("example.invalid", 80), timeout=0.2)


def test_the_browser_is_replaced():
    """Checked without calling it: if this ever failed, a call would open a tab."""
    assert webbrowser.open.__module__ != "webbrowser"


def test_it_reports_the_browser_as_unavailable():
    if webbrowser.open.__module__ == "webbrowser":
        pytest.fail("the real browser is in place, so this test must not call it")

    assert webbrowser.open("https://example.invalid") is False


def test_loopback_stays_open():
    """The tests that run a local PostgreSQL or server need it."""
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        with socket.create_connection(listener.getsockname(), timeout=2):
            pass
