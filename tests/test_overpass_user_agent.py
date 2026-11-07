"""Every Overpass request has to name its client.

overpass-api.de answers the default python-requests User-Agent with a 406, and each
of these functions swallows the error, so a plan just loses its POIs and scoring.
"""
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from velomate import route_intelligence as ri

COORDS = [(38.70 + i * 0.001, -9.10 - i * 0.001) for i in range(40)]

CALLS = {
    "get_pois": lambda: ri.get_pois(38.7, -9.1, 5),
    "verify_surface": lambda: ri.verify_surface(COORDS, "road"),
    "score_scenic": lambda: ri.score_scenic(COORDS),
    "find_cycling_trails": lambda: ri.find_cycling_trails(COORDS),
    "score_cycling_safety": lambda: ri.score_cycling_safety(COORDS),
}


@pytest.mark.parametrize("name", CALLS)
def test_the_request_names_the_client(name):
    response = MagicMock()
    response.json.return_value = {"elements": []}
    with patch.object(ri.requests, "post", return_value=response) as post, \
            patch.object(ri, "_overpass_throttle"):
        CALLS[name]()
    assert post.called
    for call in post.call_args_list:
        assert call.kwargs["headers"] == ri.OVERPASS_HEADERS


@pytest.mark.parametrize("name", CALLS)
def test_the_request_waits_as_long_as_the_server_takes(name):
    """Real answers took 33-35 s from here on 2026-11-06. A timeout under that loses
    the POIs and scoring with no sign of it, because these functions swallow it."""
    response = MagicMock()
    response.json.return_value = {"elements": []}
    with patch.object(ri.requests, "post", return_value=response) as post, \
            patch.object(ri, "_overpass_throttle"):
        CALLS[name]()
    assert post.called
    for call in post.call_args_list:
        assert call.kwargs["timeout"] == ri.OVERPASS_TIMEOUT_S > 35


def test_the_client_name_is_not_the_default():
    assert "python-requests" not in ri.OVERPASS_HEADERS["User-Agent"]
    assert ri.OVERPASS_HEADERS["User-Agent"].startswith("VeloMate")


def test_every_overpass_call_is_listed_here():
    source = Path(ri.__file__).read_text()
    assert source.count("requests.post(OVERPASS_URL") == len(CALLS)
