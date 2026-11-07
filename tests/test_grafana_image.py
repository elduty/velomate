"""Grafana's dashboards and provisioning have to live in its image.

Bind-mounted, they went stale on every deploy that re-cloned the repo: the
container kept the deleted directories, and nothing recreated it, because its
compose config hadn't changed. Built in, a change to either changes the image,
which recreates the container.
"""
import json
import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
GRAFANA = yaml.safe_load((ROOT / "docker-compose.yml").read_text())["services"]["velomate-grafana"]
DOCKERFILE = (ROOT / "grafana" / "Dockerfile").read_text()


def test_grafana_is_built_from_the_repo():
    assert GRAFANA["build"]["context"] == "./grafana"
    assert "image" not in GRAFANA


def test_nothing_from_the_repo_is_mounted_into_grafana():
    assert [v for v in GRAFANA["volumes"] if v.startswith(".")] == []


def test_the_image_carries_provisioning_and_the_chosen_dashboards():
    assert re.search(r"^COPY provisioning /etc/grafana/provisioning$", DOCKERFILE, re.M)
    assert re.search(r"^COPY dashboards/\$\{VELOMATE_UNITS\} /etc/grafana/dashboards$", DOCKERFILE, re.M)


def test_compose_passes_every_build_argument_with_the_dockerfiles_default():
    args = GRAFANA["build"]["args"]
    defaults = dict(re.findall(r"^ARG (\w+)=(\S+)$", DOCKERFILE, re.M))
    assert set(args) == set(defaults)
    for name, default in defaults.items():
        assert args[name] == "${%s:-%s}" % (name, default)


def test_the_default_basemap_needs_no_api_key():
    """Grafana's own default, Carto, serves tiles reading "API KEY REQUIRED"."""
    value = GRAFANA["environment"]["GF_GEOMAP_DEFAULT_BASELAYER_CONFIG"]
    default = re.fullmatch(r"\$\{VELOMATE_MAP_BASELAYER:-(.+)\}", value).group(1)
    assert json.loads(default)["type"] == "osm-standard"
