import pytest
from velomate import units

def test_factors_reference_values():
    assert round(10 * units.KM_TO_MI, 4) == 6.2137
    assert round(100 * units.M_TO_FT, 2) == 328.08
    assert round(40 * units.KMH_TO_MPH, 4) == 24.8548
    assert units.c_to_f(0) == 32
    assert units.c_to_f(100) == 212

def test_grafana_unit_map():
    assert units.GRAFANA_UNIT_MAP["lengthkm"] == ("lengthmi", units.KM_TO_MI)
    assert units.GRAFANA_UNIT_MAP["lengthm"] == ("lengthft", units.M_TO_FT)
    assert units.GRAFANA_UNIT_MAP["velocitykmh"] == ("velocitymph", units.KMH_TO_MPH)
    assert "watt" not in units.GRAFANA_UNIT_MAP

@pytest.mark.parametrize("system,expected", [("metric", "15.0 km"), ("imperial", "9.3 mi")])
def test_format_distance(system, expected):
    assert units.format_distance(15.0, system) == expected

@pytest.mark.parametrize("system,expected", [("metric", "260 m"), ("imperial", "853 ft")])
def test_format_elevation(system, expected):
    assert units.format_elevation(260, system) == expected

@pytest.mark.parametrize("system,expected", [("metric", "30 km/h"), ("imperial", "19 mph")])
def test_format_speed(system, expected):
    assert units.format_speed(30, system) == expected

@pytest.mark.parametrize("system,expected", [("metric", "20°C"), ("imperial", "68°F")])
def test_format_temp(system, expected):
    assert units.format_temp(20, system) == expected

def test_unknown_system_falls_back_to_metric():
    assert units.normalize_system("IMPERIAL") == "imperial"
    assert units.normalize_system("nonsense") == "metric"
    assert units.normalize_system(None) == "metric"

@pytest.mark.parametrize("system,expected", [("metric", "5.0 mm"), ("imperial", "0.2 in")])
def test_format_precip(system, expected):
    assert units.format_precip(5.0, system) == expected
